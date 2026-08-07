import {
  normalizeSubscriptionStatusCounts,
  type SubscriptionStatusCountsValue,
} from '../subscriptionStatusCounts'

type SubscriptionStatusCountsProps = {
  counts?: Partial<SubscriptionStatusCountsValue> | null
  labels?: 'short' | 'full'
  surface?: boolean
  className?: string
}

export function SubscriptionStatusCounts({
  counts,
  labels = 'full',
  surface = false,
  className = '',
}: SubscriptionStatusCountsProps) {
  const normalized = normalizeSubscriptionStatusCounts(counts)
  const items = labels === 'short'
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
  const ariaLabel = `Plus ${normalized.plus}，Free ${normalized.free}，Unknown ${normalized.unknown}`
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
