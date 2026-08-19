import { normalizeDomainList } from './domainList.ts'

export const TEMPMAIL_PREFERRED_DOMAINS_STORAGE_PREFIX = 'auto-chatgpt.tempmail-preferred-domains.v1.'
export const TEMPMAIL_PREFERRED_DOMAINS_CHANGED_EVENT = 'auto-chatgpt:tempmail-preferred-domains-changed'

type StorageLike = Pick<Storage, 'getItem' | 'setItem'>

function normalizeScope(scope: unknown) {
  return String(scope || 'chatgpt').trim().toLowerCase() || 'chatgpt'
}

function browserStorage(): StorageLike | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
}

export function tempMailPreferredDomainsStorageKey(scope: unknown) {
  return `${TEMPMAIL_PREFERRED_DOMAINS_STORAGE_PREFIX}${normalizeScope(scope)}`
}

export function loadTempMailPreferredDomains(
  scope: unknown,
  storage: StorageLike | null = browserStorage(),
): string[] | null {
  if (!storage) return null
  try {
    const raw = storage.getItem(tempMailPreferredDomainsStorageKey(scope))
    if (raw === null) return null
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? normalizeDomainList(parsed) : null
  } catch {
    return null
  }
}

export function resolveTempMailPreferredDomains(
  scope: unknown,
  fallback: unknown,
  storage: StorageLike | null = browserStorage(),
) {
  return loadTempMailPreferredDomains(scope, storage) ?? normalizeDomainList(fallback)
}

export function saveTempMailPreferredDomains(
  scope: unknown,
  domains: unknown,
  storage: StorageLike | null = browserStorage(),
) {
  if (!storage) return false
  const normalized = normalizeDomainList(domains)
  try {
    storage.setItem(tempMailPreferredDomainsStorageKey(scope), JSON.stringify(normalized))
  } catch {
    return false
  }

  if (typeof window !== 'undefined' && storage === browserStorage()) {
    window.dispatchEvent(new CustomEvent(TEMPMAIL_PREFERRED_DOMAINS_CHANGED_EVENT, {
      detail: { scope: normalizeScope(scope), domains: normalized },
    }))
  }
  return true
}

export function sameTempMailDomainOrder(left: unknown, right: unknown) {
  const normalizedLeft = normalizeDomainList(left)
  const normalizedRight = normalizeDomainList(right)
  return normalizedLeft.length === normalizedRight.length
    && normalizedLeft.every((domain, index) => domain === normalizedRight[index])
}
