import { Card, Space, Tag, Typography, theme } from 'antd'

const { Text } = Typography

type IdeaSubmitAccount = {
  account_id?: number | string
  email?: string
  status?: string
  reason?: string
  cdk_id?: number | string
  code_masked?: string
  order_id?: string
  display_id?: string
  finished_at?: string
  idea_marked_unavailable?: boolean
}

type IdeaSubmitSummaryValue = {
  payment_channel?: string
  total_accounts?: number
  pair_count?: number
  submitted?: number
  paid?: number
  failed?: number
  timeout?: number
  unsubmitted?: number
  pending?: number
  marked_unavailable?: number
  success_accounts?: IdeaSubmitAccount[]
  failed_accounts?: IdeaSubmitAccount[]
  timeout_accounts?: IdeaSubmitAccount[]
  unsubmitted_accounts?: IdeaSubmitAccount[]
  marked_unavailable_accounts?: IdeaSubmitAccount[]
}

type IdeaSubmitSummaryProps = {
  summary?: IdeaSubmitSummaryValue | null
  compact?: boolean
}

function asArray(value: unknown): IdeaSubmitAccount[] {
  return Array.isArray(value) ? value.filter((item) => item && typeof item === 'object') as IdeaSubmitAccount[] : []
}

function numberValue(value: unknown): number {
  const n = Number(value || 0)
  return Number.isFinite(n) ? n : 0
}

function accountLabel(item: IdeaSubmitAccount) {
  return String(item.email || (item.account_id ? `account_id=${item.account_id}` : '-')).trim()
}

export function IdeaSubmitSummary({ summary, compact = false }: IdeaSubmitSummaryProps) {
  const { token } = theme.useToken()
  if (!summary || typeof summary !== 'object') return null

  const successAccounts = asArray(summary.success_accounts)
  const failedAccounts = asArray(summary.failed_accounts)
  const timeoutAccounts = asArray(summary.timeout_accounts)
  const unsubmittedAccounts = asArray(summary.unsubmitted_accounts)
  const markedUnavailable = asArray(summary.marked_unavailable_accounts)
  const total = numberValue(summary.total_accounts || summary.pair_count)
  const submitted = numberValue(summary.submitted)
  const paid = numberValue(summary.paid ?? successAccounts.length)
  const failed = numberValue(summary.failed ?? failedAccounts.length)
  const timeout = numberValue(summary.timeout ?? timeoutAccounts.length)
  const unsubmitted = numberValue(summary.unsubmitted ?? unsubmittedAccounts.length)
  const pending = numberValue(summary.pending)
  const unavailable = numberValue(summary.marked_unavailable ?? markedUnavailable.length)
  const isPix = String(summary.payment_channel || '').trim().toLowerCase() === 'pix'

  const sampleAccounts = [
    ...successAccounts,
    ...failedAccounts,
    ...timeoutAccounts,
    ...unsubmittedAccounts,
  ].slice(0, 3)

  return (
    <Card
      size="small"
      title={isPix ? 'PIX 提交结果总结' : 'iDEAL 提交结果总结'}
      extra={unavailable > 0 ? <Tag color="error">已标记不可用于 iDEAL / PIX 提交 {unavailable}</Tag> : null}
      style={{ marginBottom: compact ? 0 : 8 }}
      bodyStyle={{ padding: compact ? 10 : 12 }}
    >
      <Space size={[6, 6]} wrap style={{ marginBottom: 10 }}>
        <Tag>候选 {total}</Tag>
        <Tag color="processing">已受理 {submitted}</Tag>
        <Tag color="success">成功 {paid}</Tag>
        <Tag color={failed > 0 ? 'error' : 'default'}>失败 {failed}</Tag>
        <Tag color={timeout > 0 ? 'warning' : 'default'}>超时 {timeout}</Tag>
        <Tag color={unsubmitted > 0 ? 'warning' : 'default'}>未提交 {unsubmitted}</Tag>
        {pending > 0 ? <Tag color="blue">处理中 {pending}</Tag> : null}
      </Space>

      <div
        style={{
          border: `1px solid ${token.colorBorderSecondary}`,
          borderRadius: 8,
          padding: compact ? 8 : 10,
          background: token.colorFillAlter,
        }}
      >
        <Text type="secondary" style={{ display: 'block' }}>
          分类明细已写入日志末尾，一行一个账号；结果区不展示卡密、PIX CDK 或任务轮询凭据。
        </Text>
        {sampleAccounts.length > 0 ? (
          <Space size={[6, 4]} wrap style={{ marginTop: 6 }}>
            {sampleAccounts.map((item, index) => {
              const label = accountLabel(item)
              return (
                <Tag key={`${label}-${index}`} style={{ marginInlineEnd: 0 }}>
                  {label}
                </Tag>
              )
            })}
          </Space>
        ) : null}
      </div>
    </Card>
  )
}

export default IdeaSubmitSummary
