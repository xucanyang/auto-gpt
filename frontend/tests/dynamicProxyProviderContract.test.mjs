import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  buildTaskProxyConfigPatch,
  buildTaskProxyPayload,
  normalizeTaskProxySettings,
  taskProxySettingsFromConfig,
} from '../src/lib/taskProxySettings.ts'

const proxiesSource = await readFile(new URL('../src/pages/Proxies.tsx', import.meta.url), 'utf8')
const settingsSource = await readFile(new URL('../src/pages/Settings.tsx', import.meta.url), 'utf8')
const registerPageSource = await readFile(new URL('../src/pages/RegisterTaskPage.tsx', import.meta.url), 'utf8')
const registerModalSource = await readFile(
  new URL('../src/features/auth/components/RegisterTaskModal.tsx', import.meta.url),
  'utf8',
)
const customRecheckSource = await readFile(
  new URL('../src/pages/CustomEmailRecheckPage.tsx', import.meta.url),
  'utf8',
)
const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

test('legacy task proxy settings default to Cliproxy while global MiyaIP is hydrated', () => {
  assert.equal(normalizeTaskProxySettings({ proxy_mode: 'dynamic' }).dynamic_proxy_provider, 'cliproxy')
  assert.equal(taskProxySettingsFromConfig({
    task_proxy_mode: 'dynamic',
    dynamic_proxy_provider: 'miyaip',
    dynamic_proxy_default_country: 'us',
  }).dynamic_proxy_provider, 'miyaip')
})

test('dynamic task payload always submits provider and MiyaIP excludes Cliproxy fields', () => {
  const miyaip = buildTaskProxyPayload({
    proxy_mode: 'dynamic',
    dynamic_proxy_provider: 'miyaip',
    proxy: 'socks5://user-region-US-sid-seed:pass@cliproxy.example:1080',
    proxy_country_code: 'us',
    proxy_failover: true,
    dynamic_proxy_ip_retention_minutes: 30,
  })
  assert.deepEqual(miyaip, {
    proxy_mode: 'dynamic',
    dynamic_proxy_provider: 'miyaip',
    proxy_country_code: 'US',
    proxy_failover: true,
  })

  const cliproxy = buildTaskProxyPayload({
    proxy_mode: 'dynamic',
    dynamic_proxy_provider: 'cliproxy',
    proxy: 'socks5://user-region-US-sid-seed:pass@cliproxy.example:1080',
    proxy_country_code: 'us',
    dynamic_proxy_ip_retention_minutes: 30,
  })
  assert.equal(cliproxy.dynamic_proxy_provider, 'cliproxy')
  assert.equal(cliproxy.proxy, 'socks5://user-region-US-sid-seed:pass@cliproxy.example:1080')
  assert.equal(cliproxy.dynamic_proxy_ip_retention_minutes, 30)
})

test('global channel patch preserves the inactive provider configuration', () => {
  const patch = buildTaskProxyConfigPatch({
    proxy_mode: 'dynamic',
    dynamic_proxy_provider: 'miyaip',
    proxy_country_code: 'jp',
    miyaip_crc: 'crc-value',
    miyaip_key_name: 'key-value',
    miyaip_pool: 7,
    miyaip_gateway_server: 'eu',
    miyaip_protocol: 'socks5',
    miyaip_request_timeout_seconds: 12,
  })

  assert.equal(patch.dynamic_proxy_provider, 'miyaip')
  assert.equal(patch.dynamic_proxy_default_country, 'JP')
  assert.equal(patch.miyaip_crc, 'crc-value')
  assert.equal(patch.miyaip_key_name, 'key-value')
  assert.equal(patch.miyaip_pool, '7')
  assert.equal(patch.miyaip_gateway_server, 'eu')
  assert.equal(patch.miyaip_protocol, 'socks5')
  assert.equal(patch.miyaip_request_timeout_seconds, '12')
  assert.equal(Object.hasOwn(patch, 'dynamic_proxy_template'), false)
  assert.equal(Object.hasOwn(patch, 'dynamic_proxy_ip_retention_minutes'), false)
})

test('proxy and settings pages expose both complete provider configurations', () => {
  for (const source of [proxiesSource, settingsSource]) {
    assert.match(source, /Cliproxy/)
    assert.match(source, /MiyaIP/)
    assert.match(source, /dynamic_proxy_provider/)
  }
  for (const key of [
    'miyaip_crc',
    'miyaip_key_name',
    'miyaip_pool',
    'miyaip_gateway_server',
    'miyaip_protocol',
    'miyaip_request_timeout_seconds',
  ]) {
    assert.match(proxiesSource, new RegExp(key))
    assert.match(settingsSource, new RegExp(key))
  }
  assert.match(proxiesSource, /SessionTime=-1/)
  assert.match(settingsSource, /TASK_PROXY_CONFIG_KEYS/)
  assert.match(settingsSource, /initialTaskProxyValuesRef/)
})

test('every task surface exposes the dynamic provider without credential fields', () => {
  for (const source of [registerPageSource, registerModalSource, customRecheckSource]) {
    assert.match(source, /name="dynamic_proxy_provider"/)
    assert.match(source, /value: 'cliproxy'/)
    assert.match(source, /value: 'miyaip'/)
    assert.doesNotMatch(source, /name="miyaip_crc"/)
    assert.doesNotMatch(source, /name="miyaip_key_name"/)
  }

  assert.ok((accountsSource.match(/name="dynamic_proxy_provider"/g) || []).length >= 3)
  assert.doesNotMatch(accountsSource, /name="miyaip_crc"/)
  assert.doesNotMatch(accountsSource, /name="miyaip_key_name"/)
  assert.doesNotMatch(
    `${registerPageSource}\n${registerModalSource}\n${customRecheckSource}\n${accountsSource}`,
    /失败后刷新 SID|失败后刷新 sid/,
  )
})

test('accounts registration settings persist the selected dynamic provider', () => {
  assert.match(
    accountsSource,
    /const settingsPayload = \{[\s\S]*?dynamic_proxy_provider: String\(values\.dynamic_proxy_provider \|\| 'cliproxy'\)[\s\S]*?await saveTaskProxySettingsToConfig\(settingsPayload\)/,
  )
})
