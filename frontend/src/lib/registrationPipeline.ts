export type RegistrationPipelineStageName =
  | 'registration'
  | 'zero_amount'
  | 'payment_link'
  | 'payment'

export type RegistrationPipelineStage = Record<string, unknown> & {
  state?: string
  reason_code?: string
  message?: string
  updated_at?: string
}

export type RegistrationPipeline = Record<string, unknown> & {
  registration?: RegistrationPipelineStage
  zero_amount?: RegistrationPipelineStage
  payment_link?: RegistrationPipelineStage
  payment?: RegistrationPipelineStage
  requested?: Record<string, boolean>
  active?: boolean
}

type StageMeta = { color: string; label: string }

const STAGE_META: Record<RegistrationPipelineStageName, Record<string, StageMeta>> = {
  registration: {
    succeeded: { color: 'success', label: '注册成功' },
    pending_auth: { color: 'warning', label: '注册成功 · Auth 待补' },
    failed: { color: 'error', label: '注册失败' },
  },
  zero_amount: {
    disabled: { color: 'default', label: '0 元未开启' },
    not_run: { color: 'default', label: '0 元未检测' },
    queued: { color: 'processing', label: '0 元待检测' },
    running: { color: 'processing', label: '0 元检测中' },
    eligible: { color: 'success', label: '0 元有资格' },
    ineligible: { color: 'warning', label: '非 0 元' },
    probe_failed: { color: 'error', label: '0 元检测失败' },
    pending_auth: { color: 'warning', label: '0 元待补 Auth' },
    skipped: { color: 'default', label: '0 元已跳过' },
  },
  payment_link: {
    disabled: { color: 'default', label: '提链未开启' },
    not_run: { color: 'default', label: '提链未执行' },
    waiting_zero_amount: { color: 'default', label: '提链等待 0 元' },
    queued: { color: 'processing', label: '提链待处理' },
    running: { color: 'processing', label: '提链中' },
    succeeded: { color: 'success', label: '提链成功' },
    failed: { color: 'error', label: '提链失败' },
    pending_auth: { color: 'warning', label: '提链待补 Auth' },
    blocked: { color: 'default', label: '提链未执行' },
  },
  payment: {
    disabled: { color: 'default', label: '支付未开启' },
    not_run: { color: 'default', label: '支付未执行' },
    waiting_payment_link: { color: 'default', label: '支付等待提链' },
    blocked: { color: 'default', label: '支付未执行' },
    submitting: { color: 'processing', label: '支付提交中' },
    submitted: { color: 'processing', label: '已交支付队列' },
    payment_pending: { color: 'processing', label: '支付处理中' },
    succeeded: { color: 'success', label: '支付成功' },
    submit_failed: { color: 'error', label: '支付提交失败' },
    failed: { color: 'error', label: '支付失败' },
    unknown: { color: 'warning', label: '支付结果未知' },
  },
}

const ACTIVE_STATES = new Set([
  'queued',
  'running',
  'submitting',
  'submitted',
  'payment_pending',
])

export function normalizeRegistrationPipeline(value: unknown): RegistrationPipeline {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as RegistrationPipeline
    : {}
}

export function registrationPipelineStage(
  pipeline: RegistrationPipeline,
  stage: RegistrationPipelineStageName,
): RegistrationPipelineStage {
  const value = pipeline?.[stage]
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as RegistrationPipelineStage
    : {}
}

export function registrationPipelineStageMeta(
  stage: RegistrationPipelineStageName,
  value: RegistrationPipelineStage,
): StageMeta {
  const state = String(value?.state || '').trim().toLowerCase()
  return STAGE_META[stage][state]
    || { color: 'default', label: stage === 'registration' ? '已入库' : '未执行' }
}

export function registrationPipelineStageTitle(value: RegistrationPipelineStage): string {
  return [value?.message, value?.reason_code, value?.updated_at]
    .map((item) => String(item || '').trim())
    .filter(Boolean)
    .join(' · ')
}

export function registrationPipelineIsActive(value: unknown): boolean {
  const pipeline = normalizeRegistrationPipeline(value)
  if (pipeline.active === true) return true
  return (['registration', 'zero_amount', 'payment_link', 'payment'] as RegistrationPipelineStageName[])
    .some((stage) => ACTIVE_STATES.has(
      String(registrationPipelineStage(pipeline, stage).state || '').trim().toLowerCase(),
    ))
}
