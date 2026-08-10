import {
  normalizeSubscriptionStatusCounts,
  type SubscriptionStatusCountsValue,
} from '../subscriptionStatusCounts'

type SubscriptionStatusCountsProps = {
  counts?: Partial<SubscriptionStatusCountsValue> | null
  labels?: 'short' | 'full'
  splitUnknown?: boolean
  surface?: boolean
  className?: string
}

export function SubscriptionStatusCounts({
  counts,
  labels = 'full',
  splitUnknown = false,
  surface = false,
  className = '',
}: SubscriptionStatusCountsProps) {
  const normalized = normalizeSubscriptionStatusCounts(counts)
  const items = splitUnknown
    ? labels === 'short'
      ? [
          { key: 'free', label: 'f', value: normalized.free },
          { key: 'plus', label: 'p', value: normalized.plus },
          { key: 'unconfirmable', label: 'u', value: normalized.unconfirmable },
          { key: 'pending_refresh', label: 'w', value: normalized.pending_refresh },
        ]
      : [
          { key: 'free', label: 'Free', value: normalized.free },
          { key: 'plus', label: 'Plus', value: normalized.plus },
          { key: 'unconfirmable', label: '不可确认(u)', value: normalized.unconfirmable },
          { key: 'pending_refresh', label: '待刷新(w)', value: normalized.pending_refresh },
        ]
    : labels === 'short'
      ? [
          { key: 'plus', label: 'p', value: normalized.plus },
          { key: 'free', label: 'f', value: normalized.free },
          { key: 'unknown', label: 'u', value: normalized.unknown },
        ]
      : [
          { key: 'plus', label: 'Plus', value: normalized.plus },
          { key: 'free', label: 'Free', value: normalized.free },
          { key: 'unknown', label: 'Unknown', value: normalized.unknown },
        ]
  const ariaLabel = splitUnknown
    ? `Free ${normalized.free}，Plus ${normalized.plus}，不可确认 ${normalized.unconfirmable}，待刷新 ${normalized.pending_refresh}`
    : `Plus ${normalized.plus}，Free ${normalized.free}，Unknown ${normalized.unknown}`
  return (
    <span
      className={`accounts-subscription-counts${surface ? ' accounts-subscription-counts-surface' : ''}${className ? ` ${className}` : ''}`}
      aria-label={ariaLabel}
    >
      {items.map((item) => (
        <span className="accounts-subscription-count" key={item.key} aria-hidden="true">
          <span className="accounts-subscription-count-label">{item.label}:</span>
          <span className="accounts-subscription-count-value">{item.value}</span>
        </span>
      ))}
    </span>
  )
}
