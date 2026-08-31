export const REGISTRATION_ZERO_AMOUNT_ENABLED_FIELD = 'registration_zero_amount_eligibility_enabled'
export const REGISTRATION_ZERO_AMOUNT_COUNTRY_FIELD = 'registration_zero_amount_checkout_country'
export const REGISTRATION_PAYMENT_DETAILS_ENABLED_FIELD = 'registration_payment_details_enabled'
export const DEFAULT_REGISTRATION_ZERO_AMOUNT_ENABLED = false
export const DEFAULT_REGISTRATION_PAYMENT_DETAILS_ENABLED = false
export const DEFAULT_REGISTRATION_ZERO_AMOUNT_COUNTRY = 'VN'
export const REGISTRATION_ZERO_AMOUNT_ENABLED_STORAGE_KEY =
  'auto-chatgpt.registration.zero-amount-enabled.v1'
export const REGISTRATION_ZERO_AMOUNT_COUNTRY_STORAGE_KEY =
  'auto-chatgpt.registration.zero-amount-checkout-country.v1'
export const REGISTRATION_PAYMENT_DETAILS_ENABLED_STORAGE_KEY =
  'auto-chatgpt.registration.payment-details-enabled.v1'

export type RegistrationEligibilityCountryOption = {
  value: string
  label: string
  currency: string
}

function normalizeCountry(value: unknown): string {
  return String(value || '').trim().toUpperCase()
}

function countryLabel(country: string, currency: string): string {
  try {
    const displayNames = new Intl.DisplayNames(['zh-CN'], { type: 'region' })
    return `${displayNames.of(country) || country} (${country}) · ${currency}`
  } catch {
    return `${country} · ${currency}`
  }
}

export function normalizeRegistrationEligibilityCountry(value: unknown): string {
  return normalizeCountry(value)
}

export function readRegistrationEligibilityEnabled(): boolean {
  if (typeof window === 'undefined') return DEFAULT_REGISTRATION_ZERO_AMOUNT_ENABLED
  try {
    const value = String(
      window.localStorage.getItem(REGISTRATION_ZERO_AMOUNT_ENABLED_STORAGE_KEY) || '',
    ).trim().toLowerCase()
    return ['1', 'true', 'yes', 'on', 'enabled'].includes(value)
  } catch {
    return DEFAULT_REGISTRATION_ZERO_AMOUNT_ENABLED
  }
}

export function hasStoredRegistrationEligibilityEnabled(): boolean {
  if (typeof window === 'undefined') return false
  try {
    return window.localStorage.getItem(REGISTRATION_ZERO_AMOUNT_ENABLED_STORAGE_KEY) !== null
  } catch {
    return false
  }
}

export function writeRegistrationEligibilityEnabled(value: unknown): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(
      REGISTRATION_ZERO_AMOUNT_ENABLED_STORAGE_KEY,
      value === true ? 'true' : 'false',
    )
  } catch {
    // Browser storage can be unavailable without blocking registration.
  }
}

export function readRegistrationPaymentDetailsEnabled(): boolean {
  if (typeof window === 'undefined') return DEFAULT_REGISTRATION_PAYMENT_DETAILS_ENABLED
  try {
    const value = String(
      window.localStorage.getItem(REGISTRATION_PAYMENT_DETAILS_ENABLED_STORAGE_KEY) || '',
    ).trim().toLowerCase()
    return ['1', 'true', 'yes', 'on', 'enabled'].includes(value)
  } catch {
    return DEFAULT_REGISTRATION_PAYMENT_DETAILS_ENABLED
  }
}

export function hasStoredRegistrationPaymentDetailsEnabled(): boolean {
  if (typeof window === 'undefined') return false
  try {
    return window.localStorage.getItem(REGISTRATION_PAYMENT_DETAILS_ENABLED_STORAGE_KEY) !== null
  } catch {
    return false
  }
}

export function writeRegistrationPaymentDetailsEnabled(value: unknown): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(
      REGISTRATION_PAYMENT_DETAILS_ENABLED_STORAGE_KEY,
      value === true ? 'true' : 'false',
    )
  } catch {
    // Browser storage can be unavailable without blocking registration.
  }
}

export function normalizeRegistrationEligibilityCountryOptions(
  value: unknown,
): RegistrationEligibilityCountryOption[] {
  if (!Array.isArray(value)) return []
  const options = new Map<string, string>()
  value.forEach((item) => {
    if (!item || typeof item !== 'object') return
    const raw = item as { country?: unknown; currency?: unknown }
    const country = normalizeCountry(raw.country)
    const currency = String(raw.currency || '').trim().toUpperCase()
    if (!/^[A-Z]{2}$/.test(country) || !/^[A-Z]{3}$/.test(currency) || options.has(country)) return
    options.set(country, currency)
  })
  return Array.from(options.entries())
    .sort(([left], [right]) => {
      if (left === DEFAULT_REGISTRATION_ZERO_AMOUNT_COUNTRY) return -1
      if (right === DEFAULT_REGISTRATION_ZERO_AMOUNT_COUNTRY) return 1
      return left.localeCompare(right)
    })
    .map(([country, currency]) => ({
      value: country,
      currency,
      label: countryLabel(country, currency),
    }))
}

export function readRegistrationEligibilityCountry(): string {
  if (typeof window === 'undefined') return ''
  try {
    return normalizeCountry(window.localStorage.getItem(REGISTRATION_ZERO_AMOUNT_COUNTRY_STORAGE_KEY))
  } catch {
    return ''
  }
}

export function writeRegistrationEligibilityCountry(value: unknown): void {
  const country = normalizeCountry(value)
  if (!country || typeof window === 'undefined') return
  try {
    window.localStorage.setItem(REGISTRATION_ZERO_AMOUNT_COUNTRY_STORAGE_KEY, country)
  } catch {
    // Private browsing or a blocked storage policy must not prevent registration.
  }
}
