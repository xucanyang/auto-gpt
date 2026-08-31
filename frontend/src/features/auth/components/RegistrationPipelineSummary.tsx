import { Alert, Space, Tag, Typography } from 'antd'
import type { CSSProperties } from 'react'

const { Text } = Typography

type RegistrationPipelineSummaryProps = {
  success?: number
  zeroAmount: unknown
  paypal: unknown
  style?: CSSProperties
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function count(value: unknown): number {
  const parsed = Number(value || 0)
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 0
}

export function RegistrationPipelineSummary({
  success = 0,
  zeroAmount,
  paypal,
  style,
}: RegistrationPipelineSummaryProps) {
  const zero = record(zeroAmount)
  const pay = record(paypal)
  const requestedChildKinds = Array.isArray(zero.requested_child_kinds)
    ? zero.requested_child_kinds.map((value) => String(value || '').trim().toLowerCase())
    : []
  const bundled = String(zero.kind || '').trim().toLowerCase() === 'payment_eligibility_bundle'
  const zeroEnabled = zero.enabled === true
    && (!bundled || requestedChildKinds.includes('zero_amount_eligibility'))
  const paymentDetailsEnabled = zero.enabled === true
    && bundled
    && (
      requestedChildKinds.includes('checkout_link_type')
      || requestedChildKinds.includes('payment_methods')
    )
  if (!zeroEnabled && !paymentDetailsEnabled && pay.enabled !== true) return null

  const childCounts = record(zero.child_counts)
  const zeroCounts = bundled
    ? record(childCounts.zero_amount_eligibility)
    : record(zero.counts)
  const linkCounts = record(childCounts.checkout_link_type)
  const methodCounts = record(childCounts.payment_methods)
  const payCounts = record(pay.counts)
  const payFollowup = record(pay.followup)
  const paymentEnabled = pay.payment_enabled === true
  const followupActive = count(payFollowup.active)
  const finished = Boolean(zero.finished ?? true)
    && Boolean(pay.finished ?? true)
    && followupActive === 0
  const failures = count(zeroCounts.probe_failed)
    + count(linkCounts.probe_failed)
    + count(methodCounts.probe_failed)
    + count(payCounts.extract_failed)
    + (paymentEnabled ? count(payCounts.submit_failed) : 0)
    + (paymentEnabled ? count(payFollowup.failed) + count(payFollowup.unknown) : 0)
  const pendingAuth = Math.max(
    count(record(zero.counts).pending_auth),
    count(zeroCounts.pending_auth),
    count(linkCounts.pending_auth),
    count(methodCounts.pending_auth),
    count(payCounts.pending_auth),
  )

  return (
    <Alert
      style={style}
      type={failures > 0 || pendingAuth > 0 ? 'warning' : finished ? 'success' : 'info'}
      showIcon
      message="注册链路汇总"
      description={(
        <Space direction="vertical" size={6} style={{ width: '100%' }}>
          <Space size={4} wrap>
            <Tag color="success">注册成功 {count(success)}</Tag>
            {zeroEnabled ? (
              <>
                <Tag color="success">0 元有资格 {count(zeroCounts.eligible)}</Tag>
                <Tag color="warning">非 0 元 {count(zeroCounts.ineligible)}</Tag>
                <Tag color={count(zeroCounts.probe_failed) > 0 ? 'error' : 'default'}>
                  0 元失败 {count(zeroCounts.probe_failed)}
                </Tag>
              </>
            ) : null}
            {paymentDetailsEnabled ? (
              <>
                <Tag color="blue">OAICS {count(linkCounts.oaics)}</Tag>
                <Tag color="purple">Stripe (CS) {count(linkCounts.cs)}</Tag>
                <Tag color="success">支付方式可用 {count(methodCounts.available)}</Tag>
                <Tag color="default">无可用方式 {count(methodCounts.no_methods)}</Tag>
                <Tag
                  color={count(linkCounts.probe_failed) + count(methodCounts.probe_failed) > 0
                    ? 'error'
                    : 'default'}
                >
                  明细失败 {count(linkCounts.probe_failed) + count(methodCounts.probe_failed)}
                </Tag>
              </>
            ) : null}
            {pay.enabled === true ? (
              <>
                <Tag color="success">
                  提链成功 {count(payCounts.link_succeeded)
                    + count(payCounts.submitted)
                    + count(payCounts.submit_failed)}
                </Tag>
                <Tag color={count(payCounts.extract_failed) > 0 ? 'error' : 'default'}>
                  提链失败 {count(payCounts.extract_failed)}
                </Tag>
                {paymentEnabled ? (
                  <>
                    <Tag color="processing">支付已入队 {count(payCounts.submitted)}</Tag>
                    <Tag color={count(payCounts.submit_failed) > 0 ? 'error' : 'default'}>
                      支付提交失败 {count(payCounts.submit_failed)}
                    </Tag>
                    <Tag color="processing">
                      支付处理中 {count(payFollowup.processing)}
                    </Tag>
                    <Tag color="success">支付成功 {count(payFollowup.succeeded)}</Tag>
                    <Tag color={count(payFollowup.failed) > 0 ? 'error' : 'default'}>
                      支付失败 {count(payFollowup.failed)}
                    </Tag>
                    <Tag color={count(payFollowup.unknown) > 0 ? 'warning' : 'default'}>
                      支付结果未知 {count(payFollowup.unknown)}
                    </Tag>
                  </>
                ) : null}
              </>
            ) : null}
            {pendingAuth > 0 ? <Tag color="warning">Auth 待补 {pendingAuth}</Tag> : null}
          </Space>
          <Text type="secondary" style={{ fontSize: 12 }}>
            逐账号的 0 元、链接格式、支付方式、提链和最终支付结果以 ChatGPT 账号表为准。
          </Text>
        </Space>
      )}
    />
  )
}
