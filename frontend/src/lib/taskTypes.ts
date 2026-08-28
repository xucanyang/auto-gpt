export type TaskStatus = 'running' | 'success' | 'failed' | 'skipped' | 'stopped' | 'partial' | 'interrupted' | 'pending_activation' | string

export const TASK_SOURCE_LABELS: Record<string, string> = {
  manual: '注册',
  resume_subscription_auth: '补抓Auth',
  batch_resume_subscription_auth: '批量补抓Auth',
  custom_email_recheck: '邮箱测活',
  batch_custom_email_recheck: '批量邮箱测活',
  web_session_login: '仅刷新登录态',
  batch_web_session_login: '批量仅刷新登录态',
  web_session_gcash_link: '执行登录态',
  batch_web_session_gcash_link: '批量执行登录态',
  web_session_gcash_manual: 'GCash手动提链',
  chatgpt_email_change: '邮箱换绑',
  invalid_recheck: '失效测活',
  batch_invalid_recheck: '批量失效测活',
  payment_eligibility_bundle: '一键支付资格检测',
  batch_payment_eligibility_bundle: '批量一键支付资格检测',
  zero_amount_eligibility: '0 元试用资格检测',
  batch_zero_amount_eligibility: '批量 0 元试用资格检测',
  payment_methods: '支付方式检测',
  batch_payment_methods: '批量支付方式检测',
  gcash_payment_method: 'GCash 支付方式检测',
  batch_gcash_payment_method: '批量 GCash 支付方式检测',
  checkout_link_type: '支付链接格式检测',
  batch_checkout_link_type: '批量支付链接格式检测',
  k12_workspace_recapture: '历史 K12 重跑（已退役）',
  batch_k12_workspace_recapture: '历史批量 K12 重跑（已退役）',
  phone_binding_test: '手机号绑定',
  phone_signup: '手机号注册',
  batch_sub2api_upload: 'Sub2API上传',
  batch_oaipay_upload: 'OAIPay上传',
  baxigpt_cdk_submit: 'iDEAL / PIX 批量提交',
  chatgpt_paypal_bind: 'PayPal绑定',
  chatgpt_oaipay_approval: 'OaiPay授权链接',
  batch_payment_link: '批量支付链接生成',
  pix_payment_link_cleanup: '过期 PIX 链接清理',
  upi_payment_link_cleanup: 'UPI 链接清理',
  ideal_payment_link_cleanup: 'iDEAL 链接清理',
  paypal_payment_link_cleanup: 'PayPal 链接清理',
  kakao_pay_payment_link_cleanup: 'Kakao Pay 链接清理',
  hosted_payment_link_cleanup: 'Hosted 链接清理',
  team_payment_link_cleanup: 'Team 链接清理',
  batch_probe_local_status: '批量本地状态同步',
  batch_account_action: '账号操作',
  icloud_hme_recheck_batch: 'HME邮箱复测',
  registration: '注册',
  registration_task: '注册',
  registration_checkout_probe: '注册结算探测',
  browser_auth: '浏览器认证',
  manual_poll: '手动轮询',
  proxy_pool_smoke: '代理池测试',
  proxy_pool_smoke_limited: '代理池测试',
  unit: '测试任务',
}

const TASK_SOURCE_ALIASES: Record<string, string> = {
  batch_payment_links: 'batch_payment_link',
  payment_link_batch: 'batch_payment_link',
  batch_pix_payment_link: 'batch_payment_link',
  batch_ideal_payment_link: 'batch_payment_link',
  hme_recheck_batch: 'icloud_hme_recheck_batch',
  custom_email_recheck_batch: 'batch_custom_email_recheck',
  phone_binding: 'phone_binding_test',
  phone_bind: 'phone_binding_test',
  register: 'manual',
  registration_smoke: 'manual',
}

export const TASK_SOURCE_OPTIONS = Object.entries(TASK_SOURCE_LABELS).map(([value, label]) => ({ value, label }))

export const SPECIAL_OUTCOME_LABELS: Record<string, string> = {
  invite_saved_pending_activation: '历史待激活（已退役）',
  invite_exhausted_stop_phase: '历史邀请耗尽（已退役）',
  success_skip_save: '成功不保存',
  phone_binding_test_no_phone_available: '无可用号码',
}

function normalizeTaskSourceKey(source?: string | null): string {
  return String(source || '')
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, '_')
}

function canonicalTaskSourceKey(source?: string | null): string {
  const normalized = normalizeTaskSourceKey(source)
  return TASK_SOURCE_ALIASES[normalized] || normalized
}

export function taskSourceLabel(source?: string | null): string {
  const raw = String(source || '').trim()
  const canonical = canonicalTaskSourceKey(raw)
  if (!canonical) return '其他任务'
  if (TASK_SOURCE_LABELS[canonical]) return TASK_SOURCE_LABELS[canonical]
  if (/^[\u3400-\u9fff]/.test(raw)) return raw

  // Historical smoke/test task names encode the implementation branch in
  // ``source``.  They are still registration or payment tasks to an operator;
  // never leak the internal English identifier into the task history table.
  if (canonical.includes('proxy_pool')) return '代理池测试'
  if (
    canonical.startsWith('codex_')
    || canonical.startsWith('register')
    || canonical.startsWith('registration')
    || canonical.includes('browser_registration')
  ) return '注册'
  if (canonical.includes('payment_link') && canonical.includes('cleanup')) return '支付链接清理'
  if (canonical.includes('payment_link')) return '支付链接任务'
  if (canonical.startsWith('batch_')) return '批量任务'
  if (canonical.includes('probe')) return '状态探测'
  return '其他任务'
}

export function taskSourceDisplayLabel(
  source?: string | null,
  meta?: Record<string, unknown>,
): string {
  const sourceLabel = taskSourceLabel(source)
  const canonicalSource = canonicalTaskSourceKey(source)
  const payload = recordOf(meta)
  const accountAction = recordOf(payload.account_action)
  const actionLabel = String(payload.action_label || accountAction.action_label || '').trim()
  const scope = String(payload.scope || accountAction.scope || '').trim().toLowerCase()
  if (canonicalSource === 'batch_account_action') {
    return actionLabel ? `账号操作 · ${actionLabel}` : sourceLabel
  }
  if (
    scope === 'single'
    && [
      'batch_probe_local_status',
      'batch_sub2api_upload',
      'batch_oaipay_upload',
      'batch_payment_link',
    ].includes(canonicalSource)
  ) {
    return actionLabel || sourceLabel.replace(/^批量/, '')
  }
  return sourceLabel
}

function positiveNumber(value: unknown): number {
  const n = Number(value || 0)
  return Number.isFinite(n) && n > 0 ? n : 0
}

export function taskObjectSummary(meta: Record<string, unknown> | undefined, fallbackEmail?: string): string {
  const payload = meta && typeof meta === 'object' ? meta : {}
  const emailCount = positiveNumber(payload.email_count)
  if (emailCount > 0) return `${emailCount} 个邮箱`

  const phoneCount = positiveNumber(payload.phone_count)
  if (phoneCount > 0) {
    const eligibleAccounts = positiveNumber(payload.eligible_accounts) || positiveNumber(payload.eligible)
    return eligibleAccounts > 0 ? `${phoneCount} 个号码 · ${eligibleAccounts} 个账号` : `${phoneCount} 个号码`
  }

  const pairCount = positiveNumber(payload.pair_count)
  if (pairCount > 0) return `${pairCount} 组配对`

  const eligible = positiveNumber(payload.eligible)
  if (eligible > 0) return `${eligible} 个账号`

  const requestedCount = positiveNumber(payload.requested_count)
  if (requestedCount > 0) return `${requestedCount} 个邮箱`

  const email = String(payload.email || '').trim()
  if (email) return email

  return String(fallbackEmail || '').trim() || '-'
}

export interface TaskStats {
  success: number
  skipped: number
  failed: number
  interrupted: number
  total: number
  known: boolean
}

export interface TaskStatsInput {
  status?: unknown
  success?: unknown
  skipped?: unknown
  failed?: unknown
  interrupted?: unknown
  total?: unknown
  stats_available?: unknown
  progress?: unknown
  meta_summary?: Record<string, unknown>
  detail?: Record<string, unknown>
  [key: string]: unknown
}

function recordOf(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

const REGISTRATION_TASK_SOURCES = new Set(['manual', 'registration', 'registration_task'])
const HME_MAIL_PROVIDERS = new Set([
  'hme_ready_api',
  'helper_ready_api',
  'icloud_hme',
  'icloud_hme_ready',
  'icloud_hme_helper_ready',
])
const TEMPMAIL_PROVIDERS = new Set(['tempmail_local', 'tempmail_api'])

function taskDomainList(value: unknown): string[] {
  let rawItems: unknown[] = []
  if (Array.isArray(value)) {
    rawItems = value
  } else if (typeof value === 'string') {
    const text = value.trim()
    if (text.startsWith('[')) {
      try {
        const parsed = JSON.parse(text)
        rawItems = Array.isArray(parsed) ? parsed : [text]
      } catch {
        rawItems = text.split(/[\n,;]+/)
      }
    } else {
      rawItems = text.split(/[\n,;]+/)
    }
  }

  const seen = new Set<string>()
  return rawItems.flatMap((item) => {
    const domain = String(item || '').trim().toLowerCase().replace(/^[@.]+/, '')
    if (!domain || seen.has(domain)) return []
    seen.add(domain)
    return [domain]
  })
}

export function registrationMailboxSummary(meta: Record<string, unknown> | undefined): string {
  const payload = recordOf(meta)
  const mailbox = recordOf(payload.registration_mailbox)
  const provider = String(mailbox.provider || payload.mail_provider || '')
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, '_')

  if (HME_MAIL_PROVIDERS.has(provider)) return 'iCloud'
  if (provider && !TEMPMAIL_PROVIDERS.has(provider)) return ''

  const rawDomains = Object.prototype.hasOwnProperty.call(mailbox, 'domains')
    ? mailbox.domains
    : payload.tempmail_fixed_domains
  const domains = taskDomainList(rawDomains)
  const primaryDomain = taskDomainList([
    mailbox.primary_domain || payload.tempmail_primary_domain,
  ])[0] || ''
  if (primaryDomain && !domains.includes(primaryDomain)) domains.push(primaryDomain)

  const configuredCount = positiveNumber(
    mailbox.domain_count ?? payload.tempmail_domain_count,
  )
  if (Math.max(configuredCount, domains.length) > 1) return '多域名'
  return domains[0] || primaryDomain
}

export interface ActiveTaskLabelInput {
  source?: string | null
  progress?: unknown
  meta?: Record<string, unknown>
  email?: unknown
  platform?: unknown
}

export function activeTaskLabel(item: ActiveTaskLabelInput): string {
  const source = canonicalTaskSourceKey(item?.source)
  const sourceLabel = taskSourceDisplayLabel(item?.source, item?.meta)
  const progress = String(item?.progress || '').trim() || '-'

  if (REGISTRATION_TASK_SOURCES.has(source)) {
    const mailbox = registrationMailboxSummary(item?.meta)
    return `${sourceLabel}·${progress}${mailbox ? `-${mailbox}` : ''}`
  }

  const fallback = String(item?.email || item?.platform || '').trim()
  return `${sourceLabel} · ${progress} · ${taskObjectSummary(item?.meta, fallback)}`
}

function countValue(value: unknown): number | null {
  if (value === null || value === undefined || typeof value === 'boolean') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed >= 0 ? Math.floor(parsed) : null
}

function countResultItems(value: unknown): Partial<Record<keyof Omit<TaskStats, 'total' | 'known'>, number>> {
  if (!Array.isArray(value)) return {}
  const result: Partial<Record<keyof Omit<TaskStats, 'total' | 'known'>, number>> = {}
  const add = (key: keyof Omit<TaskStats, 'total' | 'known'>) => {
    result[key] = (result[key] || 0) + 1
  }
  value.forEach((item) => {
    const row = recordOf(item)
    const status = String(row.status || row.result || '').trim().toLowerCase()
    if (row.ok === true || ['success', 'succeeded', 'done', 'completed', 'ok', 'alive', 'bound', 'uploaded', 'paid', 'registered_phone_signup'].includes(status)) {
      add('success')
    } else if (['skipped', 'skip', 'unsubmitted', 'not_started', 'not_tested', 'not_available', 'no_phone_available'].includes(status)) {
      add('skipped')
    } else if (['interrupted', 'remote_interrupted', 'timeout', 'timed_out'].includes(status)) {
      add('interrupted')
    } else if (row.ok === false || status) {
      add('failed')
    }
  })
  return result
}

function logStats(logs: unknown): Partial<Record<keyof Omit<TaskStats, 'total' | 'known'>, number>> & { summary?: [number, number, number, number] } {
  if (!Array.isArray(logs)) return {}
  const result: Partial<Record<keyof Omit<TaskStats, 'total' | 'known'>, number>> = {}
  let summary: [number, number, number, number] | undefined
  const add = (key: keyof Omit<TaskStats, 'total' | 'known'>) => {
    result[key] = (result[key] || 0) + 1
  }
  logs.forEach((raw) => {
    const line = String(raw || '')
    const lower = line.toLowerCase()
    if (lower.includes('[summary]') || line.includes('[汇总]') || line.includes('完成:') || line.includes('失败:') || line.includes('任务已停止')) {
      const match = line.match(/成功\s*[=:：]?\s*(\d+).*?跳过\s*[=:：]?\s*(\d+).*?失败\s*[=:：]?\s*(\d+)(?:.*?中断\s*[=:：]?\s*(\d+))?/)
      if (match) summary = [Number(match[1]), Number(match[2]), Number(match[3]), Number(match[4] || 0)]
    }
    const outcome = line.match(/\boutcome\s*=\s*(SUCCESS|SKIPPED|FAILED|STOPPED|INTERRUPTED|NOT_STARTED)\b/i)?.[1]?.toLowerCase()
    if (outcome === 'success') add('success')
    else if (outcome === 'skipped' || outcome === 'not_started') add('skipped')
    else if (outcome === 'stopped' || outcome === 'interrupted') add('interrupted')
    else if (outcome === 'failed') add('failed')

    if (!lower.includes('[summary]') && !line.includes('[汇总]')) {
      const marker = line.match(/\[(OK|SUCCESS|FAIL|FAILED|ERROR|SKIP|INTERRUPTED)\]/i)?.[1]?.toLowerCase()
      if (marker === 'ok' || marker === 'success') add('success')
      else if (marker === 'skip') add('skipped')
      else if (marker === 'interrupted') add('interrupted')
      else if (marker) add('failed')
    }
  })
  return summary ? { ...result, summary } : result
}

export function deriveTaskStats(input: TaskStatsInput | null | undefined): TaskStats {
  const record = recordOf(input)
  const detail = recordOf(record.detail)
  const meta = recordOf(detail.meta)
  const summaryMeta = recordOf(record.meta_summary)
  const counts: TaskStats = { success: 0, skipped: 0, failed: 0, interrupted: 0, total: 0, known: false }
  const apply = (key: keyof Omit<TaskStats, 'total' | 'known'>, value: unknown, force = false) => {
    const parsed = countValue(value)
    if (parsed === null) return
    if (parsed > 0 || force) counts.known = true
    if (parsed > counts[key]) counts[key] = parsed
  }

  for (const key of ['success', 'skipped', 'failed', 'interrupted'] as const) {
    if (key in record) apply(key, record[key])
    if (key in detail) apply(key, detail[key])
  }
  for (const [key, target] of [
    ['runtime_success', 'success'],
    ['runtime_skipped', 'skipped'],
    ['runtime_errors', 'failed'],
    ['runtime_interrupted', 'interrupted'],
  ] as const) {
    const value = meta[key]
    if (target === 'failed' && Array.isArray(value)) apply(target, value.length, true)
    else apply(target, value, true)
  }

  const idea = Object.keys(recordOf(meta.idea_submit_summary)).length > 0
    ? recordOf(meta.idea_submit_summary)
    : recordOf(detail.idea_submit_summary)
  if (Object.keys(idea).length > 0) {
    apply('success', idea.success ?? idea.paid ?? idea.completed, true)
    apply('skipped', idea.skipped ?? idea.unsubmitted, true)
    apply('failed', (countValue(idea.failed) || 0) + (countValue(idea.timeout) || 0), true)
    apply('interrupted', idea.interrupted, true)
  }

  for (const key of ['runtime_results', 'account_results', 'results'] as const) {
    const resultCounts = countResultItems(meta[key] ?? detail[key])
    for (const stat of ['success', 'skipped', 'failed', 'interrupted'] as const) apply(stat, resultCounts[stat], true)
  }
  if (Array.isArray(meta.registered_accounts)) apply('success', meta.registered_accounts.length, true)
  if (Array.isArray(meta.auth_pending_accounts)) apply('success', meta.auth_pending_accounts.length, true)

  const parsedLogs = logStats(detail.logs ?? record.logs)
  if (parsedLogs.summary) {
    const [success, skipped, failed, interrupted] = parsedLogs.summary
    apply('success', success, true)
    apply('skipped', skipped, true)
    apply('failed', failed, true)
    apply('interrupted', interrupted, true)
  } else {
    for (const stat of ['success', 'skipped', 'failed', 'interrupted'] as const) apply(stat, parsedLogs[stat], true)
  }
  if (Array.isArray(detail.errors) && detail.errors.length > 0) apply('failed', detail.errors.length, true)

  const status = String(record.status || detail.status_snapshot || '').trim().toLowerCase()
  if (counts.success === 0 && counts.skipped === 0 && counts.failed === 0 && counts.interrupted === 0) {
    if (['success', 'succeeded', 'done', 'complete', 'completed'].includes(status)) apply('success', 1, true)
    else if (['failed', 'failure', 'error'].includes(status)) apply('failed', 1, true)
    else if (status === 'skipped') apply('skipped', 1, true)
    else if (status === 'interrupted') apply('interrupted', 1, true)
  }

  let declaredTotal = 0
  for (const source of [detail, meta, summaryMeta, record]) {
    for (const key of ['total', 'requested_count', 'total_requested', 'total_requested_accounts', 'eligible', 'eligible_accounts', 'email_count', 'phone_count', 'pair_count']) {
      const value = countValue(source[key])
      if (value !== null) declaredTotal = Math.max(declaredTotal, value)
    }
  }
  const progress = String(record.progress || detail.progress || '')
  const progressTotal = progress.match(/\b\d+\s*\/\s*(\d+)\b/)?.[1]
  if (progressTotal) declaredTotal = Math.max(declaredTotal, Number(progressTotal))
  counts.total = declaredTotal > 0
    ? declaredTotal
    : counts.success + counts.skipped + counts.failed + counts.interrupted
  const hasErrors = (Array.isArray(detail.errors) && detail.errors.length > 0)
  counts.known = Boolean(counts.known || hasErrors || record.stats_available === true)
  return counts
}

export function statusTagColor(status?: TaskStatus) {
  const normalized = String(status || '').trim().toLowerCase()
  if (['running', 'pending', 'queued', 'starting', 'processing'].includes(normalized)) return 'processing'
  if (['success', 'succeeded', 'done', 'complete', 'completed'].includes(normalized)) return 'success'
  if (normalized === 'pending_activation') return 'default'
  if (normalized === 'skipped') return 'warning'
  if (['stopped', 'cancelled', 'canceled', 'aborted', 'terminated'].includes(normalized)) return 'warning'
  if (normalized === 'partial') return 'warning'
  if (normalized === 'interrupted') return 'warning'
  return 'error'
}

export function statusLabel(status?: TaskStatus) {
  const normalized = String(status || '').trim().toLowerCase()
  if (['running', 'processing', 'in_progress', 'in-progress'].includes(normalized)) return '运行中'
  if (['pending', 'queued', 'created', 'waiting'].includes(normalized)) return '排队中'
  if (['success', 'succeeded', 'done', 'complete', 'completed'].includes(normalized)) return '成功'
  if (normalized === 'pending_activation') return '历史待激活（已退役）'
  if (normalized === 'skipped') return '跳过'
  if (['stopped', 'cancelled', 'canceled', 'aborted', 'terminated'].includes(normalized)) return '已停止'
  if (normalized === 'partial') return '部分失败'
  if (normalized === 'interrupted') return '远端中断'
  if (['failed', 'failure', 'error'].includes(normalized)) return '失败'
  return normalized || '-'
}

export function taskOutcomeLabel(outcome?: string) {
  switch (String(outcome || '').trim()) {
    case 'task_created':
      return '已创建'
    case 'success':
      return '成功'
    case 'success_skip_save':
      return '成功不保存'
    case 'invite_saved_pending_activation':
      return '历史待激活（已退役）'
    case 'activation_success':
      return '历史激活成功（已退役）'
    case 'activation_failed':
      return '历史激活失败（已退役）'
    case 'resume_subscription_auth_success':
      return '补抓成功'
    case 'resume_subscription_auth_failed':
      return '补抓失败'
    case 'resume_subscription_auth_stopped':
      return '补抓停止'
    case 'resume_subscription_auth_skipped':
      return '补抓跳过'
    case 'batch_resume_subscription_auth_success':
      return '批量补抓成功'
    case 'batch_resume_subscription_auth_failed':
      return '批量补抓失败'
    case 'batch_resume_subscription_auth_stopped':
      return '批量补抓停止'
    case 'web_session_login_success':
      return '仅刷新登录态成功'
    case 'web_session_login_failed':
      return '仅刷新登录态失败'
    case 'web_session_login_stopped':
      return '仅刷新登录态停止'
    case 'web_session_login_skipped':
      return '仅刷新登录态跳过'
    case 'batch_web_session_login_success':
      return '批量仅刷新登录态成功'
    case 'batch_web_session_login_failed':
      return '批量仅刷新登录态失败'
    case 'batch_web_session_login_stopped':
      return '批量仅刷新登录态停止'
    case 'batch_web_session_login_exception':
      return '批量仅刷新登录态异常'
    case 'k12_workspace_recapture_success':
      return '历史 K12 重跑成功（已退役）'
    case 'k12_workspace_recapture_failed':
      return '历史 K12 重跑失败（已退役）'
    case 'k12_workspace_recapture_stopped':
      return '历史 K12 重跑停止（已退役）'
    case 'k12_workspace_recapture_skipped':
      return '历史 K12 重跑跳过（已退役）'
    case 'batch_k12_workspace_recapture_success':
      return '历史批量 K12 重跑成功（已退役）'
    case 'batch_k12_workspace_recapture_failed':
      return '历史批量 K12 重跑失败（已退役）'
    case 'batch_k12_workspace_recapture_stopped':
      return '历史批量 K12 重跑停止（已退役）'
    case 'chatgpt_paypal_bind_success':
      return 'PayPal绑定成功'
    case 'chatgpt_paypal_bind_failed':
      return 'PayPal绑定失败'
    case 'chatgpt_paypal_bind_stopped':
      return 'PayPal绑定停止'
    case 'chatgpt_oaipay_approval_success':
      return 'OaiPay链接成功'
    case 'chatgpt_oaipay_approval_failed':
      return 'OaiPay链接失败'
    case 'chatgpt_oaipay_approval_stopped':
      return 'OaiPay链接停止'
    case 'skipped':
      return '跳过'
    case 'failed':
      return '失败'
    case 'invite_exhausted_stop_phase':
      return '历史邀请耗尽（已退役）'
    case 'phone_binding_test_no_phone_available':
      return '无可用号码'
    case 'phone_signup_success':
      return '手机号注册成功'
    case 'phone_signup_failed':
      return '手机号注册失败'
    case 'batch_sub2api_upload_success':
      return 'Sub2API上传成功'
    case 'batch_sub2api_upload_failed':
      return 'Sub2API上传失败'
    case 'batch_sub2api_upload_stopped':
      return 'Sub2API上传停止'
    case 'batch_sub2api_upload_empty':
      return 'Sub2API上传无处理'
    case 'batch_oaipay_upload_success':
      return 'OAIPay上传成功'
    case 'batch_oaipay_upload_failed':
      return 'OAIPay上传失败'
    case 'batch_oaipay_upload_stopped':
      return 'OAIPay上传停止'
    case 'batch_oaipay_upload_empty':
      return 'OAIPay上传无处理'
    default:
      return String(outcome || '').trim() || '-'
  }
}
