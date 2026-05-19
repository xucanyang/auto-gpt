"""外部系统同步（自动导入 / 回填）"""

from __future__ import annotations

from typing import Any

from services.chatgpt_sync import (
    _get_account_extra,
    persist_cpa_sync_result,
    upload_chatgpt_account_to_cpa,
)
from services.chatgpt_account_state import is_chatgpt_upload_ready


def _is_config_enabled(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def sync_account(account) -> list[dict[str, Any]]:
    """根据平台将账号同步到外部系统。"""
    from core.config_store import config_store

    platform = getattr(account, "platform", "")
    results: list[dict[str, Any]] = []

    if platform == "chatgpt":
        ready, gate_msg, _capabilities = is_chatgpt_upload_ready(account)
        if not ready:
            msg = gate_msg or "跳过上传：账号材料不完整"
            persist_cpa_sync_result(account, False, msg)
            return [{"name": "Upload Gate", "ok": False, "msg": msg}]

        # 贡献模式优先级最高：开启后仅上传到贡献服务器，避免重复上报到其它平台。
        contribution_enabled = _is_config_enabled(config_store.get("contribution_enabled", "0"))
        if contribution_enabled:
            contribution_url = str(config_store.get("contribution_server_url", "") or "").strip()
            contribution_key = str(config_store.get("contribution_key", "") or "").strip()
            if not contribution_url:
                msg = "Contribution 服务器地址未配置"
                persist_cpa_sync_result(account, False, msg)
                results.append({"name": "Contribution", "ok": False, "msg": msg})
                return results

            ok, msg = upload_chatgpt_account_to_cpa(
                account,
                api_url=contribution_url,
                api_key=contribution_key or None,
            )
            persist_cpa_sync_result(account, ok, msg)
            results.append({"name": "Contribution", "ok": ok, "msg": msg})
            return results

        cpa_url = str(config_store.get("cpa_api_url", "") or "").strip()
        if cpa_url:
            ok, msg = upload_chatgpt_account_to_cpa(account)
            persist_cpa_sync_result(account, ok, msg)
            results.append({"name": "CPA", "ok": ok, "msg": msg})

        codex_proxy_url = str(config_store.get("codex_proxy_url", "") or "").strip()
        if codex_proxy_url:
            upload_type = str(config_store.get("codex_proxy_upload_type", "at") or "at").strip().lower()
            extra = _get_account_extra(account)

            class _CP:
                pass

            cp = _CP()
            cp.access_token = extra.get("access_token") or account.token
            cp.refresh_token = extra.get("refresh_token", "")

            if upload_type == "rt":
                from services.chatgpt_core.cpa_upload import upload_to_codex_proxy
                ok, msg = upload_to_codex_proxy(cp)
                results.append({"name": "CodexProxy(RT)", "ok": ok, "msg": msg})
            else:
                from services.chatgpt_core.cpa_upload import upload_at_to_codex_proxy
                ok, msg = upload_at_to_codex_proxy(cp)
                results.append({"name": "CodexProxy(AT)", "ok": ok, "msg": msg})

        # Sub2API 自动同步统一走 backfill 逻辑，避免注册后重复上传且能写回探测状态。
        sub2api_url = str(config_store.get("sub2api_api_url", "") or "").strip()
        sub2api_key = str(config_store.get("sub2api_api_key", "") or "").strip()
        if sub2api_url and sub2api_key:
            from core.db import AccountModel, Session, engine, select
            from services.sub2api_sync import backfill_chatgpt_account_to_sub2api

            account_id = getattr(account, "id", None)
            if isinstance(account, AccountModel) and isinstance(account_id, int) and account_id > 0:
                with Session(engine) as session:
                    db_account = session.exec(select(AccountModel).where(AccountModel.id == account_id)).first()
                    if db_account is None:
                        outcome = {
                            "ok": False,
                            "message": f"本地账号不存在: #{account_id}",
                        }
                    else:
                        outcome = backfill_chatgpt_account_to_sub2api(db_account, session=session, commit=True)
            else:
                outcome = backfill_chatgpt_account_to_sub2api(account)

            results.append(
                {
                    "name": "Sub2API",
                    "ok": bool(outcome.get("ok")),
                    "msg": str(outcome.get("message") or ""),
                }
            )

    return results
