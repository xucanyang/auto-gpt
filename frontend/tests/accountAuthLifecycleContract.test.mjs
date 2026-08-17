import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const detailSource = await readFile(new URL('../src/features/accounts/components/AccountDetailModal.tsx', import.meta.url), 'utf8')

test('account list compresses auth lifecycle into one status column', () => {
  const optionsStart = accountsSource.indexOf('const ACCOUNT_COLUMN_OPTIONS')
  const optionsEnd = accountsSource.indexOf('const ACCOUNT_COLUMN_OPTION_KEYS', optionsStart)
  assert.ok(optionsStart >= 0 && optionsEnd > optionsStart)
  const optionsSource = accountsSource.slice(optionsStart, optionsEnd)

  assert.match(optionsSource, /value: 'auth_type', text: '认证状态'/)
  assert.doesNotMatch(optionsSource, /access_token_expiry|refresh_token_state|account_evidence/)
  assert.match(accountsSource, /const renderAuthLifecycleState = \(record: any, options\?: \{ mobile\?: boolean \}\)/)
  assert.match(accountsSource, /details\.join\(' · '\)/)
  assert.doesNotMatch(accountsSource, /title: 'AT状态\/到期'/)
  assert.doesNotMatch(accountsSource, /title: 'RT刷新'/)
  assert.doesNotMatch(accountsSource, /title: '账号证据'/)
})

test('account detail keeps the complete auth lifecycle evidence', () => {
  assert.match(detailSource, /AT到期|AT 到期|Access Token/)
  assert.match(detailSource, /RT最近|RT 最近|Refresh Token/)
  assert.match(detailSource, /账号证据/)
  assert.match(detailSource, /authLifecycleEvents/)
})
