import os
import sys
sys.path.insert(0, "/opt/auto-gpt")
from core.config_store import config_store
api_url = config_store.get("oaipay_api_url", "")
api_key = config_store.get("oaipay_api_key", "")
import requests
base_url = api_url.split("/api/")[0].rstrip('/')
print("Fetching from:", base_url)
for path in ("/api/admin/cdk/categories", "/api/auto-gpt/categories"):
    url = f"{base_url}{path}"
    try:
        res = requests.get(url, headers={"Authorization": api_key}, timeout=10)
        print(url, res.status_code, res.text[:200])
    except Exception as e:
        print(e)
