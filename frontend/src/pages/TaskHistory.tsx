import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Drawer,
  Empty,
  Grid,
  message,
  Pagination,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
  Timeline,
  Tooltip,
  Typography,
} from 'antd'
import type { TableColumnsType } from 'antd'
import { CopyOutlined, DeleteOutlined, EyeOutlined, ReloadOutlined } from '@ant-design/icons'
import { TaskDetailHeader } from '@/components/task-detail/TaskDetailHeader'
import {
  SPECIAL_OUTCOME_LABELS,
  TASK_SOURCE_OPTIONS,
  deriveTaskStats,
  statusLabel,
  statusTagColor,
  taskObjectSummary,
  taskSourceDisplayLabel,
} from '@/lib/taskTypes'
import { apiFetch } from '@/lib/utils'
import { formatBeijingDateTime } from '@/lib/dateTime'

const { Text } = Typography

type TaskStatus = 'running' | 'success' | 'failed' | 'skipped' | 'stopped' | 'partial' | 'interrupted' | 'pending_activation'
type LogViewMode = 'info' | 'debug'
type StatusFilter = 'all' | TaskStatus

const LOG_VIEW_STORAGE_KEY = 'task-history-log-view-mode'

interface TaskLogItem {
  id: number
  task_id?: string
  created_at: string
  platform: string
  email: string
  status: TaskStatus
  error: string
  source?: string
  attempt_outcome?: string
  progress?: string
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

interface TaskLogListResponse {
  total: number
  items: TaskLogItem[]
}

interface TaskLogBatchDeleteResponse {
  deleted: number
  not_found: number[]
  total_requested: number
}

interface TaskLogDetailPayload {
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
  action_logs?: string[]
  log_lines?: string[]
  runtime_logs?: string[]
  logs_truncated?: boolean
  attempt_outcome?: string
  email?: string
  payment_events?: Array<{
    id?: number
    account_id?: number
    account?: string
    stage?: string
    level?: string
    message?: string
    created_at?: string
  }>
  [key: string]: unknown
}

interface TaskLogDetailResponse extends TaskLogItem {
  detail?: TaskLogDetailPayload
}

function parseLogLine(rawLine: string) {
  const line = String(rawLine || '')
  const normalized = line.replace(/^\[\d{2}:\d{2}:\d{2}\]\s*/, '')
  const isDebug = /^\[[^\]]*DEBUG[^\]]*\]/i.test(normalized)
  const text = isDebug ? normalized.replace(/^\[[^\]]*DEBUG[^\]]*\]\s*/i, '') : normalized
  return { raw: line, text, isDebug }
}

function sourceOf(record?: TaskLogItem | TaskLogDetailResponse | null) {
  const detail = record?.detail
  const meta = detail?.meta && typeof detail.meta === 'object' ? detail.meta : {}
  return String(record?.source || detail?.source || meta.source || '').trim()
}

function sourceMetaOf(record?: TaskLogItem | TaskLogDetailResponse | null): Record<string, unknown> {
  const detailMeta = record?.detail?.meta
  if (detailMeta && typeof detailMeta === 'object') return detailMeta
  const summary = record?.meta_summary
  return summary && typeof summary === 'object' ? summary : {}
}

function outcomeOf(record?: TaskLogItem | TaskLogDetailResponse | null) {
  return String(record?.attempt_outcome || record?.detail?.attempt_outcome || '').trim()
}

function countStats(record: TaskLogItem | TaskLogDetailResponse) {
  return deriveTaskStats(record)
}

function renderStatsTags(record: TaskLogItem | TaskLogDetailResponse) {
  const stats = countStats(record)
  if (!stats.known) return <Text type="secondary">统计暂不可用</Text>
  const tags = [
    <Tag key="success" color={stats.success > 0 ? 'success' : undefined}>成功 {stats.success}</Tag>,
    <Tag key="skipped" color={stats.skipped > 0 ? 'warning' : undefined}>跳过 {stats.skipped}</Tag>,
    <Tag key="failed" color={stats.failed > 0 ? 'error' : undefined}>失败 {stats.failed}</Tag>,
    stats.interrupted > 0 ? <Tag key="interrupted" color="warning">中断 {stats.interrupted}</Tag> : null,
  ].filter(Boolean)
  return <Space size={4} wrap>{tags}</Space>
}

function renderStatus(record: TaskLogItem | TaskLogDetailResponse) {
  const outcome = outcomeOf(record)
  return (
    <Space size={4} wrap>
      <Tag color={statusTagColor(record.status)}>{statusLabel(record.status)}</Tag>
      {SPECIAL_OUTCOME_LABELS[outcome] ? <Tag>{SPECIAL_OUTCOME_LABELS[outcome]}</Tag> : null}
    </Space>
  )
}

function renderSourceTag(record: TaskLogItem | TaskLogDetailResponse) {
  const source = sourceOf(record)
  const label = taskSourceDisplayLabel(source, sourceMetaOf(record))
  return (
    <Tooltip title={label === '其他任务' && source ? `内部来源：${source}` : undefined}>
      <Tag color="blue">{label}</Tag>
    </Tooltip>
  )
}

export default function TaskHistory() {
  const screens = Grid.useBreakpoint()
  const isMobile = screens.lg === false
  const [logs, setLogs] = useState<TaskLogItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [mobilePage, setMobilePage] = useState(1)
  const [selectedRowKeys, setSelectedRowKeys] = useState<number[]>([])
  const [detailOpen, setDetailOpen] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailRecord, setDetailRecord] = useState<TaskLogDetailResponse | null>(null)
  const [sourceFilter, setSourceFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [viewMode, setViewMode] = useState<LogViewMode>(() => {
    if (typeof window === 'undefined') return 'info'
    const saved = window.localStorage.getItem(LOG_VIEW_STORAGE_KEY)
    return saved === 'debug' ? 'debug' : 'info'
  })

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams({ page: '1', page_size: '50', platform: 'chatgpt' })
      if (sourceFilter) params.set('source', sourceFilter)
      const data = await apiFetch(`/tasks/logs?${params}`) as TaskLogListResponse
      const items = data.items || []
      setLogs(items)
      setTotal(data.total || 0)
      setSelectedRowKeys((prev) => prev.filter((key) => items.some((item) => item.id === key)))
      setMobilePage(1)
    } finally {
      setLoading(false)
    }
  }, [sourceFilter])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    if (typeof window === 'undefined') return
    window.localStorage.setItem(LOG_VIEW_STORAGE_KEY, viewMode)
  }, [viewMode])

  const filteredLogs = useMemo(() => {
    if (statusFilter === 'all') return logs
    return logs.filter((item) => {
      if (item.status === statusFilter) return true
      return statusFilter === 'pending_activation' && outcomeOf(item) === 'invite_saved_pending_activation'
    })
  }, [logs, statusFilter])

  const handleBatchDelete = async () => {
    if (selectedRowKeys.length === 0) return

    const result = await apiFetch('/tasks/logs/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ ids: selectedRowKeys }),
    }) as TaskLogBatchDeleteResponse

    message.success(`已删除 ${result.deleted} 条任务历史`)
    if (result.not_found.length > 0) {
      message.warning(`${result.not_found.length} 条记录不存在或已被删除`)
    }
    setSelectedRowKeys([])
    await load()
  }

  const openDetail = async (record: TaskLogItem) => {
    setDetailLoading(true)
    setDetailOpen(true)
    try {
      const data = await apiFetch(`/tasks/logs/${record.id}`) as TaskLogDetailResponse
      setDetailRecord(data)
    } catch (error) {
      const detail = error instanceof Error ? error.message : '获取日志详情失败'
      message.error(detail)
      setDetailRecord(null)
    } finally {
      setDetailLoading(false)
    }
  }

  const rawDetailLines = useMemo(() => {
    const detail = detailRecord?.detail
    if (!detail) return []
    for (const key of ['logs', 'action_logs', 'log_lines', 'runtime_logs'] as const) {
      if (Array.isArray(detail[key]) && detail[key].length > 0) return detail[key].map((line) => String(line || ''))
    }
    // A few pre-snapshot interrupted rows only retained the terminal error.
    // Show it as a synthetic line instead of presenting an empty drawer.
    const errors = Array.isArray(detail.errors) ? detail.errors : []
    if (errors.length > 0) return errors.map((error) => `[ERROR] ${String(error || '')}`)
    if (detailRecord.error) return [`[ERROR] ${detailRecord.error}`]
    const status = String(detailRecord.status || detail.status_snapshot || '').trim().toLowerCase()
    const progress = String(detail.progress || '').trim()
    if (status === 'interrupted') return [`[INTERRUPTED] 任务已中断${progress ? `；进度 ${progress}` : ''}；当前历史记录未保存可见日志`]
    if (status === 'stopped') return [`[STOPPED] 任务已停止${progress ? `；进度 ${progress}` : ''}；当前历史记录未保存可见日志`]
    return []
  }, [detailRecord])
  const parsedDetailLines = useMemo(() => rawDetailLines.map(parseLogLine), [rawDetailLines])
  const paymentEvents = useMemo(
    () => Array.isArray(detailRecord?.detail?.payment_events)
      ? detailRecord.detail.payment_events
      : [],
    [detailRecord],
  )
  const visibleDetailLines = useMemo(
    () => (viewMode === 'debug' ? parsedDetailLines : parsedDetailLines.filter((line) => !line.isDebug)),
    [parsedDetailLines, viewMode],
  )

  const handleCopyDetail = async () => {
    try {
      await navigator.clipboard.writeText(visibleDetailLines.map((line) => line.raw).join('\n'))
      message.success('日志已复制')
    } catch {
      message.error('复制失败')
    }
  }

  const mobilePageSize = 20
  const mobileLogs = useMemo(() => {
    const start = (mobilePage - 1) * mobilePageSize
    return filteredLogs.slice(start, start + mobilePageSize)
  }, [filteredLogs, mobilePage])

  useEffect(() => {
    const maxPage = Math.max(1, Math.ceil(filteredLogs.length / mobilePageSize))
    if (mobilePage > maxPage) setMobilePage(maxPage)
  }, [filteredLogs.length, mobilePage])

  const columns: TableColumnsType<TaskLogItem> = [
    {
      title: '时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 160,
      render: (text: string) => formatBeijingDateTime(text),
    },
    {
      title: '任务类型',
      key: 'source',
      width: 130,
      render: (_, record) => renderSourceTag(record),
    },
    {
      title: '对象',
      key: 'object',
      width: 200,
      render: (_, record) => <Text ellipsis={{ tooltip: taskObjectSummary(record.meta_summary, record.email) }}>{taskObjectSummary(record.meta_summary, record.email)}</Text>,
    },
    {
      title: '状态',
      key: 'status',
      width: 150,
      render: (_, record) => renderStatus(record),
    },
    {
      title: '统计',
      key: 'counts',
      width: 180,
      render: (_, record) => renderStatsTags(record),
    },
    {
      title: '错误信息',
      dataIndex: 'error',
      key: 'error',
      render: (text: string) => text ? <Tooltip title={text}><Text ellipsis style={{ maxWidth: 260 }}>{text}</Text></Tooltip> : '-',
    },
    {
      title: '操作',
      key: 'actions',
      width: 120,
      render: (_, record) => (
        <Button size="small" icon={<EyeOutlined />} onClick={() => void openDetail(record)}>
          查看详情
        </Button>
      ),
    },
  ]

  const renderMobileCards = () => {
    if (mobileLogs.length === 0) {
      return <Empty description={loading ? '正在加载任务历史' : '暂无任务历史'} image={Empty.PRESENTED_IMAGE_SIMPLE} />
    }

    return (
      <div className="mobile-card-list">
        {mobileLogs.map((record) => {
          const selected = selectedRowKeys.some((key) => Number(key) === Number(record.id))
          const objectSummary = taskObjectSummary(record.meta_summary, record.email)

          return (
            <Card key={record.id} size="small" className="mobile-record-card">
              <div className="mobile-record-head">
                <Checkbox
                  checked={selected}
                  onChange={(event) => {
                    setSelectedRowKeys((prev) => {
                      if (event.target.checked) return Array.from(new Set([...prev.map((key) => Number(key)), Number(record.id)]))
                      return prev.filter((key) => Number(key) !== Number(record.id))
                    })
                  }}
                />
                <div className="mobile-record-main">
                  <Typography.Text className="mobile-record-title" strong>
                    {taskSourceDisplayLabel(sourceOf(record), sourceMetaOf(record))} · {objectSummary}
                  </Typography.Text>
                  <div className="mobile-record-meta">
                    {renderStatus(record)}
                    {renderStatsTags(record)}
                    <Text type="secondary">{formatBeijingDateTime(record.created_at)}</Text>
                  </div>
                </div>
              </div>

              {record.error ? (
                <Alert style={{ marginTop: 10 }} type="warning" showIcon message={record.error} />
              ) : null}

              <div className="mobile-record-actions">
                <Button size="small" icon={<EyeOutlined />} onClick={() => void openDetail(record)}>
                  查看详情
                </Button>
              </div>
            </Card>
          )
        })}
      </div>
    )
  }

  const drawerTitle = detailRecord
    ? taskSourceDisplayLabel(sourceOf(detailRecord), sourceMetaOf(detailRecord))
    : '任务详情'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <div>
          <h1 style={{ fontSize: 24, fontWeight: 'bold', margin: 0 }}>任务历史</h1>
          <p style={{ color: '#7a8ba3', marginTop: 4 }}>注册任务执行记录</p>
        </div>
        <Space wrap>
          <Select
            value={sourceFilter}
            style={{ width: 180 }}
            options={[{ value: '', label: '全部任务类型' }, ...TASK_SOURCE_OPTIONS]}
            onChange={(value) => setSourceFilter(value)}
          />
          <Select<StatusFilter>
            value={statusFilter}
            style={{ width: 140 }}
            options={[
              { value: 'all', label: '全部状态' },
              { value: 'running', label: '运行中' },
              { value: 'success', label: '成功' },
              { value: 'failed', label: '失败' },
              { value: 'partial', label: '部分失败' },
              { value: 'interrupted', label: '远端中断' },
              { value: 'stopped', label: '已停止' },
              { value: 'skipped', label: '跳过' },
              { value: 'pending_activation', label: '历史待激活（已退役）' },
            ]}
            onChange={(value) => setStatusFilter(value)}
          />
          <Text type="secondary">{filteredLogs.length === logs.length ? total : `${filteredLogs.length}/${total}`} 条记录</Text>
          {selectedRowKeys.length > 0 && <Text type="success">已选 {selectedRowKeys.length} 条</Text>}
          {selectedRowKeys.length > 0 && (
            <Popconfirm
              title={`确认删除选中的 ${selectedRowKeys.length} 条任务历史？`}
              onConfirm={handleBatchDelete}
            >
              <Button danger icon={<DeleteOutlined />}>
                删除 {selectedRowKeys.length} 条
              </Button>
            </Popconfirm>
          )}
          <Button icon={<ReloadOutlined spin={loading} />} onClick={load} loading={loading} />
        </Space>
      </div>

      <Card>
        {isMobile ? (
          <>
            {renderMobileCards()}
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 12 }}>
              <Pagination
                size="small"
                current={mobilePage}
                pageSize={mobilePageSize}
                total={filteredLogs.length}
                showSizeChanger={false}
                onChange={setMobilePage}
              />
            </div>
          </>
        ) : (
          <Table
            rowKey="id"
            columns={columns}
            dataSource={filteredLogs}
            loading={loading}
            rowSelection={{
              selectedRowKeys,
              onChange: (keys) => setSelectedRowKeys(keys as number[]),
            }}
            pagination={{ pageSize: 20, showSizeChanger: false }}
          />
        )}
      </Card>

      <Drawer
        title={drawerTitle}
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        width={920}
        destroyOnClose={false}
      >
        {detailRecord ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <TaskDetailHeader record={detailRecord} />

            {paymentEvents.length > 0 ? (
              <Card size="small" title={`PayPal 自动支付时间线 · ${paymentEvents.length} 条`}>
                <Timeline
                  items={paymentEvents.map((event, index) => ({
                    key: `${event.id || index}-${event.stage || 'payment'}`,
                    color: String(event.level || '').toLowerCase() === 'warning' ? 'orange' : 'blue',
                    children: (
                      <Space size={[6, 2]} wrap>
                        <Tag>{String(event.stage || 'payment')}</Tag>
                        {event.created_at ? <Text type="secondary">{event.created_at}</Text> : null}
                        {event.account ? <Text type="secondary">{event.account}</Text> : null}
                        <Text>{String(event.message || '')}</Text>
                      </Space>
                    ),
                  }))}
                />
              </Card>
            ) : null}

            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <Space>
                <Text strong>日志详情</Text>
                <Text type="secondary">{rawDetailLines.length} 行</Text>
              </Space>
              <Space>
                <Segmented
                  size="small"
                  value={viewMode}
                  onChange={(value) => setViewMode(value as LogViewMode)}
                  options={[
                    { label: 'Info', value: 'info' },
                    { label: 'Debug', value: 'debug' },
                  ]}
                />
                <Button size="small" icon={<CopyOutlined />} onClick={handleCopyDetail} disabled={visibleDetailLines.length === 0}>
                  复制日志
                </Button>
              </Space>
            </div>

            <div
              style={{
                maxHeight: '62vh',
                overflow: 'auto',
                background: '#fafafa',
                border: '1px solid #f0f0f0',
                borderRadius: 8,
                padding: 12,
                fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
                fontSize: 12,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}
            >
              {detailLoading ? (
                <Text type="secondary">正在加载日志...</Text>
              ) : rawDetailLines.length === 0 ? (
                <Empty description="这条历史记录没有可显示的日志" image={Empty.PRESENTED_IMAGE_SIMPLE} />
              ) : visibleDetailLines.length === 0 ? (
                <Text type="secondary">当前 {viewMode === 'debug' ? 'Debug' : 'Info'} 视图下没有可显示的日志</Text>
              ) : (
                visibleDetailLines.map((line, index) => (
                  <div
                    key={`${index}-${line.raw}`}
                    style={{
                      lineHeight: 1.6,
                      margin: line.isDebug ? '2px 0' : 0,
                      padding: line.isDebug ? '2px 8px' : 0,
                      borderLeft: line.isDebug ? '3px solid #8b5cf6' : '3px solid transparent',
                      borderRadius: line.isDebug ? 4 : 0,
                      background: line.isDebug ? '#f5f3ff' : 'transparent',
                      color: line.isDebug
                        ? '#5b21b6'
                        : line.text.includes('✓') || line.text.includes('成功')
                          ? '#059669'
                          : line.text.includes('✗') || line.text.includes('失败') || line.text.includes('错误')
                            ? '#dc2626'
                            : line.text.includes('停止') || line.text.includes('跳过')
                              ? '#d97706'
                              : '#1f2937',
                    }}
                  >
                    {line.raw}
                  </div>
                ))
              )}
            </div>
            {detailRecord.detail?.logs_truncated ? (
              <Alert
                type="warning"
                showIcon
                message="日志窗口已裁剪"
                description={`仅保留最近 ${rawDetailLines.length} 行；更早日志已丢弃。`}
              />
            ) : null}
          </div>
        ) : detailLoading ? (
          <Text type="secondary">正在加载详情...</Text>
        ) : null}
      </Drawer>
    </div>
  )
}
