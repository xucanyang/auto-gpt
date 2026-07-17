import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Divider,
  Empty,
  Form,
  Grid,
  Input,
  InputNumber,
  Pagination,
  Row,
  Segmented,
  Space,
  List,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  ClearOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SearchOutlined,
  StopOutlined,
} from '@ant-design/icons'

import { consumeEventStream, isAbortError } from '@/lib/eventStream'
import { ApiError, apiFetch } from '@/lib/utils'

const { Text, Title } = Typography

type PipelineConfig = {
  payment_pool_threshold: number
  payment_pool_target: number
  payment_batch_interval_seconds: number
  payment_batch_max_size: number
  auto_start: boolean
  enable_auth_capture: boolean
  auth_poll_interval_seconds: number
  register_poll_interval_seconds: number
  gopay_batch_poll_interval_seconds: number
  gopay_timeout_seconds: number
  platform: string
  mail_provider: string
  proxy?: string | null
  executor_type: string
  captcha_solver: string
  register_extra: Record<string, unknown>
  gopay_country: string
  gopay_currency: string
}

type PipelineTask = {
  id?: number
  task_key?: string
  status?: string
  active_register_task_id?: string
  active_payment_batch_id?: string
  active_auth_task_id?: string
  last_error?: string
  started_at?: string
  stopped_at?: string
  updated_at?: string
}

type PipelineAccountItem = {
  id?: number
  account_id?: number
  email?: string
  pipeline_status?: string
  register_stage?: string
  payment_stage?: string
  auth_stage?: string
  account_primary_status?: string
  checkout_url?: string
  payment_batch_task_id?: string
  subscription_plan_confirmed?: string
  subscription_refresh_status?: string
  register_error_reason?: string
  register_error_detail?: string
  register_error_code?: string
  payment_error_reason?: string
  payment_error_detail?: string
  payment_error_code?: string
  auth_error_reason?: string
  auth_error_detail?: string
  auth_error_code?: string
  success_summary?: string
  updated_at?: string
}

type PipelineStatusResponse = {
  task: PipelineTask | null
  config: PipelineConfig
  queues: {
    pending_payment: PipelineAccountItem[]
    paid: PipelineAccountItem[]
    failed: PipelineAccountItem[]
    auth_pending: PipelineAccountItem[]
  }
  active_payment_batch?: {
    task_id?: string
    status?: string
    message?: string
  } | null
  task_logs?: string[]
  task_list?: Array<{
    id?: number
    task_key?: string
    status?: string
    started_at?: string
    stopped_at?: string
    updated_at?: string
  }>
  summary: {
    pending_payment_count: number
    paid_count: number
    failed_count: number
    auth_pending_count: number
  }
}

type QueueTabKey = 'pending_payment' | 'paid' | 'failed' | 'auth_pending'
type MainTabKey = 'overview' | 'config' | 'queues' | 'logs'
type LogFilterKey = 'all' | 'errors' | 'child'

const DEFAULT_CONFIG: PipelineConfig = {
  payment_pool_threshold: 3,
  payment_pool_target: 6,
  payment_batch_interval_seconds: 300,
  payment_batch_max_size: 0,
  auto_start: false,
  enable_auth_capture: false,
  auth_poll_interval_seconds: 3,
  register_poll_interval_seconds: 3,
  gopay_batch_poll_interval_seconds: 3,
  gopay_timeout_seconds: 1800,
  platform: 'chatgpt',
  mail_provider: '',
  proxy: '',
  executor_type: 'protocol',
  captcha_solver: 'yescaptcha',
  register_extra: {},
  gopay_country: 'ID',
  gopay_currency: 'IDR',
}

const STATUS_META: Record<string, { label: string; color: string }> = {
  running: { label: '运行中', color: 'processing' },
  paused: { label: '已暂停', color: 'warning' },
  stopped: { label: '已停止', color: 'default' },
  done: { label: '已完成', color: 'success' },
  success: { label: '成功', color: 'success' },
  failed: { label: '失败', color: 'error' },
  pending: { label: '等待中', color: 'default' },
  disabled: { label: '未启用', color: 'default' },
  pending_register: { label: '待注册', color: 'default' },
  registering: { label: '注册中', color: 'processing' },
  pending_payment: { label: '待支付', color: 'blue' },
  payment_reserved: { label: '支付已预留', color: 'geekblue' },
  link_generating: { label: '生成支付链接', color: 'processing' },
  link_ready: { label: '支付链接就绪', color: 'blue' },
  paying: { label: '支付中', color: 'processing' },
  paid: { label: '已支付', color: 'success' },
  payment_failed: { label: '支付失败', color: 'error' },
  auth_pending: { label: '待补抓 Auth', color: 'gold' },
  auth_running: { label: 'Auth 补抓中', color: 'processing' },
  auth_failed: { label: 'Auth 失败', color: 'error' },
  registered: { label: '已注册', color: 'default' },
  subscribed: { label: '已订阅', color: 'success' },
  invalid: { label: '已失效', color: 'error' },
}

const STAGE_META: Record<string, { label: string; color: string }> = {
  register: { label: '注册', color: 'blue' },
  payment: { label: '支付', color: 'purple' },
  auth: { label: 'Auth', color: 'gold' },
  done: { label: '完成', color: 'success' },
  idle: { label: '空闲', color: 'default' },
}

function normalizeStatus(status?: string) {
  return String(status || '').trim().toLowerCase()
}

function statusColor(status?: string) {
  return STATUS_META[normalizeStatus(status)]?.color || 'default'
}

function statusLabel(status?: string) {
  const value = String(status || '').trim()
  return STATUS_META[normalizeStatus(value)]?.label || value || '未知'
}

function getAccountIssueText(record: PipelineAccountItem) {
  return (
    String(record.register_error_reason || '').trim()
    || String(record.payment_error_reason || '').trim()
    || String(record.auth_error_reason || '').trim()
    || String(record.register_error_detail || '').trim()
    || String(record.payment_error_detail || '').trim()
    || String(record.auth_error_detail || '').trim()
    || String(record.success_summary || '').trim()
  )
}

function getAccountIssueStage(record: PipelineAccountItem) {
  if (record.register_error_reason || record.register_error_detail || record.register_error_code) return 'register'
  if (record.payment_error_reason || record.payment_error_detail || record.payment_error_code) return 'payment'
  if (record.auth_error_reason || record.auth_error_detail || record.auth_error_code) return 'auth'
  const pipelineStatus = normalizeStatus(record.pipeline_status)
  if (pipelineStatus.startsWith('auth_')) return 'auth'
  if (pipelineStatus.includes('payment') || pipelineStatus === 'paying' || pipelineStatus === 'paid') return 'payment'
  if (pipelineStatus === 'done') return 'done'
  return 'register'
}

function accountStageLabel(record: PipelineAccountItem) {
  return STAGE_META[getAccountIssueStage(record)] || STAGE_META.idle
}

function queueRowKey(record: PipelineAccountItem) {
  return String(
    record.id
    || record.account_id
    || record.email
    || `${record.pipeline_status || 'item'}_${record.updated_at || ''}`,
  )
}

function formatDateTime(value?: string) {
  if (!value) return '-'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function queueColumns(titleKey: 'pipeline_status' | 'payment_stage' | 'auth_stage' = 'pipeline_status') {
  return [
    {
      title: '账号',
      dataIndex: 'email',
      key: 'email',
      render: (value: string, record: PipelineAccountItem) => (
        <Space direction="vertical" size={0}>
          <Text>{value || '-'}</Text>
          <Text type="secondary">#{record.account_id || '-'}</Text>
        </Space>
      ),
    },
    {
      title: '阶段',
      key: 'stage',
      width: 88,
      render: (_: unknown, record: PipelineAccountItem) => {
        const stage = accountStageLabel(record)
        return <Tag color={stage.color}>{stage.label}</Tag>
      },
    },
    {
      title: '主状态',
      dataIndex: 'account_primary_status',
      key: 'account_primary_status',
      width: 110,
      render: (value: string) => <Tag color={statusColor(value)}>{statusLabel(value)}</Tag>,
    },
    {
      title: '流程状态',
      dataIndex: titleKey,
      key: titleKey,
      width: 130,
      render: (value: string) => <Tag color={statusColor(value)}>{statusLabel(value)}</Tag>,
    },
    {
      title: '套餐',
      dataIndex: 'subscription_plan_confirmed',
      key: 'subscription_plan_confirmed',
      width: 120,
      render: (value: string, record: PipelineAccountItem) => {
        const refresh = String(record.subscription_refresh_status || '').trim()
        if (refresh === 'failed') {
          return <Tag color="warning">{value || 'unknown'} / 待刷新</Tag>
        }
        return <Tag>{value || '-'}</Tag>
      },
    },
    {
      title: '说明',
      key: 'summary',
      render: (_: unknown, record: PipelineAccountItem) => {
        const issue = getAccountIssueText(record)
        const isError = Boolean(record.register_error_reason || record.payment_error_reason || record.auth_error_reason)
        return (
          <Text type={isError ? 'danger' : 'secondary'} style={{ whiteSpace: 'pre-wrap' }}>
            {issue || '-'}
          </Text>
        )
      },
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      key: 'updated_at',
      width: 190,
      render: (value: string) => <Text type="secondary">{formatDateTime(value)}</Text>,
    },
  ]
}

function matchesQueueSearch(record: PipelineAccountItem, keyword: string) {
  const text = keyword.trim().toLowerCase()
  if (!text) return true
  const haystack = [
    record.email,
    String(record.account_id || ''),
    record.pipeline_status,
    record.payment_stage,
    record.auth_stage,
    record.account_primary_status,
    record.register_error_reason,
    record.register_error_detail,
    record.register_error_code,
    record.payment_error_reason,
    record.payment_error_detail,
    record.payment_error_code,
    record.auth_error_reason,
    record.auth_error_detail,
    record.auth_error_code,
    record.success_summary,
    record.subscription_plan_confirmed,
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
  return haystack.includes(text)
}

function matchesErrorOnly(record: PipelineAccountItem, errorOnly: boolean) {
  if (!errorOnly) return true
  return Boolean(
    record.register_error_reason
    || record.register_error_detail
    || record.payment_error_reason
    || record.payment_error_detail
    || record.auth_error_reason
    || record.auth_error_detail,
  )
}

function matchesLogFilter(line: string, filter: LogFilterKey) {
  const text = line.toLowerCase()
  if (filter === 'errors') {
    return text.includes('[fail]') || text.includes('失败') || text.includes('error') || text.includes('异常')
  }
  if (filter === 'child') {
    return line.startsWith('[注册任务]') || line.startsWith('[Auth任务]')
  }
  return true
}

function logLineColor(line: string) {
  const text = line.toLowerCase()
  if (text.includes('[fail]') || text.includes('失败') || text.includes('error') || text.includes('异常')) return '#dc2626'
  if (line.includes('[注册任务]')) return '#2563eb'
  if (line.includes('GoPay') || line.includes('支付')) return '#7c3aed'
  if (line.includes('[Auth任务]') || line.includes('Auth')) return '#b45309'
  return undefined
}

function buildActionHint({
  task,
  summary,
  activePaymentBatch,
  config,
}: {
  task?: PipelineTask | null
  summary?: PipelineStatusResponse['summary']
  activePaymentBatch?: PipelineStatusResponse['active_payment_batch']
  config?: PipelineConfig
}) {
  if (!task) return { type: 'info' as const, message: '尚未创建流水线任务', description: '点击启动后会创建任务，并按配置开始补货、支付和 Auth 补抓。' }
  if (task.last_error) return { type: 'warning' as const, message: '最近一次调度出现异常', description: task.last_error }
  if (task.status === 'stopped') return { type: 'info' as const, message: '流水线已停止', description: '启动后会继续使用当前配置调度新的补货和支付批次。' }
  if (task.status === 'paused') return { type: 'warning' as const, message: '流水线已暂停', description: '当前不会启动新的注册、支付或 Auth 任务。' }
  if (task.active_register_task_id) return { type: 'info' as const, message: '正在注册补货', description: `注册任务 ${task.active_register_task_id} 正在运行。` }
  if (activePaymentBatch || task.active_payment_batch_id) return { type: 'info' as const, message: '正在处理 GoPay 支付批次', description: activePaymentBatch?.message || `支付批次 ${task.active_payment_batch_id || '-'} 正在运行。` }
  if (task.active_auth_task_id) return { type: 'info' as const, message: '正在补抓 Auth', description: `Auth 任务 ${task.active_auth_task_id} 正在运行。` }
  if (!config?.enable_auth_capture && Number(summary?.paid_count || 0) > 0) return { type: 'success' as const, message: '支付完成，Auth 补抓未启用', description: '已支付账号会直接进入完成状态，不会继续补抓 Auth。' }
  if (Number(summary?.failed_count || 0) > 0) return { type: 'warning' as const, message: '有失败账号需要查看', description: `失败队列中有 ${summary?.failed_count || 0} 个账号，建议先按错误原因分组排查。` }
  if (Number(summary?.pending_payment_count || 0) > 0) return { type: 'info' as const, message: '待支付池已有账号', description: '下一轮支付调度会按批次配置处理待支付账号。' }
  return { type: 'success' as const, message: '当前没有待处理账号', description: '流水线会按补货阈值继续观察并启动新的注册任务。' }
}

export default function Pipeline() {
  const [form] = Form.useForm<PipelineConfig>()
  const screens = Grid.useBreakpoint()
  const isMobile = screens.lg === false
  const [configLoading, setConfigLoading] = useState(false)
  const [actionLoading, setActionLoading] = useState('')
  const [statusData, setStatusData] = useState<PipelineStatusResponse | null>(null)
  const [logs, setLogs] = useState<string[]>([])
  const [activeTab, setActiveTab] = useState<MainTabKey>('overview')
  const [queueTab, setQueueTab] = useState<QueueTabKey>('pending_payment')
  const [queueSearch, setQueueSearch] = useState('')
  const [queueErrorOnly, setQueueErrorOnly] = useState(false)
  const [queueMobilePage, setQueueMobilePage] = useState(1)
  const [logFilter, setLogFilter] = useState<LogFilterKey>('all')
  const [selectedTaskKey, setSelectedTaskKey] = useState('')
  const [selectedTaskLogsCache, setSelectedTaskLogsCache] = useState<Record<string, string[]>>({})

  const loadConfig = async () => {
    setConfigLoading(true)
    try {
      const config = await apiFetch('/pipeline/config')
      const supportedConfig = { ...(config || {}) }
      delete supportedConfig.gopay_plan
      form.setFieldsValue({ ...DEFAULT_CONFIG, ...supportedConfig })
    } catch (error: any) {
      message.error(error?.message || '加载流水线配置失败')
    } finally {
      setConfigLoading(false)
    }
  }

  const loadStatus = async () => {
    try {
      const data = await apiFetch('/pipeline/status')
      setStatusData(data)
    } catch (error: any) {
      message.error(error?.message || '加载流水线状态失败')
    }
  }

  const saveConfig = async (values: PipelineConfig) => {
    setActionLoading('save')
    try {
      await apiFetch('/pipeline/config', {
        method: 'PUT',
        body: JSON.stringify({ config: { ...values, gopay_plan: 'plus' } }),
      })
      message.success('流水线配置已保存')
      await loadStatus()
    } catch (error: any) {
      message.error(error?.message || '保存流水线配置失败')
    } finally {
      setActionLoading('')
    }
  }

  const invokeAction = async (path: '/pipeline/start' | '/pipeline/stop' | '/pipeline/pause', key: string) => {
    setActionLoading(key)
    try {
      await apiFetch(path, { method: 'POST' })
      await loadStatus()
      message.success('操作已提交')
    } catch (error: any) {
      message.error(error?.message || '操作失败')
    } finally {
      setActionLoading('')
    }
  }

  useEffect(() => {
    loadConfig()
    loadStatus()
    const timer = window.setInterval(() => {
      loadStatus()
    }, 3000)
    return () => {
      window.clearInterval(timer)
    }
  }, [])

  useEffect(() => {
    const persistedLogs = Array.isArray(statusData?.task_logs)
      ? statusData!.task_logs!.map((line) => String(line))
      : []
    if (persistedLogs.length > 0) {
      setLogs((prev) => {
        if (prev.length >= persistedLogs.length) {
          return prev
        }
        return persistedLogs.slice(-500)
      })
    }
    const latestTaskKey = String(statusData?.task?.task_key || '').trim()
    if (latestTaskKey && !selectedTaskKey) {
      setSelectedTaskKey(latestTaskKey)
    }
  }, [selectedTaskKey, statusData?.task?.task_key, statusData?.task_logs])

  useEffect(() => {
    const controller = new AbortController()
    let retryCount = 0
    let retryTimer: number | undefined

    const waitForRetry = (delayMs: number) => new Promise<void>((resolve) => {
      const finish = () => {
        if (retryTimer !== undefined) window.clearTimeout(retryTimer)
        retryTimer = undefined
        controller.signal.removeEventListener('abort', finish)
        resolve()
      }
      retryTimer = window.setTimeout(finish, delayMs)
      controller.signal.addEventListener('abort', finish, { once: true })
    })

    const connect = async () => {
      while (!controller.signal.aborted) {
        try {
          await consumeEventStream('/pipeline/logs/stream', {
            signal: controller.signal,
            onEvent: (event) => {
              if (controller.signal.aborted) return false
              retryCount = 0
              try {
                const payload = JSON.parse(event.data || '{}')
                if (payload.line) {
                  setLogs((prev) => {
                    const line = String(payload.line)
                    if (prev[prev.length - 1] === line) return prev
                    return [...prev, line].slice(-500)
                  })
                }
              } catch {
                // Ignore malformed event payloads and keep the stream alive.
              }
            },
          })
        } catch (error: unknown) {
          if (controller.signal.aborted || isAbortError(error)) return
          if (error instanceof ApiError && error.status === 401) return
        }

        retryCount += 1
        const retryMs = Math.min(1000 * (2 ** (retryCount - 1)), 8000)
        await waitForRetry(retryMs)
      }
    }

    void connect()
    return () => {
      controller.abort()
      if (retryTimer !== undefined) window.clearTimeout(retryTimer)
    }
  }, [])

  const task = statusData?.task
  const queues = statusData?.queues || {
    pending_payment: [],
    paid: [],
    failed: [],
    auth_pending: [],
  }
  const summary = statusData?.summary
  const activePaymentBatch = statusData?.active_payment_batch
  const isRunning = task?.status === 'running'
  const isPaused = task?.status === 'paused'
  const totalAccounts = (
    (summary?.pending_payment_count ?? 0)
    + (summary?.paid_count ?? 0)
    + (summary?.failed_count ?? 0)
    + (summary?.auth_pending_count ?? 0)
  )
  const actionHint = buildActionHint({
    task,
    summary,
    activePaymentBatch,
    config: statusData?.config,
  })

  const stats = useMemo(
    () => [
      { key: 'pending_payment', label: '待支付', value: summary?.pending_payment_count ?? 0, color: 'blue' },
      { key: 'paid', label: '已支付', value: summary?.paid_count ?? 0, color: 'green' },
      { key: 'failed', label: '失败', value: summary?.failed_count ?? 0, color: 'red' },
      { key: 'auth_pending', label: '待补抓', value: summary?.auth_pending_count ?? 0, color: 'gold' },
    ],
    [summary],
  )

  const queueDataMap: Record<QueueTabKey, PipelineAccountItem[]> = {
    pending_payment: queues.pending_payment,
    paid: queues.paid,
    failed: queues.failed,
    auth_pending: queues.auth_pending,
  }

  const queueColumnMap: Record<QueueTabKey, ReturnType<typeof queueColumns>> = {
    pending_payment: queueColumns('pipeline_status'),
    paid: queueColumns('payment_stage'),
    failed: queueColumns('payment_stage'),
    auth_pending: queueColumns('auth_stage'),
  }

  const stageCards = [
    {
      key: 'register',
      title: '注册补货',
      status: task?.active_register_task_id ? 'running' : isRunning ? 'pending' : task?.status || 'stopped',
      metric: `待支付池 ${summary?.pending_payment_count ?? 0}/${statusData?.config?.payment_pool_target ?? DEFAULT_CONFIG.payment_pool_target}`,
      detail: task?.active_register_task_id || `阈值 ${statusData?.config?.payment_pool_threshold ?? DEFAULT_CONFIG.payment_pool_threshold}`,
    },
    {
      key: 'payment',
      title: 'GoPay 支付',
      status: activePaymentBatch || task?.active_payment_batch_id ? 'paying' : (summary?.pending_payment_count || 0) > 0 ? 'pending_payment' : 'pending',
      metric: `已支付 ${summary?.paid_count ?? 0}`,
      detail: task?.active_payment_batch_id || activePaymentBatch?.task_id || `批次 ${statusData?.config?.payment_batch_max_size || '不限'}`,
    },
    {
      key: 'auth',
      title: 'Auth 补抓',
      status: !statusData?.config?.enable_auth_capture ? 'disabled' : task?.active_auth_task_id ? 'auth_running' : (summary?.auth_pending_count || 0) > 0 ? 'auth_pending' : 'pending',
      metric: `待补抓 ${summary?.auth_pending_count ?? 0}`,
      detail: statusData?.config?.enable_auth_capture ? (task?.active_auth_task_id || '已启用') : '未启用',
    },
  ]

  const failureGroups = useMemo(() => {
    const counts = new Map<string, number>()
    for (const item of queues.failed || []) {
      const reason = getAccountIssueText(item) || '未知失败'
      counts.set(reason, (counts.get(reason) || 0) + 1)
    }
    return Array.from(counts.entries())
      .map(([reason, count]) => ({ reason, count }))
      .sort((a, b) => b.count - a.count)
      .slice(0, 6)
  }, [queues.failed])

  const attentionItems = useMemo(() => {
    return [
      ...(queues.failed || []),
      ...(queues.auth_pending || []),
      ...(queues.pending_payment || []),
    ].slice(0, 8)
  }, [queues.auth_pending, queues.failed, queues.pending_payment])

  const filteredQueueData = useMemo(() => {
    return queueDataMap[queueTab].filter((record) => {
      return matchesQueueSearch(record, queueSearch) && matchesErrorOnly(record, queueErrorOnly)
    })
  }, [queueDataMap, queueErrorOnly, queueSearch, queueTab])

  const queueMobilePageSize = 10
  const mobileQueueData = useMemo(() => {
    const start = (queueMobilePage - 1) * queueMobilePageSize
    return filteredQueueData.slice(start, start + queueMobilePageSize)
  }, [filteredQueueData, queueMobilePage])

  useEffect(() => {
    setQueueMobilePage(1)
  }, [queueErrorOnly, queueSearch, queueTab])

  useEffect(() => {
    const maxPage = Math.max(1, Math.ceil(filteredQueueData.length / queueMobilePageSize))
    if (queueMobilePage > maxPage) setQueueMobilePage(maxPage)
  }, [filteredQueueData.length, queueMobilePage])

  const filteredLogs = useMemo(() => {
    return logs.filter((line) => matchesLogFilter(line, logFilter))
  }, [logFilter, logs])

  const recentLogLines = useMemo(() => {
    return [...logs].slice(-12).reverse()
  }, [logs])

  const taskList = statusData?.task_list || []

  useEffect(() => {
    const activeKey = String(statusData?.task?.task_key || '').trim()
    if (!selectedTaskKey || selectedTaskKey === activeKey) {
      return
    }
    const selected = taskList.find((item) => String(item.task_key || '').trim() === selectedTaskKey)
    const selectedId = Number(selected?.id || 0)
    if (selectedId <= 0) {
      return
    }
    let cancelled = false
    ;(async () => {
      try {
        const data = await apiFetch(`/pipeline/logs/task/${selectedId}`) as { logs?: string[] }
        if (cancelled) return
        const nextLogs = Array.isArray(data?.logs) ? data.logs.map((line) => String(line)) : []
        setSelectedTaskLogsCache((prev) => ({ ...prev, [selectedTaskKey]: nextLogs }))
      } catch {
        // Ignore task history fetch errors to keep the log panel responsive.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [selectedTaskKey, statusData?.task?.task_key, taskList])

  const selectedTaskLogs = useMemo(() => {
    const activeKey = String(statusData?.task?.task_key || '').trim()
    if (!selectedTaskKey || selectedTaskKey === activeKey) {
      return filteredLogs
    }
    return (selectedTaskLogsCache[selectedTaskKey] || []).filter((line) => matchesLogFilter(line, logFilter))
  }, [filteredLogs, logFilter, selectedTaskKey, selectedTaskLogsCache, statusData?.task?.task_key])

  const renderQueueMobileCards = (
    data: PipelineAccountItem[],
    titleKey: 'pipeline_status' | 'payment_stage' | 'auth_stage' = 'pipeline_status',
    emptyText = '暂无账号',
  ) => {
    if (data.length === 0) {
      return <Empty description={emptyText} image={Empty.PRESENTED_IMAGE_SIMPLE} />
    }

    return (
      <div className="mobile-card-list">
        {data.map((record) => {
          const stage = accountStageLabel(record)
          const issue = getAccountIssueText(record)
          const isError = Boolean(record.register_error_reason || record.payment_error_reason || record.auth_error_reason)
          const flowStatus = String(record[titleKey] || '')

          return (
            <Card key={queueRowKey(record)} size="small" className="mobile-record-card">
              <div className="mobile-record-head">
                <div className="mobile-record-main">
                  <Text strong className="mobile-record-title" copyable={record.email ? { text: record.email, tooltips: ['复制邮箱', '已复制'] } : false}>
                    {record.email || '-'}
                  </Text>
                  <div className="mobile-record-meta">
                    <Tag color={stage.color}>{stage.label}</Tag>
                    <Tag color={statusColor(record.account_primary_status)}>{statusLabel(record.account_primary_status)}</Tag>
                    <Tag color={statusColor(flowStatus)}>{statusLabel(flowStatus)}</Tag>
                  </div>
                </div>
              </div>

              <div className="mobile-record-section">
                <div className="mobile-record-field">
                  <span className="mobile-record-label">账号 ID</span>
                  <span className="mobile-record-value">#{record.account_id || record.id || '-'}</span>
                </div>
                <div className="mobile-record-field">
                  <span className="mobile-record-label">套餐</span>
                  <span className="mobile-record-value">
                    {record.subscription_plan_confirmed || '-'}
                    {record.subscription_refresh_status === 'failed' ? ' / 待刷新' : ''}
                  </span>
                </div>
                <div className="mobile-record-field">
                  <span className="mobile-record-label">更新时间</span>
                  <span className="mobile-record-value">{formatDateTime(record.updated_at)}</span>
                </div>
                <div className="mobile-record-field">
                  <span className="mobile-record-label">说明</span>
                  <Text className="mobile-record-value" type={isError ? 'danger' : 'secondary'}>
                    {issue || '-'}
                  </Text>
                </div>
              </div>
            </Card>
          )
        })}
      </div>
    )
  }

  return (
    <Space direction="vertical" size={16} style={{ display: 'flex' }}>
      <Card>
        <Space direction="vertical" size={16} style={{ display: 'flex' }}>
          <Row gutter={[16, 16]} align="middle" justify="space-between">
            <Col xs={24} xl={10}>
              <Space direction="vertical" size={6}>
                <Title level={3} style={{ margin: 0 }}>ChatGPT Auto Pipeline</Title>
                <Space wrap size={[8, 8]}>
                  <Tag color={statusColor(task?.status)}>流水线 {statusLabel(task?.status)}</Tag>
                  <Tag color={statusData?.config?.auto_start ? 'success' : 'default'}>
                    自动恢复 {statusData?.config?.auto_start ? '开启' : '关闭'}
                  </Tag>
                  <Tag color={statusData?.config?.enable_auth_capture ? 'processing' : 'default'}>
                    Auth 补抓 {statusData?.config?.enable_auth_capture ? '开启' : '关闭'}
                  </Tag>
                  <Tag>账号总数 {totalAccounts}</Tag>
                </Space>
              </Space>
            </Col>

            <Col xs={24} xl={14}>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 10 }}>
                {stats.map((stat) => (
                  <div
                    key={stat.key}
                    style={{
                      padding: '12px 14px',
                      border: '1px solid rgba(148,163,184,0.24)',
                      borderRadius: 8,
                      background: 'rgba(15,23,42,0.22)',
                    }}
                  >
                    <Text type="secondary">{stat.label}</Text>
                    <Title level={4} style={{ margin: '2px 0 0' }}>{stat.value}</Title>
                  </div>
                ))}
              </div>
            </Col>
          </Row>

          <Alert type={actionHint.type} showIcon message={actionHint.message} description={actionHint.description} />

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 12 }}>
            {stageCards.map((stage, index) => (
              <div
                key={stage.key}
                style={{
                  padding: 14,
                  border: '1px solid rgba(148,163,184,0.28)',
                  borderRadius: 8,
                  background: index === 1 ? 'rgba(124,58,237,0.06)' : 'rgba(15,23,42,0.04)',
                }}
              >
                <Space direction="vertical" size={6} style={{ display: 'flex' }}>
                  <Space wrap style={{ justifyContent: 'space-between', width: '100%' }}>
                    <Text strong>{stage.title}</Text>
                    <Tag color={statusColor(stage.status)}>{statusLabel(stage.status)}</Tag>
                  </Space>
                  <Title level={5} style={{ margin: 0 }}>{stage.metric}</Title>
                  <Text type="secondary" ellipsis={{ tooltip: stage.detail }}>{stage.detail}</Text>
                </Space>
              </div>
            ))}
          </div>

          <Divider style={{ margin: 0 }} />

          <Row gutter={[16, 16]} align="middle" justify="space-between">
            <Col xs={24} xl={15}>
              <Space wrap size={[8, 8]}>
                <Button
                  type="primary"
                  icon={<PlayCircleOutlined />}
                  onClick={() => invokeAction('/pipeline/start', 'start')}
                  loading={actionLoading === 'start'}
                  disabled={isRunning}
                >
                  启动
                </Button>
                <Button
                  icon={<PauseCircleOutlined />}
                  onClick={() => invokeAction('/pipeline/pause', 'pause')}
                  loading={actionLoading === 'pause'}
                  disabled={!task?.status || task?.status === 'stopped'}
                >
                  {isPaused ? '恢复' : '暂停'}
                </Button>
                <Button
                  danger
                  icon={<StopOutlined />}
                  onClick={() => invokeAction('/pipeline/stop', 'stop')}
                  loading={actionLoading === 'stop'}
                  disabled={!task?.status || task?.status === 'stopped'}
                >
                  停止
                </Button>
                <Button icon={<ReloadOutlined />} onClick={() => { loadConfig(); loadStatus() }} loading={configLoading}>
                  刷新
                </Button>
              </Space>
            </Col>

            <Col xs={24} xl={9}>
              <Space wrap size={[8, 8]} style={{ justifyContent: 'flex-end', width: '100%' }}>
                <Tag>注册 {task?.active_register_task_id || '-'}</Tag>
                <Tag>支付 {task?.active_payment_batch_id || '-'}</Tag>
                <Tag>Auth {task?.active_auth_task_id || '-'}</Tag>
              </Space>
            </Col>
          </Row>
        </Space>
      </Card>

      <Tabs
        activeKey={activeTab}
        onChange={(key) => setActiveTab(key as MainTabKey)}
        items={[
          {
            key: 'overview',
            label: '概览',
            children: (
              <Space direction="vertical" size={16} style={{ display: 'flex' }}>
                {task?.last_error ? (
                  <Alert type="warning" showIcon message="最近错误" description={task.last_error} />
                ) : null}

                <Row gutter={[16, 16]}>
                  <Col xs={24} xl={12}>
                    <Card title="任务概况">
                      <Descriptions
                        size="small"
                        column={1}
                        bordered
                        items={[
                          { key: 'taskKey', label: '任务 Key', children: task?.task_key || '-' },
                          { key: 'status', label: '当前状态', children: <Tag color={statusColor(task?.status)}>{statusLabel(task?.status)}</Tag> },
                          { key: 'started', label: '启动时间', children: formatDateTime(task?.started_at) },
                          { key: 'updated', label: '更新时间', children: formatDateTime(task?.updated_at) },
                          { key: 'stopped', label: '停止时间', children: formatDateTime(task?.stopped_at) },
                          { key: 'mailProvider', label: '邮箱提供方', children: statusData?.config?.mail_provider || '-' },
                          { key: 'executor', label: '执行器', children: statusData?.config?.executor_type || '-' },
                          { key: 'captcha', label: '验证码服务', children: statusData?.config?.captcha_solver || '-' },
                        ]}
                      />
                    </Card>
                  </Col>

                  <Col xs={24} xl={12}>
                    <Card title="当前支付批次">
                      {activePaymentBatch ? (
                        <Alert
                          type="info"
                          showIcon
                          message={`batch_id=${activePaymentBatch.task_id || '-'} status=${activePaymentBatch.status || '-'}`}
                          description={activePaymentBatch.message || '当前批次运行中'}
                        />
                      ) : (
                        <Text type="secondary">当前没有活跃的批量支付任务。</Text>
                      )}
                    </Card>
                  </Col>

                  <Col xs={24} xl={12}>
                    <Card title="运行开关">
                      <Space wrap size={[8, 8]}>
                        <Tag color={statusData?.config?.auto_start ? 'success' : 'default'}>
                          自动恢复 {statusData?.config?.auto_start ? '已开启' : '已关闭'}
                        </Tag>
                        <Tag color={statusData?.config?.enable_auth_capture ? 'processing' : 'default'}>
                          Auth 补抓 {statusData?.config?.enable_auth_capture ? '已开启' : '已关闭'}
                        </Tag>
                        <Tag>平台 {statusData?.config?.platform || '-'}</Tag>
                        <Tag>国家 {statusData?.config?.gopay_country || '-'}</Tag>
                        <Tag>币种 {statusData?.config?.gopay_currency || '-'}</Tag>
                      </Space>
                    </Card>
                  </Col>

                  <Col xs={24} xl={12}>
                    <Card title="最近事件">
                      <Space direction="vertical" size={8} style={{ display: 'flex', minHeight: 180 }}>
                        {recentLogLines.length === 0 ? <Text type="secondary">暂无事件</Text> : null}
                        {recentLogLines.map((line, index) => (
                          <Text key={`${index}_${line}`} code style={{ whiteSpace: 'pre-wrap', color: logLineColor(line) }}>{line}</Text>
                        ))}
                      </Space>
                    </Card>
                  </Col>
                </Row>

                {failureGroups.length > 0 ? (
                  <Card title="失败原因分组">
                    <Space wrap size={[8, 8]}>
                      {failureGroups.map((item) => (
                        <Tag key={item.reason} color="error" style={{ padding: '4px 8px', whiteSpace: 'normal' }}>
                          {item.count} 个 · {item.reason}
                        </Tag>
                      ))}
                    </Space>
                  </Card>
                ) : null}

                <Card title="需要关注的账号">
                  {isMobile ? (
                    renderQueueMobileCards(attentionItems, 'pipeline_status', '暂无需要关注的账号')
                  ) : (
                    <Table
                      size="small"
                      rowKey={queueRowKey}
                      columns={queueColumns('pipeline_status')}
                      dataSource={attentionItems}
                      pagination={false}
                      scroll={{ x: 980 }}
                      locale={{ emptyText: '暂无需要关注的账号' }}
                    />
                  )}
                </Card>
              </Space>
            ),
          },
          {
            key: 'config',
            label: '调度配置',
            children: (
              <Card
                title="配置区"
                extra={<Button icon={<ReloadOutlined />} onClick={loadConfig} loading={configLoading}>刷新配置</Button>}
              >
                {isRunning ? (
                  <Alert
                    type="info"
                    showIcon
                    style={{ marginBottom: 16 }}
                    message="流水线运行中，配置暂时锁定"
                    description="暂停或停止后可以保存新的调度参数。"
                  />
                ) : null}
                <Form
                  form={form}
                  layout="vertical"
                  initialValues={DEFAULT_CONFIG}
                  onFinish={saveConfig}
                  disabled={isRunning}
                >
                  <Row gutter={[16, 8]}>
                    <Col xs={24} xl={12}>
                      <Card size="small" title="补货配置">
                        <Row gutter={16}>
                          <Col span={12}>
                            <Form.Item name="payment_pool_threshold" label="待支付池阈值">
                              <InputNumber min={1} style={{ width: '100%' }} />
                            </Form.Item>
                          </Col>
                          <Col span={12}>
                            <Form.Item name="payment_pool_target" label="待支付池目标值">
                              <InputNumber min={1} style={{ width: '100%' }} />
                            </Form.Item>
                          </Col>
                          <Col span={12}>
                            <Form.Item name="register_poll_interval_seconds" label="注册轮询间隔（秒）">
                              <InputNumber min={0} style={{ width: '100%' }} />
                            </Form.Item>
                          </Col>
                        </Row>
                      </Card>
                    </Col>

                    <Col xs={24} xl={12}>
                      <Card size="small" title="支付配置">
                        <Row gutter={16}>
                          <Col span={12}>
                            <Form.Item name="payment_batch_interval_seconds" label="支付批次间隔（秒）">
                              <InputNumber min={0} style={{ width: '100%' }} />
                            </Form.Item>
                          </Col>
                          <Col span={12}>
                            <Form.Item name="payment_batch_max_size" label="批次最大数量">
                              <InputNumber min={0} style={{ width: '100%' }} />
                            </Form.Item>
                          </Col>
                          <Col span={12}>
                            <Form.Item name="gopay_batch_poll_interval_seconds" label="GoPay 轮询间隔（秒）">
                              <InputNumber min={0} style={{ width: '100%' }} />
                            </Form.Item>
                          </Col>
                          <Col span={12}>
                            <Form.Item name="gopay_timeout_seconds" label="GoPay 超时（秒）">
                              <InputNumber min={0} style={{ width: '100%' }} />
                            </Form.Item>
                          </Col>
                        </Row>
                      </Card>
                    </Col>

                    <Col xs={24} xl={12}>
                      <Card size="small" title="Auth 配置">
                        <Row gutter={16}>
                          <Col span={12}>
                            <Form.Item
                              name="enable_auth_capture"
                              valuePropName="checked"
                              label="支付后自动补抓 Auth"
                              extra="默认关闭。开启后，支付成功账号会进入单线程顺序补抓。"
                            >
                              <Checkbox>启用 Auth 补抓</Checkbox>
                            </Form.Item>
                          </Col>
                          <Col span={12}>
                            <Form.Item name="auth_poll_interval_seconds" label="Auth 轮询间隔（秒）">
                              <InputNumber min={0} style={{ width: '100%' }} />
                            </Form.Item>
                          </Col>
                        </Row>
                      </Card>
                    </Col>

                    <Col xs={24} xl={12}>
                      <Card size="small" title="运行配置">
                        <Row gutter={16}>
                          <Col span={12}>
                            <Form.Item
                              name="auto_start"
                              valuePropName="checked"
                              label="服务启动自动恢复"
                              extra="开启后，服务启动时会按最新流水线任务状态恢复并继续调度。"
                            >
                              <Checkbox>启用 auto_start</Checkbox>
                            </Form.Item>
                          </Col>
                          <Col span={12}>
                            <Form.Item name="platform" label="平台">
                              <Input disabled />
                            </Form.Item>
                          </Col>
                          <Col span={12}>
                            <Form.Item name="mail_provider" label="mail_provider">
                              <Input disabled />
                            </Form.Item>
                          </Col>
                          <Col span={12}>
                            <Form.Item name="executor_type" label="executor_type">
                              <Input disabled />
                            </Form.Item>
                          </Col>
                          <Col span={12}>
                            <Form.Item name="captcha_solver" label="captcha_solver">
                              <Input disabled />
                            </Form.Item>
                          </Col>
                          <Col span={12}>
                            <Form.Item name="gopay_country" label="GoPay 国家">
                              <Input disabled />
                            </Form.Item>
                          </Col>
                          <Col span={12}>
                            <Form.Item name="gopay_currency" label="GoPay 币种">
                              <Input disabled />
                            </Form.Item>
                          </Col>
                        </Row>
                      </Card>
                    </Col>
                  </Row>

                  <Space style={{ marginTop: 16 }}>
                    <Button type="primary" htmlType="submit" loading={actionLoading === 'save'} disabled={isRunning}>
                      保存配置
                    </Button>
                  </Space>
                </Form>
              </Card>
            ),
          },
          {
            key: 'queues',
            label: '账号队列',
            children: (
              <Card>
                <Space direction="vertical" size={16} style={{ display: 'flex' }}>
                  <Row gutter={[12, 12]} justify="space-between" align="middle">
                    <Col xs={24} xl={14}>
                      <Space wrap>
                        <Input
                          allowClear
                          prefix={<SearchOutlined />}
                          placeholder="搜索邮箱、账号 ID、状态、失败原因"
                          value={queueSearch}
                          onChange={(event) => setQueueSearch(event.target.value)}
                          style={{ width: 320, maxWidth: '100%' }}
                        />
                        <Checkbox checked={queueErrorOnly} onChange={(event) => setQueueErrorOnly(event.target.checked)}>
                          只看有错误原因的账号
                        </Checkbox>
                      </Space>
                    </Col>
                    <Col xs={24} xl={10}>
                      <Space wrap style={{ justifyContent: 'flex-end', width: '100%' }}>
                        {stats.map((stat) => (
                          <Tag key={stat.key} color={stat.color}>
                            {stat.label}: {stat.value}
                          </Tag>
                        ))}
                      </Space>
                    </Col>
                  </Row>

                  <Segmented
                    value={queueTab}
                    onChange={(value) => setQueueTab(value as QueueTabKey)}
                    options={[
                      { value: 'pending_payment', label: `待支付 ${queues.pending_payment.length}` },
                      { value: 'paid', label: `已支付 ${queues.paid.length}` },
                      { value: 'failed', label: `失败 ${queues.failed.length}` },
                      { value: 'auth_pending', label: `待补抓 ${queues.auth_pending.length}` },
                    ]}
                  />

                  {queueTab === 'failed' && failureGroups.length > 0 ? (
                    <Alert
                      type="warning"
                      showIcon
                      message="失败原因分组"
                      description={
                        <Space wrap size={[8, 8]}>
                          {failureGroups.map((item) => (
                            <Tag key={item.reason} color="error" style={{ whiteSpace: 'normal' }}>
                              {item.count} 个 · {item.reason}
                            </Tag>
                          ))}
                        </Space>
                      }
                    />
                  ) : null}

                  {isMobile ? (
                    <>
                      {renderQueueMobileCards(
                        mobileQueueData,
                        queueTab === 'auth_pending' ? 'auth_stage' : queueTab === 'pending_payment' ? 'pipeline_status' : 'payment_stage',
                        '暂无账号',
                      )}
                      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                        <Pagination
                          size="small"
                          current={queueMobilePage}
                          pageSize={queueMobilePageSize}
                          total={filteredQueueData.length}
                          showSizeChanger={false}
                          onChange={setQueueMobilePage}
                        />
                      </div>
                    </>
                  ) : (
                    <Table
                      rowKey={queueRowKey}
                      columns={queueColumnMap[queueTab]}
                      dataSource={filteredQueueData}
                      pagination={{ pageSize: 10, showSizeChanger: false }}
                      scroll={{ x: 980 }}
                    />
                  )}
                </Space>
              </Card>
            ),
          },
          {
            key: 'logs',
            label: '运行日志',
            children: (
              <Card>
                <Row gutter={[16, 16]}>
                  <Col xs={24} xl={7}>
                    <Card
                      size="small"
                      title="任务列表"
                      extra={<Text type="secondary">账号数 {totalAccounts}</Text>}
                    >
                      <List
                        size="small"
                        dataSource={taskList}
                        locale={{ emptyText: '暂无任务' }}
                        renderItem={(item) => {
                          const taskKey = String(item.task_key || '').trim()
                          const active = taskKey === selectedTaskKey || (!selectedTaskKey && taskKey === String(statusData?.task?.task_key || '').trim())
                          return (
                            <List.Item
                              style={{
                                cursor: 'pointer',
                                padding: '10px 12px',
                                borderRadius: 10,
                                background: active ? 'rgba(22,119,255,0.14)' : 'transparent',
                              }}
                              onClick={() => setSelectedTaskKey(taskKey)}
                            >
                              <Space direction="vertical" size={2} style={{ width: '100%' }}>
                                <Space wrap>
                                  <Text strong>{taskKey || '-'}</Text>
                                  <Tag color={statusColor(item.status)}>{statusLabel(item.status)}</Tag>
                                </Space>
                                <Text type="secondary">{formatDateTime(item.updated_at)}</Text>
                              </Space>
                            </List.Item>
                          )
                        }}
                      />
                    </Card>
                  </Col>

                  <Col xs={24} xl={17}>
                    <Space direction="vertical" size={16} style={{ display: 'flex' }}>
                      <Row gutter={[12, 12]} justify="space-between" align="middle">
                        <Col xs={24} xl={14}>
                          <Segmented
                            value={logFilter}
                            onChange={(value) => setLogFilter(value as LogFilterKey)}
                            options={[
                              { value: 'all', label: '全部日志' },
                              { value: 'errors', label: '错误' },
                              { value: 'child', label: '子任务' },
                            ]}
                          />
                        </Col>
                        <Col xs={24} xl={10}>
                          <Space wrap style={{ justifyContent: 'flex-end', width: '100%' }}>
                            <Text type="secondary">
                              账号数 {totalAccounts} / 日志 {selectedTaskLogs.length}
                            </Text>
                            <Button icon={<ClearOutlined />} onClick={() => setLogs([])}>
                              清空前端显示
                            </Button>
                          </Space>
                        </Col>
                      </Row>

                      <Space direction="vertical" size={4} style={{ display: 'flex', maxHeight: 520, overflow: 'auto' }}>
                        {selectedTaskLogs.length === 0 ? <Text type="secondary">暂无日志</Text> : null}
                        {selectedTaskLogs.map((line, index) => (
                          <Text key={`${index}_${line}`} code style={{ whiteSpace: 'pre-wrap', color: logLineColor(line) }}>{line}</Text>
                        ))}
                      </Space>
                    </Space>
                  </Col>
                </Row>
              </Card>
            ),
          },
        ]}
      />
    </Space>
  )
}
