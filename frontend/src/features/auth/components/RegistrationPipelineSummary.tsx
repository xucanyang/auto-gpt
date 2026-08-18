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
  if (zero.enabled !== true && pay.enabled !== true) return null

  const zeroCounts = record(zero.counts)
  const payCounts = record(pay.counts)
  const paymentEnabled = pay.payment_enabled === true
  const finished = Boolean(zero.finished ?? true) && Boolean(pay.finished ?? true)
  const failures = count(zeroCounts.probe_failed)
    + count(payCounts.extract_failed)
    + (paymentEnabled ? count(payCounts.submit_failed) : 0)
  const pendingAuth = Math.max(
    count(zeroCounts.pending_auth),
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
            {zero.enabled === true ? (
              <>
                <Tag color="success">0 元有资格 {count(zeroCounts.eligible)}</Tag>
                <Tag color="warning">非 0 元 {count(zeroCounts.ineligible)}</Tag>
                <Tag color={count(zeroCounts.probe_failed) > 0 ? 'error' : 'default'}>
                  0 元失败 {count(zeroCounts.probe_failed)}
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
                    <Tag color="processing">已交支付 {count(payCounts.submitted)}</Tag>
                    <Tag color={count(payCounts.submit_failed) > 0 ? 'error' : 'default'}>
                      支付提交失败 {count(payCounts.submit_failed)}
                    </Tag>
                  </>
                ) : null}
              </>
            ) : null}
            {pendingAuth > 0 ? <Tag color="warning">Auth 待补 {pendingAuth}</Tag> : null}
          </Space>
          <Text type="secondary" style={{ fontSize: 12 }}>
            逐账号的 0 元、提链和最终支付结果以 ChatGPT 账号表的“注册链路”为准。
          </Text>
        </Space>
      )}
    />
  )
}
