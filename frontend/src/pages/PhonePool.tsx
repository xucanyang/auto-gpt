import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Card, Checkbox, Descriptions, Drawer, Empty, Grid, Table, Button, Input, Tag, Space, Popconfirm, message, Typography, Tooltip, Select, Pagination, Skeleton, Switch, theme } from 'antd'
import {
  UploadOutlined,
  ReloadOutlined,
  DeleteOutlined,
  PlayCircleOutlined,
  StopOutlined,
  UndoOutlined,
  DatabaseOutlined,
  CopyOutlined,
  BugOutlined,
  CloudSyncOutlined,
  SyncOutlined,
  SwapOutlined,
  DownloadOutlined,
} from '@ant-design/icons'
import { ApiError, apiFetch } from '@/lib/utils'

type PhonePoolItem = {
  id: number
  phone_e164: string
  api_url: string
  api_host: string
  source_api_url?: string
  source_api_host?: string
  forwarded_api_url?: string
  forwarded_api_host?: string
  api_forwarded?: boolean
  forward_status?: string
  api_expired_date: string
  api_expiry_checked_at: string
  api_expiry_status: string
  api_expiry_error: string
  label: string
  status: string
  available: boolean
  self_available?: boolean
  self_unavailable_reason?: string
  prefix?: string
  prefix_status?: string
  prefix_total?: number
  prefix_available_count?: number
  prefix_rejected_count?: number
  prefix_remaining_capacity?: number
  ordinary_task_eligible?: boolean
  ordinary_task_block_reason?: string
  remaining_capacity: number
  bound_count: number
  bound_account_emails?: string[]
  max_accounts: number
  success_count: number
  fail_count: number
  last_error_code: string
  last_error_message: string
  cooldown_until: string | null
  last_used_at: string | null
  created_at: string | null
  updated_at: string | null
}

type PhonePoolSummary = {
  total?: number
  available?: number
  remaining_capacity?: number
  rate_limited?: number
  unavailable?: number
  cannot_send?: number
  cooldown?: number
  exhausted?: number
  disabled?: number
  available_prefixes?: PhonePoolPrefixItem[]
  rejected_phone_count?: number
  rejected_prefix_count?: number
  rejected_prefixes?: PhonePoolPrefixItem[]
  exhausted_prefix_count?: number
  exhausted_prefixes?: PhonePoolPrefixItem[]
  temporary_prefix_count?: number
  temporary_prefixes?: PhonePoolPrefixItem[]
  prefix_health?: {
    available?: PhonePoolPrefixItem[]
    unavailable?: PhonePoolPrefixItem[]
    exhausted?: PhonePoolPrefixItem[]
    temporary?: PhonePoolPrefixItem[]
  }
  phone_signup_available_prefix_count?: number
  phone_signup_available_prefixes?: PhonePoolPrefixItem[]
  phone_signup_unavailable_prefix_count?: number
  phone_signup_unavailable_prefixes?: PhonePoolPrefixItem[]
  phone_signup_prefix_health?: {
    available?: PhonePoolPrefixItem[]
    unavailable?: PhonePoolPrefixItem[]
    untested?: PhonePoolPrefixItem[]
  }
}

type PhonePoolPrefixItem = {
  prefix?: string
  status?: string
  total?: number
  count?: number
  available_count?: number
  rejected_count?: number
  bind_limit_count?: number
  remaining_capacity?: number
  rate_limited_count?: number
  cooldown_count?: number
  disabled_count?: number
  cannot_send_count?: number
  success_count?: number
  failure_count?: number
  last_seen_at?: string
}

type PhonePoolBatchAction = 'delete' | 'reset' | 'disable'
type ApiExpiryRefreshScope = 'selected' | 'page' | 'record' | 'all'
type ApiExpiryFilter = '' | 'unchecked' | 'ok' | 'missing' | 'error' | 'expired' | 'soon7' | 'soon30'
type TaskEligibilityFilter = '' | 'eligible' | 'prefix_blocked' | 'self_blocked'
type PhoneBindingFilter = '' | 'bound' | 'unbound'

type PhonePoolDiagnostics = {
  item?: PhonePoolItem
  counts?: {
    local_bound_count?: number
    matched_account_count?: number
    matched_bound_count?: number
    remaining_capacity?: number
    max_accounts?: number
  }
  matched_accounts?: Array<{
    id?: number
    email?: string
    account_status?: string
    binding_status?: string
    api_url?: string
    task_id?: string
    bound_at?: string
    error?: string
  }>
  notes?: Array<{
    severity?: string
    code?: string
    message?: string
  }>
}

type PhonePoolForwardSync = {
  status?: string
  last_attempt_at?: string
  last_success_at?: string
  last_error?: string
  inventory_count?: number
  route_count?: number
  owner_count?: number
  trigger?: string
}

type PhonePoolForwardRegistry = {
  status?: string
  last_error?: string
  [key: string]: unknown
}

type PhonePoolForwardingConfig = {
  enabled: boolean
  active_origin: string
  previous_origins: string[]
  relay_configured?: boolean
  forward_status?: string
  affected_records?: number
  source_host_count?: number
  registry?: PhonePoolForwardRegistry
  sync?: PhonePoolForwardSync
}

const STATUS_META: Record<string, { color: string; label: string }> = {
  active: { color: 'success', label: '可用' },
  cannot_send: { color: 'error', label: '不可用' },
  rate_limited: { color: 'warning', label: '限流' },
  cooldown: { color: 'warning', label: '冷却中' },
  exhausted: { color: 'default', label: '已绑满' },
  disabled: { color: 'default', label: '已停用' },
}

const STATUS_FILTER_OPTIONS = [
  { value: '', label: '全部号码状态' },
  { value: 'active', label: '可用' },
  { value: 'rate_limited', label: '限流' },
  { value: 'cannot_send', label: '不可用' },
  { value: 'cooldown', label: '冷却中' },
  { value: 'exhausted', label: '已绑满' },
  { value: 'disabled', label: '已停用' },
]

const PHONE_POOL_PAGE_SIZE = 10
const API_EXPIRY_REFRESH_BATCH_SIZE = 20
const API_EXPIRY_SOON_DAYS = 7
const API_EXPIRY_WARNING_DAYS = 30

const API_EXPIRY_FILTER_OPTIONS = [
  { value: '', label: '全部API到期' },
  { value: 'unchecked', label: '未获取' },
  { value: 'error', label: '获取失败' },
  { value: 'missing', label: '未返回有效期' },
  { value: 'expired', label: '已过期' },
  { value: 'soon7', label: '7天内到期' },
  { value: 'soon30', label: '30天内到期' },
  { value: 'ok', label: '已获取' },
] as const

const TASK_ELIGIBILITY_FILTER_OPTIONS = [
  { value: '', label: '全部任务状态' },
  { value: 'eligible', label: '普通任务可选' },
  { value: 'prefix_blocked', label: '号段跳过' },
  { value: 'self_blocked', label: '自身不可用' },
] as const

const PHONE_BINDING_FILTER_OPTIONS = [
  { value: '', label: '全部绑定情况' },
  { value: 'bound', label: '已绑定' },
  { value: 'unbound', label: '未绑定' },
]

const PREFIX_STATUS_META: Record<string, { color: string; label: string }> = {
  available: { color: 'success', label: '号段可用' },
  unavailable: { color: 'error', label: '号段不可用' },
  exhausted: { color: 'default', label: '号段绑满' },
  temporary: { color: 'warning', label: '暂不可用' },
  unknown: { color: 'default', label: '号段未知' },
}

const SELF_REASON_LABELS: Record<string, string> = {
  disabled: '人工停用',
  openai_rejected: 'OpenAI 拒绝',
  api_no_code: 'API 无码',
  api_error: 'API 异常',
  api_forward_error: '转发临时故障',
  cannot_send: '不可发码',
  rate_limited: '限流中',
  cooldown: '冷却中',
  exhausted: '已绑满',
  no_capacity: '容量为 0',
  missing_api_url: '缺少 API',
  self_unavailable: '自身不可用',
}

const TASK_BLOCK_REASON_LABELS: Record<string, string> = {
  prefix_unavailable: '号段不可用',
  prefix_exhausted: '号段容量用尽',
  prefix_temporary: '号段暂不可用',
  prefix_unknown: '号段未知',
  disabled: '人工停用',
  openai_rejected: 'OpenAI 拒绝',
  api_no_code: 'API 无码',
  api_error: 'API 异常',
  api_forward_error: '转发临时故障',
  cannot_send: '不可发码',
  rate_limited: '限流中',
  cooldown: '冷却中',
  exhausted: '已绑满',
  no_capacity: '容量为 0',
  missing_api_url: '缺少 API',
  self_unavailable: '自身不可用',
}

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

function formatApiExpiredDate(value?: string | null) {
  const text = String(value || '').trim()
  if (!text) return '-'
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text
  return formatBeijingTime(text)
}

function parseApiExpiryTime(value?: string | null) {
  const text = String(value || '').trim()
  if (!text) return null
  const normalized = /^\d{4}-\d{2}-\d{2}$/.test(text)
    ? `${text}T23:59:59Z`
    : /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(text)
      ? `${text.replace(' ', 'T')}Z`
      : text
  const time = new Date(normalized).getTime()
  return Number.isFinite(time) ? time : null
}

function apiExpiryMeta(record: PhonePoolItem) {
  const status = String(record.api_expiry_status || '').trim()
  const expiredText = String(record.api_expired_date || '').trim()
  const error = String(record.api_expiry_error || '').trim()
  const checkedAt = String(record.api_expiry_checked_at || '').trim()
  const expiryTime = parseApiExpiryTime(expiredText)
  if (!expiredText) {
    if (status === 'error') return { key: 'error', color: 'error', label: '获取失败', detail: error || '请求收码 API 失败', expiryTime, checkedAt }
    if (status === 'missing_expired_date') return { key: 'missing', color: 'default', label: '未返回', detail: 'API 响应没有 data.expired_date', expiryTime, checkedAt }
    return { key: 'unchecked', color: 'default', label: '未获取', detail: '导入后后台会自动获取一次', expiryTime, checkedAt }
  }
  if (expiryTime === null) return { key: 'ok', color: 'processing', label: '已获取', detail: expiredText, expiryTime, checkedAt }
  const diffDays = Math.ceil((expiryTime - Date.now()) / 86400000)
  if (diffDays < 0) return { key: 'expired', color: 'error', label: '已过期', detail: expiredText, expiryTime, checkedAt }
  if (diffDays <= API_EXPIRY_SOON_DAYS) return { key: 'soon7', color: 'warning', label: `${Math.max(diffDays, 0)}天内`, detail: expiredText, expiryTime, checkedAt }
  if (diffDays <= API_EXPIRY_WARNING_DAYS) return { key: 'soon30', color: 'gold', label: `${diffDays}天`, detail: expiredText, expiryTime, checkedAt }
  return { key: 'ok', color: 'success', label: `${diffDays}天`, detail: expiredText, expiryTime, checkedAt }
}

function matchesApiExpiryFilter(record: PhonePoolItem, filter: ApiExpiryFilter) {
  if (!filter) return true
  const meta = apiExpiryMeta(record)
  if (filter === 'soon30') return meta.key === 'soon7' || meta.key === 'soon30'
  if (filter === 'ok') return Boolean(String(record.api_expired_date || '').trim())
  return meta.key === filter
}

function isPhoneBound(record: PhonePoolItem) {
  if (Number(record.bound_count || 0) > 0) return true
  return Array.isArray(record.bound_account_emails)
    && record.bound_account_emails.some((email) => Boolean(String(email || '').trim()))
}

function matchesPhoneBindingFilter(record: PhonePoolItem, filter: PhoneBindingFilter) {
  if (!filter) return true
  const bound = isPhoneBound(record)
  return filter === 'bound' ? bound : !bound
}

function statusTag(status: string, tooltip?: string) {
  const meta = STATUS_META[status] || { color: 'default', label: status || '未知' }
  const tag = <Tag color={meta.color}>{meta.label}</Tag>
  return tooltip ? <Tooltip title={tooltip}>{tag}</Tooltip> : tag
}

function prefixStatusTag(record: PhonePoolItem, compact = false) {
  const status = String(record.prefix_status || 'unknown')
  const meta = PREFIX_STATUS_META[status] || PREFIX_STATUS_META.unknown
  const prefix = String(record.prefix || '').trim()
  const detail = [
    prefix ? `号段: ${prefix}` : '',
    `状态: ${meta.label}`,
    `总数: ${Number(record.prefix_total || 0)}`,
    `号码自身可用: ${Number(record.prefix_available_count || 0)}`,
    `OpenAI 拒绝: ${Number(record.prefix_rejected_count || 0)}`,
    `容量: ${Number(record.prefix_remaining_capacity || 0)}`,
  ].filter(Boolean).join('\n')
  return (
    <Space direction="vertical" size={compact ? 1 : 2}>
      <Tooltip title={detail}>
        <Tag color={meta.color}>{prefix ? `${prefix} ${compact ? meta.label.replace('号段', '') : meta.label}` : meta.label}</Tag>
      </Tooltip>
      {!compact ? (
        <Typography.Text type="secondary" style={{ fontSize: 11, lineHeight: '16px' }}>
          拒绝 {Number(record.prefix_rejected_count || 0)}，自身可用 {Number(record.prefix_available_count || 0)}
        </Typography.Text>
      ) : null}
    </Space>
  )
}

function selfStatusDetail(record: PhonePoolItem) {
  const reason = String(record.self_unavailable_reason || '').trim()
  if (record.self_available !== false) return '号码自身可用'
  return SELF_REASON_LABELS[reason] || reason || '号码自身不可用'
}

function taskEligibilityBlock(record: PhonePoolItem, compact = false) {
  const eligible = Boolean(record.ordinary_task_eligible)
  const reason = String(record.ordinary_task_block_reason || '').trim()
  const reasonLabel = TASK_BLOCK_REASON_LABELS[reason] || reason || ''
  return (
    <Space direction="vertical" size={compact ? 1 : 2}>
      <Tag color={eligible ? 'success' : reason.startsWith('prefix_') ? 'warning' : 'default'}>
        {eligible ? '普通可选' : '普通跳过'}
      </Tag>
      {!eligible || !compact ? (
        <Typography.Text type={eligible ? 'secondary' : reason.startsWith('prefix_') ? 'warning' : 'secondary'} style={{ fontSize: compact ? 11 : 12, lineHeight: '16px' }}>
          {eligible ? '号段可用 + 自身可用' : reasonLabel || '暂不选择'}
        </Typography.Text>
      ) : null}
    </Space>
  )
}

function noteAlertType(severity?: string): 'success' | 'info' | 'warning' | 'error' {
  const value = String(severity || '').toLowerCase()
  if (value === 'success') return 'success'
  if (value === 'error') return 'error'
  if (value === 'warning') return 'warning'
  return 'info'
}

const FORWARD_STATUS_META: Record<string, { color: string; label: string }> = {
  active: { color: 'success', label: '已启用' },
  ready: { color: 'success', label: '已就绪' },
  synced: { color: 'success', label: '已同步' },
  forwarded: { color: 'success', label: '已转发' },
  ok: { color: 'success', label: '正常' },
  syncing: { color: 'processing', label: '同步中' },
  pending: { color: 'processing', label: '待同步' },
  degraded: { color: 'warning', label: '部分异常' },
  stale: { color: 'warning', label: '待刷新' },
  disabled: { color: 'default', label: '未启用' },
  direct: { color: 'default', label: '直连' },
  bypassed: { color: 'default', label: '直连' },
  not_configured: { color: 'warning', label: '未配置' },
  unavailable: { color: 'error', label: '不可达' },
  conflict: { color: 'error', label: '冲突' },
  error: { color: 'error', label: '异常' },
  failed: { color: 'error', label: '失败' },
}

const EMPTY_FORWARDING_CONFIG: PhonePoolForwardingConfig = {
  enabled: false,
  active_origin: '',
  previous_origins: [],
  relay_configured: false,
  forward_status: 'disabled',
  sync: {},
}

function forwardStatusTag(status?: string, fallback: 'forwarded' | 'direct' | 'unknown' = 'unknown') {
  const key = String(status || '').trim().toLowerCase()
  const meta = FORWARD_STATUS_META[key]
    || (fallback === 'forwarded'
      ? FORWARD_STATUS_META.forwarded
      : fallback === 'direct'
        ? FORWARD_STATUS_META.direct
        : { color: 'default', label: key || '未知' })
  return <Tag color={meta.color}>{meta.label}</Tag>
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function normalizeForwardingConfig(value: unknown, fallback: PhonePoolForwardingConfig = EMPTY_FORWARDING_CONFIG): PhonePoolForwardingConfig {
  const envelope = asRecord(value)
  const data = asRecord(envelope?.forwarding) || envelope || {}
  const previousOrigins = Array.isArray(data.previous_origins)
    ? data.previous_origins.map((item: unknown) => String(item || '').trim()).filter(Boolean)
    : fallback.previous_origins
  const sync = data.sync && typeof data.sync === 'object'
    ? data.sync as PhonePoolForwardSync
    : fallback.sync
  const registry = data.registry && typeof data.registry === 'object'
    ? data.registry as PhonePoolForwardRegistry
    : fallback.registry
  const affectedRecords = Number(data.affected_records)
  const sourceHostCount = Number(data.source_host_count)
  return {
    enabled: typeof data.enabled === 'boolean' ? data.enabled : fallback.enabled,
    active_origin: String(data.active_origin ?? fallback.active_origin ?? '').trim(),
    previous_origins: Array.from(new Set(previousOrigins)),
    relay_configured: typeof data.relay_configured === 'boolean' ? data.relay_configured : fallback.relay_configured,
    forward_status: String(data.forward_status ?? fallback.forward_status ?? '').trim(),
    affected_records: Number.isFinite(affectedRecords) ? affectedRecords : fallback.affected_records,
    source_host_count: Number.isFinite(sourceHostCount) ? sourceHostCount : fallback.source_host_count,
    registry,
    sync,
  }
}

function normalizeForwardOrigin(value: string, label: string) {
  const text = String(value || '').trim()
  if (!text) return ''
  let parsed: URL
  try {
    parsed = new URL(text)
  } catch {
    throw new Error(`${label}必须是完整的 http(s) Origin`)
  }
  if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) {
    throw new Error(`${label}必须是完整的 http(s) Origin`)
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash || (parsed.pathname && parsed.pathname !== '/')) {
    throw new Error(`${label}只能填写协议、域名和端口，不能包含路径、参数或账号信息`)
  }
  return parsed.origin
}

function parsePreviousOrigins(value: string, activeOrigin: string) {
  const entries = String(value || '')
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean)
  const normalized: string[] = []
  const seen = new Set<string>()
  entries.forEach((entry, index) => {
    const origin = normalizeForwardOrigin(entry, `兼容旧域名第 ${index + 1} 项`)
    const key = origin.toLowerCase()
    if (!origin || key === activeOrigin.toLowerCase() || seen.has(key)) return
    seen.add(key)
    normalized.push(origin)
  })
  return normalized
}

function apiUrlHost(value?: string | null) {
  const text = String(value || '').trim()
  if (!text) return ''
  try {
    return new URL(text).host
  } catch {
    return ''
  }
}

function sourceApiUrl(record: Pick<PhonePoolItem, 'source_api_url' | 'api_url'>) {
  return String(record.source_api_url || record.api_url || '').trim()
}

function forwardedApiUrl(record: Pick<PhonePoolItem, 'forwarded_api_url'>) {
  return String(record.forwarded_api_url || '').trim()
}

function effectiveApiUrl(record: Pick<PhonePoolItem, 'source_api_url' | 'api_url' | 'forwarded_api_url' | 'forward_status'>) {
  const forwarded = forwardedApiUrl(record)
  if (forwarded) return forwarded
  const status = String(record.forward_status || '').trim().toLowerCase()
  if (['unavailable', 'conflict', 'error', 'failed'].includes(status)) return ''
  return sourceApiUrl(record)
}

function missingForwardedApiText(status?: string) {
  const value = String(status || '').trim().toLowerCase()
  if (['unavailable', 'conflict', 'error', 'failed'].includes(value)) {
    return '转发当前不可用，已禁止回退原始 API'
  }
  if (['syncing', 'pending', 'stale'].includes(value)) {
    return '转发路由尚未生成'
  }
  if (['disabled', 'direct', 'bypassed', 'not_configured', ''].includes(value)) {
    return '转发未启用，当前使用原始 API'
  }
  return '转发路由尚未生成'
}

function sourceApiHost(record: Pick<PhonePoolItem, 'source_api_url' | 'source_api_host' | 'api_url' | 'api_host'>) {
  return String(record.source_api_host || record.api_host || apiUrlHost(sourceApiUrl(record)) || '').trim()
}

function forwardedApiHost(record: Pick<PhonePoolItem, 'forwarded_api_url' | 'forwarded_api_host'>) {
  return String(record.forwarded_api_host || apiUrlHost(forwardedApiUrl(record)) || '').trim()
}

function recordUsesForwarding(record: Pick<PhonePoolItem, 'source_api_url' | 'api_url' | 'forwarded_api_url' | 'api_forwarded'>) {
  const source = sourceApiUrl(record)
  const forwarded = forwardedApiUrl(record)
  return Boolean(forwarded && (record.api_forwarded !== false || forwarded !== source))
}

function apiRoutePath(value?: string | null) {
  const text = String(value || '').trim()
  if (!text) return '-'
  try {
    const parsed = new URL(text)
    return `${parsed.pathname || '/'}${parsed.search || ''}`
  } catch {
    return text
  }
}

function errorMessage(error: unknown) {
  if (error instanceof Error) return String(error.message || '').trim()
  const record = asRecord(error)
  return typeof record?.message === 'string' ? record.message.trim() : ''
}

function forwardingRequestError(error: unknown, fallback: string) {
  const detail = errorMessage(error)
  const status = error instanceof ApiError ? error.status : Number(asRecord(error)?.status || 0)
  if (status === 409) return detail ? `域名冲突：${detail}` : '域名与现有来源或后缀冲突'
  if (status === 503) return detail ? `转发服务不可用：${detail}` : '转发服务未配置或当前不可达'
  return detail || fallback
}

async function copyTextToClipboard(text: string) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Fall through for non-secure browser contexts.
    }
  }
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', 'true')
  textarea.style.position = 'fixed'
  textarea.style.left = '-9999px'
  document.body.appendChild(textarea)
  textarea.select()
  try {
    return document.execCommand('copy')
  } finally {
    document.body.removeChild(textarea)
  }
}

function phoneApiCopyText(
  record: Pick<PhonePoolItem, 'phone_e164' | 'source_api_url' | 'api_url' | 'forwarded_api_url' | 'forward_status'>,
  mode: 'effective' | 'source' = 'effective',
) {
  const phone = String(record.phone_e164 || '').trim()
  const apiUrl = mode === 'source' ? sourceApiUrl(record) : effectiveApiUrl(record)
  return apiUrl ? `${phone}----${apiUrl}` : phone
}

async function copyPhoneApiLine(
  record: Pick<PhonePoolItem, 'phone_e164' | 'source_api_url' | 'api_url' | 'forwarded_api_url' | 'forward_status'>,
  mode: 'effective' | 'source' = 'effective',
) {
  const text = phoneApiCopyText(record, mode)
  if (!text) {
    message.info('暂无可复制内容')
    return
  }
  const ok = await copyTextToClipboard(text)
  if (ok) {
    const apiUrl = mode === 'source' ? sourceApiUrl(record) : effectiveApiUrl(record)
    const forwarded = mode === 'effective' && Boolean(forwardedApiUrl(record))
    message.success(apiUrl ? mode === 'source' ? '已复制原始API' : forwarded ? '已复制转发API' : '已复制完整API' : '已复制手机号')
    return
  }
  message.error('复制失败')
}

function downloadText(filename: string, text: string) {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

export default function PhonePool() {
  const { token } = theme.useToken()
  const screens = Grid.useBreakpoint()
  const isMobile = screens.lg === false
  const [items, setItems] = useState<PhonePoolItem[]>([])
  const [summary, setSummary] = useState<PhonePoolSummary>({})
  const [statusFilter, setStatusFilter] = useState('')
  const [apiExpiryFilter, setApiExpiryFilter] = useState<ApiExpiryFilter>('')
  const [taskEligibilityFilter, setTaskEligibilityFilter] = useState<TaskEligibilityFilter>('')
  const [phoneBindingFilter, setPhoneBindingFilter] = useState<PhoneBindingFilter>('')
  const [phoneSearch, setPhoneSearch] = useState('')
  const [currentPage, setCurrentPage] = useState(1)
  const [selectedRowKeys, setSelectedRowKeys] = useState<number[]>([])
  const [loading, setLoading] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [importText, setImportText] = useState('')
  const [importing, setImporting] = useState(false)
  const [reconciling, setReconciling] = useState(false)
  const [apiExpiryLoading, setApiExpiryLoading] = useState('')
  const [batchAction, setBatchAction] = useState<PhonePoolBatchAction | ''>('')
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false)
  const [diagnosticsLoading, setDiagnosticsLoading] = useState(false)
  const [diagnosticsRecordId, setDiagnosticsRecordId] = useState<number | null>(null)
  const [diagnostics, setDiagnostics] = useState<PhonePoolDiagnostics | null>(null)
  const [forwardingOpen, setForwardingOpen] = useState(false)
  const [forwardingLoading, setForwardingLoading] = useState(false)
  const [forwardingSaving, setForwardingSaving] = useState(false)
  const [forwardingSyncing, setForwardingSyncing] = useState(false)
  const [forwardingError, setForwardingError] = useState('')
  const [forwardingConfig, setForwardingConfig] = useState<PhonePoolForwardingConfig | null>(null)
  const [forwardingDraftEnabled, setForwardingDraftEnabled] = useState(false)
  const [forwardingDraftOrigin, setForwardingDraftOrigin] = useState('')
  const [forwardingDraftPrevious, setForwardingDraftPrevious] = useState('')

  const applyForwardingDraft = useCallback((config: PhonePoolForwardingConfig) => {
    setForwardingDraftEnabled(Boolean(config.enabled))
    setForwardingDraftOrigin(String(config.active_origin || ''))
    setForwardingDraftPrevious((config.previous_origins || []).join('\n'))
  }, [])

  const loadForwarding = useCallback(async (silent = false) => {
    setForwardingLoading(true)
    if (!silent) setForwardingError('')
    try {
      const data = await apiFetch('/phone-pool/forwarding')
      const next = normalizeForwardingConfig(data)
      setForwardingConfig(next)
      applyForwardingDraft(next)
      setForwardingError('')
      return next
    } catch (error: unknown) {
      const detail = forwardingRequestError(error, '读取 API 转发配置失败')
      setForwardingError(detail)
      if (!silent) message.error(detail)
      return null
    } finally {
      setForwardingLoading(false)
    }
  }, [applyForwardingDraft])

  const load = useCallback(async (nextStatus = statusFilter) => {
    setLoading(true)
    try {
      const query = nextStatus ? `?status=${encodeURIComponent(nextStatus)}` : ''
      const data = await apiFetch(`/phone-pool${query}`)
      const nextItems = Array.isArray(data?.items) ? data.items : []
      setItems(nextItems)
      setSelectedRowKeys((prev) => {
        const ids = new Set(nextItems.map((item: PhonePoolItem) => Number(item.id)))
        return prev.filter((key) => ids.has(Number(key)))
      })
      setSummary(data?.summary && typeof data.summary === 'object' ? data.summary : {
        total: Number(data?.total || 0),
        available: Number(data?.available || 0),
        remaining_capacity: Number(data?.remaining_capacity || 0),
      })
    } finally {
      setLoading(false)
    }
  }, [statusFilter])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    void loadForwarding(true)
  }, [loadForwarding])

  const applyPhoneItems = useCallback((records: PhonePoolItem[], missing: number[] = []) => {
    const missingIds = new Set(missing.map((id) => Number(id)).filter((id) => Number.isFinite(id)))
    const incoming = new Map<number, PhonePoolItem>()
    records
      .filter((record) => Number.isFinite(Number(record?.id)))
      .forEach((record) => incoming.set(Number(record.id), record))

    setItems((current) => {
      const seen = new Set<number>()
      const next: PhonePoolItem[] = []
      current.forEach((item) => {
        const id = Number(item.id)
        if (missingIds.has(id)) return
        const patched = incoming.get(id)
        if (patched) {
          seen.add(id)
          if (!statusFilter || String(patched.status || '') === statusFilter) {
            next.push(patched)
          }
          return
        }
        next.push(item)
      })
      incoming.forEach((record, id) => {
        if (seen.has(id)) return
        if (!statusFilter || String(record.status || '') === statusFilter) {
          next.push(record)
        }
      })
      return next
    })

    if (missingIds.size > 0) {
      setSelectedRowKeys((current) => current.filter((id) => !missingIds.has(Number(id))))
    }
  }, [statusFilter])

  const refreshSummary = useCallback(async () => {
    const data = await apiFetch('/phone-pool/summary')
    if (data?.summary && typeof data.summary === 'object') {
      setSummary(data.summary)
    }
  }, [])

  const openForwardingSettings = () => {
    setForwardingOpen(true)
    setForwardingError('')
    if (forwardingConfig) {
      applyForwardingDraft(forwardingConfig)
      return
    }
    void loadForwarding(false)
  }

  const saveForwardingSettings = async () => {
    setForwardingError('')
    let activeOrigin = ''
    let previousOrigins: string[] = []
    try {
      activeOrigin = normalizeForwardOrigin(forwardingDraftOrigin, '当前主域名')
      if (forwardingDraftEnabled && !activeOrigin) {
        throw new Error('启用转发前必须填写当前主域名')
      }
      previousOrigins = parsePreviousOrigins(forwardingDraftPrevious, activeOrigin)
    } catch (error: unknown) {
      const detail = errorMessage(error) || '转发域名格式无效'
      setForwardingError(detail)
      message.error(detail)
      return
    }

    setForwardingSaving(true)
    try {
      const data = await apiFetch('/phone-pool/forwarding', {
        method: 'PUT',
        body: JSON.stringify({
          enabled: forwardingDraftEnabled,
          active_origin: activeOrigin,
          previous_origins: previousOrigins,
        }),
      })
      const next = normalizeForwardingConfig(data, {
        ...EMPTY_FORWARDING_CONFIG,
        enabled: forwardingDraftEnabled,
        active_origin: activeOrigin,
        previous_origins: previousOrigins,
      })
      setForwardingConfig(next)
      applyForwardingDraft(next)
      setForwardingError('')
      message.success(next.enabled ? 'API 转发配置已保存' : 'API 转发已关闭')
      await load()
    } catch (error: unknown) {
      const detail = forwardingRequestError(error, '保存 API 转发配置失败')
      setForwardingError(detail)
      message.error(detail)
    } finally {
      setForwardingSaving(false)
    }
  }

  const retryForwardingSync = async () => {
    setForwardingSyncing(true)
    setForwardingError('')
    try {
      const data = await apiFetch('/phone-pool/forwarding/sync', { method: 'POST' })
      const next = normalizeForwardingConfig(data, forwardingConfig || EMPTY_FORWARDING_CONFIG)
      setForwardingConfig(next)
      applyForwardingDraft(next)
      message.success('转发路由同步已触发')
      await load()
    } catch (error: unknown) {
      const detail = forwardingRequestError(error, '重试转发路由同步失败')
      setForwardingError(detail)
      message.error(detail)
    } finally {
      setForwardingSyncing(false)
    }
  }

  const refreshPhoneRows = useCallback(async (ids: number[]) => {
    const rowIds = ids.map((id) => Number(id)).filter((id) => Number.isFinite(id) && id > 0)
    if (rowIds.length === 0) return
    const data = await apiFetch('/phone-pool/snapshot', {
      method: 'POST',
      body: JSON.stringify({ ids: rowIds }),
    })
    applyPhoneItems(Array.isArray(data?.items) ? data.items : [], Array.isArray(data?.missing) ? data.missing : [])
    if (data?.summary && typeof data.summary === 'object') {
      setSummary(data.summary)
    }
  }, [applyPhoneItems])

  const loadDiagnostics = useCallback(async (id: number) => {
    if (!id) return
    setDiagnosticsLoading(true)
    try {
      const data = await apiFetch(`/phone-pool/${id}/diagnostics`)
      setDiagnostics(data && typeof data === 'object' ? data : null)
      if (data?.item) {
        applyPhoneItems([data.item])
      }
    } catch (e: any) {
      message.error(e?.message || '读取手机号诊断失败')
    } finally {
      setDiagnosticsLoading(false)
    }
  }, [applyPhoneItems])

  const openDiagnostics = (record: PhonePoolItem) => {
    const id = Number(record.id)
    setDiagnosticsRecordId(id)
    setDiagnosticsOpen(true)
    setDiagnostics(null)
    void loadDiagnostics(id)
  }

  const refreshOpenDiagnostics = async (id: number) => {
    if (diagnosticsOpen && diagnosticsRecordId === id) {
      await loadDiagnostics(id)
    }
  }

  const importPhones = async () => {
    if (!importText.trim()) return
    setImporting(true)
    try {
      const result = await apiFetch('/phone-pool/import', {
        method: 'POST',
        body: JSON.stringify({ text: importText }),
      })
      const errors = Array.isArray(result?.errors) ? result.errors.length : 0
      const warnings = Array.isArray(result?.warnings) ? result.warnings.length : 0
      const pendingExpiry = Array.isArray(result?.refresh_ids) ? result.refresh_ids.length : 0
      message.success(`导入完成：新增 ${Number(result?.added || 0)}，更新 API/token ${Number(result?.api_replaced ?? result?.updated ?? 0)}，未变化 ${Number(result?.unchanged || 0)}，重复覆盖 ${Number(result?.deduped || 0)}，跳过 ${Number(result?.skipped || 0)}，错误 ${errors}${warnings ? `，提醒 ${warnings}` : ''}${pendingExpiry ? `，后台重新获取 API 到期 ${pendingExpiry} 个` : ''}`)
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
    const record = await apiFetch(`/phone-pool/${id}/reset`, { method: 'POST' })
    if (record?.id) applyPhoneItems([record])
    message.success('已重置状态')
    try {
      await refreshPhoneRows([id])
    } catch {
      await refreshSummary()
    }
    await refreshOpenDiagnostics(id)
  }

  const toggle = async (record: PhonePoolItem) => {
    const action = record.status === 'disabled' ? 'enable' : 'disable'
    const nextRecord = await apiFetch(`/phone-pool/${record.id}/${action}`, { method: 'POST' })
    if (nextRecord?.id) applyPhoneItems([nextRecord])
    message.success(action === 'enable' ? '已启用' : '已停用')
    try {
      await refreshPhoneRows([record.id])
    } catch {
      await refreshSummary()
    }
    await refreshOpenDiagnostics(record.id)
  }

  const del = async (id: number) => {
    await apiFetch(`/phone-pool/${id}`, { method: 'DELETE' })
    message.success('已删除')
    applyPhoneItems([], [id])
    await refreshSummary()
    if (diagnosticsRecordId === id) {
      setDiagnosticsOpen(false)
      setDiagnosticsRecordId(null)
      setDiagnostics(null)
    }
  }

  const runBatchAction = async (action: PhonePoolBatchAction) => {
    const ids = selectedRowKeys.map((key) => Number(key)).filter((id) => Number.isFinite(id))
    if (ids.length === 0) return
    const label = action === 'delete' ? '删除' : action === 'reset' ? '恢复' : '禁用'
    setBatchAction(action)
    try {
      const results = await Promise.allSettled(ids.map((id) => {
        if (action === 'delete') {
          return apiFetch(`/phone-pool/${id}`, { method: 'DELETE' })
        }
        if (action === 'reset') {
          return apiFetch(`/phone-pool/${id}/reset`, { method: 'POST' })
        }
        return apiFetch(`/phone-pool/${id}/disable`, { method: 'POST' })
      }))
      const successCount = results.filter((result) => result.status === 'fulfilled').length
      const failedCount = results.length - successCount
      if (successCount > 0) {
        message.success(`批量${label}完成：成功 ${successCount} 个${failedCount ? `，失败 ${failedCount} 个` : ''}`)
      }
      if (failedCount > 0 && successCount === 0) {
        message.error(`批量${label}失败：${failedCount} 个手机号未处理成功`)
      } else if (failedCount > 0) {
        message.warning(`${failedCount} 个手机号${label}失败，请刷新后重试`)
      }
      setSelectedRowKeys([])
      await load()
    } finally {
      setBatchAction('')
    }
  }

  const reconcile = async () => {
    setReconciling(true)
    try {
      const result = await apiFetch('/phone-pool/reconcile', { method: 'POST' })
      message.success(`同步完成：新建 ${Number(result?.created || 0)}，更新 ${Number(result?.updated || 0)}`)
      await load()
      if (diagnosticsRecordId) {
        await loadDiagnostics(diagnosticsRecordId)
      }
    } catch (e: any) {
      message.error(e?.message || '回填失败')
    } finally {
      setReconciling(false)
    }
  }

  const refreshApiExpiry = async (scope: ApiExpiryRefreshScope, recordId?: number) => {
    let ids = scope === 'selected'
      ? selectedRowKeys.map((key) => Number(key)).filter((id) => Number.isFinite(id) && id > 0)
      : scope === 'record'
        ? [Number(recordId || 0)].filter((id) => Number.isFinite(id) && id > 0)
        : scope === 'all'
          ? []
          : paginatedItems.map((item) => Number(item.id)).filter((id) => Number.isFinite(id) && id > 0)
    if (scope === 'all') {
      try {
        const data = await apiFetch('/phone-pool')
        const allItems = Array.isArray(data?.items) ? data.items : []
        ids = allItems.map((item: any) => Number(item?.id)).filter((id: number) => Number.isFinite(id) && id > 0)
        if (!statusFilter) {
          setItems(allItems)
        }
        if (data?.summary && typeof data.summary === 'object') {
          setSummary(data.summary)
        }
      } catch (e: any) {
        message.error(e?.message || '读取全部手机号失败')
        return
      }
    }
    if (ids.length === 0) {
      message.warning(scope === 'selected' ? '请先选择手机号' : scope === 'record' ? '当前号码无效' : scope === 'all' ? '手机号池没有号码' : '当前页没有手机号')
      return
    }
    setApiExpiryLoading(scope)
    const toastKey = `phone-pool-api-expiry:${scope}`
    const forceRefresh = scope === 'all'
    const summaryTotals: Record<string, number> = {
      total: 0,
      checked: 0,
      success: 0,
      missing_expired_date: 0,
      error: 0,
      skipped: 0,
      not_found: 0,
    }
    const chunkSize = scope === 'all' ? API_EXPIRY_REFRESH_BATCH_SIZE : 100
    const chunks: number[][] = []
    for (let index = 0; index < ids.length; index += chunkSize) {
      chunks.push(ids.slice(index, index + chunkSize))
    }
    let processed = 0
    try {
      for (const chunk of chunks) {
        message.loading({
          content: `${forceRefresh ? '刷新' : '补全'} API 到期中：${processed}/${ids.length}`,
          key: toastKey,
          duration: 0,
        })
        const result = await apiFetch('/phone-pool/api-expiry/refresh', {
          method: 'POST',
          body: JSON.stringify({ ids: chunk, force: forceRefresh }),
        })
        processed += chunk.length
        const records = Array.isArray(result?.results)
          ? result.results.map((item: any) => item?.item).filter(Boolean)
          : []
        if (records.length > 0) applyPhoneItems(records)
        const s = result?.summary || {}
        Object.keys(summaryTotals).forEach((key) => {
          summaryTotals[key] += Number(s?.[key] || 0)
        })
      }
      message.success({
        content: `API 到期${forceRefresh ? '刷新' : '补全'}完成：总数 ${ids.length}，检查 ${Number(summaryTotals.checked || 0)}，成功 ${Number(summaryTotals.success || 0)}，未返回 ${Number(summaryTotals.missing_expired_date || 0)}，失败 ${Number(summaryTotals.error || 0)}，跳过 ${Number(summaryTotals.skipped || 0)}`,
        key: toastKey,
      })
      if (scope === 'all') {
        await load()
      }
      await refreshSummary()
      if (scope === 'record' && recordId) await refreshOpenDiagnostics(Number(recordId))
    } catch (e: any) {
      message.error({ content: e?.message || `${forceRefresh ? '刷新' : '补全'} API 到期时间失败`, key: toastKey })
    } finally {
      setApiExpiryLoading('')
    }
  }

  const copyPrefixes = async (prefixItems: Array<{ prefix: string }>, separator: 'space' | 'comma') => {
    const prefixes = prefixItems.map((item) => item.prefix).filter(Boolean)
    if (prefixes.length === 0) {
      message.info('暂无可复制号段')
      return
    }
    const text = prefixes.join(separator === 'comma' ? ',' : ' ')
    if (await copyTextToClipboard(text)) {
      message.success(`已复制 ${prefixes.length} 个号段`)
      return
    }
    message.error('复制失败')
  }

  const exportPhones = () => {
    const selectedIdSet = new Set(selectedRowKeys.map((key) => Number(key)).filter((id) => Number.isFinite(id) && id > 0))
    const sourceItems = selectedIdSet.size > 0
      ? items.filter((item) => selectedIdSet.has(Number(item.id)))
      : filteredItems
    const phones: string[] = []
    const seen = new Set<string>()
    sourceItems.forEach((item) => {
      const phone = String(item.phone_e164 || '').trim()
      if (!phone || seen.has(phone)) return
      seen.add(phone)
      phones.push(phone)
    })
    if (phones.length === 0) {
      message.warning(selectedIdSet.size > 0 ? '选中的记录里没有可导出的手机号' : '当前类别没有可导出的手机号')
      return
    }
    const stamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '')
    const scope = selectedIdSet.size > 0 ? 'selected' : String(statusFilter || 'current').replace(/[^a-z0-9_-]+/gi, '_')
    downloadText(`phone-pool-${scope}-${stamp}.txt`, `${phones.join('\n')}\n`)
    message.success(`已导出 ${phones.length} 个手机号`)
  }

  const totalCount = Number(summary.total || 0)
  const availableCount = Number(summary.available || 0)
  const remainingCapacity = Number(summary.remaining_capacity || 0)
  const rateLimitedCount = Number(summary.rate_limited || 0)
  const unavailableCount = Number(summary.unavailable || summary.cannot_send || 0)
  const exhaustedCount = Number(summary.exhausted || 0)
  const disabledCount = Number(summary.disabled || 0)
  const rejectedPhoneCount = Number(summary.rejected_phone_count || 0)
  const loadedSourceHostCount = useMemo(() => new Set(
    items.map((item) => sourceApiHost(item)).filter(Boolean),
  ).size, [items])
  const forwardingAffectedRecords = Number(forwardingConfig?.affected_records ?? totalCount)
  const forwardingSourceHostCount = Number(forwardingConfig?.source_host_count ?? loadedSourceHostCount)
  const forwardingSync = forwardingConfig?.sync || {}
  const forwardingRegistryStatus = String(forwardingConfig?.registry?.status || '').trim()
  const forwardingStatus = String(forwardingConfig?.forward_status || (forwardingConfig?.enabled ? 'pending' : 'disabled')).trim()
  const normalizePrefixItems = useCallback((raw: PhonePoolPrefixItem[] | undefined) => {
    const items = Array.isArray(raw) ? raw : []
    return items
      .map((item) => ({
        prefix: String(item?.prefix || '').replace(/\D/g, '').slice(0, 4),
        status: String(item?.status || ''),
        total: Number(item?.total || 0),
        count: Number(item?.count ?? item?.available_count ?? item?.rejected_count ?? item?.total ?? 0),
        available_count: Number(item?.available_count || 0),
        rejected_count: Number(item?.rejected_count ?? item?.count ?? 0),
        bind_limit_count: Number(item?.bind_limit_count || 0),
        remaining_capacity: Number(item?.remaining_capacity || 0),
        rate_limited_count: Number(item?.rate_limited_count || 0),
        cooldown_count: Number(item?.cooldown_count || 0),
        disabled_count: Number(item?.disabled_count || 0),
        cannot_send_count: Number(item?.cannot_send_count || 0),
        success_count: Number(item?.success_count || 0),
        failure_count: Number(item?.failure_count || 0),
        last_seen_at: String(item?.last_seen_at || ''),
      }))
      .filter((item) => item.prefix.length === 4 && (item.count > 0 || item.total > 0 || item.available_count > 0 || item.rejected_count > 0 || item.success_count > 0 || item.failure_count > 0))
  }, [])
  const availablePrefixes = useMemo(() => {
    const raw = summary.prefix_health?.available || summary.available_prefixes
    return raw
      ? normalizePrefixItems(raw)
      : []
  }, [normalizePrefixItems, summary.available_prefixes, summary.prefix_health?.available])
  const rejectedPrefixes = useMemo(() => {
    const raw = summary.prefix_health?.unavailable || summary.rejected_prefixes
    return raw ? normalizePrefixItems(raw) : []
  }, [normalizePrefixItems, summary.prefix_health?.unavailable, summary.rejected_prefixes])
  const exhaustedPrefixes = useMemo(() => {
    const raw = summary.prefix_health?.exhausted || summary.exhausted_prefixes
    return raw ? normalizePrefixItems(raw) : []
  }, [normalizePrefixItems, summary.exhausted_prefixes, summary.prefix_health?.exhausted])
  const temporaryPrefixes = useMemo(() => {
    const raw = summary.prefix_health?.temporary || summary.temporary_prefixes
    return raw ? normalizePrefixItems(raw) : []
  }, [normalizePrefixItems, summary.prefix_health?.temporary, summary.temporary_prefixes])
  const signupAvailablePrefixes = useMemo(() => {
    const raw = summary.phone_signup_prefix_health?.available || summary.phone_signup_available_prefixes
    return raw ? normalizePrefixItems(raw) : []
  }, [normalizePrefixItems, summary.phone_signup_available_prefixes, summary.phone_signup_prefix_health?.available])
  const signupUnavailablePrefixes = useMemo(() => {
    const raw = summary.phone_signup_prefix_health?.unavailable || summary.phone_signup_unavailable_prefixes
    return raw ? normalizePrefixItems(raw) : []
  }, [normalizePrefixItems, summary.phone_signup_prefix_health?.unavailable, summary.phone_signup_unavailable_prefixes])
  const filteredItems = useMemo(() => {
    const query = phoneSearch.replace(/\D/g, '')
    return items.filter((item) => {
      if (query && !String(item.phone_e164 || '').replace(/\D/g, '').includes(query)) return false
      if (!matchesPhoneBindingFilter(item, phoneBindingFilter)) return false
      if (!matchesApiExpiryFilter(item, apiExpiryFilter)) return false
      if (taskEligibilityFilter === 'eligible') return Boolean(item.ordinary_task_eligible)
      if (taskEligibilityFilter === 'prefix_blocked') return Boolean(item.self_available) && String(item.ordinary_task_block_reason || '').startsWith('prefix_')
      if (taskEligibilityFilter === 'self_blocked') return !item.self_available
      return true
    })
  }, [apiExpiryFilter, items, phoneBindingFilter, phoneSearch, taskEligibilityFilter])
  const paginatedItems = useMemo(() => {
    const start = (currentPage - 1) * PHONE_POOL_PAGE_SIZE
    return filteredItems.slice(start, start + PHONE_POOL_PAGE_SIZE)
  }, [currentPage, filteredItems])
  const hasActiveFilters = Boolean(
    phoneSearch.replace(/\D/g, '')
    || statusFilter
    || phoneBindingFilter
    || taskEligibilityFilter
    || apiExpiryFilter,
  )
  const phonePoolEmptyText = phoneSearch.replace(/\D/g, '')
    ? '未找到匹配手机号'
    : hasActiveFilters
      ? '没有符合当前筛选条件的手机号'
      : '暂无手机号'

  useEffect(() => {
    setCurrentPage(1)
    setSelectedRowKeys([])
  }, [apiExpiryFilter, phoneBindingFilter, phoneSearch, statusFilter, taskEligibilityFilter])

  useEffect(() => {
    const maxPage = Math.max(1, Math.ceil(filteredItems.length / PHONE_POOL_PAGE_SIZE))
    if (currentPage > maxPage) setCurrentPage(maxPage)
  }, [currentPage, filteredItems.length])

  const summaryItems = [
    { label: '全部', value: totalCount, detail: `当前显示 ${filteredItems.length}`, color: token.colorText },
    { label: '普通可选', value: availableCount, detail: `容量 ${remainingCapacity}`, color: token.colorSuccess },
    { label: '限流', value: rateLimitedCount, detail: '等待恢复', color: token.colorWarning },
    { label: '不可用', value: unavailableCount, detail: '无法发码/API异常', color: token.colorError },
    { label: '已绑满', value: exhaustedCount, detail: '达到上限', color: token.colorTextSecondary },
    { label: '停用', value: disabledCount, detail: '人工关闭', color: token.colorTextSecondary },
  ]
  const diagnosticItem = diagnostics?.item
  const diagnosticNotes = Array.isArray(diagnostics?.notes) ? diagnostics.notes : []
  const diagnosticAccounts = Array.isArray(diagnostics?.matched_accounts) ? diagnostics.matched_accounts : []
  const diagnosticCounts = diagnostics?.counts || {}

  const renderPrefixBlock = (
    title: string,
    prefixes: ReturnType<typeof normalizePrefixItems>,
    options: {
      border: string
      background: string
      tagColor: string
      summary: string
      empty: string
      renderDetail: (item: ReturnType<typeof normalizePrefixItems>[number]) => string
    },
  ) => (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: 12,
        flexWrap: 'wrap',
        border: `1px solid ${options.border}`,
        borderRadius: token.borderRadius,
        padding: '10px 12px',
        background: options.background,
      }}
    >
      <Space size={8} wrap>
        <Typography.Text strong>{title}</Typography.Text>
        <Tag color={options.tagColor}>{prefixes.length} 个号段</Tag>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>{options.summary}</Typography.Text>
        {prefixes.length > 0 ? (
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', maxHeight: 68, overflowY: 'auto' }}>
            {prefixes.map((item) => (
              <Tag key={item.prefix} color={options.tagColor}>
                {item.prefix} ({options.renderDetail(item)})
              </Tag>
            ))}
          </div>
        ) : (
          <Typography.Text type="secondary">{options.empty}</Typography.Text>
        )}
      </Space>
      <Space size={8} wrap>
        <Button
          size="small"
          icon={<CopyOutlined />}
          disabled={prefixes.length === 0}
          onClick={() => void copyPrefixes(prefixes, 'space')}
        >
          复制空格
        </Button>
        <Button
          size="small"
          icon={<CopyOutlined />}
          disabled={prefixes.length === 0}
          onClick={() => void copyPrefixes(prefixes, 'comma')}
        >
          复制逗号
        </Button>
      </Space>
    </div>
  )

  const renderApiExpiryCell = (record: PhonePoolItem, compact = false) => {
    const meta = apiExpiryMeta(record)
    const expired = formatApiExpiredDate(record.api_expired_date)
    const tooltip = [
      record.api_expired_date ? `到期: ${expired}` : '',
      meta.detail && !record.api_expired_date ? meta.detail : '',
      record.api_expiry_error ? `错误: ${record.api_expiry_error}` : '',
    ].filter(Boolean).join('\n')
    return (
      <Space direction="vertical" size={compact ? 1 : 2}>
        <Tooltip title={tooltip || undefined}>
          <Tag color={meta.color}>{meta.label}</Tag>
        </Tooltip>
        <Typography.Text
          type={record.api_expired_date ? undefined : 'secondary'}
          style={{ fontSize: compact ? 12 : 13, lineHeight: '18px' }}
        >
          {expired}
        </Typography.Text>
      </Space>
    )
  }

  const renderApiRouteCell = (record: PhonePoolItem, compact = false) => {
    const sourceUrl = sourceApiUrl(record)
    const forwardedUrl = forwardedApiUrl(record)
    const effectiveUrl = effectiveApiUrl(record)
    const sourceHost = sourceApiHost(record)
    const relayHost = forwardedApiHost(record)
    const forwarded = recordUsesForwarding(record)
    const routeTooltip = (
      <div style={{ maxWidth: 520 }}>
        <div>原始：{sourceUrl || '-'}</div>
        <div>实际：{effectiveUrl || '-'}</div>
      </div>
    )
    return (
      <div style={{ minWidth: 0, maxWidth: compact ? '100%' : 320 }}>
        <Space size={5} style={{ display: 'flex', minWidth: 0 }}>
          <Typography.Text
            ellipsis={{ tooltip: sourceHost || sourceUrl || '-' }}
            style={{ maxWidth: compact ? 112 : 126, fontSize: compact ? 12 : 13 }}
          >
            {sourceHost || '-'}
          </Typography.Text>
          {forwarded ? <SwapOutlined style={{ flex: '0 0 auto', color: token.colorTextTertiary }} /> : null}
          {forwarded ? (
            <Typography.Text
              ellipsis={{ tooltip: relayHost || forwardedUrl }}
              strong
              style={{ maxWidth: compact ? 112 : 126, fontSize: compact ? 12 : 13 }}
            >
              {relayHost || '-'}
            </Typography.Text>
          ) : null}
          {forwardStatusTag(record.forward_status, forwarded ? 'forwarded' : 'direct')}
        </Space>
        <Space size={3} style={{ display: 'flex', minWidth: 0, marginTop: 2 }}>
          <Tooltip title={routeTooltip}>
            <Typography.Text
              type="secondary"
              ellipsis
              style={{ display: 'block', minWidth: 0, flex: '1 1 auto', maxWidth: compact ? 240 : 282, fontSize: 11 }}
            >
              {apiRoutePath(effectiveUrl)}
            </Typography.Text>
          </Tooltip>
          {effectiveUrl ? (
            <Tooltip title={forwarded ? '复制转发API' : '复制完整API'}>
              <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => void copyPhoneApiLine(record)} />
            </Tooltip>
          ) : null}
        </Space>
      </div>
    )
  }

  const renderPhoneMobileCards = () => {
    if (!paginatedItems.length) {
      return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={phonePoolEmptyText} />
    }
    return (
      <div className="mobile-card-list">
        {paginatedItems.map((record) => {
          const id = Number(record.id)
          const checked = selectedRowKeys.includes(id)
          const emails = Array.isArray(record.bound_account_emails) ? record.bound_account_emails.filter(Boolean) : []
          const error = String(record.last_error_message || record.last_error_code || '').trim()
          return (
            <Card key={id} size="small" className="mobile-record-card">
              <div className="mobile-record-head">
                <Checkbox
                  checked={checked}
                  onChange={(event) => {
                    const nextChecked = event.target.checked
                    setSelectedRowKeys((current) => nextChecked
                      ? Array.from(new Set([...current, id]))
                      : current.filter((item) => item !== id))
                  }}
                />
                <div className="mobile-record-main">
                  <Typography.Text code copyable={{ text: phoneApiCopyText(record), tooltips: [effectiveApiUrl(record) ? forwardedApiUrl(record) ? '复制转发API' : '复制完整API' : '复制手机号', '已复制'] }} className="mobile-record-title">
                    {record.phone_e164}
                  </Typography.Text>
                  <div className="mobile-record-meta">
                    {statusTag(record.status, error)}
                    {taskEligibilityBlock(record, true)}
                    <Tag color={record.self_available ? 'blue' : 'default'}>余 {record.remaining_capacity}</Tag>
                    <Tag>{record.bound_count}/{record.max_accounts}</Tag>
                    {record.label ? <Tag>{record.label}</Tag> : null}
                  </div>
                </div>
              </div>
              <div className="mobile-record-section">
                <div className="mobile-record-field">
                  <span className="mobile-record-label">收码 API</span>
                  <span className="mobile-record-value">{renderApiRouteCell(record, true)}</span>
                </div>
                <div className="mobile-record-field">
                  <span className="mobile-record-label">API到期</span>
                  <span className="mobile-record-value">{renderApiExpiryCell(record, true)}</span>
                </div>
                <div className="mobile-record-field">
                  <span className="mobile-record-label">普通任务</span>
                  <span className="mobile-record-value">{taskEligibilityBlock(record, true)}</span>
                </div>
                <div className="mobile-record-field">
                  <span className="mobile-record-label">绑定邮箱</span>
                  <span className="mobile-record-value">{emails.slice(0, 2).join('，') || '-'}</span>
                </div>
                <div className="mobile-record-field">
                  <span className="mobile-record-label">成功 / 失败</span>
                  <span className="mobile-record-value">{record.success_count} / {record.fail_count}</span>
                </div>
                <div className="mobile-record-field">
                  <span className="mobile-record-label">最近绑定</span>
                  <span className="mobile-record-value">{formatBeijingTime(record.last_used_at)}</span>
                </div>
              </div>
              {error ? (
                <Typography.Text type="danger" style={{ display: 'block', marginTop: 8, fontSize: 12 }} ellipsis={{ tooltip: error }}>
                  {error}
                </Typography.Text>
              ) : null}
              <div className="mobile-record-actions">
                <Button size="small" icon={<BugOutlined />} onClick={() => openDiagnostics(record)}>诊断</Button>
                {record.status !== 'active' && record.status !== 'disabled' ? (
                  <Button size="small" icon={<UndoOutlined />} onClick={() => reset(record.id)}>恢复</Button>
                ) : null}
                <Button size="small" icon={record.status === 'disabled' ? <PlayCircleOutlined /> : <StopOutlined />} onClick={() => toggle(record)}>
                  {record.status === 'disabled' ? '启用' : '禁用'}
                </Button>
                <Popconfirm title="确认删除该手机号？" onConfirm={() => del(record.id)}>
                  <Button size="small" danger icon={<DeleteOutlined />}>删除</Button>
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
      title: '手机号',
      dataIndex: 'phone_e164',
      key: 'phone_e164',
      render: (value: string, record: PhonePoolItem) => (
        <Space direction="vertical" size={2}>
          <Typography.Text code copyable={{ text: phoneApiCopyText(record), tooltips: [effectiveApiUrl(record) ? forwardedApiUrl(record) ? '复制转发API' : '复制完整API' : '复制手机号', '已复制'] }}>{value}</Typography.Text>
          {record.label ? <Typography.Text type="secondary" style={{ fontSize: 12 }}>{record.label}</Typography.Text> : null}
        </Space>
      ),
    },
    {
      title: '收码 API',
      dataIndex: 'api_url',
      key: 'api_url',
      width: 344,
      render: (_value: string, record: PhonePoolItem) => renderApiRouteCell(record),
    },
    {
      title: 'API到期',
      dataIndex: 'api_expired_date',
      key: 'api_expired_date',
      width: 170,
      sorter: (a: PhonePoolItem, b: PhonePoolItem) => {
        const at = parseApiExpiryTime(a.api_expired_date)
        const bt = parseApiExpiryTime(b.api_expired_date)
        if (at === null && bt === null) return Number(a.id || 0) - Number(b.id || 0)
        if (at === null) return 1
        if (bt === null) return -1
        return at - bt
      },
      render: (_value: string, record: PhonePoolItem) => renderApiExpiryCell(record),
    },
    {
      title: '号码状态',
      dataIndex: 'status',
      key: 'status',
      width: 112,
      render: (value: string, record: PhonePoolItem) => statusTag(value, record.last_error_message || record.last_error_code),
    },
    {
      title: '普通任务',
      key: 'ordinary_task_eligible',
      width: 128,
      render: (_: any, record: PhonePoolItem) => taskEligibilityBlock(record),
    },
    {
      title: '已绑/上限',
      key: 'capacity',
      render: (_: any, record: PhonePoolItem) => (
        <Space>
          <Tag color={record.self_available ? 'blue' : 'default'}>{record.bound_count}/{record.max_accounts}</Tag>
          <Typography.Text type="secondary">余 {record.remaining_capacity}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '绑定邮箱',
      key: 'bound_account_emails',
      width: 300,
      render: (_: any, record: PhonePoolItem) => {
        const emails = Array.isArray(record.bound_account_emails) ? record.bound_account_emails.filter(Boolean) : []
        if (emails.length === 0) return <Typography.Text type="secondary">-</Typography.Text>
        return (
          <div style={{ width: 276, minWidth: 0 }}>
            <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 3, fontSize: 12 }}>
              共 {emails.length} 个
            </Typography.Text>
            <div
              style={{
                display: 'flex',
                flexDirection: 'column',
                gap: 2,
                maxHeight: 88,
                overflowY: emails.length > 3 ? 'auto' : 'visible',
                paddingRight: emails.length > 3 ? 4 : 0,
              }}
            >
              {emails.map((email) => (
                <Typography.Text
                  key={email}
                  copyable={{ text: email, tooltips: ['复制邮箱', '已复制'] }}
                  ellipsis={{ tooltip: email }}
                  style={{
                    display: 'block',
                    width: 276,
                    lineHeight: '20px',
                    fontSize: 12,
                  }}
                >
                  {email}
                </Typography.Text>
              ))}
            </div>
          </div>
        )
      },
    },
    {
      title: '成功/失败',
      key: 'stats',
      render: (_: any, record: PhonePoolItem) => (
        <Space>
          <Tag color="success">{record.success_count}</Tag>
          <Tag color="error">{record.fail_count}</Tag>
        </Space>
      ),
    },
    {
      title: '冷却/绑定时间',
      key: 'time',
      render: (_: any, record: PhonePoolItem) => (
        <Space direction="vertical" size={2}>
          <Typography.Text type={record.cooldown_until ? 'warning' : 'secondary'} style={{ fontSize: 12 }}>
            冷却：{formatBeijingTime(record.cooldown_until)}
          </Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            绑定：{formatBeijingTime(record.last_used_at)}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: '操作',
      key: 'actions',
      fixed: 'right',
      render: (_: any, record: PhonePoolItem) => (
        <Space>
          <Tooltip title="状态诊断">
            <Button type="text" size="small" icon={<BugOutlined />} onClick={() => openDiagnostics(record)} />
          </Tooltip>
          {record.status !== 'active' && record.status !== 'disabled' ? (
            <Tooltip title="恢复可用">
              <Button type="text" size="small" icon={<UndoOutlined />} onClick={() => reset(record.id)} />
            </Tooltip>
          ) : null}
          <Tooltip title={record.status === 'disabled' ? '启用' : '禁用'}>
            <Button
              type="text"
              size="small"
              icon={record.status === 'disabled' ? <PlayCircleOutlined /> : <StopOutlined />}
              onClick={() => toggle(record)}
            />
          </Tooltip>
          <Tooltip title="删除">
            <Popconfirm title="确认删除该手机号？" onConfirm={() => del(record.id)}>
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
          <h1 style={{ fontSize: 24, fontWeight: 'bold', margin: 0 }}>手机号池</h1>
          <p style={{ color: token.colorTextSecondary, marginTop: 4, marginBottom: 0 }}>
            固定 relay 自有号库存，供 OpenAI 手机号绑定任务自动取号。
          </p>
        </div>
        <Space wrap>
          <Space size={4}>
            <Button
              icon={<SwapOutlined />}
              onClick={openForwardingSettings}
              loading={forwardingLoading && !forwardingConfig}
            >
              API 转发
            </Button>
            {forwardingError && !forwardingConfig
              ? <Tag color="error">状态未知</Tag>
              : forwardStatusTag(forwardingStatus, forwardingConfig?.enabled ? 'forwarded' : 'direct')}
          </Space>
          <Button icon={<UploadOutlined />} onClick={() => setImportOpen((open) => !open)}>
            {importOpen ? '收起导入' : '导入号码'}
          </Button>
          <Button icon={<DatabaseOutlined />} onClick={reconcile} loading={reconciling}>同步已绑定数</Button>
          <Button
            icon={<ReloadOutlined />}
            onClick={() => {
              void load()
            }}
            loading={loading}
          >
            刷新
          </Button>
        </Space>
      </div>

      {renderPrefixBlock('可用号段', availablePrefixes, {
        border: token.colorSuccessBorder,
        background: token.colorSuccessBg,
        tagColor: 'green',
        summary: `普通绑定可用容量 ${remainingCapacity}`,
        empty: '暂无健康可用号段',
        renderDetail: (item) => `可用 ${item.available_count} / 容量 ${item.remaining_capacity}`,
      })}

      {renderPrefixBlock('不可用号段', rejectedPrefixes, {
        border: token.colorErrorBorder,
        background: token.colorErrorBg,
        tagColor: 'red',
        summary: `OpenAI 拒绝 ${rejectedPhoneCount} 个号码，普通绑定已跳过这些号段`,
        empty: '暂无 OpenAI 拒绝号段',
        renderDetail: (item) => `拒绝 ${item.rejected_count} / 号码可用 ${item.available_count} / 容量 ${item.remaining_capacity}`,
      })}

      {signupAvailablePrefixes.length > 0 ? renderPrefixBlock('可注册号段', signupAvailablePrefixes, {
        border: token.colorSuccessBorder,
        background: token.colorSuccessBg,
        tagColor: 'green',
        summary: '手机号注册成功样本命中的号段；只影响注册号段状态，不改号码自身状态',
        empty: '暂无可注册号段',
        renderDetail: (item) => `成功 ${item.success_count} / 失败 ${item.failure_count}`,
      }) : null}

      {signupUnavailablePrefixes.length > 0 ? renderPrefixBlock('不可注册号段', signupUnavailablePrefixes, {
        border: token.colorErrorBorder,
        background: token.colorErrorBg,
        tagColor: 'volcano',
        summary: '没有成功样本但有注册失败样本；只作为注册号段状态，不会禁用手机号',
        empty: '暂无不可注册号段',
        renderDetail: (item) => `失败 ${item.failure_count} / 成功 ${item.success_count}`,
      }) : null}

      {exhaustedPrefixes.length > 0 ? renderPrefixBlock('号段绑定上限', exhaustedPrefixes, {
        border: token.colorBorderSecondary,
        background: token.colorFillAlter,
        tagColor: 'default',
        summary: '这些号段没有被 OpenAI 拒绝，只是号码容量已用完',
        empty: '暂无绑满号段',
        renderDetail: (item) => `绑满 ${item.bind_limit_count} / 总 ${item.total}`,
      }) : null}

      {temporaryPrefixes.length > 0 ? renderPrefixBlock('暂不可用号段', temporaryPrefixes, {
        border: token.colorWarningBorder,
        background: token.colorWarningBg,
        tagColor: 'gold',
        summary: '冷却、限流、停用等暂不进入普通绑定的号段',
        empty: '暂无暂不可用号段',
        renderDetail: (item) => `限流 ${item.rate_limited_count} / 冷却 ${item.cooldown_count} / 停用 ${item.disabled_count}`,
      }) : null}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(136px, 1fr))', gap: 10 }}>
        {summaryItems.map((item) => (
          <div
            key={item.label}
            style={{
              border: `1px solid ${token.colorBorderSecondary}`,
              borderRadius: token.borderRadiusLG,
              padding: '10px 12px',
              background: token.colorBgContainer,
            }}
          >
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>{item.label}</Typography.Text>
            <div style={{ color: item.color, fontSize: 22, fontWeight: 700, lineHeight: 1.2 }}>{item.value}</div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>{item.detail}</Typography.Text>
          </div>
        ))}
      </div>

      {importOpen ? (
        <Card
          title="导入 relay 自有号"
          extra={<Button type="text" size="small" onClick={() => setImportOpen(false)}>收起</Button>}
        >
          <Space direction="vertical" style={{ width: '100%' }}>
            <Input.TextArea
              value={importText}
              onChange={(e) => setImportText(e.target.value)}
              autoSize={{ minRows: 4, maxRows: 10 }}
              placeholder={'+13434832954----https://api.sms8.net/api/record?token=xxx\n+12082260171|https://sms24.uk/api/sms/recordText?token=xxx&tpl=1'}
              style={{ fontFamily: 'monospace' }}
            />
            <Typography.Text type="secondary">
              每行一个：+手机号----收码API，也兼容 +手机号|收码API。相同手机号会替换新的 API/token，不会重置号码状态、绑定次数和历史记录；同批重复以后面的为准。
            </Typography.Text>
            <div>
              <Button type="primary" icon={<UploadOutlined />} loading={importing} disabled={!importText.trim()} onClick={importPhones}>
                导入号码
              </Button>
            </div>
          </Space>
        </Card>
      ) : null}

      <Card
        title="号码列表"
        extra={
          <Space wrap>
            <Input.Search
              value={phoneSearch}
              allowClear
              inputMode="tel"
              placeholder="搜索手机号"
              style={{ width: 220 }}
              onChange={(event) => setPhoneSearch(event.target.value)}
            />
            <Select
              value={statusFilter}
              options={STATUS_FILTER_OPTIONS}
              style={{ width: 146 }}
              onChange={(value) => setStatusFilter(value)}
            />
            <Select
              value={phoneBindingFilter}
              options={PHONE_BINDING_FILTER_OPTIONS}
              style={{ width: 132 }}
              onChange={(value) => setPhoneBindingFilter(value as PhoneBindingFilter)}
            />
            <Select
              value={taskEligibilityFilter}
              options={TASK_ELIGIBILITY_FILTER_OPTIONS as any}
              style={{ width: 148 }}
              onChange={(value) => setTaskEligibilityFilter(value as TaskEligibilityFilter)}
            />
            <Select
              value={apiExpiryFilter}
              options={API_EXPIRY_FILTER_OPTIONS as any}
              style={{ width: 148 }}
              onChange={(value) => setApiExpiryFilter(value as ApiExpiryFilter)}
            />
            <Popconfirm
              title={`确认重新刷新全部 ${totalCount} 个手机号的 API 到期时间？`}
              description="会重新请求每个收码 API，即使已有到期时间也会覆盖；数量多时会比较慢。"
              disabled={totalCount === 0}
              onConfirm={() => void refreshApiExpiry('all')}
            >
              <Button
                size="small"
                icon={<SyncOutlined />}
                disabled={totalCount === 0}
                loading={apiExpiryLoading === 'all'}
              >
                刷新全部API到期
              </Button>
            </Popconfirm>
            <Button
              size="small"
              icon={<ReloadOutlined />}
              disabled={paginatedItems.length === 0}
              loading={apiExpiryLoading === 'page'}
              onClick={() => void refreshApiExpiry('page')}
            >
              补全本页API到期
            </Button>
            <Button
              size="small"
              icon={<ReloadOutlined />}
              disabled={selectedRowKeys.length === 0}
              loading={apiExpiryLoading === 'selected'}
              onClick={() => void refreshApiExpiry('selected')}
            >
              补全选中
            </Button>
            <Button
              size="small"
              icon={<DownloadOutlined />}
              disabled={selectedRowKeys.length === 0 && filteredItems.length === 0}
              onClick={exportPhones}
            >
              {selectedRowKeys.length > 0 ? '导出选中手机号' : '导出当前类别手机号'}
            </Button>
            <Typography.Text type="secondary">显示 {filteredItems.length} 个</Typography.Text>
            <Typography.Text type={selectedRowKeys.length > 0 ? 'success' : 'secondary'}>
              已选 {selectedRowKeys.length} 个
            </Typography.Text>
            <Popconfirm
              title={`确认恢复选中的 ${selectedRowKeys.length} 个手机号？`}
              disabled={selectedRowKeys.length === 0}
              onConfirm={() => runBatchAction('reset')}
            >
              <Button
                size="small"
                icon={<UndoOutlined />}
                disabled={selectedRowKeys.length === 0}
                loading={batchAction === 'reset'}
              >
                恢复
              </Button>
            </Popconfirm>
            <Popconfirm
              title={`确认禁用选中的 ${selectedRowKeys.length} 个手机号？`}
              disabled={selectedRowKeys.length === 0}
              onConfirm={() => runBatchAction('disable')}
            >
              <Button
                size="small"
                icon={<StopOutlined />}
                disabled={selectedRowKeys.length === 0}
                loading={batchAction === 'disable'}
              >
                禁用
              </Button>
            </Popconfirm>
            <Popconfirm
              title={`确认删除选中的 ${selectedRowKeys.length} 个手机号？`}
              disabled={selectedRowKeys.length === 0}
              onConfirm={() => runBatchAction('delete')}
            >
              <Button
                size="small"
                danger
                icon={<DeleteOutlined />}
                disabled={selectedRowKeys.length === 0}
                loading={batchAction === 'delete'}
              >
                删除
              </Button>
            </Popconfirm>
          </Space>
        }
      >
        {isMobile ? (
          renderPhoneMobileCards()
        ) : (
          <Table
            rowKey="id"
            columns={columns}
            dataSource={paginatedItems}
            loading={loading}
            rowSelection={{
              selectedRowKeys,
              preserveSelectedRowKeys: true,
              onChange: (keys) => setSelectedRowKeys(keys.map((key) => Number(key))),
            }}
            pagination={false}
            locale={{ emptyText: phonePoolEmptyText }}
            scroll={{ x: 1680, y: 520 }}
            sticky
          />
        )}
        <div style={{ display: 'flex', justifyContent: 'flex-end', paddingTop: 12 }}>
          <Pagination
            current={currentPage}
            pageSize={PHONE_POOL_PAGE_SIZE}
            total={filteredItems.length}
            showSizeChanger={false}
            showLessItems
            responsive
            showTotal={(total) => `共 ${total} 个`}
            onChange={setCurrentPage}
          />
        </div>
      </Card>

      <Drawer
        title="手机号 API 转发"
        width={isMobile ? '100%' : 540}
        open={forwardingOpen}
        maskClosable={!forwardingSaving}
        keyboard={!forwardingSaving}
        onClose={() => {
          if (forwardingSaving) return
          setForwardingOpen(false)
          setForwardingError('')
          if (forwardingConfig) applyForwardingDraft(forwardingConfig)
        }}
        footer={(
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <Button
              disabled={forwardingSaving}
              onClick={() => {
                setForwardingOpen(false)
                setForwardingError('')
                if (forwardingConfig) applyForwardingDraft(forwardingConfig)
              }}
            >
              取消
            </Button>
            <Button
              type="primary"
              loading={forwardingSaving}
              disabled={forwardingLoading || forwardingSyncing || (forwardingDraftEnabled && !forwardingDraftOrigin.trim())}
              onClick={() => void saveForwardingSettings()}
            >
              保存配置
            </Button>
          </div>
        )}
      >
        {forwardingLoading && !forwardingConfig ? (
          <Skeleton active paragraph={{ rows: 8 }} />
        ) : (
          <Space direction="vertical" size={14} style={{ width: '100%' }}>
            {forwardingError ? (
              <Alert
                type="error"
                showIcon
                message={forwardingError}
                action={(
                  <Button size="small" loading={forwardingLoading} onClick={() => void loadForwarding(false)}>
                    重新读取
                  </Button>
                )}
              />
            ) : null}

            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 16 }}>
              <div style={{ minWidth: 0 }}>
                <Typography.Text strong>启用 API 转发</Typography.Text>
                <Typography.Text type="secondary" style={{ display: 'block', marginTop: 2, fontSize: 12 }}>
                  关闭后号码继续使用原始接码 API。
                </Typography.Text>
              </div>
              <Switch
                checked={forwardingDraftEnabled}
                disabled={forwardingLoading || forwardingSaving || forwardingSyncing}
                onChange={setForwardingDraftEnabled}
              />
            </div>

            <div>
              <Typography.Text strong>当前主域名</Typography.Text>
              <Input
                value={forwardingDraftOrigin}
                disabled={forwardingLoading || forwardingSaving}
                placeholder="https://relay.example.com"
                style={{ marginTop: 6 }}
                onChange={(event) => setForwardingDraftOrigin(event.target.value)}
              />
            </div>

            <div>
              <Space size={6}>
                <Typography.Text strong>兼容旧域名</Typography.Text>
                <Tag>{String(forwardingDraftPrevious || '').split(/[\n,]+/).filter((item) => item.trim()).length}</Tag>
              </Space>
              <Input.TextArea
                value={forwardingDraftPrevious}
                disabled={forwardingLoading || forwardingSaving}
                autoSize={{ minRows: 2, maxRows: 6 }}
                placeholder={'https://relay-old.example.com\nhttps://relay-legacy.example.com'}
                style={{ marginTop: 6, fontFamily: 'monospace' }}
                onChange={(event) => setForwardingDraftPrevious(event.target.value)}
              />
            </div>

            <Alert
              type="info"
              showIcon
              message="域名变更后，新链接使用当前主域名；旧域名还需同步配置 DNS、证书和 Nginx 指向本 Relay。"
            />

            <div>
              <Typography.Text strong>影响范围</Typography.Text>
              <Descriptions size="small" bordered column={isMobile ? 1 : 2} style={{ marginTop: 6 }}>
                <Descriptions.Item label="手机号">{forwardingAffectedRecords}</Descriptions.Item>
                <Descriptions.Item label="来源域名">{forwardingSourceHostCount}</Descriptions.Item>
                <Descriptions.Item label="转发状态">
                  {forwardStatusTag(forwardingStatus, forwardingConfig?.enabled ? 'forwarded' : 'direct')}
                </Descriptions.Item>
                <Descriptions.Item label="Relay">
                  <Tag color={forwardingConfig?.relay_configured ? 'success' : 'warning'}>
                    {forwardingConfig?.relay_configured ? '已配置' : '未配置'}
                  </Tag>
                </Descriptions.Item>
                {forwardingRegistryStatus ? (
                  <Descriptions.Item label="Registry" span={isMobile ? 1 : 2}>
                    {forwardStatusTag(forwardingRegistryStatus)}
                  </Descriptions.Item>
                ) : null}
              </Descriptions>
            </div>

            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                <Space size={6} wrap>
                  <Typography.Text strong>同步状态</Typography.Text>
                  {forwardStatusTag(forwardingSync.status, forwardingConfig?.enabled ? 'unknown' : 'direct')}
                </Space>
                <Button
                  size="small"
                  icon={<CloudSyncOutlined />}
                  loading={forwardingSyncing}
                  disabled={forwardingLoading || forwardingSaving || !forwardingConfig?.relay_configured}
                  onClick={() => void retryForwardingSync()}
                >
                  重试同步
                </Button>
              </div>
              <Descriptions size="small" column={1} style={{ marginTop: 6 }}>
                <Descriptions.Item label="最近尝试">{formatBeijingTime(forwardingSync.last_attempt_at)}</Descriptions.Item>
                <Descriptions.Item label="最近成功">{formatBeijingTime(forwardingSync.last_success_at)}</Descriptions.Item>
                <Descriptions.Item label="路由 / 归属 / 库存">
                  {Number(forwardingSync.route_count || 0)} / {Number(forwardingSync.owner_count || 0)} / {Number(forwardingSync.inventory_count || 0)}
                </Descriptions.Item>
                {forwardingSync.trigger ? <Descriptions.Item label="触发来源">{forwardingSync.trigger}</Descriptions.Item> : null}
              </Descriptions>
              {forwardingSync.last_error ? (
                <Alert type="error" showIcon message={forwardingSync.last_error} style={{ marginTop: 6 }} />
              ) : null}
            </div>
          </Space>
        )}
      </Drawer>

      <Drawer
        title="手机号状态诊断"
        width={680}
        open={diagnosticsOpen}
        onClose={() => {
          setDiagnosticsOpen(false)
          setDiagnosticsRecordId(null)
          setDiagnostics(null)
        }}
        extra={(
          <Space>
            <Button
              size="small"
              icon={<ReloadOutlined />}
              loading={diagnosticsLoading}
              disabled={!diagnosticsRecordId}
              onClick={() => diagnosticsRecordId && void loadDiagnostics(diagnosticsRecordId)}
            >
              刷新诊断
            </Button>
            <Button size="small" icon={<SyncOutlined />} loading={reconciling} onClick={() => void reconcile()}>
              同步已绑定数
            </Button>
          </Space>
        )}
      >
        {diagnosticItem ? (
          <Space direction="vertical" size={14} style={{ width: '100%' }}>
            <Space wrap>
              <Typography.Text code>{diagnosticItem.phone_e164}</Typography.Text>
              {statusTag(diagnosticItem.status, diagnosticItem.last_error_message || diagnosticItem.last_error_code)}
              {prefixStatusTag(diagnosticItem, true)}
              {taskEligibilityBlock(diagnosticItem, true)}
              <Tag color={diagnosticItem.self_available ? 'blue' : 'default'}>
                余 {diagnosticItem.remaining_capacity} / 上限 {diagnosticItem.max_accounts}
              </Tag>
              {diagnosticItem.label ? <Tag>{diagnosticItem.label}</Tag> : null}
            </Space>

            {diagnosticNotes.length ? (
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                {diagnosticNotes.map((note, index) => (
                  <Alert
                    key={`${note.code || 'note'}-${index}`}
                    showIcon
                    type={noteAlertType(note.severity)}
                    message={note.message || note.code || '诊断信息'}
                  />
                ))}
              </Space>
            ) : (
              <Alert type="info" showIcon message="暂无诊断提示" />
            )}

            <Descriptions size="small" bordered column={1}>
              <Descriptions.Item label="原始 API">
                {sourceApiUrl(diagnosticItem) ? (
                  <Typography.Text ellipsis={{ tooltip: sourceApiUrl(diagnosticItem) }}>
                    {sourceApiUrl(diagnosticItem)}
                  </Typography.Text>
                ) : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="转发 API">
                {forwardedApiUrl(diagnosticItem) ? (
                  <Typography.Text copyable={{ text: phoneApiCopyText(diagnosticItem), tooltips: ['复制转发API', '已复制'] }} ellipsis={{ tooltip: forwardedApiUrl(diagnosticItem) }}>
                    {forwardedApiUrl(diagnosticItem)}
                  </Typography.Text>
                ) : <Typography.Text type="secondary">{missingForwardedApiText(diagnosticItem.forward_status)}</Typography.Text>}
              </Descriptions.Item>
              <Descriptions.Item label="原始 Host">{sourceApiHost(diagnosticItem) || '-'}</Descriptions.Item>
              <Descriptions.Item label="转发 Host">{forwardedApiHost(diagnosticItem) || '-'}</Descriptions.Item>
              <Descriptions.Item label="转发状态">
                {forwardStatusTag(diagnosticItem.forward_status, recordUsesForwarding(diagnosticItem) ? 'forwarded' : 'direct')}
              </Descriptions.Item>
              <Descriptions.Item label="API到期">{renderApiExpiryCell(diagnosticItem)}</Descriptions.Item>
              <Descriptions.Item label="号码自身">{selfStatusDetail(diagnosticItem)}</Descriptions.Item>
              <Descriptions.Item label="号段状态">{prefixStatusTag(diagnosticItem)}</Descriptions.Item>
              <Descriptions.Item label="普通任务">{taskEligibilityBlock(diagnosticItem)}</Descriptions.Item>
              <Descriptions.Item label="本地已绑 / 账号记录">
                {Number(diagnosticCounts.local_bound_count || 0)} / {Number(diagnosticCounts.matched_bound_count || 0)}
                <Typography.Text type="secondary">，匹配账号 {Number(diagnosticCounts.matched_account_count || 0)} 个</Typography.Text>
              </Descriptions.Item>
              <Descriptions.Item label="成功 / 失败">
                <Tag color="success">{diagnosticItem.success_count}</Tag>
                <Tag color="error">{diagnosticItem.fail_count}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="最近绑定">{formatBeijingTime(diagnosticItem.last_used_at)}</Descriptions.Item>
              <Descriptions.Item label="冷却到">{formatBeijingTime(diagnosticItem.cooldown_until)}</Descriptions.Item>
              <Descriptions.Item label="最近错误">
                {diagnosticItem.last_error_code || diagnosticItem.last_error_message ? (
                  <Space direction="vertical" size={2}>
                    {diagnosticItem.last_error_code ? <Tag color="error">{diagnosticItem.last_error_code}</Tag> : null}
                    {diagnosticItem.last_error_message ? <Typography.Text>{diagnosticItem.last_error_message}</Typography.Text> : null}
                  </Space>
                ) : '-'}
              </Descriptions.Item>
            </Descriptions>

            <Space wrap>
              <Button
                icon={<CopyOutlined />}
                disabled={!sourceApiUrl(diagnosticItem)}
                onClick={() => void copyPhoneApiLine(diagnosticItem, 'source')}
              >
                复制原始API
              </Button>
              <Button
                icon={<ReloadOutlined />}
                loading={apiExpiryLoading === 'record'}
                onClick={() => void refreshApiExpiry('record', diagnosticItem.id)}
              >
                补全当前API到期
              </Button>
              {diagnosticItem.status !== 'active' ? (
                <Button icon={<UndoOutlined />} onClick={() => void reset(diagnosticItem.id)}>
                  恢复可用
                </Button>
              ) : null}
              <Button
                icon={diagnosticItem.status === 'disabled' ? <PlayCircleOutlined /> : <StopOutlined />}
                onClick={() => void toggle(diagnosticItem)}
              >
                {diagnosticItem.status === 'disabled' ? '启用号码' : '禁用号码'}
              </Button>
              <Popconfirm title="确认删除该手机号？" onConfirm={() => void del(diagnosticItem.id)}>
                <Button danger icon={<DeleteOutlined />}>删除号码</Button>
              </Popconfirm>
            </Space>

            <div>
              <Typography.Text strong>绑定账号</Typography.Text>
              {diagnosticAccounts.length ? (
                <Table
                  size="small"
                  rowKey={(row: any) => String(row.id || row.email)}
                  columns={[
                    {
                      title: '邮箱',
                      dataIndex: 'email',
                      render: (value: string) => <Typography.Text copyable={{ text: value }} ellipsis>{value || '-'}</Typography.Text>,
                    },
                    {
                      title: '账号状态',
                      dataIndex: 'account_status',
                      width: 100,
                      render: (value: string) => <Tag>{value || '-'}</Tag>,
                    },
                    {
                      title: '绑定状态',
                      dataIndex: 'binding_status',
                      width: 100,
                      render: (value: string) => <Tag color={value === 'bound' ? 'success' : 'default'}>{value || '-'}</Tag>,
                    },
                    {
                      title: '时间',
                      dataIndex: 'bound_at',
                      width: 150,
                      render: (value: string) => <Typography.Text type="secondary" style={{ fontSize: 12 }}>{formatBeijingTime(value)}</Typography.Text>,
                    },
                  ]}
                  dataSource={diagnosticAccounts}
                  pagination={false}
                  style={{ marginTop: 8 }}
                />
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有账号记录绑定这个号码" />
              )}
            </div>
          </Space>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={diagnosticsLoading ? '正在读取诊断...' : '选择一行后查看诊断'} />
        )}
      </Drawer>
    </div>
  )
}
