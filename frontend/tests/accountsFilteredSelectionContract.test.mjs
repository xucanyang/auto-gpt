import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const actionModalSource = await readFile(
  new URL('../src/features/accounts/components/BatchAccountActionModal.tsx', import.meta.url),
  'utf8',
)

test('account summary exposes a bounded current-filter quantity selector', () => {
  assert.match(accountsSource, /const MAX_FILTERED_ACCOUNT_SELECTION = 5000/)
  assert.match(accountsSource, /data-testid="filtered-account-selection-popover"/)
  assert.match(accountsSource, /aria-label="批量选择账号数量"/)
  assert.match(accountsSource, /max=\{Math\.max\(filteredSelectionMaximum, 1\)\}/)
  assert.match(accountsSource, /按当前列表排序冻结前 N 个账号/)
  assert.match(accountsSource, /按数量选择/)
})

test('filtered quantity selection resolves server-side and becomes explicit cross-page row keys', () => {
  const handlerStart = accountsSource.indexOf('const resolveFilteredAccountSelection = useCallback')
  const handlerEnd = accountsSource.indexOf('\n  const fetchPaypalFilteredEligibleAccounts', handlerStart)
  assert.ok(handlerStart >= 0 && handlerEnd > handlerStart)
  const handler = accountsSource.slice(handlerStart, handlerEnd)

  assert.match(handler, /applyAccountTaskScopeToBody\(body, \{ scope: 'filtered' \}\)/)
  assert.match(handler, /delete body\.all_filtered/)
  assert.match(handler, /body\.sort_by = currentAccountSortBy/)
  assert.match(handler, /body\.sort_order = currentAccountSortOrder/)
  assert.match(handler, /body\.limit = requestedCount/)
  assert.match(handler, /'\/accounts\/selection\/resolve'/)
  assert.match(handler, /setSelectedRowKeys\(accountIds\)/)
  assert.match(handler, /setSelectedAccountSnapshots\(snapshots\)/)
  assert.match(handler, /setFilteredSelectionMeta/)
})

test('task action limits come from backend metadata and open quantity selection before a failing request', () => {
  assert.match(actionModalSource, /max_accounts\?: number/)
  assert.match(accountsSource, /function accountActionMaxAccounts/)
  assert.match(accountsSource, /action\.execution\?\.max_accounts/)
  assert.match(accountsSource, /if \(scope !== 'single' && targetCount > maxAccounts\)/)
  assert.match(accountsSource, /openFilteredSelection\(maxAccounts\)/)
  assert.match(accountsSource, /maxAccounts=\{pendingAccountAction\?\.maxAccounts \|\| 1000\}/)
})

test('manual checkbox changes discard only the generated-selection label, not selected ids', () => {
  assert.match(
    accountsSource,
    /setSelectedRowKeys=\{\(keys\) => \{\s+setFilteredSelectionMeta\(null\)\s+setSelectedRowKeys\(keys\)\s+\}\}/,
  )
  assert.match(accountsSource, /筛选冻结/)
})
