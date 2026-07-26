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
  // 默认不强制国家；pool 留空=不限；dynamic 由表单校验必填
  proxy_country_code: '',
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

function explicitCountryCode(record: Record<string, unknown>, fallback = '') {
  for (const key of ['proxy_country_code', 'register_proxy_country_code', 'probe_proxy_country_code']) {
    if (Object.prototype.hasOwnProperty.call(record, key)) {
      // 允许显式空串表示「不限」，不再回落到 JP
      return String(record[key] ?? '').trim().toUpperCase().slice(0, 2)
    }
  }
  return countryCode(undefined, fallback)
}

export function normalizeTaskProxySettings(value: unknown, fallback?: Partial<TaskProxySettings>): TaskProxySettings {
  const base = { ...DEFAULT_TASK_PROXY_SETTINGS, ...(fallback || {}) }
  const record = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  const mode = normalizeTaskProxyMode(valueOf(record, 'proxy_mode', 'register_proxy_mode', 'probe_proxy_mode'), base.proxy_mode)
  return {
    proxy_mode: mode,
    proxy: stringWithDefault(valueOf(record, 'proxy', 'proxy_url', 'register_proxy', 'probe_proxy'), base.proxy),
    proxy_country_code: explicitCountryCode(record, base.proxy_country_code),
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
  const legacyTaskProxyUrl = stringWithDefault(cfg.task_proxy_url, '')
  const taskProxyUrl = stringWithDefault(legacyTaskProxyUrl, base.proxy)
  const globalDynamicTemplate = stringWithDefault(cfg.dynamic_proxy_template, legacyTaskProxyUrl)
  const proxy = mode === 'dynamic'
    ? (globalDynamicTemplate ? '' : taskProxyUrl)
    : taskProxyUrl
  // pool/specified：全局国家可预填，但允许用户清空表示不限
  // dynamic：优先 dynamic_proxy_default_country，不再硬编码 JP
  const country = mode === 'dynamic'
    ? countryCode(
      cfg.dynamic_proxy_default_country,
      countryCode(cfg.task_proxy_country_code, base.proxy_country_code),
    )
    : countryCode(cfg.task_proxy_country_code, base.proxy_country_code)
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
  const rawValues = values && typeof values === 'object' ? values as Record<string, unknown> : {}
  const payload: Record<string, unknown> = {}
  const modeField = firstOwn(rawValues, 'proxy_mode', 'register_proxy_mode', 'probe_proxy_mode')
  const proxyField = firstOwn(rawValues, 'proxy', 'proxy_url', 'register_proxy', 'probe_proxy')
  const countryField = firstOwn(rawValues, 'proxy_country_code', 'register_proxy_country_code', 'probe_proxy_country_code')
  const failoverField = firstOwn(rawValues, 'proxy_failover', 'register_proxy_failover', 'probe_proxy_failover')
  const maxCandidatesField = firstOwn(rawValues, 'proxy_max_candidates', 'register_proxy_max_candidates', 'probe_proxy_max_candidates')
  const minScoreField = firstOwn(rawValues, 'proxy_min_score', 'register_proxy_min_score', 'probe_proxy_min_score')

  if (modeField.present && isProvided(modeField.value)) payload.proxy_mode = settings.proxy_mode
  if (
    proxyField.present
    && taskProxyNeedsProxyUrl(settings)
    && (isProvided(proxyField.value) || settings.proxy_mode === 'specified')
  ) {
    payload.proxy = settings.proxy || null
  }
  if (countryField.present) payload.proxy_country_code = settings.proxy_country_code
  if (failoverField.present) payload.proxy_failover = settings.proxy_failover
  if (taskProxyUsesPoolSelector(settings)) {
    if (maxCandidatesField.present && isProvided(maxCandidatesField.value)) {
      payload.proxy_max_candidates = settings.proxy_max_candidates
    }
    if (minScoreField.present && isProvided(minScoreField.value)) {
      payload.proxy_min_score = settings.proxy_min_score
    }
  }
  if (
    settings.proxy_mode === 'dynamic'
    && hasOwn(rawValues, 'dynamic_proxy_ip_retention_minutes')
    && isProvided(rawValues.dynamic_proxy_ip_retention_minutes)
  ) {
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

function hasOwn(record: Record<string, unknown>, key: string) {
  return Object.prototype.hasOwnProperty.call(record, key)
}

function firstOwn(record: Record<string, unknown>, ...keys: string[]) {
  for (const key of keys) {
    if (hasOwn(record, key)) return { present: true, value: record[key] }
  }
  return { present: false, value: undefined }
}

function isProvided(value: unknown) {
  return value !== undefined && value !== null && value !== ''
}

function putBoolean(data: Record<string, string>, key: string, value: unknown, fallback: boolean) {
  data[key] = booleanWithDefault(value, fallback) ? 'true' : 'false'
}

function putNumber(
  data: Record<string, string>,
  key: string,
  value: unknown,
  fallback: number,
  minimum: number,
  maximum: number,
) {
  data[key] = String(numberWithDefault(value, fallback, minimum, maximum))
}

/**
 * Build a field-level global-config patch from an explicitly supplied task
 * form. Missing fields are intentionally omitted: task forms are often
 * rendered without the global retention/node controls and must not reset the
 * shared dynamic-node configuration to frontend defaults.
 */
export function buildTaskProxyConfigPatch(values: unknown): Record<string, string> {
  const rawValues = values && typeof values === 'object' ? values as Record<string, unknown> : {}
  const data: Record<string, string> = {}
  const modeField = firstOwn(rawValues, 'proxy_mode', 'register_proxy_mode', 'probe_proxy_mode')
  const modeProvided = modeField.present && isProvided(modeField.value)
  const mode = modeProvided
    ? normalizeTaskProxyMode(modeField.value)
    : undefined
  if (modeProvided) data.task_proxy_mode = mode || 'dynamic'

  const proxyField = firstOwn(rawValues, 'proxy', 'proxy_url', 'register_proxy', 'probe_proxy')
  const countryField = firstOwn(rawValues, 'proxy_country_code', 'register_proxy_country_code', 'probe_proxy_country_code')
  const failoverField = firstOwn(rawValues, 'proxy_failover', 'register_proxy_failover', 'probe_proxy_failover')
  const maxCandidatesField = firstOwn(rawValues, 'proxy_max_candidates', 'register_proxy_max_candidates', 'probe_proxy_max_candidates')
  const minScoreField = firstOwn(rawValues, 'proxy_min_score', 'register_proxy_min_score', 'probe_proxy_min_score')
  const canonicalTemplateField = firstOwn(rawValues, 'dynamic_proxy_template')
  const canonicalCountryField = firstOwn(rawValues, 'dynamic_proxy_default_country')

  // A canonical field is also a valid direct input for the Settings page;
  // task forms normally use the shorter proxy/country aliases above.
  const dynamicIntent = mode === 'dynamic'
    || canonicalTemplateField.present
    || canonicalCountryField.present
    || (hasOwn(rawValues, 'dynamic_proxy_ip_retention_minutes') && isProvided(rawValues.dynamic_proxy_ip_retention_minutes))
    || (hasOwn(rawValues, 'dynamic_proxy_probe_enabled') && isProvided(rawValues.dynamic_proxy_probe_enabled))
  const effectiveMode = mode || (dynamicIntent ? 'dynamic' : undefined)

  if (effectiveMode === 'dynamic') {
    const templateValue = canonicalTemplateField.present
      ? stringWithDefault(canonicalTemplateField.value, '')
      : proxyField.present
        ? stringWithDefault(proxyField.value, '')
        : ''
    // Empty task-level proxy means “use the existing global dynamic node”; it
    // is not a request to erase the shared node.
    if (templateValue) data.dynamic_proxy_template = templateValue

    const countryValue = canonicalCountryField.present
      ? countryCode(canonicalCountryField.value)
      : countryField.present
        ? countryCode(countryField.value)
        : ''
    // An empty dynamic country is invalid for execution, so preserve the
    // existing default instead of silently replacing it with a fallback.
    if (countryValue) data.dynamic_proxy_default_country = countryValue

    if (failoverField.present && isProvided(failoverField.value)) putBoolean(data, 'task_proxy_failover', failoverField.value, false)
    if (
      hasOwn(rawValues, 'dynamic_proxy_ip_retention_minutes')
      && isProvided(rawValues.dynamic_proxy_ip_retention_minutes)
    ) {
      putNumber(data, 'dynamic_proxy_ip_retention_minutes', rawValues.dynamic_proxy_ip_retention_minutes, 5, 1, 1440)
    }
    if (hasOwn(rawValues, 'dynamic_proxy_probe_enabled') && isProvided(rawValues.dynamic_proxy_probe_enabled)) {
      putBoolean(data, 'dynamic_proxy_probe_enabled', rawValues.dynamic_proxy_probe_enabled, true)
    }
    if (hasOwn(rawValues, 'dynamic_proxy_require_country_match') && isProvided(rawValues.dynamic_proxy_require_country_match)) {
      putBoolean(data, 'dynamic_proxy_require_country_match', rawValues.dynamic_proxy_require_country_match, true)
    }
    if (hasOwn(rawValues, 'dynamic_proxy_probe_timeout_seconds') && isProvided(rawValues.dynamic_proxy_probe_timeout_seconds)) {
      putNumber(data, 'dynamic_proxy_probe_timeout_seconds', rawValues.dynamic_proxy_probe_timeout_seconds, 8, 2, 60)
    }

    // Keep the legacy runtime fields empty whenever dynamic mode is explicitly
    // selected. This is a compatibility cleanup, not a source of defaults.
    if (modeProvided) {
      data.task_proxy_url = ''
      data.task_proxy_country_code = ''
    }
  } else if (effectiveMode === 'specified') {
    if (proxyField.present) data.task_proxy_url = stringWithDefault(proxyField.value, '')
    if (countryField.present) data.task_proxy_country_code = countryCode(countryField.value)
    if (failoverField.present && isProvided(failoverField.value)) putBoolean(data, 'task_proxy_failover', failoverField.value, false)
    if (maxCandidatesField.present && isProvided(maxCandidatesField.value)) {
      putNumber(data, 'task_proxy_max_candidates', maxCandidatesField.value, 5, 1, 100)
      data.proxy_pool_max_candidates = data.task_proxy_max_candidates
    }
    if (minScoreField.present && isProvided(minScoreField.value)) {
      putNumber(data, 'task_proxy_min_score', minScoreField.value, 50, 0, 100)
      data.proxy_scan_min_score = data.task_proxy_min_score
    }
  } else if (effectiveMode === 'pool') {
    if (countryField.present) data.task_proxy_country_code = countryCode(countryField.value)
    if (failoverField.present && isProvided(failoverField.value)) putBoolean(data, 'task_proxy_failover', failoverField.value, false)
    if (maxCandidatesField.present && isProvided(maxCandidatesField.value)) {
      putNumber(data, 'task_proxy_max_candidates', maxCandidatesField.value, 5, 1, 100)
      data.proxy_pool_max_candidates = data.task_proxy_max_candidates
    }
    if (minScoreField.present && isProvided(minScoreField.value)) {
      putNumber(data, 'task_proxy_min_score', minScoreField.value, 50, 0, 100)
      data.proxy_scan_min_score = data.task_proxy_min_score
    }
  } else if (failoverField.present && isProvided(failoverField.value)) {
    putBoolean(data, 'task_proxy_failover', failoverField.value, false)
  }

  // The proxy page exposes this global runtime switch alongside the node. It
  // is intentionally guarded by field presence so unrelated forms cannot
  // reset it.
  if (
    hasOwn(rawValues, 'dynamic_proxy_probe_enabled')
    && isProvided(rawValues.dynamic_proxy_probe_enabled)
    && !hasOwn(data, 'dynamic_proxy_probe_enabled')
  ) {
    putBoolean(data, 'dynamic_proxy_probe_enabled', rawValues.dynamic_proxy_probe_enabled, true)
  }
  return data
}

export async function saveTaskProxySettingsToConfig(
  values: unknown,
  options: { baseRevision?: number } = {},
) {
  const settings = normalizeTaskProxySettings(values)
  const data = buildTaskProxyConfigPatch(values)
  if (Object.keys(data).length === 0) return settings

  const body: Record<string, unknown> = { data }
  if (options.baseRevision !== undefined) body.base_revision = options.baseRevision
  await apiFetch('/config', {
    method: 'PUT',
    body: JSON.stringify(body),
  })
  return settings
}
