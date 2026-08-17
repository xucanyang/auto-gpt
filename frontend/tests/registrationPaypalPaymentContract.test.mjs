import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const pageSource = await readFile(new URL('../src/pages/RegisterTaskPage.tsx', import.meta.url), 'utf8')
const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const modalSource = await readFile(new URL('../src/features/auth/components/RegisterTaskModal.tsx', import.meta.url), 'utf8')
const fieldSource = await readFile(new URL('../src/features/auth/components/RegistrationPaypalPaymentField.tsx', import.meta.url), 'utf8')
const summarySource = await readFile(new URL('../src/features/auth/components/RegistrationPaypalPaymentSummary.tsx', import.meta.url), 'utf8')
const libSource = await readFile(new URL('../src/lib/registrationPaypalPayment.ts', import.meta.url), 'utf8')

test('both registration entrypoints submit and persist the PayPal post-registration switch', () => {
  for (const source of [pageSource, accountsSource]) {
    assert.match(source, /REGISTRATION_PAYPAL_PAYMENT_ENABLED_FIELD/)
    assert.match(source, /registration_paypal_payment_enabled/)
  }
  assert.match(accountsSource, /writeRegistrationPaypalPaymentEnabled/)
  assert.match(accountsSource, /savedRegistrationPaypalPaymentEnabled/)
  assert.match(pageSource, /readRegistrationPaypalPaymentEnabled/)
  assert.match(modalSource, /RegistrationPaypalPaymentField/)
  assert.match(pageSource, /RegistrationPaypalPaymentField/)
})

test('PayPal registration field defaults off and explicitly warns about real payment', () => {
  assert.match(libSource, /DEFAULT_REGISTRATION_PAYPAL_PAYMENT_ENABLED = false/)
  assert.match(libSource, /REGISTRATION_PAYPAL_PAYMENT_ENABLED_STORAGE_KEY/)
  assert.match(fieldSource, /label="注册后提链并支付"/)
  assert.match(fieldSource, /真实 PayPal 支付/)
  assert.match(fieldSource, /writeRegistrationPaypalPaymentEnabled\(next\)/)
})

test('registration task surfaces expose queue handoff states without claiming payment success', () => {
  for (const source of [pageSource, modalSource]) {
    assert.match(source, /RegistrationPaypalPaymentSummary/)
    assert.match(source, /registration_paypal_payment/)
  }
  assert.match(summarySource, /已交支付队列/)
  assert.match(summarySource, /提链失败/)
  assert.match(summarySource, /入队失败/)
  assert.match(summarySource, /待补 Auth/)
  assert.match(summarySource, /PayPal Buyer \/ 代理/)
  assert.match(summarySource, /submitted_results/)
  assert.match(summarySource, /state\) === 'submitted'/)
  assert.match(summarySource, /提链成功并已提交支付队列/)
  assert.match(summarySource, /不代表最终支付完成/)
  assert.match(summarySource, /复制成功账号/)
  assert.match(summarySource, /复制账号 ID \+ 邮箱/)
})
