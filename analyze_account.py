import json
from sqlmodel import Session, select
from core.db import engine, AccountModel
from services.chatgpt_core.cpa_upload import generate_token_json
from services.chatgpt_core.oaipay_upload import build_oaipay_account_payload

with Session(engine) as session:
    account = session.exec(select(AccountModel).where(AccountModel.email == '11.webcast-plumes@icloud.com')).first()
    if account:
        extra = getattr(account, "extra", {})
        if callable(extra):
            extra = extra()
        sync = extra.get("sync_statuses", {}).get("oaipay", {})
        
        token_data = generate_token_json(account)
        bound_phone = extra.get("chatgpt_bound_phone") or extra.get("chatgpt_phone_binding") or {}
        if isinstance(bound_phone, dict):
            api_url_val = bound_phone.get("api_url", "")
            if api_url_val:
                token_data["api_url"] = api_url_val
                token_data["phone"] = bound_phone.get("phone", "")
        
        print("====== OAIPAY SYNC STATE ======")
        print(json.dumps(sync, indent=2, ensure_ascii=False))
        print("\n====== GENERATED EXTRA INFO ======")
        print(json.dumps(token_data, indent=2, ensure_ascii=False))
        
        # also print basic account info
        print("\n====== BASIC INFO ======")
        print(f"Email: {account.email}")
        print(f"Password: {account.password}")
    else:
        print("Account not found.")
