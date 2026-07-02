import os
import re

def patch_file(filepath, patterns):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    original_content = content
    for search_str, replace_str in patterns:
        content = content.replace(search_str, replace_str)
        
    if content != original_content:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Patched {filepath}")

# 1. db.py
patch_file("core/db.py", [
    ('sub2api_state: str = Field(default="unknown", index=True)', 'sub2api_state: str = Field(default="unknown", index=True)\n    oaipay_state: str = Field(default="unknown", index=True)'),
    ("sub2api_state TEXT NOT NULL DEFAULT 'unknown',", "sub2api_state TEXT NOT NULL DEFAULT 'unknown',\n                oaipay_state TEXT NOT NULL DEFAULT 'unknown',"),
    ('"sub2api_state": "TEXT NOT NULL DEFAULT \'unknown\'",', '"sub2api_state": "TEXT NOT NULL DEFAULT \'unknown\'",\n            "oaipay_state": "TEXT NOT NULL DEFAULT \'unknown\'",'),
    ('"ON account_list_state(sub2api_state)"\n        )', '"ON account_list_state(sub2api_state)"\n        )\n        conn.exec_driver_sql(\n            "CREATE INDEX IF NOT EXISTS idx_account_list_state_oaipay_state "\n            "ON account_list_state(oaipay_state)"\n        )'),
])

# 2. config.py
patch_file("api/config.py", [
    ('"sub2api_api_url",', '"sub2api_api_url",\n    "oaipay_api_url",'),
    ('"sub2api_api_key",', '"sub2api_api_key",\n    "oaipay_api_key",'),
    ('"sub2api_group_ids",', '"sub2api_group_ids",\n    "oaipay_group",'),
])

# 3. api/integrations.py
patch_file("api/integrations.py", [
    ('from services.sub2api_sync import backfill_chatgpt_account_to_sub2api, get_sub2api_sync_state', 'from services.sub2api_sync import backfill_chatgpt_account_to_sub2api, get_sub2api_sync_state\nfrom services.oaipay_sync import backfill_chatgpt_account_to_oaipay, get_oaipay_sync_state'),
    ('sub2api_state: str = ""', 'sub2api_state: str = ""\n    oaipay_state: str = ""'),
    ('sub2api_state=body.sub2api_state,', 'sub2api_state=body.sub2api_state,\n            oaipay_state=body.oaipay_state,'),
    ('if destination == "sub2api":\n                    state = get_sub2api_sync_state(row)', 'if destination == "sub2api":\n                    state = get_sub2api_sync_state(row)\n                elif destination == "oaipay":\n                    state = get_oaipay_sync_state(row)'),
    ('if destination == "sub2api":\n                        outcome = backfill_chatgpt_account_to_sub2api(row, session=s, commit=True)\n                        default_name = "Sub2API"', 'if destination == "sub2api":\n                        outcome = backfill_chatgpt_account_to_sub2api(row, session=s, commit=True)\n                        default_name = "Sub2API"\n                    elif destination == "oaipay":\n                        outcome = backfill_chatgpt_account_to_oaipay(row, session=s, commit=True)\n                        default_name = "OAIPay"'),
])

# 4. api/actions.py
patch_file("api/actions.py", [
    ('from services.sub2api_sync import backfill_chatgpt_account_to_sub2api, probe_chatgpt_sub2api_status, update_account_model_sub2api_sync', 'from services.sub2api_sync import backfill_chatgpt_account_to_sub2api, probe_chatgpt_sub2api_status, update_account_model_sub2api_sync\nfrom services.oaipay_sync import backfill_chatgpt_account_to_oaipay, probe_chatgpt_oaipay_status, update_account_model_oaipay_sync'),
    ('sub2api_state: str = ""', 'sub2api_state: str = ""\n    oaipay_state: str = ""'),
    ('if platform == "chatgpt" and action_id == "sync_sub2api_status":\n        sync_result = probe_chatgpt_sub2api_status(acc_model)\n        update_account_model_sub2api_sync(\n            acc_model, sync_result, session=session, commit=True\n        )\n        return {\"ok\": True, \"message\": \"已成功探测 Sub2API 状态\"}', 'if platform == "chatgpt" and action_id == "sync_sub2api_status":\n        sync_result = probe_chatgpt_sub2api_status(acc_model)\n        update_account_model_sub2api_sync(\n            acc_model, sync_result, session=session, commit=True\n        )\n        return {"ok": True, "message": "已成功探测 Sub2API 状态"}\n    if platform == "chatgpt" and action_id == "sync_oaipay_status":\n        sync_result = probe_chatgpt_oaipay_status(acc_model)\n        update_account_model_oaipay_sync(\n            acc_model, sync_result, session=session, commit=True\n        )\n        return {"ok": True, "message": "已成功探测 OAIPay 状态"}'),
    ('if platform == "chatgpt" and action_id == "upload_sub2api":\n        outcome = backfill_chatgpt_account_to_sub2api(acc_model, session=session, commit=False)', 'if platform == "chatgpt" and action_id == "upload_sub2api":\n        outcome = backfill_chatgpt_account_to_sub2api(acc_model, session=session, commit=False)\n    if platform == "chatgpt" and action_id == "upload_oaipay":\n        outcome = backfill_chatgpt_account_to_oaipay(acc_model, session=session, commit=False)'),
    ('sub2api_state=body.sub2api_state,', 'sub2api_state=body.sub2api_state,\n        oaipay_state=body.oaipay_state,'),
    ('def _execute_batch_sub2api_sync(accounts: list[AccountModel], session: Session) -> dict[str, Any]:\n    success = 0\n    total = len(accounts)\n    for acc_model in accounts:\n        sync_result = probe_chatgpt_sub2api_status(acc_model)\n        update_account_model_sub2api_sync(acc_model, sync_result, session=session, commit=False)\n        if sync_result.get("status") == "success":\n            success += 1\n    summary = f"{success}/{total}"\n    return {\n        "ok": True,\n        "message": f"Sub2API 状态同步完成：{summary}",\n        "summary": summary\n    }', 'def _execute_batch_sub2api_sync(accounts: list[AccountModel], session: Session) -> dict[str, Any]:\n    success = 0\n    total = len(accounts)\n    for acc_model in accounts:\n        sync_result = probe_chatgpt_sub2api_status(acc_model)\n        update_account_model_sub2api_sync(acc_model, sync_result, session=session, commit=False)\n        if sync_result.get("status") == "success":\n            success += 1\n    summary = f"{success}/{total}"\n    return {\n        "ok": True,\n        "message": f"Sub2API 状态同步完成：{summary}",\n        "summary": summary\n    }\n\ndef _execute_batch_oaipay_sync(accounts: list[AccountModel], session: Session) -> dict[str, Any]:\n    success = 0\n    total = len(accounts)\n    for acc_model in accounts:\n        sync_result = probe_chatgpt_oaipay_status(acc_model)\n        update_account_model_oaipay_sync(acc_model, sync_result, session=session, commit=False)\n        if sync_result.get("status") == "success":\n            success += 1\n    summary = f"{success}/{total}"\n    return {\n        "ok": True,\n        "message": f"OAIPay 状态同步完成：{summary}",\n        "summary": summary\n    }'),
    ('if platform == "chatgpt" and action_id in {"sync_cliproxyapi_status", "sync_sub2api_status"}:\n        if action_id == "sync_cliproxyapi_status":\n            batch_result = _execute_batch_cliproxyapi_sync(accounts, session)\n        else:\n            batch_result = _execute_batch_sub2api_sync(accounts, session)', 'if platform == "chatgpt" and action_id in {"sync_cliproxyapi_status", "sync_sub2api_status", "sync_oaipay_status"}:\n        if action_id == "sync_cliproxyapi_status":\n            batch_result = _execute_batch_cliproxyapi_sync(accounts, session)\n        elif action_id == "sync_sub2api_status":\n            batch_result = _execute_batch_sub2api_sync(accounts, session)\n        else:\n            batch_result = _execute_batch_oaipay_sync(accounts, session)'),
])

# 5. api/accounts.py
patch_file("api/accounts.py", [
    ('sub2api_sync = _build_sync_summary(sync_statuses.get("sub2api") if isinstance(sync_statuses.get("sub2api"), dict) else {})', 'sub2api_sync = _build_sync_summary(sync_statuses.get("sub2api") if isinstance(sync_statuses.get("sub2api"), dict) else {})\n    oaipay_sync = _build_sync_summary(sync_statuses.get("oaipay") if isinstance(sync_statuses.get("oaipay"), dict) else {})'),
    ('"sub2api": sub2api_sync,', '"sub2api": sub2api_sync,\n        "oaipay": oaipay_sync,'),
    ('"sub2api_remote_state": _safe_str(sub2api_sync.get("remote_state")),', '"sub2api_remote_state": _safe_str(sub2api_sync.get("remote_state")),\n        "oaipay_remote_state": _safe_str(oaipay_sync.get("remote_state")),'),
    ('"sub2apiSync": sub2api_sync,', '"sub2apiSync": sub2api_sync,\n        "oaipaySync": oaipay_sync,'),
    ('sub2api_state: Optional[str] = None,', 'sub2api_state: Optional[str] = None,\n    oaipay_state: Optional[str] = None,'),
    ('sub2api_state=sub2api_state,', 'sub2api_state=sub2api_state,\n        oaipay_state=oaipay_state,'),
])
print("Done patching Python files.")
