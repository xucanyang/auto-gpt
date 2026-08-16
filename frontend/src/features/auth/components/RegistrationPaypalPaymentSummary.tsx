import { Alert, Space, Tag, Typography } from 'antd'
import type { CSSProperties } from 'react'

const { Text } = Typography

type RegistrationPaypalPaymentSummaryProps = {
  value: any
  style?: CSSProperties
}

const STATE_META: Record<string, { color: string; label: string }> = {
  submitted: { color: 'success', label: '已交支付队列' },
  extract_failed: { color: 'error', label: '提链失败' },
  submit_failed: { color: 'error', label: '入队失败' },
  pending_auth: { color: 'default', label: '待补 Auth' },
  running: { color: 'processing', label: '处理中' },
  queued: { color: 'processing', label: '待处理' },
  skipped: { color: 'default', label: '已跳过' },
}

function count(value: unknown): number {
  const parsed = Number(value || 0)
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 0
}

export function RegistrationPaypalPaymentSummary({
  value,
  style,
}: RegistrationPaypalPaymentSummaryProps) {
  if (!value || typeof value !== 'object' || value.enabled !== true) return null
  const counts = value.counts && typeof value.counts === 'object' ? value.counts : {}
  const results = Array.isArray(value.results) ? value.results : []
  const scheduled = count(value.scheduled)
  const completed = count(counts.completed)
  const submitted = count(counts.submitted)
  const extractFailed = count(counts.extract_failed)
  const submitFailed = count(counts.submit_failed)
  const pendingAuth = count(counts.pending_auth)
  const skipped = count(counts.skipped)
  const finished = Boolean(value.finished)
  const linkProfile = value.link_profile && typeof value.link_profile === 'object'
    ? value.link_profile
    : {}
  const paymentProfile = value.payment_profile && typeof value.payment_profile === 'object'
    ? value.payment_profile
    : {}
  const hasAttention = extractFailed > 0 || submitFailed > 0 || pendingAuth > 0 || skipped > 0
  const alertType = hasAttention ? 'warning' : finished && submitted > 0 ? 'success' : 'info'
  const linkRegion = [linkProfile.country, linkProfile.currency]
    .map((item) => String(item || '').trim().toUpperCase())
    .filter(Boolean)
    .join(' / ')
  const buyerRegion = [paymentProfile.country, paymentProfile.proxy_country]
    .map((item) => String(item || '').trim().toUpperCase())
    .filter(Boolean)
    .join(' / ')

  return (
    <Alert
      style={style}
      type={alertType}
      showIcon
      message={scheduled > 0
        ? `注册后 PayPal：已处理 ${completed} / ${scheduled}`
        : '注册后 PayPal 提链并支付'}
      description={(
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Space size={4} wrap>
            <Tag color="processing">待处理 {count(counts.queued)}</Tag>
            <Tag color="processing">处理中 {count(counts.running)}</Tag>
            <Tag color="success">已交队列 {submitted}</Tag>
            <Tag color={extractFailed > 0 ? 'error' : 'default'}>提链失败 {extractFailed}</Tag>
            <Tag color={submitFailed > 0 ? 'error' : 'default'}>入队失败 {submitFailed}</Tag>
            <Tag>待补 Auth {pendingAuth}</Tag>
            {skipped > 0 ? <Tag>已跳过 {skipped}</Tag> : null}
          </Space>
          {linkRegion || buyerRegion ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              提链 {linkRegion || '-'}
              {' · '}
              PayPal Buyer / 代理 {buyerRegion || '-'}
              {paymentProfile.browser_profile
                ? ` · ${String(paymentProfile.browser_profile)}`
                : ''}
            </Text>
          ) : null}
          {results.length > 0 ? (
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              {results.slice(-8).reverse().map((item: any, index: number) => {
                const state = String(item?.state || 'skipped').trim().toLowerCase()
                const meta = STATE_META[state] || STATE_META.skipped
                const message = String(item?.message || item?.reason_code || '').trim()
                const remote = String(item?.remote_status || '').trim()
                return (
                  <div
                    key={`${item?.account_id || item?.email || index}-${item?.completed_at || index}`}
                    style={{ display: 'flex', alignItems: 'center', gap: 4, flexWrap: 'wrap', minWidth: 0 }}
                  >
                    <Tag color={meta.color}>{meta.label}</Tag>
                    {item?.idempotent ? <Tag>幂等复用</Tag> : null}
                    <Text
                      ellipsis={{ tooltip: String(item?.email || item?.account_id || '-') }}
                      style={{ maxWidth: 240 }}
                    >
                      {String(item?.email || item?.account_id || '-')}
                    </Text>
                    {remote ? <Text type="secondary" style={{ fontSize: 12 }}>远端 {remote}</Text> : null}
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
