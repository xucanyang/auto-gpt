import type { CSSProperties } from 'react'
import { Select } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { activeTaskLabel } from '@/lib/taskTypes'

type ActiveTaskSnapshot = {
  id?: string | number
  task_id?: string | number
  source?: string
  progress?: string | number
  meta?: Record<string, unknown>
  email?: string
  platform?: string
  [key: string]: unknown
}

type ActiveTasksPanelProps = {
  loading: boolean
  items: ActiveTaskSnapshot[]
  onRefresh: () => Promise<void> | void
  onOpen?: () => void
  onOpenTaskSnapshot: (snapshot: ActiveTaskSnapshot) => void
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
        const snapshot = items.find((item) => String(item?.id || item?.task_id || '') === String(value))
        if (snapshot) onOpenTaskSnapshot(snapshot)
      }}
      options={[
        { value: '', label: items.length ? `正在运行 ${items.length} 个任务` : '无运行中任务', disabled: true },
        ...items.map((item) => ({
          value: String(item.id || item.task_id || ''),
          label: activeTaskLabel(item),
        })),
      ]}
      suffixIcon={<ReloadOutlined spin={loading} onClick={() => void onRefresh()} />}
    />
  )
}
