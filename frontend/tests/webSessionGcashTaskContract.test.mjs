import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const toolbarSource = await readFile(
  new URL('../src/features/accounts/components/AccountsToolbar.tsx', import.meta.url),
  'utf8',
)
const taskPanelSource = await readFile(new URL('../src/components/TaskLogPanel.tsx', import.meta.url), 'utf8')
const modalSource = await readFile(
  new URL('../src/features/auth/components/RegisterTaskModal.tsx', import.meta.url),
  'utf8',
)
const taskTypesSource = await readFile(new URL('../src/lib/taskTypes.ts', import.meta.url), 'utf8')
const actionSurfaceSource = await readFile(
  new URL('../src/features/accounts/components/AccountActionSurface.tsx', import.meta.url),
  'utf8',
)

test('execute login state dispatches the complete single and batch workflow', () => {
  const handlersStart = accountsSource.indexOf('const openWebSessionLoginConfig = async')
  const handlersEnd = accountsSource.indexOf('const openInvalidRecheckConfig = async', handlersStart)
  const handlers = accountsSource.slice(handlersStart, handlersEnd)
  assert.match(handlers, /handleWebSessionGcash[\s\S]+openWebSessionLoginConfig\('single', record, 'gcash'\)/)
  assert.match(handlers, /handleBatchWebSessionGcash[\s\S]+openWebSessionLoginConfig\('batch', null, 'gcash'\)/)
  assert.match(handlers, /'\/tasks\/chatgpt\/web-session-gcash'/)
  assert.match(handlers, /postAccountScopeRequest\(`\$\{endpoint\}\/batch`/)
  assert.match(handlers, /concurrency: requestedConcurrency/)
  assert.match(handlers, /setTaskModalMode\('web_session_login'\)/)
})

test('execute login state is primary while refresh-only remains secondary', () => {
  assert.match(toolbarSource, /\| 'webSessionGcash'/)
  assert.match(toolbarSource, /case 'webSessionGcash':[\s\S]{0,320}批量执行登录态/)
  assert.match(toolbarSource, /onBatchWebSessionGcash\(\)/)
  assert.match(accountsSource, /onClick=\{\(\) => handleWebSessionGcash\(record\)\}[\s\S]{0,120}执行登录态/)
  assert.match(accountsSource, /onWebSessionLoginTask=\{handleWebSessionGcash\}/)
  assert.match(accountsSource, /key: '__web_session_refresh__', label: '仅刷新登录态（不提 GCash）'/)
  assert.match(accountsSource, /并发数只限制同时登录的账号数/)
  assert.match(accountsSource, /同一个浏览器上下文的新标签页打开/)
  assert.match(accountsSource, /完整流程：并发登录并写回本次最新 AccessToken、Session、Cookie/)
})

test('combined task sources retain persistent browser lease controls and GCash tab status', () => {
  assert.match(taskPanelSource, /taskSource === 'web_session_gcash_link'/)
  assert.match(taskPanelSource, /taskSource === 'batch_web_session_gcash_link'/)
  assert.match(taskPanelSource, /gcash_state/)
  assert.match(taskPanelSource, /case 'submitting':[\s\S]{0,80}case 'running':[\s\S]{0,100}提链中/)
  assert.match(taskPanelSource, /\['queued', 'submitting', 'running'\]/)
  assert.match(taskPanelSource, /gcash_tab_state/)
  assert.match(taskPanelSource, /链接成功/)
  assert.match(taskPanelSource, /支付页/)
  assert.match(taskPanelSource, /停止并释放全部/)
  assert.match(modalSource, /batch_web_session_gcash_link/)
  assert.match(modalSource, /批量执行登录态/)
  assert.match(taskTypesSource, /web_session_gcash_link: '执行登录态'/)
  assert.match(taskTypesSource, /batch_web_session_gcash_link: '批量执行登录态'/)
  assert.match(taskPanelSource, /isGcashWebSessionTask \? '执行登录态' : '仅刷新登录态'/)
})

test('legacy toolbar preferences migrate execute-login intent to the complete workflow', () => {
  assert.match(accountsSource, /toolbar-actions\.v2/)
  assert.match(accountsSource, /toolbar-actions\.v1/)
  assert.match(
    accountsSource,
    /actionId === 'webSessionLogin' \? 'webSessionGcash' : actionId/,
  )
  assert.match(accountsSource, /JSON\.stringify\(migratedActions\)/)
  assert.match(accountsSource, /value: 'webSessionGcash', text: '批量执行登录态'/)
  assert.match(accountsSource, /value: 'webSessionLogin', text: '批量仅刷新登录态'/)
})

test('account list puts default GCash columns after email, migrates v6, and shares one page clock', () => {
  assert.match(accountsSource, /visible-columns\.v7/)
  assert.match(accountsSource, /visible-columns\.v6/)
  assert.match(accountsSource, /visible-columns\.v5/)
  assert.match(accountsSource, /!legacyColumns\.includes\('gcash_link'\)/)
  assert.match(accountsSource, /!legacyColumns\.includes\('gcash_remaining'\)/)
  assert.match(accountsSource, /value: 'gcash_link', text: 'GCash 链接'/)
  assert.match(accountsSource, /value: 'gcash_remaining', text: 'GCash剩余时间'/)
  assert.match(accountsSource, /gcashPaymentLinkFromAccount\(\{ \.\.\.account, extra \}\)/)
  assert.match(accountsSource, /setInterval\(\(\) => setGcashNowMs\(Date\.now\(\)\), 1000\)/)
  assert.match(accountsSource, /key="gcash_link"/)
  assert.match(accountsSource, /key="gcash_remaining"/)
  assert.match(accountsSource, /hasActiveWebSessionGcashTask[\s\S]+refetchAccounts\(\)[\s\S]+3000/)

  const columnsSource = accountsSource.slice(
    accountsSource.indexOf('const columns: any[] = ['),
    accountsSource.indexOf('const visibleColumns ='),
  )
  const emailIndex = columnsSource.indexOf("key: 'email'")
  const gcashLinkIndex = columnsSource.indexOf("key: 'gcash_link'")
  const gcashRemainingIndex = columnsSource.indexOf("key: 'gcash_remaining'")
  const manuallyUsedIndex = columnsSource.indexOf("key: 'manually_used'")
  assert.ok(emailIndex >= 0 && emailIndex < gcashLinkIndex)
  assert.ok(gcashLinkIndex < gcashRemainingIndex)
  assert.ok(gcashRemainingIndex < manuallyUsedIndex)
})

test('single-account GCash eligibility dispatches its real dedicated kind', () => {
  const branch = actionSurfaceSource.match(
    /if \(actionId === 'gcash_payment_method'[\s\S]+?(?=    if \(actionId === 'checkout_link_type')/,
  )?.[0] || ''
  assert.match(branch, /onPaymentEligibilityTask\(acc, 'gcash_payment_method'\)/)
  assert.doesNotMatch(branch, /onPaymentEligibilityTask\(acc, 'payment_methods'\)/)
})
