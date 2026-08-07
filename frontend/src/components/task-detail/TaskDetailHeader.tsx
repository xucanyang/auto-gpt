import type { ReactNode } from 'react'
import { Alert, Card, Descriptions, Space, Tag, Typography } from 'antd'
import IdeaSubmitSummary from '@/components/idea/IdeaSubmitSummary'
import { PhoneBindingResultsTable } from '@/components/phone-binding/PhoneBindingResultsTable'
import { ApprovalUrlResultsTable } from '@/components/approval-url/ApprovalUrlResultsTable'
import { SubscriptionStatusCounts } from '@/features/accounts/components/SubscriptionStatusCounts'
import { normalizeSubscriptionStatusCounts } from '@/features/accounts/subscriptionStatusCounts'
import {
  SPECIAL_OUTCOME_LABELS,
  deriveTaskStats,
  statusLabel,
  statusTagColor,
  taskObjectSummary,
  taskSourceLabel,
} from '@/lib/taskTypes'

const { Text } = Typography

type TaskLogDetailPayload = {
  task_id?: string
  status_snapshot?: string
  progress?: string
  success?: number
  skipped?: number
  errors?: string[]
  cashier_urls?: string[]
  source?: string
  meta?: Record<string, unknown>
  logs?: string[]
  attempt_outcome?: string
  email?: string
  requested_count?: number
  requested_concurrency?: number
  [key: string]: unknown
}

type TaskDetailRecord = {
  id: number
  task_id?: string
  created_at?: string
  platform?: string
  email?: string
  status?: string
  error?: string
  source?: string
  attempt_outcome?: string
  success?: number
  skipped?: number
  failed?: number
  interrupted?: number
  total?: number
  stats_available?: boolean
  meta_summary?: Record<string, unknown>
  detail?: TaskLogDetailPayload
  [key: string]: unknown
}

type TaskDetailHeaderProps = {
  record: TaskDetailRecord
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function sourceOf(record: TaskDetailRecord) {
  const detail = asRecord(record.detail)
  const meta = asRecord(detail.meta)
  return String(record.source || detail.source || meta.source || '').trim()
}

function outcomeOf(record: TaskDetailRecord) {
  const detail = asRecord(record.detail)
  return String(record.attempt_outcome || detail.attempt_outcome || '').trim()
}

function statsOf(record: TaskDetailRecord) {
  return deriveTaskStats(record)
}

function renderStatsTags(record: TaskDetailRecord) {
  const stats = statsOf(record)
  if (!stats.known) return <Text type="secondary">统计暂不可用</Text>
  const tags = [
    <Tag key="success" color={stats.success > 0 ? 'success' : undefined}>成功 {stats.success}</Tag>,
    <Tag key="skipped" color={stats.skipped > 0 ? 'warning' : undefined}>跳过 {stats.skipped}</Tag>,
    <Tag key="failed" color={stats.failed > 0 ? 'error' : undefined}>失败 {stats.failed}</Tag>,
    stats.interrupted > 0 ? <Tag key="interrupted" color="warning">中断 {stats.interrupted}</Tag> : null,
  ].filter(Boolean)
  return <Space size={4} wrap>{tags}</Space>
}

function ManualSummary({ meta }: { meta: Record<string, unknown> }) {
  const flags = asRecord(meta.extra_flags)
  const rows: { key: string; label: string; value: ReactNode }[] = [
    { key: 'requested_count', label: '请求数量', value: String(meta.requested_count ?? meta.email_count ?? '-') },
    { key: 'requested_concurrency', label: '并发', value: String(meta.requested_concurrency ?? meta.concurrency ?? '-') },
    { key: 'mail_provider', label: '邮箱服务', value: String(meta.mail_provider || flags.mail_provider || '-') },
  ]

  return (
    <Descriptions bordered size="small" column={2} items={rows.map((row) => ({ key: row.key, label: row.label, children: row.value }))} />
  )
}

function PaymentLinks({ urls }: { urls: unknown }) {
  const links = asArray(urls).map((url) => String(url || '').trim()).filter(Boolean)
  if (links.length === 0) return <Text type="secondary">暂无收银台链接</Text>
  return (
    <Card size="small" title={`收银台链接 (${links.length})`}>
      <Space direction="vertical" style={{ width: '100%' }} size={6}>
        {links.map((url, index) => (
          <Text key={`${url}-${index}`} copyable={{ text: url }} ellipsis={{ tooltip: url }} style={{ maxWidth: '100%' }}>
            {url}
          </Text>
        ))}
      </Space>
    </Card>
  )
}

function LocalStatusSummary({ meta }: { meta: Record<string, unknown> }) {
  const countsAvailable = Boolean(
    meta.subscription_counts
    && typeof meta.subscription_counts === 'object'
    && !Array.isArray(meta.subscription_counts),
  )
  if (!countsAvailable) {
    return <Text type="secondary">该历史任务未记录刷新后订阅分布</Text>
  }
  const counts = normalizeSubscriptionStatusCounts(meta.subscription_counts)
  return (
    <Descriptions
      bordered
      size="small"
      column={1}
      items={[
        {
          key: 'subscription_counts',
          label: '刷新后订阅分布',
          children: <SubscriptionStatusCounts counts={counts} labels="full" surface />,
        },
      ]}
    />
  )
}

function GenericSummary({ record, meta, errors }: { record: TaskDetailRecord; meta: Record<string, unknown>; errors: unknown[] }) {
  return (
    <Space direction="vertical" style={{ width: '100%' }} size={8}>
      <Descriptions
        bordered
        size="small"
        column={1}
        items={[{ key: 'object', label: '对象', children: taskObjectSummary(meta, record.email) }]}
      />
      {errors.length > 0 ? (
        <Alert
          type="warning"
          showIcon
          message={`错误列表 (${errors.length})`}
          description={
            <Space direction="vertical" size={4}>
              {errors.map((error, index) => <Text key={`${index}-${String(error)}`}>{String(error)}</Text>)}
            </Space>
          }
        />
      ) : null}
    </Space>
  )
}

export function TaskDetailHeader({ record }: TaskDetailHeaderProps) {
  const detail = asRecord(record.detail)
  const meta = asRecord(detail.meta)
  const source = sourceOf(record)
  const outcome = outcomeOf(record)
  const errors = asArray(detail.errors)
  const prefixSample = asRecord(meta.prefix_sample)
  const showPrefixSummary = Boolean(prefixSample.enabled)
  const runtimeResults = asArray(meta.runtime_results)
  const isPhoneSignupDetail = source === 'phone_signup'
    || Boolean(asRecord(meta.phone_signup).enabled)
    || runtimeResults.some((item) => String(asRecord(item).status || '') === 'registered_phone_signup')

  const content = (() => {
    if (source === 'phone_binding_test') {
      return (
        <PhoneBindingResultsTable
          results={runtimeResults}
          prefixSummary={meta.prefix_summary}
          showPrefixSummary={showPrefixSummary}
        />
      )
    }
    if (isPhoneSignupDetail) {
      return (
        <PhoneBindingResultsTable
          results={runtimeResults}
          prefixSummary={meta.prefix_summary}
          showPrefixSummary={showPrefixSummary || asArray(asRecord(meta.prefix_summary).items).length > 0}
          boundPhoneLines={asArray(meta.registered_phone_lines).map((line) => String(line || '')).filter(Boolean)}
          showSuccessfulLines
          emptyText="任务结束后，这里会输出已完成手机号注册的手机号。"
        />
      )
    }
    if (source === 'batch_payment_link') return <PaymentLinks urls={detail.cashier_urls} />
    if (source === 'batch_probe_local_status') return <LocalStatusSummary meta={meta} />
    if (source === 'chatgpt_oaipay_approval') return <ApprovalUrlResultsTable results={runtimeResults} />
    if (source === 'baxigpt_cdk_submit') {
      const ideaSummary = asRecord(meta.idea_submit_summary || detail.idea_submit_summary)
      return (
        <Space direction="vertical" style={{ width: '100%' }} size={10}>
          {Object.keys(ideaSummary).length > 0 ? <IdeaSubmitSummary summary={ideaSummary} compact /> : null}
          <Text type="secondary">提交账号的分类结果已在任务日志末尾按一行一个输出；历史详情不再展示卡密。</Text>
        </Space>
      )
    }
    if (source === 'manual') return <ManualSummary meta={meta} />
    return <GenericSummary record={record} meta={meta} errors={errors} />
  })()

  const displaySource = isPhoneSignupDetail && source === 'manual' ? 'phone_signup' : source

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={12}>
      <Space wrap size={[6, 8]}>
        <Tag color="blue">{taskSourceLabel(displaySource)}</Tag>
        <Tag color={statusTagColor(record.status)}>{statusLabel(record.status)}</Tag>
        {SPECIAL_OUTCOME_LABELS[outcome] ? <Tag>{SPECIAL_OUTCOME_LABELS[outcome]}</Tag> : null}
        <Text type="secondary">{record.created_at ? new Date(record.created_at).toLocaleString('zh-CN') : '-'}</Text>
        {renderStatsTags(record)}
      </Space>

      {content}

      {record.error ? <Alert type="error" showIcon message={record.error} /> : null}
      <Text type="secondary" copyable={detail.task_id ? { text: String(detail.task_id) } : false} style={{ fontSize: 12 }}>
        task_id: {String(detail.task_id || record.task_id || '-')}
      </Text>
    </Space>
  )
}
