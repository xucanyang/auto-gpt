import { Alert, Space, Tag, Typography } from 'antd'
import type { CSSProperties } from 'react'

const { Text } = Typography

type RegistrationEligibilitySummaryProps = {
  value: any
  style?: CSSProperties
}

const STATE_META: Record<string, { color: string; label: string }> = {
  eligible: { color: 'success', label: '0 元可用' },
  ineligible: { color: 'warning', label: '非 0 元' },
  probe_failed: { color: 'error', label: '检测失败' },
  pending_auth: { color: 'default', label: '待补 Auth' },
  running: { color: 'processing', label: '检测中' },
  queued: { color: 'processing', label: '待检测' },
  skipped: { color: 'default', label: '已跳过' },
}

function count(value: unknown): number {
  const parsed = Number(value || 0)
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 0
}

export function RegistrationEligibilitySummary({ value, style }: RegistrationEligibilitySummaryProps) {
  if (!value || typeof value !== 'object' || value.enabled !== true) return null
  const counts = value.counts && typeof value.counts === 'object' ? value.counts : {}
  const results = Array.isArray(value.results) ? value.results : []
  const submitted = count(value.submitted)
  const finished = Boolean(value.finished)
  const failed = count(counts.probe_failed)
  const pendingAuth = count(counts.pending_auth)
  const skipped = count(counts.skipped)
  const profile = value.profile && typeof value.profile === 'object' ? value.profile : {}
  const proxyChain = profile.proxy_chain && typeof profile.proxy_chain === 'object' ? profile.proxy_chain : {}
  const chainLabel = [proxyChain.checkout, proxyChain.promotion, proxyChain.taxes]
    .map((item) => String(item || '').trim().toUpperCase())
    .filter(Boolean)
    .join(' -> ')
  const alertType = failed > 0 || pendingAuth > 0 || skipped > 0
    ? 'warning'
    : finished && submitted > 0
      ? 'success'
      : 'info'

  return (
    <Alert
      style={style}
      type={alertType}
      showIcon
      message={submitted > 0
        ? `注册后 0 元试用资格：已完成 ${count(counts.completed)} / ${submitted}`
        : '注册后自动检测 0 元试用资格'}
      description={(
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Space size={4} wrap>
            <Tag color="processing">待检测 {count(counts.queued)}</Tag>
            <Tag color="processing">检测中 {count(counts.running)}</Tag>
            <Tag color="success">0 元可用 {count(counts.eligible)}</Tag>
            <Tag color="warning">非 0 元 {count(counts.ineligible)}</Tag>
            <Tag color={failed > 0 ? 'error' : 'default'}>检测失败 {failed}</Tag>
            <Tag color={pendingAuth > 0 ? 'default' : undefined}>待补 Auth {pendingAuth}</Tag>
            {skipped > 0 ? <Tag>已跳过 {skipped}</Tag> : null}
          </Space>
          {chainLabel ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {String(profile.currency || 'PHP').toUpperCase()} · {chainLabel}
            </Text>
          ) : null}
          {results.length > 0 ? (
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              {results.slice(-8).reverse().map((item: any, index: number) => {
                const state = String(item?.state || 'skipped').trim().toLowerCase()
                const meta = STATE_META[state] || STATE_META.skipped
                const amount = item?.amount_minor === null || item?.amount_minor === undefined
                  ? ''
                  : `金额 ${String(item.amount_minor)} ${String(item.currency || 'PHP').toUpperCase()}（最小单位）`
                const message = String(item?.message || item?.reason_code || '').trim()
                return (
                  <div
                    key={`${item?.account_id || item?.email || index}-${item?.checked_at || index}`}
                    style={{ display: 'flex', alignItems: 'center', gap: 4, flexWrap: 'wrap', minWidth: 0 }}
                  >
                    <Tag color={meta.color}>{meta.label}</Tag>
                    <Text ellipsis={{ tooltip: String(item?.email || item?.account_id || '-') }} style={{ maxWidth: 240 }}>
                      {String(item?.email || item?.account_id || '-')}
                    </Text>
                    {amount ? <Text type="secondary" style={{ fontSize: 12 }}>{amount}</Text> : null}
                    {message ? (
                      <Text
                        type="secondary"
                        ellipsis={{ tooltip: message }}
                        style={{ flex: '1 1 180px', minWidth: 0, maxWidth: 420, fontSize: 12 }}
                      >
                        {message}
                      </Text>
                    ) : null}
                  </div>
                )
              })}
            </Space>
          ) : null}
        </Space>
      )}
    />
  )
}
