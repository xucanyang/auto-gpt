import re
with open("frontend/src/features/accounts/components/AccountsToolbar.tsx", "r", encoding="utf-8") as f:
    content = f.read()
if "accountsCount?: number" not in content:
    content = content.replace("total: number", "total: number\n  accountsCount?: number")
with open("frontend/src/features/accounts/components/AccountsToolbar.tsx", "w", encoding="utf-8") as f:
    f.write(content)

with open("frontend/src/pages/Accounts.tsx", "r", encoding="utf-8") as f:
    content = f.read()
content = content.replace("<BatchGopayWorkbench", "<BatchGopayWorkbench formatGopayPhoneExpiryLabel={(p) => ''}")
content = content.replace("<AccountDetailModal", "<AccountDetailModal getAccessToken={async () => ''} onCopyAccessToken={() => {}} isAccessTokenCopied={false}")
with open("frontend/src/pages/Accounts.tsx", "w", encoding="utf-8") as f:
    f.write(content)
