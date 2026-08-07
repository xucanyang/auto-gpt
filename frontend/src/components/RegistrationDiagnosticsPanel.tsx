import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Button,
  Dropdown,
  Empty,
  Popconfirm,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
  theme,
} from 'antd'
import type { MenuProps } from 'antd'
import {
  BugOutlined,
  DeleteOutlined,
  DownloadOutlined,
  FileSearchOutlined,
  PushpinFilled,
  PushpinOutlined,
  ReloadOutlined,
} from '@ant-design/icons'

import { registrationDiagnosticsModeLabel } from '@/lib/registrationDiagnostics'
import { apiFetch, getToken } from '@/lib/utils'

type DiagnosticFile = { size_bytes?: number }

type DiagnosticArtifact = {
  id: number
  task_id: string
  attempt_id: number
  attempt_number: number
  mode: string
  outcome: string
  failure_code: string
  failure_stage: string
  status: string
  email_masked: string
  size_bytes: number
  pinned: boolean
  truncation_reason: string
  created_at: string
  finished_at: string
  files?: Record<string, DiagnosticFile>
  summary?: { warnings?: string[] }
}

type DiagnosticsResponse = {
  items?: DiagnosticArtifact[]
  summary?: {
    artifact_count?: number
    recording_count?: number
    failure_count?: number
    success_count?: number
    total_bytes?: number
  }
}

interface RegistrationDiagnosticsPanelProps {
  taskId: string
  mode: string
  active: boolean
}

function formatBytes(value: unknown): string {
  const bytes = Number(value || 0)
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  const units = ['B', 'KiB', 'MiB', 'GiB']
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  const amount = bytes / (1024 ** index)
  return `${amount >= 100 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`
}

function outcomeTag(item: DiagnosticArtifact) {
  if (item.status === 'recording') return <Tag color="processing">采集中</Tag>
  if (item.status === 'truncated') return <Tooltip title={item.truncation_reason || '部分大文件已按单次配额移除'}><Tag color="warning">已截断</Tag></Tooltip>
  if (item.status === 'finalize_failed') return <Tooltip title={item.truncation_reason || '诊断制品收口未完整完成'}><Tag color="error">收口异常</Tag></Tooltip>
  if (item.status === 'skipped') return <Tag>未采集</Tag>
  if (item.status === 'pruned') return <Tooltip title={item.truncation_reason || '已按保留策略清理'}><Tag>已清理</Tag></Tooltip>
  if (item.outcome === 'success') return <Tag color="success">成功样本</Tag>
  if (item.outcome === 'failed') return <Tag color="error">失败样本</Tag>
  if (item.outcome === 'stopped' || item.outcome === 'interrupted') return <Tag color="warning">中断样本</Tag>
  return <Tag>{item.outcome || item.status || '未知'}</Tag>
}

function filenameFromDisposition(header: string | null, fallback: string): string {
  const match = String(header || '').match(/filename\*?=(?:UTF-8''|"?)([^";]+)/i)
  if (!match?.[1]) return fallback
  try {
    return decodeURIComponent(match[1].replace(/^"|"$/g, ''))
  } catch {
    return fallback
  }
}

export function RegistrationDiagnosticsPanel({ taskId, mode, active }: RegistrationDiagnosticsPanelProps) {
  const { token } = theme.useToken()
  const [items, setItems] = useState<DiagnosticArtifact[]>([])
  const [summary, setSummary] = useState<DiagnosticsResponse['summary']>({})
  const [loading, setLoading] = useState(false)
  const [actionId, setActionId] = useState(0)

  const load = useCallback(async (silent = false) => {
    if (!taskId) return
    if (!silent) setLoading(true)
    try {
      const response = await apiFetch(`/tasks/${taskId}/diagnostics`) as DiagnosticsResponse
      setItems(Array.isArray(response.items) ? response.items : [])
      setSummary(response.summary || {})
    } catch (error: unknown) {
      if (!silent) message.error(error instanceof Error ? error.message : '读取注册诊断失败')
    } finally {
      if (!silent) setLoading(false)
    }
  }, [taskId])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => void load(true), 8000)
    return () => window.clearInterval(timer)
  }, [active, load])

  const download = useCallback(async (item: DiagnosticArtifact, filename = '') => {
    setActionId(item.id)
    try {
      const suffix = filename ? `/files/${encodeURIComponent(filename)}` : '/download'
      const response = await fetch(`/api/tasks/${taskId}/diagnostics/${item.id}${suffix}`, {
        headers: { Authorization: `Bearer ${getToken()}` },
      })
      if (!response.ok) {
        const payload = await response.json().catch(() => null)
        throw new Error(payload?.detail || `下载失败 (${response.status})`)
      }
      const blob = await response.blob()
      const fallback = filename || `registration-diagnostic-${taskId}-attempt-${item.attempt_number}.zip`
      const downloadName = filenameFromDisposition(response.headers.get('content-disposition'), fallback)
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = downloadName
      anchor.style.display = 'none'
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (error: unknown) {
      message.error(error instanceof Error ? error.message : '下载注册诊断失败')
    } finally {
      setActionId(0)
    }
  }, [taskId])

  const togglePinned = useCallback(async (item: DiagnosticArtifact) => {
    setActionId(item.id)
    try {
      await apiFetch(`/tasks/${taskId}/diagnostics/${item.id}/pin`, {
        method: 'POST',
        body: JSON.stringify({ pinned: !item.pinned }),
      })
      await load(true)
      message.success(item.pinned ? '已取消固定保留' : '已固定保留')
    } catch (error: unknown) {
      message.error(error instanceof Error ? error.message : '更新保留状态失败')
    } finally {
      setActionId(0)
    }
  }, [load, taskId])

  const remove = useCallback(async (item: DiagnosticArtifact) => {
    setActionId(item.id)
    try {
      await apiFetch(`/tasks/${taskId}/diagnostics/${item.id}`, { method: 'DELETE' })
      await load(true)
      message.success('诊断包已删除')
    } catch (error: unknown) {
      message.error(error instanceof Error ? error.message : '删除诊断包失败')
    } finally {
      setActionId(0)
    }
  }, [load, taskId])

  const columns = useMemo(() => [
    {
      title: '尝试',
      key: 'attempt',
      width: 92,
      render: (_: unknown, item: DiagnosticArtifact) => (
        <Typography.Text code>#{item.attempt_number || item.attempt_id}</Typography.Text>
      ),
    },
    {
      title: '结果',
      key: 'outcome',
      width: 112,
      render: (_: unknown, item: DiagnosticArtifact) => outcomeTag(item),
    },
    {
      title: '阶段 / 诊断',
      key: 'diagnosis',
      width: 240,
      render: (_: unknown, item: DiagnosticArtifact) => (
        <Space direction="vertical" size={1} style={{ maxWidth: 420 }}>
          <Typography.Text>{item.failure_stage || (item.outcome === 'success' ? 'completed' : '-')}</Typography.Text>
          {item.failure_code ? <Typography.Text type="secondary" ellipsis={{ tooltip: item.failure_code }}>{item.failure_code}</Typography.Text> : null}
        </Space>
      ),
    },
    {
      title: '账号',
      dataIndex: 'email_masked',
      width: 190,
      ellipsis: true,
      render: (value: string) => value || '-',
    },
    {
      title: '大小',
      dataIndex: 'size_bytes',
      width: 90,
      render: formatBytes,
    },
    {
      title: '制品',
      key: 'files',
      width: 180,
      render: (_: unknown, item: DiagnosticArtifact) => {
        const files = item.files || {}
        const hasBrowserHar = Boolean(files['network.har.zip'])
        const hasProtocolHar = Boolean(files['protocol.har.zip'])
        const videoUnavailable = item.mode === 'full'
          && !files['video.webm']
          && !files['video.zip']
          && (item.summary?.warnings || []).some((warning) => warning.startsWith('video_capture_unavailable:'))
        return (
          <Space size={[4, 4]} wrap>
            {files['trace.zip'] ? <Tag>Trace</Tag> : null}
            {hasBrowserHar ? <Tag>HAR</Tag> : null}
            {hasProtocolHar ? <Tag>协议 HAR</Tag> : null}
            {files['video.webm'] || files['video.zip'] ? <Tag>视频</Tag> : null}
            {videoUnavailable ? <Tooltip title="当前浏览器运行时不支持原生视频，Trace 与 HAR 已正常保留"><Tag>视频不可用</Tag></Tooltip> : null}
            {files['final-page.png'] ? <Tag>现场</Tag> : null}
          </Space>
        )
      },
    },
    {
      title: '',
      key: 'actions',
      fixed: 'right' as const,
      width: 150,
      render: (_: unknown, item: DiagnosticArtifact) => {
        const availableFiles = Object.keys(item.files || {}).filter((name) => (
          ['trace.zip', 'video.webm', 'video.zip', 'diagnosis.json'].includes(name)
          || name.endsWith('.har.zip')
        ))
        const menuItems: MenuProps['items'] = [
          { key: 'bundle', label: '完整诊断包', icon: <DownloadOutlined /> },
          ...availableFiles.map((name) => ({ key: name, label: name, icon: <FileSearchOutlined /> })),
        ]
        const downloadable = ['ready', 'truncated', 'finalize_failed'].includes(item.status)
        const deletable = item.status !== 'recording' && !item.pinned
        return (
          <Space size={2}>
            <Dropdown
              disabled={!downloadable}
              menu={{
                items: menuItems,
                onClick: ({ key }) => void download(item, key === 'bundle' ? '' : key),
              }}
              trigger={['click']}
            >
              <Tooltip title="下载诊断制品">
                <Button size="small" type="text" icon={<DownloadOutlined />} loading={actionId === item.id} disabled={!downloadable} aria-label="下载诊断制品" />
              </Tooltip>
            </Dropdown>
            <Tooltip title={item.pinned ? '取消固定保留' : '固定保留'}>
              <Button
                size="small"
                type="text"
                icon={item.pinned ? <PushpinFilled /> : <PushpinOutlined />}
                onClick={() => void togglePinned(item)}
                disabled={!downloadable || actionId === item.id}
                aria-label={item.pinned ? '取消固定保留' : '固定保留'}
              />
            </Tooltip>
            <Popconfirm
              title="删除这个诊断包？"
              onConfirm={() => void remove(item)}
              disabled={!deletable}
            >
              <Tooltip title={item.pinned ? '请先取消固定保留' : item.status === 'recording' ? '采集完成后才能删除' : '删除诊断包'}>
                <Button
                  size="small"
                  type="text"
                  danger
                  icon={<DeleteOutlined />}
                  disabled={!deletable || actionId === item.id}
                  aria-label="删除诊断包"
                />
              </Tooltip>
            </Popconfirm>
          </Space>
        )
      },
    },
  ], [actionId, download, remove, togglePinned])

  return (
    <section
      style={{
        marginBottom: 10,
        border: `1px solid ${token.colorBorderSecondary}`,
        borderRadius: 6,
        overflow: 'hidden',
        background: token.colorBgContainer,
      }}
    >
      <div style={{ minHeight: 42, padding: '7px 10px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap', borderBottom: `1px solid ${token.colorBorderSecondary}` }}>
        <Space size={8} wrap>
          <BugOutlined style={{ color: token.colorPrimary }} />
          <Typography.Text strong>注册诊断</Typography.Text>
          <Tag color={mode === 'full' ? 'warning' : 'processing'}>{registrationDiagnosticsModeLabel(mode)}</Tag>
          <Typography.Text type="secondary">
            {Number(summary?.recording_count || 0)} 采集中 · {Number(summary?.failure_count || 0)} 失败 · {Number(summary?.success_count || 0)} 对照 · {formatBytes(summary?.total_bytes)}
          </Typography.Text>
        </Space>
        <Tooltip title="刷新诊断列表">
          <Button size="small" type="text" icon={<ReloadOutlined />} loading={loading} onClick={() => void load()} aria-label="刷新诊断列表" />
        </Tooltip>
      </div>
      {items.length ? (
        <Table
          size="small"
          rowKey="id"
          columns={columns}
          dataSource={items}
          tableLayout="fixed"
          pagination={items.length > 5 ? { pageSize: 5, size: 'small', showSizeChanger: false } : false}
          scroll={{ x: 1050, y: 240 }}
        />
      ) : (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={loading ? '正在读取诊断制品' : '等待首个诊断制品'} style={{ margin: '18px 0' }} />
      )}
    </section>
  )
}

export default RegistrationDiagnosticsPanel
