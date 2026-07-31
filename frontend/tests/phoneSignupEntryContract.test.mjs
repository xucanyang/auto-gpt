import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const registerPageSource = await readFile(new URL('../src/pages/RegisterTaskPage.tsx', import.meta.url), 'utf8')
const registerModalSource = await readFile(new URL('../src/features/auth/components/RegisterTaskModal.tsx', import.meta.url), 'utf8')
const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

test('both ChatGPT registration surfaces expose the separate phone signup entry', () => {
  for (const source of [registerPageSource, registerModalSource]) {
    assert.match(source, /value: 'email_signup', label: '邮箱注册'/)
    assert.match(source, /value: 'phone_signup', label: '手机号注册'/)
  }
})

test('the ChatGPT account modal submits the existing phone signup contract', () => {
  const handler = accountsSource.match(/const handleRegister = async \(\) => \{[\s\S]+?\n  const handleDetailSave = async/)?.[0] || ''

  assert.match(handler, /const phoneSignupEnabled =/)
  assert.match(handler, /chatgpt_registration_entry:\s*[\s\S]*phoneSignupEnabled \? 'phone_signup' : 'email_signup'/)
  assert.match(handler, /chatgpt_phone_signup_password: phoneSignupEnabled \? normalizedLoginPassword : undefined/)
  assert.match(handler, /chatgpt_phone_signup_use_pool: phoneSignupEnabled \? Boolean\(values\.chatgpt_phone_signup_use_pool\) : undefined/)
  assert.match(handler, /chatgpt_phone_signup_phone_lines: phoneSignupEnabled \? String\(values\.chatgpt_phone_signup_phone_lines \|\| ''\)\.trim\(\) : undefined/)
  assert.match(handler, /const adaptedRegisterExtra = phoneSignupEnabled/)
  assert.match(handler, /concurrency: phoneSignupEnabled \? 1 : values\.concurrency/)
  assert.match(handler, /password: phoneSignupEnabled[\s\S]+?\? normalizedLoginPassword/)
})
