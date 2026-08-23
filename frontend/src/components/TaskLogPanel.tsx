import { type ComponentProps, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Badge, Button, Card, Descriptions, Empty, message, Popconfirm, Segmented, Space, Table, Tag, Timeline, theme, Tooltip } from 'antd'
import { CopyOutlined, FastForwardOutlined, PauseCircleOutlined, PoweroffOutlined, ReloadOutlined, StopOutlined } from '@ant-design/icons'

import IdeaSubmitSummary from '@/components/idea/IdeaSubmitSummary'
import { TaskVerificationPanel } from '@/components/TaskVerificationPanel'
import type { PendingVerificationChallenge } from '@/components/TaskVerificationPanel'
import { RegistrationDiagnosticsPanel } from '@/components/RegistrationDiagnosticsPanel'
import {
  RegistrationTaskLogTabs,
  type RegistrationTaskLogRegionStatus,
} from '@/features/auth/components/RegistrationTaskLogTabs'
import { consumeEventStream, isAbortError } from '@/lib/eventStream'
import { paymentEligibilityFailureBreakdown } from '@/lib/paymentEligibilityFailure'
import {
  REGISTRATION_LOG_REGION_LABELS,
  REGISTRATION_LOG_REGIONS,
  isRegistrationTaskSnapshot,
  partitionRegistrationTaskLogs,
  type RegistrationLogRegion,
} from '@/lib/registrationTaskLogs'
import { ApiError, apiFetch } from '@/lib/utils'
import { getTaskTerminalStatus, type TaskTerminalStatus } from '@/lib/taskStatus'

interface TaskLogPanelProps {
  taskId: string
  onDone?: () => Promise<void> | void
  showTaskControls?: boolean
}

type TaskPanelStatus = 'idle' | TaskTerminalStatus
type LogViewMode = 'info' | 'debug'
type StopMode = 'none' | 'after_current' | 'immediate'
type WebSessionLeaseStatus =
  | 'reserved'
  | 'waiting_capacity'
  | 'authenticating'
  | 'refreshing_session'
  | 'ready_holding'
  | 'releasing'
  | 'released'
  | 'stopped'
  | 'failed'
  | 'interrupted'

type WebSessionLeaseSnapshot = {
  lease_id: string
  account_id: number
  email?: string
  status: WebSessionLeaseStatus | string
  ready_at?: string
  held_seconds?: number
  release_requested?: boolean
  profile_saved?: boolean
  restored_profile?: boolean
  profile_path?: string
  refresh_count?: number
  gcash_state?: string
  gcash_error?: string
  gcash_link_expires_at?: number
  gcash_qr_expires_at?: number
  gcash_tab_state?: string
  gcash_tab_opened_at?: string
  gcash_tab_updated_at?: string
  gcash_tab_last_error?: string
  error?: string
}

type WebSessionLeaseCounts = {
  total: number
  active: number
  holding: number
  released: number
  failed: number
  gcashRunning: number
  gcashSucceeded: number
  gcashFailed: number
  gcashTabReady: number
}
type TaskCurrentState = {
  task?: string
  task_label?: string
  item_index?: number
  item_total?: number
  email?: string
  account_id?: number
  phone?: string
  phase?: string
  phase_label?: string
  stage_index?: number
  stage_total?: number
  started_at?: string
  last_message?: string
  next_step?: string
  resource_touched?: boolean
}

type PaymentEvent = {
  id?: number
  account_id?: number
  account?: string
  stage?: string
  level?: string
  message?: string
  metadata?: Record<string, unknown>
  created_at?: string
}

type TaskSnapshot = {
  platform?: string
  source?: string
  status?: string
  status_snapshot?: string
  logs?: string[]
  payment_events?: PaymentEvent[]
  timeline?: PaymentEvent[]
  log_next_index?: number
  capabilities?: {
    stop_after_current?: boolean
    stop_modes?: string[]
  }
  control?: {
    stop_requested?: boolean
    stop_after_current_requested?: boolean
    after_current_requested?: boolean
    stop_mode?: StopMode
  }
  pending_verification?: {
    challenge_id: string
    phase?: string
    phase_label?: string
    email?: string
    created_at?: number
    expires_at?: number
    timeout_seconds?: number
    metadata?: Record<string, unknown>
    actions?: string[]
  } | null
  meta?: {
    source?: string
    current?: TaskCurrentState
    idea_submit_summary?: ComponentProps<typeof IdeaSubmitSummary>['summary']
    registration_diagnostics?: { mode?: string }
    eligibility_kind?: string
    eligibility_summary?: Record<string, unknown>
    eligibility_failure_summary?: Record<string, number>
    results?: unknown[]
    registration_browser?: Record<string, unknown>
    registration_mailbox?: Record<string, unknown>
    registration_domain_task_group?: Record<string, unknown>
    phone_signup?: Record<string, unknown>
    registration_pipeline_request?: Record<string, unknown>
    registration_zero_amount_eligibility_request?: Record<string, unknown>
    registration_zero_amount_eligibility?: Record<string, unknown>
    registration_paypal_link_request?: Record<string, unknown>
    registration_paypal_payment_request?: Record<string, unknown>
    registration_paypal_payment?: Record<string, unknown>
  }
}

const LOG_VIEW_STORAGE_KEY = 'task-log-panel-view-mode'
const EMPTY_WEB_SESSION_LEASE_COUNTS: WebSessionLeaseCounts = {
  total: 0,
  active: 0,
  holding: 0,
  released: 0,
  failed: 0,
  gcashRunning: 0,
  gcashSucceeded: 0,
  gcashFailed: 0,
  gcashTabReady: 0,
}

const ACTIVE_WEB_SESSION_LEASE_STATUSES = new Set([
  'reserved',
  'waiting_capacity',
  'authenticating',
  'refreshing_session',
  'ready_holding',
  'releasing',
])

function webSessionLeaseStatusView(status: string) {
  switch (status) {
    case 'reserved':
      return { label: '已登记', color: 'default' }
    case 'waiting_capacity':
      return { label: '等待容量', color: 'gold' }
    case 'authenticating':
      return { label: '登录中', color: 'processing' }
    case 'refreshing_session':
      return { label: '同步中', color: 'cyan' }
    case 'ready_holding':
      return { label: '保持中', color: 'success' }
    case 'releasing':
      return { label: '释放中', color: 'warning' }
    case 'released':
      return { label: '已释放', color: 'default' }
    case 'stopped':
      return { label: '已停止', color: 'default' }
    case 'failed':
      return { label: '登录失败', color: 'error' }
    case 'interrupted':
      return { label: '异常中断', color: 'error' }
    default:
      return { label: status || '未知', color: 'default' }
  }
}

function webSessionGcashStatusView(status: string) {
  switch (String(status || '').trim().toLowerCase()) {
    case 'not_requested':
      return { label: '等待登录', color: 'default' }
    case 'queued':
      return { label: '提链排队', color: 'gold' }
    case 'submitting':
    case 'running':
      return { label: '提链中', color: 'processing' }
    case 'succeeded':
      return { label: '链接成功', color: 'success' }
    case 'failed':
      return { label: '提链失败', color: 'error' }
    case 'interrupted':
      return { label: '提链中断', color: 'warning' }
    default:
      return { label: status || '未开始', color: 'default' }
  }
}

function webSessionGcashTabStatusView(status: string) {
  switch (String(status || '').trim().toLowerCase()) {
    case 'not_requested':
      return { label: '未打开', color: 'default' }
    case 'opening':
      return { label: '打开中', color: 'processing' }
    case 'ready':
      return { label: '已打开', color: 'success' }
    case 'closed':
      return { label: '已关闭', color: 'default' }
    case 'timed_out':
      return { label: '打开超时', color: 'warning' }
    case 'cancelled':
      return { label: '已取消', color: 'default' }
    case 'failed':
      return { label: '打开失败', color: 'error' }
    default:
      return { label: status || '未打开', color: 'default' }
  }
}

function formatHeldDuration(value: unknown) {
  const totalSeconds = Math.max(0, Math.floor(Number(value) || 0))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

function parseLogLine(rawLine: string) {
  const line = String(rawLine || '')
  const timeMatch = line.match(/^\[((?:(?:\d{4}-\d{2}-\d{2})[ T])?\d{2}:\d{2}:\d{2})\]\s*/)
  const time = timeMatch?.[1] || ''
  const normalized = line.replace(/^\[(?:(?:\d{4}-\d{2}-\d{2})[ T])?\d{2}:\d{2}:\d{2}\]\s*/, '')
  const isDebug = /^\[[^\]]*DEBUG[^\]]*\]/i.test(normalized)
  const text = isDebug ? normalized.replace(/^\[[^\]]*DEBUG[^\]]*\]\s*/, '') : normalized
  const phoneBindingAccountMatch = text.match(/^\[手机号绑定\]\[账号\s+(\d+)\/(\d+)\]/)
  const phoneBindingAccountKey = phoneBindingAccountMatch
    ? `${phoneBindingAccountMatch[1]}/${phoneBindingAccountMatch[2]}`
    : ''
  return { raw: line, text, isDebug, time, phoneBindingAccountKey }
}

function recordOf(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function countOf(value: unknown): number {
  const parsed = Number(value || 0)
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 0
}

function registrationTaskRegionStatus(
  snapshot: TaskSnapshot | null,
  terminalStatus: TaskPanelStatus,
  region: RegistrationLogRegion,
  logCount: number,
): RegistrationTaskLogRegionStatus {
  const meta = recordOf(snapshot?.meta)
  const pipelineRequest = recordOf(meta.registration_pipeline_request)
  const zeroRequest = recordOf(meta.registration_zero_amount_eligibility_request)
  const zeroRuntime = recordOf(meta.registration_zero_amount_eligibility)
  const zeroCounts = recordOf(zeroRuntime.counts)
  const linkRequest = recordOf(meta.registration_paypal_link_request)
  const paymentRequest = recordOf(meta.registration_paypal_payment_request)
  const paypalRuntime = recordOf(meta.registration_paypal_payment)
  const paypalCounts = recordOf(paypalRuntime.counts)
  const followup = recordOf(paypalRuntime.followup)
  const snapshotStatus = String(snapshot?.status || snapshot?.status_snapshot || '').trim().toLowerCase()

  if (region === 'registration') {
    if (terminalStatus === 'done') return { color: 'success', label: '已完成' }
    if (terminalStatus === 'stopped') return { color: 'warning', label: '已停止' }
    if (terminalStatus === 'failed') return { color: 'error', label: '失败' }
    if (terminalStatus === 'partial') return { color: 'warning', label: '部分失败' }
    if (terminalStatus === 'interrupted') return { color: 'warning', label: '结果未知' }
    if (snapshotStatus === 'pending') return { color: 'default', label: '排队中' }
    return { color: 'processing', label: snapshot ? '进行中' : '加载中' }
  }

  if (region === 'zero_amount') {
    const requested = pipelineRequest.zero_amount_enabled === true
      || zeroRequest.enabled === true
      || zeroRuntime.enabled === true
      || logCount > 0
    if (!requested) return { color: 'default', label: '未开启' }
    if (countOf(zeroCounts.running) + countOf(zeroCounts.queued) > 0) {
      return { color: 'processing', label: '检测中' }
    }
    if (zeroRuntime.finished === true) {
      return countOf(zeroCounts.probe_failed) > 0
        ? { color: 'warning', label: '已完成 · 有失败' }
        : { color: 'success', label: '已完成' }
    }
    if (terminalStatus !== 'idle' && logCount === 0) return { color: 'default', label: '未执行' }
    return { color: 'default', label: '等待注册结果' }
  }

  if (region === 'payment_link') {
    const requested = pipelineRequest.payment_link_enabled === true
      || linkRequest.enabled === true
      || paypalRuntime.enabled === true
      || logCount > 0
    if (!requested) return { color: 'default', label: '未开启' }
    if (countOf(paypalCounts.running) + countOf(paypalCounts.queued) > 0) {
      return { color: 'processing', label: '提链中' }
    }
    if (paypalRuntime.finished === true) {
      return countOf(paypalCounts.extract_failed) > 0
        ? { color: 'warning', label: '已完成 · 有失败' }
        : { color: 'success', label: '已完成' }
    }
    if (terminalStatus !== 'idle' && logCount === 0) return { color: 'default', label: '未执行' }
    return { color: 'default', label: '等待0元结果' }
  }

  const requested = pipelineRequest.payment_enabled === true
    || paymentRequest.enabled === true
    || paypalRuntime.payment_enabled === true
    || logCount > 0
  if (!requested) return { color: 'default', label: '未开启' }
  if (
    countOf(followup.active) > 0
    || countOf(followup.processing) > 0
    || (logCount > 0 && paypalRuntime.finished !== true)
  ) {
    return { color: 'processing', label: '处理中' }
  }
  if (followup.finished === true || (paypalRuntime.finished === true && terminalStatus !== 'idle')) {
    return countOf(followup.failed) + countOf(followup.unknown) + countOf(paypalCounts.submit_failed) > 0
      ? { color: 'warning', label: '已完成 · 有异常' }
      : { color: 'success', label: '已完成' }
  }
  if (terminalStatus !== 'idle' && logCount === 0) return { color: 'default', label: '未执行' }
  return { color: 'default', label: '等待提链' }
}

export function TaskLogPanel({ taskId, onDone, showTaskControls = true }: TaskLogPanelProps) {
  const { token } = theme.useToken()
  const [lines, setLines] = useState<string[]>([])
  const [error, setError] = useState('')
  const [terminalStatus, setTerminalStatus] = useState<TaskPanelStatus>('idle')
  const [taskSnapshot, setTaskSnapshot] = useState<TaskSnapshot | null>(null)
  const [current, setCurrent] = useState<TaskCurrentState | null>(null)
  const [currentNow, setCurrentNow] = useState(() => Date.now())
  const [pageVisible, setPageVisible] = useState(
    typeof document === 'undefined' ? true : document.visibilityState === 'visible',
  )
  const [skipLoading, setSkipLoading] = useState(false)
  const [stopLoading, setStopLoading] = useState(false)
  const [stopMode, setStopMode] = useState<StopMode>('none')
  const [webSessionLeases, setWebSessionLeases] = useState<WebSessionLeaseSnapshot[]>([])
  const [webSessionLeaseCounts, setWebSessionLeaseCounts] = useState<WebSessionLeaseCounts>(EMPTY_WEB_SESSION_LEASE_COUNTS)
  const [webSessionLeaseLoading, setWebSessionLeaseLoading] = useState(false)
  const [webSessionLeaseError, setWebSessionLeaseError] = useState('')
  const [webSessionLeaseAction, setWebSessionLeaseAction] = useState('')
  const [registrationRegion, setRegistrationRegion] = useState<RegistrationLogRegion>('registration')
  const [viewMode, setViewMode] = useState<LogViewMode>(() => {
    if (typeof window === 'undefined') return 'info'
    const saved = window.localStorage.getItem(LOG_VIEW_STORAGE_KEY)
    return saved === 'debug' ? 'debug' : 'info'
  })
  const panelRef = useRef<HTMLDivElement>(null)
  const onDoneRef = useRef(onDone)
  const nextSinceRef = useRef(0)
  const terminalNotifyRef = useRef('')
  const doneCallbackNotifyRef = useRef('')

  const isFinished = terminalStatus !== 'idle'
  const interactionLocked = isFinished || stopMode !== 'none'
  const supportsStopAfterCurrent = Boolean(
    taskSnapshot?.capabilities?.stop_after_current
      || taskSnapshot?.capabilities?.stop_modes?.includes?.('after_current'),
  )
  const taskSource = String(taskSnapshot?.source || taskSnapshot?.meta?.source || '').trim().toLowerCase()
  const isGcashWebSessionTask = taskSource === 'web_session_gcash_link' || taskSource === 'batch_web_session_gcash_link'
  const isWebSessionTask = taskSource === 'web_session_login'
    || taskSource === 'batch_web_session_login'
    || isGcashWebSessionTask
  const isBatchWebSessionTask = taskSource === 'batch_web_session_login' || taskSource === 'batch_web_session_gcash_link'
  const paymentEvents: PaymentEvent[] = useMemo(
    () => Array.isArray(taskSnapshot?.payment_events)
      ? taskSnapshot.payment_events
      : Array.isArray(taskSnapshot?.timeline)
        ? taskSnapshot.timeline
        : [],
    [taskSnapshot?.payment_events, taskSnapshot?.timeline],
  )
  const isRegistrationTask = useMemo(
    () => isRegistrationTaskSnapshot(taskSnapshot, lines),
    [lines, taskSnapshot],
  )
  const registrationLogRegions = useMemo(
    () => partitionRegistrationTaskLogs(lines, paymentEvents),
    [lines, paymentEvents],
  )
  const registrationRegionCounts = useMemo(
    () => REGISTRATION_LOG_REGIONS.reduce((result, region) => {
      result[region] = registrationLogRegions[region].length
      return result
    }, {} as Record<RegistrationLogRegion, number>),
    [registrationLogRegions],
  )
  const activeLines = useMemo(
    () => (
      isRegistrationTask
        ? registrationLogRegions[registrationRegion]
        : lines
    ).slice(-4000),
    [isRegistrationTask, lines, registrationLogRegions, registrationRegion],
  )
  const parsedLines = useMemo(() => activeLines.map(parseLogLine), [activeLines])
  const effectiveViewMode: LogViewMode = isRegistrationTask && registrationRegion !== 'registration'
    ? 'info'
    : viewMode
  const infoCount = useMemo(() => parsedLines.filter((line) => !line.isDebug).length, [parsedLines])
  const debugCount = useMemo(() => parsedLines.filter((line) => line.isDebug).length, [parsedLines])
  const visibleLines = useMemo(
    () => parsedLines.filter((line) => (effectiveViewMode === 'debug' ? line.isDebug : !line.isDebug)),
    [effectiveViewMode, parsedLines],
  )
  const groupedVisibleLines = useMemo(() => {
    let lastPhoneBindingAccountKey = ''
    return visibleLines.map((line) => {
      const key = line.phoneBindingAccountKey
      const accountGap = Boolean(key && lastPhoneBindingAccountKey && key !== lastPhoneBindingAccountKey)
      if (key) lastPhoneBindingAccountKey = key
      return { ...line, accountGap }
    })
  }, [visibleLines])
  const activeRegistrationRegionStatus = useMemo(
    () => registrationTaskRegionStatus(
      taskSnapshot,
      terminalStatus,
      registrationRegion,
      registrationRegionCounts[registrationRegion],
    ),
    [registrationRegion, registrationRegionCounts, taskSnapshot, terminalStatus],
  )
  const snapshotMeta = recordOf(taskSnapshot?.meta)
  const registrationDomainTaskGroup = recordOf(snapshotMeta.registration_domain_task_group)
  const rotatingRegistrationGroupId = (
    String(registrationDomainTaskGroup.mode || '').trim().toLowerCase() === 'rotating'
      ? String(registrationDomainTaskGroup.id || '').trim()
      : ''
  )
  const registrationPipelineRequest = recordOf(snapshotMeta.registration_pipeline_request)
  const registrationPaypalLinkRequest = recordOf(snapshotMeta.registration_paypal_link_request)
  const registrationPaypalPaymentRequest = recordOf(snapshotMeta.registration_paypal_payment_request)
  const registrationPaypalRuntime = recordOf(snapshotMeta.registration_paypal_payment)
  const registrationPaypalTracking = isRegistrationTask && (
    registrationPipelineRequest.payment_link_enabled === true
    || registrationPipelineRequest.payment_enabled === true
    || registrationPaypalLinkRequest.enabled === true
    || registrationPaypalPaymentRequest.enabled === true
    || registrationPaypalRuntime.enabled === true
    || paymentEvents.length > 0
  )

  const fetchWebSessionLeases = useCallback(async (signal?: AbortSignal) => {
    const response = await apiFetch(`/tasks/${taskId}/web-session-leases`, { signal }) as {
      web_session_leases?: WebSessionLeaseSnapshot[]
      web_session_lease_counts?: Partial<WebSessionLeaseCounts>
    }
    const leases = Array.isArray(response.web_session_leases)
      ? response.web_session_leases
      : []
    const rawCounts = response.web_session_lease_counts || {}
    setWebSessionLeases(leases)
    setWebSessionLeaseCounts({
      total: Number(rawCounts.total ?? leases.length) || 0,
      active: Number(rawCounts.active ?? leases.filter((item) => ACTIVE_WEB_SESSION_LEASE_STATUSES.has(String(item.status || ''))).length) || 0,
      holding: Number(rawCounts.holding ?? leases.filter((item) => item.status === 'ready_holding').length) || 0,
      released: Number(rawCounts.released ?? leases.filter((item) => item.status === 'released').length) || 0,
      failed: Number(rawCounts.failed ?? leases.filter((item) => item.status === 'failed' || item.status === 'interrupted').length) || 0,
      gcashRunning: Number(rawCounts.gcashRunning ?? leases.filter((item) => ['queued', 'submitting', 'running'].includes(String(item.gcash_state || '').trim().toLowerCase())).length) || 0,
      gcashSucceeded: Number(rawCounts.gcashSucceeded ?? leases.filter((item) => item.gcash_state === 'succeeded').length) || 0,
      gcashFailed: Number(rawCounts.gcashFailed ?? leases.filter((item) => ['failed', 'interrupted'].includes(String(item.gcash_state || ''))).length) || 0,
      gcashTabReady: Number(rawCounts.gcashTabReady ?? leases.filter((item) => item.gcash_tab_state === 'ready').length) || 0,
    })
    setWebSessionLeaseError('')
    return leases
  }, [taskId])

  const handleWebSessionLeaseAction = async (
    action: 'refresh' | 'release' | 'release-all',
    accountId?: number,
  ) => {
    if (!taskId || webSessionLeaseAction) return
    const actionKey = action === 'release-all' ? action : `${action}:${Number(accountId || 0)}`
    setWebSessionLeaseAction(actionKey)
    try {
      const path = action === 'release-all'
        ? `/tasks/${taskId}/web-session-leases/release-all`
        : `/tasks/${taskId}/web-session-leases/${Number(accountId || 0)}/${action}`
      await apiFetch(path, { method: 'POST' })
      await fetchWebSessionLeases()
      if (action === 'refresh') {
        message.success('最新 AT、Session 与 Cookie 已同步')
      } else if (action === 'release-all') {
        if (isBatchWebSessionTask) setStopMode('after_current')
        message.success('已停止新增浏览器，并请求保存、释放全部本地浏览器')
      } else {
        message.success('已请求保存 Profile 并释放本地浏览器')
      }
    } catch (error_: unknown) {
      const detail = error_ instanceof Error ? error_.message : '请求失败'
      message.error(detail)
    } finally {
      setWebSessionLeaseAction('')
    }
  }

  const handleCopyAll = async () => {
    try {
      const text = groupedVisibleLines
        .flatMap((line) => (line.accountGap ? ['', line.raw] : [line.raw]))
        .join('\n')
      await navigator.clipboard.writeText(text)
      message.success('日志已复制')
    } catch {
      message.error('复制失败')
    }
  }

  const handleSkipCurrent = async () => {
    if (interactionLocked) return
    setSkipLoading(true)
    try {
      const response = await apiFetch(`/tasks/${taskId}/skip-current`, { method: 'POST' }) as {
        control?: { targeted_skip_attempts?: number }
      }
      const targeted = Number(response.control?.targeted_skip_attempts || 0)
      message.success(
        targeted > 1
          ? `已发送跳过 ${targeted} 个进行中账号请求`
          : '已发送跳过当前账号请求',
      )
    } catch (error_: unknown) {
      const detail = error_ instanceof Error ? error_.message : '请求失败'
      message.error(detail)
    } finally {
      setSkipLoading(false)
    }
  }

  const handleStopTask = async (mode: Exclude<StopMode, 'none'>) => {
    if (isFinished || stopLoading || (mode === 'after_current' && stopMode !== 'none') || (mode === 'immediate' && stopMode === 'immediate')) return
    setStopLoading(true)
    try {
      const stopPath = rotatingRegistrationGroupId
        ? `/tasks/register/domain-groups/${encodeURIComponent(rotatingRegistrationGroupId)}/stop`
        : `/tasks/${taskId}/stop`
      const response = await apiFetch(stopPath, {
        method: 'POST',
        body: JSON.stringify({ mode }),
      }) as {
        control?: {
          stop_mode?: StopMode
          stop_requested?: boolean
          stop_after_current_requested?: boolean
        }
      }
      const returnedMode = response.control?.stop_mode
      setStopMode(
        returnedMode === 'after_current' || returnedMode === 'immediate'
          ? returnedMode
          : response.control?.stop_requested
            ? 'immediate'
            : response.control?.stop_after_current_requested
              ? 'after_current'
              : mode,
      )
      if (mode === 'after_current') {
        message.success(
          rotatingRegistrationGroupId
            ? '已停止轮换补位与技术重试；当前执行中的账号会正常完成后整组收口'
            : isWebSessionTask
            ? '已停止新增浏览器；当前浏览器继续保持，等待逐个释放'
            : '已停止后续账号投递；当前执行中的账号会正常完成，日志已保存',
        )
      } else {
        message.success(
          rotatingRegistrationGroupId
            ? '已请求立即停止整个轮换组；等待域名、技术重试和后续补位均已取消'
            : '已请求立即停止；已运行日志已保存，正在等待任务收口',
        )
      }
    } catch (error_: unknown) {
      const detail = error_ instanceof Error ? error_.message : '请求失败'
      message.error(detail)
    } finally {
      setStopLoading(false)
    }
  }

  useEffect(() => {
    onDoneRef.current = onDone
  }, [onDone])

  useEffect(() => {
    setRegistrationRegion('registration')
  }, [taskId])

  useEffect(() => {
    if (typeof window === 'undefined') return
    window.localStorage.setItem(LOG_VIEW_STORAGE_KEY, viewMode)
  }, [viewMode])

  useEffect(() => {
    if (typeof document === 'undefined') return
    const updateVisibility = () => {
      setPageVisible(document.visibilityState === 'visible')
    }
    updateVisibility()
    document.addEventListener('visibilitychange', updateVisibility)
    return () => {
      document.removeEventListener('visibilitychange', updateVisibility)
    }
  }, [])

  useEffect(() => {
    if (!current?.started_at || terminalStatus !== 'idle' || !pageVisible) return
    const timer = window.setInterval(() => setCurrentNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [current?.started_at, pageVisible, terminalStatus])

  useEffect(() => {
    if (!taskId || !pageVisible) return
    const controller = new AbortController()
    let cancelled = false
    const timers = new Set<number>()
    const baseRetryMs = 1000
    const maxRetryMs = 8000
    nextSinceRef.current = 0
    setLines([])
    setError('')
    setTerminalStatus('idle')
    setStopMode('none')
    setTaskSnapshot(null)
    setWebSessionLeases([])
    setWebSessionLeaseCounts(EMPTY_WEB_SESSION_LEASE_COUNTS)
    setWebSessionLeaseError('')
    setWebSessionLeaseAction('')

    const sleep = (ms: number) => new Promise<void>((resolve) => {
      let settled = false
      const finish = () => {
        if (settled) return
        settled = true
        timers.delete(timer)
        controller.signal.removeEventListener('abort', finish)
        resolve()
      }
      const timer = window.setTimeout(finish, ms)
      timers.add(timer)
      controller.signal.addEventListener('abort', finish, { once: true })
    })

    const notifyTaskDone = (status?: TaskTerminalStatus | string) => {
      const key = `${taskId}:done`
      if (doneCallbackNotifyRef.current === key) return
      doneCallbackNotifyRef.current = key

      const timer = window.setTimeout(() => {
        timers.delete(timer)
        if (cancelled) return
        void Promise.resolve(onDoneRef.current?.()).catch((error_: unknown) => {
          const detail = error_ instanceof Error ? error_.message : '刷新页面状态失败'
          message.warning(`任务已结束，但刷新页面状态失败：${detail}`)
        })
      }, status === 'failed' ? 0 : 500)
      timers.add(timer)
    }

    const initSnapshot = async (): Promise<boolean> => {
      try {
        const snapshot = await apiFetch(`/tasks/${taskId}`, { signal: controller.signal }) as {
          logs?: string[]
          status?: TaskTerminalStatus | string
          status_snapshot?: string
          control?: {
            stop_requested?: boolean
            stop_after_current_requested?: boolean
            after_current_requested?: boolean
            stop_mode?: StopMode
          }
          meta?: { current?: TaskCurrentState }
          log_next_index?: number
        }
        if (cancelled) return true

        setTaskSnapshot(snapshot)
        const snapshotLines = Array.isArray(snapshot.logs) ? snapshot.logs : []
        setLines(snapshotLines.slice(-4000))
        const snapshotNextIndex = Number(snapshot.log_next_index)
        nextSinceRef.current = Number.isFinite(snapshotNextIndex)
          ? Math.max(0, snapshotNextIndex)
          : snapshotLines.length
        const snapshotStopMode = snapshot.control?.stop_mode
        setStopMode(
          snapshotStopMode === 'after_current' || snapshotStopMode === 'immediate'
            ? snapshotStopMode
            : snapshot.control?.stop_requested
              ? 'immediate'
              : snapshot.control?.stop_after_current_requested || snapshot.control?.after_current_requested
                ? 'after_current'
                : 'none',
        )
        setCurrent(snapshot?.meta?.current && typeof snapshot.meta.current === 'object' ? snapshot.meta.current : null)

        const terminal = getTaskTerminalStatus(snapshot.status || snapshot.status_snapshot)
        if (terminal) {
          setTerminalStatus(terminal)
          // Keep the completed task's snapshot logs visible. The SSE stream immediately ends
          // for terminal tasks and otherwise would leave the panel looking empty.
          notifyTaskDone(terminal)
          return true
        }
      } catch (error_: unknown) {
        if (!cancelled && !controller.signal.aborted) {
          const detail = error_ instanceof Error ? error_.message : '获取任务快照失败'
          setError(detail)
        }
      }
      return false
    }

    const connectStreamOnce = async (): Promise<boolean> => {
      let reachedTerminalState = false
      try {
        const since = nextSinceRef.current
        await consumeEventStream(`/tasks/${taskId}/logs/stream?since=${since}`, {
          signal: controller.signal,
          onOpen: () => {
            if (!cancelled) setError('')
          },
          onEvent: (event) => {
            if (cancelled || controller.signal.aborted) return false
            try {
              const payload = JSON.parse(event.data) as {
                line?: string
                done?: boolean
                status?: string
              }
              const logLine = payload.line
              if (typeof logLine === 'string') {
                nextSinceRef.current += 1
                setLines((previous) => [...previous, logLine].slice(-4000))
              }
              if (payload.done) {
                const terminal = getTaskTerminalStatus(payload.status) || 'done'
                setTerminalStatus(terminal)
                void apiFetch(`/tasks/${taskId}`, { signal: controller.signal })
                  .then((snapshot) => {
                    if (!cancelled) setTaskSnapshot(snapshot)
                  })
                  .catch(() => undefined)
                // Notify the parent after the terminal state is visible so pages can refresh
                // their page data without making the final log lines disappear first.
                notifyTaskDone(terminal)
                reachedTerminalState = true
                return false
              }
            } catch {
              // ignore malformed SSE payload
            }
          },
        })
        return reachedTerminalState
      } catch (error_: unknown) {
        if (cancelled || controller.signal.aborted || isAbortError(error_)) return true
        if (error_ instanceof ApiError && error_.status === 401) return true
        if (error_ instanceof ApiError && error_.status >= 400 && error_.status < 500) {
          setError(`日志流连接失败 (${error_.status})`)
          return true
        }
        return false
      }
    }

    const connectStream = async () => {
      const shouldStopImmediately = await initSnapshot()
      if (shouldStopImmediately || cancelled) return

      let retryCount = 0
      while (!cancelled) {
        const shouldStop = await connectStreamOnce()
        if (shouldStop || cancelled) return

        retryCount += 1
        const retryMs = Math.min(baseRetryMs * (2 ** (retryCount - 1)), maxRetryMs)
        setError(`日志流连接中断，${retryMs / 1000}s 后重试（第 ${retryCount} 次）`)
        await sleep(retryMs)
      }
    }

    void connectStream()

    return () => {
      cancelled = true
      controller.abort()
      timers.forEach((timer) => window.clearTimeout(timer))
      timers.clear()
    }
  }, [taskId, pageVisible])

  useEffect(() => {
    if (!taskId || !pageVisible || terminalStatus !== 'idle') return
    const paymentEligibilityTask = taskSource.includes('zero_amount_eligibility')
      || taskSource.includes('payment_methods')
      || taskSource.includes('gcash_payment_method')
      || taskSource.includes('checkout_link_type')
      || taskSource.includes('payment_eligibility_bundle')
    if (!paymentEligibilityTask) return

    const controller = new AbortController()
    let cancelled = false
    let timer = 0
    let finished = false

    const poll = async () => {
      if (cancelled) return
      try {
        const snapshot = await apiFetch(`/tasks/${taskId}`, {
          signal: controller.signal,
          cache: 'no-store',
        }) as TaskSnapshot
        if (cancelled || !snapshot || typeof snapshot !== 'object') return

        setTaskSnapshot(snapshot)
        setCurrent(snapshot?.meta?.current && typeof snapshot.meta.current === 'object' ? snapshot.meta.current : null)
        const terminal = getTaskTerminalStatus(snapshot.status || snapshot.status_snapshot)
        if (terminal) {
          finished = true
          setTerminalStatus(terminal)
        }
      } catch (error_: unknown) {
        if (cancelled || controller.signal.aborted || isAbortError(error_)) return
      } finally {
        if (!cancelled && !finished) {
          timer = window.setTimeout(poll, 500)
        }
      }
    }

    void poll()
    return () => {
      cancelled = true
      controller.abort()
      if (timer) window.clearTimeout(timer)
    }
  }, [pageVisible, taskId, taskSource, terminalStatus])

  useEffect(() => {
    if (!taskId || !pageVisible || !registrationPaypalTracking) return
    const controller = new AbortController()
    let cancelled = false
    let timer = 0

    const poll = async () => {
      if (cancelled) return
      let shouldContinue = true
      try {
        const snapshot = await apiFetch(`/tasks/${taskId}`, {
          signal: controller.signal,
          cache: 'no-store',
        }) as TaskSnapshot
        if (cancelled || !snapshot || typeof snapshot !== 'object') return

        setTaskSnapshot(snapshot)
        setCurrent(snapshot?.meta?.current && typeof snapshot.meta.current === 'object' ? snapshot.meta.current : null)
        const terminal = getTaskTerminalStatus(snapshot.status || snapshot.status_snapshot)
        if (terminal) setTerminalStatus(terminal)
        const nextMeta = recordOf(snapshot.meta)
        const nextPaypal = recordOf(nextMeta.registration_paypal_payment)
        const nextFollowup = recordOf(nextPaypal.followup)
        shouldContinue = !terminal || countOf(nextFollowup.active) > 0
      } catch (error_: unknown) {
        if (cancelled || controller.signal.aborted || isAbortError(error_)) return
      } finally {
        if (!cancelled && shouldContinue) {
          timer = window.setTimeout(poll, 2000)
        }
      }
    }

    timer = window.setTimeout(poll, 1000)
    return () => {
      cancelled = true
      controller.abort()
      if (timer) window.clearTimeout(timer)
    }
  }, [pageVisible, registrationPaypalTracking, taskId])

  useEffect(() => {
    if (!taskId || !isWebSessionTask || !pageVisible) return
    const controller = new AbortController()
    let cancelled = false
    let timer = 0

    const poll = async () => {
      if (cancelled) return
      setWebSessionLeaseLoading(true)
      try {
        const [, snapshot] = await Promise.all([
          fetchWebSessionLeases(controller.signal),
          apiFetch(`/tasks/${taskId}`, { signal: controller.signal }),
        ]) as [WebSessionLeaseSnapshot[], TaskSnapshot]
        if (!cancelled && snapshot && typeof snapshot === 'object') {
          setTaskSnapshot(snapshot)
          setCurrent(snapshot?.meta?.current && typeof snapshot.meta.current === 'object' ? snapshot.meta.current : null)
          const snapshotStopMode = snapshot?.control?.stop_mode
          setStopMode(
            snapshotStopMode === 'after_current' || snapshotStopMode === 'immediate'
              ? snapshotStopMode
              : snapshot?.control?.stop_requested
                ? 'immediate'
                : snapshot?.control?.stop_after_current_requested || snapshot?.control?.after_current_requested
                  ? 'after_current'
                  : 'none',
          )
        }
      } catch (error_: unknown) {
        if (!cancelled && !controller.signal.aborted && !isAbortError(error_)) {
          const detail = error_ instanceof Error ? error_.message : '浏览器状态刷新失败'
          setWebSessionLeaseError(detail)
        }
      } finally {
        if (!cancelled) {
          setWebSessionLeaseLoading(false)
          timer = window.setTimeout(poll, 2000)
        }
      }
    }

    void poll()
    return () => {
      cancelled = true
      controller.abort()
      if (timer) window.clearTimeout(timer)
    }
  }, [fetchWebSessionLeases, isWebSessionTask, pageVisible, taskId])

  useEffect(() => {
    if (!panelRef.current) return
    panelRef.current.scrollTop = panelRef.current.scrollHeight
  }, [activeLines, registrationRegion])

  useEffect(() => {
    if (!taskId || !['failed', 'partial', 'interrupted'].includes(terminalStatus)) return
    const key = `${taskId}:${terminalStatus}`
    if (terminalNotifyRef.current === key) return
    terminalNotifyRef.current = key
    if (terminalStatus === 'partial') {
      message.warning('任务部分失败，请查看日志里的失败原因')
    } else if (terminalStatus === 'interrupted') {
      message.warning('远端任务中断或结果未知，请查看日志里的失败原因')
    } else {
      message.error('任务失败，请查看日志里的失败原因')
    }
  }, [taskId, terminalStatus])

  const footerText =
    terminalStatus === 'done'
      ? { text: '任务完成', color: '#10b981' }
      : terminalStatus === 'stopped'
        ? { text: '任务已停止', color: '#d97706' }
        : terminalStatus === 'partial'
          ? { text: '部分失败', color: '#d97706' }
          : terminalStatus === 'interrupted'
            ? { text: '远端中断', color: '#d97706' }
        : terminalStatus === 'failed'
          ? { text: '任务失败', color: '#dc2626' }
          : null

  const currentElapsedText = useMemo(() => {
    if (!current?.started_at) return ''
    const started = Date.parse(current.started_at)
    if (!Number.isFinite(started)) return ''
    const diff = Math.max(0, Math.floor((currentNow - started) / 1000))
    const minutes = Math.floor(diff / 60)
    const seconds = diff % 60
    return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
  }, [current?.started_at, currentNow])

  const ideaSubmitSummary = taskSnapshot?.meta?.idea_submit_summary
  const showIdeaSubmitSummary = String(taskSnapshot?.source || taskSnapshot?.meta?.source || '').trim() === 'baxigpt_cdk_submit'
  const registrationDiagnosticsMode = String(taskSnapshot?.meta?.registration_diagnostics?.mode || 'off')
  const showRegistrationDiagnostics = registrationDiagnosticsMode !== 'off'
  const eligibilitySummary = taskSnapshot?.meta?.eligibility_summary || {}
  const bundleEligibility = taskSource.includes('payment_eligibility_bundle')
  const bundleZeroSummary = recordOf((eligibilitySummary as Record<string, unknown>)["zero_amount_eligibility"])
  const bundleMethodsSummary = recordOf((eligibilitySummary as Record<string, unknown>)["payment_methods"])
  const bundleLinkSummary = recordOf((eligibilitySummary as Record<string, unknown>)["checkout_link_type"])
  const eligibilityFailures = paymentEligibilityFailureBreakdown(
    taskSnapshot?.meta?.eligibility_failure_summary,
    taskSnapshot?.meta?.results,
  )
  const showEligibilitySummary = taskSource.includes('zero_amount_eligibility')
    || taskSource.includes('payment_methods')
    || taskSource.includes('gcash_payment_method')
    || taskSource.includes('checkout_link_type')
    || bundleEligibility
  const showGenericTaskControls = showTaskControls && Boolean(taskSnapshot) && !isWebSessionTask
  const activeRegistrationRegionLabel = REGISTRATION_LOG_REGION_LABELS[registrationRegion]
  const emptyLogText = isRegistrationTask
    ? activeRegistrationRegionStatus.label === '未开启'
      ? `本任务未开启${activeRegistrationRegionLabel}`
      : activeRegistrationRegionStatus.label === '未执行'
        ? `${activeRegistrationRegionLabel}未执行`
        : activeLines.length === 0
          ? `等待${activeRegistrationRegionLabel}日志...`
          : `当前 ${effectiveViewMode === 'debug' ? 'Debug' : 'Info'} 视图下没有可显示的日志`
    : lines.length === 0
      ? '等待日志...'
      : `当前 ${effectiveViewMode === 'debug' ? 'Debug' : 'Info'} 视图下没有可显示的日志`

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <div style={{ display: 'flex', justifyContent: showGenericTaskControls ? 'space-between' : 'flex-end', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
        {showGenericTaskControls ? (
          <Space>
            <Button
              size="small"
              icon={<FastForwardOutlined />}
              onClick={handleSkipCurrent}
              loading={skipLoading}
              disabled={interactionLocked}
            >
              跳过当前账号
            </Button>
            {supportsStopAfterCurrent ? (
              <Button
                size="small"
                onClick={() => handleStopTask('after_current')}
                loading={stopLoading && stopMode === 'none'}
                disabled={isFinished || stopMode !== 'none'}
              >
                {rotatingRegistrationGroupId ? '完成当前后停止整组' : '完成当前后停止'}
              </Button>
            ) : null}
            <Button
              size="small"
              danger
              icon={<StopOutlined />}
              onClick={() => handleStopTask('immediate')}
              loading={stopLoading}
              disabled={isFinished || stopMode === 'immediate'}
            >
              {rotatingRegistrationGroupId ? '立即停止整组' : '立即停止'}
            </Button>
          </Space>
        ) : null}
        <Space>
          <Segmented
            size="small"
            value={effectiveViewMode}
            onChange={(value) => setViewMode(value as LogViewMode)}
            options={[
              {
                label: (
                  <Space size={4}>
                    <span>Info</span>
                    <Badge count={infoCount} size="small" style={{ backgroundColor: '#64748b' }} />
                  </Space>
                ),
                value: 'info',
              },
              {
                label: (
                  <Space size={4}>
                    <span>Debug</span>
                    <Badge count={debugCount} size="small" style={{ backgroundColor: '#7c3aed' }} />
                  </Space>
                ),
                value: 'debug',
                disabled: isRegistrationTask && registrationRegion !== 'registration',
              },
            ]}
          />
          <Button size="small" icon={<CopyOutlined />} onClick={handleCopyAll} disabled={visibleLines.length === 0}>
            {isRegistrationTask ? '复制当前区域' : '复制日志'}
          </Button>
        </Space>
      </div>

      {isWebSessionTask ? (
        <div
          style={{
            marginBottom: 8,
            border: `1px solid ${token.colorBorderSecondary}`,
            borderRadius: token.borderRadius,
            background: token.colorFillAlter,
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              minHeight: 44,
              padding: '8px 12px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: 8,
              flexWrap: 'wrap',
              borderBottom: `1px solid ${token.colorBorderSecondary}`,
            }}
          >
            <Space size={[4, 4]} wrap>
              <strong style={{ color: token.colorText }}>{isGcashWebSessionTask ? '登录态 + GCash' : '浏览器登录态'}</strong>
              <Tag color="success">保持 {webSessionLeaseCounts.holding}</Tag>
              <Tag color="processing">运行 {webSessionLeaseCounts.active}</Tag>
              <Tag>已释放 {webSessionLeaseCounts.released}</Tag>
              {webSessionLeaseCounts.failed > 0 ? <Tag color="error">异常 {webSessionLeaseCounts.failed}</Tag> : null}
              {isGcashWebSessionTask ? <Tag color="processing">提链中 {webSessionLeaseCounts.gcashRunning}</Tag> : null}
              {isGcashWebSessionTask ? <Tag color="success">链接成功 {webSessionLeaseCounts.gcashSucceeded}</Tag> : null}
              {isGcashWebSessionTask && webSessionLeaseCounts.gcashFailed > 0 ? <Tag color="error">提链失败 {webSessionLeaseCounts.gcashFailed}</Tag> : null}
              {isGcashWebSessionTask ? <Tag color="cyan">支付页 {webSessionLeaseCounts.gcashTabReady}</Tag> : null}
            </Space>
            {showTaskControls ? (
              <Space size={6} wrap>
                {isBatchWebSessionTask ? (
                  <Button
                    size="small"
                    icon={<PauseCircleOutlined />}
                    onClick={() => handleStopTask('after_current')}
                    loading={stopLoading && stopMode === 'none'}
                    disabled={isFinished || stopMode !== 'none'}
                  >
                    {stopMode === 'after_current' ? '已停止新增' : '停止新增浏览器'}
                  </Button>
                ) : null}
                <Popconfirm
                  title={isBatchWebSessionTask ? '停止并释放全部浏览器？' : '停止并释放浏览器？'}
                  description="先保存当前 Profile，再关闭本地浏览器；不会请求 ChatGPT logout。"
                  okText="停止并释放"
                  cancelText="取消"
                  onConfirm={() => handleWebSessionLeaseAction('release-all')}
                >
                  <Button
                    size="small"
                    danger
                    icon={<PoweroffOutlined />}
                    loading={webSessionLeaseAction === 'release-all'}
                    disabled={isFinished || webSessionLeaseCounts.active === 0 || Boolean(webSessionLeaseAction)}
                  >
                    {isBatchWebSessionTask ? '停止并释放全部' : '停止并释放浏览器'}
                  </Button>
                </Popconfirm>
              </Space>
            ) : null}
          </div>
          {webSessionLeaseError ? (
            <div style={{ padding: '8px 12px 0', color: token.colorErrorText }}>
              浏览器状态刷新失败：{webSessionLeaseError}
            </div>
          ) : null}
          <Table<WebSessionLeaseSnapshot>
            rowKey="lease_id"
            size="small"
            pagination={false}
            dataSource={webSessionLeases}
            loading={webSessionLeaseLoading && webSessionLeases.length === 0}
            scroll={{ x: isGcashWebSessionTask ? 1010 : 760 }}
            locale={{
              emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="等待浏览器租约" />,
            }}
            columns={[
              {
                title: '账号',
                key: 'account',
                width: 230,
                render: (_value, lease) => (
                  <div style={{ minWidth: 0 }}>
                    <div style={{ color: token.colorText, wordBreak: 'break-all' }}>{lease.email || `账号 ${lease.account_id}`}</div>
                    <div style={{ color: token.colorTextSecondary, fontSize: 12 }}>ID {lease.account_id}</div>
                  </div>
                ),
              },
              {
                title: '状态',
                dataIndex: 'status',
                width: 112,
                render: (status: string, lease) => {
                  const view = webSessionLeaseStatusView(status)
                  return (
                    <Tooltip title={lease.error || view.label}>
                      <Tag color={view.color}>{view.label}</Tag>
                    </Tooltip>
                  )
                },
              },
              {
                title: '保持时长',
                dataIndex: 'held_seconds',
                width: 104,
                render: (value: number, lease) => lease.ready_at ? formatHeldDuration(value) : '-',
              },
              {
                title: 'Profile',
                key: 'profile',
                width: 176,
                render: (_value, lease) => (
                  <Tooltip title={lease.profile_path || '浏览器状态尚未落盘'}>
                    <Space size={[4, 4]} wrap>
                      <Tag color={lease.profile_saved ? 'success' : 'default'}>
                        {lease.profile_saved ? '已保存' : '待保存'}
                      </Tag>
                      {lease.restored_profile ? <Tag color="blue">已注入</Tag> : null}
                      {Number(lease.refresh_count || 0) > 0 ? <span style={{ color: token.colorTextSecondary }}>同步 {lease.refresh_count}</span> : null}
                    </Space>
                  </Tooltip>
                ),
              },
              ...(isGcashWebSessionTask ? [
                {
                  title: 'GCash',
                  key: 'gcash',
                  width: 126,
                  render: (_value: unknown, lease: WebSessionLeaseSnapshot) => {
                    const view = webSessionGcashStatusView(String(lease.gcash_state || ''))
                    const detail = [
                      lease.gcash_error,
                      lease.gcash_qr_expires_at ? `二维码到期: ${lease.gcash_qr_expires_at}` : '',
                      lease.gcash_link_expires_at ? `链接到期: ${lease.gcash_link_expires_at}` : '',
                    ].filter(Boolean).join('\n')
                    return (
                      <Tooltip title={detail ? <span style={{ whiteSpace: 'pre-wrap' }}>{detail}</span> : view.label}>
                        <Tag color={view.color}>{view.label}</Tag>
                      </Tooltip>
                    )
                  },
                },
                {
                  title: '支付页',
                  key: 'gcash_tab',
                  width: 116,
                  render: (_value: unknown, lease: WebSessionLeaseSnapshot) => {
                    const view = webSessionGcashTabStatusView(String(lease.gcash_tab_state || ''))
                    const detail = lease.gcash_tab_last_error
                      || (lease.gcash_tab_opened_at ? `打开时间: ${lease.gcash_tab_opened_at}` : '')
                    return (
                      <Tooltip title={detail || view.label}>
                        <Tag color={view.color}>{view.label}</Tag>
                      </Tooltip>
                    )
                  },
                },
              ] : []),
              {
                title: '操作',
                key: 'actions',
                width: 112,
                fixed: 'right',
                render: (_value, lease) => {
                  const accountId = Number(lease.account_id || 0)
                  const refreshKey = `refresh:${accountId}`
                  const releaseKey = `release:${accountId}`
                  const active = ACTIVE_WEB_SESSION_LEASE_STATUSES.has(String(lease.status || ''))
                  return (
                    <Space size={4}>
                      <Tooltip title="同步最新登录态">
                        <Button
                          size="small"
                          aria-label={`同步账号 ${accountId} 最新登录态`}
                          icon={<ReloadOutlined />}
                          loading={webSessionLeaseAction === refreshKey}
                          disabled={lease.status !== 'ready_holding' || Boolean(webSessionLeaseAction)}
                          onClick={() => handleWebSessionLeaseAction('refresh', accountId)}
                        />
                      </Tooltip>
                      <Tooltip title="保存并释放浏览器">
                        <Popconfirm
                          title="保存并释放本地浏览器？"
                          description="不会注销网页会话，保存的账号认证材料继续保留。"
                          okText="释放"
                          cancelText="取消"
                          onConfirm={() => handleWebSessionLeaseAction('release', accountId)}
                        >
                          <Button
                            size="small"
                            danger
                            aria-label={`释放账号 ${accountId} 浏览器`}
                            icon={<PoweroffOutlined />}
                            loading={webSessionLeaseAction === releaseKey}
                            disabled={!active || lease.status === 'releasing' || Boolean(webSessionLeaseAction)}
                          />
                        </Popconfirm>
                      </Tooltip>
                    </Space>
                  )
                },
              },
            ]}
          />
        </div>
      ) : null}

      {current && (current.phase_label || current.email || current.phone || current.account_id) ? (
        <Card
          size="small"
          style={{
            marginBottom: 8,
            borderColor: 'rgba(255, 255, 255, 0.14)',
            background: '#1c1f2e',
          }}
          bodyStyle={{ padding: 12 }}
        >
          <Descriptions
            size="small"
            column={2}
            labelStyle={{ color: '#b0bcd4', fontWeight: 600 }}
            contentStyle={{ color: '#f1f5f9' }}
            items={[
              {
                key: 'current-target',
                label: '当前',
                children: (
                  <Space size={6} wrap>
                    {current.item_index && current.item_total ? (
                      <Tag color="blue">{current.item_index}/{current.item_total}</Tag>
                    ) : null}
                    <span>{current.email || current.phone || (current.account_id ? `账号 ${current.account_id}` : '-')}</span>
                  </Space>
                ),
              },
              {
                key: 'current-phase',
                label: '阶段',
                children: (
                  <Space size={6} wrap>
                    {current.stage_index && current.stage_total ? (
                      <Tag color="processing">{current.stage_index}/{current.stage_total}</Tag>
                    ) : null}
                    <span>{current.phase_label || current.phase || '-'}</span>
                  </Space>
                ),
              },
              {
                key: 'current-elapsed',
                label: '已等待',
                children: currentElapsedText || '-',
              },
              {
                key: 'current-next',
                label: '下一步',
                children: current.next_step || current.last_message || '-',
              },
              {
                key: 'current-touched',
                label: '资源触碰',
                children:
                  typeof current.resource_touched === 'boolean'
                    ? <Tag color={current.resource_touched ? 'warning' : 'default'}>{current.resource_touched ? '已触碰' : '未触碰'}</Tag>
                    : '-',
              },
              {
                key: 'current-message',
                label: '状态',
                children: current.last_message || '-',
              },
            ]}
          />
        </Card>
      ) : null}

      {taskSource === 'chatgpt_email_change' && taskSnapshot?.pending_verification ? (
        <TaskVerificationPanel
          taskId={taskId}
          verification={taskSnapshot.pending_verification as PendingVerificationChallenge}
        />
      ) : null}

      {showIdeaSubmitSummary ? <IdeaSubmitSummary summary={ideaSubmitSummary} /> : null}

      {showEligibilitySummary ? (
        <Card size="small" style={{ marginBottom: 8 }}>
          <Space size={6} wrap>
            {bundleEligibility ? (
              <>
                <Tag color="success">0 元有资格 {Number(bundleZeroSummary.eligible || 0)}</Tag>
                <Tag color="warning">非 0 元 {Number(bundleZeroSummary.ineligible || 0)}</Tag>
                <Tag color="success">支付方式可用 {Number(bundleMethodsSummary.available || 0)}</Tag>
                <Tag color="warning">无可用方式 {Number(bundleMethodsSummary.no_methods || 0)}</Tag>
                <Tag color="blue">OAICS {Number(bundleLinkSummary.oaics || 0)}</Tag>
                <Tag color="purple">Stripe (CS) {Number(bundleLinkSummary.cs || 0)}</Tag>
                <Tag color="error">检测失败 {Number(bundleZeroSummary.probe_failed || 0) + Number(bundleMethodsSummary.probe_failed || 0) + Number(bundleLinkSummary.probe_failed || 0)}</Tag>
                <Tag>跳过 {Number(bundleZeroSummary.skipped || 0) + Number(bundleMethodsSummary.skipped || 0) + Number(bundleLinkSummary.skipped || 0)}</Tag>
              </>
            ) : taskSource.includes('zero_amount') ? (
              <>
                <Tag color="success">0 元资格 {Number(eligibilitySummary.eligible || 0)}</Tag>
                <Tag color="warning">非 0 元 {Number(eligibilitySummary.ineligible || 0)}</Tag>
              </>
            ) : taskSource.includes('payment_methods') ? (
              <>
                <Tag color="success">有可用方式 {Number(eligibilitySummary.available || 0)}</Tag>
                <Tag color="warning">无可用方式 {Number(eligibilitySummary.no_methods || eligibilitySummary.unavailable || 0)}</Tag>
              </>
            ) : taskSource.includes('checkout_link_type') ? (
              <>
                <Tag color="blue">OAICS {Number(eligibilitySummary.oaics || 0)}</Tag>
                <Tag color="purple">Stripe (CS) {Number(eligibilitySummary.cs || 0)}</Tag>
              </>
            ) : (
              <>
                <Tag color="success">GCash 可用 {Number(eligibilitySummary.available || 0)}</Tag>
                <Tag color="warning">GCash 不可用 {Number(eligibilitySummary.unavailable || 0)}</Tag>
              </>
            )}
            <Tag color="error">检测失败 {Number(eligibilitySummary.probe_failed || 0)}</Tag>
            {eligibilityFailures.map((item) => (
              <Tag key={item.category} color={item.color}>{item.label} {item.count}</Tag>
            ))}
            <Tag>跳过 {Number(eligibilitySummary.skipped || 0)}</Tag>
          </Space>
        </Card>
      ) : null}

      {showRegistrationDiagnostics ? (
        <RegistrationDiagnosticsPanel
          taskId={taskId}
          mode={registrationDiagnosticsMode}
          active={!isFinished}
        />
      ) : null}

      {!isRegistrationTask && paymentEvents.length > 0 ? (
        <Card size="small" title="PayPal 自动支付时间线" style={{ marginBottom: 8 }}>
          <Timeline
            items={paymentEvents.slice(-80).map((event, index) => ({
              key: `${event.id || index}-${event.stage || 'payment'}`,
              color: String(event.level || '').toLowerCase() === 'warning' ? 'orange' : 'blue',
              children: (
                <Space size={[6, 2]} wrap>
                  <Tag>{String(event.stage || 'payment')}</Tag>
                  {event.created_at ? <span style={{ color: token.colorTextSecondary }}>{event.created_at}</span> : null}
                  {event.account ? <span style={{ color: token.colorTextSecondary }}>{event.account}</span> : null}
                  <span>{String(event.message || '')}</span>
                </Space>
              ),
            }))}
          />
        </Card>
      ) : null}

      {isRegistrationTask ? (
        <RegistrationTaskLogTabs
          activeRegion={registrationRegion}
          counts={registrationRegionCounts}
          status={activeRegistrationRegionStatus}
          onChange={setRegistrationRegion}
        />
      ) : null}

      <div
        ref={panelRef}
        className={`log-panel${isRegistrationTask ? ' registration-task-log-panel' : ''}`}
        aria-label={isRegistrationTask ? `${activeRegistrationRegionLabel}日志` : '任务日志'}
        style={{
          flex: 1,
          overflowY: 'auto',
          overflowX: 'auto',
          background: token.colorBgContainer,
          border: `1px solid ${token.colorBorderSecondary}`,
          borderRadius: 8,
          padding: 12,
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
          fontSize: 12,
          color: token.colorText,
          minHeight: 240,
          maxHeight: 'calc(100vh - 320px)',
          userSelect: 'text',
          WebkitUserSelect: 'text',
          cursor: 'text',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        {visibleLines.length === 0 && !error && (
          <div style={{ color: token.colorTextTertiary }}>
            {emptyLogText}
          </div>
        )}
        {error && <div style={{ color: '#dc2626' }}>{error}</div>}
        {groupedVisibleLines.map((line, index) => {
          return (
            <div
              key={`${index}-${line.raw}`}
              style={{
                lineHeight: 1.65,
                minHeight: line.raw === '' ? '1.65em' : undefined,
                margin: line.accountGap ? (line.isDebug ? '14px 0 2px' : '14px 0 0') : line.isDebug ? '2px 0' : 0,
                padding: line.isDebug ? '2px 8px' : 0,
                border: line.isDebug ? `1px solid ${token.colorPrimaryBorder}` : '1px solid transparent',
                borderRadius: line.isDebug ? 4 : 0,
                background: line.isDebug ? token.colorPrimaryBg : 'transparent',
                color: line.isDebug
                  ? token.colorPrimaryText
                  : line.text.includes('✓') || line.text.includes('成功')
                    ? token.colorSuccessText
                    : line.text.includes('✗') || line.text.includes('失败') || line.text.includes('错误')
                      ? token.colorErrorText
                      : line.text.includes('停止') || line.text.includes('跳过') || line.text.includes('未知') || line.text.includes('超时') || line.text.includes('待确认')
                        ? token.colorWarningText
                        : token.colorText,
              }}
            >
              {line.raw}
            </div>
          )
        })}
      </div>

      {footerText ? (
        <div style={{ fontSize: 12, color: footerText.color, marginTop: 8 }}>
          {footerText.text}
        </div>
      ) : null}
    </div>
  )
}

export default TaskLogPanel
