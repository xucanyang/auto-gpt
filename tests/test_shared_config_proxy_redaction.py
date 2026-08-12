import json
import sqlite3

from core.shared_config import SharedConfigStore


def test_shared_config_audit_redacts_proxy_template_credentials(tmp_path):
    db_path = tmp_path / "shared_config.db"
    store = SharedConfigStore(db_path)
    proxy_url = "socks5://audit-user:audit-password@proxy.example:3010"

    store.write({"dynamic_proxy_template": proxy_url}, updated_by="test", action="update")
    audit = store.audit(limit=10)
    dumped = json.dumps(audit, ensure_ascii=False)

    assert "audit-user" not in dumped
    assert "audit-password" not in dumped
    assert "proxy.example" not in dumped
    assert audit[0]["diff"]["dynamic_proxy_template"]["after"]["present"] is True
    assert audit[0]["diff"]["dynamic_proxy_template"]["after"]["length"] == len(proxy_url)


def test_shared_config_audit_api_view_and_scrub_hide_historical_proxy_url(tmp_path):
    db_path = tmp_path / "shared_config.db"
    store = SharedConfigStore(db_path)
    store.write({"task_proxy_mode": "dynamic"}, updated_by="test", action="seed")
    proxy_url = "http://legacy-user:legacy-password@legacy-proxy.example:8080"
    raw_diff = json.dumps(
        {
            "task_proxy_url": {
                "before": "",
                "after": proxy_url,
            }
        },
        ensure_ascii=False,
    )

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE shared_config_audit SET diff_json = ?", (raw_diff,))
        conn.commit()

    exposed = json.dumps(store.audit(limit=10), ensure_ascii=False)
    assert "legacy-user" not in exposed
    assert "legacy-password" not in exposed
    assert "legacy-proxy.example" not in exposed

    result = store.redact_legacy_audit()
    assert result == {"scanned": 1, "redacted": 1}

    with sqlite3.connect(db_path) as conn:
        persisted = conn.execute("SELECT diff_json FROM shared_config_audit").fetchone()[0]
    assert "legacy-user" not in persisted
    assert "legacy-password" not in persisted
    assert "legacy-proxy.example" not in persisted
    assert json.loads(persisted)["task_proxy_url"]["after"]["present"] is True


def test_shared_config_audit_redacts_miyaip_credentials(tmp_path):
    db_path = tmp_path / "shared_config.db"
    store = SharedConfigStore(db_path)
    crc = "crc-sensitive-value"
    key_name = "key-sensitive-value"

    store.write(
        {"miyaip_crc": crc, "miyaip_key_name": key_name},
        updated_by="test",
        action="update",
    )
    audit = store.audit(limit=10)
    dumped = json.dumps(audit, ensure_ascii=False)

    assert crc not in dumped
    assert key_name not in dumped
    diff = audit[0]["diff"]
    assert diff["miyaip_crc"]["after"]["length"] == len(crc)
    assert diff["miyaip_key_name"]["after"]["length"] == len(key_name)
