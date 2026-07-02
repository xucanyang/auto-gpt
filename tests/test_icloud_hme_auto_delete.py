"""Tests for iCloud HME 自动删除 worker 的分类与两段式删除逻辑。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from sqlmodel import Session, SQLModel, create_engine, select

    from core import db as core_db
    from core.db import (
        AccountModel,
        IcloudHmeAliasModel,
        bulk_enable_icloud_hme_aliases,
        list_icloud_hme_deletion_candidates,
        set_icloud_hme_alias_enabled,
    )
    import services.icloud_hme_auto_delete as auto_delete
    from services.icloud_hme_auto_delete import (
        IcloudHmeAutoDeleteConfig,
        _classify_recheck_result,
    )

    HAS_DEPS = True
except ModuleNotFoundError as exc:  # pragma: no cover
    if exc.name not in {"sqlmodel", "fastapi", "pydantic"}:
        raise
    HAS_DEPS = False


def _make_config(**overrides):
    base = dict(
        enabled=True,
        account_interval_min_minutes=0,
        account_interval_max_minutes=0,
        max_per_run=10,
        rate_limit_backoff_minutes=60,
        error_backoff_minutes=1,
        recheck_before_delete=True,
        pause_active_tasks=False,
        dead_statuses={"account_deactivated", "password_invalid"},
        icloud_cookie="ck",
        icloud_domain_base="icloud.com",
    )
    base.update(overrides)
    return IcloudHmeAutoDeleteConfig(**base)


class _FakeClient:
    def __init__(self):
        self.deactivated: list[str] = []
        self.deleted: list[str] = []

    def deactivate(self, *, anonymous_id):
        self.deactivated.append(anonymous_id)

    def delete(self, *, anonymous_id):
        self.deleted.append(anonymous_id)


@unittest.skipUnless(HAS_DEPS, "db deps not installed in this environment")
class IcloudHmeAutoDeleteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "auto_delete.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        self._patch = mock.patch.object(core_db, "engine", self.engine)
        self._patch.start()
        SQLModel.metadata.create_all(self.engine)
        core_db.init_db()
        # 重置 worker 模块级状态，避免用例间相互影响
        auto_delete._rate_limit_until = 0.0
        auto_delete._error_backoff_until = 0.0
        auto_delete._consecutive_error_count = 0
        auto_delete._next_run_at = 0.0
        auto_delete._last_error = ""
        auto_delete._last_backoff_reason = ""
        auto_delete._stop_event.clear()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _add_alias(self, *, anonymous_id, hme, **kw):
        defaults = dict(
            enabled=False,
            status="reserved",
            purpose="chatgpt_register",
            bound_service="chatgpt",
            bound_account_email="",
            task_id="",
        )
        defaults.update(kw)
        with Session(self.engine) as s:
            row = IcloudHmeAliasModel(anonymous_id=anonymous_id, hme=hme, **defaults)
            s.add(row)
            s.commit()

    def _add_account(self, *, email, status="registered") -> int:
        with Session(self.engine) as s:
            row = AccountModel(platform="chatgpt", email=email, password="pw", status=status)
            s.add(row)
            s.commit()
            s.refresh(row)
            return int(row.id or 0)

    def _alias_status(self, anonymous_id) -> str | None:
        with Session(self.engine) as s:
            row = s.exec(
                select(IcloudHmeAliasModel).where(IcloudHmeAliasModel.anonymous_id == anonymous_id)
            ).first()
            return row.status if row else None

    def _alias_payload(self, anonymous_id) -> dict | None:
        with Session(self.engine) as s:
            row = s.exec(
                select(IcloudHmeAliasModel).where(IcloudHmeAliasModel.anonymous_id == anonymous_id)
            ).first()
            if row is None:
                return None
            return {
                "status": row.status,
                "enabled": bool(row.enabled),
                "last_error": row.last_error,
            }

    # ---- 分类器 ----

    def test_classifier_buckets(self):
        self._add_account(email="alive@icloud.com", status="registered")
        dead_id = self._add_account(email="DEAD@icloud.com", status="invalid")  # 大写测归一化
        self._add_alias(anonymous_id="ready", hme="ready@icloud.com", enabled=True, status="reserved")
        self._add_alias(anonymous_id="inuse", hme="inuse@icloud.com", status="in_use")
        self._add_alias(anonymous_id="task", hme="task@icloud.com", status="reserved", enabled=True, task_id="t1")
        self._add_alias(anonymous_id="orph", hme="orphan@icloud.com", status="register_failed")
        self._add_alias(
            anonymous_id="balive", hme="alive@icloud.com", status="registered", bound_account_email="alive@icloud.com"
        )
        self._add_alias(
            anonymous_id="bdead", hme="dead@icloud.com", status="registered", bound_account_email="dead@icloud.com"
        )
        self._add_alias(anonymous_id="ret", hme="ret@icloud.com", status="retired")

        result = list_icloud_hme_deletion_candidates()
        summary = result["summary"]
        self.assertEqual(summary["ready_stock"], 1)
        self.assertEqual(summary["in_flight"], 2)  # inuse + task_id
        self.assertEqual(summary["orphan"], 1)
        self.assertEqual(summary["account_alive"], 1)
        self.assertEqual(summary["bound_invalid"], 1)
        self.assertEqual(summary["retired"], 1)

        self.assertEqual({x["anonymous_id"] for x in result["orphan"]}, {"orph"})
        self.assertEqual(len(result["bound_invalid"]), 1)
        self.assertEqual(result["bound_invalid"][0]["anonymous_id"], "bdead")
        self.assertEqual(result["bound_invalid"][0]["account_ids"], [dead_id])
        self.assertEqual({x["anonymous_id"] for x in result["candidates"]}, {"orph", "bdead"})

    def test_bulk_enable_does_not_recycle_deactivated_register_failure(self):
        self._add_alias(
            anonymous_id="dead-failed",
            hme="dead-failed@icloud.com",
            forward_to="b@example.com",
            enabled=False,
            status="register_failed",
            last_error="HTTP 403: You do not have an account because it has been deleted or deactivated.",
        )
        self._add_alias(
            anonymous_id="ordinary-failed",
            hme="ordinary-failed@icloud.com",
            forward_to="b@example.com",
            enabled=False,
            status="register_failed",
            last_error="验证码超时",
        )

        result = bulk_enable_icloud_hme_aliases(forward_to="b@example.com")

        self.assertEqual(result["recycled"], 1)
        self.assertEqual(self._alias_payload("dead-failed"), {
            "status": "register_failed",
            "enabled": False,
            "last_error": "HTTP 403: You do not have an account because it has been deleted or deactivated.",
        })
        self.assertEqual(self._alias_payload("ordinary-failed")["status"], "reserved")
        self.assertTrue(self._alias_payload("ordinary-failed")["enabled"])

    def test_deactivated_alias_cannot_be_enabled_manually(self):
        self._add_alias(
            anonymous_id="dead-disabled",
            hme="dead-disabled@icloud.com",
            enabled=False,
            status="account_deactivated",
            last_error="account_deactivated",
        )

        with self.assertRaisesRegex(ValueError, "账号已禁用"):
            set_icloud_hme_alias_enabled("dead-disabled", True)

        self.assertFalse(self._alias_payload("dead-disabled")["enabled"])

    # ---- 失效测活结论映射 ----

    def test_classify_recheck_result(self):
        dead = {"account_deactivated", "password_invalid"}
        self.assertEqual(_classify_recheck_result({"ok": True, "data": {"status": "registered"}}, dead), "alive")
        self.assertEqual(_classify_recheck_result({"ok": False, "data": {"error_code": "password_invalid"}}, dead), "dead")
        self.assertEqual(_classify_recheck_result({"ok": False, "data": {"error_code": "account_deactivated"}}, dead), "dead")
        self.assertEqual(_classify_recheck_result({"ok": False, "data": {"error_code": "network_failed", "retryable": True}}, dead), "keep")
        self.assertEqual(_classify_recheck_result({"ok": False, "data": {"error_code": "login_blocked"}}, dead), "keep")
        self.assertEqual(_classify_recheck_result(None, dead), "keep")

    # ---- 两段式 run_once：每个候选删前都测活，选择性删除 ----

    def test_run_once_recheck_selective_delete(self):
        # 失效绑定：账号已存在且 invalid（worker 用其已有 account_id 测活）
        self._add_account(email="bdead@icloud.com", status="invalid")
        self._add_alias(anonymous_id="b_dead", hme="bdead@icloud.com", status="registered", bound_account_email="bdead@icloud.com")
        # 孤儿：无账号行（worker 会临时建行探测；存活则保留、否则清理）
        self._add_alias(anonymous_id="o_dead", hme="odead@icloud.com", status="register_failed")
        self._add_alias(anonymous_id="o_alive", hme="oalive@icloud.com", status="registered", bound_account_email="oalive@icloud.com")
        self._add_alias(anonymous_id="o_trans", hme="otrans@icloud.com", status="register_failed")
        # 保护：待用库存
        self._add_alias(anonymous_id="ready", hme="r@icloud.com", enabled=True, status="reserved")

        verdict_by_email = {
            "bdead@icloud.com": {"ok": False, "data": {"error_code": "password_invalid"}},
            "odead@icloud.com": {"ok": False, "data": {"error_code": "account_deactivated"}},
            "oalive@icloud.com": {"ok": True, "data": {"status": "registered"}},
            "otrans@icloud.com": {"ok": False, "data": {"error_code": "network_failed", "retryable": True}},
        }

        def fake_recheck(account_id, **kw):
            with Session(self.engine) as s:
                acc = s.get(AccountModel, account_id)
                email = acc.email if acc else ""
            return verdict_by_email.get(email, {"ok": False, "data": {"error_code": "unknown_error"}})

        fake_client = _FakeClient()
        with (
            mock.patch.object(auto_delete, "get_icloud_hme_auto_delete_config", return_value=_make_config()),
            mock.patch.object(auto_delete, "_make_client", return_value=fake_client),
            mock.patch.object(auto_delete, "_active_task_snapshots", return_value=[]),
            mock.patch(
                "services.chatgpt_core.invalid_account_recheck.recheck_invalid_chatgpt_account",
                side_effect=fake_recheck,
            ),
        ):
            result = auto_delete.run_once(force=True, delete=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["candidate_total"], 4)  # 3 孤儿 + 1 失效绑定
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["kept_alive"], 1)
        self.assertEqual(result["skipped"], 1)
        # 只删确认失效的（孤儿测活=account_deactivated、绑定测活=password_invalid）
        self.assertEqual(set(fake_client.deleted), {"b_dead", "o_dead"})
        self.assertEqual(set(fake_client.deactivated), {"b_dead", "o_dead"})
        # 存活 / 临时失败 / 库存 都不删
        self.assertNotIn("o_alive", fake_client.deleted)
        self.assertNotIn("o_trans", fake_client.deleted)
        self.assertNotIn("ready", fake_client.deleted)
        self.assertEqual(self._alias_status("b_dead"), "retired")
        self.assertEqual(self._alias_status("o_dead"), "retired")
        self.assertEqual(self._alias_status("o_alive"), "registered")
        self.assertEqual(self._alias_status("o_trans"), "register_failed")
        self.assertEqual(self._alias_status("ready"), "reserved")

    # ---- 单次上限 ----

    def test_run_once_respects_max_per_run(self):
        for i in range(5):
            self._add_alias(anonymous_id=f"o{i}", hme=f"o{i}@icloud.com", status="register_failed")

        fake_client = _FakeClient()
        with (
            mock.patch.object(
                auto_delete,
                "get_icloud_hme_auto_delete_config",
                return_value=_make_config(max_per_run=2, recheck_before_delete=False),
            ),
            mock.patch.object(auto_delete, "_make_client", return_value=fake_client),
            mock.patch.object(auto_delete, "_active_task_snapshots", return_value=[]),
        ):
            result = auto_delete.run_once(force=True, delete=True)

        self.assertEqual(result["candidate_total"], 5)
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["capped"], 3)
        self.assertEqual(len(fake_client.deleted), 2)

    # ---- 未配置 cookie 时安全失败 ----

    def test_run_once_requires_cookie(self):
        self._add_alias(anonymous_id="orph", hme="orphan@icloud.com", status="register_failed")
        with (
            mock.patch.object(
                auto_delete,
                "get_icloud_hme_auto_delete_config",
                return_value=_make_config(icloud_cookie=""),
            ),
            mock.patch.object(auto_delete, "_active_task_snapshots", return_value=[]),
        ):
            result = auto_delete.run_once(force=True, delete=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "non_retryable_error")
        self.assertEqual(self._alias_status("orph"), "register_failed")  # 未删

    def test_run_once_rate_limit_uses_long_backoff_as_next_run(self):
        from core.base_mailbox import ICloudAliasLimitError

        self._add_alias(anonymous_id="orph", hme="orphan@icloud.com", status="register_failed")

        class RateLimitedClient(_FakeClient):
            def delete(self, *, anonymous_id):
                raise ICloudAliasLimitError("rate limit", retry_after=120)

        with (
            mock.patch.object(
                auto_delete,
                "get_icloud_hme_auto_delete_config",
                return_value=_make_config(rate_limit_backoff_minutes=1, recheck_before_delete=False),
            ),
            mock.patch.object(auto_delete, "_make_client", return_value=RateLimitedClient()),
            mock.patch.object(auto_delete, "_active_task_snapshots", return_value=[]),
            mock.patch.object(auto_delete, "_now", return_value=1000.0),
        ):
            result = auto_delete.run_once(force=True, delete=True)

        self.assertFalse(result["ok"], result)
        self.assertTrue(result["rate_limited"])
        self.assertEqual(auto_delete._rate_limit_until, 1120.0)
        self.assertEqual(auto_delete._next_run_at, 1120.0)

    def test_run_once_delete_error_uses_short_backoff(self):
        self._add_alias(anonymous_id="orph", hme="orphan@icloud.com", status="register_failed")

        class FailingClient(_FakeClient):
            def delete(self, *, anonymous_id):
                raise TimeoutError("temporary network error")

        with (
            mock.patch.object(
                auto_delete,
                "get_icloud_hme_auto_delete_config",
                return_value=_make_config(error_backoff_minutes=1, recheck_before_delete=False),
            ),
            mock.patch.object(auto_delete, "_make_client", return_value=FailingClient()),
            mock.patch.object(auto_delete, "_active_task_snapshots", return_value=[]),
            mock.patch.object(auto_delete, "_now", return_value=1000.0),
        ):
            result = auto_delete.run_once(force=True, delete=True)

        self.assertFalse(result["ok"], result)
        self.assertTrue(result["ordinary_error"])
        self.assertEqual(auto_delete._error_backoff_until, 1060.0)
        self.assertEqual(auto_delete._next_run_at, 1060.0)


if __name__ == "__main__":
    unittest.main()
