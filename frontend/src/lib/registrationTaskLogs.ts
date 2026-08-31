export const REGISTRATION_LOG_REGIONS = [
  'registration',
  'zero_amount',
  'payment_details',
  'payment_link',
  'payment',
] as const

export type RegistrationLogRegion = typeof REGISTRATION_LOG_REGIONS[number]

export const REGISTRATION_LOG_REGION_LABELS: Record<RegistrationLogRegion, string> = {
  registration: '注册',
  zero_amount: '0元检测',
  payment_details: '支付明细',
  payment_link: '提链',
  payment: '支付',
}

export type RegistrationPaymentEventLike = {
  id?: number
  account_id?: number
  account?: string
  stage?: string
  level?: string
  message?: string
  created_at?: string
}

type RegistrationTaskSnapshotLike = {
  platform?: string
  source?: string
  meta?: Record<string, unknown>
}

const REGISTRATION_TASK_SOURCES = new Set([
  'manual',
  'register',
  'phone_signup',
  'cpa_replenish',
])

const PAYMENT_LINK_EVENT_STAGES = new Set([
  'extracting_link',
  'link_extracted',
  'extract_failed',
  'pending_auth',
])

const PAYMENT_EVENT_LABELS: Record<string, string> = {
  extracting_link: '开始提链',
  link_extracted: '提链成功',
  extract_failed: '提链失败',
  pending_auth: '待补 Auth',
  submitting_payment: '提交支付',
  submit_failed: '支付提交失败',
  queued: '支付已入队',
  payment_submitted: '支付已提交',
  waiting_result: '等待支付',
  payment_authorized: '支付成功',
  payment_failed: '支付失败',
  payment_unknown: '结果未知',
  relogin_started: '支付后登录',
  relogin_succeeded: '登录成功',
  relogin_failed: '登录失败',
  deadline: '跟进超时',
  subscription_confirmed: '权益已确认',
  local_unconfirmed: '权益待确认',
  poll_error: '查询失败',
  account_identity_changed: '账号已变化',
  history_reconciliation: '恢复跟进',
}

const LOG_TIMESTAMP_PATTERN = /^\[(?:(?:\d{4}-\d{2}-\d{2})[ T])?\d{2}:\d{2}:\d{2}\]\s*/
const ZERO_AMOUNT_PREFIX_PATTERN = /\[(?:注册后\s*)?0\s*元(?:试用)?(?:资格|检测)?\]/i
const PAYMENT_DETAILS_PREFIX_PATTERN = /\[链接格式\s*\+\s*支付方式\]/i
const PAYPAL_LOG_PREFIX_PATTERN = /\[(?:PayPal\s*(?:提链|注册链路|自动支付|跟进)|支付后登录)\]/i
const PAYMENT_STRONG_PATTERN = /(?:开始提交(?:\s*PayPal)?\s*支付队列|(?:已|未)提交(?:\s*PayPal)?\s*支付队列|支付条目|等待支付结果|支付结果|支付已确认|支付后登录|重新登录|本地状态刷新|付费权益|支付入队失败|支付提交失败|结果=已交支付队列|结果=支付入队失败|payment_(?:enqueued|submitted|pending|authorized|failed|unknown)|submitting_payment|submit_failed)/i

function recordOf(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function normalizedLogBody(rawLine: unknown): string {
  return String(rawLine || '')
    .replace(LOG_TIMESTAMP_PATTERN, '')
    .replace(/^\[DEBUG\]\s*/i, '')
    .trim()
}

export function registrationLogRegionForLine(rawLine: unknown): RegistrationLogRegion {
  const body = normalizedLogBody(rawLine)
  if (ZERO_AMOUNT_PREFIX_PATTERN.test(body)) return 'zero_amount'
  if (PAYMENT_DETAILS_PREFIX_PATTERN.test(body)) return 'payment_details'
  if (/^\[支付后登录\]/i.test(body)) return 'payment'
  if (!PAYPAL_LOG_PREFIX_PATTERN.test(body)) return 'registration'
  if (/^\[PayPal\s*注册链路\]\s*汇总/i.test(body)) return 'payment_link'
  if (PAYMENT_STRONG_PATTERN.test(body)) return 'payment'
  return 'payment_link'
}

export function registrationPaymentEventRegion(
  event: RegistrationPaymentEventLike,
): Extract<RegistrationLogRegion, 'payment_link' | 'payment'> {
  const stage = String(event?.stage || '').trim().toLowerCase()
  const message = String(event?.message || '').trim()
  if (
    PAYMENT_LINK_EVENT_STAGES.has(stage)
    || stage.includes('extract')
    || stage.includes('payment_link')
    || (/提链|approval url.*(?:提取|保存)/i.test(message) && !PAYMENT_STRONG_PATTERN.test(message))
  ) {
    return 'payment_link'
  }
  return 'payment'
}

function formatEventTimestamp(value: unknown): string {
  const text = String(value || '').trim()
  const match = text.match(/(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})/)
  if (match) return `${match[1]} ${match[2]}`
  const clock = text.match(/\d{2}:\d{2}:\d{2}/)?.[0]
  return clock || '--:--:--'
}

function paymentEventSignature(event: RegistrationPaymentEventLike): string {
  const account = String(event?.account || '').trim().toLowerCase()
  const message = String(event?.message || '').trim()
  if (!message) return ''
  return `${account}\u001f${message}`
}

function rawPaypalEventSignature(rawLine: unknown): string {
  const body = normalizedLogBody(rawLine)
  const match = body.match(/^\[PayPal\s*跟进\]\[账号=([^\]]+)\]\s*(.+)$/i)
  if (!match) return ''
  return `${String(match[1] || '').trim().toLowerCase()}\u001f${String(match[2] || '').trim()}`
}

export function formatRegistrationPaymentEvent(event: RegistrationPaymentEventLike): string {
  const stage = String(event?.stage || '').trim().toLowerCase()
  const region = registrationPaymentEventRegion(event)
  const regionLabel = region === 'payment_link' ? '提链' : '支付'
  const stageLabel = PAYMENT_EVENT_LABELS[stage] || stage || '状态更新'
  const account = String(event?.account || '').trim()
  const accountTag = account ? `[账号=${account}]` : ''
  const message = String(event?.message || '').trim() || '状态已更新'
  return `[${formatEventTimestamp(event?.created_at)}] [${regionLabel}][${stageLabel}]${accountTag} ${message}`
}

export function partitionRegistrationTaskLogs(
  rawLines: unknown,
  paymentEvents: unknown,
): Record<RegistrationLogRegion, string[]> {
  const regions: Record<RegistrationLogRegion, string[]> = {
    registration: [],
    zero_amount: [],
    payment_details: [],
    payment_link: [],
    payment: [],
  }
  const rawSignatureCounts = new Map<string, number>()

  if (Array.isArray(rawLines)) {
    rawLines.forEach((value) => {
      const line = String(value || '')
      const region = registrationLogRegionForLine(line)
      regions[region].push(line)
      const signature = rawPaypalEventSignature(line)
      if (signature) {
        rawSignatureCounts.set(signature, (rawSignatureCounts.get(signature) || 0) + 1)
      }
    })
  }

  if (Array.isArray(paymentEvents)) {
    paymentEvents.forEach((value) => {
      const event = recordOf(value) as RegistrationPaymentEventLike
      const signature = paymentEventSignature(event)
      const duplicateCount = signature ? Number(rawSignatureCounts.get(signature) || 0) : 0
      if (duplicateCount > 0) {
        rawSignatureCounts.set(signature, duplicateCount - 1)
        return
      }
      regions[registrationPaymentEventRegion(event)].push(
        formatRegistrationPaymentEvent(event),
      )
    })
  }

  return regions
}

export function isRegistrationTaskSnapshot(
  snapshot: RegistrationTaskSnapshotLike | null | undefined,
  rawLines: unknown = [],
): boolean {
  const source = String(snapshot?.source || snapshot?.meta?.source || '').trim().toLowerCase()
  if (REGISTRATION_TASK_SOURCES.has(source)) return true
  if (source.startsWith('codex_') && source.includes('registration')) return true

  const meta = recordOf(snapshot?.meta)
  if (
    Object.prototype.hasOwnProperty.call(meta, 'registration_browser')
    || Object.prototype.hasOwnProperty.call(meta, 'registration_mailbox')
    || Object.prototype.hasOwnProperty.call(meta, 'registration_pipeline_request')
    || Object.prototype.hasOwnProperty.call(meta, 'registration_domain_task_group')
    || recordOf(meta.phone_signup).enabled === true
  ) {
    return true
  }

  return Array.isArray(rawLines) && rawLines.some((line) => (
    /\[\d+\/\d+\]\[步骤\d+\/\d+\s+[^\]]+\]/.test(String(line || ''))
  ))
}
