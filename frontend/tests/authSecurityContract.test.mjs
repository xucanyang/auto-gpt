import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const settingsSource = await readFile(new URL('../src/pages/Settings.tsx', import.meta.url), 'utf8')
const loginSource = await readFile(new URL('../src/pages/Login.tsx', import.meta.url), 'utf8')

test('the admin authentication disable route is not exposed by the frontend', () => {
  assert.equal(settingsSource.includes("apiFetch('/auth/disable'"), false)
  assert.equal(settingsSource.includes('关闭密码保护'), false)
})

test('the bootstrap credential is sent only through its dedicated header', () => {
  assert.match(settingsSource, /headers:\s*bootstrapToken\s*\?\s*\{\s*'X-Auth-Bootstrap-Token': bootstrapToken\s*\}/)
  assert.match(settingsSource, /body:\s*JSON\.stringify\(\{ password: values\.password \}\)/)
  assert.doesNotMatch(settingsSource, /JSON\.stringify\(\{[^}]*bootstrap_token/)
})

test('2FA changes include password and TOTP step-up fields', () => {
  const enableRequest = settingsSource.match(/apiFetch\('\/auth\/2fa\/enable'[\s\S]{0,500}?\n\s*\}\)/)?.[0] || ''
  const disableRequest = settingsSource.match(/apiFetch\('\/auth\/2fa\/disable'[\s\S]{0,500}?\n\s*\}\)/)?.[0] || ''

  assert.match(enableRequest, /secret: totpSecret/)
  assert.match(enableRequest, /code: values\.code/)
  assert.match(enableRequest, /current_password: values\.current_password/)
  assert.match(disableRequest, /code: values\.code/)
  assert.match(disableRequest, /current_password: values\.current_password/)
})

test('new administrator passwords enforce the backend 12 character minimum', () => {
  assert.match(settingsSource, /minPasswordLength\s*=\s*Math\.max\(12,/)
  assert.doesNotMatch(settingsSource, /至少 6 位/)
})

test('the UI describes the server-authoritative sliding reauthentication window', () => {
  assert.match(settingsSource, /session_idle_timeout_seconds/)
  assert.match(settingsSource, /session_absolute_timeout_seconds/)
  assert.match(settingsSource, /连续空闲 \$\{sessionIdleHours\} 小时后重新验证/)
  assert.match(settingsSource, /密码或 2FA 配置变更会立即撤销全部会话/)
  assert.match(loginSource, /连续空闲 12 小时后需要重新验证/)
})
