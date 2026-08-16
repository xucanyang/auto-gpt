export const REGISTRATION_PAYPAL_PAYMENT_ENABLED_FIELD =
  'registration_paypal_payment_enabled'
export const DEFAULT_REGISTRATION_PAYPAL_PAYMENT_ENABLED = false
export const REGISTRATION_PAYPAL_PAYMENT_ENABLED_STORAGE_KEY =
  'auto-chatgpt.registration.paypal-payment-enabled.v1'

export function readRegistrationPaypalPaymentEnabled(): boolean {
  if (typeof window === 'undefined') return DEFAULT_REGISTRATION_PAYPAL_PAYMENT_ENABLED
  try {
    const value = String(
      window.localStorage.getItem(REGISTRATION_PAYPAL_PAYMENT_ENABLED_STORAGE_KEY) || '',
    ).trim().toLowerCase()
    return ['1', 'true', 'yes', 'on', 'enabled'].includes(value)
  } catch {
    return DEFAULT_REGISTRATION_PAYPAL_PAYMENT_ENABLED
  }
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
