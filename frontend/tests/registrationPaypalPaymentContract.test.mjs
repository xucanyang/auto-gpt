import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const pageSource = await readFile(new URL('../src/pages/RegisterTaskPage.tsx', import.meta.url), 'utf8')
const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const modalSource = await readFile(new URL('../src/features/auth/components/RegisterTaskModal.tsx', import.meta.url), 'utf8')
const fieldSource = await readFile(new URL('../src/features/auth/components/RegistrationPaypalPaymentField.tsx', import.meta.url), 'utf8')
const summarySource = await readFile(new URL('../src/features/auth/components/RegistrationPipelineSummary.tsx', import.meta.url), 'utf8')
const pipelineLibSource = await readFile(new URL('../src/lib/registrationPipeline.ts', import.meta.url), 'utf8')
const libSource = await readFile(new URL('../src/lib/registrationPaypalPayment.ts', import.meta.url), 'utf8')

test('both registration entrypoints submit and persist split PayPal link and payment switches', () => {
  for (const source of [pageSource, accountsSource]) {
    assert.match(source, /REGISTRATION_PAYPAL_LINK_ENABLED_FIELD/)
    assert.match(source, /registration_paypal_link_enabled/)
    assert.match(source, /REGISTRATION_PAYPAL_PAYMENT_ENABLED_FIELD/)
    assert.match(source, /registration_paypal_payment_enabled/)
  }
  assert.match(accountsSource, /writeRegistrationPaypalLinkEnabled/)
  assert.match(accountsSource, /writeRegistrationPaypalPaymentEnabled/)
  assert.match(accountsSource, /savedRegistrationPaypalLinkEnabled/)
  assert.match(accountsSource, /savedRegistrationPaypalPaymentEnabled/)
  assert.match(pageSource, /readRegistrationPaypalPaymentEnabled/)
  assert.match(modalSource, /RegistrationPaypalPaymentField/)
  assert.match(pageSource, /RegistrationPaypalPaymentField/)
})

test('PayPal registration fields are dependency ordered and warn only on the real payment stage', () => {
  assert.match(libSource, /DEFAULT_REGISTRATION_PAYPAL_LINK_ENABLED = false/)
  assert.match(libSource, /DEFAULT_REGISTRATION_PAYPAL_PAYMENT_ENABLED = false/)
  assert.match(libSource, /REGISTRATION_PAYPAL_LINK_ENABLED_STORAGE_KEY/)
  assert.match(libSource, /REGISTRATION_PAYPAL_PAYMENT_ENABLED_STORAGE_KEY/)
  assert.match(fieldSource, /label="有 0 元资格后提链"/)
  assert.match(fieldSource, /label="提链成功后提交支付"/)
  assert.match(fieldSource, /disabled=\{!zeroEnabled\}/)
  assert.match(fieldSource, /disabled=\{!zeroEnabled \|\| !linkEnabled\}/)
  assert.match(fieldSource, /真实 PayPal 支付/)
  assert.match(fieldSource, /writeRegistrationPaypalLinkEnabled\(next\)/)
  assert.match(fieldSource, /writeRegistrationPaypalPaymentEnabled\(next\)/)
})

test('registration task surfaces use one compact pipeline summary', () => {
  for (const source of [pageSource, modalSource]) {
    assert.match(source, /RegistrationPipelineSummary/)
    assert.doesNotMatch(source, /RegistrationPaypalPaymentSummary/)
    assert.match(source, /registration_paypal_payment/)
  }
  assert.match(summarySource, /注册链路汇总/)
  assert.match(summarySource, /提链成功/)
  assert.match(summarySource, /支付已入队/)
  assert.match(summarySource, /提链失败/)
  assert.match(summarySource, /支付提交失败/)
  assert.match(summarySource, /支付处理中/)
  assert.match(summarySource, /支付成功/)
  assert.match(summarySource, /支付失败/)
  assert.match(summarySource, /支付结果未知/)
  assert.match(summarySource, /payFollowup\.succeeded/)
  assert.match(summarySource, /Auth 待补/)
  assert.match(summarySource, /paymentEnabled/)
  assert.match(accountsSource, /key: 'registration_pipeline'/)
  assert.match(accountsSource, /registrationPipelineIsActive/)
  assert.match(pipelineLibSource, /支付处理中/)
  assert.match(pipelineLibSource, /支付成功/)
  assert.match(pipelineLibSource, /支付失败/)

  const defaultsStart = accountsSource.indexOf('const DEFAULT_VISIBLE_ACCOUNT_COLUMNS')
  const defaultsEnd = accountsSource.indexOf('const ACCOUNT_COLUMN_OPTION_KEYS', defaultsStart)
  const defaults = accountsSource.slice(defaultsStart, defaultsEnd)
  assert.match(defaults, /'registration_pipeline'/)
  assert.doesNotMatch(defaults, /'zero_amount_eligibility'/)
  assert.doesNotMatch(defaults, /'payment_link'/)
})

test('terminal registration tasks keep polling only while durable payment followup is active and visible', () => {
  assert.match(libSource, /isRegistrationPaypalFollowupActive/)
  assert.match(libSource, /followup\.active/)

  assert.match(
    pageSource,
    /if \(isRegistrationPaypalFollowupActive\(normalizedTask\)\) \{\s*scheduleNextPull\(\)/,
  )
  assert.match(pageSource, /document\.visibilityState !== 'visible'/)
  assert.match(pageSource, /void pollTask\(restoredTask\.id\)/)
  assert.match(pageSource, /meta: persistedTask\?\.meta/)

  assert.match(
    accountsSource,
    /\|\| isRegistrationPaypalFollowupActive\(snapshot\)/,
  )
  assert.match(accountsSource, /if \(!taskId \|\| !registerModalOpen\)/)
  assert.match(accountsSource, /if \(!pageVisible\) return/)
})
