import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const executorOptionsSource = await readFile(new URL('../src/lib/platformExecutorOptions.ts', import.meta.url), 'utf8')
const browserFamilySource = await readFile(new URL('../src/lib/browserFamilyOptions.ts', import.meta.url), 'utf8')
const registerPageSource = await readFile(new URL('../src/pages/RegisterTaskPage.tsx', import.meta.url), 'utf8')
const registerModalSource = await readFile(new URL('../src/features/auth/components/RegisterTaskModal.tsx', import.meta.url), 'utf8')
const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const settingsSource = await readFile(new URL('../src/pages/Settings.tsx', import.meta.url), 'utf8')

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

test('browser family selection keeps protocol choices and follows each instance deep runtime', () => {
  assert.match(browserFamilySource, /value: 'random'/)
  assert.match(browserFamilySource, /value: 'chrome'/)
  assert.match(browserFamilySource, /value: 'firefox'/)
  assert.match(browserFamilySource, /value: 'safari'/)
  assert.match(browserFamilySource, /Chromium 151 on Linux（Patchright 原生）/)
  assert.match(browserFamilySource, /Firefox 147 on macOS（Camoufox 深画像）/)
  assert.match(browserFamilySource, /Linux 原生表面/)
  assert.match(browserFamilySource, /macOS 深画像/)
  assert.match(browserFamilySource, /return normalizeEffectiveDeepBrowserFamily\(effectiveDeepBrowserFamily\)/)
  for (const source of [registerPageSource, registerModalSource]) {
    assert.match(source, /name="browser_family"/)
    assert.match(source, /normalizeBrowserFamilyForExecutor\(/)
    assert.match(source, /effective_deep_browser_family/)
  }
})

test('web-session login exposes explicit profile reuse or successful browser migration', () => {
  assert.match(browserFamilySource, /value: 'account'/)
  assert.match(browserFamilySource, /复用账号身份并迁移到 Chromium/)
  assert.match(browserFamilySource, /复用账号身份并迁移到 Firefox/)
  assert.match(browserFamilySource, /使用 Chromium 151 on Linux/)
  assert.match(browserFamilySource, /使用 Firefox 147 on macOS/)
  assert.match(accountsSource, /name="browser_family"/)
  assert.match(accountsSource, /getWebSessionBrowserFamilyOptions\(/)
  assert.match(accountsSource, /getWebSessionBrowserFamilySelectionHelp\(effectiveDeepBrowserFamily\)/)
  assert.match(accountsSource, /browser_family: values\.browser_family \|\| 'account'/)
  assert.match(browserFamilySource, /失败时保留原画像/)
})

test('settings describes the effective deep runtime without persisting readonly fields', () => {
  assert.match(settingsSource, /normalizeEffectiveDeepBrowserFamily\(data\.effective_deep_browser_family\)/)
  assert.match(settingsSource, /getBrowserFamilySelectionHelp\(/)
  assert.match(settingsSource, /delete data\.effective_deep_browser_runtime/)
  assert.match(settingsSource, /delete data\.effective_deep_browser_family/)
  assert.match(settingsSource, /delete data\.effective_deep_browser_backend/)
})

test('the accounts registration flow submits and persists the form executor', () => {
  const registerHandler = accountsSource.match(/const handleRegister = async \(\) => \{[\s\S]+?\n  const handleDetailSave = async/)?.[0] || ''
  const saveHandler = accountsSource.match(/const handleSaveRegisterSettings = async \(\) => \{[\s\S]+?\n  const handleRegister = async/)?.[0] || ''

  assert.match(registerHandler, /normalizeExecutorForPlatform\(currentPlatform, values\.executor_type\)/)
  assert.match(registerHandler, /executor_type: executorType/)
  assert.match(registerHandler, /browser_family: browserFamily/)
  assert.doesNotMatch(registerHandler, /normalizeExecutorForPlatform\(currentPlatform, cfg\.default_executor\)/)
  assert.match(registerHandler, /mergeRegisterFormSettings\([\s\S]+?executor_type: executorType[\s\S]+?browser_family: browserFamily/)
  assert.match(saveHandler, /const executorType = normalizeExecutorForPlatform\(currentPlatform, values\.executor_type\)/)
  assert.match(saveHandler, /executor_type: executorType/)
  assert.match(saveHandler, /browser_family: browserFamily/)
  assert.match(saveHandler, /executor_type: settingsPayload\.executor_type/)
  assert.match(saveHandler, /browser_family: settingsPayload\.browser_family/)
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
  assert.match(hydrationSource, /savedSettings\.browser_family \|\| cfg\.default_browser_family \|\| 'random'/)
  assert.equal(
    hydrationSource.match(/registerForm\.isFieldTouched\('browser_family'\)/g)?.length,
    2,
  )
  assert.match(hydrationSource, /savedSettings\.executor_type \|\| 'protocol'/)
  assert.equal(
    hydrationSource.match(/registerForm\.isFieldTouched\('executor_type'\)/g)?.length,
    2,
  )
  assert.equal(
    hydrationSource.match(/const shouldHydrateExecutor = !registerForm\.isFieldTouched\('executor_type'\)/g)?.length,
    2,
  )
  assert.equal(hydrationSource.match(/shouldHydrateExecutor \? \{ executor_type: hydratedExecutor \} : \{\}/g)?.length, 2)
  assert.equal(hydrationSource.match(/\.\.\.executorFieldHydration/g)?.length, 2)
})
