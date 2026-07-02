import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Card, Checkbox, Descriptions, Drawer, Empty, Grid, Input, message, Pagination, Popconfirm, Progress, Select, Space, Table, Tag, theme, Tooltip, Typography } from 'antd'
import {
  BugOutlined,
  CopyOutlined,
  DeleteOutlined,
  EyeInvisibleOutlined,
  EyeOutlined,
  ReloadOutlined,
  StopOutlined,
  UndoOutlined,
  UploadOutlined,
  SyncOutlined,
} from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'

type CdkPoolItem = {
  id: number
  code_value?: string
  code_masked: string
  label: string
  status: string
  available: boolean
  bound_account_id: number
  bound_account_email: string
  bound_at: string | null
  task_id: string
  order_id: string
  display_id: string
  remote_email: string
  upstream_status: string
  code_info_remaining: number
  code_info_total: number
  last_error_code: string
  last_error_message: string
  last_query_response?: {
    remaining?: number
    total?: number
    used?: number
    status_code?: string
    orders?: Array<Record<string, any>>
  }
  submitted_at: string | null
  paid_at: string | null
  last_checked_at: string | null
  created_at: string | null
  updated_at: string | null
}

type CdkPoolSummary = {
  total?: number
  available?: number
  reserved?: number
  submitted?: number
  processing?: number
  paid?: number
  failed?: number
  disabled?: number
}

type BatchAction = 'delete' | 'reset' | 'disable' | 'status' | 'poll' | 'quota'

type BaxiGptPollerTarget = {
  record_id?: number
  id?: number
  cdk_id?: number
  task_id?: string
  source?: string
  status?: string
  last_status?: string
  error?: string
  last_error?: string
  message?: string
  next_due_at?: number
  deadline_at?: number
  timeout_seconds?: number
  interval_seconds?: number
  [key: string]: any
}

type BaxiGptPollerSnapshot = {
  running?: boolean
  queued?: number
  ids?: number[]
  targets?: BaxiGptPollerTarget[] | Record<string, BaxiGptPollerTarget>
}

type ImportQueryJob = {
  id: string
  status: string
  total: number
  progress: number
  success: number
  failed: number
  logs?: string[]
  error?: string
}

type BaxiGptDiagnostics = {
  ok?: boolean
  item?: CdkPoolItem
  quota?: Record<string, any>
  query_orders?: Array<Record<string, any>>
  query_response?: Record<string, any>
  submit_response?: Record<string, any>
  last_status_response?: Record<string, any>
  poller?: BaxiGptPollerSnapshot
  poller_target?: BaxiGptPollerTarget | null
  bound_account?: Record<string, any> | null
  task_logs?: Array<Record<string, any>>
  notes?: Array<{ level?: string; message?: string }>
}

const STATUS_META: Record<string, { color: string; label: string }> = {
  available: { color: 'success', label: '可用' },
  reserved: { color: 'warning', label: '已绑定' },
  submitted: { color: 'processing', label: '已提交' },
  processing: { color: 'processing', label: '处理中' },
  paid: { color: 'success', label: '已成功' },
  failed: { color: 'error', label: '失败' },
  disabled: { color: 'default', label: '已停用' },
  pending: { color: 'default', label: '等待中' },
  running: { color: 'processing', label: '查询中' },
  done: { color: 'success', label: '已完成' },
  stopped: { color: 'default', label: '已停止' },
}

const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'available', label: '可用' },
  { value: 'reserved', label: '已绑定' },
  { value: 'submitted', label: '已提交' },
  { value: 'processing', label: '处理中' },
  { value: 'paid', label: '已成功' },
  { value: 'failed', label: '失败' },
  { value: 'disabled', label: '已停用' },
]

const PAGE_SIZE = 10

const BEIJING_TIME_FORMATTER = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
})

function formatBeijingTime(value?: string | null) {
  const text = String(value || '').trim()
  if (!text) return '-'
  const normalized = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(text)
    ? `${text.replace(' ', 'T')}Z`
    : text
  const date = new Date(normalized)
  if (Number.isNaN(date.getTime())) return text
  const parts = BEIJING_TIME_FORMATTER.formatToParts(date)
  const read = (type: Intl.DateTimeFormatPartTypes) => parts.find((part) => part.type === type)?.value || ''
  return `${read('year')}-${read('month')}-${read('day')} ${read('hour')}:${read('minute')}:${read('second')}`
}

function statusTag(status: string, tooltip?: string) {
  const meta = STATUS_META[status] || { color: 'default', label: status || '未知' }
  const tag = <Tag color={meta.color}>{meta.label}</Tag>
  return tooltip ? <Tooltip title={tooltip}>{tag}</Tooltip> : tag
}

function queryOrders(record: CdkPoolItem) {
  const orders = record.last_query_response?.orders
  return Array.isArray(orders) ? orders.filter((order) => order && typeof order === 'object') : []
}


const TERMINAL_CDK_STATUSES = new Set(['paid', 'failed', 'disabled'])

function isTerminalCdkStatus(status?: string) {
  return TERMINAL_CDK_STATUSES.has(String(status || '').toLowerCase())
}

function isManualWatchableCdk(item?: Partial<CdkPoolItem> | null) {
  const status = String(item?.status || '').toLowerCase()
  return Boolean(String(item?.order_id || '').trim()) && status !== 'available' && !isTerminalCdkStatus(status)
}

function normalizeIdList(value: any): number[] {
  if (!Array.isArray(value)) return []
  const seen = new Set<number>()
  const ids: number[] = []
  value.forEach((raw) => {
    const id = Number(raw)
    if (Number.isFinite(id) && id > 0 && !seen.has(id)) {
      seen.add(id)
      ids.push(id)
    }
  })
  return ids
}

function normalizePollerSnapshot(value: any): BaxiGptPollerSnapshot {
  if (!value || typeof value !== 'object') return {}
  return {
    running: Boolean(value.running),
    queued: Number(value.queued || 0),
    ids: normalizeIdList(value.ids),
    targets: value.targets,
  }
}

function pollerTargetEntries(poller?: BaxiGptPollerSnapshot): Array<[number, BaxiGptPollerTarget]> {
  const targets = poller?.targets
  if (Array.isArray(targets)) {
    return targets
      .map((target) => {
        const id = Number(target?.record_id ?? target?.id ?? target?.cdk_id ?? 0)
        return [id, target] as [number, BaxiGptPollerTarget]
      })
      .filter(([id]) => Number.isFinite(id) && id > 0)
  }
  if (targets && typeof targets === 'object') {
    return Object.entries(targets)
      .map(([key, target]) => {
        const id = Number((target as BaxiGptPollerTarget)?.record_id ?? (target as BaxiGptPollerTarget)?.id ?? (target as BaxiGptPollerTarget)?.cdk_id ?? key)
        return [id, target as BaxiGptPollerTarget] as [number, BaxiGptPollerTarget]
      })
      .filter(([id]) => Number.isFinite(id) && id > 0)
  }
  return []
}

function pollerIds(poller?: BaxiGptPollerSnapshot) {
  const ids = new Set<number>(normalizeIdList(poller?.ids))
  pollerTargetEntries(poller).forEach(([id]) => ids.add(id))
  return Array.from(ids)
}

function getPollerTarget(poller: BaxiGptPollerSnapshot | undefined, id: number) {
  return pollerTargetEntries(poller).find(([targetId]) => Number(targetId) === Number(id))?.[1]
}

function rowFromActionResult(row: any): CdkPoolItem | null {
  const item = row?.item && typeof row.item === 'object' ? row.item : row
  const id = Number(item?.id || 0)
  const looksLikeCdkRow = item && typeof item === 'object' && (item.status !== undefined || item.code_masked !== undefined || item.code_value !== undefined)
  return Number.isFinite(id) && id > 0 && looksLikeCdkRow ? item as CdkPoolItem : null
}

function formatRemainingSeconds(deadlineAt?: number) {
  const deadline = Number(deadlineAt || 0)
  if (!Number.isFinite(deadline) || deadline <= 0) return ''
  const seconds = Math.max(0, Math.round(deadline - Date.now() / 1000))
  if (seconds <= 0) return '已到超时点'
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return minutes > 0 ? `剩余约 ${minutes}分${rest ? `${rest}秒` : ''}` : `剩余约 ${rest}秒`
}

function pollerSourceLabel(source?: string) {
  switch (String(source || '').trim()) {
    case 'manual_poll':
      return '手动轮询'
    case 'restart_restore':
      return '重启恢复'
    case 'submit_success':
      return '提交后轮询'
    default:
      return String(source || '').trim() || '后台轮询'
  }
}

function pollerTargetTooltip(target?: BaxiGptPollerTarget) {
  if (!target) return ''
  const parts = [
    `来源：${pollerSourceLabel(target.source)}`,
    target.task_id ? `任务：${target.task_id}` : '',
    target.last_status ? `最近状态：${target.last_status}` : '',
    target.interval_seconds ? `间隔：${target.interval_seconds}s` : '',
    target.timeout_seconds ? `超时：${target.timeout_seconds}s` : '',
    target.deadline_at ? `剩余：${formatRemainingSeconds(Number(target.deadline_at || 0))}` : '',
    target.last_error ? `错误：${target.last_error}` : '',
  ].filter(Boolean)
  return parts.join('\n')
}

function isEmptyValue(value: any) {
  if (value == null) return true
  if (Array.isArray(value)) return value.length === 0
  if (typeof value === 'object') return Object.keys(value).length === 0
  return String(value || '').trim() === ''
}

function JsonBlock({ value, maxHeight = 180 }: { value: any; maxHeight?: number }) {
  if (isEmptyValue(value)) return <Typography.Text type="secondary">-</Typography.Text>
  return (
    <pre
      style={{
        margin: 0,
        maxHeight,
        overflow: 'auto',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        fontSize: 12,
        lineHeight: 1.55,
      }}
    >
      {typeof value === 'string' ? value : JSON.stringify(value, null, 2)}
    </pre>
  )
}

function noteColor(level?: string) {
  switch (String(level || '').toLowerCase()) {
    case 'success':
      return 'success'
    case 'error':
      return 'error'
    case 'warning':
      return 'warning'
    default:
      return 'info'
  }
}

function orderStatusColor(status?: string) {
  const normalized = String(status || '').toLowerCase()
  if (['paid', 'success', 'completed'].includes(normalized)) return 'success'
  if (['processing', 'pending', 'submitted'].includes(normalized)) return 'processing'
  if (['failed', 'expired', 'cancelled', 'canceled', 'invalid', 'used'].includes(normalized)) return 'error'
  return 'default'
}

function quotaInfo(record: CdkPoolItem) {
  const query = record.last_query_response && typeof record.last_query_response === 'object' ? record.last_query_response : {}
  const remaining = Number(query.remaining ?? record.code_info_remaining ?? 0)
  const total = Number(query.total ?? record.code_info_total ?? 0)
  const used = Number(query.used ?? Math.max(total - remaining, 0))
  return { remaining, total, used }
}

export default function BaxiGptCdkPool() {
  const { token } = theme.useToken()
  const screens = Grid.useBreakpoint()
  const isMobile = screens.lg === false
  const [items, setItems] = useState<CdkPoolItem[]>([])
  const [summary, setSummary] = useState<CdkPoolSummary>({})
  const [statusFilter, setStatusFilter] = useState('')
  const [search, setSearch] = useState('')
  const [currentPage, setCurrentPage] = useState(1)
  const [selectedRowKeys, setSelectedRowKeys] = useState<number[]>([])
  const [loading, setLoading] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [importText, setImportText] = useState('')
  const [importing, setImporting] = useState(false)
  const [batchAction, setBatchAction] = useState<BatchAction | ''>('')
  const [importQueryJob, setImportQueryJob] = useState<ImportQueryJob | null>(null)
  const [showPlainCodes, setShowPlainCodes] = useState(true)
  const [manualWatchingIds, setManualWatchingIds] = useState<number[]>([])
  const [pollerSnapshot, setPollerSnapshot] = useState<BaxiGptPollerSnapshot>({})
  const [diagnosticOpen, setDiagnosticOpen] = useState(false)
  const [diagnosticLoading, setDiagnosticLoading] = useState(false)
  const [diagnostic, setDiagnostic] = useState<BaxiGptDiagnostics | null>(null)
  const snapshotPullingRef = useRef(false)
  const lastSummaryRefreshRef = useRef(0)

  const patchRows = useCallback((rows: any[]) => {
    const nextRows = rows.map(rowFromActionResult).filter(Boolean) as CdkPoolItem[]
    if (nextRows.length === 0) return
    const patchById = new Map(nextRows.map((item) => [Number(item.id), item]))
    setItems((prev) => prev.map((item) => patchById.get(Number(item.id)) || item))
    const terminalIds = nextRows
      .filter((item) => isTerminalCdkStatus(item.status))
      .map((item) => Number(item.id))
    if (terminalIds.length > 0) {
      const terminalSet = new Set(terminalIds)
      setManualWatchingIds((prev) => prev.filter((id) => !terminalSet.has(Number(id))))
    }
  }, [])

  const refreshSummary = useCallback(async () => {
    try {
      const data = await apiFetch('/baxigpt-cdk-pool/summary')
      const nextSummary = data?.summary && typeof data.summary === 'object' ? data.summary : {}
      setSummary((prev) => ({
        ...prev,
        ...nextSummary,
        ...(data?.available !== undefined && nextSummary.available === undefined ? { available: Number(data.available || 0) } : {}),
      }))
      lastSummaryRefreshRef.current = Date.now()
    } catch {
      // 兼容后端暂未提供 /summary 的过渡期：保持现有 summary，不退回全量 load 轮询。
    }
  }, [])

  const loadDiagnostics = useCallback(async (id: number) => {
    const recordId = Number(id || 0)
    if (!Number.isFinite(recordId) || recordId <= 0) return null
    setDiagnosticLoading(true)
    try {
      const data = await apiFetch(`/baxigpt-cdk-pool/${recordId}/diagnostics`)
      setDiagnostic(data)
      if (data?.item) patchRows([data.item])
      if (data?.poller && typeof data.poller === 'object') {
        setPollerSnapshot(normalizePollerSnapshot(data.poller))
      }
      return data
    } catch (e: any) {
      message.error(e?.message || '读取诊断信息失败')
      return null
    } finally {
      setDiagnosticLoading(false)
    }
  }, [patchRows])

  const openDiagnostics = useCallback((record: CdkPoolItem) => {
    const recordId = Number(record?.id || 0)
    if (!Number.isFinite(recordId) || recordId <= 0) return
    setDiagnosticOpen(true)
    setDiagnostic({
      item: record,
      quota: {
        ...quotaInfo(record),
        status_code: record.last_query_response?.status_code || record.upstream_status,
        last_checked_at: record.last_checked_at,
        source: record.last_query_response ? 'local_row' : 'local_fields',
      },
      query_orders: queryOrders(record),
      query_response: record.last_query_response || {},
      submit_response: (record as any).submit_response || {},
      last_status_response: (record as any).last_status_response || {},
      poller: pollerSnapshot,
      poller_target: getPollerTarget(pollerSnapshot, recordId) || null,
    })
    void loadDiagnostics(recordId)
  }, [loadDiagnostics, pollerSnapshot])

  const load = useCallback(async (nextStatus = statusFilter) => {
    setLoading(true)
    try {
      const query = new URLSearchParams()
      if (nextStatus) query.set('status', nextStatus)
      if (search.trim()) query.set('search', search.trim())
      const data = await apiFetch(`/baxigpt-cdk-pool${query.toString() ? `?${query.toString()}` : ''}`)
      const nextItems = Array.isArray(data?.items) ? data.items : []
      setItems(nextItems)
      setSelectedRowKeys((prev) => {
        const ids = new Set(nextItems.map((item: CdkPoolItem) => Number(item.id)))
        return prev.filter((key) => ids.has(Number(key)))
      })
      setSummary(data?.summary && typeof data.summary === 'object' ? data.summary : {})
      if (data?.poller && typeof data.poller === 'object') setPollerSnapshot(normalizePollerSnapshot(data.poller))
    } finally {
      setLoading(false)
    }
  }, [statusFilter, search])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    void refreshSummary()
    const timer = window.setInterval(() => {
      void refreshSummary()
    }, 12000)
    return () => window.clearInterval(timer)
  }, [refreshSummary])

  useEffect(() => {
    setCurrentPage(1)
    setSelectedRowKeys([])
  }, [search, statusFilter])

  useEffect(() => {
    if (!importQueryJob?.id) return undefined
    const status = String(importQueryJob.status || '')
    if (!['pending', 'running'].includes(status)) return undefined
    let cancelled = false
    const pull = async () => {
      try {
        const nextJob = await apiFetch(`/baxigpt-cdk-pool/import-jobs/${encodeURIComponent(importQueryJob.id)}`)
        if (cancelled) return
        setImportQueryJob(nextJob)
        if (!['pending', 'running'].includes(String(nextJob?.status || ''))) {
          await load()
        }
      } catch (e: any) {
        if (!cancelled) message.warning(e?.message || '入库查询任务状态读取失败')
      }
    }
    void pull()
    const timer = window.setInterval(() => {
      void pull()
    }, 1500)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [importQueryJob?.id, importQueryJob?.status, load])

  const copyToClipboard = async (text: string, successText = '已复制') => {
    const value = String(text || '').trim()
    if (!value) return
    try {
      await navigator.clipboard.writeText(value)
      message.success(successText)
    } catch {
      message.error('复制失败，请手动复制')
    }
  }

  const importCodes = async () => {
    if (!importText.trim()) return
    setImporting(true)
    try {
      const result = await apiFetch('/baxigpt-cdk-pool/import', {
        method: 'POST',
        body: JSON.stringify({ text: importText }),
      })
      const errors = Array.isArray(result?.errors) ? result.errors.length : 0
      const job = result?.query_job && typeof result.query_job === 'object' ? result.query_job : null
      const jobTotal = Number(job?.total || 0)
      message.success(
        `导入完成：新增 ${Number(result?.added || 0)}，更新 ${Number(result?.updated || 0)}，跳过 ${Number(result?.skipped || 0)}，错误 ${errors}`
        + (jobTotal > 0 ? `，已开始后台入库查询 ${jobTotal} 个` : ''),
      )
      if (job) setImportQueryJob(job)
      setImportText('')
      setImportOpen(false)
      await load()
    } catch (e: any) {
      message.error(e?.message || '导入失败')
    } finally {
      setImporting(false)
    }
  }

  const reset = async (id: number) => {
    await apiFetch(`/baxigpt-cdk-pool/${id}/reset`, { method: 'POST' })
    message.success('已恢复可用')
    await load()
  }

  const disable = async (id: number) => {
    await apiFetch(`/baxigpt-cdk-pool/${id}/disable`, { method: 'POST' })
    message.success('已停用')
    await load()
  }

  const del = async (id: number) => {
    await apiFetch(`/baxigpt-cdk-pool/${id}`, { method: 'DELETE' })
    message.success('已删除')
    await load()
  }

  const runBatchAction = async (action: BatchAction) => {
    const ids = selectedRowKeys.map((key) => Number(key)).filter((id) => Number.isFinite(id))
    if (ids.length === 0) return
    const label = action === 'delete'
      ? '删除'
      : action === 'reset'
        ? '恢复'
        : action === 'disable'
          ? '停用'
          : action === 'poll'
            ? '轮询状态'
            : action === 'quota'
              ? '校验配额'
              : '查询状态'
    setBatchAction(action)
    try {
      if (action === 'status') {
        const result = await apiFetch('/baxigpt-cdk-pool/status', {
          method: 'POST',
          body: JSON.stringify({ ids }),
        })
        const rows = Array.isArray(result?.items) ? result.items : []
        patchRows(rows)
        const ok = rows.filter((item: any) => item?.ok).length
        message.success(`状态查询完成：成功 ${ok} 个，失败 ${rows.length - ok} 个`)
        void refreshSummary()
      } else if (action === 'poll') {
        const result = await apiFetch('/baxigpt-cdk-pool/poll', {
          method: 'POST',
          body: JSON.stringify({ ids, interval_seconds: 5, timeout_seconds: 300 }),
        })
        const queued = Number(result?.queued || 0)
        const skipped = Array.isArray(result?.skipped) ? result.skipped.length : 0
        const nextPoller = result?.poller && typeof result.poller === 'object' ? normalizePollerSnapshot(result.poller) : null
        const queuedIds = new Set(pollerIds(nextPoller || undefined))
        if (result?.poller && typeof result.poller === 'object') {
          setPollerSnapshot(nextPoller || {})
        }
        if (queued > 0) {
          setManualWatchingIds((prev) => Array.from(new Set([...prev, ...ids.filter((id) => queuedIds.has(Number(id)))])))
          message.success(`已加入后台轮询：${queued} 个${skipped ? `，跳过 ${skipped} 个` : ''}`)
        }
        else message.warning(`没有可轮询的订单${skipped ? `，跳过 ${skipped} 个` : ''}`)
      } else if (action === 'quota') {
        const result = await apiFetch('/baxigpt-cdk-pool/quota', {
          method: 'POST',
          body: JSON.stringify({ ids, include_query: true }),
        })
        const rows = Array.isArray(result?.items) ? result.items : []
        patchRows(rows)
        const ok = rows.filter((item: any) => item?.ok).length
        const available = rows.filter((item: any) => String(item?.item?.status || '') === 'available').length
        const paid = rows.filter((item: any) => String(item?.item?.status || '') === 'paid').length
        const failed = rows.filter((item: any) => String(item?.item?.status || '') === 'failed').length
        message.success(`配额校验完成：成功 ${ok} 个，可用 ${available} 个，已成功 ${paid} 个，失败/耗尽 ${failed} 个`)
        void refreshSummary()
      } else {
        const results = await Promise.allSettled(ids.map((id) => {
          if (action === 'delete') return apiFetch(`/baxigpt-cdk-pool/${id}`, { method: 'DELETE' })
          if (action === 'reset') return apiFetch(`/baxigpt-cdk-pool/${id}/reset`, { method: 'POST' })
          return apiFetch(`/baxigpt-cdk-pool/${id}/disable`, { method: 'POST' })
        }))
        const successCount = results.filter((result) => result.status === 'fulfilled').length
        const failedCount = results.length - successCount
        if (successCount > 0) message.success(`批量${label}完成：成功 ${successCount} 个${failedCount ? `，失败 ${failedCount} 个` : ''}`)
        if (failedCount > 0) message.warning(`${failedCount} 个卡密${label}失败，请刷新后重试`)
      }
      if (['delete', 'reset', 'disable'].includes(action)) {
        setSelectedRowKeys([])
        await load()
      }
    } finally {
      setBatchAction('')
    }
  }

  const filteredItems = useMemo(() => items, [items])
  const selectedCodeValues = useMemo(() => {
    const idSet = new Set(selectedRowKeys.map((key) => Number(key)))
    return items
      .filter((item) => idSet.has(Number(item.id)))
      .map((item) => String(item.code_value || item.code_masked || '').trim())
      .filter(Boolean)
  }, [items, selectedRowKeys])
  const paginatedItems = useMemo(() => {
    const start = (currentPage - 1) * PAGE_SIZE
    return filteredItems.slice(start, start + PAGE_SIZE)
  }, [currentPage, filteredItems])

  const backendWatchingIds = useMemo(() => pollerIds(pollerSnapshot), [pollerSnapshot])
  const watchingCdkIds = useMemo(() => {
    const ids = new Set<number>()
    const currentPageIds = new Set(paginatedItems.map((item) => Number(item.id)).filter((id) => Number.isFinite(id) && id > 0))
    manualWatchingIds.forEach((id) => {
      if (currentPageIds.has(Number(id))) ids.add(Number(id))
    })
    backendWatchingIds.forEach((id) => {
      if (currentPageIds.has(Number(id))) ids.add(Number(id))
    })
    return Array.from(ids).filter((id) => Number.isFinite(id) && id > 0)
  }, [backendWatchingIds, manualWatchingIds, paginatedItems])
  const watchingCdkIdsKey = useMemo(() => watchingCdkIds.slice().sort((a, b) => a - b).join(','), [watchingCdkIds])

  useEffect(() => {
    const maxPage = Math.max(1, Math.ceil(filteredItems.length / PAGE_SIZE))
    if (currentPage > maxPage) setCurrentPage(maxPage)
  }, [currentPage, filteredItems.length])

  useEffect(() => {
    if (manualWatchingIds.length === 0) return
    const itemById = new Map(items.map((item) => [Number(item.id), item]))
    setManualWatchingIds((prev) => prev.filter((id) => {
      const item = itemById.get(Number(id))
      return item ? isManualWatchableCdk(item) : true
    }))
  }, [items, manualWatchingIds.length])

  useEffect(() => {
    const ids = watchingCdkIdsKey
      ? watchingCdkIdsKey.split(',').map((id) => Number(id)).filter((id) => Number.isFinite(id) && id > 0)
      : []
    if (ids.length === 0) return undefined
    let cancelled = false
    const pullSnapshot = async () => {
      if (snapshotPullingRef.current) return
      snapshotPullingRef.current = true
      try {
        const data = await apiFetch('/baxigpt-cdk-pool/snapshot', {
          method: 'POST',
          body: JSON.stringify({ ids }),
        })
        if (cancelled) return
        const rows = Array.isArray(data?.items) ? data.items : []
        patchRows(rows)
        const returnedIds = new Set(
          rows
            .map(rowFromActionResult)
            .filter((item: CdkPoolItem | null): item is CdkPoolItem => Boolean(item))
            .map((item: CdkPoolItem) => Number(item.id)),
        )
        setManualWatchingIds((prev) => prev.filter((id) => !ids.includes(Number(id)) || returnedIds.has(Number(id))))
        if (data?.poller && typeof data.poller === 'object') {
          const nextPoller = normalizePollerSnapshot(data.poller)
          const activePollerIds = new Set(pollerIds(nextPoller))
          setPollerSnapshot(nextPoller)
          setManualWatchingIds((prev) => prev.filter((id) => activePollerIds.has(Number(id))))
        }
        if (Date.now() - lastSummaryRefreshRef.current > 10000) {
          void refreshSummary()
        }
      } catch {
        // snapshot 是优化接口；旧后端未提供时不要触发整页刷新或刷屏报错。
      } finally {
        snapshotPullingRef.current = false
      }
    }
    void pullSnapshot()
    const timer = window.setInterval(() => {
      void pullSnapshot()
    }, 4000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [patchRows, refreshSummary, watchingCdkIdsKey])

  const availableCount = Number(summary.available || 0)
  const submittedCount = Number(summary.submitted || 0) + Number(summary.processing || 0)
  const paidCount = Number(summary.paid || 0)
  const failedCount = Number(summary.failed || 0)
  const disabledCount = Number(summary.disabled || 0)
  const summaryItems = [
    { label: '全部', value: Number(summary.total || 0), detail: `当前显示 ${filteredItems.length}`, color: token.colorText },
    { label: '可用', value: availableCount, detail: '可分配提交', color: token.colorSuccess },
    { label: '已提交', value: submittedCount, detail: '等待状态查询', color: token.colorInfo },
    { label: '已成功', value: paidCount, detail: '上游 paid', color: token.colorSuccess },
    { label: '失败', value: failedCount, detail: '需人工处理', color: token.colorError },
    { label: '停用', value: disabledCount, detail: '不参与提交', color: token.colorTextSecondary },
  ]

  const queryOneStatus = async (id: number) => {
    setBatchAction('status')
    try {
      const result = await apiFetch('/baxigpt-cdk-pool/status', {
        method: 'POST',
        body: JSON.stringify({ ids: [id] }),
      })
      const rows = Array.isArray(result?.items) ? result.items : []
      patchRows(rows)
      const ok = rows.some((item: any) => item?.ok)
      if (ok) message.success('状态查询完成')
      else message.warning('状态查询失败')
      void refreshSummary()
    } finally {
      setBatchAction('')
    }
  }

  const pollOneStatus = async (id: number) => {
    setBatchAction('poll')
    try {
      const result = await apiFetch('/baxigpt-cdk-pool/poll', {
        method: 'POST',
        body: JSON.stringify({ ids: [id], interval_seconds: 5, timeout_seconds: 300 }),
      })
      const queued = Number(result?.queued || 0)
      const nextPoller = result?.poller && typeof result.poller === 'object' ? normalizePollerSnapshot(result.poller) : null
      if (result?.poller && typeof result.poller === 'object') {
        setPollerSnapshot(nextPoller || {})
      }
      if (queued > 0) {
        if (pollerIds(nextPoller || undefined).includes(Number(id))) {
          setManualWatchingIds((prev) => Array.from(new Set([...prev, Number(id)])))
        }
        message.success('已加入后台轮询')
      }
      else message.warning('该订单当前不可轮询')
    } finally {
      setBatchAction('')
    }
  }

  const checkOneQuota = async (id: number) => {
    setBatchAction('quota')
    try {
      const result = await apiFetch('/baxigpt-cdk-pool/quota', {
        method: 'POST',
        body: JSON.stringify({ ids: [id], include_query: true }),
      })
      const rows = Array.isArray(result?.items) ? result.items : []
      const row = rows[0] || null
      patchRows(rows)
      const status = String(row?.item?.status || '')
      if (row?.ok) {
        message.success(`配额校验完成：${STATUS_META[status]?.label || status || '已更新'}`)
      } else {
        message.warning(row?.message || '配额校验失败')
      }
      void refreshSummary()
    } finally {
      setBatchAction('')
    }
  }

  const diagnosticItem = diagnostic?.item || null
  const diagnosticRecordId = Number(diagnosticItem?.id || 0)
  const diagnosticPollTarget = diagnostic?.poller_target || (diagnosticRecordId ? getPollerTarget(pollerSnapshot, diagnosticRecordId) : null)
  const diagnosticQuota: Record<string, any> = diagnostic?.quota || (diagnosticItem ? quotaInfo(diagnosticItem) : {})
  const diagnosticOrders = Array.isArray(diagnostic?.query_orders) ? diagnostic.query_orders : []
  const diagnosticAccount = diagnostic?.bound_account && typeof diagnostic.bound_account === 'object' ? diagnostic.bound_account : null
  const diagnosticNotes = Array.isArray(diagnostic?.notes) ? diagnostic.notes : []

  const refreshDiagnosticAfter = async (action: 'quota' | 'status' | 'poll' | 'local') => {
    if (!diagnosticRecordId) return
    if (action === 'quota') await checkOneQuota(diagnosticRecordId)
    else if (action === 'status') await queryOneStatus(diagnosticRecordId)
    else if (action === 'poll') await pollOneStatus(diagnosticRecordId)
    await loadDiagnostics(diagnosticRecordId)
  }

  const renderCdkMobileCards = () => {
    if (paginatedItems.length === 0) {
      return <Empty description={loading ? '正在加载卡密' : '暂无卡密'} image={Empty.PRESENTED_IMAGE_SIMPLE} />
    }

    return (
      <div className="mobile-card-list">
        {paginatedItems.map((record) => {
          const id = Number(record.id)
          const selected = selectedRowKeys.some((key) => Number(key) === id)
          const quota = quotaInfo(record)
          const orders = queryOrders(record)
          const firstDisplay = record.display_id || record.order_id || String(orders.find((order) => order.display_id)?.display_id || '')
          const latestStatus = record.upstream_status || String(orders[0]?.status || '')
          const pollTarget = getPollerTarget(pollerSnapshot, record.id)
          const isWatching = watchingCdkIds.includes(id)
          const isBackendPolling = backendWatchingIds.includes(id)
          const pollLastStatus = String(pollTarget?.last_status || pollTarget?.status || '').trim()
          const pollError = String(pollTarget?.last_error || pollTarget?.error || pollTarget?.message || '').trim()
          const timeoutText = formatRemainingSeconds(Number(pollTarget?.deadline_at || 0))
          const codeText = showPlainCodes ? String(record.code_value || record.code_masked || '') : String(record.code_masked || record.code_value || '')
          const errorText = String(record.last_error_message || pollError || '').trim()

          return (
            <Card key={record.id} size="small" className="mobile-record-card">
              <div className="mobile-record-head">
                <Checkbox
                  checked={selected}
                  onChange={(event) => {
                    setSelectedRowKeys((prev) => {
                      if (event.target.checked) return Array.from(new Set([...prev.map((key) => Number(key)), id]))
                      return prev.filter((key) => Number(key) !== id)
                    })
                  }}
                />
                <div className="mobile-record-main">
                  <Typography.Text
                    code
                    className="mobile-record-title"
                    copyable={record.code_value ? { text: String(record.code_value), tooltips: ['复制卡密', '已复制'] } : false}
                  >
                    {codeText || '-'}
                  </Typography.Text>
                  <div className="mobile-record-meta">
                    {statusTag(record.status, record.last_error_message || record.upstream_status)}
                    <Tag color={quota.remaining > 0 ? 'success' : quota.total > 0 ? 'error' : 'default'}>
                      配额 {Number.isFinite(quota.remaining) ? quota.remaining : 0}/{Number.isFinite(quota.total) ? quota.total : 0}
                    </Tag>
                    {isWatching ? (
                      <Tooltip title={pollerTargetTooltip(pollTarget) || (isBackendPolling ? '后台轮询中' : '页面观察中')}>
                        <Tag color="processing">{isBackendPolling ? '后台轮询' : '页面观察'}</Tag>
                      </Tooltip>
                    ) : null}
                    {latestStatus ? <Tag color={orderStatusColor(latestStatus)}>{latestStatus}</Tag> : null}
                  </div>
                </div>
              </div>

              <div className="mobile-record-section">
                <div className="mobile-record-field">
                  <span className="mobile-record-label">绑定账号</span>
                  <Typography.Text
                    className="mobile-record-value"
                    copyable={record.bound_account_email ? { text: record.bound_account_email, tooltips: ['复制邮箱', '已复制'] } : false}
                  >
                    {record.bound_account_email || '-'}
                  </Typography.Text>
                </div>
                <div className="mobile-record-field">
                  <span className="mobile-record-label">订单</span>
                  <Typography.Text
                    className="mobile-record-value"
                    copyable={record.order_id ? { text: record.order_id, tooltips: ['复制订单', '已复制'] } : false}
                  >
                    {firstDisplay || (orders.length > 0 ? `历史 ${orders.length} 条` : '-')}
                  </Typography.Text>
                </div>
                <div className="mobile-record-field">
                  <span className="mobile-record-label">远端邮箱</span>
                  <Typography.Text className="mobile-record-value">{record.remote_email || '-'}</Typography.Text>
                </div>
                <div className="mobile-record-field">
                  <span className="mobile-record-label">历史/轮询</span>
                  <span className="mobile-record-value">
                    已用 {Number.isFinite(quota.used) ? quota.used : 0} · 历史 {orders.length}
                    {pollLastStatus ? ` · ${pollLastStatus}` : ''}
                    {timeoutText ? ` · ${timeoutText}` : ''}
                  </span>
                </div>
                <div className="mobile-record-field">
                  <span className="mobile-record-label">提交时间</span>
                  <span className="mobile-record-value">{formatBeijingTime(record.submitted_at)}</span>
                </div>
                <div className="mobile-record-field">
                  <span className="mobile-record-label">查询/成功时间</span>
                  <span className="mobile-record-value">{formatBeijingTime(record.last_checked_at)} / {formatBeijingTime(record.paid_at)}</span>
                </div>
              </div>

              {errorText ? (
                <Alert style={{ marginTop: 10 }} type="warning" showIcon message={errorText} />
              ) : null}

              <div className="mobile-record-actions">
                <Button size="small" icon={<SyncOutlined />} loading={batchAction === 'quota'} onClick={() => checkOneQuota(record.id)}>
                  配额
                </Button>
                {record.order_id ? (
                  <Button size="small" icon={<SyncOutlined />} loading={batchAction === 'status'} onClick={() => queryOneStatus(record.id)}>
                    查状态
                  </Button>
                ) : null}
                {record.order_id && !['paid', 'failed', 'disabled'].includes(record.status) ? (
                  <Button size="small" icon={<ReloadOutlined />} loading={batchAction === 'poll'} onClick={() => pollOneStatus(record.id)}>
                    轮询
                  </Button>
                ) : null}
                <Button size="small" icon={<BugOutlined />} onClick={() => openDiagnostics(record)}>
                  诊断
                </Button>
                {record.status !== 'available' ? (
                  <Button size="small" icon={<UndoOutlined />} onClick={() => reset(record.id)}>
                    恢复
                  </Button>
                ) : null}
                <Button size="small" icon={<StopOutlined />} onClick={() => disable(record.id)}>
                  停用
                </Button>
                <Popconfirm title="确认删除该卡密？" onConfirm={() => del(record.id)}>
                  <Button danger size="small" icon={<DeleteOutlined />}>
                    删除
                  </Button>
                </Popconfirm>
              </div>
            </Card>
          )
        })}
      </div>
    )
  }

  const columns: any[] = [
    {
      title: '卡密（明文）',
      dataIndex: 'code_masked',
      key: 'code_masked',
      render: (value: string, record: CdkPoolItem) => (
        <Space direction="vertical" size={2}>
          <Space size={6}>
            <Typography.Text
              code
              ellipsis={{ tooltip: showPlainCodes ? String(record.code_value || value || '') : String(value || '') }}
              style={{ maxWidth: 260 }}
            >
              {showPlainCodes ? (record.code_value || value) : value}
            </Typography.Text>
            {record.code_value ? (
              <Popconfirm title="确认复制该卡密明文？" onConfirm={() => copyToClipboard(String(record.code_value || ''), '已复制卡密')}>
                <Button size="small" type="text" icon={<CopyOutlined />} />
              </Popconfirm>
            ) : null}
          </Space>
          {record.label ? <Typography.Text type="secondary" style={{ fontSize: 12 }}>{record.label}</Typography.Text> : null}
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      render: (value: string, record: CdkPoolItem) => statusTag(value, record.last_error_message || record.upstream_status),
    },
    {
      title: '配额/历史',
      key: 'quota',
      render: (_: any, record: CdkPoolItem) => {
        const quota = quotaInfo(record)
        const orders = queryOrders(record)
        const paid = orders.filter((order) => String(order.status || '').toLowerCase() === 'paid').length
        const processing = orders.filter((order) => ['processing', 'pending', 'submitted'].includes(String(order.status || '').toLowerCase())).length
        const failed = orders.filter((order) => ['failed', 'expired', 'cancelled', 'canceled', 'invalid', 'used'].includes(String(order.status || '').toLowerCase())).length
        return (
          <Space direction="vertical" size={2}>
            <Typography.Text style={{ fontSize: 12 }}>
              剩余 {Number.isFinite(quota.remaining) ? quota.remaining : 0} / 总 {Number.isFinite(quota.total) ? quota.total : 0}
            </Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>已用 {Number.isFinite(quota.used) ? quota.used : 0}</Typography.Text>
            {orders.length > 0 ? (
              <Space size={4} wrap>
                {paid > 0 ? <Tag color="success">paid {paid}</Tag> : null}
                {processing > 0 ? <Tag color="processing">处理中 {processing}</Tag> : null}
                {failed > 0 ? <Tag color={quota.remaining > 0 ? 'warning' : 'error'}>失败 {failed}</Tag> : null}
              </Space>
            ) : null}
          </Space>
        )
      },
    },
    {
      title: '绑定账号',
      key: 'account',
      render: (_: any, record: CdkPoolItem) => record.bound_account_email ? (
        <Space direction="vertical" size={2}>
          <Typography.Text copyable={{ text: record.bound_account_email, tooltips: ['复制邮箱', '已复制'] }} ellipsis={{ tooltip: record.bound_account_email }} style={{ maxWidth: 220 }}>
            {record.bound_account_email}
          </Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>ID: {record.bound_account_id || '-'}</Typography.Text>
        </Space>
      ) : <Typography.Text type="secondary">-</Typography.Text>,
    },
    {
      title: '订单',
      key: 'order',
      render: (_: any, record: CdkPoolItem) => {
        const orders = queryOrders(record)
        const firstDisplay = record.display_id || record.order_id || String(orders.find((order) => order.display_id)?.display_id || '')
        const latestStatus = record.upstream_status || String(orders[0]?.status || '')
        const pollTarget = getPollerTarget(pollerSnapshot, record.id)
        const isWatching = watchingCdkIds.includes(Number(record.id))
        const isBackendPolling = backendWatchingIds.includes(Number(record.id))
        const pollLastStatus = String(pollTarget?.last_status || pollTarget?.status || '').trim()
        const pollError = String(pollTarget?.last_error || pollTarget?.error || pollTarget?.message || '').trim()
        const timeoutText = formatRemainingSeconds(Number(pollTarget?.deadline_at || 0))
        const watchTitle = pollerTargetTooltip(pollTarget) || (isBackendPolling ? '后台轮询目标存在，等待下一次 snapshot 返回明细' : '页面只读取本地快照，不请求 Idea 上游')
        if (!firstDisplay && orders.length === 0 && !isWatching) return <Typography.Text type="secondary">-</Typography.Text>
        return (
          <Space direction="vertical" size={2}>
            {firstDisplay || orders.length > 0 ? (
              <Typography.Text
                copyable={record.order_id ? { text: record.order_id, tooltips: ['复制订单', '已复制'] } : false}
                ellipsis={{ tooltip: firstDisplay }}
                style={{ maxWidth: 220 }}
              >
                {firstDisplay || `历史 ${orders.length} 条`}
              </Typography.Text>
            ) : null}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>{latestStatus || '-'}</Typography.Text>
            {isWatching ? (
              <Space size={4} wrap>
                <Tooltip title={watchTitle}>
                  <Tag color="processing">{isBackendPolling ? '后台轮询中' : '页面观察中'}</Tag>
                </Tooltip>
                {pollLastStatus ? <Tag>{pollLastStatus}</Tag> : null}
                {timeoutText ? <Typography.Text type="secondary" style={{ fontSize: 12 }}>{timeoutText}</Typography.Text> : null}
                {pollError ? <Tooltip title={pollError}><Tag color="error">有错误</Tag></Tooltip> : null}
              </Space>
            ) : null}
            {orders.length > 1 ? <Typography.Text type="secondary" style={{ fontSize: 12 }}>历史 {orders.length} 条</Typography.Text> : null}
          </Space>
        )
      },
    },
    {
      title: '远端邮箱',
      dataIndex: 'remote_email',
      key: 'remote_email',
      render: (value: string) => value ? <Typography.Text ellipsis={{ tooltip: value }} style={{ maxWidth: 180 }}>{value}</Typography.Text> : <Typography.Text type="secondary">-</Typography.Text>,
    },
    {
      title: '时间',
      key: 'time',
      render: (_: any, record: CdkPoolItem) => (
        <Space direction="vertical" size={2}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>提交：{formatBeijingTime(record.submitted_at)}</Typography.Text>
          <Typography.Text type={record.paid_at ? 'success' : 'secondary'} style={{ fontSize: 12 }}>成功：{formatBeijingTime(record.paid_at)}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>查询：{formatBeijingTime(record.last_checked_at)}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '操作',
      key: 'actions',
      fixed: 'right',
      render: (_: any, record: CdkPoolItem) => (
        <Space>
          <Tooltip title="校验配额/查询卡密历史">
            <Button type="text" size="small" icon={<SyncOutlined />} onClick={() => checkOneQuota(record.id)} />
          </Tooltip>
          {record.order_id ? (
            <Tooltip title="查询订单状态">
              <Button type="text" size="small" icon={<SyncOutlined />} onClick={() => queryOneStatus(record.id)} />
            </Tooltip>
          ) : null}
          {record.order_id && !['paid', 'failed', 'disabled'].includes(record.status) ? (
            <Tooltip title="后台轮询订单状态">
              <Button type="text" size="small" icon={<ReloadOutlined />} onClick={() => pollOneStatus(record.id)} />
            </Tooltip>
          ) : null}
          <Tooltip title="状态诊断">
            <Button type="text" size="small" icon={<BugOutlined />} onClick={() => openDiagnostics(record)} />
          </Tooltip>
          {record.status !== 'available' ? (
            <Tooltip title="恢复可用">
              <Button type="text" size="small" icon={<UndoOutlined />} onClick={() => reset(record.id)} />
            </Tooltip>
          ) : null}
          <Tooltip title="停用">
            <Button type="text" size="small" icon={<StopOutlined />} onClick={() => disable(record.id)} />
          </Tooltip>
          <Tooltip title="删除">
            <Popconfirm title="确认删除该卡密？" onConfirm={() => del(record.id)}>
              <Button type="text" size="small" danger icon={<DeleteOutlined />} />
            </Popconfirm>
          </Tooltip>
        </Space>
      ),
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <div>
          <h1 style={{ fontSize: 24, fontWeight: 'bold', margin: 0 }}>idea批量提交</h1>
          <p style={{ color: token.colorTextSecondary, marginTop: 4, marginBottom: 0 }}>
            卡密库存、账号绑定和订单状态查询。ChatGPT 页面提交任务会自动从这里取可用卡密。
          </p>
        </div>
        <Space wrap>
          <Button icon={<UploadOutlined />} onClick={() => setImportOpen((open) => !open)}>
            {importOpen ? '收起导入' : '导入卡密'}
          </Button>
          <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>刷新</Button>
        </Space>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(136px, 1fr))', gap: 10 }}>
        {summaryItems.map((item) => (
          <div key={item.label} style={{ border: `1px solid ${token.colorBorderSecondary}`, borderRadius: token.borderRadiusLG, padding: '10px 12px', background: token.colorBgContainer }}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>{item.label}</Typography.Text>
            <div style={{ color: item.color, fontSize: 22, fontWeight: 700, lineHeight: 1.2 }}>{item.value}</div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>{item.detail}</Typography.Text>
          </div>
        ))}
      </div>

      {importOpen ? (
        <Card title="导入卡密" extra={<Button type="text" size="small" onClick={() => setImportOpen(false)}>收起</Button>}>
          <Space direction="vertical" style={{ width: '100%' }}>
            <Input.TextArea
              value={importText}
              onChange={(event) => setImportText(event.target.value)}
              autoSize={{ minRows: 4, maxRows: 10 }}
              placeholder="CDK-AAAA-BBBB\nCDK-CCCC-DDDD----备注"
              style={{ fontFamily: 'monospace' }}
            />
            <Typography.Text type="secondary">一行一个卡密。支持“卡密----备注”。导入入库后会逐个调用 code-info/query，同步配额、绑定账号和历史订单。</Typography.Text>
            <Button type="primary" icon={<UploadOutlined />} loading={importing} disabled={!importText.trim()} onClick={importCodes}>导入卡密</Button>
          </Space>
        </Card>
      ) : null}

      {importQueryJob ? (
        <Card
          size="small"
          title={`入库查询任务：${Number(importQueryJob.progress || 0)} / ${Number(importQueryJob.total || 0)}`}
          extra={
            <Space>
              {statusTag(importQueryJob.status, importQueryJob.error || '')}
              <Button size="small" type="text" onClick={() => setImportQueryJob(null)}>关闭</Button>
            </Space>
          }
        >
          <Space direction="vertical" style={{ width: '100%' }}>
            <Progress
              percent={Math.round((Number(importQueryJob.progress || 0) / Math.max(Number(importQueryJob.total || 0), 1)) * 100)}
              status={String(importQueryJob.status || '') === 'failed' ? 'exception' : String(importQueryJob.status || '') === 'done' ? 'success' : 'active'}
            />
            <Typography.Text type="secondary">
              成功 {Number(importQueryJob.success || 0)} 个，失败 {Number(importQueryJob.failed || 0)} 个。导入请求已经返回，配额和历史在后台串行同步。
            </Typography.Text>
            {Array.isArray(importQueryJob.logs) && importQueryJob.logs.length > 0 ? (
              <div style={{ maxHeight: 120, overflow: 'auto', background: token.colorFillAlter, borderRadius: token.borderRadius, padding: 8 }}>
                {importQueryJob.logs.slice(-8).map((line, index) => (
                  <Typography.Text key={`${index}-${line}`} style={{ display: 'block', fontSize: 12, fontFamily: 'monospace' }}>{line}</Typography.Text>
                ))}
              </div>
            ) : null}
          </Space>
        </Card>
      ) : null}

      <Alert
        type="info"
        showIcon
        message="卡密可提交性以 /api/code-info 的 remaining 配额为准；/api/query 用来同步绑定账号、paid/failed 历史和多条提交记录。"
      />

      <Card
        title="卡密列表"
        extra={
          <Space wrap>
            <Input.Search value={search} allowClear placeholder="搜索明文卡密/账号/订单" style={{ width: 220 }} onChange={(event) => setSearch(event.target.value)} />
            <Select value={statusFilter} options={STATUS_OPTIONS} style={{ width: 132 }} onChange={(value) => setStatusFilter(value)} />
            <Typography.Text type="secondary">显示 {filteredItems.length} 个</Typography.Text>
            <Typography.Text type={selectedRowKeys.length > 0 ? 'success' : 'secondary'}>已选 {selectedRowKeys.length} 个</Typography.Text>
            <Typography.Text type={watchingCdkIds.length > 0 ? 'warning' : 'secondary'}>观察 {watchingCdkIds.length} 个</Typography.Text>
            <Button
              size="small"
              icon={showPlainCodes ? <EyeInvisibleOutlined /> : <EyeOutlined />}
              onClick={() => setShowPlainCodes((value) => !value)}
            >
              {showPlainCodes ? '隐藏明文' : '显示明文'}
            </Button>
            <Popconfirm
              title={`确认复制选中的 ${selectedCodeValues.length} 个卡密明文？`}
              disabled={selectedCodeValues.length === 0}
              onConfirm={() => copyToClipboard(selectedCodeValues.join('\n'), `已复制 ${selectedCodeValues.length} 个卡密`)}
            >
              <Button size="small" icon={<CopyOutlined />} disabled={selectedCodeValues.length === 0}>复制选中卡密</Button>
            </Popconfirm>
            <Button size="small" icon={<SyncOutlined />} disabled={selectedRowKeys.length === 0} loading={batchAction === 'quota'} onClick={() => runBatchAction('quota')}>校验配额</Button>
            <Button size="small" icon={<SyncOutlined />} disabled={selectedRowKeys.length === 0} loading={batchAction === 'status'} onClick={() => runBatchAction('status')}>查状态</Button>
            <Button size="small" icon={<ReloadOutlined />} disabled={selectedRowKeys.length === 0} loading={batchAction === 'poll'} onClick={() => runBatchAction('poll')}>轮询</Button>
            <Popconfirm title={`确认恢复选中的 ${selectedRowKeys.length} 个卡密？`} disabled={selectedRowKeys.length === 0} onConfirm={() => runBatchAction('reset')}>
              <Button size="small" icon={<UndoOutlined />} disabled={selectedRowKeys.length === 0} loading={batchAction === 'reset'}>恢复</Button>
            </Popconfirm>
            <Popconfirm title={`确认停用选中的 ${selectedRowKeys.length} 个卡密？`} disabled={selectedRowKeys.length === 0} onConfirm={() => runBatchAction('disable')}>
              <Button size="small" icon={<StopOutlined />} disabled={selectedRowKeys.length === 0} loading={batchAction === 'disable'}>停用</Button>
            </Popconfirm>
            <Popconfirm title={`确认删除选中的 ${selectedRowKeys.length} 个卡密？`} disabled={selectedRowKeys.length === 0} onConfirm={() => runBatchAction('delete')}>
              <Button size="small" danger icon={<DeleteOutlined />} disabled={selectedRowKeys.length === 0} loading={batchAction === 'delete'}>删除</Button>
            </Popconfirm>
          </Space>
        }
      >
        {isMobile ? (
          renderCdkMobileCards()
        ) : (
          <Table
            rowKey="id"
            loading={loading}
            columns={columns}
            dataSource={paginatedItems}
            pagination={false}
            size="small"
            scroll={{ x: 1400, y: 520 }}
            rowSelection={{
              selectedRowKeys,
              onChange: (keys) => setSelectedRowKeys(keys.map((key) => Number(key)).filter((key) => Number.isFinite(key))),
              preserveSelectedRowKeys: true,
            }}
          />
        )}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 12, gap: 12, flexWrap: 'wrap' }}>
          <Typography.Text type="secondary">每页 {PAGE_SIZE} 个，合计 {filteredItems.length} 个</Typography.Text>
          <Pagination current={currentPage} pageSize={PAGE_SIZE} total={filteredItems.length} showSizeChanger={false} onChange={setCurrentPage} />
        </div>
      </Card>

      <Drawer
        title={
          <Space direction="vertical" size={2}>
            <Space wrap>
              <span>Idea 状态诊断</span>
              {diagnosticItem ? statusTag(String(diagnosticItem.status || '')) : null}
            </Space>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {diagnosticItem?.code_masked || diagnosticItem?.code_value || diagnosticRecordId || '-'}
            </Typography.Text>
          </Space>
        }
        width={920}
        open={diagnosticOpen}
        onClose={() => setDiagnosticOpen(false)}
        extra={
          <Space wrap>
            <Button size="small" loading={diagnosticLoading} onClick={() => void refreshDiagnosticAfter('local')} disabled={!diagnosticRecordId}>
              刷新诊断
            </Button>
            <Button size="small" loading={batchAction === 'quota'} onClick={() => void refreshDiagnosticAfter('quota')} disabled={!diagnosticRecordId}>
              校验配额
            </Button>
            <Button size="small" loading={batchAction === 'status'} onClick={() => void refreshDiagnosticAfter('status')} disabled={!diagnosticRecordId || !diagnosticItem?.order_id}>
              查状态
            </Button>
            <Button size="small" loading={batchAction === 'poll'} onClick={() => void refreshDiagnosticAfter('poll')} disabled={!diagnosticRecordId || !diagnosticItem?.order_id || ['paid', 'failed', 'disabled'].includes(String(diagnosticItem?.status || ''))}>
              轮询
            </Button>
          </Space>
        }
      >
        {!diagnosticItem && !diagnosticLoading ? (
          <Empty description="请选择一条卡密查看诊断" />
        ) : (
          <Space direction="vertical" size={14} style={{ width: '100%' }}>
            {diagnosticNotes.length > 0 ? (
              <Space direction="vertical" size={6} style={{ width: '100%' }}>
                {diagnosticNotes.map((note, index) => (
                  <Alert
                    key={`${index}-${note.message}`}
                    type={noteColor(note.level) as any}
                    showIcon
                    message={note.message || '-'}
                  />
                ))}
              </Space>
            ) : null}

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 12 }}>
              <Card size="small" title="本地卡密 / code-info">
                <Descriptions size="small" column={1}>
                  <Descriptions.Item label="卡密">
                    <Typography.Text code copyable={diagnosticItem?.code_value ? { text: diagnosticItem.code_value } : false}>
                      {diagnosticItem?.code_value || diagnosticItem?.code_masked || '-'}
                    </Typography.Text>
                  </Descriptions.Item>
                  <Descriptions.Item label="状态">{diagnosticItem ? statusTag(String(diagnosticItem.status || '')) : '-'}</Descriptions.Item>
                  <Descriptions.Item label="配额">
                    剩余 {String(diagnosticQuota.remaining ?? diagnosticItem?.code_info_remaining ?? 0)}
                    {' / '}
                    总 {String(diagnosticQuota.total ?? diagnosticItem?.code_info_total ?? 0)}
                    {' / '}
                    已用 {String(diagnosticQuota.used ?? '-')}
                  </Descriptions.Item>
                  <Descriptions.Item label="状态码">{String(diagnosticQuota.status_code || diagnosticItem?.upstream_status || '-')}</Descriptions.Item>
                  <Descriptions.Item label="最后检查">{formatBeijingTime(String(diagnosticQuota.last_checked_at || diagnosticItem?.last_checked_at || ''))}</Descriptions.Item>
                  <Descriptions.Item label="错误">
                    {diagnosticItem?.last_error_message ? (
                      <Typography.Text type="danger">{diagnosticItem.last_error_message}</Typography.Text>
                    ) : '-'}
                  </Descriptions.Item>
                </Descriptions>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {String(diagnosticQuota.note || 'code-info 只更新本地配额字段；query 会带历史 orders。')}
                </Typography.Text>
              </Card>

              <Card size="small" title={`query 历史订单 (${diagnosticOrders.length})`}>
                {diagnosticOrders.length === 0 ? (
                  <Typography.Text type="secondary">没有 query orders</Typography.Text>
                ) : (
                  <Space direction="vertical" size={8} style={{ width: '100%' }}>
                    {diagnosticOrders.slice(0, 6).map((order, index) => {
                      const status = String(order.status || '-')
                      const display = String(order.display_id || order.order_id || `#${index + 1}`)
                      const email = String(order.email || '-')
                      return (
                        <div key={`${index}-${display}`} style={{ borderBottom: index < Math.min(diagnosticOrders.length, 6) - 1 ? `1px solid ${token.colorBorderSecondary}` : 'none', paddingBottom: 6 }}>
                          <Space wrap size={4}>
                            <Tag color={orderStatusColor(status)}>{status}</Tag>
                            <Typography.Text copyable={display !== '-' ? { text: display } : false}>{display}</Typography.Text>
                          </Space>
                          <Typography.Text type="secondary" style={{ display: 'block', fontSize: 12 }} ellipsis={{ tooltip: email }}>{email}</Typography.Text>
                          <Typography.Text type="secondary" style={{ display: 'block', fontSize: 12 }}>
                            创建 {order.created_at || '-'}，成功 {order.paid_at || '-'}
                          </Typography.Text>
                        </div>
                      )
                    })}
                    {diagnosticOrders.length > 6 ? <Typography.Text type="secondary">还有 {diagnosticOrders.length - 6} 条未展示，可看下方原始 query。</Typography.Text> : null}
                  </Space>
                )}
              </Card>

              <Card size="small" title="后台 poller target">
                {diagnosticPollTarget ? (
                  <Descriptions size="small" column={1}>
                    <Descriptions.Item label="来源">{pollerSourceLabel(diagnosticPollTarget.source)}</Descriptions.Item>
                    <Descriptions.Item label="任务">{diagnosticPollTarget.task_id || '-'}</Descriptions.Item>
                    <Descriptions.Item label="最近状态">{diagnosticPollTarget.last_status || diagnosticPollTarget.status || '-'}</Descriptions.Item>
                    <Descriptions.Item label="间隔/超时">
                      {diagnosticPollTarget.interval_seconds || '-'}s / {diagnosticPollTarget.timeout_seconds || '-'}s
                    </Descriptions.Item>
                    <Descriptions.Item label="剩余">{formatRemainingSeconds(Number(diagnosticPollTarget.deadline_at || 0)) || '-'}</Descriptions.Item>
                    <Descriptions.Item label="下次检查">{diagnosticPollTarget.next_due_at ? new Date(Number(diagnosticPollTarget.next_due_at) * 1000).toLocaleString() : '-'}</Descriptions.Item>
                    <Descriptions.Item label="错误">
                      {diagnosticPollTarget.last_error ? <Typography.Text type="danger">{diagnosticPollTarget.last_error}</Typography.Text> : '-'}
                    </Descriptions.Item>
                  </Descriptions>
                ) : (
                  <Typography.Text type="secondary">
                    当前没有 poller target。只有 submitted/processing 且有 order_id 的订单才会入队。
                  </Typography.Text>
                )}
              </Card>

              <Card size="small" title="绑定账号 extra">
                {diagnosticAccount ? (
                  <Space direction="vertical" size={8} style={{ width: '100%' }}>
                    <Descriptions size="small" column={1}>
                      <Descriptions.Item label="账号">
                        <Typography.Text copyable={{ text: String(diagnosticAccount.email || '') }}>
                          {diagnosticAccount.email || '-'}
                        </Typography.Text>
                      </Descriptions.Item>
                      <Descriptions.Item label="ID">{diagnosticAccount.id || '-'}</Descriptions.Item>
                      <Descriptions.Item label="状态">{diagnosticAccount.status || '-'}</Descriptions.Item>
                      <Descriptions.Item label="匹配">{diagnosticAccount.match_by || '-'}</Descriptions.Item>
                      <Descriptions.Item label="更新时间">{formatBeijingTime(String(diagnosticAccount.updated_at || ''))}</Descriptions.Item>
                    </Descriptions>
                    <details>
                      <summary style={{ cursor: 'pointer', color: token.colorTextSecondary }}>extra.baxigpt_cdk</summary>
                      <JsonBlock value={diagnosticAccount.baxigpt_cdk} maxHeight={150} />
                    </details>
                    <details>
                      <summary style={{ cursor: 'pointer', color: token.colorTextSecondary }}>历史 {Array.isArray(diagnosticAccount.baxigpt_cdk_history) ? diagnosticAccount.baxigpt_cdk_history.length : 0} 条</summary>
                      <JsonBlock value={diagnosticAccount.baxigpt_cdk_history} maxHeight={180} />
                    </details>
                  </Space>
                ) : (
                  <Typography.Text type="secondary">未找到绑定账号，可能只有远端邮箱或还没绑定本地账号。</Typography.Text>
                )}
              </Card>
            </div>

            <Card size="small" title="原始响应">
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 12 }}>
                <div>
                  <Typography.Text strong>submit_response</Typography.Text>
                  <JsonBlock value={diagnostic?.submit_response} />
                </div>
                <div>
                  <Typography.Text strong>last_status_response</Typography.Text>
                  <JsonBlock value={diagnostic?.last_status_response} />
                </div>
                <div>
                  <Typography.Text strong>query_response</Typography.Text>
                  <JsonBlock value={diagnostic?.query_response} />
                </div>
                <div>
                  <Typography.Text strong>task_logs</Typography.Text>
                  <JsonBlock value={diagnostic?.task_logs} />
                </div>
              </div>
            </Card>
          </Space>
        )}
      </Drawer>
    </div>
  )
}
