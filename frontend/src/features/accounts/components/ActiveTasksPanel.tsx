import { useEffect, useMemo, useState, type CSSProperties } from 'react'
import { App, Button, Checkbox, Empty, Popover, Space, Tag, Tooltip } from 'antd'
import {
  DownOutlined,
  EyeOutlined,
  ReloadOutlined,
  StopOutlined,
} from '@ant-design/icons'
import {
  buildActiveTaskStopTargets,
  buildBatchStopRequest,
  failedBatchStopTargetKeys,
  type ActiveTaskSnapshot,
  type ActiveTaskStopMode,
  type BatchStopResponse,
  type BatchStopRequest,
} from '@/lib/activeTaskControls'
import { activeTaskLabel } from '@/lib/taskTypes'

type ActiveTasksPanelProps = {
  loading: boolean
  items: ActiveTaskSnapshot[]
  onRefresh: () => Promise<void> | void
  onOpenChange?: (open: boolean) => void
  onOpenTaskSnapshot: (snapshot: ActiveTaskSnapshot) => void
  onBatchStop: (request: BatchStopRequest) => Promise<BatchStopResponse>
  style?: CSSProperties
}

function taskIdOf(item: ActiveTaskSnapshot) {
  return String(item.id || item.task_id || '').trim()
}

export function ActiveTasksPanel({
  loading,
  items,
  onRefresh,
  onOpenChange,
  onOpenTaskSnapshot,
  onBatchStop,
  style,
}: ActiveTasksPanelProps) {
  const { message, modal } = App.useApp()
  const [open, setOpen] = useState(false)
  const [selectedKeys, setSelectedKeys] = useState<string[]>([])
  const [stoppingMode, setStoppingMode] = useState<ActiveTaskStopMode | ''>('')
  const targets = useMemo(() => buildActiveTaskStopTargets(items), [items])
  const targetByKey = useMemo(
    () => new Map(targets.map((target) => [target.key, target])),
    [targets],
  )
  const selectableTargets = useMemo(
    () => targets.filter((target) => target.stopMode !== 'immediate'),
    [targets],
  )
  const selectedTargets = useMemo(
    () => selectedKeys.flatMap((key) => {
      const target = targetByKey.get(key)
      return target ? [target] : []
    }),
    [selectedKeys, targetByKey],
  )
  const selectedTaskCount = useMemo(
    () => selectedTargets.reduce((count, target) => count + target.items.length, 0),
    [selectedTargets],
  )
  const selectedGroupCount = selectedTargets.filter(
    (target) => target.targetType === 'registration_domain_group',
  ).length
  const allSelected = selectableTargets.length > 0
    && selectableTargets.every((target) => selectedKeys.includes(target.key))
  const partiallySelected = selectedKeys.length > 0 && !allSelected
  const gracefulUnsupported = selectedTargets.some((target) => !target.supportsAfterCurrent)

  useEffect(() => {
    const selectableKeys = new Set(selectableTargets.map((target) => target.key))
    setSelectedKeys((previous) => previous.filter((key) => selectableKeys.has(key)))
  }, [selectableTargets])

  const setPanelOpen = (nextOpen: boolean) => {
    setOpen(nextOpen)
    onOpenChange?.(nextOpen)
    if (nextOpen) void onRefresh()
  }

  const openTaskLog = (snapshot: ActiveTaskSnapshot) => {
    setPanelOpen(false)
    onOpenTaskSnapshot(snapshot)
  }

  const toggleTarget = (key: string, checked: boolean) => {
    setSelectedKeys((previous) => {
      if (checked) return previous.includes(key) ? previous : [...previous, key]
      return previous.filter((item) => item !== key)
    })
  }

  const toggleAll = (checked: boolean) => {
    setSelectedKeys(checked ? selectableTargets.map((target) => target.key) : [])
  }

  const requestBatchStop = (mode: ActiveTaskStopMode) => {
    if (selectedTargets.length === 0 || stoppingMode) return
    const groupText = selectedGroupCount > 0
      ? `，其中 ${selectedGroupCount} 个自动轮换组会同时取消等待域名、技术重试和后续补位`
      : ''
    modal.confirm({
      title: mode === 'immediate'
        ? `立即停止所选 ${selectedTaskCount} 个运行任务？`
        : `完成当前执行单元后停止所选 ${selectedTaskCount} 个运行任务？`,
      content: mode === 'immediate'
        ? `系统会立即发送协作式中断请求${groupText}。任务会在运行单元完成清理后进入终态，已产生的日志仍会保留。`
        : `系统会停止投递新的账号、手机号或订单，让已经开始的执行单元正常收口${groupText}。`,
      okText: mode === 'immediate' ? '立即停止' : '确认停止',
      okButtonProps: { danger: mode === 'immediate' },
      cancelText: '取消',
      onOk: async () => {
        setStoppingMode(mode)
        try {
          const response = await onBatchStop(buildBatchStopRequest(selectedTargets, mode))
          const summary = response.summary || {}
          const accepted = Number(summary.accepted || 0)
          const alreadyRequested = Number(summary.already_requested || 0)
          const alreadyTerminal = Number(summary.already_terminal || 0)
          const notFound = Number(summary.not_found || 0)
          const failed = Number(summary.failed || 0)
          const handled = accepted + alreadyRequested
          if (failed > 0) {
            message.warning(
              `已处理 ${handled} 个停止目标，${failed} 个失败；失败项已保留勾选`,
            )
          } else {
            const raced = alreadyTerminal + notFound
            message.success(
              raced > 0
                ? `已发送 ${handled} 个停止请求；${raced} 个目标已结束或不再运行`
                : `已发送 ${handled} 个停止请求，正在等待任务收口`,
            )
          }
          const failedKeys = failedBatchStopTargetKeys(response)
          setSelectedKeys((previous) => previous.filter((key) => failedKeys.has(key)))
          await onRefresh()
        } catch (error: unknown) {
          const detail = error instanceof Error ? error.message : '批量停止请求失败'
          message.error(detail)
        } finally {
          setStoppingMode('')
        }
      },
    })
  }

  const content = (
    <div className="active-tasks-popover" aria-label="正在运行任务批量控制">
      <div className="active-tasks-popover-head">
        <Checkbox
          checked={allSelected}
          indeterminate={partiallySelected}
          disabled={selectableTargets.length === 0 || Boolean(stoppingMode)}
          onChange={(event) => toggleAll(event.target.checked)}
        >
          全选可停止任务
        </Checkbox>
        <Space size={4}>
          <Tag color={selectedTaskCount > 0 ? 'blue' : 'default'}>
            已选 {selectedTaskCount}
          </Tag>
          <Tooltip title="刷新运行任务">
            <Button
              type="text"
              size="small"
              icon={<ReloadOutlined spin={loading} />}
              aria-label="刷新运行任务"
              disabled={Boolean(stoppingMode)}
              onClick={() => void onRefresh()}
            />
          </Tooltip>
        </Space>
      </div>

      <div className="active-tasks-popover-list">
        {targets.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无运行中任务" />
        ) : targets.map((target) => {
          const checked = selectedKeys.includes(target.key)
          const stopRequested = target.stopMode === 'immediate'
          const isGroup = target.targetType === 'registration_domain_group'
          return (
            <div className="active-task-target" key={target.key}>
              <div className="active-task-target-head">
                <Checkbox
                  checked={checked}
                  disabled={stopRequested || Boolean(stoppingMode)}
                  aria-label={`选择停止 ${target.label}`}
                  onChange={(event) => toggleTarget(target.key, event.target.checked)}
                />
                <div className="active-task-target-title">
                  <span title={target.label}>{target.label}</span>
                  {target.stopMode === 'immediate' ? (
                    <Tag color="warning">立即停止中</Tag>
                  ) : target.stopMode === 'after_current' ? (
                    <Tag color="processing">收口中</Tag>
                  ) : null}
                </div>
                {!isGroup ? (
                  <Tooltip title="查看任务日志">
                    <Button
                      type="text"
                      size="small"
                      icon={<EyeOutlined />}
                      aria-label={`查看任务日志 ${taskIdOf(target.items[0])}`}
                      onClick={() => openTaskLog(target.items[0])}
                    />
                  </Tooltip>
                ) : null}
              </div>
              {isGroup ? (
                <div className="active-task-group-items">
                  {target.items.map((item) => (
                    <div className="active-task-group-item" key={taskIdOf(item)}>
                      <span title={activeTaskLabel(item)}>{activeTaskLabel(item)}</span>
                      <Tooltip title="查看子任务日志">
                        <Button
                          type="text"
                          size="small"
                          icon={<EyeOutlined />}
                          aria-label={`查看子任务日志 ${taskIdOf(item)}`}
                          onClick={() => openTaskLog(item)}
                        />
                      </Tooltip>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          )
        })}
      </div>

      <div className="active-tasks-popover-actions">
        <Tooltip
          title={gracefulUnsupported ? '所选任务中包含不支持“完成当前后停止”的任务' : undefined}
        >
          <span>
            <Button
              block
              disabled={selectedTargets.length === 0 || gracefulUnsupported || Boolean(stoppingMode)}
              loading={stoppingMode === 'after_current'}
              onClick={() => requestBatchStop('after_current')}
            >
              完成当前后停止（{selectedTaskCount}）
            </Button>
          </span>
        </Tooltip>
        <Button
          block
          danger
          icon={<StopOutlined />}
          disabled={selectedTargets.length === 0 || Boolean(stoppingMode)}
          loading={stoppingMode === 'immediate'}
          onClick={() => requestBatchStop('immediate')}
        >
          立即停止（{selectedTaskCount}）
        </Button>
      </div>
    </div>
  )

  return (
    <Popover
      trigger="click"
      placement="bottomRight"
      open={open}
      onOpenChange={setPanelOpen}
      content={content}
      arrow={false}
    >
      <Button
        style={{ minWidth: 180, ...style }}
        loading={loading && items.length === 0}
        icon={loading && items.length > 0 ? <ReloadOutlined spin /> : undefined}
      >
        {items.length > 0 ? `正在运行 ${items.length} 个任务` : '无运行中任务'}
        <DownOutlined />
      </Button>
    </Popover>
  )
}
