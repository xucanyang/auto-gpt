import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  getRegisterDefaultConcurrency,
  getRegisterConcurrencyLimit,
  isRegisterUniqueExitEnabled,
  normalizeRegisterConcurrency,
  normalizeRegisterDelaySettings,
  normalizeRegisterUniqueExitPolicy,
} from '../src/lib/chatgptRegisterTaskControls.ts'

const registerPageSource = await readFile(new URL('../src/pages/RegisterTaskPage.tsx', import.meta.url), 'utf8')
const registerModalSource = await readFile(new URL('../src/features/auth/components/RegisterTaskModal.tsx', import.meta.url), 'utf8')
const tempmailDomainSelectorSource = await readFile(new URL('../src/features/auth/components/TempMailDomainSelector.tsx', import.meta.url), 'utf8')
const tempmailDomainSelectionSource = await readFile(new URL('../src/lib/tempMailDomainSelection.ts', import.meta.url), 'utf8')
const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const settingsSource = await readFile(new URL('../src/pages/Settings.tsx', import.meta.url), 'utf8')
const appStylesSource = await readFile(new URL('../src/index.css', import.meta.url), 'utf8')

test('ChatGPT executor concurrency defaults and caps are deterministic', () => {
  assert.equal(getRegisterConcurrencyLimit('chatgpt', 'protocol'), 3)
  assert.equal(getRegisterConcurrencyLimit('chatgpt', 'headless'), 2)
  assert.equal(getRegisterConcurrencyLimit('chatgpt', 'headed'), 2)
  assert.equal(normalizeRegisterConcurrency(undefined, 'chatgpt', 'protocol'), 2)
  assert.equal(normalizeRegisterConcurrency(4, 'chatgpt', 'protocol'), 3)
  assert.equal(normalizeRegisterConcurrency(3, 'chatgpt', 'headless'), 2)
  assert.equal(normalizeRegisterConcurrency(2, 'chatgpt', 'headed', true), 1)
})

test('configured defaults, caps, and delays can only lower the frontend controls', () => {
  const config = {
    chatgpt_register_protocol_default_concurrency: '1',
    chatgpt_register_protocol_max_concurrency: '2',
    chatgpt_register_browser_default_concurrency: '1',
    chatgpt_register_browser_max_concurrency: '1',
    chatgpt_register_delay_seconds: '8',
    chatgpt_register_delay_max_seconds: '12',
  }

  assert.equal(getRegisterDefaultConcurrency('chatgpt', 'protocol', config), 1)
  assert.equal(getRegisterConcurrencyLimit('chatgpt', 'protocol', config), 2)
  assert.equal(normalizeRegisterConcurrency(undefined, 'chatgpt', 'protocol', false, config), 1)
  assert.equal(normalizeRegisterConcurrency(3, 'chatgpt', 'protocol', false, config), 2)
  assert.equal(getRegisterDefaultConcurrency('chatgpt', 'headless', config), 1)
  assert.equal(getRegisterConcurrencyLimit('chatgpt', 'headless', config), 1)
  assert.deepEqual(normalizeRegisterDelaySettings({}, 'chatgpt', config), {
    register_delay_seconds: 8,
    register_delay_max_seconds: 12,
  })
})

test('browser registration settings can raise the configured task limit to fifteen', () => {
  const config = {
    chatgpt_register_browser_default_concurrency: '15',
    chatgpt_register_browser_max_concurrency: '15',
  }

  assert.equal(getRegisterDefaultConcurrency('chatgpt', 'headless', config), 15)
  assert.equal(getRegisterConcurrencyLimit('chatgpt', 'headless', config), 15)
  assert.equal(normalizeRegisterConcurrency(20, 'chatgpt', 'headless', false, config), 15)
})

test('global settings expose instance-local browser and solver capacity controls', () => {
  for (const key of [
    'chatgpt_runtime_browser_capacity_mode',
    'chatgpt_runtime_auth_browser_max_concurrency',
    'chatgpt_web_session_hold_max_sessions',
    'chatgpt_runtime_auth_browser_pid_budget',
    'chatgpt_runtime_pid_emergency_reserve',
    'chatgpt_runtime_host_memory_reserve_mib',
    'chatgpt_runtime_cpu_psi_avg10_limit',
    'chatgpt_runtime_solver_warm_browsers',
    'chatgpt_runtime_solver_max_browsers',
    'chatgpt_runtime_solver_idle_timeout_seconds',
  ]) {
    assert.match(settingsSource, new RegExp(key))
  }
})

test('non-ChatGPT registration retains its previous concurrency and delay defaults', () => {
  assert.equal(getRegisterConcurrencyLimit('google', 'protocol'), 5)
  assert.equal(normalizeRegisterConcurrency(undefined, 'google', 'protocol'), 1)
  assert.equal(normalizeRegisterConcurrency(5, 'google', 'protocol'), 5)
  assert.deepEqual(normalizeRegisterDelaySettings({}, 'google'), {
    register_delay_seconds: 0,
    register_delay_max_seconds: 0,
  })
  assert.deepEqual(normalizeRegisterDelaySettings({
    register_delay_seconds: 'invalid',
    register_delay_max_seconds: 'invalid',
  }, 'google'), {
    register_delay_seconds: 0,
    register_delay_max_seconds: 0,
  })
  assert.deepEqual(normalizeRegisterDelaySettings({ register_delay_seconds: 12 }, 'google'), {
    register_delay_seconds: 12,
    register_delay_max_seconds: 0,
  })
})

test('new delay defaults coexist with legacy fixed and disabled settings', () => {
  assert.deepEqual(normalizeRegisterDelaySettings(), {
    register_delay_seconds: 15,
    register_delay_max_seconds: 30,
  })
  assert.deepEqual(normalizeRegisterDelaySettings({ register_delay_seconds: 0 }), {
    register_delay_seconds: 0,
    register_delay_max_seconds: 0,
  })
  assert.deepEqual(normalizeRegisterDelaySettings({ register_delay_seconds: 12 }), {
    register_delay_seconds: 12,
    register_delay_max_seconds: 12,
  })
  assert.deepEqual(normalizeRegisterDelaySettings({
    register_delay_seconds: 0,
    register_delay_max_seconds: 0,
  }), {
    register_delay_seconds: 0,
    register_delay_max_seconds: 0,
  })
  assert.deepEqual(normalizeRegisterDelaySettings({
    register_delay_seconds: 12,
    register_delay_max_seconds: 0,
  }), {
    register_delay_seconds: 12,
    register_delay_max_seconds: 0,
  })
})

test('canonical unique-exit policy wins over legacy values and legacy-only values remain compatible', () => {
  assert.equal(normalizeRegisterUniqueExitPolicy('auto', true), 'auto')
  assert.equal(normalizeRegisterUniqueExitPolicy('required', false), 'required')
  assert.equal(normalizeRegisterUniqueExitPolicy('off', true), 'off')
  assert.equal(normalizeRegisterUniqueExitPolicy(undefined, true), 'required')
  assert.equal(normalizeRegisterUniqueExitPolicy(undefined, 'false'), 'off')
  assert.equal(normalizeRegisterUniqueExitPolicy(undefined, undefined), 'auto')
})

test('auto unique-exit policy follows proxy mode while explicit policies do not', () => {
  assert.equal(isRegisterUniqueExitEnabled('auto', 'dynamic'), true)
  assert.equal(isRegisterUniqueExitEnabled('auto', 'direct'), false)
  assert.equal(isRegisterUniqueExitEnabled('auto', 'pool'), false)
  assert.equal(isRegisterUniqueExitEnabled('required', 'direct'), true)
  assert.equal(isRegisterUniqueExitEnabled('off', 'dynamic'), false)
})

test('both registration surfaces expose bounded concurrency and the full delay range', () => {
  for (const source of [registerPageSource, registerModalSource]) {
    assert.match(source, /max=\{concurrencyLimit\}/)
    assert.match(source, /name="register_delay_seconds"/)
    assert.match(source, /name="register_delay_max_seconds"/)
    assert.match(source, /CHATGPT_REGISTER_DEFAULT_DELAY_SECONDS/)
    assert.match(source, /CHATGPT_REGISTER_DEFAULT_DELAY_MAX_SECONDS/)
  }
})

test('both registration surfaces share the all-then-preferred TempMail domain selector', () => {
  for (const source of [registerModalSource, registerPageSource]) {
    assert.match(source, /<TempMailDomainSelector/)
    assert.doesNotMatch(source, /<Select\s+mode="multiple"[\s\S]+?tempmailDomainOptions/)
  }

  assert.ok(
    tempmailDomainSelectorSource.indexOf('<span>全部域名</span>')
      < tempmailDomainSelectorSource.indexOf('aria-labelledby="tempmail-preferred-domains-title"'),
  )
  assert.match(tempmailDomainSelectorSource, /保存优选/)
  assert.match(tempmailDomainSelectorSource, /name=\{preferredFieldName\}/)
  assert.match(tempmailDomainSelectorSource, /name=\{fixedFieldName\}/)
  assert.match(tempmailDomainSelectorSource, /include_inactive: true/)
  assert.match(tempmailDomainSelectorSource, /domains\.map\(\(option\) =>/)
  assert.match(tempmailDomainSelectorSource, /togglePreferredMembership/)
  assert.match(tempmailDomainSelectorSource, /toggleCurrentSelection/)
  assert.match(tempmailDomainSelectorSource, /const checked = preferredDomainSet\.has\(option\.domain\)/)
  assert.match(tempmailDomainSelectorSource, /checked=\{checked\}/)
  assert.match(tempmailDomainSelectorSource, /checked=\{selectedDomainSet\.has\(domain\)\}/)
  assert.doesNotMatch(tempmailDomainSelectorSource, /\.sort\(/)
  assert.doesNotMatch(tempmailDomainSelectionSource, /\.sort\(/)

  assert.match(appStylesSource, /\.tempmail-domain-grid,[\s\S]+?grid-template-columns: repeat\(3, minmax\(0, 1fr\)\)/)
  assert.match(appStylesSource, /grid-auto-flow: row/)
  assert.match(appStylesSource, /@media \(max-width: 560px\)[\s\S]+?repeat\(2, minmax\(0, 1fr\)\)/)
  assert.match(appStylesSource, /@media \(max-width: 380px\)[\s\S]+?grid-template-columns: minmax\(0, 1fr\)/)
})

test('preferred membership and current TempMail selection hydrate and persist independently', () => {
  assert.match(accountsSource, /const tempmailPreferredDomains = resolveTempMailPreferredDomains/)
  assert.match(accountsSource, /const tempmailFixedDomains = orderTempMailSelectedDomains\(/)
  assert.match(accountsSource, /tempmail_preferred_domains: tempmailPreferredDomains/)
  assert.match(accountsSource, /tempmail_fixed_domains: tempmailFixedDomains/)
  assert.match(accountsSource, /请在优选域名中勾选至少一个本次使用的可用域名/)
  assert.match(registerPageSource, /const selectedTempMailDomains = orderTempMailSelectedDomains\(/)
  assert.match(registerPageSource, /tempmail_fixed_domains: selectedTempMailDomains/)
})

test('task creation and settings persistence retain max delay and canonical unique-exit policy', () => {
  const saveHandler = accountsSource.match(/const handleSaveRegisterSettings = async \(\) => \{[\s\S]+?\n  const handleRegister = async/)?.[0] || ''
  const registerHandler = accountsSource.match(/const handleRegister = async \(\) => \{[\s\S]+?\n  const handleDetailSave = async/)?.[0] || ''
  const pageSubmitHandler = registerPageSource.match(/const submit = async \(\) => \{[\s\S]+?\n  const pollTask = async/)?.[0] || ''

  assert.match(saveHandler, /register_delay_max_seconds: settingsPayload\.register_delay_max_seconds/)
  assert.match(saveHandler, /await registerForm\.validateFields\(\)/)
  assert.match(saveHandler, /chatgpt_register_unique_exit_ip_policy: settingsPayload\.chatgpt_register_unique_exit_ip_policy/)
  assert.match(saveHandler, /chatgpt_register_unique_exit_ip_enabled: undefined/)
  assert.match(registerHandler, /const delaySettings = normalizeRegisterDelaySettings\(values, currentPlatform, cfg\)/)
  assert.equal(registerHandler.match(/\.\.\.delaySettings/g)?.length, 2)
  assert.match(registerHandler, /chatgpt_register_unique_exit_ip_policy:\s*[\s\S]+?normalizeRegisterUniqueExitPolicy/)
  assert.match(registerHandler, /chatgpt_register_unique_exit_ip_enabled: undefined/)
  assert.match(pageSubmitHandler, /const delaySettings = normalizeRegisterDelaySettings\(values, values\.platform, registerControlConfig\)/)
  assert.match(pageSubmitHandler, /chatgpt_register_unique_exit_ip_policy:[\s\S]+?normalizeRegisterUniqueExitPolicy/)
  assert.match(pageSubmitHandler, /\.\.\.delaySettings/)
})

test('registration surfaces hydrate and expose the canonical unique-exit policy', () => {
  assert.match(registerPageSource, /chatgpt_register_unique_exit_ip_policy: normalizeRegisterUniqueExitPolicy\([\s\S]+?cfg\.chatgpt_register_unique_exit_ip_policy,[\s\S]+?cfg\.chatgpt_register_unique_exit_ip_enabled/)
  assert.match(accountsSource, /const configuredUniqueExitPolicy = normalizeRegisterUniqueExitPolicy\([\s\S]+?cfg\.chatgpt_register_unique_exit_ip_policy,[\s\S]+?cfg\.chatgpt_register_unique_exit_ip_enabled/)
  for (const source of [registerPageSource, registerModalSource]) {
    assert.match(source, /name="chatgpt_register_unique_exit_ip_policy"/)
    assert.match(source, /value: 'auto'/)
    assert.match(source, /value: 'required'/)
    assert.match(source, /value: 'off'/)
  }
})
