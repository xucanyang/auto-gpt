import { apiFetch } from '@/lib/utils'

export type TaskProxyMode = 'direct' | 'pool' | 'specified' | 'dynamic'

export type TaskProxySettings = {
  proxy_mode: TaskProxyMode
  proxy: string
  proxy_country_code: string
  proxy_failover: boolean
  proxy_max_candidates: number
  proxy_min_score: number
  dynamic_proxy_ip_retention_minutes: number
}

const DEFAULT_TASK_PROXY_SETTINGS: TaskProxySettings = {
  proxy_mode: 'dynamic',
  proxy: '',
  proxy_country_code: 'JP',
  proxy_failover: false,
  proxy_max_candidates: 5,
  proxy_min_score: 50,
  dynamic_proxy_ip_retention_minutes: 5,
}

const VALID_PROXY_MODES = new Set<TaskProxyMode>(['direct', 'pool', 'specified', 'dynamic'])

function valueOf(record: Record<string, unknown>, ...keys: string[]) {
  for (const key of keys) {
    const value = record[key]
    if (value !== undefined && value !== null && value !== '') return value
  }
  return undefined
}

function stringWithDefault(value: unknown, fallback = '') {
  const text = String(value ?? '').trim()
  return text || fallback
}

function countryCode(value: unknown, fallback = '') {
  return stringWithDefault(value, fallback).trim().toUpperCase().slice(0, 2)
}

function booleanWithDefault(value: unknown, fallback: boolean) {
  if (value === undefined || value === null || value === '') return fallback
  if (typeof value === 'boolean') return value
  const text = String(value).trim().toLowerCase()
  if (['1', 'true', 'yes', 'on', 'y', '是', '开启', '启用'].includes(text)) return true
  if (['0', 'false', 'no', 'off', 'n', '否', '关闭', '禁用'].includes(text)) return false
  return fallback
}

function numberWithDefault(value: unknown, fallback: number, minimum: number, maximum: number) {
  if (value === undefined || value === null || value === '') return fallback
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return fallback
  return Math.max(minimum, Math.min(maximum, Math.floor(parsed)))
}

export function normalizeTaskProxyMode(value: unknown, fallback: TaskProxyMode = 'dynamic'): TaskProxyMode {
  const normalized = String(value || '').trim().toLowerCase() as TaskProxyMode
  return VALID_PROXY_MODES.has(normalized) ? normalized : fallback
}

export function normalizeTaskProxySettings(value: unknown, fallback?: Partial<TaskProxySettings>): TaskProxySettings {
  const base = { ...DEFAULT_TASK_PROXY_SETTINGS, ...(fallback || {}) }
  const record = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  const mode = normalizeTaskProxyMode(valueOf(record, 'proxy_mode', 'register_proxy_mode', 'probe_proxy_mode'), base.proxy_mode)
  return {
    proxy_mode: mode,
    proxy: stringWithDefault(valueOf(record, 'proxy', 'proxy_url', 'register_proxy', 'probe_proxy'), base.proxy),
    proxy_country_code: countryCode(valueOf(record, 'proxy_country_code', 'register_proxy_country_code', 'probe_proxy_country_code'), base.proxy_country_code),
    proxy_failover: booleanWithDefault(valueOf(record, 'proxy_failover', 'register_proxy_failover', 'probe_proxy_failover'), base.proxy_failover),
    proxy_max_candidates: numberWithDefault(valueOf(record, 'proxy_max_candidates', 'register_proxy_max_candidates', 'probe_proxy_max_candidates'), base.proxy_max_candidates, 1, 100),
    proxy_min_score: numberWithDefault(valueOf(record, 'proxy_min_score', 'register_proxy_min_score', 'probe_proxy_min_score'), base.proxy_min_score, 0, 100),
    dynamic_proxy_ip_retention_minutes: numberWithDefault(valueOf(record, 'dynamic_proxy_ip_retention_minutes'), base.dynamic_proxy_ip_retention_minutes, 1, 1440),
  }
}

export function taskProxySettingsFromConfig(config: unknown, fallback?: Partial<TaskProxySettings>): TaskProxySettings {
  const cfg = config && typeof config === 'object' ? config as Record<string, unknown> : {}
  const base = normalizeTaskProxySettings(fallback || {})
  const mode = normalizeTaskProxyMode(cfg.task_proxy_mode, base.proxy_mode)
  const taskProxyUrl = stringWithDefault(cfg.task_proxy_url, base.proxy)
  const globalDynamicTemplate = stringWithDefault(cfg.dynamic_proxy_template, '')
  const proxy = mode === 'dynamic'
    ? (globalDynamicTemplate ? '' : taskProxyUrl)
    : taskProxyUrl
  const country = countryCode(
    cfg.task_proxy_country_code,
    mode === 'dynamic'
      ? countryCode(cfg.dynamic_proxy_default_country, base.proxy_country_code || 'JP')
      : base.proxy_country_code,
  )
  return {
    proxy_mode: mode,
    proxy,
    proxy_country_code: country,
    proxy_failover: booleanWithDefault(cfg.task_proxy_failover, base.proxy_failover),
    proxy_max_candidates: numberWithDefault(cfg.task_proxy_max_candidates ?? cfg.proxy_pool_max_candidates, base.proxy_max_candidates, 1, 100),
    proxy_min_score: numberWithDefault(cfg.task_proxy_min_score ?? cfg.proxy_scan_min_score, base.proxy_min_score, 0, 100),
    dynamic_proxy_ip_retention_minutes: numberWithDefault(cfg.dynamic_proxy_ip_retention_minutes, base.dynamic_proxy_ip_retention_minutes, 1, 1440),
  }
}

export function taskProxyUsesPoolSelector(settings: Pick<TaskProxySettings, 'proxy_mode' | 'proxy_failover'>) {
  return settings.proxy_mode === 'pool' || (settings.proxy_mode === 'specified' && settings.proxy_failover)
}

export function taskProxyNeedsProxyUrl(settings: Pick<TaskProxySettings, 'proxy_mode'>) {
  return settings.proxy_mode === 'specified' || settings.proxy_mode === 'dynamic'
}

export function buildTaskProxyPayload(values: unknown): Record<string, unknown> {
  const settings = normalizeTaskProxySettings(values)
  const payload: Record<string, unknown> = {
    proxy: taskProxyNeedsProxyUrl(settings) ? (settings.proxy || null) : null,
    proxy_mode: settings.proxy_mode,
    proxy_country_code: settings.proxy_country_code,
    proxy_failover: settings.proxy_failover,
  }
  if (taskProxyUsesPoolSelector(settings)) {
    payload.proxy_max_candidates = settings.proxy_max_candidates
    payload.proxy_min_score = settings.proxy_min_score
  }
  if (settings.proxy_mode === 'dynamic') {
    payload.dynamic_proxy_ip_retention_minutes = settings.dynamic_proxy_ip_retention_minutes
  }
  return payload
}

export function validateTaskProxySettings(values: unknown) {
  const settings = normalizeTaskProxySettings(values)
  if (settings.proxy_mode === 'dynamic') {
    if (!settings.proxy_country_code) throw new Error('动态代理模式必须填写出口国家')
  }
  if (settings.proxy_mode === 'specified' && !settings.proxy) {
    throw new Error('手动指定代理模式必须填写代理地址')
  }
  return settings
}

export async function saveTaskProxySettingsToConfig(values: unknown) {
  const settings = normalizeTaskProxySettings(values)
  const data: Record<string, string> = {
    task_proxy_mode: settings.proxy_mode,
    task_proxy_url: settings.proxy,
    task_proxy_country_code: settings.proxy_country_code,
    task_proxy_failover: settings.proxy_failover ? 'true' : 'false',
    task_proxy_max_candidates: String(settings.proxy_max_candidates),
    task_proxy_min_score: String(settings.proxy_min_score),
  }

  if (settings.proxy_mode === 'dynamic' && settings.proxy) {
    data.dynamic_proxy_template = settings.proxy
  }
  if (settings.proxy_mode === 'dynamic') {
    data.dynamic_proxy_ip_retention_minutes = String(settings.dynamic_proxy_ip_retention_minutes)
  }
  if (settings.proxy_country_code) {
    data.dynamic_proxy_default_country = settings.proxy_country_code
  }
  if (taskProxyUsesPoolSelector(settings)) {
    data.proxy_pool_max_candidates = String(settings.proxy_max_candidates)
    data.proxy_scan_min_score = String(settings.proxy_min_score)
  }

  await apiFetch('/config', {
    method: 'PUT',
    body: JSON.stringify({ data }),
  })
  return settings
}
