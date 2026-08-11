import { useEffect, useState } from 'react'
import {
  Button,
  Card,
  Input,
  Progress,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  ReloadOutlined,
  SearchOutlined,
  LineChartOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'
import { formatBeijingDateTime } from '@/lib/dateTime'

const { Text } = Typography

interface CodexWindowProgress {
  used_percent?: number | null
  remaining_percent?: number | null
  reset_after_seconds?: number | null
  reset_at?: string
  window_minutes?: number | null
}

interface CodexUsageRecord {
  id: number
  email: string
  status: string
  state: string
  checked_at: string
  source: string
  http_status: number
  error_code: string
  message: string
  chatgpt_account_id: string
  usage: Record<string, any>
  progress?: {
    updated_at: string
    five_hour?: CodexWindowProgress
    seven_day?: CodexWindowProgress
  }
}

function formatDuration(seconds?: number | null) {
  if (seconds === null || seconds === undefined || isNaN(seconds) || seconds <= 0) return ''
  const m = Math.floor(seconds / 60)
  if (m < 60) return `${m}分钟后重置`
  const h = Math.floor(m / 60)
  const remM = m % 60
  if (h < 24) return `${h}小时${remM > 0 ? `${remM}分` : ''}后重置`
  const d = Math.floor(h / 24)
  const remH = h % 24
  return `${d}天${remH > 0 ? `${remH}小时` : ''}后重置`
}

function codexStateTag(state?: string) {
  const s = String(state || '').trim().toLowerCase()
  switch (s) {
    case 'usable':
      return <Tag color="success">可用</Tag>
    case 'quota_exhausted':
      return <Tag color="warning">额度耗尽</Tag>
    case 'account_deactivated':
      return <Tag color="error">已失效</Tag>
    case 'refresh_token_invalidated':
      return <Tag color="error">RT失效</Tag>
    case 'access_token_invalidated':
      return <Tag color="error">AT失效</Tag>
    case 'unauthorized':
      return <Tag color="error">未授权</Tag>
    case 'payment_required':
      return <Tag color="warning">需付费/权限</Tag>
    default:
      return <Tag color="default">{s || '未探测'}</Tag>
  }
}

function renderWindowProgress(win?: CodexWindowProgress, winLabel?: string) {
  if (!win || win.used_percent === null || win.used_percent === undefined) {
    return <Text type="secondary" style={{ fontSize: 12 }}>暂无记录</Text>
  }
  const used = Math.min(100, Math.max(0, Number(win.used_percent)))
  const remaining = win.remaining_percent !== null && win.remaining_percent !== undefined
    ? Number(win.remaining_percent)
    : Math.max(0, 100 - used)
  const status = used >= 100 ? 'exception' : used >= 80 ? 'active' : 'normal'
  const strokeColor = used >= 100 ? '#ff4d4f' : used >= 80 ? '#faad14' : '#52c41a'

  return (
    <div style={{ width: 180 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 2 }}>
        <Text strong style={{ fontSize: 12 }}>{winLabel ? `[${winLabel}] ` : ''}已用 {Math.round(used * 10) / 10}%</Text>
        <Text type="secondary" style={{ fontSize: 12 }}>剩余 {Math.round(remaining * 10) / 10}%</Text>
      </div>
      <Progress percent={Math.round(used)} size="small" status={status} strokeColor={strokeColor} showInfo={false} />
      {win.reset_after_seconds ? (
        <div style={{ fontSize: 11, color: '#8c8c8c', marginTop: 2 }}>
          {formatDuration(win.reset_after_seconds)}
        </div>
      ) : null}
    </div>
  )
}

export default function CodexUsagePage() {
  const [loading, setLoading] = useState(false)
  const [refreshingIds, setRefreshingIds] = useState<Set<number>>(new Set())
  const [batchRefreshing, setBatchRefreshing] = useState(false)
  const [records, setRecords] = useState<CodexUsageRecord[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)
  const [statusFilter, setStatusFilter] = useState<string>('')
  const [searchEmail, setSearchEmail] = useState<string>('')
  const [selectedRowKeys, setSelectedRowKeys] = useState<number[]>([])

  const load = async (currentPage = page, currentSize = pageSize) => {
    setLoading(true)
    try {
      const params = new URLSearchParams({
        page: String(currentPage),
        page_size: String(currentSize),
      })
      if (statusFilter) params.append('status', statusFilter)
      if (searchEmail) params.append('email', searchEmail)

      const res = await apiFetch(`/chatgpt/codex-usage?${params.toString()}`) as {
        ok?: boolean
        total?: number
        items?: CodexUsageRecord[]
      }
      if (res?.items) {
        setRecords(res.items)
        setTotal(res.total || res.items.length)
      } else {
        setRecords([])
        setTotal(0)
      }
    } catch (err: any) {
      message.error(`加载 Codex 额度列表失败: ${err.message || err}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load(1, pageSize)
    setPage(1)
  }, [statusFilter])

  const handleSearch = () => {
    setPage(1)
    void load(1, pageSize)
  }

  const handleRefreshOne = async (id: number) => {
    setRefreshingIds((prev) => new Set(prev).add(id))
    try {
      await apiFetch(`/chatgpt/${id}/codex-usage/refresh`, {
        method: 'POST',
        body: JSON.stringify({ force: true }),
      })
      message.success(`账号 #${id} Codex 额度刷新成功`)
      await load()
    } catch (err: any) {
      message.error(`刷新账号 #${id} 失败: ${err.message || err}`)
    } finally {
      setRefreshingIds((prev) => {
        const next = new Set(prev)
        next.delete(id)
        return next
      })
    }
  }

  const handleBatchRefresh = async () => {
    setBatchRefreshing(true)
    try {
      const payload: Record<string, any> = { force: true }
      if (selectedRowKeys.length > 0) {
        payload.ids = selectedRowKeys
      } else {
        if (statusFilter) payload.status = statusFilter
        if (searchEmail) payload.email = searchEmail
        payload.limit = 100
      }
      await apiFetch('/chatgpt/codex-usage/refresh', {
        method: 'POST',
        body: JSON.stringify(payload),
      })
      message.success(
        selectedRowKeys.length > 0
          ? `已提交后台刷新选中的 ${selectedRowKeys.length} 个账号`
          : '已提交后台批量刷新额度任务'
      )
      setSelectedRowKeys([])
      setTimeout(() => {
        void load()
      }, 2000)
    } catch (err: any) {
      message.error(`批量刷新失败: ${err.message || err}`)
    } finally {
      setBatchRefreshing(false)
    }
  }

  const columns = [
    {
      title: 'ID',
      dataIndex: 'id',
      key: 'id',
      width: 70,
    },
    {
      title: '邮箱',
      dataIndex: 'email',
      key: 'email',
      width: 220,
      render: (email: string) => <Text copyable style={{ fontSize: 13 }}>{email}</Text>,
    },
    {
      title: '账号状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (status: string) => <Tag>{status}</Tag>,
    },
    {
      title: 'Codex 状态',
      dataIndex: 'state',
      key: 'state',
      width: 110,
      render: (state: string) => codexStateTag(state),
    },
    {
      title: '5小时窗口用量',
      key: 'five_hour',
      width: 200,
      render: (_: any, record: CodexUsageRecord) => renderWindowProgress(record.progress?.five_hour, '5h'),
    },
    {
      title: '7天/30天窗口用量',
      key: 'seven_day',
      width: 200,
      render: (_: any, record: CodexUsageRecord) => {
        const is30d = (record.progress?.seven_day?.window_minutes || 0) >= 20000
        return renderWindowProgress(record.progress?.seven_day, is30d ? '30d' : '7d')
      },
    },
    {
      title: '探测时间',
      key: 'checked_at',
      width: 160,
      render: (_: any, record: CodexUsageRecord) => {
        const timeStr = record.checked_at || record.progress?.updated_at || ''
        return <Text type="secondary" style={{ fontSize: 12 }}>{formatBeijingDateTime(timeStr)}</Text>
      },
    },
    {
      title: '操作',
      key: 'action',
      width: 100,
      render: (_: any, record: CodexUsageRecord) => (
        <Button
          size="small"
          icon={<ReloadOutlined />}
          loading={refreshingIds.has(record.id)}
          onClick={() => handleRefreshOne(record.id)}
        >
          刷新
        </Button>
      ),
    },
  ]

  return (
    <div style={{ padding: '16px 24px' }}>
      <Card style={{ marginBottom: 16, borderRadius: 12 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12 }}>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <LineChartOutlined style={{ fontSize: 22, color: '#1677ff' }} />
              <span style={{ fontSize: 18, fontWeight: 600 }}>Codex 额度监控列表</span>
            </div>
            <div style={{ color: '#8c8c8c', fontSize: 13, marginTop: 4 }}>
              展示 ChatGPT 账号在 Codex (gpt-5.4) 接口下 5 小时与 7 天限额窗口用量及倒计时重置状态。
            </div>
          </div>
          <Space>
            <Button
              type="primary"
              icon={<ThunderboltOutlined />}
              loading={batchRefreshing}
              onClick={handleBatchRefresh}
            >
              {selectedRowKeys.length > 0 ? `刷新选中 (${selectedRowKeys.length})` : '批量刷新页面额度'}
            </Button>
            <Button icon={<ReloadOutlined />} onClick={() => load()}>
              刷新列表
            </Button>
          </Space>
        </div>

        <div style={{ marginTop: 16, display: 'flex', gap: 12, flexWrap: 'wrap' }}>
          <Select
            style={{ width: 140 }}
            placeholder="筛选状态"
            allowClear
            value={statusFilter || undefined}
            onChange={(val) => setStatusFilter(val || '')}
            options={[
              { value: 'subscribed', label: '已订阅 (Subscribed)' },
              { value: 'trial', label: '试用中 (Trial)' },
              { value: 'registered', label: '已注册 (Registered)' },
              { value: 'expired', label: '已过期 (Expired)' },
              { value: 'invalid', label: '已失效 (Invalid)' },
            ]}
          />
          <Input
            style={{ width: 240 }}
            placeholder="按邮箱模糊搜索"
            allowClear
            value={searchEmail}
            onChange={(e) => setSearchEmail(e.target.value)}
            onPressEnter={handleSearch}
            prefix={<SearchOutlined style={{ color: '#bfbfbf' }} />}
          />
          <Button onClick={handleSearch}>搜索</Button>
        </div>
      </Card>

      <Card style={{ borderRadius: 12, padding: 0 }}>
        <Table
          rowKey="id"
          columns={columns}
          dataSource={records}
          loading={loading}
          pagination={{
            current: page,
            pageSize: pageSize,
            total: total,
            showSizeChanger: true,
            onChange: (p, s) => {
              setPage(p)
              setPageSize(s)
              void load(p, s)
            },
          }}
          rowSelection={{
            selectedRowKeys,
            onChange: (keys) => setSelectedRowKeys(keys as number[]),
          }}
          scroll={{ x: 1100 }}
        />
      </Card>
    </div>
  )
}
