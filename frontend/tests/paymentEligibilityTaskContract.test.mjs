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
const registrationCountryFieldSource = await readFile(
  new URL('../src/features/auth/components/RegistrationEligibilityCountryField.tsx', import.meta.url),
  'utf8',
)
const registrationCountrySelectSource = await readFile(
  new URL('../src/features/auth/components/RegistrationCountrySelect.tsx', import.meta.url),
  'utf8',
)
const registrationCountryLibSource = await readFile(
  new URL('../src/lib/registrationEligibilityCountry.ts', import.meta.url),
  'utf8',
)
const registrationEligibilitySummarySource = await readFile(
  new URL('../src/features/auth/components/RegistrationEligibilitySummary.tsx', import.meta.url),
  'utf8',
)
const registrationPipelineSummarySource = await readFile(
  new URL('../src/features/auth/components/RegistrationPipelineSummary.tsx', import.meta.url),
  'utf8',
)
const taskLogPanelSource = await readFile(new URL('../src/components/TaskLogPanel.tsx', import.meta.url), 'utf8')
const taskDetailHeaderSource = await readFile(
  new URL('../src/components/task-detail/TaskDetailHeader.tsx', import.meta.url),
  'utf8',
)
const failureReasonSource = await readFile(
  new URL('../src/lib/paymentEligibilityFailure.ts', import.meta.url),
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

test('combined payment eligibility uses one bundle route and exposes independent summaries', () => {
  const handlersStart = accountsSource.indexOf('const startPaymentEligibilityTask = async')
  const handlersEnd = accountsSource.indexOf('const submitInvalidRecheckConfig = async', handlersStart)
  const handlers = accountsSource.slice(handlersStart, handlersEnd)

  assert.match(handlers, /payment_eligibility_bundle/)
  assert.match(handlers, /'payment-eligibility'/)
  assert.ok(handlers.includes('/tasks/chatgpt/' + '${endpointName}' + '/batch'))
  assert.match(accountsSource, /一键检测支付资格（0 元 \+ 链接格式 \+ 支付方式/)
  assert.match(accountsSource, /PAYMENT_ELIGIBILITY_BUNDLE_COUNTRY_STORAGE_KEY/)
  assert.match(accountsSource, /loadPaymentEligibilityBundleCountry\(\)/)
  assert.match(accountsSource, /savePaymentEligibilityBundleCountry\(checkoutCountryCode\)/)
  assert.match(taskTypesSource, /payment_eligibility_bundle: '一键支付资格检测'/)
  assert.match(taskTypesSource, /batch_payment_eligibility_bundle: '批量一键支付资格检测'/)
  assert.match(taskLogPanelSource, /bundleZeroSummary/)
  assert.match(taskDetailHeaderSource, /source\.includes\('payment_eligibility_bundle'\)/)
})

test('batch payment eligibility exposes and persists arbitrary positive concurrency', () => {
  assert.match(accountsSource, /PAYMENT_ELIGIBILITY_CONCURRENCY_STORAGE_KEY/)
  assert.match(accountsSource, /loadPaymentEligibilityConcurrency\(\)/)
  assert.match(accountsSource, /savePaymentEligibilityConcurrency\(concurrency\)/)
  assert.match(accountsSource, /params: \{ concurrency, max_attempts: maxAttempts, \.\.\.proxyPayload, \.\.\.checkoutCountryPayload, checkout_transport: checkoutTransport \}/)
  assert.match(accountsSource, /label="并发数"/)
  assert.match(accountsSource, /return Math\.max\(1, Math\.floor\(parsed\)\)/)
  assert.doesNotMatch(accountsSource, /PAYMENT_ELIGIBILITY_MAX_CONCURRENCY/)
  assert.doesNotMatch(accountsSource, /payment_eligibility_concurrency/)
})

test('payment-method checks default to Browser with three attempts and retain Protocol', () => {
  assert.match(accountsSource, /defaultPaymentEligibilityCheckoutTransport[\s\S]+kind === 'payment_methods'[\s\S]+\? 'browser'[\s\S]+: 'protocol'/)
  assert.match(accountsSource, /kind === 'payment_methods' && checkoutTransport === 'browser'[\s\S]+\? 3[\s\S]+: 2/)
  assert.match(accountsSource, /max_attempts: maxAttempts/)
  assert.match(accountsSource, /value: 'browser', label: '浏览器（Patchright Chromium）'/)
  assert.match(accountsSource, /value: 'protocol', label: '协议（curl_cffi，回滚）'/)
})

test('payment eligibility task summary refreshes while the task is running', () => {
  assert.match(taskLogPanelSource, /taskSource\.includes\('zero_amount_eligibility'\)/)
  assert.match(taskLogPanelSource, /cache: 'no-store'/)
  assert.match(taskLogPanelSource, /setTaskSnapshot\(snapshot\)/)
  assert.match(taskLogPanelSource, /window\.setTimeout\(poll, 500\)/)
})

test('payment eligibility failures expose structured and legacy-compatible reasons', () => {
  for (const category of [
    'network_error',
    'checkout_create_failed',
    'auth_error',
    'proxy_error',
    'upstream_error',
    'protocol_error',
    'configuration_error',
    'other_error',
  ]) {
    assert.ok(failureReasonSource.includes(category))
  }
  assert.ok(failureReasonSource.includes('网络问题'))
  assert.ok(failureReasonSource.includes('无法创建 Checkout'))
  assert.ok(failureReasonSource.includes('checkout 创建 http'))
  assert.ok(failureReasonSource.includes('detected unusual activity'))
  assert.match(taskLogPanelSource, /eligibility_failure_summary/)
  assert.match(taskLogPanelSource, /paymentEligibilityFailureBreakdown/)
  assert.match(taskLogPanelSource, /checkout_link_type/)
  assert.match(taskDetailHeaderSource, /paymentEligibilityFailureBreakdown/)
  assert.match(taskDetailHeaderSource, /checkout_link_type/)
  assert.match(accountsSource, /paymentEligibilityFailureMeta\(zero\)/)
  assert.match(accountsSource, /paymentEligibilityFailureMeta\(pm\)/)
  assert.match(accountsSource, /paymentEligibilityFailureMeta\(lastAttempt\)/)
  assert.match(registrationEligibilitySummarySource, /paymentEligibilityFailureBreakdown/)
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
  assert.match(accountsSource, /visible-columns\.v5/)
  assert.match(accountsSource, /LEGACY_ACCOUNT_COLUMN_VISIBILITY_STORAGE_KEYS/)
  assert.match(accountsSource, /!legacyColumns\.includes\('zero_amount_eligibility'\)/)
  assert.match(accountsSource, /value: 'no_methods', text: '无可用方式'/)
  assert.match(accountsSource, /item\.toLowerCase\(\) === 'unavailable' \? 'no_methods'/)
  assert.match(accountsSource, /placeholder="支付方式"/)
  assert.doesNotMatch(accountsSource, /placeholder="GCash 方式"/)
})

test('registration surfaces show zero-amount outcomes in one pipeline summary', () => {
  for (const source of [registerModalSource, registerPageSource]) {
    assert.match(source, /RegistrationPipelineSummary/)
    assert.match(source, /registration_zero_amount_eligibility/)
  }
  assert.match(registrationPipelineSummarySource, /注册链路汇总/)
  assert.match(registrationPipelineSummarySource, /0 元有资格/)
  assert.match(registrationPipelineSummarySource, /非 0 元/)
  assert.match(registrationPipelineSummarySource, /0 元失败/)
  assert.match(registrationPipelineSummarySource, /Auth 待补/)
  assert.match(accountsSource, /zero\.amount_display/)
  assert.match(accountsSource, /0 元检测中/)
  assert.match(accountsSource, /0 元检测失败/)
  assert.match(accountsSource, /0 元待补 Auth/)
})

test('registration surfaces expose an optional persisted zero-amount check and frozen country', () => {
  for (const source of [registerModalSource, registerPageSource]) {
    assert.match(source, /RegistrationEligibilityCountryField/)
  }
  for (const source of [accountsSource, registerPageSource]) {
    assert.match(source, /registration_zero_amount_eligibility_enabled/)
    assert.match(source, /registration_zero_amount_checkout_country/)
    assert.match(source, /REGISTRATION_ZERO_AMOUNT_ENABLED_FIELD/)
    assert.match(source, /REGISTRATION_ZERO_AMOUNT_COUNTRY_FIELD/)
  }
  assert.ok(registrationCountryFieldSource.includes('label="注册后 0 元检测"'))
  assert.ok(registrationCountryFieldSource.includes('label="注册后 0 元检测国家"'))
  assert.ok(registrationCountryFieldSource.includes('<Switch checkedChildren="开启" unCheckedChildren="关闭" />'))
  assert.ok(registrationCountryFieldSource.includes('getValueFromEvent'))
  assert.ok(registrationCountryFieldSource.includes('writeRegistrationEligibilityEnabled(next)'))
  assert.ok(registrationCountryFieldSource.includes('writeRegistrationEligibilityCountry(country)'))
  assert.ok(registrationCountrySelectSource.includes("'/tasks/chatgpt/zero-amount-eligibility/profile'"))
  assert.ok(registrationCountrySelectSource.includes('optionFilterProp="label"'))
  assert.ok(registrationCountrySelectSource.includes('showSearch'))
  assert.match(registrationCountryLibSource, /DEFAULT_REGISTRATION_ZERO_AMOUNT_ENABLED = false/)
  assert.match(registrationCountryLibSource, /DEFAULT_REGISTRATION_ZERO_AMOUNT_COUNTRY = 'VN'/)
  assert.match(registrationCountryLibSource, /REGISTRATION_ZERO_AMOUNT_ENABLED_STORAGE_KEY/)
  assert.match(registrationCountryLibSource, /REGISTRATION_ZERO_AMOUNT_COUNTRY_STORAGE_KEY/)
  assert.match(accountsSource, /writeRegistrationEligibilityEnabled/)
  assert.match(accountsSource, /writeRegistrationEligibilityCountry/)
  assert.match(
    registerPageSource,
    /\[REGISTRATION_ZERO_AMOUNT_COUNTRY_FIELD\]:\s*readRegistrationEligibilityCountry\(\) \|\| DEFAULT_REGISTRATION_ZERO_AMOUNT_COUNTRY/,
  )
  assert.match(
    registerPageSource,
    /\[REGISTRATION_ZERO_AMOUNT_ENABLED_FIELD\]: readRegistrationEligibilityEnabled\(\)/,
  )
})

test('registration proxy country uses the shared searchable country directory', () => {
  for (const source of [registerModalSource, registerPageSource]) {
    assert.match(
      source,
      /name="proxy_country_code"[\s\S]{0,220}label="注册出口国家"[\s\S]{0,420}<RegistrationCountrySelect/,
    )
    assert.doesNotMatch(
      source,
      /name="proxy_country_code"[\s\S]{0,420}<Input[^>]+maxLength=\{2\}/,
    )
  }
})
