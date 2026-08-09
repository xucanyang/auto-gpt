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

test('zero-amount and GCash checks use independent single and batch task routes', () => {
  const handlersStart = accountsSource.indexOf('const startPaymentEligibilityTask = async')
  const handlersEnd = accountsSource.indexOf('const submitInvalidRecheckConfig = async', handlersStart)
  const handlers = accountsSource.slice(handlersStart, handlersEnd)

  assert.match(handlers, /'zero-amount-eligibility'/)
  assert.match(handlers, /'gcash-payment-method'/)
  assert.match(handlers, /\/batch`/)
  assert.match(handlers, /handlePaymentEligibility[\s\S]+startPaymentEligibilityTask\(kind, 'single', record\)/)
  assert.match(handlers, /handleBatchPaymentEligibility[\s\S]+startPaymentEligibilityTask\(kind, 'batch'\)/)
  assert.match(handlers, /setTaskModalMode\('payment_eligibility'\)/)
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
