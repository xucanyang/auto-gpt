import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const toolbarSource = await readFile(
  new URL('../src/features/accounts/components/AccountsToolbar.tsx', import.meta.url),
  'utf8',
)
const modalSource = await readFile(
  new URL('../src/features/accounts/components/BatchAccountActionModal.tsx', import.meta.url),
  'utf8',
)

test('account toolbar exposes metadata-driven generic batch actions', () => {
  assert.match(toolbarSource, /批量账号操作 \(\{batchAccountActionTargetCount\}\)/)
  assert.match(toolbarSource, /batchAccountActionMenuItems/)
  assert.match(accountsSource, /action\?\.batch\?\.mode === 'generic'/)
  assert.match(accountsSource, /action\.batch\?\.group === group/)
  assert.match(accountsSource, /认证与会话/)
  assert.match(accountsSource, /外部上传/)
})

test('generic batch actions use selected-first scope and the existing batch API', () => {
  const handlerStart = accountsSource.indexOf('const openBatchAccountAction = (actionId: string) => {')
  const handlerEnd = accountsSource.indexOf('\n  const handleBackfill = async', handlerStart)
  assert.ok(handlerStart >= 0 && handlerEnd > handlerStart)
  const handlers = accountsSource.slice(handlerStart, handlerEnd)

  assert.match(handlers, /normalizeAccountIds\(selectedRowKeys\)/)
  assert.match(handlers, /selectedIds\.length > 0 \? 'selected' : 'filtered'/)
  assert.match(handlers, /selected_only === true/)
  assert.match(handlers, /currentFilterScopeReady/)
  assert.match(handlers, /applyAccountTaskScopeToBody\(body, \{[\s\S]+selectedIds: pending\.selectedIds/)
  assert.match(handlers, /`\/actions\/\$\{currentPlatform\}\/\$\{encodeURIComponent\(actionId\)\}\/batch`/)
  assert.match(handlers, /showBatchActionResult\(`批量\$\{actionLabel\}结果`, result\)/)
})

test('dangerous generic batch actions require confirmation and never exceed the API limit', () => {
  assert.match(modalSource, /confirmation_param/)
  assert.match(modalSource, /请勾选确认后再执行批量操作/)
  assert.match(modalSource, /maxAccounts = 1000/)
  assert.match(modalSource, /okButtonProps=\{\{ danger: isDanger, disabled: targetCount <= 0 \|\| exceedsLimit \}\}/)
  assert.match(modalSource, /Input\.Password/)
})

test('targeted payment eligibility includes the missing GCash batch action', () => {
  assert.match(accountsSource, /key: 'gcash_payment_method',[\s\S]{0,180}批量检测 GCash 支付方式/)
  assert.match(accountsSource, /kind === 'gcash_payment_method'/)
})
