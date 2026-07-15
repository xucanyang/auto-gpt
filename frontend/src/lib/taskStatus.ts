export type NormalizedTaskStatus = 'pending' | 'running' | 'done' | 'failed' | 'stopped' | 'partial' | 'interrupted' | 'unknown'
export type TaskTerminalStatus = Extract<NormalizedTaskStatus, 'done' | 'failed' | 'stopped' | 'partial' | 'interrupted'>

const PENDING_TASK_STATUSES = new Set(['pending', 'queued', 'created', 'waiting'])
const RUNNING_TASK_STATUSES = new Set([
  'running',
  'started',
  'starting',
  'processing',
  'in_progress',
  'in-progress',
  'stopping',
  'cancelling',
  'canceling',
])
const DONE_TASK_STATUSES = new Set(['done', 'success', 'succeeded', 'complete', 'completed'])
const FAILED_TASK_STATUSES = new Set(['failed', 'failure', 'error'])
const PARTIAL_TASK_STATUSES = new Set(['partial', 'partial_failure'])
const INTERRUPTED_TASK_STATUSES = new Set(['interrupted', 'remote_interrupted'])
const STOPPED_TASK_STATUSES = new Set([
  'stopped',
  'cancelled',
  'canceled',
  'aborted',
  'terminated',
  'skipped',
])

export function normalizeTaskStatus(value: unknown): NormalizedTaskStatus {
  const normalized = String(value || '').trim().toLowerCase()
  if (PENDING_TASK_STATUSES.has(normalized)) return 'pending'
  if (RUNNING_TASK_STATUSES.has(normalized)) return 'running'
  if (DONE_TASK_STATUSES.has(normalized)) return 'done'
  if (FAILED_TASK_STATUSES.has(normalized)) return 'failed'
  if (PARTIAL_TASK_STATUSES.has(normalized)) return 'partial'
  if (INTERRUPTED_TASK_STATUSES.has(normalized)) return 'interrupted'
  if (STOPPED_TASK_STATUSES.has(normalized)) return 'stopped'
  return 'unknown'
}

export function isActiveTaskStatus(value: unknown): boolean {
  const status = normalizeTaskStatus(value)
  // Only an explicit active status may keep a client-side poller alive. Treating
  // unknown values as pending turns new/terminal backend states into endless loops.
  return status === 'pending' || status === 'running'
}

export function getTaskTerminalStatus(value: unknown): TaskTerminalStatus | null {
  const status = normalizeTaskStatus(value)
  return status === 'done' || status === 'failed' || status === 'stopped' || status === 'partial' || status === 'interrupted'
    ? status
    : null
}
