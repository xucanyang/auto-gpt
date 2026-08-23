import { Button, Space, Tag, Tooltip, Typography } from 'antd'
import { CheckOutlined, CopyOutlined, LinkOutlined } from '@ant-design/icons'

import { formatBeijingDateTime } from '@/lib/dateTime'
import {
  effectiveGcashPaymentLinkExpiryMs,
  gcashExpiryMs,
  gcashPaymentLinkIsFailed,
  gcashPaymentLinkIsRunning,
  gcashRemainingView,
  type GcashPaymentLinkSummary,
} from '@/features/accounts/gcashPaymentLink'

const { Text } = Typography

type GcashPaymentLinkCellProps = {
  value: GcashPaymentLinkSummary
  copied?: boolean
  compact?: boolean
  onCopy: () => void
}

type GcashRemainingCellProps = {
  value: GcashPaymentLinkSummary
  nowMs: number
  compact?: boolean
}

function gcashLinkStateMeta(value: GcashPaymentLinkSummary) {
  if (value.url) {
    if (String(value.state).toLowerCase() === 'expired') return { color: 'warning', label: '\u5df2\u751f\u6210' }
    return { color: 'success', label: '\u5df2\u751f\u6210' }
  }
  if (gcashPaymentLinkIsRunning(value)) return { color: 'processing', label: '\u63d0\u94fe\u4e2d' }
  if (gcashPaymentLinkIsFailed(value) || value.error) return { color: 'error', label: '\u63d0\u94fe\u5931\u8d25' }
  return null
}

export function GcashPaymentLinkCell({
  value,
  copied = false,
  compact = false,
  onCopy,
}: GcashPaymentLinkCellProps) {
  const stateMeta = gcashLinkStateMeta(value)
  if (!stateMeta) return <Text type="secondary">-</Text>

  const tooltipParts = [
    value.error,
    value.generatedAt ? `\u751f\u6210\u65f6\u95f4: ${formatBeijingDateTime(value.generatedAt)}` : '',
    value.browserTabState ? `\u8d26\u53f7\u6d4f\u89c8\u5668\u6807\u7b7e\u9875: ${value.browserTabState}` : '',
    value.url,
  ].filter(Boolean)

  return (
    <Tooltip title={tooltipParts.length > 0 ? <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{tooltipParts.join('\n')}</div> : stateMeta.label}>
      <Space size={compact ? 2 : 4} wrap={false}>
        <Tag color={stateMeta.color} style={{ marginInlineEnd: 0, whiteSpace: 'nowrap' }}>{stateMeta.label}</Tag>
        {value.url ? (
          <>
            <Button
              type="text"
              size="small"
              icon={copied ? <CheckOutlined /> : <CopyOutlined />}
              aria-label={copied ? '\u5df2\u590d\u5236 GCash \u94fe\u63a5' : '\u590d\u5236 GCash \u94fe\u63a5'}
              title={copied ? '\u5df2\u590d\u5236 GCash \u94fe\u63a5' : '\u590d\u5236 GCash \u94fe\u63a5'}
              onClick={(event) => {
                event.stopPropagation()
                onCopy()
              }}
            />
            <Button
              type="text"
              size="small"
              icon={<LinkOutlined />}
              aria-label="\u5728\u65b0\u7a97\u53e3\u6253\u5f00 GCash \u94fe\u63a5"
              title="\u5728\u65b0\u7a97\u53e3\u6253\u5f00 GCash \u94fe\u63a5"
              href={value.url}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(event) => event.stopPropagation()}
            />
          </>
        ) : null}
      </Space>
    </Tooltip>
  )
}

export function GcashRemainingCell({ value, nowMs, compact = false }: GcashRemainingCellProps) {
  const view = gcashRemainingView(value, nowMs)
  const qrExpiresAtMs = gcashExpiryMs(value.gcashQrExpiresAt)
  const linkExpiresAtMs = gcashExpiryMs(value.linkExpiresAt)
  const effectiveExpiresAtMs = effectiveGcashPaymentLinkExpiryMs(value)
  const tooltip = [
    qrExpiresAtMs ? `GCash \u4e8c\u7ef4\u7801\u5230\u671f: ${formatBeijingDateTime(qrExpiresAtMs)}` : 'GCash \u4e8c\u7ef4\u7801\u5230\u671f: \u672a\u8fd4\u56de',
    linkExpiresAtMs ? `GCash \u94fe\u63a5\u5230\u671f: ${formatBeijingDateTime(linkExpiresAtMs)}` : 'GCash \u94fe\u63a5\u5230\u671f: \u672a\u8fd4\u56de',
    effectiveExpiresAtMs ? `\u6709\u6548\u671f\u53e3\u5f84: ${formatBeijingDateTime(effectiveExpiresAtMs)}` : '\u6709\u6548\u671f\u53e3\u5f84: \u672a\u77e5',
  ].join('\n')
  const stableStyle = {
    display: 'inline-block',
    minWidth: compact ? 54 : 68,
    textAlign: 'center' as const,
    fontVariantNumeric: 'tabular-nums',
    fontFamily: 'SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
    whiteSpace: 'nowrap' as const,
  }

  if (view.state === 'unknown') {
    return <Tooltip title={tooltip}><Text type="secondary" style={stableStyle}>-</Text></Tooltip>
  }
  if (view.state === 'expired') {
    return <Tooltip title={tooltip}><Tag color="error" style={{ marginInlineEnd: 0, ...stableStyle }}>\u5df2\u8fc7\u671f</Tag></Tooltip>
  }
  if (view.state === 'warning') {
    return (
      <Tooltip title={<>\u5373\u5c06\u8fc7\u671f<br /><span style={{ whiteSpace: 'pre-wrap' }}>{tooltip}</span></>}>
        <Tag color="warning" style={{ marginInlineEnd: 0, ...stableStyle }}>{view.label}</Tag>
      </Tooltip>
    )
  }
  return <Tooltip title={tooltip}><Text style={stableStyle}>{view.label}</Text></Tooltip>
}
