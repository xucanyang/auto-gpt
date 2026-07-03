import sqlite3
import json
import argparse
import urllib.request
import urllib.error

def export_accounts_to_oaipay(db_path, oaipay_url, key, group):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Try querying accounts table which has extra_json
    try:
        cursor.execute("SELECT id, email, password, extra_json FROM accounts")
    except sqlite3.OperationalError:
        try:
            cursor.execute("SELECT id, email, password, chatgpt_local FROM account_list_state")
        except sqlite3.OperationalError:
            print("Cannot find suitable table to extract accounts")
            return

    rows = cursor.fetchall()
    
    accounts_payload = []
    for row in rows:
        acc_id, email, password, extra_data = row
        token = ""
        phone = ""
        phone_api = ""
        
        if extra_data:
            try:
                local_data = json.loads(extra_data)
                
                # token could be in token, access_token, or inside chatgpt_local
                token = local_data.get("token", "") or local_data.get("access_token", "")
                if not token and "chatgpt_local" in local_data:
                    c_local = local_data["chatgpt_local"]
                    if isinstance(c_local, str):
                        c_local = json.loads(c_local)
                    token = c_local.get("token", "") or c_local.get("access_token", "")
                    
                # phone is in chatgpt_bound_phone or chatgpt_phone_binding
                bound_phone = local_data.get("chatgpt_bound_phone") or local_data.get("chatgpt_phone_binding") or {}
                if isinstance(bound_phone, dict):
                    phone = bound_phone.get("phone", "")
                    phone_api = bound_phone.get("api_url", "")
                    
            except Exception as e:
                pass
        
        acc_info = {
            "email": email,
            "password": password,
            "extra_info": token
        }
        if phone:
            acc_info["phone"] = phone
        if phone_api:
            acc_info["phone_api"] = phone_api
            
        accounts_payload.append(acc_info)
        
    print(f"Found {len(accounts_payload)} accounts. Uploading to {oaipay_url}...")
    
    payload = {
        "key": key,
        "group": group,
        "accounts": accounts_payload
    }
    
    try:
        req = urllib.request.Request(
            oaipay_url,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                "Content-Type": "application/json",
                "x-api-key": key,
                "Authorization": f"Bearer {key}"
            },
            method="POST"
        )
        with urllib.request.urlopen(req) as response:
            res_body = response.read().decode('utf-8')
            print(f"Response: {response.status} {res_body}")
    except urllib.error.URLError as e:
        print(f"Failed to upload: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/account_manager.db", help="Path to sqlite db")
    parser.add_argument("--url", required=True, help="OAIPay api upload url")
    parser.add_argument("--key", required=True, help="OAIPay api key")
    parser.add_argument("--group", required=True, help="OAIPay category group")
    args = parser.parse_args()
    
    export_accounts_to_oaipay(args.db, args.url, args.key, args.group)
