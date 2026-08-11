export const PROJECT_TIME_ZONE = 'Asia/Shanghai'

const DATE_TIME_WITHOUT_OFFSET = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/

const BEIJING_DATE_TIME_FORMATTER = new Intl.DateTimeFormat('zh-CN', {
  timeZone: PROJECT_TIME_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
})

type DateTimePartMap = {
  year: string
  month: string
  day: string
  hour: string
  minute: string
  second: string
}

function isValidDate(value: Date): boolean {
  return Number.isFinite(value.getTime())
}

export function parseProjectDateTime(value: unknown): Date | null {
  if (value instanceof Date) return isValidDate(value) ? new Date(value.getTime()) : null

  if (typeof value === 'number' && Number.isFinite(value)) {
    const timestampMs = Math.abs(value) < 100_000_000_000 ? value * 1000 : value
    const date = new Date(timestampMs)
    return isValidDate(date) ? date : null
  }

  const text = String(value ?? '').trim()
  if (!text) return null
  if (/^-?\d+(?:\.\d+)?$/.test(text)) {
    const numeric = Number(text)
    if (Number.isFinite(numeric)) return parseProjectDateTime(numeric)
  }

  // SQLite drops tzinfo from aware datetimes. Existing offset-less project
  // rows therefore represent UTC, never the browser's local timezone.
  const normalized = DATE_TIME_WITHOUT_OFFSET.test(text)
    ? `${text.replace(' ', 'T')}Z`
    : text
  const date = new Date(normalized)
  return isValidDate(date) ? date : null
}

export function beijingDateTimeParts(value: unknown): DateTimePartMap | null {
  const date = parseProjectDateTime(value)
  if (!date) return null
  const values: Partial<DateTimePartMap> = {}
  for (const part of BEIJING_DATE_TIME_FORMATTER.formatToParts(date)) {
    if (part.type in { year: true, month: true, day: true, hour: true, minute: true, second: true }) {
      values[part.type as keyof DateTimePartMap] = part.value
    }
  }
  if (!values.year || !values.month || !values.day || !values.hour || !values.minute || !values.second) return null
  return values as DateTimePartMap
}

export function formatBeijingDateTime(value: unknown, fallback = '-'): string {
  const parts = beijingDateTimeParts(value)
  if (!parts) return String(value ?? '').trim() || fallback
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`
}

export function formatBeijingDate(value: unknown, fallback = '-'): string {
  const parts = beijingDateTimeParts(value)
  if (!parts) return String(value ?? '').trim() || fallback
  return `${parts.year}-${parts.month}-${parts.day}`
}

export function formatBeijingCompactDateTime(value: unknown, fallback = '-'): string {
  const parts = beijingDateTimeParts(value)
  if (!parts) return String(value ?? '').trim() || fallback
  return `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`
}

export function beijingFileTimestamp(value: unknown = new Date()): string {
  const parts = beijingDateTimeParts(value)
  if (!parts) return ''
  return `${parts.year}${parts.month}${parts.day}${parts.hour}${parts.minute}${parts.second}`
}
