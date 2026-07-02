import sqlite3
import json
import requests
import argparse
import os

def export_accounts_to_oaipay(db_path, oaipay_url, key, group):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Query accounts
    cursor.execute("SELECT id, email, password, chatgpt_local FROM account_list_state")
    rows = cursor.fetchall()
    
    accounts_payload = []
    for row in rows:
        email = row["email"]
        password = row["password"]
        chatgpt_local = row["chatgpt_local"]
        
        # Parse tokens
        token = ""
        if chatgpt_local:
            try:
                local_data = json.loads(chatgpt_local)
                # usually access_token or token is in local_data
                token = local_data.get("token", "") or local_data.get("access_token", "")
            except:
                pass
        
        acc_info = {
            "email": email,
            "password": password,
            "extra_info": token
        }
        accounts_payload.append(acc_info)
        
    print(f"Found {len(accounts_payload)} accounts. Uploading to {oaipay_url}...")
    
    payload = {
        "key": key,
        "group": group,
        "accounts": accounts_payload
    }
    
    try:
        url = oaipay_url.rstrip("/")
        if not url.endswith("/api/auto-gpt/upload") and not url.endswith("/api/cdk/accounts/upload"):
            url = f"{url}/api/auto-gpt/upload"
            
        res = requests.post(url, json=payload, headers={"Authorization": f"Bearer {key}"}, timeout=30)
        print(f"Status Code: {res.status_code}")
        print(f"Response: {res.text}")
    except Exception as e:
        print(f"Error during upload: {e}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Upload accounts to OAIPay (gpt.cccy.me)")
    parser.add_argument("--db", default="../account_manager.db", help="Path to account_manager.db")
    parser.add_argument("--url", required=True, help="OAIPay URL (e.g. http://127.0.0.1:8080)")
    parser.add_argument("--key", required=True, help="Upload Key / Admin Password")
    parser.add_argument("--group", required=True, help="Group / Category name")
    args = parser.parse_args()
    
    db_path = args.db
    if not os.path.exists(db_path) and os.path.exists("account_manager.db"):
        db_path = "account_manager.db"
    
    export_accounts_to_oaipay(db_path, args.url, args.key, args.group)
