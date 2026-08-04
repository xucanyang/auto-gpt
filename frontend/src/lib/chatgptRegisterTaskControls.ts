export const CHATGPT_REGISTER_DEFAULT_CONCURRENCY = 2
export const CHATGPT_REGISTER_PROTOCOL_MAX_CONCURRENCY = 3
export const CHATGPT_REGISTER_BROWSER_MAX_CONCURRENCY = 2
export const CHATGPT_REGISTER_DEFAULT_DELAY_SECONDS = 15
export const CHATGPT_REGISTER_DEFAULT_DELAY_MAX_SECONDS = 30
const LEGACY_REGISTER_DEFAULT_CONCURRENCY = 1
const LEGACY_REGISTER_MAX_CONCURRENCY = 5

export type ChatGPTRegisterUniqueExitPolicy = 'auto' | 'required' | 'off'
export type ChatGPTRegisterControlConfig = Record<string, unknown>

function hasStoredValue(value: unknown) {
  return value !== undefined && value !== null && value !== ''
}

function nonNegativeNumber(value: unknown, fallback: number) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.max(0, parsed) : fallback
}

function boundedInteger(value: unknown, fallback: number, maximum: number) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed) || !Number.isInteger(parsed)) return fallback
  return Math.max(1, Math.min(maximum, parsed))
}

function isChatGPTPlatform(platform: unknown) {
  return String(platform || '').trim().toLowerCase() === 'chatgpt'
}

function isProtocolExecutor(executor: unknown) {
  return String(executor || '').trim().toLowerCase() === 'protocol'
}

export function getRegisterConcurrencyLimit(
  platform: unknown,
  executor: unknown,
  config: ChatGPTRegisterControlConfig = {},
) {
  if (!isChatGPTPlatform(platform)) return LEGACY_REGISTER_MAX_CONCURRENCY
  if (isProtocolExecutor(executor)) {
    return boundedInteger(
      config.chatgpt_register_protocol_max_concurrency,
      CHATGPT_REGISTER_PROTOCOL_MAX_CONCURRENCY,
      CHATGPT_REGISTER_PROTOCOL_MAX_CONCURRENCY,
    )
  }
  return boundedInteger(
    config.chatgpt_register_browser_max_concurrency,
    CHATGPT_REGISTER_BROWSER_MAX_CONCURRENCY,
    CHATGPT_REGISTER_BROWSER_MAX_CONCURRENCY,
  )
}

export function getRegisterDefaultConcurrency(
  platform: unknown,
  executor: unknown,
  config: ChatGPTRegisterControlConfig = {},
) {
  if (!isChatGPTPlatform(platform)) return LEGACY_REGISTER_DEFAULT_CONCURRENCY
  const limit = getRegisterConcurrencyLimit(platform, executor, config)
  const key = isProtocolExecutor(executor)
    ? 'chatgpt_register_protocol_default_concurrency'
    : 'chatgpt_register_browser_default_concurrency'
  return boundedInteger(config[key], CHATGPT_REGISTER_DEFAULT_CONCURRENCY, limit)
}

export function normalizeRegisterConcurrency(
  value: unknown,
  platform: unknown,
  executor: unknown,
  forceSerial = false,
  config: ChatGPTRegisterControlConfig = {},
) {
  if (forceSerial) return 1
  const parsed = Number(value)
  const requested = Number.isFinite(parsed)
    ? Math.max(1, Math.floor(parsed))
    : getRegisterDefaultConcurrency(platform, executor, config)
  return Math.min(requested, getRegisterConcurrencyLimit(platform, executor, config))
}

export function normalizeRegisterDelaySettings(values: {
  register_delay_seconds?: unknown
  register_delay_max_seconds?: unknown
} = {}, platform: unknown = 'chatgpt', config: ChatGPTRegisterControlConfig = {}) {
  const chatgpt = isChatGPTPlatform(platform)
  let configuredMinimum = chatgpt
    ? nonNegativeNumber(config.chatgpt_register_delay_seconds, CHATGPT_REGISTER_DEFAULT_DELAY_SECONDS)
    : 0
  const configuredMaximumRaw = chatgpt
    ? nonNegativeNumber(config.chatgpt_register_delay_max_seconds, CHATGPT_REGISTER_DEFAULT_DELAY_MAX_SECONDS)
    : 0
  let configuredMaximum = configuredMaximumRaw
  if (configuredMaximum === 0 && configuredMinimum > 0) {
    configuredMaximum = configuredMinimum
  } else if (configuredMaximum < configuredMinimum) {
    configuredMinimum = CHATGPT_REGISTER_DEFAULT_DELAY_SECONDS
    configuredMaximum = CHATGPT_REGISTER_DEFAULT_DELAY_MAX_SECONDS
  }
  const hasMinimum = hasStoredValue(values.register_delay_seconds)
  const hasMaximum = hasStoredValue(values.register_delay_max_seconds)

  if (!hasMinimum && !hasMaximum) {
    return {
      register_delay_seconds: configuredMinimum,
      register_delay_max_seconds: configuredMaximum,
    }
  }

  // A lone minimum is the legacy fixed-delay contract. Explicit max=0 is also
  // retained because the backend canonicalizes it to the same fixed delay.
  const minimum = hasMinimum
    ? nonNegativeNumber(values.register_delay_seconds, configuredMinimum)
    : 0
  return {
    register_delay_seconds: minimum,
    register_delay_max_seconds: hasMaximum
      ? nonNegativeNumber(values.register_delay_max_seconds, configuredMaximum)
      : chatgpt
        ? minimum
        : 0,
  }
}

function legacyBoolean(value: unknown) {
  if (typeof value === 'boolean') return value
  return ['1', 'true', 'yes', 'on'].includes(String(value || '').trim().toLowerCase())
}

export function normalizeRegisterUniqueExitPolicy(
  policy: unknown,
  legacyEnabled?: unknown,
  fallback: ChatGPTRegisterUniqueExitPolicy = 'auto',
): ChatGPTRegisterUniqueExitPolicy {
  const normalized = String(policy || '').trim().toLowerCase()
  if (normalized === 'auto' || normalized === 'required' || normalized === 'off') {
    return normalized
  }
  if (hasStoredValue(legacyEnabled)) {
    return legacyBoolean(legacyEnabled) ? 'required' : 'off'
  }
  return fallback
}

export function isRegisterUniqueExitEnabled(
  policy: unknown,
  proxyMode: unknown,
) {
  const normalized = normalizeRegisterUniqueExitPolicy(policy)
  return normalized === 'required'
    || (normalized === 'auto' && String(proxyMode || '').trim().toLowerCase() === 'dynamic')
}
