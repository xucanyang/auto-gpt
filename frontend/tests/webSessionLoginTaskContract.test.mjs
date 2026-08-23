import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const toolbarSource = await readFile(
  new URL('../src/features/accounts/components/AccountsToolbar.tsx', import.meta.url),
  'utf8',
)
const actionSurfaceSource = await readFile(
  new URL('../src/features/accounts/components/AccountActionSurface.tsx', import.meta.url),
  'utf8',
)
const modalSource = await readFile(
  new URL('../src/features/auth/components/RegisterTaskModal.tsx', import.meta.url),
  'utf8',
)
const taskTypesSource = await readFile(new URL('../src/lib/taskTypes.ts', import.meta.url), 'utf8')
const taskLogPanelSource = await readFile(new URL('../src/components/TaskLogPanel.tsx', import.meta.url), 'utf8')

test('refresh-only login keeps independent single and batch compatibility routes', () => {
  const sourceMapper = accountsSource.match(/function taskModalModeFromSource[\s\S]+?\n}/)?.[0] || ''
  assert.match(
    sourceMapper,
    /normalized === 'web_session_login'[\s\S]+normalized === 'batch_web_session_login'[\s\S]+return 'web_session_login'/,
  )

  const handlersStart = accountsSource.indexOf('const openWebSessionLoginConfig = async')
  const handlersEnd = accountsSource.indexOf('const openInvalidRecheckConfig = async', handlersStart)
  const handlers = accountsSource.slice(handlersStart, handlersEnd)
  assert.match(handlers, /handleWebSessionLogin[\s\S]+openWebSessionLoginConfig\('single', record, 'login'\)/)
  assert.match(handlers, /handleBatchWebSessionLogin[\s\S]+openWebSessionLoginConfig\('batch', null, 'login'\)/)
  assert.match(handlers, /\/tasks\/chatgpt\/web-session-login'/)
  assert.match(handlers, /postAccountScopeRequest\(`\$\{endpoint\}\/batch`/)
  assert.match(handlers, /params:\s*\{[\s\S]+concurrency: requestedConcurrency,[\s\S]+\.\.\.proxyPayload/)
  assert.match(handlers, /setTaskModalMode\('web_session_login'\)/)
})

test('refresh-only login is secondary and explicitly excludes GCash', () => {
  assert.match(accountsSource, /key: '__web_session_refresh__', label: '仅刷新登录态（不提 GCash）'/)
  assert.match(accountsSource, /__web_session_refresh__[\s\S]{0,160}handleWebSessionLogin\(record\)/)
  assert.doesNotMatch(accountsSource, /shouldShowWebSessionLoginButton/)
  assert.doesNotMatch(accountsSource, /onClick=\{\(\) => handleWebSessionLogin\(record\)\}[\s\S]{0,120}执行登录态/)

  assert.match(toolbarSource, /\| 'webSessionLogin'/)
  assert.match(toolbarSource, /case 'webSessionLogin':[\s\S]{0,320}批量仅刷新登录态/)
  assert.match(toolbarSource, /onBatchWebSessionLogin\(\)/)
  assert.match(actionSurfaceSource, /actionId === 'web_session_login' && onWebSessionLoginTask/)
})

test('web session login configuration preserves business-state boundaries', () => {
  const modalStart = accountsSource.indexOf('open={webSessionLoginConfigOpen}')
  const modalEnd = accountsSource.indexOf("title={invalidRecheckConfigMode === 'single'", modalStart)
  const configModal = accountsSource.slice(modalStart, modalEnd)
  assert.match(configModal, /name="concurrency"/)
  assert.match(configModal, /name="proxy_mode" label="代理方式"/)
  assert.match(configModal, /AccessToken、Session、Cookie、账号身份和浏览器 Profile/)
  assert.match(configModal, /持续保持本地浏览器/)
  assert.match(configModal, /仅刷新登录态/)
  assert.match(configModal, /不会发起 GCash 提链/)
  assert.match(configModal, /不会请求 ChatGPT logout/)
  assert.match(configModal, /账号使用状态、订阅、手机号及邮箱绑定状态保持不变/)
})

test('web session task panel exposes persistent lease controls without generic stop semantics', () => {
  assert.match(taskLogPanelSource, /\/tasks\/\$\{taskId\}\/web-session-leases/)
  assert.match(taskLogPanelSource, /web-session-leases\/\$\{Number\(accountId \|\| 0\)\}\/\$\{action\}/)
  assert.match(taskLogPanelSource, /web-session-leases\/release-all/)
  assert.match(taskLogPanelSource, /停止新增浏览器/)
  assert.match(taskLogPanelSource, /停止并释放全部/)
  assert.match(taskLogPanelSource, /同步最新登录态/)
  assert.match(taskLogPanelSource, /Profile/)
  assert.match(taskLogPanelSource, /不会请求 ChatGPT logout/)
  assert.match(taskLogPanelSource, /!isWebSessionTask/)
})

test('task history and live modal identify refresh-only legacy sources', () => {
  assert.match(taskTypesSource, /web_session_login: '仅刷新登录态'/)
  assert.match(taskTypesSource, /batch_web_session_login: '批量仅刷新登录态'/)
  const titleBranch = modalSource.match(
    /if \(taskModalMode === 'web_session_login'\) \{[\s\S]+?(?=    if \(taskModalMode === 'invalid_recheck'\))/,
  )?.[0] || ''
  assert.match(titleBranch, /`仅刷新登录态 \$\{taskModalAccount\.email\}`/)
  assert.match(titleBranch, /`批量仅刷新登录态 \(\$\{eligible} 个\)`/)
  assert.doesNotMatch(titleBranch, /失效测活|补抓Auth/)
})
