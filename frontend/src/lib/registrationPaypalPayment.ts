export const REGISTRATION_PAYPAL_LINK_ENABLED_FIELD =
  'registration_paypal_link_enabled'
export const REGISTRATION_PAYPAL_PAYMENT_ENABLED_FIELD =
  'registration_paypal_payment_enabled'
export const DEFAULT_REGISTRATION_PAYPAL_LINK_ENABLED = false
export const DEFAULT_REGISTRATION_PAYPAL_PAYMENT_ENABLED = false
export const REGISTRATION_PAYPAL_LINK_ENABLED_STORAGE_KEY =
  'auto-chatgpt.registration.paypal-link-enabled.v1'
export const REGISTRATION_PAYPAL_PAYMENT_ENABLED_STORAGE_KEY =
  'auto-chatgpt.registration.paypal-payment-enabled.v1'

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function nonNegativeCount(value: unknown): number {
  const parsed = Number(value || 0)
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 0
}

export function isRegistrationPaypalFollowupActive(taskSnapshot: unknown): boolean {
  const task = asRecord(taskSnapshot)
  const meta = asRecord(task.meta)
  const payment = asRecord(meta.registration_paypal_payment)
  const followup = asRecord(payment.followup)
  return nonNegativeCount(followup.active) > 0
}

function readBooleanStorage(key: string, fallback: boolean): boolean {
  if (typeof window === 'undefined') return fallback
  try {
    const value = String(window.localStorage.getItem(key) || '').trim().toLowerCase()
    return ['1', 'true', 'yes', 'on', 'enabled'].includes(value)
  } catch {
    return fallback
  }
}

export function hasStoredRegistrationPaypalLinkEnabled(): boolean {
  if (typeof window === 'undefined') return false
  try {
    return window.localStorage.getItem(REGISTRATION_PAYPAL_LINK_ENABLED_STORAGE_KEY) !== null
  } catch {
    return false
  }
}

export function readRegistrationPaypalLinkEnabled(): boolean {
  if (hasStoredRegistrationPaypalLinkEnabled()) {
    return readBooleanStorage(
      REGISTRATION_PAYPAL_LINK_ENABLED_STORAGE_KEY,
      DEFAULT_REGISTRATION_PAYPAL_LINK_ENABLED,
    )
  }
  // The old single switch meant extraction plus payment. Preserve that intent
  // until the operator saves the new split settings.
  return readRegistrationPaypalPaymentEnabled()
}

export function readRegistrationPaypalPaymentEnabled(): boolean {
  return readBooleanStorage(
    REGISTRATION_PAYPAL_PAYMENT_ENABLED_STORAGE_KEY,
    DEFAULT_REGISTRATION_PAYPAL_PAYMENT_ENABLED,
  )
}

export function hasStoredRegistrationPaypalPaymentEnabled(): boolean {
  if (typeof window === 'undefined') return false
  try {
    return window.localStorage.getItem(
      REGISTRATION_PAYPAL_PAYMENT_ENABLED_STORAGE_KEY,
    ) !== null
  } catch {
    return false
  }
}

export function writeRegistrationPaypalPaymentEnabled(value: unknown): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(
      REGISTRATION_PAYPAL_PAYMENT_ENABLED_STORAGE_KEY,
      value === true ? 'true' : 'false',
    )
  } catch {
    // Browser storage availability must not block registration.
  }
}

export function writeRegistrationPaypalLinkEnabled(value: unknown): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(
      REGISTRATION_PAYPAL_LINK_ENABLED_STORAGE_KEY,
      value === true ? 'true' : 'false',
    )
  } catch {
    // Browser storage availability must not block registration.
  }
}
