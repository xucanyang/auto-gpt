import { normalizeDomainList } from './domainList.ts'

export type TempMailDomainOption = {
  domain: string
  available?: boolean
  status?: string
  dns_status?: string
}

export function normalizeTempMailDomainOptions(input: unknown): TempMailDomainOption[] {
  if (!Array.isArray(input)) return []

  const seen = new Set<string>()
  const options: TempMailDomainOption[] = []
  input.forEach((raw) => {
    const item = raw && typeof raw === 'object' ? raw as TempMailDomainOption : null
    const domain = normalizeDomainList([item?.domain])[0]
    if (!domain || seen.has(domain)) return
    seen.add(domain)
    options.push({
      domain,
      available: item?.available !== false,
      status: String(item?.status || '').trim().toLowerCase(),
      dns_status: String(item?.dns_status || '').trim().toLowerCase(),
    })
  })
  return options
}

export function orderTempMailSelectedDomains(
  selectedDomains: unknown,
  preferredDomains: unknown,
  availableDomains?: unknown,
) {
  const selectedSet = new Set(normalizeDomainList(selectedDomains))
  const preferred = normalizeDomainList(preferredDomains)
  if (availableDomains === undefined) {
    return preferred.filter((domain) => selectedSet.has(domain))
  }

  const availableSet = new Set(normalizeDomainList(availableDomains))
  return preferred.filter((domain) => selectedSet.has(domain) && availableSet.has(domain))
}

export function updateTempMailPreferredMembership(
  preferredDomains: unknown,
  selectedDomains: unknown,
  domain: unknown,
  checked: boolean,
) {
  const preferred = normalizeDomainList(preferredDomains)
  const normalizedDomain = normalizeDomainList([domain])[0]
  if (!normalizedDomain) {
    return {
      preferredDomains: preferred,
      selectedDomains: orderTempMailSelectedDomains(selectedDomains, preferred),
    }
  }

  const nextPreferredDomains = checked
    ? normalizeDomainList([...preferred, normalizedDomain])
    : preferred.filter((item) => item !== normalizedDomain)
  return {
    preferredDomains: nextPreferredDomains,
    selectedDomains: orderTempMailSelectedDomains(selectedDomains, nextPreferredDomains),
  }
}

export function updateTempMailCurrentSelection(
  selectedDomains: unknown,
  preferredDomains: unknown,
  domain: unknown,
  checked: boolean,
) {
  const preferred = normalizeDomainList(preferredDomains)
  const selectedSet = new Set(normalizeDomainList(selectedDomains))
  const normalizedDomain = normalizeDomainList([domain])[0]
  if (!normalizedDomain || !preferred.includes(normalizedDomain)) {
    return orderTempMailSelectedDomains(Array.from(selectedSet), preferred)
  }

  if (checked) selectedSet.add(normalizedDomain)
  else selectedSet.delete(normalizedDomain)
  return orderTempMailSelectedDomains(Array.from(selectedSet), preferred)
}

export function clearTempMailPreferredSelection() {
  return {
    preferredDomains: [] as string[],
    selectedDomains: [] as string[],
    primaryDomain: '',
  }
}

export function clearTempMailCurrentSelection() {
  return {
    selectedDomains: [] as string[],
    primaryDomain: '',
  }
}
