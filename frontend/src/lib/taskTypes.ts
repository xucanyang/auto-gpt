export type TaskStatus = 'running' | 'success' | 'failed' | 'skipped' | 'stopped' | 'pending_activation' | string

export const TASK_SOURCE_LABELS: Record<string, string> = {
  manual: '注册',
  resume_subscription_auth: '补抓Auth',
  batch_resume_subscription_auth: '批量补抓Auth',
  custom_email_recheck: '邮箱测活',
  batch_custom_email_recheck: '批量邮箱测活',
  invalid_recheck: '失效测活',
  batch_invalid_recheck: '批量失效测活',
  k12_workspace_recapture: 'K12重跑',
  batch_k12_workspace_recapture: '批量K12重跑',
  phone_binding_test: '手机号绑定',
  phone_signup: '手机号注册',
  batch_sub2api_upload: 'Sub2API上传',
  batch_oaipay_upload: 'OAIPay上传',
  baxigpt_cdk_submit: 'idea批量提交',
  chatgpt_paypal_bind: 'PayPal绑定',
  chatgpt_oaipay_approval: 'OaiPay授权链接',
  batch_payment_link: '批量支付链接',
  gopay_payment: 'GoPay支付',
  idea_oaipay_pipeline: '账号处理流水线',
}

export const TASK_SOURCE_OPTIONS = Object.entries(TASK_SOURCE_LABELS).map(([value, label]) => ({ value, label }))

export const SPECIAL_OUTCOME_LABELS: Record<string, string> = {
  invite_saved_pending_activation: '待激活',
  invite_exhausted_stop_phase: '邀请耗尽',
  success_skip_save: '成功不保存',
  phone_binding_test_no_phone_available: '无可用号码',
}

export function taskSourceLabel(source?: string | null): string {
  const normalized = String(source || '').trim()
  return TASK_SOURCE_LABELS[normalized] || normalized || 'task'
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

export function statusTagColor(status?: TaskStatus) {
  const normalized = String(status || '').trim().toLowerCase()
  if (normalized === 'running') return 'processing'
  if (normalized === 'success') return 'success'
  if (normalized === 'pending_activation') return 'processing'
  if (normalized === 'skipped') return 'warning'
  if (normalized === 'stopped') return 'warning'
  return 'error'
}

export function statusLabel(status?: TaskStatus) {
  const normalized = String(status || '').trim().toLowerCase()
  if (normalized === 'running') return '运行中'
  if (normalized === 'success') return '成功'
  if (normalized === 'pending_activation') return '待激活'
  if (normalized === 'skipped') return '跳过'
  if (normalized === 'stopped') return '已停止'
  if (normalized === 'failed') return '失败'
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
      return '待激活'
    case 'activation_success':
      return '激活成功'
    case 'activation_failed':
      return '激活失败'
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
    case 'k12_workspace_recapture_success':
      return 'K12重跑成功'
    case 'k12_workspace_recapture_failed':
      return 'K12重跑失败'
    case 'k12_workspace_recapture_stopped':
      return 'K12重跑停止'
    case 'k12_workspace_recapture_skipped':
      return 'K12重跑跳过'
    case 'batch_k12_workspace_recapture_success':
      return '批量K12重跑成功'
    case 'batch_k12_workspace_recapture_failed':
      return '批量K12重跑失败'
    case 'batch_k12_workspace_recapture_stopped':
      return '批量K12重跑停止'
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
      return '邀请耗尽'
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
