import { normalizeDomainList } from './domainList.ts'

export const REGISTRATION_DOMAIN_TASK_MODE_FIELD = 'registration_domain_task_mode'
export const REGISTRATION_DOMAIN_TASK_MODE_COMBINED = 'combined'
export const REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN = 'per_domain'
export const REGISTRATION_DOMAIN_TASK_MODE_ROTATING = 'rotating'
export const REGISTRATION_DOMAIN_ACTIVE_SLOTS_FIELD = 'registration_domain_active_slots'
export const REGISTRATION_DOMAIN_REJECTION_THRESHOLD_FIELD = 'registration_domain_rejection_rate_threshold_percent'
export const REGISTRATION_DOMAIN_REJECTION_MIN_SAMPLES_FIELD = 'registration_domain_rejection_rate_min_samples'
export const REGISTRATION_DOMAIN_NO_LINK_STREAK_FIELD = 'registration_domain_no_link_streak_threshold'

export type RegistrationDomainTaskMode =
  | typeof REGISTRATION_DOMAIN_TASK_MODE_COMBINED
  | typeof REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN
  | typeof REGISTRATION_DOMAIN_TASK_MODE_ROTATING

export type RegistrationDomainTaskItem = {
  taskId: string
  domain: string
  position: number
  state: string
  attempt: number
  isCurrent: boolean
  error: string
  quality: Record<string, unknown>
  trigger: Record<string, unknown>
}

export type RegistrationDomainTaskError = {
  domain: string
  position: number
  state: string
  message: string
  retryCount: number
  retryLimit: number
}

export type RegistrationDomainTaskDomain = {
  domain: string
  position: number
  state: string
  taskId: string
  error: string
  attemptCount: number
  retryCount: number
  retryLimit: number
  nextRetryAt: string
  technicalFailure: Record<string, unknown>
  quality: Record<string, unknown>
  trigger: Record<string, unknown>
}

export type RegistrationDomainTaskGroup = {
  groupId: string
  mode:
    | typeof REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN
    | typeof REGISTRATION_DOMAIN_TASK_MODE_ROTATING
  state: string
  requestedDomainCount: number
  taskAttemptCount: number
  requestedCountPerTask: number
  requestedConcurrencyPerTask: number
  activeDomainSlots: number
  policy: Record<string, unknown>
  counts: Record<string, number>
  domains: RegistrationDomainTaskDomain[]
  failure: Record<string, unknown>
  technicalFailures: Record<string, unknown>[]
  stopReason: string
  tasks: RegistrationDomainTaskItem[]
  errors: RegistrationDomainTaskError[]
}

type RegistrationTaskRequestPayload = Record<string, unknown> & {
  extra?: Record<string, unknown>
}

type RegistrationTaskApiFetch = (
  path: string,
  options?: RequestInit,
) => Promise<unknown>

function positiveInteger(value: unknown, fallback = 0) {
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : fallback
}

function recordOf(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

export function normalizeRegistrationDomainTaskMode(value: unknown): RegistrationDomainTaskMode {
  const normalized = String(value || '').trim().toLowerCase()
  if (normalized === REGISTRATION_DOMAIN_TASK_MODE_ROTATING) {
    return REGISTRATION_DOMAIN_TASK_MODE_ROTATING
  }
  if (normalized === REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN) {
    return REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN
  }
  return REGISTRATION_DOMAIN_TASK_MODE_COMBINED
}

export function registrationTaskCreateEndpoint(mode: unknown) {
  return normalizeRegistrationDomainTaskMode(mode) !== REGISTRATION_DOMAIN_TASK_MODE_COMBINED
    ? '/tasks/register/by-domain'
    : '/tasks/register'
}

function requestErrorStatus(error: unknown) {
  return positiveInteger(recordOf(error).status)
}

function requestErrorMessage(error: unknown) {
  if (error instanceof Error && error.message.trim()) return error.message.trim()
  return String(recordOf(error).message || '注册任务创建失败').trim()
}

function canonicalDomainTaskRequest(request: RegistrationTaskRequestPayload) {
  const extra = recordOf(request.extra)
  let domains = normalizeDomainList(extra.tempmail_fixed_domains)
  if (domains.length === 0) {
    domains = normalizeDomainList([extra.tempmail_primary_domain]).slice(0, 1)
  }
  if (domains.length === 0) {
    throw new Error('请在优选域名中勾选至少一个本次使用的可用域名')
  }
  return {
    domains,
    request: {
      ...request,
      extra: {
        ...extra,
        tempmail_primary_domain: domains[0],
        tempmail_fixed_domains: domains,
      },
    },
  }
}

export async function createRegistrationTasks(
  apiFetch: RegistrationTaskApiFetch,
  request: RegistrationTaskRequestPayload,
  mode: unknown,
): Promise<Record<string, unknown>> {
  const normalizedMode = normalizeRegistrationDomainTaskMode(mode)
  if (normalizedMode === REGISTRATION_DOMAIN_TASK_MODE_COMBINED) {
    return recordOf(await apiFetch('/tasks/register', {
      method: 'POST',
      body: JSON.stringify(request),
    }))
  }
  if (normalizedMode === REGISTRATION_DOMAIN_TASK_MODE_ROTATING) {
    if (
      request.registration_zero_amount_eligibility_enabled !== true
      || request.registration_paypal_link_enabled !== true
    ) {
      throw new Error('按域名自动轮换要求同时开启注册后 0 元检测和提链')
    }
  }

  const canonical = canonicalDomainTaskRequest(request)
  if (normalizedMode === REGISTRATION_DOMAIN_TASK_MODE_ROTATING) {
    return recordOf(await apiFetch('/tasks/register/by-domain', {
      method: 'POST',
      body: JSON.stringify({
        ...canonical.request,
        registration_domain_task_mode: REGISTRATION_DOMAIN_TASK_MODE_ROTATING,
      }),
    }))
  }

  try {
    return recordOf(await apiFetch('/tasks/register/by-domain', {
      method: 'POST',
      body: JSON.stringify(canonical.request),
    }))
  } catch (error: unknown) {
    if (![404, 405].includes(requestErrorStatus(error))) throw error
  }

  // Rolling deployments may briefly serve the new frontend from an instance
  // whose running backend predates the group endpoint. Preserve the feature by
  // expanding the same frozen request through the existing manual-task API.
  const extra = recordOf(canonical.request.extra)
  const domains = canonical.domains

  const settled = await Promise.allSettled(domains.map(async (domain, index) => {
    const childRequest = {
      ...canonical.request,
      extra: {
        ...extra,
        tempmail_mode: 'fixed_domain',
        tempmail_primary_domain: domain,
        tempmail_fixed_domains: [domain],
      },
    }
    const response = recordOf(await apiFetch('/tasks/register', {
      method: 'POST',
      body: JSON.stringify(childRequest),
    }))
    const taskId = String(response.task_id || '').trim()
    if (!taskId) throw new Error('创建任务成功，但未返回 task_id')
    return { task_id: taskId, domain, position: index + 1 }
  }))

  const tasks: Array<Record<string, unknown>> = []
  const errors: Array<Record<string, unknown>> = []
  settled.forEach((result, index) => {
    if (result.status === 'fulfilled') {
      tasks.push(result.value)
      return
    }
    errors.push({
      domain: domains[index],
      position: index + 1,
      message: requestErrorMessage(result.reason),
    })
  })
  if (tasks.length === 0) {
    throw new Error(`所有按域名注册任务均创建失败：${String(errors[0]?.message || '未知错误')}`)
  }

  const randomPart = globalThis.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(16).slice(2, 10)}`
  return {
    task_id: tasks[0].task_id,
    task_group_id: `register_client_group_${randomPart}`,
    mode: REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN,
    requested_domain_count: domains.length,
    created_count: tasks.length,
    failed_count: errors.length,
    requested_count_per_task: positiveInteger(request.count, 1),
    requested_concurrency_per_task: positiveInteger(request.concurrency, 1),
    tasks,
    errors,
    compatibility_transport: 'single_task_api',
  }
}

export function normalizeRegistrationDomainTaskGroup(value: unknown): RegistrationDomainTaskGroup | null {
  const payload = recordOf(value)
  const rawTasks = Array.isArray(payload.tasks) ? payload.tasks : []
  const seenTaskIds = new Set<string>()
  const tasks = rawTasks.flatMap((raw, index) => {
    const item = recordOf(raw)
    const taskId = String(item.task_id || item.taskId || '').trim()
    const domain = normalizeDomainList([item.domain])[0] || ''
    if (!taskId || !domain || seenTaskIds.has(taskId)) return []
    seenTaskIds.add(taskId)
    return [{
      taskId,
      domain,
      position: positiveInteger(item.position, index + 1),
      state: String(item.state || 'active').trim().toLowerCase() || 'active',
      attempt: positiveInteger(item.attempt, 1),
      isCurrent: item.is_current === undefined && item.isCurrent === undefined
        ? true
        : Boolean(item.is_current ?? item.isCurrent),
      error: String(item.error || '').trim(),
      quality: recordOf(item.quality),
      trigger: recordOf(item.trigger),
    }]
  })
  if (tasks.length === 0) return null

  const rawErrors = Array.isArray(payload.errors) ? payload.errors : []
  const errors = rawErrors.flatMap((raw, index) => {
    const item = recordOf(raw)
    const domain = normalizeDomainList([item.domain])[0] || ''
    const message = String(item.message || '').trim()
    if (!domain || !message) return []
    return [{
      domain,
      position: positiveInteger(item.position, tasks.length + index + 1),
      state: String(item.state || 'failed').trim().toLowerCase() || 'failed',
      message,
      retryCount: positiveInteger(item.retry_count ?? item.retryCount),
      retryLimit: positiveInteger(item.retry_limit ?? item.retryLimit),
    }]
  })

  const requestedDomainCount = positiveInteger(
    payload.requested_domain_count ?? payload.requestedDomainCount,
    tasks.length + errors.length,
  )
  const mode = normalizeRegistrationDomainTaskMode(payload.mode)
  const normalizedGroupMode = mode === REGISTRATION_DOMAIN_TASK_MODE_ROTATING
    ? REGISTRATION_DOMAIN_TASK_MODE_ROTATING
    : REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN
  const rawDomains = Array.isArray(payload.domains) ? payload.domains : []
  const domains = rawDomains.flatMap((raw, index) => {
    const item = recordOf(raw)
    const domain = normalizeDomainList([item.domain])[0] || ''
    if (!domain) return []
    return [{
      domain,
      position: positiveInteger(item.position, index + 1),
      state: String(item.state || 'pending').trim().toLowerCase() || 'pending',
      taskId: String(item.task_id || item.taskId || '').trim(),
      error: String(item.error || '').trim(),
      attemptCount: positiveInteger(item.attempt_count ?? item.attemptCount),
      retryCount: positiveInteger(item.retry_count ?? item.retryCount),
      retryLimit: positiveInteger(item.retry_limit ?? item.retryLimit),
      nextRetryAt: String(item.next_retry_at || item.nextRetryAt || '').trim(),
      technicalFailure: recordOf(item.technical_failure ?? item.technicalFailure),
      quality: recordOf(item.quality),
      trigger: recordOf(item.trigger),
    }]
  })
  const rawCounts = recordOf(payload.counts)
  const counts = Object.fromEntries(
    Object.entries(rawCounts).map(([key, value]) => [key, positiveInteger(value)]),
  )
  const observedDomains = new Set([
    ...tasks.map((item) => item.domain),
    ...errors.map((item) => item.domain),
    ...domains.map((item) => item.domain),
  ]).size
  const rawTechnicalFailures = Array.isArray(payload.technical_failures)
    ? payload.technical_failures
    : Array.isArray(payload.technicalFailures)
      ? payload.technicalFailures
      : []
  return {
    groupId: String(payload.task_group_id || payload.groupId || '').trim()
      || `registration-domain-group-${tasks[0].taskId}`,
    mode: normalizedGroupMode,
    state: String(payload.state || 'running').trim().toLowerCase() || 'running',
    requestedDomainCount: Math.max(requestedDomainCount, observedDomains),
    taskAttemptCount: positiveInteger(
      payload.task_attempt_count ?? payload.taskAttemptCount,
      tasks.length,
    ),
    requestedCountPerTask: positiveInteger(
      payload.requested_count_per_task ?? payload.requestedCountPerTask,
      1,
    ),
    requestedConcurrencyPerTask: positiveInteger(
      payload.requested_concurrency_per_task ?? payload.requestedConcurrencyPerTask,
      1,
    ),
    activeDomainSlots: positiveInteger(
      payload.active_domain_slots ?? payload.activeDomainSlots,
      normalizedGroupMode === REGISTRATION_DOMAIN_TASK_MODE_ROTATING ? 1 : tasks.length,
    ),
    policy: recordOf(payload.policy),
    counts,
    domains,
    failure: recordOf(payload.failure),
    technicalFailures: rawTechnicalFailures.map(recordOf),
    stopReason: String(payload.stop_reason || payload.stopReason || '').trim(),
    tasks,
    errors,
  }
}

export async function fetchRegistrationDomainTaskGroup(
  apiFetch: RegistrationTaskApiFetch,
  groupId: string,
) {
  const normalizedGroupId = String(groupId || '').trim()
  if (!normalizedGroupId) return null
  const response = await apiFetch(`/tasks/register/domain-groups/${encodeURIComponent(normalizedGroupId)}`)
  return normalizeRegistrationDomainTaskGroup(response)
}

export function isRegistrationDomainTaskGroupActive(group: RegistrationDomainTaskGroup | null) {
  return group?.mode === REGISTRATION_DOMAIN_TASK_MODE_ROTATING
    && ['running', 'stopping', 'failing'].includes(group.state)
}

export function registrationDomainTaskTotalTarget(
  domainCount: unknown,
  requestedCountPerTask: unknown,
) {
  return positiveInteger(domainCount) * positiveInteger(requestedCountPerTask)
}
