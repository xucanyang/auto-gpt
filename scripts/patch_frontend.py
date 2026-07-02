import os

def patch_file(filepath, replacements):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    for search, replace in replacements:
        content = content.replace(search, replace)
        
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

# 1. taskTypes.ts
patch_file("frontend/src/lib/taskTypes.ts", [
    ("batch_sub2api_upload: 'Sub2API上传',", "batch_sub2api_upload: 'Sub2API上传',\n  batch_oaipay_upload: 'OAIPay上传',")
])

# 2. RegisterTaskModal.tsx
patch_file("frontend/src/features/auth/components/RegisterTaskModal.tsx", [
    ("taskModalMode: 'register' | 'resume_auth' | 'payment_link' | 'sub2api_upload' | 'baxigpt_cdk' | 'paypal_bind'", "taskModalMode: 'register' | 'resume_auth' | 'payment_link' | 'sub2api_upload' | 'oaipay_upload' | 'baxigpt_cdk' | 'paypal_bind'"),
    ("if (taskModalMode === 'sub2api_upload') {\n      return eligible > 0 ? `Sub2API 批量上传 (${eligible} 个)` : 'Sub2API 批量上传'\n    }", "if (taskModalMode === 'sub2api_upload') {\n      return eligible > 0 ? `Sub2API 批量上传 (${eligible} 个)` : 'Sub2API 批量上传'\n    }\n    if (taskModalMode === 'oaipay_upload') {\n      return eligible > 0 ? `OAIPay 批量上传 (${eligible} 个)` : 'OAIPay 批量上传'\n    }"),
])

# 3. Settings.tsx
patch_file("frontend/src/pages/Settings.tsx", [
    ("titles: ['CPA 面板', 'Sub2API 面板', 'CodexProxy'],", "titles: ['CPA 面板', 'Sub2API 面板', 'OAIPay 面板', 'CodexProxy'],"),
    ("CPA / CodexProxy / Sub2API 自动上传会被停用", "CPA / CodexProxy / Sub2API / OAIPay 自动上传会被停用"),
    ("{ key: 'sub2api_group_ids', label: '分组 ID', placeholder: '多个分组用英文逗号分隔，例如 2,4,8' },", "{ key: 'sub2api_group_ids', label: '分组 ID', placeholder: '多个分组用英文逗号分隔，例如 2,4,8' },"),
    ("""
      {
        title: 'CodexProxy',
""", """
      {
        title: 'OAIPay 面板',
        desc: '一键将账号推送到 OAIPay (gpt.cccy.me)',
        items: [
          { key: 'oaipay_api_url', label: 'API URL', placeholder: 'http://gpt.cccy.me/api/auto-gpt/upload' },
          { key: 'oaipay_api_key', label: 'API Key (管理员密码)', secret: true },
          { key: 'oaipay_group', label: '默认分组', placeholder: '例如: auto-gpt' },
        ]
      },
      {
        title: 'CodexProxy',
""")
])

# Copy OaipayOverviewPanel.tsx
with open("frontend/src/features/accounts/components/Sub2ApiOverviewPanel.tsx", "r", encoding="utf-8") as f:
    content = f.read()
content = content.replace("sub2api", "oaipay").replace("Sub2Api", "Oaipay").replace("Sub2API", "OAIPay")
with open("frontend/src/features/accounts/components/OaipayOverviewPanel.tsx", "w", encoding="utf-8") as f:
    f.write(content)

# 4. AccountsPage.tsx or wherever the panel is included. Let's find out where Sub2ApiOverviewPanel is imported.
