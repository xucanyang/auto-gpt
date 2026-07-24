import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const executorOptionsSource = await readFile(new URL('../src/lib/platformExecutorOptions.ts', import.meta.url), 'utf8')
const registerPageSource = await readFile(new URL('../src/pages/RegisterTaskPage.tsx', import.meta.url), 'utf8')
const registerModalSource = await readFile(new URL('../src/features/auth/components/RegisterTaskModal.tsx', import.meta.url), 'utf8')
const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

test('registration executor options define three mutually exclusive modes', () => {
  assert.match(executorOptionsSource, /value: 'protocol'/)
  assert.match(executorOptionsSource, /value: 'headless'/)
  assert.match(executorOptionsSource, /value: 'headed'/)
  assert.match(executorOptionsSource, /三种注册执行器互斥/)
  assert.match(executorOptionsSource, /执行中不会[^\n]+自动切换/)
})

test('both registration entrypoints expose the task executor contract', () => {
  for (const source of [registerPageSource, registerModalSource]) {
    assert.match(source, /name="executor_type"/)
    assert.match(source, /extra=\{EXECUTOR_SELECTION_HELP\}/)
  }
  assert.match(registerModalSource, /getExecutorOptions\(currentPlatform\)/)
})

test('the accounts registration flow submits and persists the form executor', () => {
  const registerHandler = accountsSource.match(/const handleRegister = async \(\) => \{[\s\S]+?\n  const handleDetailSave = async/)?.[0] || ''
  const saveHandler = accountsSource.match(/const handleSaveRegisterSettings = async \(\) => \{[\s\S]+?\n  const handleRegister = async/)?.[0] || ''

  assert.match(registerHandler, /normalizeExecutorForPlatform\(currentPlatform, values\.executor_type\)/)
  assert.match(registerHandler, /executor_type: executorType/)
  assert.doesNotMatch(registerHandler, /normalizeExecutorForPlatform\(currentPlatform, cfg\.default_executor\)/)
  assert.match(registerHandler, /mergeRegisterFormSettings\([\s\S]+?executor_type: executorType/)
  assert.match(saveHandler, /executor_type: normalizeExecutorForPlatform\(currentPlatform, values\.executor_type\)/)
  assert.match(saveHandler, /executor_type: settingsPayload\.executor_type/)
})

test('the accounts registration form restores the saved executor without overwriting user input', () => {
  const hydrationStart = accountsSource.indexOf("if (!registerModalOpen) return")
  const hydrationEnd = accountsSource.indexOf(
    '}, [registerModalOpen, currentPlatform, registerForm, loadConfigCache])',
    hydrationStart,
  )
  assert.notEqual(hydrationStart, -1)
  assert.notEqual(hydrationEnd, -1)
  const hydrationSource = accountsSource.slice(hydrationStart, hydrationEnd)

  assert.match(hydrationSource, /savedSettings\.executor_type \|\| cfg\.default_executor \|\| ''/)
  assert.match(hydrationSource, /savedSettings\.executor_type \|\| 'protocol'/)
  assert.equal(
    hydrationSource.match(/registerForm\.isFieldTouched\('executor_type'\)/g)?.length,
    2,
  )
  assert.equal(
    hydrationSource.match(/isFieldTouched\('executor_type'\)\s*\? \{\}\s*:\s*\{\s*executor_type:/g)?.length,
    2,
  )
  assert.equal(hydrationSource.match(/\.\.\.executorFieldHydration/g)?.length, 2)
})
