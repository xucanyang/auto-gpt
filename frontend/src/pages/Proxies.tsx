import { useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Card, Checkbox, Col, Descriptions, Drawer, Empty, Grid, Input, InputNumber, Popconfirm, Progress, Row, Select, Space, Switch, Table, Tag, Tooltip, Typography, message } from 'antd'
import {
  PlusOutlined,
  DeleteOutlined,
  ReloadOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  SwapRightOutlined,
  SwapLeftOutlined,
  SearchOutlined,
  ThunderboltOutlined,
  PauseCircleOutlined,
  WarningOutlined,
  BugOutlined,
} from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'
import { saveTaskProxySettingsToConfig } from '@/lib/taskProxySettings'

interface ProxyRecord {
  id: number
  url: string
  region?: string
  scheme?: string
  host?: string
  port?: number
  is_active?: boolean
  success_count?: number
  fail_count?: number
  homepage_success_count?: number
  homepage_fail_count?: number
  homepage_consecutive_failures?: number
  homepage_circuit_open_until?: string | null
  homepage_last_error?: string | null
  desired_country_code?: string
  provider?: string
  note?: string
  exit_ip?: string
  exit_country_code?: string
  exit_country_name?: string
  exit_region_name?: string
  exit_city?: string
  exit_asn?: string
  exit_isp?: string
  scan_status?: string
  last_scan_at?: string | null
  last_latency_ms?: number
  last_error_code?: string
  last_error?: string | null
  chatgpt_status?: string
  chatgpt_status_code?: number
  chatgpt_latency_ms?: number
  chatgpt_last_checked_at?: string | null
  chatgpt_last_error?: string | null
  health_score?: number
  consecutive_failures?: number
  cooldown_until?: string | null
  last_probe_json?: string
}

interface ConfigPayload {
  proxy_pool_cooldown_enabled?: boolean | string
  proxy_scan_enabled?: boolean | string
  proxy_scan_interval_minutes?: string
  proxy_scan_concurrency?: string
  proxy_scan_timeout_seconds?: string
  proxy_scan_targets?: string
  proxy_scan_only_active?: boolean | string
  proxy_scan_min_score?: string
  proxy_pool_max_candidates?: string
  dynamic_proxy_template?: string
  dynamic_proxy_default_country?: string
  dynamic_proxy_probe_enabled?: boolean | string
  dynamic_proxy_require_country_match?: boolean | string
  dynamic_proxy_probe_timeout_seconds?: string
}

interface ProxyScanJob {
  job_id: string
  status: string
  total: number
  done: number
  ok: number
  failed: number
  degraded: number
  targets?: string[]
  recent_results?: Array<Record<string, unknown>>
  error?: string
}

interface ProxyDiagnostics {
  item?: ProxyRecord
  masked_url?: string
  endpoint?: {
    scheme?: string
    host?: string
    port?: number
  }
  last_probe?: Record<string, unknown>
  scheduler?: Record<string, unknown>
  notes?: Array<{
    severity?: string
    code?: string
    message?: string
  }>
}

type ProxyStatusFilter = 'all' | 'active' | 'disabled' | 'cooling' | 'failed' | 'healthy' | 'degraded' | 'unchecked'

function normalizeProxyLines(value: string) {
  const seen = new Set<string>()
  const duplicates: string[] = []
  const lines: string[] = []
  value
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .forEach((line) => {
      if (seen.has(line)) {
        duplicates.push(line)
        return
      }
      seen.add(line)
      lines.push(line)
    })
  return { lines, duplicates }
}

function isCooling(proxy: ProxyRecord) {
  return Boolean(proxy.homepage_circuit_open_until || proxy.cooldown_until)
}

function hasRecentFailure(proxy: ProxyRecord) {
  return (
    Number(proxy.homepage_consecutive_failures || 0) > 0
    || Number(proxy.consecutive_failures || 0) > 0
    || Boolean(proxy.homepage_last_error || proxy.last_error || proxy.chatgpt_last_error)
  )
}

function getProxyHealth(proxy: ProxyRecord) {
  if (!proxy.is_active) return { label: '禁用', color: 'default', icon: <PauseCircleOutlined /> }
  if (isCooling(proxy)) return { label: '冷却中', color: 'warning', icon: <WarningOutlined /> }
  const scanStatus = String(proxy.scan_status || '').toLowerCase()
  if (scanStatus === 'failed') return { label: '不可用', color: 'error', icon: <CloseCircleOutlined /> }
  if (scanStatus === 'degraded') return { label: '降级', color: 'orange', icon: <WarningOutlined /> }
  if (scanStatus === 'unchecked' || !scanStatus) return { label: '未扫描', color: 'default', icon: <WarningOutlined /> }
  if (hasRecentFailure(proxy)) return { label: '需关注', color: 'error', icon: <CloseCircleOutlined /> }
  return { label: '可用', color: 'success', icon: <CheckCircleOutlined /> }
}

function maskProxyUrl(value: string) {
  const text = String(value || '').trim()
  if (!text) return ''
  try {
    const url = new URL(text)
    if (url.username || url.password) {
      url.username = '***'
      url.password = '***'
      return url.toString()
    }
  } catch {
    // 兼容非标准代理输入
  }
  if (text.includes('@') && text.includes('://')) {
    const [scheme, rest] = text.split('://', 2)
    return `${scheme}://***:***@${rest.split('@').slice(1).join('@')}`
  }
  return text
}

function scanStatusLabel(value?: string) {
  const status = String(value || 'unchecked').toLowerCase()
  if (status === 'ok') return { label: '可用', color: 'success' }
  if (status === 'degraded') return { label: '降级', color: 'orange' }
  if (status === 'failed') return { label: '失败', color: 'error' }
  return { label: '未扫描', color: 'default' }
}

function chatgptStatusLabel(value?: string) {
  const status = String(value || 'unchecked').toLowerCase()
  if (status === 'ok') return { label: '可用', color: 'success' }
  if (status === 'blocked_403') return { label: '403', color: 'error' }
  if (status === 'rate_limited_429') return { label: '429', color: 'warning' }
  if (status === 'timeout') return { label: '超时', color: 'warning' }
  if (status === 'unchecked') return { label: '未检测', color: 'default' }
  return { label: status || '失败', color: 'error' }
}

function noteAlertType(severity?: string): 'success' | 'info' | 'warning' | 'error' {
  const value = String(severity || '').toLowerCase()
  if (value === 'success') return 'success'
  if (value === 'error') return 'error'
  if (value === 'warning') return 'warning'
  return 'info'
}

function formatDateTime(value?: string | null) {
  if (!value) return '未扫描'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString()
}

function statBox(label: string, value: number, tone: 'primary' | 'default' | 'warning' | 'error' = 'default') {
  const toneStyle = {
    primary: { background: 'rgba(99, 102, 241, 0.1)', border: 'rgba(99, 102, 241, 0.28)' },
    default: { background: 'rgba(122, 139, 163, 0.08)', border: 'rgba(122, 139, 163, 0.18)' },
    warning: { background: 'rgba(250, 173, 20, 0.1)', border: 'rgba(250, 173, 20, 0.28)' },
    error: { background: 'rgba(255, 77, 79, 0.08)', border: 'rgba(255, 77, 79, 0.24)' },
  }[tone]
  return (
    <div
      style={{
        padding: '12px 14px',
        borderRadius: 12,
        border: `1px solid ${toneStyle.border}`,
        background: toneStyle.background,
        minHeight: 78,
      }}
    >
      <Typography.Text type="secondary" style={{ display: 'block', fontSize: 12 }}>{label}</Typography.Text>
      <Typography.Text strong style={{ display: 'block', marginTop: 6, fontSize: 24, lineHeight: 1.1 }}>{value}</Typography.Text>
    </div>
  )
}

export default function Proxies() {
  const screens = Grid.useBreakpoint()
  const isMobile = screens.lg === false
  const [proxies, setProxies] = useState<ProxyRecord[]>([])
  const [newProxy, setNewProxy] = useState('')
  const [region, setRegion] = useState('')
  const [searchText, setSearchText] = useState('')
  const [statusFilter, setStatusFilter] = useState<ProxyStatusFilter>('all')
  const [regionFilter, setRegionFilter] = useState<string>('all')
  const [countryFilter, setCountryFilter] = useState<string>('all')
  const [checking, setChecking] = useState(false)
  const [clearingCooldowns, setClearingCooldowns] = useState(false)
  const [cooldownEnabled, setCooldownEnabled] = useState(true)
  const [autoScanEnabled, setAutoScanEnabled] = useState(false)
  const [scanOnlyActive, setScanOnlyActive] = useState(true)
  const [scanIntervalMinutes, setScanIntervalMinutes] = useState(30)
  const [scanConcurrency, setScanConcurrency] = useState(8)
  const [scanTimeoutSeconds, setScanTimeoutSeconds] = useState(8)
  const [scanMinScore, setScanMinScore] = useState(50)
  const [poolMaxCandidates, setPoolMaxCandidates] = useState(5)
  const [dynamicProxyTemplate, setDynamicProxyTemplate] = useState('')
  const [dynamicProxyCountry, setDynamicProxyCountry] = useState('JP')
  const [dynamicProxyProbe, setDynamicProxyProbe] = useState(true)
  const [dynamicPreviewLoading, setDynamicPreviewLoading] = useState(false)
  const [dynamicSaving, setDynamicSaving] = useState(false)
  const [dynamicPreviewResult, setDynamicPreviewResult] = useState<Record<string, any> | null>(null)
  const [scanJob, setScanJob] = useState<ProxyScanJob | null>(null)
  const scanPollTimerRef = useRef<number | null>(null)
  const [schedulerStatus, setSchedulerStatus] = useState<Record<string, unknown> | null>(null)
  const [savingCooldownSetting, setSavingCooldownSetting] = useState(false)
  const [savingScanSetting, setSavingScanSetting] = useState(false)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [selectedProxyIds, setSelectedProxyIds] = useState<number[]>([])
  const [bulkDeleting, setBulkDeleting] = useState(false)
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false)
  const [diagnosticsLoading, setDiagnosticsLoading] = useState(false)
  const [diagnosticsProxyId, setDiagnosticsProxyId] = useState<number | null>(null)
  const [proxyDiagnostics, setProxyDiagnostics] = useState<ProxyDiagnostics | null>(null)

  const parsedInput = useMemo(() => normalizeProxyLines(newProxy), [newProxy])
  const existingProxyUrls = useMemo(() => new Set(proxies.map((proxy) => proxy.url)), [proxies])
  const existingInputCount = parsedInput.lines.filter((line) => existingProxyUrls.has(line)).length

  const summary = useMemo(() => {
    const total = proxies.length
    const active = proxies.filter((proxy) => proxy.is_active).length
    const cooling = proxies.filter(isCooling).length
    const failed = proxies.filter(hasRecentFailure).length
    const healthy = proxies.filter((proxy) => String(proxy.scan_status || '').toLowerCase() === 'ok').length
    const chatgptOk = proxies.filter((proxy) => String(proxy.chatgpt_status || '').toLowerCase() === 'ok').length
    const exitCountries = new Set(
      proxies
        .map((proxy) => String(proxy.exit_country_code || '').trim().toUpperCase())
        .filter(Boolean),
    )
    return {
      total,
      active,
      disabled: total - active,
      cooling,
      failed,
      healthy,
      chatgptOk,
      exitCountryCount: exitCountries.size,
    }
  }, [proxies])

  const regionOptions = useMemo(() => {
    const regions = Array.from(new Set(proxies.map((proxy) => String(proxy.region || '').trim()).filter(Boolean))).sort()
    return [
      { label: '全部地区', value: 'all' },
      { label: '未标记', value: '__empty__' },
      ...regions.map((item) => ({ label: item, value: item })),
    ]
  }, [proxies])

  const countryOptions = useMemo(() => {
    const countries = Array.from(new Set(proxies.map((proxy) => String(proxy.exit_country_code || '').trim().toUpperCase()).filter(Boolean))).sort()
    return [
      { label: '全部出口', value: 'all' },
      { label: '未知出口', value: '__empty__' },
      ...countries.map((item) => ({ label: item, value: item })),
    ]
  }, [proxies])

  const filteredProxies = useMemo(() => {
    const keyword = searchText.trim().toLowerCase()
    return proxies.filter((proxy) => {
      if (keyword) {
        const haystack = `${proxy.url || ''} ${proxy.region || ''} ${proxy.exit_ip || ''} ${proxy.exit_country_code || ''} ${proxy.exit_asn || ''} ${proxy.exit_isp || ''} ${proxy.homepage_last_error || ''} ${proxy.last_error || ''}`.toLowerCase()
        if (!haystack.includes(keyword)) return false
      }
      if (regionFilter === '__empty__' && String(proxy.region || '').trim()) return false
      if (regionFilter !== 'all' && regionFilter !== '__empty__' && String(proxy.region || '').trim() !== regionFilter) return false
      if (countryFilter === '__empty__' && String(proxy.exit_country_code || '').trim()) return false
      if (countryFilter !== 'all' && countryFilter !== '__empty__' && String(proxy.exit_country_code || '').trim().toUpperCase() !== countryFilter) return false
      if (statusFilter === 'active' && !proxy.is_active) return false
      if (statusFilter === 'disabled' && proxy.is_active) return false
      if (statusFilter === 'cooling' && !isCooling(proxy)) return false
      if (statusFilter === 'failed' && !hasRecentFailure(proxy)) return false
      if (statusFilter === 'healthy' && String(proxy.scan_status || '').toLowerCase() !== 'ok') return false
      if (statusFilter === 'degraded' && String(proxy.scan_status || '').toLowerCase() !== 'degraded') return false
      if (statusFilter === 'unchecked' && String(proxy.scan_status || 'unchecked').toLowerCase() !== 'unchecked') return false
      return true
    })
  }, [proxies, countryFilter, regionFilter, searchText, statusFilter])

  const load = async () => {
    setLoading(true)
    setLoadError('')
    try {
      const data = await apiFetch('/proxies') as ProxyRecord[]
      const items = Array.isArray(data) ? data : []
      setProxies(items)
      const validIds = new Set(items.map((proxy) => proxy.id))
      setSelectedProxyIds((current) => current.filter((id) => validIds.has(id)))
      const cfg = await apiFetch('/config') as ConfigPayload
      setCooldownEnabled(
        String(cfg?.proxy_pool_cooldown_enabled ?? 'true').trim().toLowerCase() !== 'false',
      )
      setAutoScanEnabled(String(cfg?.proxy_scan_enabled ?? 'false').trim().toLowerCase() === 'true')
      setScanOnlyActive(String(cfg?.proxy_scan_only_active ?? 'true').trim().toLowerCase() !== 'false')
      setScanIntervalMinutes(Math.max(1, Number(cfg?.proxy_scan_interval_minutes || 30) || 30))
      setScanConcurrency(Math.max(1, Number(cfg?.proxy_scan_concurrency || 8) || 8))
      setScanTimeoutSeconds(Math.max(2, Number(cfg?.proxy_scan_timeout_seconds || 8) || 8))
      setScanMinScore(Math.max(0, Number(cfg?.proxy_scan_min_score || 50) || 50))
      setPoolMaxCandidates(Math.max(1, Number(cfg?.proxy_pool_max_candidates || 5) || 5))
      setDynamicProxyTemplate(String(cfg?.dynamic_proxy_template || ''))
      setDynamicProxyCountry(String(cfg?.dynamic_proxy_default_country || 'JP').trim().toUpperCase() || 'JP')
      setDynamicProxyProbe(String(cfg?.dynamic_proxy_probe_enabled ?? 'true').trim().toLowerCase() !== 'false')
      try {
        const scheduler = await apiFetch('/proxies/scan-scheduler/status') as Record<string, unknown>
        setSchedulerStatus(scheduler)
      } catch {
        setSchedulerStatus(null)
      }
    } catch (error) {
      const text = error instanceof Error ? error.message : '代理数据加载失败'
      setLoadError(text)
      message.error(text)
    } finally {
      setLoading(false)
    }
  }

  const patchProxyRows = (records: ProxyRecord[], missing: number[] = []) => {
    const missingIds = new Set(missing.map((id) => Number(id)).filter((id) => Number.isFinite(id)))
    const incoming = new Map<number, ProxyRecord>()
    records
      .filter((record) => Number.isFinite(Number(record?.id)))
      .forEach((record) => incoming.set(Number(record.id), record))

    setProxies((current) => {
      const seen = new Set<number>()
      const next: ProxyRecord[] = []
      current.forEach((item) => {
        const id = Number(item.id)
        if (missingIds.has(id)) return
        const patched = incoming.get(id)
        if (patched) {
          seen.add(id)
          next.push(patched)
          return
        }
        next.push(item)
      })
      incoming.forEach((record, id) => {
        if (!seen.has(id)) next.push(record)
      })
      return next
    })

    if (missingIds.size > 0) {
      setSelectedProxyIds((current) => current.filter((id) => !missingIds.has(Number(id))))
    }
  }

  const refreshProxyRows = async (ids: number[]) => {
    const rowIds = Array.from(new Set(ids.map((id) => Number(id)).filter((id) => Number.isFinite(id) && id > 0)))
    if (rowIds.length === 0) return
    const data = await apiFetch('/proxies/snapshot', {
      method: 'POST',
      body: JSON.stringify({ ids: rowIds }),
    }) as { items?: ProxyRecord[]; missing?: number[] }
    patchProxyRows(Array.isArray(data?.items) ? data.items : [], Array.isArray(data?.missing) ? data.missing : [])
  }

  const loadProxyDiagnostics = async (id: number) => {
    if (!id) return
    setDiagnosticsLoading(true)
    try {
      const data = await apiFetch(`/proxies/${id}/diagnostics`) as ProxyDiagnostics
      setProxyDiagnostics(data && typeof data === 'object' ? data : null)
      if (data?.item) patchProxyRows([data.item])
      if (data?.scheduler) setSchedulerStatus(data.scheduler)
    } catch (error) {
      message.error(error instanceof Error ? error.message : '读取代理诊断失败')
    } finally {
      setDiagnosticsLoading(false)
    }
  }

  const openProxyDiagnostics = (record: ProxyRecord) => {
    const id = Number(record.id)
    setDiagnosticsProxyId(id)
    setDiagnosticsOpen(true)
    setProxyDiagnostics(null)
    void loadProxyDiagnostics(id)
  }

  const refreshOpenProxyDiagnostics = async (id: number) => {
    if (diagnosticsOpen && diagnosticsProxyId === id) {
      await loadProxyDiagnostics(id)
    }
  }

  useEffect(() => {
    void load()
    return () => {
      if (scanPollTimerRef.current) {
        window.clearTimeout(scanPollTimerRef.current)
        scanPollTimerRef.current = null
      }
    }
  }, [])

  const add = async () => {
    const lines = parsedInput.lines.filter((line) => !existingProxyUrls.has(line))
    if (!lines.length) {
      message.warning(parsedInput.lines.length ? '输入的代理都已存在' : '请先填写代理地址')
      return
    }
    try {
      if (lines.length > 1) {
        await apiFetch('/proxies/bulk', {
          method: 'POST',
          body: JSON.stringify({ proxies: lines, region }),
        })
      } else {
        await apiFetch('/proxies', {
          method: 'POST',
          body: JSON.stringify({ url: lines[0], region }),
        })
      }
      message.success(`添加成功：${lines.length} 条`)
      setNewProxy('')
      setRegion('')
      await load()
    } catch (error) {
      const text = error instanceof Error ? error.message : '添加失败'
      message.error(`添加失败: ${text}`)
    }
  }

  const del = async (id: number) => {
    await apiFetch(`/proxies/${id}`, { method: 'DELETE' })
    message.success('删除成功')
    patchProxyRows([], [id])
    if (diagnosticsProxyId === id) {
      setDiagnosticsOpen(false)
      setDiagnosticsProxyId(null)
      setProxyDiagnostics(null)
    }
  }

  const bulkDelete = async () => {
    const ids = Array.from(new Set(selectedProxyIds.map((id) => Number(id)).filter((id) => Number.isFinite(id) && id > 0)))
    if (!ids.length) {
      message.warning('请先勾选要删除的代理')
      return
    }
    setBulkDeleting(true)
    try {
      const result = await apiFetch('/proxies/bulk-delete', {
        method: 'POST',
        body: JSON.stringify({ ids }),
      }) as { deleted?: number; missing?: number }
      const deleted = Number(result?.deleted || 0)
      const missing = Number(result?.missing || 0)
      message.success(missing ? `已删除 ${deleted} 条，未找到 ${missing} 条` : `已删除 ${deleted} 条`)
      setSelectedProxyIds([])
      await load()
    } catch (error) {
      message.error(error instanceof Error ? error.message : '批量删除失败')
    } finally {
      setBulkDeleting(false)
    }
  }

  const toggle = async (id: number) => {
    const result = await apiFetch(`/proxies/${id}/toggle`, { method: 'PATCH' }) as ProxyRecord
    if (result?.id) {
      patchProxyRows([result])
    } else {
      setProxies((current) => current.map((proxy) => proxy.id === id ? { ...proxy, is_active: !proxy.is_active } : proxy))
    }
    await refreshOpenProxyDiagnostics(id)
  }

  const pollScanJob = async (jobId: string) => {
    if (!jobId) return
    try {
      const job = await apiFetch(`/proxies/scan/${encodeURIComponent(jobId)}`) as ProxyScanJob
      setScanJob(job)
      if (job.status === 'pending' || job.status === 'running') {
        scanPollTimerRef.current = window.setTimeout(() => {
          void pollScanJob(jobId)
        }, 1800)
        return
      }
      setChecking(false)
      const recentIds = Array.isArray(job.recent_results)
        ? job.recent_results.map((item) => Number(item?.proxy_id)).filter((id) => Number.isFinite(id) && id > 0)
        : []
      if (recentIds.length) {
        await refreshProxyRows(recentIds)
      } else {
        await load()
      }
      if (diagnosticsProxyId && recentIds.includes(diagnosticsProxyId)) {
        await loadProxyDiagnostics(diagnosticsProxyId)
      }
      if (job.status === 'done') {
        message.success(`扫描完成：成功 ${Number(job.ok || 0)}，降级 ${Number(job.degraded || 0)}，失败 ${Number(job.failed || 0)}`)
      } else if (job.status === 'cancelled') {
        message.warning('扫描已取消')
      } else if (job.error) {
        message.error(String(job.error))
      }
    } catch (error) {
      setChecking(false)
      message.error(error instanceof Error ? error.message : '扫描进度获取失败')
    }
  }

  const startScan = async (targets: string[], label: string, ids: number[] = []) => {
    setChecking(true)
    try {
      const job = await apiFetch('/proxies/scan', {
        method: 'POST',
        body: JSON.stringify({
          ids,
          scope: ids.length ? 'selected' : 'all',
          targets,
          concurrency: scanConcurrency,
          timeout_seconds: scanTimeoutSeconds,
          refresh_geo: true,
          only_active: false,
        }),
      }) as ProxyScanJob
      setScanJob(job)
      message.success(`${label}已启动`)
      void pollScanJob(job.job_id)
    } catch (error) {
      const text = error instanceof Error ? error.message : '检测任务启动失败'
      message.error(text)
      setChecking(false)
    }
  }

  const check = async () => startScan(['basic', 'geo', 'chatgpt'], '完整扫描')

  const scanOne = async (id: number) => {
    await startScan(['basic', 'geo', 'chatgpt'], '单个代理扫描', [id])
  }

  const clearCooldowns = async () => {
    setClearingCooldowns(true)
    try {
      const result = await apiFetch('/proxies/clear-cooldowns', { method: 'POST' }) as { cleared?: number }
      message.success(`已清空冷却：${Number(result?.cleared || 0)} 条`)
      await load()
    } catch (error) {
      message.error(error instanceof Error ? error.message : '清空冷却失败')
    } finally {
      setClearingCooldowns(false)
    }
  }

  const clearOneCooldown = async (id: number) => {
    const result = await apiFetch(`/proxies/${id}/clear-cooldown`, { method: 'POST' }) as ProxyRecord
    if (result?.id) patchProxyRows([result])
    message.success('已清空该代理冷却')
    await refreshOpenProxyDiagnostics(id)
  }

  const toggleCooldownSetting = async (checked: boolean) => {
    setSavingCooldownSetting(true)
    try {
      await apiFetch('/config', {
        method: 'PUT',
        body: JSON.stringify({
          data: {
            proxy_pool_cooldown_enabled: checked,
          },
        }),
      })
      setCooldownEnabled(checked)
      message.success(checked ? '已启用代理冷却' : '已关闭代理冷却')
    } catch (error) {
      message.error(error instanceof Error ? error.message : '保存代理冷却设置失败')
    } finally {
      setSavingCooldownSetting(false)
    }
  }

  const saveScanSettings = async (patch: Record<string, unknown> = {}) => {
    setSavingScanSetting(true)
    try {
      const data = {
        proxy_scan_enabled: autoScanEnabled,
        proxy_scan_interval_minutes: String(scanIntervalMinutes || 30),
        proxy_scan_concurrency: String(scanConcurrency || 8),
        proxy_scan_timeout_seconds: String(scanTimeoutSeconds || 8),
        proxy_scan_targets: 'basic,geo,chatgpt',
        proxy_scan_only_active: scanOnlyActive,
        proxy_scan_min_score: String(scanMinScore || 0),
        proxy_pool_max_candidates: String(poolMaxCandidates || 5),
        ...patch,
      }
      await apiFetch('/config', {
        method: 'PUT',
        body: JSON.stringify({ data }),
      })
      if (Object.prototype.hasOwnProperty.call(patch, 'proxy_scan_enabled')) {
        setAutoScanEnabled(Boolean(patch.proxy_scan_enabled))
      }
      message.success('代理扫描设置已保存')
      await load()
    } catch (error) {
      message.error(error instanceof Error ? error.message : '保存代理扫描设置失败')
    } finally {
      setSavingScanSetting(false)
    }
  }

  const saveDynamicProxySettings = async () => {
    const template = dynamicProxyTemplate.trim()
    const country = dynamicProxyCountry.trim().toUpperCase()
    if (!template) {
      message.warning('请填写动态代理模板')
      return
    }
    if (!country) {
      message.warning('请填写出口国家')
      return
    }
    setDynamicSaving(true)
    try {
      await saveTaskProxySettingsToConfig({
        proxy_mode: 'dynamic',
        proxy: template,
        proxy_country_code: country,
        proxy_failover: false,
        proxy_max_candidates: poolMaxCandidates,
        proxy_min_score: scanMinScore,
      })
      message.success('动态代理已保存为所有任务默认代理')
      await load()
    } catch (error) {
      message.error(error instanceof Error ? error.message : '保存动态代理设置失败')
    } finally {
      setDynamicSaving(false)
    }
  }

  const previewDynamicProxy = async () => {
    const template = dynamicProxyTemplate.trim()
    const country = dynamicProxyCountry.trim().toUpperCase()
    if (!template) {
      message.warning('请填写动态代理模板')
      return
    }
    if (!country) {
      message.warning('请填写出口国家')
      return
    }
    setDynamicPreviewLoading(true)
    try {
      const result = await apiFetch('/proxies/dynamic-preview', {
        method: 'POST',
        body: JSON.stringify({
          proxy: template,
          country_code: country,
          refresh_sid: true,
          probe: dynamicProxyProbe,
          require_country_match: true,
          timeout_seconds: scanTimeoutSeconds,
        }),
      }) as Record<string, any>
      setDynamicPreviewResult(result)
      if (result.ok) {
        message.success(result.match ? `动态代理出口匹配 ${country}` : '动态代理模板解析成功')
      } else {
        message.warning(String(result.message || '动态代理预览未通过'))
      }
    } catch (error) {
      const text = error instanceof Error ? error.message : '动态代理预览失败'
      setDynamicPreviewResult({ ok: false, message: text })
      message.error(text)
    } finally {
      setDynamicPreviewLoading(false)
    }
  }

  const runAutoScanNow = async () => {
    setChecking(true)
    try {
      const result = await apiFetch('/proxies/scan-scheduler/run', { method: 'POST' }) as { started?: boolean; job_id?: string; job?: ProxyScanJob; reason?: string }
      const job = result.job || (result.job_id ? { job_id: result.job_id } as ProxyScanJob : null)
      if (result.started && job?.job_id) {
        setScanJob(job)
        message.success('自动扫描已手动触发')
        void pollScanJob(job.job_id)
      } else {
        setChecking(false)
        message.warning(result.reason || '自动扫描未启动')
      }
    } catch (error) {
      setChecking(false)
      message.error(error instanceof Error ? error.message : '触发自动扫描失败')
    }
  }

  const diagnosticItem = proxyDiagnostics?.item
  const diagnosticNotes = Array.isArray(proxyDiagnostics?.notes) ? proxyDiagnostics.notes : []
  const diagnosticEndpoint = proxyDiagnostics?.endpoint || {}
  const diagnosticProbe = proxyDiagnostics?.last_probe || {}

  const renderProxyMobileCards = () => {
    if (!filteredProxies.length) {
      return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无代理，先在上方添加。" />
    }
    return (
      <div className="mobile-card-list">
        {filteredProxies.map((record) => {
          const health = getProxyHealth(record)
          const scan = scanStatusLabel(record.scan_status)
          const chatgpt = chatgptStatusLabel(record.chatgpt_status)
          const id = Number(record.id)
          const checked = selectedProxyIds.includes(id)
          const error = String(record.last_error || record.homepage_last_error || record.chatgpt_last_error || '').trim()
          return (
            <Card key={id} size="small" className="mobile-record-card">
              <div className="mobile-record-head">
                <Checkbox
                  checked={checked}
                  onChange={(event) => {
                    const nextChecked = event.target.checked
                    setSelectedProxyIds((current) => nextChecked
                      ? Array.from(new Set([...current, id]))
                      : current.filter((item) => item !== id))
                  }}
                />
                <div className="mobile-record-main">
                  <Typography.Text code copyable={{ text: record.url }} className="mobile-record-title">
                    {maskProxyUrl(record.url)}
                  </Typography.Text>
                  <div className="mobile-record-meta">
                    <Tag color={health.color} icon={health.icon}>{health.label}</Tag>
                    <Tag color={scan.color}>扫描 {scan.label}</Tag>
                    <Tag color={chatgpt.color}>ChatGPT {chatgpt.label}</Tag>
                    {record.region ? <Tag>{record.region}</Tag> : null}
                    {record.desired_country_code ? <Tag color="blue">期望 {record.desired_country_code}</Tag> : null}
                  </div>
                </div>
              </div>
              <div className="mobile-record-section">
                <div className="mobile-record-field">
                  <span className="mobile-record-label">出口</span>
                  <span className="mobile-record-value">
                    {[record.exit_country_code, record.exit_city || record.exit_region_name].filter(Boolean).join(' · ') || '未扫描'}
                    {record.exit_ip ? ` · ${record.exit_ip}` : ''}
                  </span>
                </div>
                <div className="mobile-record-field">
                  <span className="mobile-record-label">评分 / 延迟</span>
                  <span className="mobile-record-value">
                    {Number(record.health_score || 0) ? `${record.health_score} 分` : '未评分'}
                    {Number(record.last_latency_ms || 0) ? ` · ${record.last_latency_ms}ms` : ''}
                  </span>
                </div>
                <div className="mobile-record-field">
                  <span className="mobile-record-label">成功 / 失败</span>
                  <span className="mobile-record-value">
                    {Number(record.success_count || 0)} / {Number(record.fail_count || 0)}
                    {Number(record.homepage_fail_count || 0) ? ` · ChatGPT失败 ${record.homepage_fail_count}` : ''}
                  </span>
                </div>
                <div className="mobile-record-field">
                  <span className="mobile-record-label">最近扫描</span>
                  <span className="mobile-record-value">{formatDateTime(record.last_scan_at)}</span>
                </div>
              </div>
              {error ? (
                <Typography.Text type="danger" style={{ display: 'block', marginTop: 8, fontSize: 12 }} ellipsis={{ tooltip: error }}>
                  {error}
                </Typography.Text>
              ) : null}
              <div className="mobile-record-actions">
                <Button size="small" icon={<BugOutlined />} onClick={() => openProxyDiagnostics(record)}>诊断</Button>
                <Button size="small" icon={<ThunderboltOutlined />} onClick={() => void scanOne(record.id)}>扫描</Button>
                <Button size="small" icon={record.is_active ? <SwapLeftOutlined /> : <SwapRightOutlined />} onClick={() => void toggle(record.id)}>
                  {record.is_active ? '禁用' : '启用'}
                </Button>
                <Popconfirm title="确认删除？" onConfirm={() => void del(record.id)}>
                  <Button size="small" danger icon={<DeleteOutlined />}>删除</Button>
                </Popconfirm>
              </div>
            </Card>
          )
        })}
      </div>
    )
  }

  const columns = [
    {
      title: '代理地址',
      dataIndex: 'url',
      key: 'url',
      width: 360,
      render: (text: string) => (
        <Typography.Text code copyable={{ text }} style={{ fontSize: 12 }} ellipsis>
          {maskProxyUrl(text)}
        </Typography.Text>
      ),
    },
    {
      title: '标签 / 期望',
      dataIndex: 'region',
      key: 'region',
      width: 150,
      render: (text: string, record: ProxyRecord) => (
        <Space size={4} wrap>
          {text ? <Tag>{text}</Tag> : <Typography.Text type="secondary">未标记</Typography.Text>}
          {record.desired_country_code ? <Tag color="blue">期望 {record.desired_country_code}</Tag> : null}
        </Space>
      ),
    },
    {
      title: '状态',
      key: 'health',
      width: 120,
      render: (_: unknown, record: ProxyRecord) => {
        const health = getProxyHealth(record)
        return <Tag color={health.color} icon={health.icon}>{health.label}</Tag>
      },
    },
    {
      title: '实测出口',
      key: 'exit',
      width: 230,
      render: (_: unknown, record: ProxyRecord) => {
        const country = String(record.exit_country_code || '').trim().toUpperCase()
        const city = String(record.exit_city || record.exit_region_name || '').trim()
        const mismatch = Boolean(record.desired_country_code && country && record.desired_country_code !== country)
        if (!record.exit_ip && !country) return <Typography.Text type="secondary">未扫描</Typography.Text>
        return (
          <Space direction="vertical" size={2}>
            <Space size={4} wrap>
              {country ? <Tag color={mismatch ? 'warning' : 'blue'}>{country}{city ? ` · ${city}` : ''}</Tag> : <Tag>未知</Tag>}
              {mismatch ? <Tag color="warning">地区不一致</Tag> : null}
            </Space>
            {record.exit_ip ? <Typography.Text code copyable={{ text: record.exit_ip }} style={{ fontSize: 12 }}>{record.exit_ip}</Typography.Text> : null}
            {record.exit_asn || record.exit_isp ? (
              <Typography.Text type="secondary" ellipsis style={{ maxWidth: 200, fontSize: 12 }}>
                {[record.exit_asn, record.exit_isp].filter(Boolean).join(' · ')}
              </Typography.Text>
            ) : null}
          </Space>
        )
      },
    },
    {
      title: '扫描',
      key: 'scan',
      width: 170,
      render: (_: unknown, record: ProxyRecord) => {
        const scan = scanStatusLabel(record.scan_status)
        const score = Number(record.health_score || 0)
        return (
          <Space direction="vertical" size={4}>
            <Space size={4}>
              <Tag color={scan.color}>{scan.label}</Tag>
              <Tag color={score >= 70 ? 'success' : score >= 40 ? 'warning' : 'default'}>{score ? `${score}分` : '未评分'}</Tag>
            </Space>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>{Number(record.last_latency_ms || 0) ? `${record.last_latency_ms}ms` : '无延迟数据'}</Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>{formatDateTime(record.last_scan_at)}</Typography.Text>
          </Space>
        )
      },
    },
    {
      title: 'ChatGPT',
      key: 'chatgpt',
      width: 150,
      render: (_: unknown, record: ProxyRecord) => {
        const status = chatgptStatusLabel(record.chatgpt_status)
        return (
          <Space direction="vertical" size={4}>
            <Tag color={status.color}>{status.label}</Tag>
            {Number(record.chatgpt_latency_ms || 0) ? <Typography.Text type="secondary" style={{ fontSize: 12 }}>{record.chatgpt_latency_ms}ms</Typography.Text> : null}
            {record.chatgpt_last_error ? (
              <Tooltip title={String(record.chatgpt_last_error)}>
                <Typography.Text type="secondary" ellipsis style={{ maxWidth: 130, fontSize: 12 }}>
                  {String(record.chatgpt_last_error)}
                </Typography.Text>
              </Tooltip>
            ) : null}
          </Space>
        )
      },
    },
    {
      title: '成功 / 失败',
      key: 'stats',
      width: 140,
      render: (_: unknown, record: ProxyRecord) => (
        <Space size={4}>
          <Tag color="success">{Number(record.success_count || 0)}</Tag>
          <Typography.Text type="secondary">/</Typography.Text>
          <Tag color="error">{Number(record.fail_count || 0)}</Tag>
        </Space>
      ),
    },
    {
      title: '最近错误',
      key: 'last_error',
      width: 260,
      render: (_: unknown, record: ProxyRecord) => {
        const error = String(record.last_error || record.homepage_last_error || record.chatgpt_last_error || '').trim()
        const code = String(record.last_error_code || '').trim()
        if (!error && !code && !record.homepage_circuit_open_until && !record.cooldown_until) {
          return <Typography.Text type="secondary">无</Typography.Text>
        }
        return (
          <Space direction="vertical" size={4} style={{ maxWidth: 250 }}>
            {code ? <Tag color="error">{code}</Tag> : null}
            {record.cooldown_until || record.homepage_circuit_open_until ? (
              <Typography.Text type="warning" style={{ fontSize: 12 }}>
                冷却至: {String(record.cooldown_until || record.homepage_circuit_open_until)}
              </Typography.Text>
            ) : null}
            {error ? (
              <Tooltip title={error}>
                <Typography.Text type="secondary" ellipsis style={{ maxWidth: 240, fontSize: 12 }}>
                  {error}
                </Typography.Text>
              </Tooltip>
            ) : null}
          </Space>
        )
      },
    },
    {
      title: '操作',
      key: 'action',
      width: 180,
      fixed: 'right' as const,
      render: (_: unknown, record: ProxyRecord) => (
        <Space>
          <Tooltip title="状态诊断">
            <Button type="text" size="small" icon={<BugOutlined />} onClick={() => openProxyDiagnostics(record)} />
          </Tooltip>
          <Tooltip title={record.is_active ? '禁用代理' : '启用代理'}>
            <Button
              type="text"
              size="small"
              icon={record.is_active ? <SwapLeftOutlined /> : <SwapRightOutlined />}
              onClick={() => void toggle(record.id)}
            />
          </Tooltip>
          <Tooltip title="扫描此代理">
            <Button type="text" size="small" icon={<ThunderboltOutlined />} onClick={() => void scanOne(record.id)} />
          </Tooltip>
          <Popconfirm title="确认删除？" onConfirm={() => void del(record.id)}>
            <Button type="text" size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
        <div>
          <h1 style={{ fontSize: 24, fontWeight: 'bold', margin: 0 }}>代理管理</h1>
          <p style={{ color: '#7a8ba3', marginTop: 4 }}>维护注册、支付和状态同步会用到的代理池</p>
        </div>
        <Space wrap>
          <Space>
            <Typography.Text type="secondary">代理冷却</Typography.Text>
            <Switch checked={cooldownEnabled} loading={savingCooldownSetting} onChange={toggleCooldownSetting} />
          </Space>
          <Button onClick={clearCooldowns} loading={clearingCooldowns} disabled={!summary.cooling && !summary.failed}>
            清空冷却
          </Button>
          <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
            刷新
          </Button>
          <Button icon={<ThunderboltOutlined spin={checking} />} onClick={() => void startScan(['basic', 'geo'], '快速扫描')} loading={checking}>
            快速扫描
          </Button>
          <Button type="primary" icon={<ThunderboltOutlined spin={checking} />} onClick={check} loading={checking}>
            完整扫描
          </Button>
        </Space>
      </div>

      {loadError ? (
        <Alert
          type="error"
          showIcon
          closable
          message="代理数据加载失败"
          description={loadError}
          action={<Button size="small" onClick={() => void load()}>重试</Button>}
          onClose={() => setLoadError('')}
        />
      ) : null}

      <Row gutter={[12, 12]}>
        <Col xs={12} md={8} xl={4}>{statBox('代理总数', summary.total, 'primary')}</Col>
        <Col xs={12} md={8} xl={4}>{statBox('活跃', summary.active, 'default')}</Col>
        <Col xs={12} md={8} xl={4}>{statBox('健康可用', summary.healthy, summary.healthy ? 'primary' : 'default')}</Col>
        <Col xs={12} md={8} xl={4}>{statBox('ChatGPT 可用', summary.chatgptOk, summary.chatgptOk ? 'primary' : 'default')}</Col>
        <Col xs={12} md={8} xl={4}>{statBox('冷却中', summary.cooling, summary.cooling ? 'warning' : 'default')}</Col>
        <Col xs={12} md={8} xl={4}>{statBox('出口国家', summary.exitCountryCount, summary.exitCountryCount ? 'primary' : 'default')}</Col>
      </Row>

      {scanJob ? (
        <Card>
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
              <Space wrap>
                <Typography.Text strong>扫描任务</Typography.Text>
                <Tag color={scanJob.status === 'done' ? 'success' : scanJob.status === 'failed' ? 'error' : 'processing'}>
                  {scanJob.status}
                </Tag>
                <Typography.Text type="secondary">{scanJob.job_id}</Typography.Text>
              </Space>
              <Typography.Text type="secondary">
                成功 {Number(scanJob.ok || 0)}，降级 {Number(scanJob.degraded || 0)}，失败 {Number(scanJob.failed || 0)}
              </Typography.Text>
            </Space>
            <Progress
              percent={scanJob.total ? Math.round((Number(scanJob.done || 0) / Number(scanJob.total || 1)) * 100) : 100}
              status={scanJob.status === 'failed' ? 'exception' : scanJob.status === 'done' ? 'success' : 'active'}
            />
          </Space>
        </Card>
      ) : null}

      <Card title="自动扫描与选择策略">
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Space wrap>
            <Space>
              <Typography.Text type="secondary">自动扫描</Typography.Text>
              <Switch
                checked={autoScanEnabled}
                loading={savingScanSetting}
                onChange={(checked) => void saveScanSettings({ proxy_scan_enabled: checked })}
              />
            </Space>
            <Space>
              <Typography.Text type="secondary">只扫活跃</Typography.Text>
              <Switch checked={scanOnlyActive} onChange={setScanOnlyActive} />
            </Space>
            <Button onClick={() => void saveScanSettings()} loading={savingScanSetting}>保存扫描设置</Button>
            <Button onClick={() => void runAutoScanNow()} loading={checking}>立即按自动配置扫描</Button>
            {schedulerStatus?.next_run_at ? <Tag>下次: {String(schedulerStatus.next_run_at)}</Tag> : null}
          </Space>
          <Space wrap>
            <InputNumber min={1} max={1440} value={scanIntervalMinutes} onChange={(value) => setScanIntervalMinutes(Number(value || 30))} addonBefore="间隔分钟" />
            <InputNumber min={1} max={32} value={scanConcurrency} onChange={(value) => setScanConcurrency(Number(value || 8))} addonBefore="并发" />
            <InputNumber min={2} max={60} value={scanTimeoutSeconds} onChange={(value) => setScanTimeoutSeconds(Number(value || 8))} addonBefore="超时秒" />
            <InputNumber min={0} max={100} value={scanMinScore} onChange={(value) => setScanMinScore(Number(value || 0))} addonBefore="最低健康分" />
            <InputNumber min={1} max={100} value={poolMaxCandidates} onChange={(value) => setPoolMaxCandidates(Number(value || 5))} addonBefore="候选数" />
          </Space>
          <Typography.Text type="secondary">
            自动扫描目标固定为基础连通、出口国家和 ChatGPT 首页；代理池及指定代理失败回退时按这里的最低健康分和候选数挑选。动态代理不使用代理池健康分/候选数，只按模板和出口国家生成运行代理。
          </Typography.Text>
        </Space>
      </Card>

      <Card title="动态代理预览">
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Alert
            type="info"
            showIcon
            message="动态代理不是本地代理池记录"
            description="输入包含 region-XX / sid-xxx-t- 的模板，系统会按出口国家改写 region 并刷新 sid；预览和日志只展示脱敏地址。"
          />
          <Space wrap align="start" style={{ width: '100%' }}>
            <Input.Password
              value={dynamicProxyTemplate}
              onChange={(event) => setDynamicProxyTemplate(event.target.value)}
              placeholder="socks5://user-region-JP-sid-xxxx-t-1:pass@host:port"
              style={{ width: isMobile ? '100%' : 520 }}
            />
            <Input
              value={dynamicProxyCountry}
              onChange={(event) => setDynamicProxyCountry(event.target.value.trim().toUpperCase())}
              placeholder="US / JP / SG"
              maxLength={2}
              style={{ width: 120 }}
            />
            <Checkbox checked={dynamicProxyProbe} onChange={(event) => setDynamicProxyProbe(event.target.checked)}>实测出口</Checkbox>
            <Button type="primary" icon={<ThunderboltOutlined />} loading={dynamicPreviewLoading} onClick={() => void previewDynamicProxy()}>
              预览动态出口
            </Button>
            <Button loading={dynamicSaving} onClick={() => void saveDynamicProxySettings()}>
              保存为任务默认
            </Button>
          </Space>
          {dynamicPreviewResult ? (
            <Descriptions size="small" bordered column={isMobile ? 1 : 3}>
              <Descriptions.Item label="结果">
                <Tag color={dynamicPreviewResult.ok ? 'success' : 'error'}>{dynamicPreviewResult.ok ? '通过' : '未通过'}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="期望国家">{dynamicPreviewResult.expected_country || dynamicProxyCountry || '-'}</Descriptions.Item>
              <Descriptions.Item label="实测国家">{dynamicPreviewResult.actual_country || (dynamicPreviewResult.probe_enabled ? '-' : '未探测')}</Descriptions.Item>
              <Descriptions.Item label="出口 IP">{dynamicPreviewResult.exit_ip || '-'}</Descriptions.Item>
              <Descriptions.Item label="Provider">{dynamicPreviewResult.provider || '-'}</Descriptions.Item>
              <Descriptions.Item label="sid">{dynamicPreviewResult.sid_refreshed ? '已刷新' : '未刷新/无 sid'}</Descriptions.Item>
              <Descriptions.Item label="运行代理" span={isMobile ? 1 : 3}>
                <Typography.Text code copyable={Boolean(dynamicPreviewResult.runtime_proxy_redacted)}>
                  {dynamicPreviewResult.runtime_proxy_redacted || dynamicPreviewResult.proxy || '-'}
                </Typography.Text>
              </Descriptions.Item>
              {dynamicPreviewResult.message ? (
                <Descriptions.Item label="说明" span={isMobile ? 1 : 3}>{String(dynamicPreviewResult.message)}</Descriptions.Item>
              ) : null}
            </Descriptions>
          ) : null}
        </Space>
      </Card>

      <Card
        title="添加代理"
        extra={(
          <Space size={6} wrap>
            <Tag color="blue">待添加 {parsedInput.lines.length}</Tag>
            {parsedInput.duplicates.length ? <Tag color="warning">输入重复 {parsedInput.duplicates.length}</Tag> : null}
            {existingInputCount ? <Tag color="default">已存在 {existingInputCount}</Tag> : null}
          </Space>
        )}
      >
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <Input.TextArea
            value={newProxy}
            onChange={(event) => setNewProxy(event.target.value)}
            placeholder={'每行一个代理，例如：\nhttp://user:pass@host:port\nsocks5://host:port'}
            rows={4}
            style={{ fontFamily: 'monospace' }}
          />
          <Space wrap>
            <Input
              value={region}
              onChange={(event) => setRegion(event.target.value)}
              placeholder="地区标签，如 US / SG / JP"
              style={{ width: 220 }}
            />
            <Button type="primary" icon={<PlusOutlined />} onClick={add} disabled={!parsedInput.lines.length}>
              添加可用输入
            </Button>
            <Typography.Text type="secondary">会自动跳过输入内重复和已存在代理。</Typography.Text>
          </Space>
        </Space>
      </Card>

      <Card>
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
            <Space wrap>
              <Input
                allowClear
                prefix={<SearchOutlined />}
                value={searchText}
                onChange={(event) => setSearchText(event.target.value)}
                placeholder="搜索地址、地区或最近错误"
                style={{ width: 280 }}
              />
              <Select
                value={statusFilter}
                onChange={setStatusFilter}
                style={{ width: 140 }}
                options={[
                  { label: '全部状态', value: 'all' },
                  { label: '活跃', value: 'active' },
                  { label: '禁用', value: 'disabled' },
                  { label: '冷却中', value: 'cooling' },
                  { label: '需关注', value: 'failed' },
                  { label: '健康可用', value: 'healthy' },
                  { label: '降级', value: 'degraded' },
                  { label: '未扫描', value: 'unchecked' },
                ]}
              />
              <Select value={regionFilter} onChange={setRegionFilter} style={{ width: 140 }} options={regionOptions} />
              <Select value={countryFilter} onChange={setCountryFilter} style={{ width: 140 }} options={countryOptions} />
            </Space>
            <Space wrap>
              <Typography.Text type="secondary">
                显示 {filteredProxies.length} / {proxies.length}
              </Typography.Text>
              {selectedProxyIds.length ? <Tag color="blue">已选 {selectedProxyIds.length}</Tag> : null}
              {selectedProxyIds.length ? (
                <Button size="small" onClick={() => setSelectedProxyIds([])}>
                  清空选择
                </Button>
              ) : null}
              {selectedProxyIds.length ? (
                <Popconfirm
                  title={`确认删除选中的 ${selectedProxyIds.length} 个代理？`}
                  description="只删除当前勾选的代理，不会删除筛选结果中的其他代理。"
                  okText="删除"
                  cancelText="取消"
                  okButtonProps={{ danger: true, loading: bulkDeleting }}
                  onConfirm={() => void bulkDelete()}
                >
                  <Button danger icon={<DeleteOutlined />} loading={bulkDeleting}>
                    批量删除
                  </Button>
                </Popconfirm>
              ) : (
                <Button danger icon={<DeleteOutlined />} disabled>
                  批量删除
                </Button>
              )}
            </Space>
          </Space>
          {isMobile ? (
            renderProxyMobileCards()
          ) : (
            <Table<ProxyRecord>
              rowKey="id"
              columns={columns}
              dataSource={filteredProxies}
              loading={loading}
              rowSelection={{
                selectedRowKeys: selectedProxyIds,
                onChange: (keys) => {
                  const ids = keys
                    .map((key) => Number(key))
                    .filter((id) => Number.isFinite(id))
                  setSelectedProxyIds(ids)
                },
                preserveSelectedRowKeys: true,
              }}
              scroll={{ x: 1650 }}
              pagination={{ pageSize: 20, showSizeChanger: true, showTotal: (total) => `共 ${total} 条` }}
              locale={{ emptyText: '暂无代理，先在上方添加。' }}
            />
          )}
        </Space>
      </Card>

      <Drawer
        title="代理状态诊断"
        width={720}
        open={diagnosticsOpen}
        onClose={() => {
          setDiagnosticsOpen(false)
          setDiagnosticsProxyId(null)
          setProxyDiagnostics(null)
        }}
        extra={(
          <Space>
            <Button
              size="small"
              icon={<ReloadOutlined />}
              loading={diagnosticsLoading}
              disabled={!diagnosticsProxyId}
              onClick={() => diagnosticsProxyId && void loadProxyDiagnostics(diagnosticsProxyId)}
            >
              刷新诊断
            </Button>
            <Button
              size="small"
              icon={<ThunderboltOutlined />}
              loading={checking}
              disabled={!diagnosticsProxyId}
              onClick={() => diagnosticsProxyId && void scanOne(diagnosticsProxyId)}
            >
              扫描此代理
            </Button>
          </Space>
        )}
      >
        {diagnosticItem ? (
          <Space direction="vertical" size={14} style={{ width: '100%' }}>
            <Space wrap>
              <Typography.Text code copyable={{ text: diagnosticItem.url }}>
                {proxyDiagnostics?.masked_url || maskProxyUrl(diagnosticItem.url)}
              </Typography.Text>
              <Tag color={diagnosticItem.is_active ? 'success' : 'default'}>
                {diagnosticItem.is_active ? '启用' : '禁用'}
              </Tag>
              <Tag color={Number(diagnosticItem.health_score || 0) >= 70 ? 'success' : Number(diagnosticItem.health_score || 0) >= 40 ? 'warning' : 'default'}>
                {Number(diagnosticItem.health_score || 0) ? `${diagnosticItem.health_score} 分` : '未评分'}
              </Tag>
              {diagnosticItem.region ? <Tag>{diagnosticItem.region}</Tag> : null}
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
              <Descriptions.Item label="端点">
                <Space wrap>
                  <Tag>{diagnosticEndpoint.scheme || diagnosticItem.scheme || '-'}</Tag>
                  <Typography.Text>{diagnosticEndpoint.host || diagnosticItem.host || '-'}</Typography.Text>
                  <Typography.Text type="secondary">:{diagnosticEndpoint.port || diagnosticItem.port || '-'}</Typography.Text>
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="实测出口">
                <Space wrap>
                  {diagnosticItem.exit_country_code ? <Tag color="blue">{diagnosticItem.exit_country_code}</Tag> : <Tag>未知</Tag>}
                  {diagnosticItem.exit_ip ? <Typography.Text code copyable={{ text: diagnosticItem.exit_ip }}>{diagnosticItem.exit_ip}</Typography.Text> : null}
                  {[diagnosticItem.exit_city, diagnosticItem.exit_asn, diagnosticItem.exit_isp].filter(Boolean).join(' · ') || null}
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="扫描状态">
                <Space wrap>
                  <Tag color={scanStatusLabel(diagnosticItem.scan_status).color}>{scanStatusLabel(diagnosticItem.scan_status).label}</Tag>
                  <Typography.Text type="secondary">{formatDateTime(diagnosticItem.last_scan_at)}</Typography.Text>
                  {Number(diagnosticItem.last_latency_ms || 0) ? <Tag>{diagnosticItem.last_latency_ms}ms</Tag> : null}
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="ChatGPT">
                <Space wrap>
                  <Tag color={chatgptStatusLabel(diagnosticItem.chatgpt_status).color}>{chatgptStatusLabel(diagnosticItem.chatgpt_status).label}</Tag>
                  {Number(diagnosticItem.chatgpt_status_code || 0) ? <Tag>HTTP {diagnosticItem.chatgpt_status_code}</Tag> : null}
                  {Number(diagnosticItem.chatgpt_latency_ms || 0) ? <Tag>{diagnosticItem.chatgpt_latency_ms}ms</Tag> : null}
                  <Typography.Text type="secondary">{formatDateTime(diagnosticItem.chatgpt_last_checked_at)}</Typography.Text>
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="成功 / 失败">
                <Space wrap>
                  <Tag color="success">基础 {Number(diagnosticItem.success_count || 0)}</Tag>
                  <Tag color="error">基础失败 {Number(diagnosticItem.fail_count || 0)}</Tag>
                  <Tag color="success">ChatGPT {Number(diagnosticItem.homepage_success_count || 0)}</Tag>
                  <Tag color="error">ChatGPT失败 {Number(diagnosticItem.homepage_fail_count || 0)}</Tag>
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="冷却">
                <Space direction="vertical" size={2}>
                  <Typography.Text type={diagnosticItem.cooldown_until ? 'warning' : 'secondary'}>
                    基础：{formatDateTime(diagnosticItem.cooldown_until)}
                  </Typography.Text>
                  <Typography.Text type={diagnosticItem.homepage_circuit_open_until ? 'warning' : 'secondary'}>
                    ChatGPT：{formatDateTime(diagnosticItem.homepage_circuit_open_until)}
                  </Typography.Text>
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="最近错误">
                {diagnosticItem.last_error || diagnosticItem.homepage_last_error || diagnosticItem.chatgpt_last_error || diagnosticItem.last_error_code ? (
                  <Space direction="vertical" size={2}>
                    {diagnosticItem.last_error_code ? <Tag color="error">{diagnosticItem.last_error_code}</Tag> : null}
                    {diagnosticItem.last_error ? <Typography.Text>{diagnosticItem.last_error}</Typography.Text> : null}
                    {diagnosticItem.homepage_last_error ? <Typography.Text>{diagnosticItem.homepage_last_error}</Typography.Text> : null}
                    {diagnosticItem.chatgpt_last_error ? <Typography.Text>{diagnosticItem.chatgpt_last_error}</Typography.Text> : null}
                  </Space>
                ) : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="调度器">
                {proxyDiagnostics?.scheduler ? (
                  <Space wrap>
                    <Tag color={Boolean(proxyDiagnostics.scheduler.enabled) ? 'success' : 'default'}>
                      {Boolean(proxyDiagnostics.scheduler.enabled) ? '自动扫描开' : '自动扫描关'}
                    </Tag>
                    {proxyDiagnostics.scheduler.next_run_at ? <Typography.Text type="secondary">下次 {String(proxyDiagnostics.scheduler.next_run_at)}</Typography.Text> : null}
                  </Space>
                ) : '-'}
              </Descriptions.Item>
            </Descriptions>

            <Space wrap>
              <Button
                icon={diagnosticItem.is_active ? <SwapLeftOutlined /> : <SwapRightOutlined />}
                onClick={() => void toggle(diagnosticItem.id)}
              >
                {diagnosticItem.is_active ? '禁用代理' : '启用代理'}
              </Button>
              <Button icon={<ReloadOutlined />} onClick={() => void clearOneCooldown(diagnosticItem.id)}>
                清空该代理冷却
              </Button>
              <Popconfirm title="确认删除该代理？" onConfirm={() => void del(diagnosticItem.id)}>
                <Button danger icon={<DeleteOutlined />}>删除代理</Button>
              </Popconfirm>
            </Space>

            <div>
              <Typography.Text strong>最近探测摘要</Typography.Text>
              {Object.keys(diagnosticProbe).length ? (
                <pre
                  style={{
                    marginTop: 8,
                    maxHeight: 300,
                    overflow: 'auto',
                    padding: 12,
                    borderRadius: 8,
                    background: 'rgba(122, 139, 163, 0.08)',
                    fontSize: 12,
                  }}
                >
                  {JSON.stringify(diagnosticProbe, null, 2)}
                </pre>
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无探测摘要，先扫描此代理" />
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
