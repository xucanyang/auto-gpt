export function normalizeDomainList(input: unknown): string[] {
  const rawItems = Array.isArray(input)
    ? input
    : String(input || '')
      .split(/[\n,;]+/)
      .map((item) => item.trim())

  const seen = new Set<string>()
  const domains: string[] = []
  rawItems.forEach((item) => {
    const domain = String(item || '').trim().toLowerCase().replace(/^@+/, '').replace(/^\.+/, '')
    if (!domain || seen.has(domain)) return
    seen.add(domain)
    domains.push(domain)
  })
  return domains
}

export function parseStoredDomainList(value: unknown): string[] {
  if (Array.isArray(value)) return normalizeDomainList(value)
  const text = String(value || '').trim()
  if (!text) return []
  if (text.startsWith('[')) {
    try {
      const parsed = JSON.parse(text)
      if (Array.isArray(parsed)) return normalizeDomainList(parsed)
    } catch {
      // Fall through to comma/newline parsing.
    }
  }
  return normalizeDomainList(text)
}
