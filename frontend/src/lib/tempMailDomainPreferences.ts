import { normalizeDomainList } from './domainList.ts'
import { apiFetch } from './utils.ts'

export const TEMPMAIL_PREFERRED_DOMAINS_CHANGED_EVENT = 'auto-chatgpt:tempmail-preferred-domains-changed'

type PreferenceApiFetch = (
  path: string,
  options?: RequestInit,
) => Promise<unknown>

function normalizeScope(scope: unknown) {
  return String(scope || 'chatgpt').trim().toLowerCase() || 'chatgpt'
}

export function resolveTempMailPreferredDomains(
  scope: unknown,
  configuredDomains: unknown,
) {
  normalizeScope(scope)
  return normalizeDomainList(configuredDomains)
}

export function tempMailPreferredDomainsConfigPatch(domains: unknown) {
  const normalized = normalizeDomainList(domains)
  return {
    tempmail_fixed_domains: JSON.stringify(normalized),
    tempmail_primary_domain: normalized[0] || '',
  }
}

export async function saveTempMailPreferredDomains(
  scope: unknown,
  domains: unknown,
  request: PreferenceApiFetch = apiFetch,
) {
  const normalizedScope = normalizeScope(scope)
  const normalized = normalizeDomainList(domains)
  await request('/config', {
    method: 'PUT',
    body: JSON.stringify({
      data: tempMailPreferredDomainsConfigPatch(normalized),
    }),
  })

  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(TEMPMAIL_PREFERRED_DOMAINS_CHANGED_EVENT, {
      detail: { scope: normalizedScope, domains: normalized },
    }))
  }
  return normalized
}

export function sameTempMailDomainOrder(left: unknown, right: unknown) {
  const normalizedLeft = normalizeDomainList(left)
  const normalizedRight = normalizeDomainList(right)
  return normalizedLeft.length === normalizedRight.length
    && normalizedLeft.every((domain, index) => domain === normalizedRight[index])
}
