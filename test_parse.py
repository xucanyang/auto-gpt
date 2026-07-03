import json
extra_json = '''{
"chatgpt_phone_binding": {"phone": "+13026878939", "api_url": "https://sms.aa8.pl/api/record?token=3k8eqn2a0j66lpvn5kuc8bhftd7k6beshw3m"}
}'''
extra = json.loads(extra_json)
bound_phone = extra.get("chatgpt_bound_phone") or extra.get("chatgpt_phone_binding") or {}
phone_val = ""
api_url_val = ""
if isinstance(bound_phone, dict):
    api_url_val = bound_phone.get("api_url", "")
    phone_val = bound_phone.get("phone", "")
print("phone_val:", phone_val)
print("api_url_val:", api_url_val)
