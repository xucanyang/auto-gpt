import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  REGISTRATION_DIAGNOSTICS_OPTIONS,
  normalizeRegistrationDiagnosticsMode,
  registrationDiagnosticsModeLabel,
} from '../src/lib/registrationDiagnostics.ts'

const registerPageSource = await readFile(
  new URL('../src/pages/RegisterTaskPage.tsx', import.meta.url),
  'utf8',
)
const registerModalSource = await readFile(
  new URL('../src/features/auth/components/RegisterTaskModal.tsx', import.meta.url),
  'utf8',
)
const accountsSource = await readFile(
  new URL('../src/pages/Accounts.tsx', import.meta.url),
  'utf8',
)
const taskLogSource = await readFile(
  new URL('../src/components/TaskLogPanel.tsx', import.meta.url),
  'utf8',
)
const panelSource = await readFile(
  new URL('../src/components/RegistrationDiagnosticsPanel.tsx', import.meta.url),
  'utf8',
)

test('registration diagnostics exposes the three explicit retention modes', () => {
  assert.deepEqual(
    REGISTRATION_DIAGNOSTICS_OPTIONS.map(({ label, value }) => ({ label, value })),
    [
      { label: '关闭', value: 'off' },
      { label: '智能诊断', value: 'smart' },
      { label: '全量留存', value: 'full' },
    ],
  )
  assert.equal(normalizeRegistrationDiagnosticsMode('smart', 'headless'), 'smart')
  assert.equal(normalizeRegistrationDiagnosticsMode('full', 'headed'), 'full')
  assert.equal(normalizeRegistrationDiagnosticsMode('full', 'protocol'), 'full')
  assert.equal(normalizeRegistrationDiagnosticsMode('full', 'headless', 'google'), 'off')
  assert.equal(normalizeRegistrationDiagnosticsMode('invalid', 'headless'), 'off')
  assert.equal(registrationDiagnosticsModeLabel('full'), '全量留存')
})

test('both registration entrypoints render the mode for every ChatGPT executor', () => {
  assert.match(registerPageSource, /<Form\.Item name="platform" hidden>/)
  for (const source of [registerPageSource, registerModalSource]) {
    assert.match(source, /name="registration_diagnostics_mode"/)
    assert.match(source, /REGISTRATION_DIAGNOSTICS_OPTIONS/)
    assert.match(source, /currentPlatform === 'chatgpt'|platform === 'chatgpt'/)
    assert.match(source, /\['protocol', 'headless', 'headed'\]\.includes\(executorType\)/)
  }
})

test('both task creation requests freeze the normalized diagnostics mode', () => {
  const pageSubmit = registerPageSource.match(
    /const submit = async \(\) => \{[\s\S]+?\n  const pollTask = async/,
  )?.[0] || ''
  const accountSubmit = accountsSource.match(
    /const handleRegister = async \(\) => \{[\s\S]+?\n  const handleDetailSave = async/,
  )?.[0] || ''

  for (const source of [pageSubmit, accountSubmit]) {
    assert.match(source, /registration_diagnostics_mode: normalizeRegistrationDiagnosticsMode\(/)
    assert.match(source, /values\.registration_diagnostics_mode/)
    assert.match(source, /executorType/)
  }
})

test('task log diagnostics surface supports refresh, download, pin and delete contracts', () => {
  assert.match(taskLogSource, /<RegistrationDiagnosticsPanel/)
  assert.match(taskLogSource, /meta\?\.registration_diagnostics\?\.mode/)
  assert.match(panelSource, /\/tasks\/\$\{taskId\}\/diagnostics/)
  assert.match(panelSource, /\/files\/\$\{encodeURIComponent\(filename\)\}/)
  assert.match(panelSource, /\/diagnostics\/\$\{item\.id\}\/pin/)
  assert.match(panelSource, /method: 'DELETE'/)
  assert.match(panelSource, /name\.endsWith\('\.har\.zip'\)/)
  assert.match(panelSource, /key-http-responses\.jsonl/)
  assert.match(panelSource, /HTTP 证据/)
  assert.match(panelSource, /视频不可用/)
  assert.match(panelSource, /window\.setInterval\(\(\) => void load\(true\), 8000\)/)
})
