export const PAYMENT_ELIGIBILITY_FAILURE_META = {
  network_error: { label: '网络问题', color: 'orange' },
  checkout_create_failed: { label: '无法创建 Checkout', color: 'volcano' },
  auth_error: { label: '认证问题', color: 'red' },
  proxy_error: { label: '代理问题', color: 'gold' },
  upstream_error: { label: '上游接口问题', color: 'magenta' },
  protocol_error: { label: '返回格式问题', color: 'purple' },
  configuration_error: { label: '配置问题', color: 'geekblue' },
  other_error: { label: '其他问题', color: 'default' },
} as const

export type PaymentEligibilityFailureCategory = keyof typeof PAYMENT_ELIGIBILITY_FAILURE_META

export const PAYMENT_ELIGIBILITY_FAILURE_ORDER = Object.keys(
  PAYMENT_ELIGIBILITY_FAILURE_META,
) as PaymentEligibilityFailureCategory[]

type UnknownRecord = Record<string, unknown>

function asRecord(value: unknown): UnknownRecord {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : {}
}

function includesAny(value: string, markers: string[]) {
  return markers.some((marker) => value.includes(marker))
}

export function paymentEligibilityFailureMeta(
  value: unknown,
  fallbackMessage: unknown = '',
) {
  const source = asRecord(value)
  const explicit = String(source.failure_category || '').trim().toLowerCase()
  if (explicit in PAYMENT_ELIGIBILITY_FAILURE_META) {
    const category = explicit as PaymentEligibilityFailureCategory
    return { category, ...PAYMENT_ELIGIBILITY_FAILURE_META[category] }
  }

  const text = [
    source.message,
    source.error,
    source.reason_code,
    fallbackMessage,
  ].map((item) => String(item || '').trim().toLowerCase()).filter(Boolean).join(' ')

  let category: PaymentEligibilityFailureCategory = 'other_error'
  if (includesAny(text, [
    '账号缺少 access token',
    '账号认证已失效',
    'http 401',
    'unauthorized',
    'token_invalidated',
    'auth_invalidated',
    'authentication',
  ])) {
    category = 'auth_error'
  } else if (includesAny(text, [
    '代理出口',
    '代理解析',
    '代理不可用',
    '代理解析后为空',
    '指定代理',
    '未解析到可用代理',
    '动态代理',
    '代理模式',
    '必须使用与结账国家一致的代理',
    'proxy country',
    'proxy_mode',
  ])) {
    category = 'proxy_error'
  } else if (includesAny(text, [
    '网络失败',
    'timed out',
    'timeout',
    'connection refused',
    'connection reset',
    'connection aborted',
    'connection error',
    'remote disconnected',
    'name resolution',
    'network is unreachable',
    'dns error',
    'ssl error',
    'tls error',
  ])) {
    category = 'network_error'
  } else if (includesAny(text, [
    'checkout 创建 http',
    'detected unusual activity',
    'could not create checkout',
    'checkout creation failed',
  ])) {
    category = 'checkout_create_failed'
  } else if (includesAny(text, [
    'configuration_error',
    '配置错误',
    '配置无效',
    'unsupported eligibility kind',
    'unsupported billing country',
    '不支持的账单国家',
    '结账国家必须',
    '结账国家不受支持',
  ])) {
    category = 'configuration_error'
  } else if (includesAny(text, [
    '返回不是 json',
    '返回格式无效',
    '未返回受支持的 session id',
    'provider 无法识别',
    'checkout_provider 不是',
    'processor_entity 不是',
    'checkout_state 缺失',
    '结账金额',
    '结账货币',
    'oaics total',
    'oaics 货币',
    '无法提取最终金额',
    '币种不一致',
  ])) {
    category = 'protocol_error'
  } else if (includesAny(text, [
    'upstream',
    'promotion 刷新 http',
    'taxes 刷新 http',
    'stripe',
    'http 429',
    'http 500',
    'http 502',
    'http 503',
    'http 504',
  ])) {
    category = 'upstream_error'
  }

  return { category, ...PAYMENT_ELIGIBILITY_FAILURE_META[category] }
}

export function paymentEligibilityFailureBreakdown(
  summaryValue: unknown,
  resultsValue: unknown,
) {
  const explicitSummary = asRecord(summaryValue)
  const explicitCounts = new Map<PaymentEligibilityFailureCategory, number>()
  for (const category of PAYMENT_ELIGIBILITY_FAILURE_ORDER) {
    const count = Number(explicitSummary[category] || 0)
    if (Number.isFinite(count) && count > 0) explicitCounts.set(category, Math.floor(count))
  }

  if (explicitCounts.size === 0 && Array.isArray(resultsValue)) {
    for (const item of resultsValue) {
      const result = asRecord(item)
      const state = String(result.state || '').trim().toLowerCase()
      const status = String(result.status || '').trim().toLowerCase()
      if (state !== 'probe_failed' && status !== 'failed') continue
      const { category } = paymentEligibilityFailureMeta(result)
      explicitCounts.set(category, Number(explicitCounts.get(category) || 0) + 1)
    }
  }

  return PAYMENT_ELIGIBILITY_FAILURE_ORDER
    .filter((category) => Number(explicitCounts.get(category) || 0) > 0)
    .map((category) => ({
      category,
      count: Number(explicitCounts.get(category) || 0),
      ...PAYMENT_ELIGIBILITY_FAILURE_META[category],
    }))
}
