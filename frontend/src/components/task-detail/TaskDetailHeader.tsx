import type { ReactNode } from 'react'
import { Alert, Card, Descriptions, Space, Tag, Typography } from 'antd'
import IdeaSubmitSummary from '@/components/idea/IdeaSubmitSummary'
import { PhoneBindingResultsTable } from '@/components/phone-binding/PhoneBindingResultsTable'
import { ApprovalUrlResultsTable } from '@/components/approval-url/ApprovalUrlResultsTable'
import {
  SPECIAL_OUTCOME_LABELS,
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
  meta_summary?: Record<string, unknown>
  detail?: TaskLogDetailPayload
}

type TaskDetailHeaderProps = {
  record: TaskDetailRecord
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function asArray(value: unknown): any[] {
  return Array.isArray(value) ? value : []
}

function numberValue(value: unknown): number {
  const n = Number(value || 0)
  return Number.isFinite(n) ? n : 0
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
  const detail = asRecord(record.detail)
  const errors = asArray(detail.errors)
  return {
    success: numberValue(record.success ?? detail.success),
    skipped: numberValue(record.skipped ?? detail.skipped),
    failed: numberValue(record.failed ?? errors.length),
  }
}

function renderStatsTags(record: TaskDetailRecord) {
  const stats = statsOf(record)
  const tags = [
    stats.success > 0 ? <Tag key="success" color="success">成功 {stats.success}</Tag> : null,
    stats.skipped > 0 ? <Tag key="skipped" color="warning">跳过 {stats.skipped}</Tag> : null,
    stats.failed > 0 ? <Tag key="failed" color="error">失败 {stats.failed}</Tag> : null,
  ].filter(Boolean)
  return tags.length > 0 ? <Space size={4} wrap>{tags}</Space> : <Text type="secondary">-</Text>
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

function GenericSummary({ record, meta, errors }: { record: TaskDetailRecord; meta: Record<string, unknown>; errors: any[] }) {
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
    || runtimeResults.some((item: any) => String(item?.status || '') === 'registered_phone_signup')

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
