import os

def copy_and_patch(src, dst):
    with open(src, 'r', encoding='utf-8') as f:
        content = f.read()
    
    content = content.replace("sub2api", "oaipay")
    content = content.replace("Sub2api", "Oaipay")
    content = content.replace("Sub2API", "OAIPay")
    
    # Custom fix for the API URL in oaipay_upload.py
    # sub2api hits /api/v1/admin/accounts
    # oaipay hits /api/auto-gpt/upload
    content = content.replace('f"{api_url.rstrip(\'/\')}/api/v1/admin/accounts"', 'f"{api_url.rstrip(\'/\')}/api/auto-gpt/upload"')
    
    with open(dst, 'w', encoding='utf-8') as f:
        f.write(content)

copy_and_patch("services/sub2api_sync.py", "services/oaipay_sync.py")
copy_and_patch("services/chatgpt_core/sub2api_upload.py", "services/chatgpt_core/oaipay_upload.py")
print("Done copying services.")
