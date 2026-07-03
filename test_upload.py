import sys
from sqlmodel import Session, select
from core.db import engine, AccountModel
from services.chatgpt_core.oaipay_upload import upload_to_oaipay_detailed

with Session(engine) as session:
    account = session.exec(select(AccountModel).where(AccountModel.email.like('%11.webcast-plumes%'))).first()
    if not account:
        print("Account not found")
        sys.exit(1)
    
    print(f"Found account: {account.email}")
    print(f"Extra JSON: {account.extra_json}")
    
    # Upload
    res = upload_to_oaipay_detailed(account, api_url="http://172.17.0.1:8315", api_key="admin", group_ids=[])
    print(f"Upload result: {res}")
