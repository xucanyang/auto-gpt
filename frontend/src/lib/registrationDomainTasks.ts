import { normalizeDomainList } from './domainList.ts'

export const REGISTRATION_DOMAIN_TASK_MODE_FIELD = 'registration_domain_task_mode'
export const REGISTRATION_DOMAIN_TASK_MODE_COMBINED = 'combined'
export const REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN = 'per_domain'

export type RegistrationDomainTaskMode =
  | typeof REGISTRATION_DOMAIN_TASK_MODE_COMBINED
  | typeof REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN

export type RegistrationDomainTaskItem = {
  taskId: string
  domain: string
  position: number
}

export type RegistrationDomainTaskError = {
  domain: string
  position: number
  message: string
}

export type RegistrationDomainTaskGroup = {
  groupId: string
  mode: typeof REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN
  requestedDomainCount: number
  requestedCountPerTask: number
  requestedConcurrencyPerTask: number
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
  return String(value || '').trim().toLowerCase() === REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN
    ? REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN
    : REGISTRATION_DOMAIN_TASK_MODE_COMBINED
}

export function registrationTaskCreateEndpoint(mode: unknown) {
  return normalizeRegistrationDomainTaskMode(mode) === REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN
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

export async function createRegistrationTasks(
  apiFetch: RegistrationTaskApiFetch,
  request: RegistrationTaskRequestPayload,
  mode: unknown,
): Promise<Record<string, unknown>> {
  const normalizedMode = normalizeRegistrationDomainTaskMode(mode)
  if (normalizedMode !== REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN) {
    return recordOf(await apiFetch('/tasks/register', {
      method: 'POST',
      body: JSON.stringify(request),
    }))
  }

  try {
    return recordOf(await apiFetch('/tasks/register/by-domain', {
      method: 'POST',
      body: JSON.stringify(request),
    }))
  } catch (error: unknown) {
    if (![404, 405].includes(requestErrorStatus(error))) throw error
  }

  // Rolling deployments may briefly serve the new frontend from an instance
  // whose running backend predates the group endpoint. Preserve the feature by
  // expanding the same frozen request through the existing manual-task API.
  const extra = recordOf(request.extra)
  const domains = normalizeDomainList(extra.tempmail_fixed_domains)
  if (domains.length === 0) {
    throw new Error('按域名拆分任务时没有可用的 TempMail 域名')
  }

  const settled = await Promise.allSettled(domains.map(async (domain, index) => {
    const childRequest = {
      ...request,
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
      message,
    }]
  })

  const requestedDomainCount = positiveInteger(
    payload.requested_domain_count ?? payload.requestedDomainCount,
    tasks.length + errors.length,
  )
  return {
    groupId: String(payload.task_group_id || payload.groupId || '').trim()
      || `registration-domain-group-${tasks[0].taskId}`,
    mode: REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN,
    requestedDomainCount: Math.max(requestedDomainCount, tasks.length + errors.length),
    requestedCountPerTask: positiveInteger(
      payload.requested_count_per_task ?? payload.requestedCountPerTask,
      1,
    ),
    requestedConcurrencyPerTask: positiveInteger(
      payload.requested_concurrency_per_task ?? payload.requestedConcurrencyPerTask,
      1,
    ),
    tasks,
    errors,
  }
}

export function registrationDomainTaskTotalTarget(
  domainCount: unknown,
  requestedCountPerTask: unknown,
) {
  return positiveInteger(domainCount) * positiveInteger(requestedCountPerTask)
}
