import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const toolbarSource = await readFile(
  new URL('../src/features/accounts/components/AccountsToolbar.tsx', import.meta.url),
  'utf8',
)
const actionSurfaceSource = await readFile(
  new URL('../src/features/accounts/components/AccountActionSurface.tsx', import.meta.url),
  'utf8',
)
const taskTypesSource = await readFile(new URL('../src/lib/taskTypes.ts', import.meta.url), 'utf8')
const querySource = await readFile(
  new URL('../src/features/accounts/hooks/useAccountsQuery.ts', import.meta.url),
  'utf8',
)
const registerModalSource = await readFile(
  new URL('../src/features/auth/components/RegisterTaskModal.tsx', import.meta.url),
  'utf8',
)
const registerPageSource = await readFile(new URL('../src/pages/RegisterTaskPage.tsx', import.meta.url), 'utf8')
const registrationSummarySource = await readFile(
  new URL('../src/features/auth/components/RegistrationEligibilitySummary.tsx', import.meta.url),
  'utf8',
)

test('zero-amount and GCash checks use independent single and batch task routes', () => {
  const handlersStart = accountsSource.indexOf('const startPaymentEligibilityTask = async')
  const handlersEnd = accountsSource.indexOf('const submitInvalidRecheckConfig = async', handlersStart)
  const handlers = accountsSource.slice(handlersStart, handlersEnd)

  assert.match(handlers, /'zero-amount-eligibility'/)
  assert.match(handlers, /'gcash-payment-method'/)
  assert.match(handlers, /\/batch`/)
  assert.match(handlers, /handlePaymentEligibility[\s\S]+kind === 'gcash_payment_method'[\s\S]+startPaymentEligibilityTask\(kind, 'single', record\)/)
  assert.match(handlers, /handlePaymentEligibility[\s\S]+setPaymentEligibilityConfigMode\('single'\)[\s\S]+setPaymentEligibilityConfigOpen\(true\)/)
  assert.match(handlers, /handleBatchPaymentEligibility[\s\S]+setPaymentEligibilityConfigOpen\(true\)/)
  assert.match(handlers, /submitPaymentEligibilityConfig[\s\S]+startPaymentEligibilityTask\([\s\S]+paymentEligibilityConfigMode/)
  assert.match(handlers, /setTaskModalMode\('payment_eligibility'\)/)
})

test('batch payment eligibility exposes and persists bounded concurrency', () => {
  assert.match(accountsSource, /PAYMENT_ELIGIBILITY_CONCURRENCY_STORAGE_KEY/)
  assert.match(accountsSource, /PAYMENT_ELIGIBILITY_MAX_CONCURRENCY = 10/)
  assert.match(accountsSource, /loadPaymentEligibilityConcurrency\(\)/)
  assert.match(accountsSource, /savePaymentEligibilityConcurrency\(concurrency\)/)
  assert.match(accountsSource, /params: \{ concurrency, max_attempts: 2, \.\.\.proxyPayload, \.\.\.promotionProxyPayload \}/)
  assert.match(accountsSource, /label=\{`并发数（1-\$\{PAYMENT_ELIGIBILITY_MAX_CONCURRENCY\}）`\}/)
  assert.match(accountsSource, /max=\{PAYMENT_ELIGIBILITY_MAX_CONCURRENCY\}/)
  assert.doesNotMatch(accountsSource, /payment_eligibility_concurrency/)
})

test('zero-amount checks select and persist a promotion proxy country without changing GCash', () => {
  const handlersStart = accountsSource.indexOf('const startPaymentEligibilityTask = async')
  const handlersEnd = accountsSource.indexOf('const submitInvalidRecheckConfig = async', handlersStart)
  const handlers = accountsSource.slice(handlersStart, handlersEnd)

  assert.match(accountsSource, /ZERO_AMOUNT_PROMOTION_COUNTRY_STORAGE_KEY = 'auto-chatgpt\.accounts\.zero-amount-promotion-country\.v1'/)
  assert.match(accountsSource, /DEFAULT_ZERO_AMOUNT_PROMOTION_COUNTRY = 'VN'/)
  assert.match(accountsSource, /loadZeroAmountPromotionCountry\(\)/)
  assert.match(accountsSource, /saveZeroAmountPromotionCountry\(promotionProxyCountryCode\)/)
  assert.match(accountsSource, /label="优惠检测代理国家"/)
  assert.match(accountsSource, /paymentEligibilityConfigKind === 'zero_amount_eligibility'[\s\S]+name="promotion_proxy_country_code"[\s\S]+<Select/)
  assert.match(accountsSource, /showSearch[\s\S]+optionFilterProp="label"[\s\S]+paymentEligibilityPromotionCountryOptions/)
  assert.match(handlers, /kind === 'zero_amount_eligibility'[\s\S]+promotion_proxy_country_code:/)
  assert.match(handlers, /kind === 'gcash_payment_method'[\s\S]+startPaymentEligibilityTask\(kind, 'single', record\)/)
  assert.doesNotMatch(handlers, /kind === 'gcash_payment_method'[\s\S]{0,180}promotion_proxy_country_code/)
})

test('account actions and toolbar keep both payment eligibility operations separate', () => {
  assert.match(actionSurfaceSource, /actionId === 'zero_amount_eligibility' && onPaymentEligibilityTask/)
  assert.match(actionSurfaceSource, /onPaymentEligibilityTask\(acc, 'zero_amount_eligibility'\)/)
  assert.match(actionSurfaceSource, /actionId === 'gcash_payment_method' && onPaymentEligibilityTask/)
  assert.match(actionSurfaceSource, /onPaymentEligibilityTask\(acc, 'gcash_payment_method'\)/)

  assert.match(toolbarSource, /'支付资格检测'/)
  assert.match(accountsSource, /key: 'zero_amount_eligibility'[\s\S]{0,180}批量检测 0 元试用资格/)
  assert.match(accountsSource, /key: 'gcash_payment_method'[\s\S]{0,180}批量检测 GCash 支付方式/)
})

test('account list exposes independent states, filters, and task labels', () => {
  assert.match(accountsSource, /title:[\s\S]{0,120}'支付资格'/)
  assert.match(accountsSource, /label: '0 元可用'/)
  assert.match(accountsSource, /label: 'GCash 可用'/)
  assert.match(querySource, /params\.set\('zero_amount_eligibility_state', zeroAmountEligibilityState\)/)
  assert.match(querySource, /params\.set\('gcash_payment_method_state', gcashPaymentMethodState\)/)
  assert.match(taskTypesSource, /zero_amount_eligibility: '0 元试用资格检测'/)
  assert.match(taskTypesSource, /gcash_payment_method: 'GCash 支付方式检测'/)
  assert.match(accountsSource, /visible-columns\.v4/)
  assert.match(accountsSource, /LEGACY_ACCOUNT_COLUMN_VISIBILITY_STORAGE_KEYS/)
  assert.match(accountsSource, /!legacyColumns\.includes\('payment_eligibility'\)/)
})

test('registration surfaces show automatic zero-amount progress and terminal outcomes', () => {
  for (const source of [registerModalSource, registerPageSource]) {
    assert.match(source, /RegistrationEligibilitySummary/)
    assert.match(source, /registration_zero_amount_eligibility/)
  }
  assert.match(registrationSummarySource, /待检测/)
  assert.match(registrationSummarySource, /检测中/)
  assert.match(registrationSummarySource, /0 元可用/)
  assert.match(registrationSummarySource, /非 0 元/)
  assert.match(registrationSummarySource, /检测失败/)
  assert.match(registrationSummarySource, /待补 Auth/)
  assert.match(accountsSource, /0 元检测中/)
  assert.match(accountsSource, /0 元检测失败/)
  assert.match(accountsSource, /0 元待补 Auth/)
})
