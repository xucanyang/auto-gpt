import { Button, Space, Table, Tag, Tooltip, Typography, message } from 'antd'
import { CopyOutlined, ExportOutlined } from '@ant-design/icons'

const { Text } = Typography

function statusColor(status: string) {
  if (status === 'success') return 'success'
  if (status === 'skipped') return 'warning'
  if (status === 'failed') return 'error'
  return 'processing'
}

async function copyText(text: string, label: string) {
  const value = String(text || '').trim()
  if (!value) {
    message.warning(`没有可复制的${label}`)
    return
  }
  try {
    await navigator.clipboard.writeText(value)
    message.success(`已复制${label}`)
  } catch {
    message.error(`复制${label}失败`)
  }
}

type ApprovalUrlResultsTableProps = {
  results: any[]
  emptyText?: string
}

function getPaypalUrl(item: any): string {
  return String(item?.paypal_url || item?.approval_url || item?.provider_redirect_url || item?.long_url || '').trim()
}

export function ApprovalUrlResultsTable({
  results,
  emptyText = '任务运行后，这里会按账号展示上游返回的 paypalUrl/approvalUrl 和相关数据。',
}: ApprovalUrlResultsTableProps) {
  const rows = Array.isArray(results) ? results : []
  const successRows = rows.filter((item) => String(item?.status || '') === 'success' && getPaypalUrl(item))
  const allApprovalUrls = successRows.map((item) => getPaypalUrl(item)).filter(Boolean)
  const accountMarkedLines = successRows.map((item) => {
    const email = String(item?.email || item?.account_id || '').trim() || '-'
    return `${email}\t${getPaypalUrl(item)}`
  })

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={8}>
      <Space wrap>
        <Tag color="success">成功 {successRows.length}</Tag>
        <Tag color="error">失败 {rows.filter((item) => String(item?.status || '') === 'failed').length}</Tag>
        <Button
          size="small"
          icon={<CopyOutlined />}
          disabled={allApprovalUrls.length === 0}
          onClick={() => copyText(allApprovalUrls.join('\n'), 'approvalUrl')}
        >
          复制全部 URL
        </Button>
        <Button
          size="small"
          icon={<CopyOutlined />}
          disabled={accountMarkedLines.length === 0}
          onClick={() => copyText(accountMarkedLines.join('\n'), '账号标记结果')}
        >
          复制账号+URL
        </Button>
      </Space>

      {rows.length > 0 ? (
        <Table
          size="small"
          rowKey={(item: any, index) => `${item?.account_id || 'account'}-${index}`}
          pagination={false}
          dataSource={rows}
          columns={[
            {
              title: '账号',
              dataIndex: 'email',
              width: 230,
              render: (value: string, record: any) => {
                const text = String(value || record?.account_id || '').trim()
                return <Text copyable={Boolean(text)}>{text || '-'}</Text>
              },
            },
            {
              title: '状态',
              dataIndex: 'status',
              width: 100,
              render: (value: string) => <Tag color={statusColor(String(value || ''))}>{String(value || '-')}</Tag>,
            },
            {
              title: 'paypalUrl',
              dataIndex: 'paypal_url',
              width: 420,
              render: (_value: string, record: any) => {
                const text = getPaypalUrl(record)
                const canOpen = /^https?:\/\//i.test(text)
                if (!text) return <Text>-</Text>
                return (
                  <Space size={4} style={{ maxWidth: 390 }}>
                    <Text copyable={{ text }} ellipsis={{ tooltip: text }} style={{ maxWidth: 310 }}>
                      {text}
                    </Text>
                    <Tooltip title={canOpen ? '打开 approvalUrl' : '不是可打开的链接'}>
                      <Button
                        size="small"
                        type="link"
                        icon={<ExportOutlined />}
                        disabled={!canOpen}
                        href={canOpen ? text : undefined}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        打开
                      </Button>
                    </Tooltip>
                  </Space>
                )
              },
            },
            {
              title: '金额',
              dataIndex: 'checkout_amount',
              width: 100,
              render: (value: string) => <Text>{value || '-'}</Text>,
            },
            {
              title: 'cs_id',
              dataIndex: 'cs_id',
              width: 220,
              render: (value: string) => {
                const text = String(value || '').trim()
                return <Text copyable={Boolean(text)} ellipsis={{ tooltip: text }}>{text || '-'}</Text>
              },
            },
            {
              title: 'pm_id',
              dataIndex: 'payment_method_id',
              width: 180,
              render: (value: string) => {
                const text = String(value || '').trim()
                return <Text copyable={Boolean(text)} ellipsis={{ tooltip: text }}>{text || '-'}</Text>
              },
            },
            {
              title: '原因/错误',
              dataIndex: 'reason',
              width: 260,
              render: (value: string, record: any) => {
                const text = String(value || record?.error || record?.provider_error || '').trim()
                return <Text ellipsis={{ tooltip: text }} style={{ maxWidth: 240 }}>{text || '-'}</Text>
              },
            },
          ]}
          scroll={{ x: 1510, y: 300 }}
        />
      ) : (
        <Text type="secondary">{emptyText}</Text>
      )}
    </Space>
  )
}
