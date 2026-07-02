import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Card, Col, Empty, Row, Space, Spin, Tag, Typography } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'

const { Text } = Typography

const PRIMARY_STATUS_KEYS = ['registered', 'pending_payment', 'subscribed', 'invalid']

const STATUS_META: Record<string, {
  label: string
  color: string
}> = {
  registered: {
    label: '已注册',
    color: 'default',
  },
  pending_payment: {
    label: '待支付',
    color: 'processing',
  },
  subscribed: {
    label: '已订阅',
    color: 'success',
  },
  invalid: {
    label: '无效',
    color: 'error',
  },
}

const HEALTH_STATUS_COLORS: Record<string, string> = {
  healthy: 'success',
  warning: 'warning',
  error: 'error',
  unknown: 'default',
}

const HEALTH_STATUS_LABELS: Record<string, string> = {
  healthy: '正常',
  warning: '注意',
  error: '异常',
  unknown: '未启用',
}

type CountMap = Record<string, number>

interface AccountStats {
  total?: number
  by_platform?: CountMap
  by_status?: CountMap
}

interface SystemHealthResource {
  key: string
  title: string
  status: string
  message?: string
  action_path?: string
}

interface SystemHealth {
  generated_at?: string
  summary?: Record<string, number>
  resources?: SystemHealthResource[]
}

function getErrorMessage(error: unknown) {
  return error instanceof Error ? error.message : '读取数据失败'
}

export default function Dashboard() {
  const [stats, setStats] = useState<AccountStats | null>(null)
  const [health, setHealth] = useState<SystemHealth | null>(null)
  const [loading, setLoading] = useState(false)
  const [healthLoading, setHealthLoading] = useState(false)
  const [error, setError] = useState('')
  const [healthError, setHealthError] = useState('')

  const loadStats = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const data = await apiFetch('/accounts/stats') as AccountStats
      setStats(data)
    } catch (err) {
      setError(getErrorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [])

  const loadHealth = useCallback(async () => {
    setHealthLoading(true)
    setHealthError('')
    try {
      const data = await apiFetch('/system/health') as SystemHealth
      setHealth(data)
    } catch (err) {
      setHealthError(getErrorMessage(err))
    } finally {
      setHealthLoading(false)
    }
  }, [])

  const load = useCallback(async () => {
    await Promise.all([loadStats(), loadHealth()])
  }, [loadHealth, loadStats])

  useEffect(() => {
    void load()
  }, [load])

  const totalAccounts = Number(stats?.total || 0)
  const healthResources = health?.resources || []
  const healthSummary = health?.summary || {}
  const statusCardBackground = 'rgba(99, 102, 241, 0.08)'
  const statusCardBorder = 'rgba(99, 102, 241, 0.22)'
  const statusSummaryItems = [
    { key: 'total', label: '账号总数', value: totalAccounts, tagColor: '' },
    ...PRIMARY_STATUS_KEYS.map((status) => ({
      key: status,
      label: STATUS_META[status]?.label || status,
      value: Number(stats?.by_status?.[status] || 0),
      tagColor: STATUS_META[status]?.color || 'default',
    })),
  ]

  return (
    <div style={{ padding: 0 }}>
      <div style={{ marginBottom: 24, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <h1 style={{ fontSize: 24, fontWeight: 'bold', margin: 0 }}>仪表盘</h1>
          <p style={{ color: '#7a8ba3', marginTop: 4 }}>账号状态与运行资源健康</p>
        </div>
        <Button icon={<ReloadOutlined spin={loading || healthLoading} />} onClick={load} loading={loading || healthLoading}>
          刷新
        </Button>
      </div>

      {error ? (
        <Alert
          type="error"
          showIcon
          closable
          message="账号统计加载失败"
          description={error}
          action={<Button size="small" onClick={loadStats}>重试</Button>}
          onClose={() => setError('')}
          style={{ marginBottom: 16 }}
        />
      ) : null}

      <Card>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'flex-start',
            gap: 12,
            flexWrap: 'wrap',
            marginBottom: 16,
          }}
        >
          <div>
            <Text strong style={{ fontSize: 16 }}>状态分布</Text>
            <Text type="secondary" style={{ display: 'block', marginTop: 4 }}>
              只保留账号总数和核心账号状态数量。
            </Text>
          </div>
        </div>

        {loading && !stats ? (
          <div style={{ textAlign: 'center', padding: 40 }}>
            <Spin />
          </div>
        ) : stats ? (
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(112px, 1fr))',
              gap: 12,
              overflowX: 'hidden',
              paddingBottom: 2,
            }}
          >
            {statusSummaryItems.map((item) => (
              <div
                key={item.key}
                style={{
                  minWidth: 0,
                  padding: '14px 16px',
                  borderRadius: 12,
                  border: `1px solid ${statusCardBorder}`,
                  background: statusCardBackground,
                }}
              >
                <Space size={6} wrap>
                  {item.tagColor ? <Tag color={item.tagColor}>{item.label}</Tag> : <Text type="secondary">{item.label}</Text>}
                </Space>
                <Text strong style={{ display: 'block', marginTop: 8, fontSize: 26, lineHeight: 1.1 }}>
                  {item.value}
                </Text>
              </div>
            ))}
          </div>
        ) : error ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="统计不可用，点击上方重试" />
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无状态统计" />
        )}
      </Card>

      {healthError ? (
        <Alert
          type="warning"
          showIcon
          closable
          message="运行资源健康读取失败"
          description={healthError}
          action={<Button size="small" onClick={loadHealth}>重试</Button>}
          onClose={() => setHealthError('')}
          style={{ marginTop: 16 }}
        />
      ) : null}

      <Card
        title="运行资源健康"
        extra={
          <Space size={6} wrap>
            <Tag color="success">正常 {healthSummary.healthy || 0}</Tag>
            <Tag color="warning">注意 {healthSummary.warning || 0}</Tag>
            <Tag color="error">异常 {healthSummary.error || 0}</Tag>
            <Tag>未启用 {healthSummary.unknown || 0}</Tag>
          </Space>
        }
        style={{ marginTop: 16 }}
      >
        {healthLoading && !health ? (
          <div style={{ textAlign: 'center', padding: 40 }}>
            <Spin />
          </div>
        ) : healthResources.length ? (
          <Row gutter={[12, 12]}>
            {healthResources.map((resource) => (
              <Col xs={24} sm={12} lg={8} xl={6} key={resource.key}>
                <div
                  style={{
                    minHeight: 112,
                    padding: 14,
                    border: '1px solid rgba(128,128,128,0.24)',
                    borderRadius: 10,
                    display: 'flex',
                    flexDirection: 'column',
                    justifyContent: 'space-between',
                    gap: 10,
                  }}
                >
                  <div>
                    <Space style={{ width: '100%', justifyContent: 'space-between' }} align="start">
                      <Text strong>{resource.title}</Text>
                      <Tag color={HEALTH_STATUS_COLORS[resource.status] || 'default'}>
                        {HEALTH_STATUS_LABELS[resource.status] || resource.status}
                      </Tag>
                    </Space>
                    <Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
                      {resource.message || '-'}
                    </Text>
                  </div>
                  {resource.action_path ? (
                    <Button
                      size="small"
                      type="link"
                      style={{ padding: 0, alignSelf: 'flex-start' }}
                      onClick={() => {
                        window.location.href = resource.action_path || '/'
                      }}
                    >
                      去处理
                    </Button>
                  ) : null}
                </div>
              </Col>
            ))}
          </Row>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无资源健康数据" />
        )}
      </Card>
    </div>
  )
}
