import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, App, Button, Card, Descriptions, Drawer, Form, Input, InputNumber, Select, Space, Switch, Table, Tabs, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ApiOutlined, CopyOutlined, DownloadOutlined, KeyOutlined, PlusOutlined, ReloadOutlined, SafetyOutlined, SearchOutlined, ToolOutlined } from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'
import { formatBeijingDate, formatBeijingDateTime } from '@/lib/dateTime'

type SkuSummary = {
  sku_code: string
  name: string
  prefix: string
  enabled: boolean
  delivery_profile: string
  sort_policy: string
  available_accounts: number
  unused_cards: number
  stock_delta: number
  risk: boolean
  next_account?: { email?: string; subscription_active_until?: string; status?: string } | null
}

type Summary = {
  api?: Record<string, any>
  stock?: { items?: SkuSummary[] }
  recent_errors?: ApiLog[]
}

type Batch = {
  id: number
  name: string
  sku_code: string
  code_prefix: string
  total_count: number
  strict_stock_check: boolean
  expires_at: string
  created_at: string
  counts?: Record<string, number>
  unused_count?: number
  redeemed_count?: number
  disabled_count?: number
  expired_count?: number
}

type CardItem = {
  id: number
  batch_id: number
  sku_code: string
  code_mask: string
  status: string
  assigned_account_id: number
  assigned_email_snapshot: string
  assigned_at: string
  redeem_count: number
  first_redeemed_at: string
  last_redeemed_at: string
  last_failure_code: string
  last_failure_at: string
  expires_at: string
}

type EventItem = {
  id: number
  card_id: number
  sku_code: string
  account_id: number
  event_type: string
  result: string
  failure_code: string
  delivery_sequence: number
  request_id: string
  consumer: string
  message: string
  created_at: string
}

type ApiLog = {
  id: number
  trace_id: string
  request_id: string
  consumer: string
  code_mask: string
  card_id: number
  sku_code: string
  assigned_account_email: string
  action: string
  result: string
  error_code: string
  redeem_index: number
  first_redeem: boolean
  idempotent_replay: boolean
  duplicate_check_status: string
  duplicate_check_message: string
  duration_ms: number
  message: string
  created_at: string
  decision?: any
  response_summary?: any
}


type ConsistencyIssue = {
  type: string
  severity: string
  repairable: boolean
  card_id?: number
  account_id?: number
  api_log_id?: number
  card_ids?: number[]
  message: string
}

type ConsistencyReport = {
  ok: boolean
  issue_count: number
  repairable_count: number
  by_type?: Record<string, number>
  issues?: ConsistencyIssue[]
}

const STATUS_META: Record<string, { label: string; color: string }> = {
  unused: { label: '未兑换', color: 'default' },
  redeemed: { label: '已兑换', color: 'success' },
  disabled: { label: '已禁用', color: 'error' },
  expired: { label: '已过期', color: 'warning' },
  blocked: { label: '已锁定', color: 'error' },
}

const RESULT_META: Record<string, { label: string; color: string }> = {
  success: { label: '成功', color: 'success' },
  failed: { label: '失败', color: 'error' },
}

function statusTag(status?: string) {
  const meta = STATUS_META[String(status || '')] || { label: status || '-', color: 'default' }
  return <Tag color={meta.color}>{meta.label}</Tag>
}

function resultTag(result?: string) {
  const meta = RESULT_META[String(result || '')] || { label: result || '-', color: 'default' }
  return <Tag color={meta.color}>{meta.label}</Tag>
}

function fmt(value?: string) {
  return formatBeijingDateTime(value)
}


function getDetailPhoneBinding(detail: any) {
  const payloadAccount = detail?.card?.delivery_payload?.account && typeof detail.card.delivery_payload.account === 'object'
    ? detail.card.delivery_payload.account
    : {}
  const fromPayload = payloadAccount.phone_binding && typeof payloadAccount.phone_binding === 'object' ? payloadAccount.phone_binding : {}
  let accountExtra: any = {}
  try {
    accountExtra = detail?.account?.extra_json ? JSON.parse(String(detail.account.extra_json)) : {}
  } catch {
    accountExtra = {}
  }
  const fromExtra = accountExtra.chatgpt_phone_binding && typeof accountExtra.chatgpt_phone_binding === 'object' ? accountExtra.chatgpt_phone_binding : {}
  const binding = Object.keys(fromPayload).length ? fromPayload : fromExtra
  const phone = String(binding.phone || binding.phone_e164 || payloadAccount.phone || '').trim()
  const apiUrl = String(binding.api_url || binding.sms_api || payloadAccount.sms_api || '').trim()
  return {
    bound: Boolean(binding.bound || phone),
    phone,
    apiUrl,
    message: String(binding.message || payloadAccount.phone_binding_message || '该账号没有手机号绑定记录').trim(),
    boundAt: String(binding.bound_at || '').trim(),
    apiExpiredDate: String(binding.api_expired_date || '').trim(),
  }
}

function downloadText(filename: string, text: string) {
  const blob = new Blob([text], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

export default function DeliveryCardsPage() {
  const { message, modal } = App.useApp()
  const [activeTab, setActiveTab] = useState('overview')
  const [summary, setSummary] = useState<Summary>({})
  const [batches, setBatches] = useState<Batch[]>([])
  const [cards, setCards] = useState<CardItem[]>([])
  const [events, setEvents] = useState<EventItem[]>([])
  const [apiLogs, setApiLogs] = useState<ApiLog[]>([])
  const [settings, setSettings] = useState<Record<string, any>>({})
  const [consistency, setConsistency] = useState<ConsistencyReport | null>(null)
  const [tokenTestValue, setTokenTestValue] = useState('')
  const [lookupCode, setLookupCode] = useState('')
  const [loading, setLoading] = useState(false)
  const [batchOpen, setBatchOpen] = useState(false)
  const [detailOpen, setDetailOpen] = useState(false)
  const [detail, setDetail] = useState<any>(null)
  const [batchForm] = Form.useForm()
  const [settingsForm] = Form.useForm()
  const [filters, setFilters] = useState({ sku_code: '', status: '', search: '' })

  const loadAll = useCallback(async () => {
    setLoading(true)
    try {
      const [summaryData, batchData, cardData, eventData, logData, settingsData, consistencyData] = await Promise.all([
        apiFetch('/admin/delivery-cards/summary'),
        apiFetch('/admin/delivery-cards/batches'),
        apiFetch('/admin/delivery-cards/cards'),
        apiFetch('/admin/delivery-cards/events'),
        apiFetch('/admin/delivery-cards/api-logs'),
        apiFetch('/admin/delivery-cards/settings'),
        apiFetch('/admin/delivery-cards/consistency'),
      ])
      setSummary(summaryData as Summary)
      setBatches((batchData as any).items || [])
      setCards((cardData as any).items || [])
      setEvents((eventData as any).items || [])
      setApiLogs((logData as any).items || [])
      setSettings(settingsData as Record<string, any>)
      setConsistency(consistencyData as ConsistencyReport)
      settingsForm.setFieldsValue(settingsData)
    } finally {
      setLoading(false)
    }
  }, [settingsForm])

  useEffect(() => { void loadAll() }, [loadAll])

  const filteredCards = useMemo(() => {
    const q = filters.search.trim().toLowerCase()
    return cards.filter((item) => {
      if (filters.sku_code && item.sku_code !== filters.sku_code) return false
      if (filters.status && item.status !== filters.status) return false
      if (q && !`${item.code_mask} ${item.assigned_email_snapshot} ${item.last_failure_code}`.toLowerCase().includes(q)) return false
      return true
    })
  }, [cards, filters])

  const openDetail = async (id: number) => {
    const data = await apiFetch(`/admin/delivery-cards/cards/${id}`)
    setDetail(data)
    setDetailOpen(true)
  }

  const createBatch = async () => {
    const values = await batchForm.validateFields()
    const result = await apiFetch('/admin/delivery-cards/batches', {
      method: 'POST',
      body: JSON.stringify(values),
    }) as any
    message.success(`已生成 ${result?.codes?.length || 0} 张交付卡密`)
    if (result?.csv) downloadText(`${values.name || values.sku_code}-delivery-cards.csv`, result.csv)
    setBatchOpen(false)
    batchForm.resetFields()
    await loadAll()
  }

  const rotateToken = () => {
    modal.confirm({
      title: '轮换兑换 API Token？',
      content: '旧 Token 会立即失效，外部系统未更新前将无法兑换卡密。新 Token 只显示一次。',
      okText: '轮换 Token',
      cancelText: '取消',
      onOk: async () => {
        const result = await apiFetch('/admin/delivery-cards/settings/token/rotate', { method: 'POST' }) as any
        modal.info({
          title: '新 Token 已生成，只显示一次',
          content: <Typography.Text copyable={{ text: result.token }} code style={{ wordBreak: 'break-all' }}>{result.token}</Typography.Text>,
        })
        await loadAll()
      },
    })
  }

  const saveSettings = async () => {
    const values = await settingsForm.validateFields()
    const data: Record<string, any> = {}
    Object.entries(values).forEach(([key, value]) => { data[key] = String(value ?? '') })
    await apiFetch('/admin/delivery-cards/settings', { method: 'PUT', body: JSON.stringify({ data }) })
    message.success('设置已保存')
    await loadAll()
  }

  const testDeliveryToken = async () => {
    if (!tokenTestValue.trim()) {
      message.warning('先输入要测试的兑换 API Token')
      return
    }
    const result = await apiFetch('/admin/delivery-cards/settings/test-token', {
      method: 'POST',
      body: JSON.stringify({ token: tokenTestValue.trim() }),
    }) as any
    if (result?.ok) message.success('Token 匹配当前兑换 API')
    else message.error('Token 不匹配或未配置')
  }

  const lookupCard = async () => {
    if (!lookupCode.trim()) {
      message.warning('请输入完整卡密')
      return
    }
    const data = await apiFetch('/admin/delivery-cards/cards/lookup', {
      method: 'POST',
      body: JSON.stringify({ code: lookupCode.trim() }),
    })
    setDetail(data)
    setDetailOpen(true)
  }

  const repairConsistency = () => {
    modal.confirm({
      title: '执行一致性修复？',
      content: '只修复安全项：已绑定账号补 manually_used 标记，已绑定却显示未兑换的卡密恢复为已兑换。重复绑定等高风险问题只报告不自动改。',
      okText: '执行修复',
      cancelText: '取消',
      onOk: async () => {
        const result = await apiFetch('/admin/delivery-cards/consistency/repair', { method: 'POST' }) as any
        message.success(`已修复 ${result?.repaired_count || 0} 项`)
        setConsistency(result?.after || null)
        await loadAll()
      },
    })
  }

  const disableCard = async (record: CardItem) => {
    await apiFetch(`/admin/delivery-cards/cards/${record.id}/disable`, { method: 'POST', body: JSON.stringify({ reason: '管理端禁用' }) })
    message.success('已禁用')
    await loadAll()
  }

  const enableCard = async (record: CardItem) => {
    await apiFetch(`/admin/delivery-cards/cards/${record.id}/enable`, { method: 'POST' })
    message.success('已启用')
    await loadAll()
  }

  const stockColumns: ColumnsType<SkuSummary> = [
    { title: '类型', dataIndex: 'sku_code', render: (v, r) => <Space><Tag color={r.risk ? 'error' : 'blue'}>{String(v).toUpperCase()}</Tag><span>{r.name}</span></Space> },
    { title: '前缀', dataIndex: 'prefix', width: 90 },
    { title: '可用账号', dataIndex: 'available_accounts', width: 110 },
    { title: '未兑换卡密', dataIndex: 'unused_cards', width: 120 },
    { title: '库存差额', dataIndex: 'stock_delta', width: 110, render: (v) => <Tag color={Number(v) < 0 ? 'error' : Number(v) === 0 ? 'warning' : 'success'}>{v}</Tag> },
    { title: '分配顺序', dataIndex: 'sort_policy', width: 150, render: (v) => v === 'earliest_expiry' ? '最早到期优先' : '最早入库优先' },
    { title: '下一个账号', dataIndex: 'next_account', render: (v) => v ? `${v.email || '-'}${v.subscription_active_until ? ` · ${v.subscription_active_until}` : ''}` : '-' },
    { title: '状态', dataIndex: 'risk', width: 120, render: (risk, r) => risk ? <Tag color="error">超发风险</Tag> : r.stock_delta === 0 ? <Tag color="warning">刚好匹配</Tag> : <Tag color="success">正常</Tag> },
  ]

  const batchColumns: ColumnsType<Batch> = [
    { title: '批次', dataIndex: 'name' },
    { title: '类型', dataIndex: 'sku_code', width: 90, render: (v) => <Tag>{String(v).toUpperCase()}</Tag> },
    { title: '总数', dataIndex: 'total_count', width: 80 },
    { title: '未兑换', dataIndex: 'unused_count', width: 90 },
    { title: '已兑换', dataIndex: 'redeemed_count', width: 90 },
    { title: '禁用', dataIndex: 'disabled_count', width: 80 },
    { title: '库存检查', dataIndex: 'strict_stock_check', width: 110, render: (v) => v ? <Tag color="success">严格</Tag> : <Tag color="warning">允许超发</Tag> },
    { title: '创建时间', dataIndex: 'created_at', width: 180, render: fmt },
  ]

  const cardColumns: ColumnsType<CardItem> = [
    { title: '卡密', dataIndex: 'code_mask', width: 210, render: (v) => <Typography.Text code>{v}</Typography.Text> },
    { title: '类型', dataIndex: 'sku_code', width: 90, render: (v) => <Tag>{String(v).toUpperCase()}</Tag> },
    { title: '状态', dataIndex: 'status', width: 100, render: statusTag },
    { title: '绑定账号', dataIndex: 'assigned_email_snapshot', render: (v) => v || <Typography.Text type="secondary">未分配</Typography.Text> },
    { title: '取回次数', dataIndex: 'redeem_count', width: 100 },
    { title: '首次兑换', dataIndex: 'first_redeemed_at', width: 170, render: fmt },
    { title: '最近取回', dataIndex: 'last_redeemed_at', width: 170, render: fmt },
    { title: '最近失败', dataIndex: 'last_failure_code', width: 150, render: (v) => v ? <Tag color="error">{v}</Tag> : '-' },
    { title: '操作', width: 180, render: (_, r) => <Space><Button size="small" onClick={() => openDetail(r.id)}>详情</Button>{r.status === 'disabled' ? <Button size="small" onClick={() => enableCard(r)}>启用</Button> : <Button size="small" danger onClick={() => disableCard(r)}>禁用</Button>}</Space> },
  ]

  const eventColumns: ColumnsType<EventItem> = [
    { title: '时间', dataIndex: 'created_at', width: 170, render: fmt },
    { title: '结果', dataIndex: 'result', width: 90, render: resultTag },
    { title: '事件', dataIndex: 'event_type', width: 130 },
    { title: '类型', dataIndex: 'sku_code', width: 90, render: (v) => v ? <Tag>{String(v).toUpperCase()}</Tag> : '-' },
    { title: '卡密 ID', dataIndex: 'card_id', width: 90 },
    { title: '账号 ID', dataIndex: 'account_id', width: 90 },
    { title: '第几次', dataIndex: 'delivery_sequence', width: 90 },
    { title: '请求方', dataIndex: 'consumer', width: 120 },
    { title: 'Request ID', dataIndex: 'request_id', width: 180 },
    { title: '失败原因', dataIndex: 'failure_code', width: 180, render: (v) => v ? <Tag color="error">{v}</Tag> : '-' },
    { title: '消息', dataIndex: 'message' },
  ]

  const apiLogColumns: ColumnsType<ApiLog> = [
    { title: '时间', dataIndex: 'created_at', width: 170, render: fmt },
    { title: '结果', dataIndex: 'result', width: 90, render: resultTag },
    { title: '动作', dataIndex: 'action', width: 140 },
    { title: '类型', dataIndex: 'sku_code', width: 90, render: (v) => v ? <Tag>{String(v).toUpperCase()}</Tag> : '-' },
    { title: '卡密', dataIndex: 'code_mask', width: 210, render: (v) => v ? <Typography.Text code>{v}</Typography.Text> : '-' },
    { title: '账号', dataIndex: 'assigned_account_email', width: 210, render: (v) => v || '-' },
    { title: '第几次', dataIndex: 'redeem_index', width: 90 },
    { title: '查重', dataIndex: 'duplicate_check_status', width: 110, render: (v) => v ? <Tag color={v === 'failed' ? 'error' : v === 'passed' ? 'success' : 'default'}>{v === 'passed' ? '通过' : v === 'failed' ? '失败' : v}</Tag> : '-' },
    { title: '请求方', dataIndex: 'consumer', width: 120 },
    { title: 'Request ID', dataIndex: 'request_id', width: 180 },
    { title: '耗时', dataIndex: 'duration_ms', width: 90, render: (v) => `${v || 0}ms` },
    { title: '错误', dataIndex: 'error_code', width: 180, render: (v) => v ? <Tag color="error">{v}</Tag> : '-' },
  ]

  const overview = (
    <Space direction="vertical" size={14} style={{ width: '100%' }}>
      {summary.api?.api_enabled ? (
        <Alert type="success" showIcon message={`外部兑换 API 已启用 · Token ${summary.api?.token_configured ? '已配置' : '未配置'} · 今日成功 ${summary.api?.today_success || 0} · 今日失败 ${summary.api?.today_failed || 0}`} />
      ) : (
        <Alert type="warning" showIcon message="外部兑换 API 未启用，外部系统无法兑换卡密。" />
      )}
      <Card title="类型库存平衡" size="small">
        <Table rowKey="sku_code" size="small" pagination={false} columns={stockColumns} dataSource={summary.stock?.items || []} />
      </Card>
      <Card title="最近异常" size="small">
        <Table rowKey="id" size="small" pagination={false} columns={apiLogColumns} dataSource={summary.recent_errors || []} scroll={{ x: 'max-content' }} />
      </Card>
    </Space>
  )

  const severityColor = (severity?: string) => severity === 'critical' ? 'error' : severity === 'high' ? 'warning' : 'default'

  const consistencyColumns: ColumnsType<ConsistencyIssue> = [
    { title: '级别', dataIndex: 'severity', width: 90, render: (v) => <Tag color={severityColor(v)}>{v || '-'}</Tag> },
    { title: '问题类型', dataIndex: 'type', width: 240 },
    { title: '卡密 ID', dataIndex: 'card_id', width: 100, render: (v) => v || '-' },
    { title: '账号 ID', dataIndex: 'account_id', width: 100, render: (v) => v || '-' },
    { title: '可修复', dataIndex: 'repairable', width: 100, render: (v) => v ? <Tag color="success">是</Tag> : <Tag>否</Tag> },
    { title: '说明', dataIndex: 'message' },
  ]

  const opsTab = (
    <Space direction="vertical" size={14} style={{ width: '100%' }}>
      <Card size="small" title="安全卡密查询">
        <Space.Compact style={{ width: 'min(720px, 100%)' }}>
          <Input.Password value={lookupCode} onChange={(e) => setLookupCode(e.target.value)} placeholder="输入完整卡密，后端只做 hash 匹配，不保存明文" />
          <Button icon={<SearchOutlined />} onClick={lookupCard}>查询</Button>
        </Space.Compact>
      </Card>
      <Card
        size="small"
        title={<Space><SafetyOutlined />一致性检查</Space>}
        extra={<Space><Button icon={<ReloadOutlined />} onClick={loadAll}>重新检查</Button><Button icon={<ToolOutlined />} disabled={!consistency?.repairable_count} onClick={repairConsistency}>修复安全项</Button></Space>}
      >
        {consistency?.ok ? (
          <Alert showIcon type="success" message="当前未发现交付卡密一致性问题" />
        ) : (
          <Alert showIcon type="warning" message={`发现 ${consistency?.issue_count || 0} 个问题，其中 ${consistency?.repairable_count || 0} 个可自动修复`} style={{ marginBottom: 12 }} />
        )}
        <Table rowKey={(r) => `${r.type}-${r.card_id || r.account_id || r.api_log_id || r.message}`} size="small" pagination={{ pageSize: 20 }} columns={consistencyColumns} dataSource={consistency?.issues || []} scroll={{ x: 'max-content' }} />
      </Card>
    </Space>
  )

  const settingsTab = (
    <Card size="small">
      <Form layout="vertical" form={settingsForm} initialValues={settings}>
        <Alert style={{ marginBottom: 14 }} showIcon type="info" message="外部系统通过 POST /api/public/delivery-cards/redeem 同步兑换账号。库存不足时返回 POOL_EMPTY，卡密不会被消耗。" />
        <Form.Item name="delivery_cards_api_enabled" label="启用外部兑换 API" valuePropName="checked" getValueFromEvent={(checked) => checked ? 'true' : 'false'} getValueProps={(value) => ({ checked: String(value) === 'true' })}>
          <Switch />
        </Form.Item>
        <Descriptions size="small" bordered column={1} style={{ marginBottom: 16 }}>
          <Descriptions.Item label="Token 状态">{settings.token_configured ? <Tag color="success">已配置 · ****{settings.token_last4}</Tag> : <Tag color="warning">未配置</Tag>}</Descriptions.Item>
          <Descriptions.Item label="接口地址"><Typography.Text code>POST /api/public/delivery-cards/redeem</Typography.Text></Descriptions.Item>
        </Descriptions>
        <Space style={{ marginBottom: 20 }} wrap>
          <Button icon={<KeyOutlined />} onClick={rotateToken}>生成/轮换 Token</Button>
          <Input.Password value={tokenTestValue} onChange={(e) => setTokenTestValue(e.target.value)} placeholder="粘贴 Token 测试" style={{ width: 320 }} />
          <Button onClick={testDeliveryToken}>测试 Token</Button>
        </Space>
        <Form.Item name="delivery_cards_api_rate_limit_per_minute" label="每分钟请求上限">
          <InputNumber min={1} max={10000} style={{ width: 220 }} />
        </Form.Item>
        <Form.Item name="delivery_cards_api_failed_block_threshold" label="5 分钟失败次数阻断阈值">
          <InputNumber min={0} max={10000} style={{ width: 220 }} />
        </Form.Item>
        <Form.Item name="delivery_cards_api_failed_block_minutes" label="失败阻断分钟数">
          <InputNumber min={1} max={1440} style={{ width: 220 }} />
        </Form.Item>
        <Form.Item name="delivery_cards_api_failure_mode" label="失败响应模式">
          <Select style={{ maxWidth: 320 }} options={[{ value: 'safe', label: '安全模式，推荐' }, { value: 'debug', label: '调试模式' }]} />
        </Form.Item>
        <Form.Item name="delivery_cards_plus_sort_policy" label="PLUS 分配顺序">
          <Select style={{ maxWidth: 360 }} options={[{ value: 'earliest_expiry', label: '最早到期优先，推荐' }, { value: 'oldest_created', label: '最早入库优先' }, { value: 'oldest_subscription_started', label: '最早开通订阅优先，缺失时回退' }]} />
        </Form.Item>
        <Form.Item name="delivery_cards_free_sort_policy" label="FREE 分配顺序">
          <Select style={{ maxWidth: 320 }} options={[{ value: 'oldest_created', label: '最早入库优先' }]} />
        </Form.Item>
        <Space>
          <Button type="primary" onClick={saveSettings}>保存设置</Button>
          <Button icon={<CopyOutlined />} onClick={() => navigator.clipboard.writeText(`curl -X POST ${window.location.origin}/api/public/delivery-cards/redeem \\\n  -H 'Authorization: Bearer <TOKEN>' \\\n  -H 'Content-Type: application/json' \\\n  -H 'Idempotency-Key: order_10001' \\\n  -d '{"code":"PLUS-XXXX-XXXX-XXXX-XXXX","consumer":"partner-a","request_id":"order_10001"}'`)}>复制 curl 示例</Button>
        </Space>
      </Form>
    </Card>
  )

  return (
    <div style={{ width: '100%', minWidth: 0 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'flex-start', marginBottom: 14, flexWrap: 'wrap' }}>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>交付卡密</Typography.Title>
          <Typography.Text type="secondary">外部系统通过 API 兑换 PLUS / FREE 卡密，首次兑换时动态分配账号。</Typography.Text>
        </div>
        <Space wrap>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => { batchForm.setFieldsValue({ sku_code: 'plus', count: 10, strict_stock_check: true, name: `交付卡密批次 ${formatBeijingDate(new Date())}` }); setBatchOpen(true) }}>创建批次</Button>
          <Button icon={<SafetyOutlined />} onClick={() => setActiveTab('ops')}>运维检查</Button>
          <Button icon={<ApiOutlined />} onClick={() => setActiveTab('settings')}>API 设置</Button>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={loadAll}>刷新</Button>
        </Space>
      </div>
      <Tabs activeKey={activeTab} onChange={setActiveTab} items={[
        { key: 'overview', label: '总览', children: overview },
        { key: 'batches', label: '批次', children: <Table rowKey="id" loading={loading} columns={batchColumns} dataSource={batches} scroll={{ x: 'max-content' }} /> },
        { key: 'cards', label: '卡密', children: <Space direction="vertical" style={{ width: '100%' }}><Space wrap><Select value={filters.sku_code} onChange={(v) => setFilters((p) => ({ ...p, sku_code: v }))} style={{ width: 140 }} options={[{ value: '', label: '全部类型' }, { value: 'plus', label: 'PLUS' }, { value: 'free', label: 'FREE' }]} /><Select value={filters.status} onChange={(v) => setFilters((p) => ({ ...p, status: v }))} style={{ width: 140 }} options={[{ value: '', label: '全部状态' }, { value: 'unused', label: '未兑换' }, { value: 'redeemed', label: '已兑换' }, { value: 'disabled', label: '已禁用' }, { value: 'expired', label: '已过期' }]} /><Input.Search allowClear placeholder="搜索卡密后缀 / 绑定邮箱" style={{ width: 260 }} onSearch={(v) => setFilters((p) => ({ ...p, search: v }))} /></Space><Table rowKey="id" loading={loading} columns={cardColumns} dataSource={filteredCards} scroll={{ x: 'max-content' }} /></Space> },
        { key: 'events', label: '兑换记录', children: <Table rowKey="id" loading={loading} columns={eventColumns} dataSource={events} scroll={{ x: 'max-content' }} /> },
        { key: 'apiLogs', label: '兑换 API 日志', children: <Table rowKey="id" loading={loading} columns={apiLogColumns} dataSource={apiLogs} scroll={{ x: 'max-content' }} expandable={{ expandedRowRender: (r) => <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{JSON.stringify({ trace_id: r.trace_id, duplicate_check: r.duplicate_check_message, decision: r.decision, response_summary: r.response_summary, phone: r.response_summary?.phone || '', phone_bound: r.response_summary?.phone_bound, message: r.message }, null, 2)}</pre> }} /> },
        { key: 'ops', label: '运维检查', children: opsTab },
        { key: 'settings', label: 'API 设置', children: settingsTab },
      ]} />

      <Drawer title="创建交付卡密批次" open={batchOpen} onClose={() => setBatchOpen(false)} width={520} extra={<Space><Button onClick={() => setBatchOpen(false)}>取消</Button><Button type="primary" icon={<DownloadOutlined />} onClick={createBatch}>生成并下载 CSV</Button></Space>}>
        <Form layout="vertical" form={batchForm}>
          <Form.Item name="name" label="批次名称" rules={[{ required: true, message: '请输入批次名称' }]}><Input /></Form.Item>
          <Form.Item name="sku_code" label="卡密类型" rules={[{ required: true }]}><Select options={[{ value: 'plus', label: 'PLUS · ChatGPT Plus 账号' }, { value: 'free', label: 'FREE · ChatGPT Free 账号' }]} /></Form.Item>
          <Form.Item name="count" label="生成数量" rules={[{ required: true }]}><InputNumber min={1} max={5000} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="strict_stock_check" label="严格库存检查" valuePropName="checked"><Switch defaultChecked /></Form.Item>
          <Form.Item name="expires_at" label="首次兑换有效期"><Input placeholder="留空表示不过期，例如 2026-07-01T00:00:00Z" /></Form.Item>
          <Alert showIcon type="warning" message="完整卡密只在生成后的 CSV 中展示，请妥善保存。过期时间只限制首次兑换，已兑换卡密仍可取回。" />
        </Form>
      </Drawer>

      <Drawer title="交付卡密详情" open={detailOpen} onClose={() => setDetailOpen(false)} width={720}>
        {detail ? <Space direction="vertical" style={{ width: '100%' }} size={14}>
          <Descriptions bordered size="small" column={1}>
            <Descriptions.Item label="卡密">{detail.card?.code_mask}</Descriptions.Item>
            <Descriptions.Item label="类型">{String(detail.card?.sku_code || '').toUpperCase()}</Descriptions.Item>
            <Descriptions.Item label="状态">{statusTag(detail.card?.status)}</Descriptions.Item>
            <Descriptions.Item label="绑定账号">{detail.card?.assigned_email_snapshot || '未分配'}</Descriptions.Item>
            <Descriptions.Item label="取回次数">{detail.card?.redeem_count || 0}</Descriptions.Item>
            {(() => {
              const phoneBinding = getDetailPhoneBinding(detail)
              return phoneBinding.bound ? (
                <>
                  <Descriptions.Item label="绑定手机号"><Typography.Text copyable>{phoneBinding.phone}</Typography.Text></Descriptions.Item>
                  <Descriptions.Item label="接码 API">{phoneBinding.apiUrl ? <Typography.Text copyable style={{ wordBreak: 'break-all' }}>{phoneBinding.apiUrl}</Typography.Text> : <Typography.Text type="secondary">未记录接码 API</Typography.Text>}</Descriptions.Item>
                  <Descriptions.Item label="手机号绑定时间">{phoneBinding.boundAt || '-'}</Descriptions.Item>
                  <Descriptions.Item label="接码 API 有效期">{phoneBinding.apiExpiredDate || '-'}</Descriptions.Item>
                </>
              ) : (
                <Descriptions.Item label="手机号绑定"><Typography.Text type="secondary">{phoneBinding.message || '该账号没有手机号绑定记录'}</Typography.Text></Descriptions.Item>
              )
            })()}
          </Descriptions>
          <Card size="small" title="最近事件"><Table size="small" rowKey="id" pagination={false} columns={eventColumns.slice(0, 8)} dataSource={detail.events || []} scroll={{ x: 'max-content' }} /></Card>
          <Card size="small" title="最近 API 日志"><Table size="small" rowKey="id" pagination={false} columns={apiLogColumns.slice(0, 9)} dataSource={detail.api_logs || []} scroll={{ x: 'max-content' }} /></Card>
        </Space> : null}
      </Drawer>
    </div>
  )
}
