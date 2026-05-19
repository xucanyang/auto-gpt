import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  message,
  Popconfirm,
  Segmented,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import type { TableColumnsType } from 'antd'
import { CopyOutlined, DeleteOutlined, EyeOutlined, ReloadOutlined } from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'

const { Text } = Typography

type TaskStatus = 'running' | 'success' | 'failed' | 'skipped' | 'stopped' | 'pending_activation'
type LogViewMode = 'info' | 'debug'

const LOG_VIEW_STORAGE_KEY = 'task-history-log-view-mode'

interface TaskLogItem {
  id: number
  task_id?: string
  created_at: string
  platform: string
  email: string
  status: TaskStatus
  error: string
  detail?: TaskLogDetailPayload
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
  attempt_outcome?: string
  email?: string
}

interface TaskLogDetailResponse extends TaskLogItem {
  detail?: TaskLogDetailPayload
}

function parseLogLine(rawLine: string) {
  const line = String(rawLine || '')
  const normalized = line.replace(/^\[\d{2}:\d{2}:\d{2}\]\s*/, '')
  const isDebug = normalized.startsWith('[DEBUG] ')
  const text = isDebug ? normalized.replace(/^\[DEBUG\]\s*/, '') : normalized
  return { raw: line, text, isDebug }
}

function statusTagColor(status: TaskStatus) {
  if (status === 'running') return 'processing'
  if (status === 'success') return 'success'
  if (status === 'pending_activation') return 'processing'
  if (status === 'skipped') return 'warning'
  if (status === 'stopped') return 'warning'
  return 'error'
}

function statusLabel(status: TaskStatus) {
  if (status === 'running') return '运行中'
  if (status === 'success') return '成功'
  if (status === 'pending_activation') return '待激活'
  if (status === 'skipped') return '跳过'
  if (status === 'stopped') return '已停止'
  return '失败'
}

function taskOutcomeLabel(outcome?: string) {
  switch (String(outcome || '').trim()) {
    case 'task_created':
      return '已创建'
    case 'success':
      return '成功'
    case 'success_skip_save':
      return '成功不保存'
    case 'invite_saved_pending_activation':
      return '待激活'
    case 'activation_success':
      return '激活成功'
    case 'activation_failed':
      return '激活失败'
    case 'resume_subscription_auth_success':
      return '补抓成功'
    case 'resume_subscription_auth_failed':
      return '补抓失败'
    case 'resume_subscription_auth_stopped':
      return '补抓停止'
    case 'resume_subscription_auth_skipped':
      return '补抓跳过'
    case 'batch_resume_subscription_auth_success':
      return '批量补抓成功'
    case 'batch_resume_subscription_auth_failed':
      return '批量补抓失败'
    case 'batch_resume_subscription_auth_stopped':
      return '批量补抓停止'
    case 'skipped':
      return '跳过'
    case 'failed':
      return '失败'
    case 'invite_exhausted_stop_phase':
      return '邀请耗尽'
    default:
      return String(outcome || '').trim() || '-'
  }
}

export default function TaskHistory() {
  const [logs, setLogs] = useState<TaskLogItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [selectedRowKeys, setSelectedRowKeys] = useState<number[]>([])
  const [detailOpen, setDetailOpen] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailRecord, setDetailRecord] = useState<TaskLogDetailResponse | null>(null)
  const [viewMode, setViewMode] = useState<LogViewMode>(() => {
    if (typeof window === 'undefined') return 'info'
    const saved = window.localStorage.getItem(LOG_VIEW_STORAGE_KEY)
    return saved === 'debug' ? 'debug' : 'info'
  })

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams({ page: '1', page_size: '50', platform: 'chatgpt' })
      const data = await apiFetch(`/tasks/logs?${params}`) as TaskLogListResponse
      setLogs(data.items || [])
      setTotal(data.total || 0)
      setSelectedRowKeys((prev) => prev.filter((key) => data.items.some((item) => item.id === key)))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    if (typeof window === 'undefined') return
    window.localStorage.setItem(LOG_VIEW_STORAGE_KEY, viewMode)
  }, [viewMode])

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

  const rawDetailLines = useMemo(
    () => (Array.isArray(detailRecord?.detail?.logs) ? detailRecord?.detail?.logs || [] : []),
    [detailRecord],
  )
  const parsedDetailLines = useMemo(() => rawDetailLines.map(parseLogLine), [rawDetailLines])
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

  const columns: TableColumnsType<TaskLogItem> = [
    {
      title: '时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 180,
      render: (text: string) => (text ? new Date(text).toLocaleString('zh-CN') : '-'),
    },
    {
      title: '任务 ID',
      dataIndex: 'task_id',
      key: 'task_id',
      width: 180,
      render: (text: string) => <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{text || '-'}</span>,
    },
    {
      title: '平台',
      dataIndex: 'platform',
      key: 'platform',
      width: 100,
      render: (text: string) => <Tag>{text}</Tag>,
    },
    {
      title: '邮箱',
      dataIndex: 'email',
      key: 'email',
      render: (text: string) => <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{text || '-'}</span>,
    },
    {
      title: '结果',
      key: 'attempt_outcome',
      width: 120,
      render: (_, record) => {
        const outcome = record.detail?.attempt_outcome
        return <Tag>{taskOutcomeLabel(outcome)}</Tag>
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (status: TaskStatus) => <Tag color={statusTagColor(status)}>{statusLabel(status)}</Tag>,
    },
    {
      title: '进度',
      key: 'progress',
      width: 100,
      render: (_, record) => record.detail?.progress || '-',
    },
    {
      title: '统计',
      key: 'counts',
      width: 150,
      render: (_, record) => {
        const success = Number(record.detail?.success || 0)
        const skipped = Number(record.detail?.skipped || 0)
        const failed = Array.isArray(record.detail?.errors) ? record.detail?.errors.length : 0
        return `${success}/${skipped}/${failed}`
      },
    },
    {
      title: '错误信息',
      dataIndex: 'error',
      key: 'error',
      render: (text: string) => text || '-',
    },
    {
      title: '操作',
      key: 'actions',
      width: 110,
      render: (_, record) => (
        <Button size="small" icon={<EyeOutlined />} onClick={() => void openDetail(record)}>
          查看日志
        </Button>
      ),
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <h1 style={{ fontSize: 24, fontWeight: 'bold', margin: 0 }}>任务历史</h1>
          <p style={{ color: '#7a8ba3', marginTop: 4 }}>注册任务执行记录</p>
        </div>
        <Space>
          <Text type="secondary">{total} 条记录</Text>
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
        <Table
          rowKey="id"
          columns={columns}
          dataSource={logs}
          loading={loading}
          rowSelection={{
            selectedRowKeys,
            onChange: (keys) => setSelectedRowKeys(keys as number[]),
          }}
          pagination={{ pageSize: 20, showSizeChanger: false }}
        />
      </Card>

      <Drawer
        title={detailRecord ? `任务日志 #${detailRecord.id}` : '任务日志'}
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        width={920}
        destroyOnClose={false}
      >
        {detailRecord && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <Descriptions bordered size="small" column={2}>
              <Descriptions.Item label="平台">{detailRecord.platform || '-'}</Descriptions.Item>
              <Descriptions.Item label="状态">
                <Tag color={statusTagColor(detailRecord.status)}>{statusLabel(detailRecord.status)}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="邮箱">{detailRecord.email || '-'}</Descriptions.Item>
              <Descriptions.Item label="时间">
                {detailRecord.created_at ? new Date(detailRecord.created_at).toLocaleString('zh-CN') : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="任务 ID">{detailRecord.detail?.task_id || '-'}</Descriptions.Item>
              <Descriptions.Item label="结果">{taskOutcomeLabel(detailRecord.detail?.attempt_outcome)}</Descriptions.Item>
              <Descriptions.Item label="进度">{detailRecord.detail?.progress || '-'}</Descriptions.Item>
              <Descriptions.Item label="成功">{detailRecord.detail?.success ?? 0}</Descriptions.Item>
              <Descriptions.Item label="跳过">{detailRecord.detail?.skipped ?? 0}</Descriptions.Item>
              <Descriptions.Item label="失败">{Array.isArray(detailRecord.detail?.errors) ? detailRecord.detail?.errors.length : 0}</Descriptions.Item>
              <Descriptions.Item label="错误信息" span={2}>{detailRecord.error || '-'}</Descriptions.Item>
            </Descriptions>

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
                <Empty description="这条历史记录还没有持久化完整日志" image={Empty.PRESENTED_IMAGE_SIMPLE} />
              ) : visibleDetailLines.length === 0 ? (
                <Text type="secondary">当前 Info 视图下没有可显示的日志</Text>
              ) : (
                visibleDetailLines.map((line, index) => (
                  <div
                    key={`${index}-${line.raw}`}
                    style={{
                      lineHeight: 1.6,
                      color: line.isDebug
                        ? '#6b7280'
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
          </div>
        )}
      </Drawer>
    </div>
  )
}
