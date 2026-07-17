import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const settingsSource = await readFile(new URL('../src/pages/Settings.tsx', import.meta.url), 'utf8')

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
