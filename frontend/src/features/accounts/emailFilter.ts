export const MAX_EXACT_EMAIL_FILTER_COUNT = 1000

export type ParsedAccountEmailFilter = {
  mode: 'empty' | 'single' | 'bulk'
  search: string
  emails: string[]
  inputCount: number
  duplicateCount: number
}

export function parseAccountEmailFilter(value: unknown): ParsedAccountEmailFilter {
  const lines = String(value || '')
    .split(/\r\n?|\n/)
    .map((line) => line.trim())
    .filter(Boolean)
  const emails: string[] = []
  const seen = new Set<string>()

  for (const line of lines) {
    const normalized = line.toLowerCase()
    if (seen.has(normalized)) continue
    seen.add(normalized)
    emails.push(normalized)
  }

  if (lines.length > 1) {
    return {
      mode: 'bulk',
      search: '',
      emails,
      inputCount: lines.length,
      duplicateCount: lines.length - emails.length,
    }
  }

  return {
    mode: emails.length === 1 ? 'single' : 'empty',
    search: lines[0] || '',
    emails,
    inputCount: lines.length,
    duplicateCount: lines.length - emails.length,
  }
}

export function canonicalizeAccountEmailFilter(value: unknown): string {
  const parsed = parseAccountEmailFilter(value)
  if (parsed.mode !== 'bulk') return parsed.search
  if (parsed.emails.length === 1) return `${parsed.emails[0]}\n${parsed.emails[0]}`
  return parsed.emails.join('\n')
}

export function hasMultipleEmailFilterLines(value: unknown): boolean {
  return String(value || '')
    .split(/\r\n?|\n/)
    .filter((line) => line.trim())
    .length > 1
}
