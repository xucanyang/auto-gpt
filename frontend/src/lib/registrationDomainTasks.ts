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
