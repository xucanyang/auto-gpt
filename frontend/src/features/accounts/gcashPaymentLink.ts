import { parseProjectDateTime } from '@/lib/dateTime'

export type GcashPaymentLinkState =
  | 'unknown'
  | 'queued'
  | 'submitting'
  | 'running'
  | 'active'
  | 'expired'
  | 'failed'
  | string

export type GcashPaymentLinkSummary = {
  state: GcashPaymentLinkState
  url: string
  generatedAt: unknown
  gcashQrExpiresAt: unknown
  linkExpiresAt: unknown
  effectiveExpiresAt: unknown
  browserTabState: string
  error: string
}

export type GcashRemainingView = {
  state: 'unknown' | 'active' | 'warning' | 'expired'
  label: string
  remainingSeconds: number | null
  expiresAtMs: number | null
}

const GCASH_RUNNING_STATES = new Set([
  'queued',
  'pending',
  'authenticating',
  'auth_ready',
  'submitting',
  'gcash_submitting',
  'running',
  'gcash_running',
])

const GCASH_FAILED_STATES = new Set([
  'failed',
  'error',
  'probe_failed',
  'gcash_failed',
  'link_failed',
])

const ADYEN_GCASH_REDIRECT_HOST = 'checkoutshopper-live.adyen.com'
const ADYEN_GCASH_REDIRECT_PATH = '/checkoutshopper/checkoutPaymentRedirect'

function recordOf(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function firstDefined(...values: unknown[]): unknown {
  return values.find((value) => value !== undefined && value !== null && value !== '')
}

export function safeGcashPaymentLinkUrl(value: unknown): string {
  const text = String(value || '').trim()
  if (!text || text.length > 8192) return ''
  try {
    const parsed = new URL(text)
    return parsed.protocol === 'https:'
      && parsed.hostname.toLowerCase() === ADYEN_GCASH_REDIRECT_HOST
      && !parsed.username
      && !parsed.password
      && (!parsed.port || parsed.port === '443')
      && parsed.pathname === ADYEN_GCASH_REDIRECT_PATH
      && !parsed.hash
      && Boolean(parsed.searchParams.get('redirectData')?.trim())
      ? text
      : ''
  } catch {
    return ''
  }
}

export function gcashPaymentLinkFromAccount(account: unknown): GcashPaymentLinkSummary {
  const record = recordOf(account)
  const extra = recordOf(record.extra)
  const raw = recordOf(
    record.gcash_payment_link
    || record.gcashPaymentLink
    || extra.chatgpt_gcash_payment_link,
  )
  const result = recordOf(raw.result)
  const url = safeGcashPaymentLinkUrl(firstDefined(
    raw.url,
    raw.provider_redirect_url,
    result.url,
    result.provider_redirect_url,
  ))
  let state = String(firstDefined(raw.state, raw.status, raw.link_status) || '').trim().toLowerCase()
  if (!state) state = url ? 'active' : 'unknown'

  return {
    state,
    url,
    generatedAt: firstDefined(raw.generated_at, raw.generatedAt, raw.created_at, result.generated_at),
    gcashQrExpiresAt: firstDefined(
      raw.gcash_qr_expires_at,
      raw.gcashQrExpiresAt,
      raw.qr_expires_at,
      result.gcash_qr_expires_at,
      result.qr_expires_at,
    ),
    linkExpiresAt: firstDefined(raw.link_expires_at, raw.linkExpiresAt, result.link_expires_at),
    effectiveExpiresAt: firstDefined(raw.effective_expires_at, raw.effectiveExpiresAt, result.effective_expires_at),
    browserTabState: String(firstDefined(
      raw.browser_tab_state,
      raw.browserTabState,
      raw.gcash_tab_state,
      result.browser_tab_state,
      result.gcash_tab_state,
    ) || '').trim().toLowerCase(),
    error: String(firstDefined(
      raw.error,
      raw.browser_tab_error,
      raw.message,
      result.error,
      result.browser_tab_error,
    ) || '').trim(),
  }
}

export function gcashExpiryMs(value: unknown): number | null {
  if (typeof value === 'boolean' || value === null || value === undefined || value === '') return null
  const date = parseProjectDateTime(value)
  const timestamp = date?.getTime()
  return timestamp && Number.isFinite(timestamp) && timestamp > 0 ? timestamp : null
}

export function effectiveGcashPaymentLinkExpiryMs(
  value: Pick<GcashPaymentLinkSummary, 'gcashQrExpiresAt' | 'linkExpiresAt' | 'effectiveExpiresAt'>,
): number | null {
  const explicitCandidates = [
    gcashExpiryMs(value.gcashQrExpiresAt),
    gcashExpiryMs(value.linkExpiresAt),
  ].filter((item): item is number => item !== null)
  if (explicitCandidates.length > 0) return Math.min(...explicitCandidates)
  return gcashExpiryMs(value.effectiveExpiresAt)
}

export function formatGcashRemainingSeconds(totalSeconds: number): string {
  const seconds = Math.max(0, Math.ceil(Number(totalSeconds) || 0))
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const remainder = seconds % 60
  if (days > 0) return `${days}\u5929 ${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}`
  if (hours > 0) {
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`
  }
  return `${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`
}

export function gcashRemainingView(
  value: Pick<GcashPaymentLinkSummary, 'gcashQrExpiresAt' | 'linkExpiresAt' | 'effectiveExpiresAt'>,
  nowMs: number,
  warningSeconds = 120,
): GcashRemainingView {
  const expiresAtMs = effectiveGcashPaymentLinkExpiryMs(value)
  if (expiresAtMs === null) {
    return { state: 'unknown', label: '-', remainingSeconds: null, expiresAtMs: null }
  }
  const remainingSeconds = Math.ceil((expiresAtMs - nowMs) / 1000)
  if (remainingSeconds <= 0) {
    return { state: 'expired', label: '\u5df2\u8fc7\u671f', remainingSeconds: 0, expiresAtMs }
  }
  return {
    state: remainingSeconds <= warningSeconds ? 'warning' : 'active',
    label: formatGcashRemainingSeconds(remainingSeconds),
    remainingSeconds,
    expiresAtMs,
  }
}

export function gcashPaymentLinkIsRunning(value: Pick<GcashPaymentLinkSummary, 'state'>): boolean {
  return GCASH_RUNNING_STATES.has(String(value.state || '').trim().toLowerCase())
}

export function gcashPaymentLinkIsFailed(value: Pick<GcashPaymentLinkSummary, 'state'>): boolean {
  return GCASH_FAILED_STATES.has(String(value.state || '').trim().toLowerCase())
}
