import type { CSSProperties } from 'react'
import { Select } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'

type ActiveTasksPanelProps = {
  loading: boolean
  items: any[]
  onRefresh: () => Promise<void> | void
  onOpen?: () => void
  onOpenTaskSnapshot: (snapshot: any) => void
  style?: CSSProperties
}

export function ActiveTasksPanel({
  loading,
  items,
  onRefresh,
  onOpen,
  onOpenTaskSnapshot,
  style,
}: ActiveTasksPanelProps) {
  const formatSourceLabel = (item: any) => {
    const source = String(item?.source || '').trim()
    if (source === 'resume_subscription_auth') return '补抓Auth'
    if (source === 'batch_resume_subscription_auth') return '批量补抓Auth'
    if (source === 'phone_binding_test') return '号码绑定测试'
    if (source === 'invalid_recheck') return '失效测活'
    if (source === 'batch_invalid_recheck') return '批量失效测活'
    if (source === 'batch_payment_link') return '批量订阅链接'
    if (source === 'gopay_payment') return 'GoPay支付'
    return source || 'task'
  }

  const formatMetaLabel = (item: any) => {
    if (item?.meta?.email) return String(item.meta.email)
    const phoneCount = Number(item?.meta?.phone_count || 0)
    if (phoneCount > 0) return `${phoneCount} 个号码`
    const eligible = Number(item?.meta?.eligible || 0)
    if (eligible > 0) return `${eligible} 个账号`
    return String(item?.platform || '')
  }

  return (
    <Select
      value=""
      loading={loading}
      style={{ minWidth: 180, ...style }}
      placeholder="正在运行任务"
      onDropdownVisibleChange={(open) => {
        if (open) {
          onOpen?.()
          void onRefresh()
        }
      }}
      onChange={(value) => {
        const snapshot = items.find((item: any) => String(item?.id || item?.task_id || '') === String(value))
        if (snapshot) onOpenTaskSnapshot(snapshot)
      }}
      options={[
        { value: '', label: items.length ? `正在运行 ${items.length} 个任务` : '无运行中任务', disabled: true },
        ...items.map((item: any) => ({
          value: String(item.id || item.task_id || ''),
          label: `${formatSourceLabel(item)} · ${item.progress || '-'} · ${formatMetaLabel(item)}`,
        })),
      ]}
      suffixIcon={<ReloadOutlined spin={loading} onClick={() => void onRefresh()} />}
    />
  )
}
