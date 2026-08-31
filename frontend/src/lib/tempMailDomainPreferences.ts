import { normalizeDomainList } from './domainList.ts'

export const TEMPMAIL_PREFERRED_DOMAINS_STORAGE_PREFIX = 'auto-chatgpt.tempmail-preferred-domains.v1.'
export const TEMPMAIL_PREFERRED_DOMAINS_CHANGED_EVENT = 'auto-chatgpt:tempmail-preferred-domains-changed'

type StorageLike = Pick<Storage, 'getItem' | 'setItem'>

const AUTO_PLUS3_FAILED_DOMAIN_CLEANUP_HOST = 'auto-plus3.cccy.me'
const AUTO_PLUS3_FAILED_DOMAIN_CLEANUP_MARKER_PREFIX = 'auto-chatgpt.tempmail-preferred-domains.migration.v1.auto-plus3-20260901.'
const AUTO_PLUS3_FAILED_DOMAINS = new Set([
  'f867.com',
  'gdyfcw.com',
  'ieazg.com',
  'sefg.asia',
  'tadouhy.com',
  'xmdjxds.com',
  'yhegsi.com',
])

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

function browserHostname() {
  if (typeof window === 'undefined') return ''
  return String(window.location?.hostname || '').trim().toLowerCase()
}

function migrateAutoPlus3FailedDomains(
  scope: unknown,
  domains: string[],
  storage: StorageLike,
  hostname: unknown,
) {
  const normalizedScope = normalizeScope(scope)
  const normalizedHostname = String(hostname || '').trim().toLowerCase()
  if (
    normalizedScope !== 'chatgpt'
    || normalizedHostname !== AUTO_PLUS3_FAILED_DOMAIN_CLEANUP_HOST
  ) {
    return domains
  }

  const markerKey = `${AUTO_PLUS3_FAILED_DOMAIN_CLEANUP_MARKER_PREFIX}${normalizedScope}`
  try {
    if (storage.getItem(markerKey) === '1') return domains
    const migrated = domains.filter((domain) => !AUTO_PLUS3_FAILED_DOMAINS.has(domain))
    storage.setItem(tempMailPreferredDomainsStorageKey(normalizedScope), JSON.stringify(migrated))
    storage.setItem(markerKey, '1')
    return migrated
  } catch {
    return domains
  }
}

export function tempMailPreferredDomainsStorageKey(scope: unknown) {
  return `${TEMPMAIL_PREFERRED_DOMAINS_STORAGE_PREFIX}${normalizeScope(scope)}`
}

export function loadTempMailPreferredDomains(
  scope: unknown,
  storage: StorageLike | null = browserStorage(),
  hostname: unknown = browserHostname(),
): string[] | null {
  if (!storage) return null
  try {
    const raw = storage.getItem(tempMailPreferredDomainsStorageKey(scope))
    if (raw === null) return null
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return null
    return migrateAutoPlus3FailedDomains(
      scope,
      normalizeDomainList(parsed),
      storage,
      hostname,
    )
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
