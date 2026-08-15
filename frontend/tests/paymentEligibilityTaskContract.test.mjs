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
const taskLogPanelSource = await readFile(new URL('../src/components/TaskLogPanel.tsx', import.meta.url), 'utf8')

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

test('batch payment eligibility exposes and persists arbitrary positive concurrency', () => {
  assert.match(accountsSource, /PAYMENT_ELIGIBILITY_CONCURRENCY_STORAGE_KEY/)
  assert.match(accountsSource, /loadPaymentEligibilityConcurrency\(\)/)
  assert.match(accountsSource, /savePaymentEligibilityConcurrency\(concurrency\)/)
  assert.match(accountsSource, /params: \{ concurrency, max_attempts: 2, \.\.\.proxyPayload, \.\.\.checkoutCountryPayload \}/)
  assert.match(accountsSource, /label="并发数"/)
  assert.match(accountsSource, /return Math\.max\(1, Math\.floor\(parsed\)\)/)
  assert.doesNotMatch(accountsSource, /PAYMENT_ELIGIBILITY_MAX_CONCURRENCY/)
  assert.doesNotMatch(accountsSource, /payment_eligibility_concurrency/)
})

test('payment eligibility task summary refreshes while the task is running', () => {
  assert.match(taskLogPanelSource, /taskSource\.includes\('zero_amount_eligibility'\)/)
  assert.match(taskLogPanelSource, /cache: 'no-store'/)
  assert.match(taskLogPanelSource, /setTaskSnapshot\(snapshot\)/)
  assert.match(taskLogPanelSource, /window\.setTimeout\(poll, 500\)/)
})

test('zero-amount checks select and persist one checkout country without changing GCash', () => {
  const handlersStart = accountsSource.indexOf('const startPaymentEligibilityTask = async')
  const handlersEnd = accountsSource.indexOf('const submitInvalidRecheckConfig = async', handlersStart)
  const handlers = accountsSource.slice(handlersStart, handlersEnd)

  assert.match(accountsSource, /ZERO_AMOUNT_CHECKOUT_COUNTRY_STORAGE_KEY = 'auto-chatgpt\.accounts\.zero-amount-checkout-country\.v2'/)
  assert.match(accountsSource, /LEGACY_ZERO_AMOUNT_PROMOTION_COUNTRY_STORAGE_KEY/)
  assert.match(accountsSource, /DEFAULT_ZERO_AMOUNT_CHECKOUT_COUNTRY = 'VN'/)
  assert.match(accountsSource, /loadZeroAmountCheckoutCountry\(\)/)
  assert.match(accountsSource, /saveZeroAmountCheckoutCountry\(checkoutCountryCode\)/)
  assert.match(accountsSource, /label="结账国家"/)
  assert.match(accountsSource, /paymentEligibilityConfigKind === 'zero_amount_eligibility'[\s\S]+name="checkout_country_code"[\s\S]+<Select/)
  assert.match(accountsSource, /showSearch[\s\S]+optionFilterProp="label"[\s\S]+paymentEligibilityCheckoutCountryOptions/)
  assert.match(accountsSource, /\/tasks\/chatgpt\/zero-amount-eligibility\/profile/)
  assert.match(accountsSource, /normalizeTeamBillingCountryOptions\(profile\?\.billing_country_options\)/)
  assert.match(handlers, /kind === 'zero_amount_eligibility'[\s\S]+checkout_country_code:/)
  assert.match(handlers, /kind === 'gcash_payment_method'[\s\S]+startPaymentEligibilityTask\(kind, 'single', record\)/)
  assert.doesNotMatch(handlers, /kind === 'gcash_payment_method'[\s\S]{0,180}checkout_country_code/)
})

test('account actions and toolbar keep both payment eligibility operations separate', () => {
  assert.match(actionSurfaceSource, /actionId === 'zero_amount_eligibility' && onPaymentEligibilityTask/)
  assert.match(actionSurfaceSource, /onPaymentEligibilityTask\(acc, 'zero_amount_eligibility'\)/)
  assert.match(actionSurfaceSource, /actionId === 'payment_methods' && onPaymentEligibilityTask/)
  assert.match(actionSurfaceSource, /actionId === 'gcash_payment_method' && onPaymentEligibilityTask/)

  assert.match(toolbarSource, /'支付资格检测'/)
  assert.match(accountsSource, /key: 'zero_amount_eligibility'[\s\S]{0,180}批量检测 0 元试用资格/)
  assert.match(accountsSource, /key: 'payment_methods'[\s\S]{0,180}批量检测支付方式/)
})

test('account list exposes independent states, filters, and task labels', () => {
  assert.match(accountsSource, /title:[\s\S]{0,120}'0元资格'/)
  assert.match(accountsSource, /title:[\s\S]{0,120}'支付方式'/)
  assert.match(accountsSource, /label: '0 元可用'/)
  assert.match(accountsSource, /value: 'probe_failed', text: '检测失败'/)
  assert.match(querySource, /params\.set\('zero_amount_eligibility_state', zeroAmountEligibilityState\)/)
  assert.match(querySource, /params\.set\('gcash_payment_method_state', gcashPaymentMethodState\)/)
  assert.match(taskTypesSource, /zero_amount_eligibility: '0 元试用资格检测'/)
  assert.match(taskTypesSource, /payment_methods: '支付方式检测'/)
  assert.match(accountsSource, /visible-columns\.v4/)
  assert.match(accountsSource, /LEGACY_ACCOUNT_COLUMN_VISIBILITY_STORAGE_KEYS/)
  assert.match(accountsSource, /!legacyColumns\.includes\('zero_amount_eligibility'\)/)
  assert.match(accountsSource, /value: 'no_methods', text: '无可用方式'/)
  assert.match(accountsSource, /item\.toLowerCase\(\) === 'unavailable' \? 'no_methods'/)
  assert.match(accountsSource, /placeholder="支付方式"/)
  assert.doesNotMatch(accountsSource, /placeholder="GCash 方式"/)
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
  assert.match(registrationSummarySource, /amount_display/)
  assert.match(accountsSource, /zero\.amount_display/)
  assert.match(accountsSource, /0 元检测中/)
  assert.match(accountsSource, /0 元检测失败/)
  assert.match(accountsSource, /0 元待补 Auth/)
})
