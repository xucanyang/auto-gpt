import { CopyOutlined } from '@ant-design/icons'
import { Alert, Button, Input, Space, Table, Tag, Typography, message } from 'antd'
import type { TableColumnsType } from 'antd'
import type { CSSProperties } from 'react'

const { Text } = Typography

type RegistrationPaypalPaymentSummaryProps = {
  value: unknown
  style?: CSSProperties
}

type PaypalPaymentResult = Record<string, unknown>

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

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function resultList(value: unknown): PaypalPaymentResult[] {
  return Array.isArray(value) ? value.filter(isRecord) : []
}

function copyTextToClipboardFallback(text: string) {
  if (typeof document === 'undefined') return false
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', 'true')
  textarea.style.position = 'fixed'
  textarea.style.top = '0'
  textarea.style.left = '-9999px'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.focus()
  textarea.select()
  textarea.setSelectionRange(0, textarea.value.length)
  try {
    return document.execCommand('copy')
  } finally {
    document.body.removeChild(textarea)
  }
}

async function copyText(text: string, label: string) {
  const content = String(text || '').trim()
  if (!content) {
    message.warning(`没有可复制的${label}`)
    return
  }
  try {
    let copied = false
    if (
      typeof window !== 'undefined'
      && typeof navigator !== 'undefined'
      && navigator.clipboard?.writeText
      && window.isSecureContext
    ) {
      try {
        await navigator.clipboard.writeText(content)
        copied = true
      } catch {
        copied = false
      }
    }
    if (!copied) copied = copyTextToClipboardFallback(content)
    if (!copied) throw new Error('copy failed')
    message.success(`已复制${label}`)
  } catch (error) {
    console.warn('copy failed', error)
    message.error(`复制${label}失败`)
  }
}

function resultState(value: unknown): string {
  return String(value || '').trim().toLowerCase()
}

function accountResultLine(item: PaypalPaymentResult): string {
  const accountId = String(item?.account_id || '').trim()
  const email = String(item?.email || '').trim()
  if (accountId && email) return `[${accountId}] ${email}`
  return email || (accountId ? `[${accountId}]` : '')
}

const submittedResultColumns: TableColumnsType<PaypalPaymentResult> = [
  {
    title: '账号 ID',
    dataIndex: 'account_id',
    width: 100,
    render: (value: unknown) => <Text copyable={Boolean(value)}>{String(value || '-')}</Text>,
  },
  {
    title: '账号邮箱',
    dataIndex: 'email',
    width: 240,
    render: (value: unknown) => <Text copyable={Boolean(value)}>{String(value || '-')}</Text>,
  },
  {
    title: '提交结果',
    dataIndex: 'state',
    width: 110,
    render: () => <Tag color="success">已交队列</Tag>,
  },
  {
    title: '远端状态',
    dataIndex: 'remote_status',
    width: 110,
    render: (value: unknown) => <Tag color="processing">{String(value || '-')}</Tag>,
  },
  {
    title: '批次 ID',
    dataIndex: 'batch_id',
    width: 170,
    render: (value: unknown) => (
      <Text
        copyable={value ? { text: String(value), tooltips: ['复制批次 ID', '已复制'] } : false}
        ellipsis={{ tooltip: String(value || '') }}
        style={{ maxWidth: 145, fontFamily: 'monospace', fontSize: 12 }}
      >
        {String(value || '-')}
      </Text>
    ),
  },
  {
    title: '条目 ID',
    dataIndex: 'item_id',
    width: 170,
    render: (value: unknown) => (
      <Text
        copyable={value ? { text: String(value), tooltips: ['复制条目 ID', '已复制'] } : false}
        ellipsis={{ tooltip: String(value || '') }}
        style={{ maxWidth: 145, fontFamily: 'monospace', fontSize: 12 }}
      >
        {String(value || '-')}
      </Text>
    ),
  },
  {
    title: '提交时间',
    dataIndex: 'completed_at',
    width: 210,
    render: (value: unknown) => <Text>{String(value || '-')}</Text>,
  },
]

export function RegistrationPaypalPaymentSummary({
  value,
  style,
}: RegistrationPaypalPaymentSummaryProps) {
  if (!isRecord(value) || value.enabled !== true) return null
  const counts = isRecord(value.counts) ? value.counts : {}
  const results = resultList(value.results)
  const submittedResults = Array.isArray(value.submitted_results)
    ? resultList(value.submitted_results).filter((item) => resultState(item.state) === 'submitted')
    : results.filter((item) => resultState(item.state) === 'submitted')
  const nonSubmittedResults = results.filter((item) => resultState(item.state) !== 'submitted')
  const scheduled = count(value.scheduled)
  const completed = count(counts.completed)
  const submitted = count(counts.submitted)
  const extractFailed = count(counts.extract_failed)
  const submitFailed = count(counts.submit_failed)
  const pendingAuth = count(counts.pending_auth)
  const skipped = count(counts.skipped)
  const finished = Boolean(value.finished)
  const linkProfile = isRecord(value.link_profile) ? value.link_profile : {}
  const paymentProfile = isRecord(value.payment_profile) ? value.payment_profile : {}
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
  const submittedAccountLines = Array.from(new Set(
    submittedResults.map(accountResultLine).filter(Boolean),
  ))
  const submittedEmails = Array.from(new Set(
    submittedResults
      .map((item) => String(item.email || '').trim())
      .filter(Boolean),
  ))
  const submittedResultTotal = Math.max(
    submitted,
    count(value.submitted_results_total),
    submittedResults.length,
  )
  const submittedResultsTruncated = Boolean(value.submitted_results_truncated)
    || submittedResultTotal > submittedResults.length

  return (
    <Space direction="vertical" size={12} style={{ width: '100%', ...style }}>
      <Alert
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
            {nonSubmittedResults.length > 0 ? (
              <Space direction="vertical" size={4} style={{ width: '100%' }}>
                {nonSubmittedResults.slice(-8).reverse().map((item, index) => {
                  const state = String(item.state || 'skipped').trim().toLowerCase()
                  const meta = STATE_META[state] || STATE_META.skipped
                  const resultMessage = String(item.message || item.reason_code || '').trim()
                  const remote = String(item.remote_status || '').trim()
                  return (
                    <div
                      key={`${item.account_id || item.email || index}-${item.completed_at || index}`}
                      style={{ display: 'flex', alignItems: 'center', gap: 4, flexWrap: 'wrap', minWidth: 0 }}
                    >
                      <Tag color={meta.color}>{meta.label}</Tag>
                      {item.idempotent ? <Tag>幂等复用</Tag> : null}
                      <Text
                        ellipsis={{ tooltip: String(item.email || item.account_id || '-') }}
                        style={{ maxWidth: 240 }}
                      >
                        {String(item.email || item.account_id || '-')}
                      </Text>
                      {remote ? <Text type="secondary" style={{ fontSize: 12 }}>远端 {remote}</Text> : null}
                      {resultMessage ? (
                        <Text
                          type="secondary"
                          ellipsis={{ tooltip: resultMessage }}
                          style={{ flex: '1 1 180px', minWidth: 0, maxWidth: 420, fontSize: 12 }}
                        >
                          {resultMessage}
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
      <Alert
        type={submittedResultTotal > 0 ? 'success' : 'info'}
        showIcon
        message={`提链成功并已提交支付队列：${submittedResultTotal} 个`}
        description={(
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Text type="secondary">
              此处仅表示 PayPal approval URL 已成功提取并交给支付队列，不代表最终支付完成。
            </Text>
            {submittedAccountLines.length > 0 ? (
              <>
                <Input.TextArea
                  value={submittedAccountLines.join('\n')}
                  autoSize={{ minRows: 2, maxRows: 6 }}
                  readOnly
                />
                <Space wrap>
                  <Button
                    size="small"
                    icon={<CopyOutlined />}
                    disabled={submittedEmails.length === 0}
                    onClick={() => copyText(submittedEmails.join('\n'), '成功账号')}
                  >
                    复制成功账号
                  </Button>
                  <Button
                    size="small"
                    icon={<CopyOutlined />}
                    disabled={submittedAccountLines.length === 0}
                    onClick={() => copyText(submittedAccountLines.join('\n'), '账号 ID 和邮箱')}
                  >
                    复制账号 ID + 邮箱
                  </Button>
                </Space>
                {submittedResultsTruncated ? (
                  <Text type="warning">
                    成功账号共 {submittedResultTotal} 个，当前任务快照保留最近 {submittedResults.length} 条明细。
                  </Text>
                ) : null}
                <Table
                  size="small"
                  rowKey={(item, index) => `${item.account_id || item.email || 'account'}-${item.item_id || item.completed_at || index}`}
                  columns={submittedResultColumns}
                  dataSource={[...submittedResults].reverse()}
                  pagination={false}
                  scroll={{ x: 1110, y: 240 }}
                />
              </>
            ) : (
              <Text type="secondary">
                {finished ? '本任务没有提链并入队成功的账号。' : '任务运行中，成功账号会实时显示在这里。'}
              </Text>
            )}
          </Space>
        )}
      />
    </Space>
  )
}
