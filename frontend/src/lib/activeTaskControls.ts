import { activeTaskLabel } from './taskTypes.ts'

export type ActiveTaskSnapshot = {
  id?: string | number
  task_id?: string | number
  status?: string
  source?: string
  progress?: string | number
  meta?: Record<string, unknown>
  capabilities?: {
    stop_after_current?: unknown
    stop_modes?: unknown
  }
  control?: {
    stop_mode?: unknown
    stop_requested?: unknown
    stop_after_current_requested?: unknown
  }
  email?: string
  platform?: string
  [key: string]: unknown
}

export type ActiveTaskStopMode = 'immediate' | 'after_current'
export type ActiveTaskStopTargetType = 'task' | 'registration_domain_group'

export type ActiveTaskStopTarget = {
  key: string
  targetType: ActiveTaskStopTargetType
  targetId: string
  label: string
  items: ActiveTaskSnapshot[]
  supportsAfterCurrent: boolean
  stopMode: '' | ActiveTaskStopMode
}

export type BatchStopRequest = {
  mode: ActiveTaskStopMode
  task_ids: string[]
  registration_domain_group_ids: string[]
}

export type BatchStopResultItem = {
  target_type: ActiveTaskStopTargetType
  target_id: string
  status: 'accepted' | 'already_requested' | 'already_terminal' | 'not_found' | 'failed' | string
  code?: string
  message?: string
  registration_domain_group_id?: string
}

export type BatchStopResponse = {
  ok?: boolean
  mode?: ActiveTaskStopMode
  requested_count?: number
  partial_failure?: boolean
  summary?: Partial<Record<'accepted' | 'already_requested' | 'already_terminal' | 'not_found' | 'failed', number>>
  results?: BatchStopResultItem[]
}

function recordOf(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function taskIdOf(item: ActiveTaskSnapshot): string {
  return String(item.id || item.task_id || '').trim()
}

function stopModeOf(item: ActiveTaskSnapshot): '' | ActiveTaskStopMode {
  const mode = String(item.control?.stop_mode || '').trim().toLowerCase()
  if (mode === 'immediate' || item.control?.stop_requested === true) return 'immediate'
  if (mode === 'after_current' || item.control?.stop_after_current_requested === true) {
    return 'after_current'
  }
  return ''
}

function supportsAfterCurrent(item: ActiveTaskSnapshot): boolean {
  const stopModes = Array.isArray(item.capabilities?.stop_modes)
    ? item.capabilities?.stop_modes
    : []
  return item.capabilities?.stop_after_current === true || stopModes.includes('after_current')
}

function rotationGroupIdOf(item: ActiveTaskSnapshot): string {
  const group = recordOf(recordOf(item.meta).registration_domain_task_group)
  const mode = String(group.mode || '').trim().toLowerCase()
  return mode === 'rotating' ? String(group.id || '').trim() : ''
}

function strongestStopMode(items: ActiveTaskSnapshot[]): '' | ActiveTaskStopMode {
  const modes = items.map(stopModeOf)
  if (modes.includes('immediate')) return 'immediate'
  if (modes.includes('after_current')) return 'after_current'
  return ''
}

export function activeTaskTargetKey(
  targetType: ActiveTaskStopTargetType,
  targetId: string,
) {
  return `${targetType}:${targetId}`
}

export function buildActiveTaskStopTargets(items: ActiveTaskSnapshot[]): ActiveTaskStopTarget[] {
  const targets: ActiveTaskStopTarget[] = []
  const targetByKey = new Map<string, ActiveTaskStopTarget>()

  items.forEach((item) => {
    const taskId = taskIdOf(item)
    if (!taskId) return
    const groupId = rotationGroupIdOf(item)
    const targetType: ActiveTaskStopTargetType = groupId
      ? 'registration_domain_group'
      : 'task'
    const targetId = groupId || taskId
    const key = activeTaskTargetKey(targetType, targetId)
    const existing = targetByKey.get(key)
    if (existing) {
      existing.items.push(item)
      existing.label = `自动轮换 · ${existing.items.length} 个运行任务`
      existing.stopMode = strongestStopMode(existing.items)
      return
    }

    const target: ActiveTaskStopTarget = {
      key,
      targetType,
      targetId,
      label: groupId ? '自动轮换 · 1 个运行任务' : activeTaskLabel(item),
      items: [item],
      supportsAfterCurrent: groupId ? true : supportsAfterCurrent(item),
      stopMode: stopModeOf(item),
    }
    targets.push(target)
    targetByKey.set(key, target)
  })
  return targets
}

export function buildBatchStopRequest(
  targets: ActiveTaskStopTarget[],
  mode: ActiveTaskStopMode,
): BatchStopRequest {
  return targets.reduce<BatchStopRequest>((request, target) => {
    if (target.targetType === 'registration_domain_group') {
      request.registration_domain_group_ids.push(target.targetId)
    } else {
      request.task_ids.push(target.targetId)
    }
    return request
  }, {
    mode,
    task_ids: [],
    registration_domain_group_ids: [],
  })
}

export function failedBatchStopTargetKeys(response: BatchStopResponse): Set<string> {
  return new Set(
    (Array.isArray(response.results) ? response.results : [])
      .filter((result) => result.status === 'failed')
      .map((result) => activeTaskTargetKey(result.target_type, String(result.target_id || '').trim()))
      .filter((key) => !key.endsWith(':')),
  )
}
