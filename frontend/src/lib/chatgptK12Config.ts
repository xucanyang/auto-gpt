export const CHATGPT_K12_FIELD_NAMES = [
  'chatgpt_k12_enabled',
  'chatgpt_k12_workspace_ids',
  'chatgpt_k12_save_all_spaces',
  'chatgpt_k12_strict_join',
  'chatgpt_k12_join_timeout_seconds',
  'chatgpt_k12_join_retry_count',
  'chatgpt_k12_post_join_poll_seconds',
  'chatgpt_k12_capture_refresh_tokens',
]

export function normalizeK12WorkspaceIds(value: unknown): string[] {
  const rawItems = Array.isArray(value)
    ? value
    : String(value || '')
      .split(/[\n,\s,;，；]+/)
      .map((item) => item.trim())
  const seen = new Set<string>()
  const items: string[] = []
  rawItems.forEach((item) => {
    const text = String(item || '').trim()
    if (!text || seen.has(text)) return
    seen.add(text)
    items.push(text)
  })
  return items
}

export function booleanConfigValue(value: unknown, fallback = false): boolean {
  if (value === undefined || value === null || value === '') return fallback
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  const normalized = String(value).trim().toLowerCase()
  if (['1', 'true', 'yes', 'y', 'on', 'enabled', 'enable', '开启', '启用'].includes(normalized)) return true
  if (['0', 'false', 'no', 'n', 'off', 'disabled', 'disable', '关闭', '禁用'].includes(normalized)) return false
  return fallback
}

export function boundedInt(value: unknown, fallback: number, min: number, max: number) {
  const parsed = Number.parseInt(String(value ?? ''), 10)
  if (!Number.isFinite(parsed)) return fallback
  return Math.max(min, Math.min(max, parsed))
}

export function normalizeK12PollDelays(value: unknown, fallback = '3,8,15'): string {
  const rawItems = Array.isArray(value)
    ? value
    : String(value || '')
      .split(/[\n,\s,;，；]+/)
      .map((item) => item.trim())
  const items = rawItems
    .map((item) => Number.parseInt(String(item || '').trim(), 10))
    .filter((item) => Number.isFinite(item) && item >= 0 && item <= 3600)
  return items.length > 0 ? items.join(',') : fallback
}

export function chatgptK12InitialValues(cfg: Record<string, any> | undefined | null = {}) {
  return {
    chatgpt_k12_enabled: booleanConfigValue(cfg?.chatgpt_k12_enabled),
    chatgpt_k12_workspace_ids: normalizeK12WorkspaceIds(cfg?.chatgpt_k12_workspace_ids),
    chatgpt_k12_save_all_spaces: booleanConfigValue(cfg?.chatgpt_k12_save_all_spaces, true),
    chatgpt_k12_strict_join: booleanConfigValue(cfg?.chatgpt_k12_strict_join),
    chatgpt_k12_join_timeout_seconds: cfg?.chatgpt_k12_join_timeout_seconds || 60,
    chatgpt_k12_join_retry_count: cfg?.chatgpt_k12_join_retry_count || 2,
    chatgpt_k12_post_join_poll_seconds: cfg?.chatgpt_k12_post_join_poll_seconds || '3,8,15',
    // 预留项：当前版本不做 per-workspace RT 捕获，任何旧配置都在前端归零。
    chatgpt_k12_capture_refresh_tokens: false,
  }
}

export function buildChatGPTK12Payload(values: Record<string, any>) {
  const enabled = booleanConfigValue(values.chatgpt_k12_enabled)
  const workspaceIds = normalizeK12WorkspaceIds(values.chatgpt_k12_workspace_ids)
  const saveAllSpaces = booleanConfigValue(values.chatgpt_k12_save_all_spaces, true)
  const strictJoin = booleanConfigValue(values.chatgpt_k12_strict_join)
  const timeoutSeconds = boundedInt(values.chatgpt_k12_join_timeout_seconds, 60, 30, 3600)
  const retry = boundedInt(values.chatgpt_k12_join_retry_count, 2, 0, 20)
  const postJoinPollSeconds = normalizeK12PollDelays(values.chatgpt_k12_post_join_poll_seconds)
  const captureRefreshTokens = false

  return {
    chatgpt_k12_enabled: enabled,
    chatgpt_k12_workspace_ids: workspaceIds,
    chatgpt_k12_save_all_spaces: saveAllSpaces,
    chatgpt_k12_strict_join: strictJoin,
    chatgpt_k12_join_timeout_seconds: timeoutSeconds,
    chatgpt_k12_join_retry_count: retry,
    chatgpt_k12_post_join_poll_seconds: postJoinPollSeconds,
    chatgpt_k12_capture_refresh_tokens: captureRefreshTokens,
    chatgpt_k12: {
      enabled,
      workspace_ids: workspaceIds,
      save_all_spaces: saveAllSpaces,
      strict_join: strictJoin,
      join_timeout_seconds: timeoutSeconds,
      join_retry_count: retry,
      post_join_poll_seconds: postJoinPollSeconds,
      capture_refresh_tokens: captureRefreshTokens,
    },
  }
}

export function buildChatGPTK12ConfigData(values: Record<string, any>) {
  return {
    chatgpt_k12_enabled: booleanConfigValue(values.chatgpt_k12_enabled),
    chatgpt_k12_workspace_ids: normalizeK12WorkspaceIds(values.chatgpt_k12_workspace_ids).join(','),
    chatgpt_k12_save_all_spaces: booleanConfigValue(values.chatgpt_k12_save_all_spaces, true),
    chatgpt_k12_strict_join: booleanConfigValue(values.chatgpt_k12_strict_join),
    chatgpt_k12_join_timeout_seconds: String(boundedInt(values.chatgpt_k12_join_timeout_seconds, 60, 30, 3600)),
    chatgpt_k12_join_retry_count: String(boundedInt(values.chatgpt_k12_join_retry_count, 2, 0, 20)),
    chatgpt_k12_post_join_poll_seconds: normalizeK12PollDelays(values.chatgpt_k12_post_join_poll_seconds),
    chatgpt_k12_capture_refresh_tokens: false,
  }
}
