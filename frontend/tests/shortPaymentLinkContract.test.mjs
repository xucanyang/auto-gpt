import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

test('payment-link modal exposes the login-bound Plus short-link mode', () => {
  assert.match(accountsSource, /label: 'Plus 登录态短链', value: 'short'/)
  assert.match(accountsSource, /登录态短链要求账号保存 Web Session/)
  assert.match(accountsSource, /缺少 Web Session 的账号不会进入生成队列/)
  assert.match(accountsSource, /\/tasks\/chatgpt\/payment-links\/short-profile/)
})

test('short-link submission freezes the local source, format, country and currency', () => {
  const builder = accountsSource.match(/const buildBatchPaymentLinkParams = [\s\S]+?\n  const loadBatchPaymentLinkProfile/)?.[0] || ''
  const submitter = accountsSource.match(/const submitBatchPaymentLinkConfig = async \(\) => \{[\s\S]+?\n  const handleInvalidRecheck/)?.[0] || ''

  assert.match(builder, /payment_source: SHORT_PAYMENT_SOURCE/)
  assert.match(builder, /payment_link_format: SHORT_PAYMENT_LINK_FORMAT/)
  assert.match(builder, /short_country/)
  assert.match(builder, /short_currency/)
  assert.match(submitter, /const isShortPayment = batchPaymentLinkPlan === 'short'/)
  assert.match(submitter, /if \(isShortPayment\)/)
  assert.match(submitter, /CHATGPT SHORT/)
})
