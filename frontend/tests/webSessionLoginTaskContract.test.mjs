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

test('web session login has independent single and batch task contracts', () => {
  const sourceMapper = accountsSource.match(/function taskModalModeFromSource[\s\S]+?\n}/)?.[0] || ''
  assert.match(
    sourceMapper,
    /normalized === 'web_session_login' \|\| normalized === 'batch_web_session_login'\) return 'web_session_login'/,
  )

  const handlersStart = accountsSource.indexOf('const openWebSessionLoginConfig = async')
  const handlersEnd = accountsSource.indexOf('const openInvalidRecheckConfig = async', handlersStart)
  const handlers = accountsSource.slice(handlersStart, handlersEnd)
  assert.match(handlers, /handleWebSessionLogin[\s\S]+openWebSessionLoginConfig\('single', record\)/)
  assert.match(handlers, /handleBatchWebSessionLogin[\s\S]+openWebSessionLoginConfig\('batch'\)/)
  assert.match(handlers, /\/tasks\/chatgpt\/web-session-login'/)
  assert.match(handlers, /\/tasks\/chatgpt\/web-session-login\/batch'/)
  assert.match(handlers, /params:\s*\{[\s\S]+concurrency: requestedConcurrency,[\s\S]+\.\.\.proxyPayload/)
  assert.match(handlers, /setTaskModalMode\('web_session_login'\)/)
})

test('account rows and toolbar expose web session login without invalid-status gating', () => {
  assert.match(accountsSource, /onClick=\{\(\) => handleWebSessionLogin\(record\)\}[\s\S]{0,120}执行登录态/)
  assert.doesNotMatch(accountsSource, /shouldShowWebSessionLoginButton/)
  assert.match(accountsSource, /onWebSessionLoginTask=\{handleWebSessionLogin\}/)

  assert.match(toolbarSource, /\| 'webSessionLogin'/)
  assert.match(toolbarSource, /case 'webSessionLogin':[\s\S]{0,260}批量执行登录态/)
  assert.match(toolbarSource, /onBatchWebSessionLogin\(\)/)
  assert.match(actionSurfaceSource, /actionId === 'web_session_login' && onWebSessionLoginTask/)
})

test('web session login configuration preserves business-state boundaries', () => {
  const modalStart = accountsSource.indexOf("title={webSessionLoginConfigMode === 'single'")
  const modalEnd = accountsSource.indexOf("title={invalidRecheckConfigMode === 'single'", modalStart)
  const configModal = accountsSource.slice(modalStart, modalEnd)
  assert.match(configModal, /name="concurrency"/)
  assert.match(configModal, /name="proxy_mode" label="代理方式"/)
  assert.match(configModal, /AccessToken、Session、Cookie、账号身份和登录浏览器信息/)
  assert.match(configModal, /账号使用状态、订阅、手机号及邮箱绑定状态保持不变/)
})

test('task history and live modal use dedicated web session login labels', () => {
  assert.match(taskTypesSource, /web_session_login: '执行登录态'/)
  assert.match(taskTypesSource, /batch_web_session_login: '批量执行登录态'/)
  const titleBranch = modalSource.match(
    /if \(taskModalMode === 'web_session_login'\) \{[\s\S]+?(?=    if \(taskModalMode === 'invalid_recheck'\))/,
  )?.[0] || ''
  assert.match(titleBranch, /`执行登录态 \$\{taskModalAccount\.email\}`/)
  assert.match(titleBranch, /`批量执行登录态 \(\$\{eligible} 个\)`/)
  assert.doesNotMatch(titleBranch, /失效测活|补抓Auth/)
})
