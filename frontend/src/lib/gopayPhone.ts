export const DEFAULT_GOPAY_PHONE_COUNTRY_CODE = '62'

export function normalizeGopayPhonePart(value: unknown) {
  return String(value || '').replace(/\D/g, '')
}

export function normalizeGopayRecognizedCountryCodes(value: unknown): string[] {
  const rawItems = Array.isArray(value)
    ? value.map((item) => normalizeGopayPhonePart(item))
    : Array.from(String(value || '').matchAll(/\d+/g)).map((match) => match[0])
  const seen = new Set<string>()
  const normalized: string[] = []
  rawItems.forEach((item) => {
    const code = normalizeGopayPhonePart(item)
    if (!code || seen.has(code)) return
    seen.add(code)
    normalized.push(code)
  })
  return normalized.length > 0 ? normalized : [DEFAULT_GOPAY_PHONE_COUNTRY_CODE]
}

export function splitGopayPhoneInput(
  phoneCountryCode: unknown,
  phoneNumber: unknown,
  recognizedCountryCodes: unknown,
) {
  const countryCode = normalizeGopayPhonePart(phoneCountryCode) || DEFAULT_GOPAY_PHONE_COUNTRY_CODE
  const number = normalizeGopayPhonePart(phoneNumber)
  const codes = normalizeGopayRecognizedCountryCodes(recognizedCountryCodes)
  const sortedCodes = codes
    .map((code, index) => ({ code, index }))
    .sort((a, b) => b.code.length - a.code.length || a.index - b.index)

  for (const { code } of sortedCodes) {
    if (number.startsWith(code) && number.length > code.length) {
      return {
        phone_country_code: code,
        phone_number: number.slice(code.length),
      }
    }
  }

  return {
    phone_country_code: countryCode,
    phone_number: number,
  }
}
