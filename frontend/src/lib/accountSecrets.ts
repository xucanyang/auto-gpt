import { apiFetch } from '@/lib/utils'

export type AccountSecretField = 'access_token' | 'refresh_token' | 'password'
export type AccountSecrets = Partial<Record<AccountSecretField, string>>

function isPlainObject(value: unknown): value is Record<string, any> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

function parseExtraJson(raw: unknown): Record<string, any> {
  if (!raw) return {}
  try {
    const parsed = JSON.parse(String(raw || '{}'))
    return isPlainObject(parsed) ? parsed : {}
  } catch {
    return {}
  }
}

function firstText(...values: unknown[]): string {
  for (const value of values) {
    const text = String(value || '').trim()
    if (text) return text
  }
  return ''
}

function normalizeAccountSecretsPayload(payload: unknown): AccountSecrets {
  const root = isPlainObject(payload) ? payload : {}
  const secrets = isPlainObject(root.secrets) ? root.secrets : {}
  const account = isPlainObject(root.account)
    ? root.account
    : isPlainObject(root.item)
      ? root.item
      : {}
  const extra = {
    ...parseExtraJson(root.extra_json),
    ...(isPlainObject(root.extra) ? root.extra : {}),
    ...parseExtraJson(account.extra_json),
    ...(isPlainObject(account.extra) ? account.extra : {}),
  }

  return {
    access_token: firstText(
      secrets.access_token,
      secrets.accessToken,
      secrets.webAccessToken,
      root.access_token,
      root.accessToken,
      root.webAccessToken,
      root.token,
      account.access_token,
      account.accessToken,
      account.webAccessToken,
      account.token,
      extra.access_token,
      extra.accessToken,
      extra.webAccessToken,
    ),
    refresh_token: firstText(
      secrets.refresh_token,
      secrets.refreshToken,
      root.refresh_token,
      root.refreshToken,
      account.refresh_token,
      account.refreshToken,
      extra.refresh_token,
      extra.refreshToken,
    ),
    password: firstText(
      secrets.password,
      root.password,
      root.login_password,
      account.password,
      account.login_password,
    ),
  }
}

export async function fetchAccountSecrets(accountId: number, fields: AccountSecretField[]): Promise<AccountSecrets> {
  const uniqueFields = Array.from(new Set(fields)).filter(Boolean)
  if (!accountId || uniqueFields.length === 0) return {}

  try {
    const payload = await apiFetch(`/accounts/${accountId}/secrets?fields=${encodeURIComponent(uniqueFields.join(','))}`)
    return normalizeAccountSecretsPayload(payload)
  } catch {
    // Phase 0 rollout is split across workers: until /secrets lands, fall back to
    // the existing detail endpoint so copy actions still work on current backend.
    const detailPayload = await apiFetch(`/accounts/${accountId}`)
    return normalizeAccountSecretsPayload(detailPayload)
  }
}
