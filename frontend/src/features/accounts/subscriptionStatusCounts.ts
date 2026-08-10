export type SubscriptionStatusCountsValue = {
  plus: number
  free: number
  unknown: number
  unconfirmable: number
  pending_refresh: number
}

function normalizeCount(value: unknown) {
  const parsed = Number(value || 0)
  return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : 0
}

export function normalizeSubscriptionStatusCounts(value: unknown): SubscriptionStatusCountsValue {
  const counts = value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
  const hasSplitUnknown = Object.prototype.hasOwnProperty.call(counts, 'unconfirmable')
    || Object.prototype.hasOwnProperty.call(counts, 'pending_refresh')
  const legacyUnknown = normalizeCount(counts.unknown)
  const unconfirmable = hasSplitUnknown ? normalizeCount(counts.unconfirmable) : legacyUnknown
  const pendingRefresh = hasSplitUnknown ? normalizeCount(counts.pending_refresh) : 0
  return {
    plus: normalizeCount(counts.plus),
    free: normalizeCount(counts.free),
    unknown: hasSplitUnknown ? unconfirmable + pendingRefresh : legacyUnknown,
    unconfirmable,
    pending_refresh: pendingRefresh,
  }
}
