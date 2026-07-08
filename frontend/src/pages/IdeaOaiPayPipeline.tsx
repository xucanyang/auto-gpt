import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Grid,
  Input,
  InputNumber,
  Row,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
  theme,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  PauseCircleOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  StopOutlined,
} from '@ant-design/icons'
import { Link } from 'react-router-dom'

import TaskLogPanel from '@/components/TaskLogPanel'
import { apiFetch } from '@/lib/utils'

const { Text, Title } = Typography
const { TextArea } = Input

type PipelineTask = {
  id?: number
  task_key?: string
  status?: string
  source_type?: string
  target_success_count?: number
  active_register_task_id?: string
  active_idea_task_id?: string
  active_phone_task_id?: string
  last_error?: string
  started_at?: string
  stopped_at?: string
  updated_at?: string
}

type PipelineItem = {
  id?: number
  account_id?: number
  email?: string
  source_stage?: string
  register_stage?: string
  idea_stage?: string
  check_stage?: string
  gate_stage?: string
  phone_stage?: string
  oaipay_stage?: string
  overall_status?: string
  subscription_type_before?: string
  subscription_type_after?: string
  account_validity?: string
  cdk_id?: number
  cdk_masked?: string
  idea_task_id?: string
  idea_order_id?: string
  idea_display_id?: string
  idea_error?: string
  phone_task_id?: string
  phone_policy?: string
  phone_error?: string
  oaipay_remote_state?: string
  oaipay_remote_account_id?: string
  oaipay_message?: string
  last_error?: string
  updated_at?: string
}

type PipelineSummary = {
  total?: number
  done?: number
  failed?: number
  manual_required?: number
  running?: number
  registered?: number
  idea_paid?: number
  check_pass?: number
  phone_success?: number
  oaipay_success?: number
}

type PipelineStatusResponse = {
  task: PipelineTask | null
  config?: PipelineConfig
  summary?: PipelineSummary
  items?: PipelineItem[]
  logs?: string[]
  history?: PipelineTask[]
}

type OaipayCategory = {
  id: number
  name: string
  description?: string
  stock?: number
  account_total?: number
}

type PipelineConfig = {
  source: {
    type: 'local' | 'register'
    account_ids: number[]
    all_filtered: boolean
    email: string
    status: string
    manually_used?: string | null
    auth_type: string
    subscription_type: string
    account_validity: string
    sub2api_state: string
    oaipay_state: string
    limit: number
    target_count: number
    register: Record<string, unknown>
  }
  idea: {
    enabled: boolean
    use_pool: boolean
    code_lines: string
    precheck: boolean
    failure_continue: boolean
    submit_interval_seconds: number
    auto_poll_status: boolean
    status_poll_interval_seconds: number
    status_poll_timeout_seconds: number
    skip_if_subscription_in: string[]
  }
  check: {
    enabled: boolean
    gate: {
      enabled: boolean
      mode: 'none' | 'account_valid' | 'subscription_in' | 'upload_ready'
      allowed_subscription_types: string[]
    }
  }
  phone: {
    policy: 'disabled' | 'best_effort' | 'required'
    apply_to: 'gate_passed' | 'all' | 'free' | 'plus'
    use_pool: boolean
    phone_lines: string
    timeout_seconds: number
    poll_interval_seconds: number
    max_resend_attempts: number
    resend_interval_seconds: number
    account_interval_seconds: number
    proxy?: string | null
    proxy_mode: string
    proxy_country_code: string
    proxy_failover: boolean
    proxy_max_candidates: number
    proxy_min_score: number
  }
  oaipay: {
    enabled: boolean
    category_id?: number | null
    exists_as_success: boolean
    require_phone_bound: boolean
    require_subscription_in: string[]
  }
  auto_start: boolean
  tick_interval_seconds: number
}

type FilterKey = 'all' | 'attention' | 'running' | 'done'

const DEFAULT_CONFIG: PipelineConfig = {
  source: {
    type: 'local',
    account_ids: [],
    all_filtered: false,
    email: '',
    status: '',
    manually_used: null,
    auth_type: '',
    subscription_type: '',
    account_validity: '',
    sub2api_state: '',
    oaipay_state: '',
    limit: 50,
    target_count: 0,
    register: {
      batch_size: 5,
      concurrency: 1,
      mail_provider: '',
      executor_type: 'protocol',
      captcha_solver: 'yescaptcha',
    },
  },
  idea: {
    enabled: true,
    use_pool: true,
    code_lines: '',
    precheck: true,
    failure_continue: true,
    submit_interval_seconds: 5,
    auto_poll_status: true,
    status_poll_interval_seconds: 5,
    status_poll_timeout_seconds: 1800,
    skip_if_subscription_in: ['plus', 'pro', 'team', 'enterprise'],
  },
  check: {
    enabled: true,
    gate: {
      enabled: true,
      mode: 'account_valid',
      allowed_subscription_types: [],
    },
  },
  phone: {
    policy: 'disabled',
    apply_to: 'gate_passed',
    use_pool: true,
    phone_lines: '',
    timeout_seconds: 180,
    poll_interval_seconds: 5,
    max_resend_attempts: 0,
    resend_interval_seconds: 30,
    account_interval_seconds: 60,
    proxy: '',
    proxy_mode: 'pool',
    proxy_country_code: '',
    proxy_failover: true,
    proxy_max_candidates: 0,
    proxy_min_score: 0,
  },
  oaipay: {
    enabled: false,
    category_id: null,
    exists_as_success: true,
    require_phone_bound: false,
    require_subscription_in: [],
  },
  auto_start: false,
  tick_interval_seconds: 3,
}

const STATUS_META: Record<string, { label: string; color: string }> = {
  running: { label: '运行中', color: 'processing' },
  paused: { label: '已暂停', color: 'warning' },
  stopped: { label: '已停止', color: 'default' },
  done: { label: '已完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
  pending: { label: '等待', color: 'default' },
  disabled: { label: '关闭', color: 'default' },
  skipped: { label: '跳过', color: 'warning' },
  success: { label: '成功', color: 'success' },
  manual_required: { label: '需人工', color: 'warning' },
  selected: { label: '本地选择', color: 'blue' },
  registered: { label: '注册来源', color: 'blue' },
  submitting: { label: '提交中', color: 'processing' },
  polling: { label: '轮询中', color: 'processing' },
  paid: { label: 'Idea paid', color: 'success' },
  timeout: { label: '超时', color: 'warning' },
  refreshed: { label: '已刷新', color: 'success' },
  pass: { label: '放行', color: 'success' },
  blocked: { label: '阻断', color: 'error' },
  probing: { label: '探测中', color: 'processing' },
  uploaded: { label: '已上传', color: 'success' },
  exists: { label: '远端已存在', color: 'success' },
  ambiguous: { label: '多候选', color: 'warning' },
}

function normalized(value?: string) {
  return String(value || '').trim().toLowerCase()
}

function statusLabel(value?: string) {
  const key = normalized(value)
  return STATUS_META[key]?.label || value || '-'
}

function statusColor(value?: string) {
  return STATUS_META[normalized(value)]?.color || 'default'
}

function StatusTag({ value }: { value?: string }) {
  return <Tag color={statusColor(value)}>{statusLabel(value)}</Tag>
}

function parseIdLines(raw: string): number[] {
  const seen = new Set<number>()
  String(raw || '')
    .split(/[\s,，]+/)
    .forEach((part) => {
      const id = Number(part.trim())
      if (Number.isInteger(id) && id > 0) seen.add(id)
    })
  return [...seen]
}

function optionalNumber(value: unknown): number | null {
  const text = String(value ?? '').trim()
  if (!text) return null
  const num = Number(text)
  return Number.isFinite(num) ? num : null
}

function objectRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function firstText(...values: unknown[]): string {
  for (const value of values) {
    const text = String(value ?? '').trim()
    if (text) return text
  }
  return ''
}

function firstNumber(...values: unknown[]): number | undefined {
  for (const value of values) {
    const num = Number(value)
    if (Number.isFinite(num)) return num
  }
  return undefined
}

function extractOaipayCategories(payload: unknown): OaipayCategory[] {
  const root = objectRecord(payload)
  const data = objectRecord(root?.data)
  const rawItems =
    (Array.isArray(payload) ? payload : null)
    || (Array.isArray(root?.categories) ? root.categories : null)
    || (Array.isArray(root?.data) ? root.data : null)
    || (Array.isArray(root?.items) ? root.items : null)
    || (Array.isArray(data?.categories) ? data.categories : null)
    || (Array.isArray(data?.items) ? data.items : null)
    || []

  const seen = new Set<number>()
  return rawItems.flatMap((raw) => {
    const item = objectRecord(raw)
    if (!item) return []
    const id = firstNumber(item.id, item.category_id, item.value)
    if (!id || seen.has(id)) return []
    seen.add(id)
    return [{
      id,
      name: firstText(item.name, item.title, item.label, `分组 ${id}`),
      description: firstText(item.description),
      stock: firstNumber(item.stock, item.account_in_stock),
      account_total: firstNumber(item.account_total, item.total),
    }]
  })
}

function formatOaipayCategoryLabel(category: OaipayCategory): string {
  const stats: string[] = []
  if (typeof category.stock === 'number') stats.push(`库存 ${category.stock}`)
  if (typeof category.account_total === 'number') stats.push(`总数 ${category.account_total}`)
  const suffix = stats.length ? ` · ${stats.join(' / ')}` : ''
  return `#${category.id} ${category.name}${suffix}`
}

function formatDateTime(value?: string) {
  if (!value) return '-'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function itemIssue(record: PipelineItem) {
  return String(
    record.last_error
    || record.idea_error
    || record.phone_error
    || record.oaipay_message
    || '',
  ).trim()
}

function rowKey(record: PipelineItem) {
  return String(record.id || record.account_id || record.email || record.updated_at || Math.random())
}

function buildConfigFromValues(values: Record<string, unknown>): PipelineConfig {
  const sourceType = String(values.source_type || 'local') as 'local' | 'register'
  const gateMode = String(values.gate_mode || 'account_valid') as PipelineConfig['check']['gate']['mode']
  const phonePolicy = String(values.phone_policy || 'disabled') as PipelineConfig['phone']['policy']
  const phoneApplyTo = String(values.phone_apply_to || 'gate_passed') as PipelineConfig['phone']['apply_to']
  const accountIds = parseIdLines(String(values.account_ids_text || ''))
  return {
    source: {
      ...DEFAULT_CONFIG.source,
      type: sourceType,
      account_ids: accountIds,
      all_filtered: Boolean(values.all_filtered),
      email: String(values.email || ''),
      status: String(values.status || ''),
      subscription_type: String(values.subscription_type || ''),
      account_validity: String(values.account_validity || ''),
      oaipay_state: String(values.oaipay_state || ''),
      limit: Number(values.limit || 0),
      target_count: Number(values.target_count || 0),
      register: {
        ...DEFAULT_CONFIG.source.register,
        batch_size: Number(values.register_batch_size || 5),
        concurrency: Number(values.register_concurrency || 1),
        mail_provider: String(values.mail_provider || ''),
        proxy_mode: String(values.register_proxy_mode || ''),
        proxy: String(values.register_proxy || ''),
        proxy_country_code: String(values.register_proxy_country_code || ''),
      },
    },
    idea: {
      ...DEFAULT_CONFIG.idea,
      enabled: Boolean(values.idea_enabled),
      use_pool: Boolean(values.idea_use_pool),
      code_lines: String(values.code_lines || ''),
      precheck: Boolean(values.idea_precheck),
      failure_continue: Boolean(values.idea_failure_continue),
      submit_interval_seconds: Number(values.submit_interval_seconds || 0),
      status_poll_interval_seconds: Number(values.status_poll_interval_seconds || 5),
      status_poll_timeout_seconds: Number(values.status_poll_timeout_seconds || 1800),
      skip_if_subscription_in: Array.isArray(values.idea_skip_subs) ? values.idea_skip_subs as string[] : [],
    },
    check: {
      enabled: Boolean(values.check_enabled),
      gate: {
        enabled: Boolean(values.gate_enabled),
        mode: gateMode,
        allowed_subscription_types: Array.isArray(values.allowed_subscription_types) ? values.allowed_subscription_types as string[] : [],
      },
    },
    phone: {
      ...DEFAULT_CONFIG.phone,
      policy: phonePolicy,
      apply_to: phoneApplyTo,
      use_pool: Boolean(values.phone_use_pool),
      phone_lines: String(values.phone_lines || ''),
      timeout_seconds: Number(values.phone_timeout_seconds || 180),
      poll_interval_seconds: Number(values.phone_poll_interval_seconds || 5),
      proxy_mode: String(values.phone_proxy_mode || 'pool'),
      proxy: String(values.phone_proxy || ''),
      proxy_country_code: String(values.phone_proxy_country_code || ''),
      proxy_failover: Boolean(values.phone_proxy_failover),
    },
    oaipay: {
      enabled: Boolean(values.oaipay_enabled),
      category_id: optionalNumber(values.oaipay_category_id),
      exists_as_success: Boolean(values.oaipay_exists_as_success),
      require_phone_bound: Boolean(values.oaipay_require_phone_bound),
      require_subscription_in: Array.isArray(values.oaipay_require_subscription_in) ? values.oaipay_require_subscription_in as string[] : [],
    },
    auto_start: false,
    tick_interval_seconds: Number(values.tick_interval_seconds || 3),
  }
}

function initialFormValues(config?: PipelineConfig) {
  const cfg = config || DEFAULT_CONFIG
  return {
    source_type: cfg.source.type,
    account_ids_text: (cfg.source.account_ids || []).join('\n'),
    all_filtered: cfg.source.all_filtered,
    email: cfg.source.email,
    status: cfg.source.status,
    subscription_type: cfg.source.subscription_type,
    account_validity: cfg.source.account_validity,
    oaipay_state: cfg.source.oaipay_state,
    limit: cfg.source.limit || 50,
    target_count: cfg.source.target_count || 0,
    register_batch_size: Number(cfg.source.register?.batch_size || 5),
    register_concurrency: Number(cfg.source.register?.concurrency || 1),
    mail_provider: String(cfg.source.register?.mail_provider || ''),
    register_proxy_mode: String(cfg.source.register?.proxy_mode || ''),
    register_proxy: String(cfg.source.register?.proxy || ''),
    register_proxy_country_code: String(cfg.source.register?.proxy_country_code || ''),
    idea_enabled: cfg.idea.enabled,
    idea_use_pool: cfg.idea.use_pool,
    code_lines: cfg.idea.code_lines,
    idea_precheck: cfg.idea.precheck,
    idea_failure_continue: cfg.idea.failure_continue,
    submit_interval_seconds: cfg.idea.submit_interval_seconds,
    status_poll_interval_seconds: cfg.idea.status_poll_interval_seconds,
    status_poll_timeout_seconds: cfg.idea.status_poll_timeout_seconds,
    idea_skip_subs: cfg.idea.skip_if_subscription_in,
    check_enabled: cfg.check.enabled,
    gate_enabled: cfg.check.gate.enabled,
    gate_mode: cfg.check.gate.mode,
    allowed_subscription_types: cfg.check.gate.allowed_subscription_types,
    phone_policy: cfg.phone.policy,
    phone_apply_to: cfg.phone.apply_to,
    phone_use_pool: cfg.phone.use_pool,
    phone_lines: cfg.phone.phone_lines,
    phone_timeout_seconds: cfg.phone.timeout_seconds,
    phone_poll_interval_seconds: cfg.phone.poll_interval_seconds,
    phone_proxy_mode: cfg.phone.proxy_mode,
    phone_proxy: cfg.phone.proxy || '',
    phone_proxy_country_code: cfg.phone.proxy_country_code,
    phone_proxy_failover: cfg.phone.proxy_failover,
    oaipay_enabled: cfg.oaipay.enabled,
    oaipay_category_id: cfg.oaipay.category_id,
    oaipay_exists_as_success: cfg.oaipay.exists_as_success,
    oaipay_require_phone_bound: cfg.oaipay.require_phone_bound,
    oaipay_require_subscription_in: cfg.oaipay.require_subscription_in,
    tick_interval_seconds: cfg.tick_interval_seconds,
  }
}

export default function IdeaOaiPayPipelinePage() {
  const { message } = App.useApp()
  const { token } = theme.useToken()
  const screens = Grid.useBreakpoint()
  const [form] = Form.useForm()
  const [statusData, setStatusData] = useState<PipelineStatusResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [actionLoading, setActionLoading] = useState('')
  const [filter, setFilter] = useState<FilterKey>('all')
  const [search, setSearch] = useState('')
  const [logTaskId, setLogTaskId] = useState('')
  const [oaipayCategories, setOaipayCategories] = useState<OaipayCategory[]>([])
  const [oaipayCategoryLoading, setOaipayCategoryLoading] = useState(false)
  const [oaipayCategoryLoaded, setOaipayCategoryLoaded] = useState(false)
  const [oaipayCategoryError, setOaipayCategoryError] = useState('')
  const oaipayUploadEnabled = Form.useWatch('oaipay_enabled', form)
  const isMobile = screens.md === false

  const loadOaipayCategories = useCallback(async (options: { force?: boolean; silent?: boolean } = {}) => {
    if (!options.force && (oaipayCategoryLoaded || oaipayCategoryLoading)) return
    setOaipayCategoryLoading(true)
    setOaipayCategoryError('')
    try {
      const res = await apiFetch('/integrations/oaipay-categories')
      const root = objectRecord(res)
      const errorText = firstText(root?.error, root?.detail, root?.message)
      if (root?.success === false && errorText) {
        throw new Error(errorText)
      }
      const categories = extractOaipayCategories(res)
      setOaipayCategories(categories)
      setOaipayCategoryLoaded(true)
      if (!options.silent) {
        message.success(categories.length ? `已获取 ${categories.length} 个 OAIPay 分组` : 'OAIPay 没有返回可选分组，可继续留空使用自动分类')
      }
    } catch (error) {
      const text = (error as Error).message || String(error)
      setOaipayCategoryError(text)
      setOaipayCategoryLoaded(false)
      if (!options.silent) message.error(`获取 OAIPay 分组失败: ${text}`)
    } finally {
      setOaipayCategoryLoading(false)
    }
  }, [message, oaipayCategoryLoaded, oaipayCategoryLoading])

  const loadStatus = async (options: { syncForm?: boolean } = {}) => {
    setLoading(true)
    try {
      const data = await apiFetch('/idea-oaipay-pipeline/status?item_limit=1000') as PipelineStatusResponse
      setStatusData(data)
      if (options.syncForm && data.config) {
        form.setFieldsValue(initialFormValues(data.config))
      }
    } catch (error) {
      message.error(`读取流水线状态失败: ${(error as Error).message}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadStatus({ syncForm: true })
    const timer = window.setInterval(() => loadStatus(), 3000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    if (oaipayUploadEnabled) {
      void loadOaipayCategories({ silent: true })
    }
  }, [loadOaipayCategories, oaipayUploadEnabled])

  const task = statusData?.task || null
  const summary = statusData?.summary || {}
  const items = statusData?.items || []
  const logs = statusData?.logs || []
  const isRunning = normalized(task?.status) === 'running'
  const isPaused = normalized(task?.status) === 'paused'

  const stats = [
    { key: 'total', label: '账号', value: summary.total || 0, detail: '本次快照/注册回收账号' },
    { key: 'idea', label: 'Idea paid', value: summary.idea_paid || 0, detail: '上游已确认开通' },
    { key: 'gate', label: '状态放行', value: summary.check_pass || 0, detail: '本地刷新后通过 gate' },
    { key: 'phone', label: '手机号成功', value: summary.phone_success || 0, detail: '真实绑定成功' },
    { key: 'oaipay', label: 'OAIPay 成功', value: summary.oaipay_success || 0, detail: '上传或远端已存在' },
    { key: 'attention', label: '需关注', value: (summary.failed || 0) + (summary.manual_required || 0), detail: '失败或人工处理' },
  ]

  const filteredItems = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    return items.filter((item) => {
      if (keyword) {
        const haystack = `${item.email || ''} ${item.account_id || ''} ${item.last_error || ''}`.toLowerCase()
        if (!haystack.includes(keyword)) return false
      }
      if (filter === 'attention') return Boolean(item.last_error) || ['failed', 'manual_required'].includes(normalized(item.overall_status))
      if (filter === 'running') return normalized(item.overall_status) === 'running'
      if (filter === 'done') return normalized(item.overall_status) === 'done'
      return true
    })
  }, [filter, items, search])

  const invokeAction = async (path: string, key: string) => {
    setActionLoading(key)
    try {
      await apiFetch(path, { method: 'POST' })
      await loadStatus()
      message.success('操作已提交')
    } catch (error) {
      message.error(`操作失败: ${(error as Error).message}`)
    } finally {
      setActionLoading('')
    }
  }

  const handleStart = async () => {
    const values = await form.validateFields()
    const config = buildConfigFromValues(values)
    setActionLoading('start')
    try {
      await apiFetch('/idea-oaipay-pipeline/start', {
        method: 'POST',
        body: JSON.stringify({ config }),
      })
      await loadStatus({ syncForm: true })
      message.success('流水线已启动')
    } catch (error) {
      message.error(`启动失败: ${(error as Error).message}`)
    } finally {
      setActionLoading('')
    }
  }

  const retryStage = async (record: PipelineItem, stage: string) => {
    if (!record.id) return
    setActionLoading(`${record.id}_${stage}`)
    try {
      await apiFetch(`/idea-oaipay-pipeline/items/${record.id}/retry/${stage}`, { method: 'POST' })
      await loadStatus()
      message.success('已重置阶段')
    } catch (error) {
      message.error(`重试失败: ${(error as Error).message}`)
    } finally {
      setActionLoading('')
    }
  }

  const columns: ColumnsType<PipelineItem> = [
    {
      title: '账号',
      dataIndex: 'email',
      fixed: isMobile ? undefined : 'left',
      width: 220,
      render: (value: string, record) => (
        <Space direction="vertical" size={0}>
          <Text>{value || '-'}</Text>
          <Text type="secondary">#{record.account_id || '-'}</Text>
        </Space>
      ),
    },
    { title: '来源', width: 120, render: (_, record) => <Space size={4} wrap><StatusTag value={record.source_stage} /><StatusTag value={record.register_stage} /></Space> },
    { title: 'Idea', width: 180, render: (_, record) => <StageCell stage={record.idea_stage} sub={record.idea_order_id || record.cdk_masked || record.idea_error} /> },
    { title: '状态刷新', width: 160, render: (_, record) => <StageCell stage={record.check_stage} sub={`${record.subscription_type_before || '-'} → ${record.subscription_type_after || '-'}`} /> },
    { title: 'Gate', width: 130, render: (_, record) => <StageCell stage={record.gate_stage} sub={record.account_validity || '-'} /> },
    { title: '手机号', width: 150, render: (_, record) => <StageCell stage={record.phone_stage} sub={record.phone_task_id || record.phone_error} /> },
    { title: 'OAIPay', width: 170, render: (_, record) => <StageCell stage={record.oaipay_stage} sub={record.oaipay_remote_state || record.oaipay_message} /> },
    { title: '整体', width: 110, render: (_, record) => <StatusTag value={record.overall_status} /> },
    {
      title: '错误/备注',
      dataIndex: 'last_error',
      width: 260,
      ellipsis: true,
      render: (_, record) => <Text type={itemIssue(record) ? 'danger' : 'secondary'} ellipsis={{ tooltip: itemIssue(record) }}>{itemIssue(record) || '-'}</Text>,
    },
    { title: '更新时间', width: 170, render: (_, record) => <Text type="secondary">{formatDateTime(record.updated_at)}</Text> },
    {
      title: '操作',
      fixed: isMobile ? undefined : 'right',
      width: 210,
      render: (_, record) => (
        <Space wrap size={[4, 4]}>
          {record.idea_task_id ? <Button size="small" onClick={() => setLogTaskId(String(record.idea_task_id))}>Idea日志</Button> : null}
          {record.phone_task_id ? <Button size="small" onClick={() => setLogTaskId(String(record.phone_task_id))}>手机号日志</Button> : null}
          <Button size="small" loading={actionLoading === `${record.id}_check`} onClick={() => retryStage(record, 'check')}>重刷</Button>
          <Button size="small" loading={actionLoading === `${record.id}_oaipay`} onClick={() => retryStage(record, 'oaipay')}>传OAIPay</Button>
        </Space>
      ),
    },
  ]

  return (
    <Space direction="vertical" size={16} style={{ display: 'flex' }}>
      <Card>
        <Space direction="vertical" size={14} style={{ display: 'flex' }}>
          <Row gutter={[16, 16]} align="middle" justify="space-between">
            <Col xs={24} lg={10}>
              <Space direction="vertical" size={8}>
                <Title level={3} style={{ margin: 0 }}>账号处理流水线</Title>
                <Space wrap size={[8, 8]}>
                  <StatusTag value={task?.status || 'stopped'} />
                  <Tag color="blue">来源 {task?.source_type || '-'}</Tag>
                  <Tag>目标 {task?.target_success_count || '-'}</Tag>
                  <Tag>账号 {summary.total || 0}</Tag>
                </Space>
              </Space>
            </Col>
            <Col xs={24} lg={14}>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(115px, 1fr))', gap: 10 }}>
                {stats.map((stat) => (
                  <button
                    key={stat.key}
                    type="button"
                    onClick={() => stat.key === 'attention' ? setFilter('attention') : undefined}
                    style={{
                      textAlign: 'left',
                      padding: '10px 12px',
                      border: `1px solid ${token.colorBorderSecondary}`,
                      borderRadius: 8,
                      background: token.colorFillAlter,
                      cursor: stat.key === 'attention' ? 'pointer' : 'default',
                    }}
                  >
                    <Text type="secondary">{stat.label}</Text>
                    <Title level={4} style={{ margin: '2px 0 0' }}>{stat.value}</Title>
                    <Text type="secondary" style={{ fontSize: 12 }}>{stat.detail}</Text>
                  </button>
                ))}
              </div>
            </Col>
          </Row>

          {task?.last_error ? <Alert type="warning" showIcon message="最近错误" description={task.last_error} /> : null}

          <Row gutter={[12, 12]} justify="space-between" align="middle">
            <Col xs={24} lg={12}>
              <Space wrap size={[8, 8]}>
                <Button type="primary" icon={<PlayCircleOutlined />} onClick={handleStart} loading={actionLoading === 'start'} disabled={isRunning || isPaused}>启动新流水线</Button>
                <Button icon={<PauseCircleOutlined />} onClick={() => invokeAction(isPaused ? '/idea-oaipay-pipeline/resume' : '/idea-oaipay-pipeline/pause', 'pause')} loading={actionLoading === 'pause'} disabled={!task || normalized(task.status) === 'stopped'}>{isPaused ? '恢复' : '暂停'}</Button>
                <Button danger icon={<StopOutlined />} onClick={() => invokeAction('/idea-oaipay-pipeline/stop', 'stop')} loading={actionLoading === 'stop'} disabled={!task || normalized(task.status) === 'stopped'}>停止</Button>
                <Button icon={<ReloadOutlined />} onClick={() => loadStatus()} loading={loading}>刷新</Button>
              </Space>
            </Col>
            <Col xs={24} lg={12}>
              <Space wrap size={[6, 6]} style={{ justifyContent: 'flex-end', width: '100%' }}>
                <Tag>注册 {task?.active_register_task_id || '-'}</Tag>
                <Tag>Idea {task?.active_idea_task_id || '-'}</Tag>
                <Tag>手机号 {task?.active_phone_task_id || '-'}</Tag>
              </Space>
            </Col>
          </Row>
        </Space>
      </Card>

      <Tabs
        items={[
          { key: 'overview', label: '概览', children: <OverviewPanel task={task} items={items} logs={logs} /> },
          {
            key: 'config',
            label: '配置',
            children: (
              <ConfigPanel
                form={form}
                oaipayCategories={oaipayCategories}
                oaipayCategoryLoading={oaipayCategoryLoading}
                oaipayCategoryError={oaipayCategoryError}
                onLoadOaipayCategories={loadOaipayCategories}
              />
            ),
          },
          {
            key: 'items',
            label: '账号明细',
            children: (
              <Card>
                <Space direction="vertical" size={12} style={{ display: 'flex' }}>
                  <Row gutter={[8, 8]} justify="space-between">
                    <Col xs={24} md={12}>
                      <Input.Search placeholder="搜索邮箱 / account_id / 错误" allowClear value={search} onChange={(event) => setSearch(event.target.value)} />
                    </Col>
                    <Col xs={24} md={12}>
                      <Segmented
                        block={isMobile}
                        value={filter}
                        onChange={(value) => setFilter(value as FilterKey)}
                        options={[
                          { label: '全部', value: 'all' },
                          { label: '需关注', value: 'attention' },
                          { label: '运行中', value: 'running' },
                          { label: '已完成', value: 'done' },
                        ]}
                      />
                    </Col>
                  </Row>
                  <Table
                    size="small"
                    rowKey={rowKey}
                    columns={columns}
                    dataSource={filteredItems}
                    loading={loading}
                    scroll={{ x: 1500 }}
                    pagination={{ pageSize: 20, showSizeChanger: true }}
                  />
                </Space>
              </Card>
            ),
          },
          { key: 'logs', label: '运行日志', children: <LogsPanel task={task} logs={logs} onOpenTask={setLogTaskId} /> },
        ]}
      />

      <Drawer open={Boolean(logTaskId)} onClose={() => setLogTaskId('')} title={`子任务日志 ${logTaskId || ''}`} width={720} destroyOnClose>
        {logTaskId ? <TaskLogPanel taskId={logTaskId} onDone={loadStatus} /> : null}
      </Drawer>
    </Space>
  )
}

function StageCell({ stage, sub }: { stage?: string; sub?: string }) {
  return (
    <Space direction="vertical" size={0} style={{ maxWidth: 180 }}>
      <StatusTag value={stage} />
      {sub ? <Text type="secondary" ellipsis={{ tooltip: sub }}>{sub}</Text> : null}
    </Space>
  )
}

function OverviewPanel({ task, items, logs }: { task: PipelineTask | null; items: PipelineItem[]; logs: string[] }) {
  const attentionItems = items.filter((item) => item.last_error || ['failed', 'manual_required'].includes(normalized(item.overall_status))).slice(0, 8)
  return (
    <Row gutter={[16, 16]}>
      <Col xs={24} xl={12}>
        <Card title="任务概况">
          {task ? (
            <Descriptions size="small" bordered column={1} items={[
              { key: 'key', label: '任务 Key', children: task.task_key || '-' },
              { key: 'status', label: '状态', children: <StatusTag value={task.status} /> },
              { key: 'source', label: '来源', children: task.source_type || '-' },
              { key: 'target', label: '目标成功数', children: task.target_success_count || '-' },
              { key: 'started', label: '启动时间', children: formatDateTime(task.started_at) },
              { key: 'updated', label: '更新时间', children: formatDateTime(task.updated_at) },
            ]} />
          ) : <Empty description="还没有流水线任务" />}
        </Card>
      </Col>
      <Col xs={24} xl={12}>
        <Card title="最近事件">
          <Space direction="vertical" size={6} style={{ display: 'flex', minHeight: 180 }}>
            {logs.slice(-8).length === 0 ? <Text type="secondary">暂无日志</Text> : null}
            {logs.slice(-8).map((line, index) => <Text key={`${index}_${line}`} code style={{ whiteSpace: 'pre-wrap' }}>{line}</Text>)}
          </Space>
        </Card>
      </Col>
      <Col span={24}>
        <Card title="需要关注的账号">
          {attentionItems.length === 0 ? <Text type="secondary">当前没有失败或人工处理账号。</Text> : (
            <Table
              size="small"
              rowKey={rowKey}
              pagination={false}
              dataSource={attentionItems}
              columns={[
                { title: '账号', render: (_, record) => <Text>{record.email || record.account_id || '-'}</Text> },
                { title: '整体', render: (_, record) => <StatusTag value={record.overall_status} /> },
                { title: 'Idea', render: (_, record) => <StatusTag value={record.idea_stage} /> },
                { title: '手机号', render: (_, record) => <StatusTag value={record.phone_stage} /> },
                { title: 'OAIPay', render: (_, record) => <StatusTag value={record.oaipay_stage} /> },
                { title: '原因', render: (_, record) => <Text type="danger" ellipsis={{ tooltip: itemIssue(record) }}>{itemIssue(record)}</Text> },
              ]}
            />
          )}
        </Card>
      </Col>
    </Row>
  )
}

function ConfigPanel({
  form,
  oaipayCategories,
  oaipayCategoryLoading,
  oaipayCategoryError,
  onLoadOaipayCategories,
}: {
  form: ReturnType<typeof Form.useForm>[0]
  oaipayCategories: OaipayCategory[]
  oaipayCategoryLoading: boolean
  oaipayCategoryError: string
  onLoadOaipayCategories: (options?: { force?: boolean; silent?: boolean }) => Promise<void>
}) {
  const oaipayEnabled = Form.useWatch('oaipay_enabled', form)
  const oaipayCategoryOptions = useMemo(
    () => oaipayCategories.map((category) => ({
      value: category.id,
      label: formatOaipayCategoryLabel(category),
    })),
    [oaipayCategories],
  )

  return (
    <Form form={form} layout="vertical" initialValues={initialFormValues(DEFAULT_CONFIG)} className="idea-oaipay-config-form">
      <Space direction="vertical" size={12} style={{ display: 'flex' }}>
        <Card
          title="账号来源"
          size="small"
          className="idea-oaipay-config-card"
          extra={<Text type="secondary">选择账号入口、筛选范围和注册补位目标</Text>}
        >
          <div className="idea-oaipay-source-grid">
            <Form.Item name="source_type" label="来源">
              <Select options={[{ value: 'local', label: '本地账号' }, { value: 'register', label: '注册新账号' }]} />
            </Form.Item>
            <Form.Item name="limit" label="本地账号限制">
              <InputNumber min={0} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="target_count" label="注册目标成功数">
              <InputNumber min={0} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="all_filtered" valuePropName="checked" label="筛选范围">
              <Checkbox>使用筛选范围</Checkbox>
            </Form.Item>
            <Form.Item name="account_ids_text" label="本地账号 ID" className="idea-oaipay-field-wide">
              <TextArea rows={4} placeholder="一行一个或逗号分隔；为空时可用筛选范围" />
            </Form.Item>
            <Form.Item name="email" label="邮箱搜索">
              <Input allowClear />
            </Form.Item>
            <Form.Item name="status" label="账号状态">
              <Input allowClear placeholder="registered" />
            </Form.Item>
            <Form.Item name="subscription_type" label="订阅筛选">
              <Input allowClear placeholder="free / plus" />
            </Form.Item>
            <Form.Item name="account_validity" label="有效性">
              <Input allowClear placeholder="valid / invalid" />
            </Form.Item>
            <Form.Item name="oaipay_state" label="OAIPay 状态">
              <Input allowClear placeholder="not_found" />
            </Form.Item>
            <Form.Item name="register_batch_size" label="注册补位批量">
              <InputNumber min={1} max={50} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="register_concurrency" label="注册并发">
              <InputNumber min={1} max={10} style={{ width: '100%' }} />
            </Form.Item>
          </div>
        </Card>

        <div className="idea-oaipay-stage-grid">
          <Card title="Idea 提交" size="small" className="idea-oaipay-config-card" extra={<Link to="/baxigpt-cdk-pool">卡密池</Link>}>
            <div className="idea-oaipay-card-fields">
              <div className="idea-oaipay-toggle-strip">
                <Form.Item name="idea_enabled" valuePropName="checked" label="启用">
                  <Switch />
                </Form.Item>
                <Form.Item name="idea_use_pool" valuePropName="checked" label="使用卡密池">
                  <Switch />
                </Form.Item>
                <Form.Item name="idea_failure_continue" valuePropName="checked" label="失败后继续">
                  <Switch />
                </Form.Item>
              </div>
              <Form.Item name="code_lines" label="粘贴卡密" className="idea-oaipay-field-full">
                <TextArea rows={3} placeholder="关闭卡密池时必填；支持多行" />
              </Form.Item>
              <Form.Item name="submit_interval_seconds" label="提交间隔">
                <InputNumber min={0} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="status_poll_interval_seconds" label="轮询间隔">
                <InputNumber min={1} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="status_poll_timeout_seconds" label="未返回提醒" className="idea-oaipay-field-full" extra="到点只写提醒日志，继续等待 paid/failed 终态。">
                <InputNumber min={1800} step={60} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="idea_skip_subs" label="已有这些订阅时跳过 Idea" className="idea-oaipay-field-full">
                <Select mode="multiple" options={subscriptionOptions} />
              </Form.Item>
            </div>
          </Card>

          <Card title="本地状态刷新 / Gate" size="small" className="idea-oaipay-config-card">
            <div className="idea-oaipay-card-fields">
              <Form.Item name="check_enabled" valuePropName="checked" label="刷新本地状态">
                <Switch />
              </Form.Item>
              <Form.Item name="gate_enabled" valuePropName="checked" label="启用放行条件">
                <Switch />
              </Form.Item>
              <Form.Item name="gate_mode" label="放行模式" className="idea-oaipay-field-full">
                <Select options={[{ value: 'none', label: '不限制' }, { value: 'account_valid', label: '账号有效' }, { value: 'subscription_in', label: '指定订阅类型' }, { value: 'upload_ready', label: '满足上传条件' }]} />
              </Form.Item>
              <Form.Item name="allowed_subscription_types" label="允许订阅类型" className="idea-oaipay-field-full">
                <Select mode="multiple" options={subscriptionOptions} />
              </Form.Item>
            </div>
          </Card>

          <Card title="手机号绑定" size="small" className="idea-oaipay-config-card">
            <div className="idea-oaipay-card-fields">
              <Form.Item name="phone_policy" label="策略">
                <Select options={[{ value: 'disabled', label: '不绑定' }, { value: 'best_effort', label: '尽力绑定' }, { value: 'required', label: '必须绑定' }]} />
              </Form.Item>
              <Form.Item name="phone_apply_to" label="适用账号">
                <Select options={[{ value: 'gate_passed', label: '通过 Gate' }, { value: 'all', label: '全部' }, { value: 'free', label: '仅 free' }, { value: 'plus', label: '仅 plus' }]} />
              </Form.Item>
              <Form.Item name="phone_use_pool" valuePropName="checked" label="使用手机号池" className="idea-oaipay-field-full">
                <Switch />
              </Form.Item>
              <Form.Item name="phone_lines" label="粘贴手机号/API" className="idea-oaipay-field-full">
                <TextArea rows={3} placeholder="+1xxx----https://... 或 +1xxx|https://...；为空则使用手机号池" />
              </Form.Item>
              <Form.Item name="phone_timeout_seconds" label="收码超时">
                <InputNumber min={1800} step={60} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="phone_poll_interval_seconds" label="轮询间隔">
                <InputNumber min={1} style={{ width: '100%' }} />
              </Form.Item>
            </div>
          </Card>

          <Card
            title="OAIPay 上传"
            size="small"
            className="idea-oaipay-config-card"
            extra={
              <Button
                size="small"
                icon={<ReloadOutlined />}
                loading={oaipayCategoryLoading}
                onClick={() => onLoadOaipayCategories({ force: true })}
              >
                刷新分组
              </Button>
            }
          >
            <div className="idea-oaipay-card-fields">
              <Form.Item name="oaipay_enabled" valuePropName="checked" label="启用上传">
                <Switch onChange={(checked) => {
                  if (checked) void onLoadOaipayCategories()
                }} />
              </Form.Item>
              <Form.Item
                name="oaipay_category_id"
                label="自动分类兜底分组"
                tooltip="启用上传后默认按账号状态自动分类；这里仅在自动规则未命中或远端分类不存在时作为兜底，不会覆盖已命中的自动分类。"
              >
                <Select
                  allowClear
                  showSearch
                  disabled={!oaipayEnabled && oaipayCategoryOptions.length === 0}
                  loading={oaipayCategoryLoading}
                  options={oaipayCategoryOptions}
                  optionFilterProp="label"
                  placeholder={oaipayCategoryLoading ? '正在获取分组...' : '可选：自动分类未命中时使用'}
                  onOpenChange={(open) => {
                    if (open) void onLoadOaipayCategories()
                  }}
                  notFoundContent={oaipayCategoryLoading ? '正在获取分组...' : '暂无分组；可刷新或留空'}
                />
              </Form.Item>
              {oaipayCategoryError ? (
                <Alert
                  showIcon
                  type="warning"
                  className="idea-oaipay-field-full"
                  message="OAIPay 分组获取失败"
                  description={`请检查全局配置里的 OAIPay API URL / API Key，或点击刷新重试：${oaipayCategoryError}`}
                  action={
                    <Button size="small" onClick={() => onLoadOaipayCategories({ force: true })} loading={oaipayCategoryLoading}>
                      重试
                    </Button>
                  }
                />
              ) : null}
              <Form.Item name="oaipay_exists_as_success" valuePropName="checked" className="idea-oaipay-field-full">
                <Checkbox>远端已存在视为成功</Checkbox>
              </Form.Item>
              <Form.Item name="oaipay_require_phone_bound" valuePropName="checked" className="idea-oaipay-field-full">
                <Checkbox>要求手机号绑定成功</Checkbox>
              </Form.Item>
              <Form.Item name="oaipay_require_subscription_in" label="上传订阅要求" className="idea-oaipay-field-full">
                <Select mode="multiple" allowClear options={subscriptionOptions} />
              </Form.Item>
              <Form.Item name="tick_interval_seconds" label="调度间隔" className="idea-oaipay-field-full">
                <InputNumber min={1} max={60} style={{ width: '100%' }} />
              </Form.Item>
            </div>
          </Card>
        </div>
      </Space>
    </Form>
  )
}

const subscriptionOptions = ['free', 'plus', 'pro', 'team', 'enterprise'].map((value) => ({ value, label: value }))

function LogsPanel({ task, logs, onOpenTask }: { task: PipelineTask | null; logs: string[]; onOpenTask: (taskId: string) => void }) {
  return (
    <Row gutter={[16, 16]}>
      <Col xs={24} xl={8}>
        <Card title="子任务">
          <Space direction="vertical" size={8} style={{ display: 'flex' }}>
            {task?.active_register_task_id ? <Button onClick={() => onOpenTask(String(task.active_register_task_id))}>注册任务 {task.active_register_task_id}</Button> : null}
            {task?.active_idea_task_id ? <Button onClick={() => onOpenTask(String(task.active_idea_task_id))}>Idea任务 {task.active_idea_task_id}</Button> : null}
            {task?.active_phone_task_id ? <Button onClick={() => onOpenTask(String(task.active_phone_task_id))}>手机号任务 {task.active_phone_task_id}</Button> : null}
            {!task?.active_register_task_id && !task?.active_idea_task_id && !task?.active_phone_task_id ? <Text type="secondary">当前没有活跃子任务。</Text> : null}
          </Space>
        </Card>
      </Col>
      <Col xs={24} xl={16}>
        <Card title="主流水线日志">
          <Space direction="vertical" size={6} style={{ display: 'flex', maxHeight: 520, overflow: 'auto' }}>
            {logs.length === 0 ? <Text type="secondary">暂无日志</Text> : null}
            {logs.map((line, index) => <Text key={`${index}_${line}`} code style={{ whiteSpace: 'pre-wrap' }}>{line}</Text>)}
          </Space>
        </Card>
      </Col>
    </Row>
  )
}
