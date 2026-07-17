from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "docker-compose.multi.yml").read_text(encoding="utf-8")
NGINX_ROOT = ROOT / "ops" / "nginx"


def test_business_and_cliproxy_ports_are_loopback_only():
    expected_bindings = {
        "127.0.0.1:8000:8000",
        "127.0.0.1:8001:8000",
        "127.0.0.1:8003:8000",
        "127.0.0.1:${CLIPROXYAPI_PORT_BIND:-8317}:8317",
        "127.0.0.1:${CLIPROXYAPI_PORT_BIND_PLUS:-8318}:8317",
        "127.0.0.1:${CLIPROXYAPI_PORT_BIND_PLUS2:-8320}:8317",
    }
    for binding in expected_bindings:
        assert f'- "{binding}"' in COMPOSE

    for public_binding in (
        '- "8000:8000"',
        '- "8001:8000"',
        '- "8003:8000"',
        '- "${CLIPROXYAPI_PORT_BIND:-8317}:8317"',
        '- "${CLIPROXYAPI_PORT_BIND_PLUS:-8318}:8317"',
        '- "${CLIPROXYAPI_PORT_BIND_PLUS2:-8320}:8317"',
    ):
        assert public_binding not in COMPOSE


def test_nginx_admin_log_format_never_persists_query_strings():
    source = (NGINX_ROOT / "00-auto-gpt-security.conf").read_text(encoding="utf-8")
    log_block = source.split("log_format auto_gpt_admin", 1)[1].split(";", 1)[0]

    assert "$uri" in log_block
    assert "$host" in log_block
    assert "$upstream_addr" in log_block
    assert "$request_uri" not in log_block
    assert "$args" not in log_block
    assert re.search(r"\$request(?!_)", log_block) is None
    assert "access_token=" in source


def test_only_cloudflare_fronted_vhost_trusts_cf_connecting_ip():
    main = (NGINX_ROOT / "vhosts" / "auto-gpt.cccy.me.conf").read_text(encoding="utf-8")
    plus = (NGINX_ROOT / "vhosts" / "auto-plus.cccy.me.conf").read_text(encoding="utf-8")
    plus2 = (NGINX_ROOT / "vhosts" / "auto-plus2.cccy.me.conf").read_text(encoding="utf-8")
    real_ip_include = "include /etc/nginx/snippets/auto-gpt-cloudflare-realip.conf;"

    assert real_ip_include in main
    assert real_ip_include not in plus
    assert real_ip_include not in plus2
    assert "real_ip_header CF-Connecting-IP;" in (
        NGINX_ROOT / "snippets" / "auto-gpt-cloudflare-realip.conf"
    ).read_text(encoding="utf-8")


def test_each_vhost_has_independent_safe_logs_and_query_token_rejection():
    for hostname in ("auto-gpt.cccy.me", "auto-plus.cccy.me", "auto-plus2.cccy.me"):
        source = (NGINX_ROOT / "vhosts" / f"{hostname}.conf").read_text(encoding="utf-8")
        assert f"access_log /var/log/nginx/{hostname}.access.log auto_gpt_admin;" in source
        assert f"error_log /var/log/nginx/{hostname}.error.log warn;" in source
        assert "if ($auto_gpt_reject_query_token) { return 400; }" in source
        assert "limit_req zone=auto_gpt_auth" in source
        auth_location = source.split("location ^~ /api/auth/", 1)[1].split("location /", 1)[0]
        assert "client_max_body_size 16k;" in auth_location
        assert "include /etc/nginx/snippets/auto-gpt-proxy-common.conf;" in auth_location

def test_nginx_installer_activates_main_vhost_and_removes_legacy_duplicate():
    source = (ROOT / "scripts" / "install-auto-gpt-nginx-security.sh").read_text(
        encoding="utf-8"
    )

    assert 'ACTIVE_MAIN_CONF="/etc/nginx/conf.d/01-auto-gpt.cccy.me.conf"' in source
    assert 'LEGACY_COMBINED_CONF="/etc/nginx/conf.d/cccy-apps.conf"' in source
    assert "strip_legacy_auto_gpt_vhost" in source
    assert "assert_active_main_vhost" in source
    assert "conf.d/managed/auto-gpt.cccy.me.conf" not in source

