import re

with open("frontend/src/pages/Accounts.tsx", "r", encoding="utf-8") as f:
    content = f.read()

replacements = [
    (
        "const handleBackfill = async (destination: 'cliproxyapi' | 'sub2api', mode: 'pending' | 'selected') => {",
        "const handleBackfill = async (destination: 'cliproxyapi' | 'sub2api' | 'oaipay', mode: 'pending' | 'selected') => {"
    ),
    (
        "const destinationLabel = destination === 'sub2api' ? 'Sub2API' : 'CLIProxyAPI'",
        "const destinationLabel = destination === 'sub2api' ? 'Sub2API' : destination === 'oaipay' ? 'OAIPay' : 'CLIProxyAPI'"
    ),
    (
        "const handleBatchStatusSync = async (kind: 'probe' | 'remote' | 'sub2api', scope: 'selected' | 'all') => {",
        "const handleBatchStatusSync = async (kind: 'probe' | 'remote' | 'sub2api' | 'oaipay', scope: 'selected' | 'all') => {"
    ),
    (
        "const actionId = kind === 'remote' ? 'sync_cliproxyapi_status' : kind === 'sub2api' ? 'sync_sub2api_status' : 'sync_account_status'",
        "const actionId = kind === 'remote' ? 'sync_cliproxyapi_status' : kind === 'sub2api' ? 'sync_sub2api_status' : kind === 'oaipay' ? 'sync_oaipay_status' : 'sync_account_status'"
    ),
    (
        "const actionLabel = kind === 'remote' ? 'CLIProxyAPI 状态同步' : kind === 'sub2api' ? 'Sub2API 状态同步' : '云端状态同步'",
        "const actionLabel = kind === 'remote' ? 'CLIProxyAPI 状态同步' : kind === 'sub2api' ? 'Sub2API 状态同步' : kind === 'oaipay' ? 'OAIPay 状态同步' : '云端状态同步'"
    ),
    (
        "const getPendingBackfillCount = (destination: 'cliproxyapi' | 'sub2api') => {",
        "const getPendingBackfillCount = (destination: 'cliproxyapi' | 'sub2api' | 'oaipay') => {"
    ),
    (
        "if (destination === 'sub2api') {\n      return summarizeSub2ApiStates(accounts).pending\n    }",
        "if (destination === 'sub2api') {\n      return summarizeSub2ApiStates(accounts).pending\n    }\n    if (destination === 'oaipay') {\n      return summarizeSub2ApiStates(accounts).pending // We can just use sub2api logic for now or oaipay\n    }"
    ),
    (
        "const buildBackfillLabel = (destination: 'cliproxyapi' | 'sub2api') => {",
        "const buildBackfillLabel = (destination: 'cliproxyapi' | 'sub2api' | 'oaipay') => {"
    ),
    (
        "const isBackfillActionLoading = (destination: 'cliproxyapi' | 'sub2api', scope: 'selected' | 'pending') =>",
        "const isBackfillActionLoading = (destination: 'cliproxyapi' | 'sub2api' | 'oaipay', scope: 'selected' | 'pending') =>"
    ),
    (
        "const buildBackfillMenuLabel = (destination: 'cliproxyapi' | 'sub2api') => {",
        "const buildBackfillMenuLabel = (destination: 'cliproxyapi' | 'sub2api' | 'oaipay') => {"
    ),
    (
        "{ key: `sub2api:${backfillScope}`, label: buildBackfillMenuLabel('sub2api'), disabled: backfillDisabled },",
        "{ key: `sub2api:${backfillScope}`, label: buildBackfillMenuLabel('sub2api'), disabled: backfillDisabled },\n    { key: `oaipay:${backfillScope}`, label: buildBackfillMenuLabel('oaipay'), disabled: backfillDisabled },"
    ),
    (
        "{ key: `sub2api:${getStatusSyncScope()}`, label: getStatusSyncScope() === 'selected' ? `同步所选 Sub2API 状态 (${selectedRowKeys.length})` : `同步当前筛选 Sub2API 状态 (${total})`, disabled: getStatusSyncScope() === 'selected' ? selectedRowKeys.length === 0 : total === 0 },",
        "{ key: `sub2api:${getStatusSyncScope()}`, label: getStatusSyncScope() === 'selected' ? `同步所选 Sub2API 状态 (${selectedRowKeys.length})` : `同步当前筛选 Sub2API 状态 (${total})`, disabled: getStatusSyncScope() === 'selected' ? selectedRowKeys.length === 0 : total === 0 },\n    { key: `oaipay:${getStatusSyncScope()}`, label: getStatusSyncScope() === 'selected' ? `同步所选 OAIPay 状态 (${selectedRowKeys.length})` : `同步当前筛选 OAIPay 状态 (${total})`, disabled: getStatusSyncScope() === 'selected' ? selectedRowKeys.length === 0 : total === 0 },"
    ),
]

for src, dst in replacements:
    # Need to be very careful about exact formatting
    content = content.replace(src, dst)
    # Also handle multiline variants for menu items
    if "backfillMenuItems" in src:
        pass # Handle manually later if not matched

with open("frontend/src/pages/Accounts.tsx", "w", encoding="utf-8") as f:
    f.write(content)

