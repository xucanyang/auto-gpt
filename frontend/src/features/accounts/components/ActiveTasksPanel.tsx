import type { CSSProperties } from 'react'
import { Select } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { taskObjectSummary, taskSourceLabel } from '@/lib/taskTypes'

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
          label: `${taskSourceLabel(item?.source)} · ${item.progress || '-'} · ${taskObjectSummary(item?.meta, item?.email || item?.platform)}`,
        })),
      ]}
      suffixIcon={<ReloadOutlined spin={loading} onClick={() => void onRefresh()} />}
    />
  )
}
