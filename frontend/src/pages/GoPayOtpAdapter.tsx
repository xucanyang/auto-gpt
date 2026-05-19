import { type CSSProperties, useEffect, useMemo, useState } from 'react'
import {
  App,
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Row,
  Space,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import {
  ApiOutlined,
  DeleteOutlined,
  LinkOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SaveOutlined,
  SafetyOutlined,
} from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'

const { Paragraph, Text, Title } = Typography
const { TextArea } = Input

const responsiveContainerStyle: CSSProperties = {
  width: '100%',
  maxWidth: '100%',
  minWidth: 0,
}

const responsiveCardStyle: CSSProperties = {
  ...responsiveContainerStyle,
  overflow: 'hidden',
}

const pageHeaderStyle: CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  gap: 16,
  alignItems: 'flex-start',
  flexWrap: 'wrap',
}

const compactCellStyle: CSSProperties = {
  display: 'inline-block',
  maxWidth: '100%',
  verticalAlign: 'bottom',
}

function compactText(value?: string | number | null, width = 160) {
  const text = value === undefined || value === null || value === '' ? '-' : String(value)
  return (
    <Text ellipsis={{ tooltip: text }} style={{ ...compactCellStyle, width }}>
      {text}
    </Text>
  )
}

type PhonePoolItem = {
  phone_country_code: string
  phone_number: string
  uid: string
  status: 'ready' | 'reserved' | 'used' | 'invalid' | string
  source: string
  package_name: string
  title: string
  label: string
  device: string
  pin?: string
  has_pin?: boolean
  pin_source?: string
  note?: string
  enabled?: boolean
  last_otp?: string
  last_otp_at?: string
  last_error?: string
  status_text?: string
  last_seen_at: string
  last_used_account_id?: number | null
  gopay_session_id: string
  updated_at: string
}

type ActiveSession = {
  uid: string
  account_id: number
  gopay_session_id: string
  phone_country_code: string
  phone_number: string
  phase: string
  status: string
  last_error: string
  status_text?: string
  phase_text?: string
  pin_source?: string
  active_action?: string
  otp_resend_count?: number
  otp_auto_resend_done?: boolean
  last_otp_resend_at?: string
  started_at?: string
  updated_at?: string
}

type RecentEvent = {
  event_id: string
  received_at: string
  type?: string
  status: string
  status_text?: string
  phase_text?: string
  uid: string
  phone_country_code?: string
  phone_number?: string
  otp: string
  package_name: string
  title: string
  device?: string
  detail?: string
  message?: string
  raw_excerpt?: string
  phase?: string
  account_id?: number
  gopay_session_id?: string
}

type AdapterSummary = {
  bindings_total?: number
  bindings_enabled?: number
  bindings_disabled?: number
  sessions_total?: number
  sessions_waiting_otp?: number
  sessions_missing?: number
  phone_pool?: Record<string, number>
  recent_events?: Record<string, number>
}

type AdapterState = {
  phone_pool: PhonePoolItem[]
  sessions: ActiveSession[]
  recent_events: RecentEvent[]
  summary?: AdapterSummary
  secret_enabled: boolean
  webhook_path: string
  message_template: string
  bind_message_template?: string
  web_params_form: string
  web_params_json: string
  otp_auto_resend_delay_seconds: number
}

const emptyPoolItem: PhonePoolItem = {
  phone_country_code: '86',
  phone_number: '',
  uid: '',
  status: 'ready',
  source: 'manual',
  package_name: '',
  title: '',
  label: '',
  device: '',
  pin: '',
  note: '',
  enabled: true,
  last_otp: '',
  last_otp_at: '',
  last_error: '',
  last_seen_at: '',
  gopay_session_id: '',
  updated_at: '',
}

function cleanPoolItem(value: PhonePoolItem): PhonePoolItem {
  return {
    phone_country_code: String(value.phone_country_code || '86').replace(/\D/g, '') || '86',
    phone_number: String(value.phone_number || '').replace(/\D/g, ''),
    uid: String(value.uid || '').trim(),
    status: String(value.status || 'ready').trim() || 'ready',
    source: String(value.source || 'manual').trim() || 'manual',
    package_name: String(value.package_name || '').trim(),
    title: String(value.title || '').trim(),
    label: String(value.label || '').trim(),
    device: String(value.device || '').trim(),
    pin: String(value.pin || '').replace(/\D/g, ''),
    note: String(value.note || '').trim(),
    enabled: value.enabled !== false,
    last_otp: String(value.last_otp || '').replace(/\D/g, ''),
    last_otp_at: value.last_otp_at || '',
    last_error: String(value.last_error || '').trim(),
    last_seen_at: value.last_seen_at || '',
    last_used_account_id: value.last_used_account_id ?? null,
    gopay_session_id: String(value.gopay_session_id || '').trim(),
    updated_at: value.updated_at || '',
  }
}

function statusColor(status: string) {
  if (['submitted', 'bound', 'used', 'succeeded', '支付成功', '验证码已提交', '手机号已绑定'].includes(status)) return 'green'
  if (['started', 'ready', 'reserved', 'waiting_otp', 'waiting_link_pin', 'waiting_payment_pin', '支付会话已启动', '可用', '等待验证码', '等待绑定PIN', '等待支付PIN'].includes(status)) return 'blue'
  if (['duplicate', 'ignored', '重复提交已忽略', '已忽略'].includes(status)) return 'gold'
  if (['conflict', 'submit_failed', 'failed', 'invalid', 'missing', 'unmatched_phone', 'unmatched_session', 'missing_uid', 'missing_phone', 'missing_otp', '绑定冲突', '验证码提交失败', '支付失败', '无效', '会话丢失', '未匹配手机号', '未匹配会话', '格式错误'].includes(status)) return 'red'
  return 'default'
}

function displayStatus(record: { status?: string; status_text?: string }) {
  return record.status_text || record.status || '-'
}

function displayPhase(record: { phase?: string; phase_text?: string }) {
  return record.phase_text || record.phase || '-'
}

function formatBeijingTime(value?: string) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  const beijing = new Date(date.getTime() + 8 * 60 * 60 * 1000)
  const pad = (part: number) => String(part).padStart(2, '0')
  return `${beijing.getUTCFullYear()}-${pad(beijing.getUTCMonth() + 1)}-${pad(beijing.getUTCDate())} ${pad(beijing.getUTCHours())}:${pad(beijing.getUTCMinutes())}:${pad(beijing.getUTCSeconds())}`
}

function eventTimeMs(event: RecentEvent) {
  const ms = new Date(event.received_at || '').getTime()
  return Number.isFinite(ms) ? ms : 0
}

function phoneText(item: { phone_country_code?: string; phone_number?: string }) {
  const number = item.phone_number || ''
  return number ? `+${item.phone_country_code || '86'} ${number}` : '-'
}

export default function GoPayOtpAdapter() {
  const { message } = App.useApp()
  const [state, setState] = useState<AdapterState | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [activeEvent, setActiveEvent] = useState<RecentEvent | null>(null)
  const [poolForm] = Form.useForm<PhonePoolItem>()
  const [startForm] = Form.useForm()
  const [secretForm] = Form.useForm<{ smsforwarder_secret: string }>()
  const [delayForm] = Form.useForm<{ otp_auto_resend_delay_seconds: number }>()
  const [parseForm] = Form.useForm<{ raw: string }>()
  const [parseResult, setParseResult] = useState<Record<string, unknown> | null>(null)

  const webhookUrl = useMemo(() => {
    if (!state) return ''
    return `${window.location.origin}${state.webhook_path}`
  }, [state])

  const load = async () => {
    setLoading(true)
    try {
      const data = await apiFetch('/integrations/gopay-otp')
      setState(data)
      delayForm.setFieldsValue({
        otp_auto_resend_delay_seconds: Number(data.otp_auto_resend_delay_seconds ?? 120),
      })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    poolForm.setFieldsValue(emptyPoolItem)
    load().catch((error) => message.error(error.message || '加载失败'))
  }, [])

  const savePhonePool = async (items: PhonePoolItem[]) => {
    setSaving(true)
    try {
      const data = await apiFetch('/integrations/gopay-otp/phone-pool', {
        method: 'PUT',
        body: JSON.stringify({ items }),
      })
      setState(data)
      message.success('手机号池已保存')
    } finally {
      setSaving(false)
    }
  }

  const addPoolItem = async (values: PhonePoolItem) => {
    const next = cleanPoolItem(values)
    if (!next.phone_number) {
      message.warning('手机号不能为空')
      return
    }
    const key = `${next.phone_country_code}:${next.phone_number}`
    const items = state?.phone_pool || []
    if (items.some((item) => `${item.phone_country_code}:${item.phone_number}` === key)) {
      message.warning('手机号池已存在该号码')
      return
    }
    await savePhonePool([...items, next])
    poolForm.setFieldsValue(emptyPoolItem)
  }

  const updatePoolItem = async (record: PhonePoolItem, patch: Partial<PhonePoolItem>) => {
    const key = `${record.phone_country_code}:${record.phone_number}`
    const items = (state?.phone_pool || []).map((item) => (
      `${item.phone_country_code}:${item.phone_number}` === key ? cleanPoolItem({ ...item, ...patch }) : item
    ))
    await savePhonePool(items)
  }

  const removePoolItem = async (record: PhonePoolItem) => {
    const key = `${record.phone_country_code}:${record.phone_number}`
    await savePhonePool((state?.phone_pool || []).filter((item) => `${item.phone_country_code}:${item.phone_number}` !== key))
  }

  const saveSecret = async (values: { smsforwarder_secret: string }) => {
    const data = await apiFetch('/integrations/gopay-otp/settings', {
      method: 'PUT',
      body: JSON.stringify({ smsforwarder_secret: values.smsforwarder_secret || '' }),
    })
    setState(data)
    secretForm.resetFields()
    message.success('SmsForwarder Secret 已更新')
  }

  const clearSecret = async () => {
    const data = await apiFetch('/integrations/gopay-otp/settings', {
      method: 'PUT',
      body: JSON.stringify({ clear_secret: true }),
    })
    setState(data)
    message.success('SmsForwarder Secret 已清空')
  }

  const saveAutoResendDelay = async (values: { otp_auto_resend_delay_seconds: number }) => {
    const data = await apiFetch('/integrations/gopay-otp/settings', {
      method: 'PUT',
      body: JSON.stringify({
        otp_auto_resend_delay_seconds: Number(values.otp_auto_resend_delay_seconds || 0),
      }),
    })
    setState(data)
    delayForm.setFieldsValue({
      otp_auto_resend_delay_seconds: Number(data.otp_auto_resend_delay_seconds ?? 120),
    })
    message.success('自动重发延迟已更新')
  }

  const startByUid = async (values: { account_id: number; uid: string; pin?: string; plan?: string; force?: boolean }) => {
    const data = await apiFetch('/integrations/gopay-otp/start-by-uid', {
      method: 'POST',
      body: JSON.stringify({
        account_id: Number(values.account_id),
        uid: values.uid,
        pin: values.pin || '',
        plan: values.plan || 'plus',
        force: Boolean(values.force),
      }),
    })
    message.success(`GoPay 会话已启动：${data.session?.gopay_session_id || ''}`)
    await load()
  }

  const clearSession = async (uid: string) => {
    const data = await apiFetch(`/integrations/gopay-otp/sessions/${encodeURIComponent(uid)}/clear`, {
      method: 'POST',
    })
    setState(data)
    message.success('会话记录已清除')
  }

  const resendOtp = async (uid: string) => {
    const data = await apiFetch(`/integrations/gopay-otp/sessions/${encodeURIComponent(uid)}/resend-otp`, {
      method: 'POST',
    })
    setState(data)
    message.success('GoPay OTP 重发请求已提交')
  }

  const testParse = async (values: { raw: string }) => {
    const data = await apiFetch('/integrations/gopay-otp/test-parse', {
      method: 'POST',
      body: JSON.stringify({ raw: values.raw || '' }),
    })
    setParseResult(data)
  }

  const phonePool = state?.phone_pool || []
  const sessions = state?.sessions || []
  const events = [...(state?.recent_events || [])].sort((a, b) => eventTimeMs(b) - eventTimeMs(a))
  const summary = state?.summary || {}
  const eventSummary = summary.recent_events || {}
  const poolSummary = summary.phone_pool || {}

  const overview = (
    <Row gutter={[16, 16]}>
      <Col xs={24} sm={12} xl={6}>
        <Card>
          <Statistic title="手机号池" value={phonePool.length} suffix={`可用 ${poolSummary.ready || 0}`} />
        </Card>
      </Col>
      <Col xs={24} sm={12} xl={6}>
        <Card>
          <Statistic title="等待 OTP 会话" value={summary.sessions_waiting_otp || 0} suffix={`missing ${summary.sessions_missing || 0}`} />
        </Card>
      </Col>
      <Col xs={24} sm={12} xl={6}>
        <Card>
          <Statistic title="手机号池 ready" value={poolSummary.ready || 0} suffix={`reserved ${poolSummary.reserved || 0}`} />
        </Card>
      </Col>
      <Col xs={24} sm={12} xl={6}>
        <Card>
          <Statistic title="近 1 小时异常" value={(eventSummary.conflict || 0) + (eventSummary.submit_failed || 0) + (eventSummary.ignored || 0)} suffix={`成功 ${eventSummary.submitted || 0}`} />
        </Card>
      </Col>
      <Col xs={24} xl={14}>
        <Card title={<Space><ApiOutlined /> SmsForwarder Webhook</Space>} loading={loading && !state}>
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Alert
              type={state?.secret_enabled ? 'success' : 'warning'}
              showIcon
              message={state?.secret_enabled ? '已启用 SmsForwarder 签名校验' : '尚未设置 Secret，公开 webhook 入口不会校验签名'}
            />
            <Descriptions column={1} size="small">
              <Descriptions.Item label="Webhook Server">
                <Paragraph copyable={{ text: webhookUrl }} style={{ marginBottom: 0 }}>{webhookUrl || '-'}</Paragraph>
              </Descriptions.Item>
              <Descriptions.Item label="请求方式">POST</Descriptions.Item>
              <Descriptions.Item label="webParams 表单">
                <Paragraph copyable={{ text: state?.web_params_form || '' }} style={{ marginBottom: 0 }}>
                  {state?.web_params_form || '-'}
                </Paragraph>
              </Descriptions.Item>
              <Descriptions.Item label="webParams JSON">
                <Paragraph copyable={{ text: state?.web_params_json || '' }} style={{ marginBottom: 0 }}>
                  {state?.web_params_json || '-'}
                </Paragraph>
              </Descriptions.Item>
            </Descriptions>
            <Form form={secretForm} layout="inline" onFinish={saveSecret}>
              <Form.Item name="smsforwarder_secret" style={{ flex: 1 }}>
                <Input.Password prefix={<SafetyOutlined />} placeholder="SmsForwarder webhook secret" />
              </Form.Item>
              <Button htmlType="submit" type="primary" icon={<SaveOutlined />}>保存 Secret</Button>
              <Button danger onClick={clearSecret}>清空</Button>
            </Form>
            <Form form={delayForm} layout="inline" onFinish={saveAutoResendDelay}>
              <Form.Item name="otp_auto_resend_delay_seconds" label="自动重发延迟" style={{ marginBottom: 0 }}>
                <InputNumber min={0} max={3600} precision={0} addonAfter="秒" />
              </Form.Item>
              <Button htmlType="submit" icon={<SaveOutlined />}>保存</Button>
            </Form>
          </Space>
        </Card>
      </Col>
      <Col xs={24} xl={10}>
        <Card title={<Space><PlayCircleOutlined /> 按 UID 启动 GoPay</Space>}>
          <Form form={startForm} layout="vertical" onFinish={startByUid}>
            <Row gutter={12}>
              <Col span={12}>
                <Form.Item name="account_id" label="ChatGPT Account ID" rules={[{ required: true }]}>
                  <InputNumber min={1} style={{ width: '100%' }} />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item name="uid" label="UID" rules={[{ required: true }]}>
                  <Input placeholder="99910283" />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item name="pin" label="GoPay PIN">
                  <Input.Password placeholder="可选，留空则用默认值" />
                </Form.Item>
              </Col>
              <Col span={8}>
                <Form.Item name="plan" label="Plan" initialValue="plus">
                  <Input />
                </Form.Item>
              </Col>
              <Col span={4}>
                <Form.Item name="force" label="覆盖" valuePropName="checked">
                  <Switch />
                </Form.Item>
              </Col>
            </Row>
            <Button type="primary" htmlType="submit" icon={<PlayCircleOutlined />}>启动支付</Button>
          </Form>
        </Card>
      </Col>
    </Row>
  )

  return (
    <Space direction="vertical" size={16} style={responsiveContainerStyle}>
      <div style={pageHeaderStyle}>
        <div style={{ minWidth: 0 }}>
          <Title level={3} style={{ marginBottom: 4 }}>GoPay OTP Webhook Adapter</Title>
          <Text type="secondary">手机号池统一管理 UID 绑定、PIN 来源、活跃会话和 SmsForwarder 日志。</Text>
        </div>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新</Button>
      </div>

      <Tabs
        defaultActiveKey="overview"
        style={responsiveContainerStyle}
        items={[
          {
            key: 'overview',
            label: '概览',
            children: overview,
          },
          {
            key: 'phonePool',
            label: '手机号池',
            children: (
              <Card style={responsiveCardStyle}>
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 16 }}
                  message="手机号池是唯一主入口：UID 绑定、手机号状态、GoPay PIN 来源和最近 OTP 都在这里维护。保存后会自动同步底层 UID 绑定配置。"
                />
                <Form form={poolForm} layout="vertical" onFinish={addPoolItem}>
                  <Row gutter={12}>
                    <Col xs={12} md={3}>
                      <Form.Item name="phone_country_code" label="区号">
                        <Input placeholder="86" />
                      </Form.Item>
                    </Col>
                    <Col xs={12} md={5}>
                      <Form.Item name="phone_number" label="手机号" rules={[{ required: true }]}>
                        <Input placeholder="15335521131" />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={4}>
                      <Form.Item name="uid" label="绑定 UID">
                        <Input placeholder="可选" />
                      </Form.Item>
                    </Col>
                    <Col xs={12} md={3}>
                      <Form.Item name="status" label="状态" initialValue="ready">
                        <Input placeholder="ready" />
                      </Form.Item>
                    </Col>
                    <Col xs={12} md={3}>
                      <Form.Item name="pin" label="GoPay PIN">
                        <Input.Password inputMode="numeric" placeholder="可选，手机号专用" />
                      </Form.Item>
                    </Col>
                    <Col xs={12} md={3}>
                      <Form.Item name="label" label="备注">
                        <Input placeholder="设备或用途" />
                      </Form.Item>
                    </Col>
                    <Col xs={24} md={4}>
                      <Form.Item label=" " colon={false}>
                        <Button type="primary" htmlType="submit" loading={saving} block>加入手机号池</Button>
                      </Form.Item>
                    </Col>
                  </Row>
                </Form>
                <Table
                  size="small"
                  rowKey={(record) => `${record.phone_country_code}:${record.phone_number}`}
                  dataSource={phonePool}
                  pagination={{ pageSize: 10, responsive: true }}
                  scroll={{ x: 1180 }}
                  columns={[
                    { title: '手机号', width: 150, render: (_, record: PhonePoolItem) => compactText(phoneText(record), 130) },
                    { title: '绑定 UID', dataIndex: 'uid', width: 120, render: (value: string) => compactText(value, 100) },
                    { title: '状态', width: 110, render: (_, record: PhonePoolItem) => <Tag color={statusColor(displayStatus(record))}>{displayStatus(record)}</Tag> },
                    { title: 'PIN来源', width: 120, render: (_, record: PhonePoolItem) => <Tag color={record.has_pin ? 'green' : record.pin_source === '未配置' ? 'red' : 'gold'}>{record.pin_source || (record.has_pin ? '手机号PIN' : '未配置')}</Tag> },
                    { title: '设备/备注', width: 160, responsive: ['md'], render: (_, record: PhonePoolItem) => compactText(record.device || record.label || record.note || '-', 140) },
                    { title: '最近账号', dataIndex: 'last_used_account_id', width: 100, responsive: ['lg'] },
                    { title: '最近OTP', width: 220, responsive: ['md'], render: (_, record: PhonePoolItem) => compactText(record.last_otp ? `${record.last_otp} / ${formatBeijingTime(record.last_otp_at)}` : '-', 200) },
                    { title: 'Session', dataIndex: 'gopay_session_id', width: 180, responsive: ['xl'], render: (value: string) => compactText(value, 160) },
                    { title: '最近错误', dataIndex: 'last_error', width: 180, responsive: ['lg'], render: (value: string) => compactText(value, 160) },
                    {
                      title: '操作',
                      width: 210,
                      fixed: 'right',
                      render: (_, record: PhonePoolItem) => (
                        <Space size={4} wrap>
                          <Button size="small" onClick={() => updatePoolItem(record, { status: 'ready' })}>释放</Button>
                          <Button size="small" onClick={() => updatePoolItem(record, { enabled: !record.enabled })}>{record.enabled === false ? '启用' : '停用'}</Button>
                          <Button size="small" danger onClick={() => updatePoolItem(record, { status: 'invalid' })}>无效</Button>
                          <Popconfirm title="删除这个手机号？" onConfirm={() => removePoolItem(record)}>
                            <Button size="small" icon={<DeleteOutlined />} />
                          </Popconfirm>
                        </Space>
                      ),
                    },
                  ]}
                />
              </Card>
            ),
          },
          {
            key: 'sessions',
            label: '会话监控',
            children: (
              <Card style={responsiveCardStyle}>
                <Table
                  size="small"
                  rowKey="uid"
                  dataSource={sessions}
                  pagination={{ pageSize: 10, responsive: true }}
                  scroll={{ x: 1080 }}
                  columns={[
                    { title: 'UID', dataIndex: 'uid', width: 120, render: (value: string) => compactText(value, 100) },
                    { title: 'Account ID', dataIndex: 'account_id', width: 100, responsive: ['md'] },
                    { title: 'Session', dataIndex: 'gopay_session_id', width: 180, responsive: ['xl'], render: (value: string) => compactText(value, 160) },
                    { title: '手机号', width: 150, render: (_, record: ActiveSession) => compactText(phoneText(record), 130) },
                    { title: '阶段', width: 130, render: (_, record: ActiveSession) => <Tag color={statusColor(displayPhase(record))}>{displayPhase(record)}</Tag> },
                    { title: 'PIN来源', dataIndex: 'pin_source', width: 120, responsive: ['lg'], render: (value: string) => compactText(value, 100) },
                    { title: '执行中', dataIndex: 'active_action', width: 130, responsive: ['md'], render: (value: string) => compactText(value || '-', 110) },
                    { title: '错误', dataIndex: 'last_error', width: 190, responsive: ['lg'], render: (value: string) => compactText(value, 170) },
                    { title: '更新时间', width: 170, render: (_, record: ActiveSession) => compactText(formatBeijingTime(record.updated_at), 150) },
                    {
                      title: '操作',
                      width: 120,
                      fixed: 'right',
                      render: (_, record: ActiveSession) => (
                        <Space size={4}>
                          <Tooltip title="重发 OTP">
                            <Button
                              size="small"
                              icon={<ReloadOutlined />}
                              disabled={record.phase !== 'waiting_otp'}
                              onClick={() => resendOtp(record.uid)}
                            />
                          </Tooltip>
                          <Popconfirm title="只清除本地 UID 会话记录，不取消 GoPay 流程。" onConfirm={() => clearSession(record.uid)}>
                            <Tooltip title="清除记录">
                              <Button size="small" danger icon={<DeleteOutlined />} />
                            </Tooltip>
                          </Popconfirm>
                        </Space>
                      ),
                    },
                  ]}
                />
              </Card>
            ),
          },
          {
            key: 'events',
            label: 'Webhook日志',
            children: (
              <Card style={responsiveCardStyle}>
                <Table
                  size="small"
                  rowKey="event_id"
                  dataSource={events}
                  pagination={{ pageSize: 12, responsive: true }}
                  scroll={{ x: 980 }}
                  columns={[
                    {
                      title: '时间',
                      dataIndex: 'received_at',
                      width: 180,
                      render: (value: string) => formatBeijingTime(value),
                      sorter: (a: RecentEvent, b: RecentEvent) => eventTimeMs(a) - eventTimeMs(b),
                      defaultSortOrder: 'descend',
                    },
                    { title: '类型', width: 120, render: (_, record: RecentEvent) => <Tag>{record.type || '-'}</Tag> },
                    { title: 'UID', dataIndex: 'uid', width: 120, render: (value: string) => compactText(value, 100) },
                    { title: '手机号', width: 150, render: (_, record: RecentEvent) => compactText(phoneText(record), 130) },
                    { title: 'OTP', dataIndex: 'otp', width: 90 },
                    { title: '状态', width: 140, render: (_, record: RecentEvent) => <Tag color={statusColor(displayStatus(record))}>{displayStatus(record)}</Tag> },
                    { title: '详情', dataIndex: 'detail', width: 220, responsive: ['md'], render: (value: string) => compactText(value, 200) },
                    {
                      title: '操作',
                      width: 90,
                      fixed: 'right',
                      render: (_, record: RecentEvent) => (
                        <Tooltip title="查看详情">
                          <Button size="small" icon={<LinkOutlined />} onClick={() => setActiveEvent(record)} />
                        </Tooltip>
                      ),
                    },
                  ]}
                />
              </Card>
            ),
          },
          {
            key: 'templates',
            label: '模板配置',
            children: (
              <Row gutter={[16, 16]}>
                <Col xs={24} xl={12}>
                  <Card title="GOPAY_BIND 模板">
                    <Paragraph type="secondary">
                      绑定模板用于把通知 UID 与手机号写入手机号池。MSG 中只要能解析出手机号，就会自动建立 UID 与手机号的一对一关系。
                    </Paragraph>
                    <TextArea value={state?.bind_message_template || ''} rows={9} readOnly style={{ fontFamily: 'monospace' }} />
                  </Card>
                </Col>
                <Col xs={24} xl={12}>
                  <Card title="GOPAY_OTP 模板">
                    <Paragraph type="secondary">
                      OTP 模板用于接收 GoPay 验证码。系统会按 UID 找到手机号，再匹配当前等待 OTP 的 GoPay 会话。
                    </Paragraph>
                    <TextArea value={state?.message_template || ''} rows={9} readOnly style={{ fontFamily: 'monospace' }} />
                  </Card>
                </Col>
                <Col xs={24}>
                  <Card title="字段中文说明">
                    <Descriptions bordered size="small" column={{ xs: 1, md: 2 }}>
                      <Descriptions.Item label="TYPE">消息类型，推荐使用 GOPAY_BIND 或 GOPAY_OTP。</Descriptions.Item>
                      <Descriptions.Item label="UID">Android 应用 UID，用来定位手机号池中的绑定手机号。</Descriptions.Item>
                      <Descriptions.Item label="PKG">通知包名，用来辅助核对是否来自预期应用。</Descriptions.Item>
                      <Descriptions.Item label="TITLE">通知标题，例如 GoPay。</Descriptions.Item>
                      <Descriptions.Item label="MSG_BEGIN/MSG_END">通知正文边界，避免多行验证码内容被解析错。</Descriptions.Item>
                      <Descriptions.Item label="DEVICE">来源设备名，例如小米11pro。</Descriptions.Item>
                    </Descriptions>
                  </Card>
                </Col>
                <Col xs={24}>
                  <Card title="解析测试">
                    <Form form={parseForm} layout="vertical" onFinish={testParse}>
                      <Form.Item name="raw" label="SmsForwarder 原始内容">
                        <TextArea rows={8} placeholder="粘贴转发内容，测试 type / uid / phone / otp 解析结果" />
                      </Form.Item>
                      <Button htmlType="submit" type="primary">测试解析</Button>
                    </Form>
                    {parseResult ? (
                      <pre style={{ marginTop: 16, whiteSpace: 'pre-wrap' }}>
                        {JSON.stringify(parseResult, null, 2)}
                      </pre>
                    ) : null}
                  </Card>
                </Col>
              </Row>
            ),
          },
        ]}
      />

      <Drawer
        title="Webhook 事件详情"
        width={680}
        open={Boolean(activeEvent)}
        onClose={() => setActiveEvent(null)}
      >
        {activeEvent ? (
          <Descriptions column={1} bordered size="small">
            <Descriptions.Item label="时间">{formatBeijingTime(activeEvent.received_at)}</Descriptions.Item>
            <Descriptions.Item label="类型">{activeEvent.type || '-'}</Descriptions.Item>
            <Descriptions.Item label="状态"><Tag color={statusColor(displayStatus(activeEvent))}>{displayStatus(activeEvent)}</Tag></Descriptions.Item>
            <Descriptions.Item label="UID">{activeEvent.uid || '-'}</Descriptions.Item>
            <Descriptions.Item label="手机号">{phoneText(activeEvent)}</Descriptions.Item>
            <Descriptions.Item label="OTP">{activeEvent.otp || '-'}</Descriptions.Item>
            <Descriptions.Item label="包名">{activeEvent.package_name || '-'}</Descriptions.Item>
            <Descriptions.Item label="标题">{activeEvent.title || '-'}</Descriptions.Item>
            <Descriptions.Item label="设备">{activeEvent.device || '-'}</Descriptions.Item>
            <Descriptions.Item label="Session">{activeEvent.gopay_session_id || '-'}</Descriptions.Item>
            <Descriptions.Item label="详情">{activeEvent.detail || '-'}</Descriptions.Item>
            <Descriptions.Item label="内容">{activeEvent.message || '-'}</Descriptions.Item>
            <Descriptions.Item label="原始内容">{activeEvent.raw_excerpt || '-'}</Descriptions.Item>
          </Descriptions>
        ) : null}
      </Drawer>
    </Space>
  )
}
