import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const modalSource = await readFile(
  new URL('../src/features/auth/components/RegisterTaskModal.tsx', import.meta.url),
  'utf8',
)

test('invalid recheck keeps a dedicated task modal mode', () => {
  const sourceMapper = accountsSource.match(/function taskModalModeFromSource[\s\S]+?\n}/)?.[0] || ''
  assert.match(
    sourceMapper,
    /normalized === 'invalid_recheck' \|\| normalized === 'batch_invalid_recheck'\) return 'invalid_recheck'/,
  )
  assert.doesNotMatch(
    sourceMapper,
    /normalized === 'invalid_recheck' \|\| normalized === 'batch_invalid_recheck'\) return 'resume_auth'/,
  )

  const handlersStart = accountsSource.indexOf('const openInvalidRecheckConfig = async')
  const handlersEnd = accountsSource.indexOf('const openPhoneBindingTest = async', handlersStart)
  const handlers = accountsSource.slice(handlersStart, handlersEnd)
  assert.match(handlers, /handleInvalidRecheck[\s\S]+openInvalidRecheckConfig\('single', record\)/)
  assert.match(handlers, /handleBatchInvalidRecheck[\s\S]+openInvalidRecheckConfig\('batch'\)/)
  assert.match(handlers, /buildTaskProxyPayload\(values\)/)
  assert.match(handlers, /\/tasks\/chatgpt\/invalid-recheck'/)
  assert.match(handlers, /\/tasks\/chatgpt\/invalid-recheck\/batch'/)
  assert.match(handlers, /params:\s*\{[\s\S]+concurrency: requestedConcurrency,[\s\S]+\.\.\.proxyPayload/)
  assert.match(handlers, /setTaskModalMode\('invalid_recheck'\)/)
})

test('invalid recheck configuration exposes proxy mode and uncapped batch concurrency', () => {
  const modalStart = accountsSource.indexOf("title={invalidRecheckConfigMode === 'single'")
  const modalEnd = accountsSource.indexOf("title={resumeAuthConfigMode === 'single'", modalStart)
  const configModal = accountsSource.slice(modalStart, modalEnd)
  assert.match(configModal, /name="concurrency"/)
  assert.match(configModal, /<InputNumber min=\{1\} step=\{1\} precision=\{0\}/)
  assert.doesNotMatch(configModal, /name="concurrency"[\s\S]{0,300}max=\{5\}/)
  assert.match(configModal, /name="proxy_mode" label="代理方式"/)
  assert.match(configModal, /value: 'dynamic'/)
  assert.match(configModal, /value: 'pool'/)
  assert.match(configModal, /value: 'specified'/)
  assert.match(configModal, /value: 'direct'/)

  assert.doesNotMatch(accountsSource, /Math\.min\(5, Number\(values\.concurrency/)
  assert.doesNotMatch(accountsSource, /后端硬上限 5/)
})

test('invalid recheck modal title never presents the task as auth recovery', () => {
  const titleBranch = modalSource.match(
    /if \(taskModalMode === 'invalid_recheck'\) \{[\s\S]+?(?=    if \(taskModalMode === 'resume_auth'\))/,
  )?.[0] || ''
  assert.match(titleBranch, /`失效测活 \$\{taskModalAccount\.email\}`/)
  assert.match(titleBranch, /`批量失效测活 \(\$\{eligible} 个\)`/)
  assert.doesNotMatch(titleBranch, /补抓Auth/)
})
