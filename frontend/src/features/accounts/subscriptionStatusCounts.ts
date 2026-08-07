export type SubscriptionStatusCountsValue = {
  plus: number
  free: number
  unknown: number
}

function normalizeCount(value: unknown) {
  const parsed = Number(value || 0)
  return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : 0
}

export function normalizeSubscriptionStatusCounts(value: unknown): SubscriptionStatusCountsValue {
  const counts = value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
  return {
    plus: normalizeCount(counts.plus),
    free: normalizeCount(counts.free),
    unknown: normalizeCount(counts.unknown),
  }
}
