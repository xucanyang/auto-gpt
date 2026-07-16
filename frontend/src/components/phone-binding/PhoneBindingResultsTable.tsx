import { Button, Input, Space, Table, Tag, Typography, message } from 'antd'
import { CopyOutlined } from '@ant-design/icons'

const { Text } = Typography

function copyTextToClipboardFallback(text: string) {
  if (typeof document === 'undefined') return false
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', 'true')
  textarea.style.position = 'fixed'
  textarea.style.top = '0'
  textarea.style.left = '-9999px'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.focus()
  textarea.select()
  textarea.setSelectionRange(0, textarea.value.length)
  try {
    return document.execCommand('copy')
  } finally {
    document.body.removeChild(textarea)
  }
}

async function copyTextToClipboard(text: string) {
  if (typeof window !== 'undefined' && typeof navigator !== 'undefined' && navigator.clipboard?.writeText && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 非 HTTPS / 浏览器权限策略 / iframe 场景下会失败，继续走旧复制通道。
    }
  }
  return copyTextToClipboardFallback(text)
}

async function copyText(text: string, label: string) {
  const content = String(text || '').trim()
  if (!content) {
    message.warning(`没有可复制的${label}`)
    return
  }
  try {
    const ok = await copyTextToClipboard(content)
    if (!ok) throw new Error('copy failed')
    message.success(`已复制${label}`)
  } catch (error) {
    console.warn('copy failed', error)
    message.error(`复制${label}失败`)
  }
}

function phoneStatusColor(status: string) {
  if (status === 'registered_phone_signup') return 'success'
  if (status === 'bound') return 'success'
  if (status === 'openai_rejected' || status === 'openai_phone_limit') return 'orange'
  if (status === 'api_no_code' || status === 'api_error') return 'gold'
  if (status === 'not_tested' || status === 'account_phone_bound') return 'default'
  if (status === 'account_auth_error' || status === 'browser_error' || status === 'unknown') return 'error'
  return 'processing'
}

function prefixStatusMeta(status: string) {
  if (status === 'positive_sample' || status === 'available') return { label: '样本成功', color: 'success' }
  if (status === 'negative_sample' || status === 'unavailable') return { label: '样本失败', color: 'error' }
  if (status === 'mixed_sample' || status === 'partial') return { label: '样本混合', color: 'warning' }
  return { label: '未测试', color: 'default' }
}

const phoneResultColumns = [
  {
    title: '手机号',
    dataIndex: 'phone',
    width: 150,
    render: (value: string) => <Text copyable>{value || '-'}</Text>,
  },
  {
    title: '状态',
    dataIndex: 'status',
    width: 130,
    render: (value: string, record: any) => (
      <Tag color={phoneStatusColor(String(value || ''))}>
        {record?.status_label || value || '-'}
      </Tag>
    ),
  },
  {
    title: '接口有效期',
    dataIndex: 'api_expired_date',
    width: 180,
    render: (value: string) => <Text>{value || '-'}</Text>,
  },
  {
    title: '收码 API',
    dataIndex: 'api_url',
    width: 300,
    render: (value: string) => (
      <Text
        copyable={value ? { text: value, tooltips: ['复制 API', '已复制'] } : false}
        ellipsis={{ tooltip: value || '' }}
        style={{ maxWidth: 270, fontFamily: 'monospace', fontSize: 12 }}
      >
        {value || '-'}
      </Text>
    ),
  },
  {
    title: '验证码时间',
    dataIndex: 'code_time',
    width: 180,
    render: (value: string) => <Text>{value || '-'}</Text>,
  },
  {
    title: '绑定账号',
    dataIndex: 'email',
    width: 220,
    render: (value: string) => <Text copyable={Boolean(value)}>{value || '-'}</Text>,
  },
  {
    title: '原因',
    dataIndex: 'reason',
    width: 260,
    render: (value: string) => (
      <Text ellipsis={{ tooltip: value || '' }} style={{ maxWidth: 240 }}>
        {value || '-'}
      </Text>
    ),
  },
]

const prefixSummaryColumns = [
  {
    title: '号段',
    dataIndex: 'prefix',
    width: 90,
    render: (value: string) => <Text copyable>{value || '-'}</Text>,
  },
  {
    title: '样本结果',
    dataIndex: 'status',
    width: 100,
    render: (value: string) => {
      const meta = prefixStatusMeta(String(value || ''))
      return <Tag color={meta.color}>{meta.label}</Tag>
    },
  },
  { title: '抽样', dataIndex: 'selected_count', width: 70 },
  { title: '成功', dataIndex: 'available_count', width: 70 },
  { title: '失败', dataIndex: 'unavailable_count', width: 70 },
  { title: '未测试', dataIndex: 'untested_count', width: 80 },
  {
    title: '抽样号码',
    dataIndex: 'phones',
    width: 300,
    render: (values: unknown) => {
      const phones = Array.isArray(values) ? values.map((value) => String(value || '').trim()).filter(Boolean) : []
      return (
        <Text
          copyable={phones.length > 0 ? { text: phones.join('\n'), tooltips: ['复制号码', '已复制'] } : false}
          ellipsis={{ tooltip: phones.join('、') }}
          style={{ maxWidth: 275 }}
        >
          {phones.join('、') || '-'}
        </Text>
      )
    },
  },
]

type PhoneBindingResultsTableProps = {
  results: any[]
  prefixSummary?: any
  showPrefixSummary?: boolean
  boundPhoneLines?: string[]
  boundPhoneResults?: any[]
  showSuccessfulLines?: boolean
  emptyText?: string
}

export function PhoneBindingResultsTable({
  results,
  prefixSummary,
  showPrefixSummary = true,
  boundPhoneLines = [],
  boundPhoneResults = [],
  showSuccessfulLines = false,
  emptyText = '任务结束后，这里会输出已真实提交验证码并完成绑定的手机号。',
}: PhoneBindingResultsTableProps) {
  const safeResults = Array.isArray(results) ? results : []
  const safeSummary = prefixSummary && typeof prefixSummary === 'object' ? prefixSummary : {}
  const prefixSummaryItems = Array.isArray(safeSummary?.items) ? safeSummary.items : []
  const positiveSamplePrefixes = prefixSummaryItems
    .filter((item: any) => ['positive_sample', 'available'].includes(String(item?.assessment || item?.status || '')))
    .map((item: any) => String(item?.prefix || '').trim())
    .filter(Boolean)
  const negativeSamplePrefixes = Array.isArray(safeSummary?.negative_sample_prefixes)
    ? safeSummary.negative_sample_prefixes.map((value: unknown) => String(value || '').trim()).filter(Boolean)
    : Array.isArray(safeSummary?.unavailable_prefixes)
      ? safeSummary.unavailable_prefixes.map((value: unknown) => String(value || '').trim()).filter(Boolean)
    : []
  const successfulPhones = safeResults
    .filter((item: any) => ['bound', 'registered_phone_signup'].includes(String(item?.status || '')) && String(item?.phone || '').trim())
    .map((item: any) => String(item.phone).trim())
  const successfulResultRawLines = safeResults
    .filter((item: any) => ['bound', 'registered_phone_signup'].includes(String(item?.status || '')))
    .map((item: any) => String(item?.raw_line || '').trim())
    .filter(Boolean)
  const successfulRawLines = boundPhoneResults.length > 0
    ? boundPhoneResults.map((item: any) => String(item?.raw_line || '').trim()).filter(Boolean)
    : boundPhoneLines.length > 0
      ? boundPhoneLines
      : successfulResultRawLines

  const hasPrefixSummary = showPrefixSummary && (prefixSummaryItems.length > 0 || Number(safeSummary?.selected_phone_count || 0) > 0)

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={8}>
      {hasPrefixSummary ? (
        <>
          <Space wrap size={[4, 6]}>
            <Tag color="success">成功样本 {Number(safeSummary?.positive_sample_prefix_count ?? safeSummary?.available_prefix_count ?? 0)}</Tag>
            <Tag color="error">失败样本 {Number(safeSummary?.negative_sample_prefix_count ?? safeSummary?.unavailable_prefix_count ?? 0)}</Tag>
            <Tag color="warning">样本混合 {Number(safeSummary?.mixed_sample_prefix_count ?? safeSummary?.partial_prefix_count ?? 0)}</Tag>
            <Tag>未测试 {Number(safeSummary?.untested_prefix_count || 0)}</Tag>
            <Button
              size="small"
              icon={<CopyOutlined />}
              disabled={positiveSamplePrefixes.length === 0}
              onClick={() => copyText(positiveSamplePrefixes.join(','), '有成功样本的号段（逗号分隔）')}
            >
              复制成功样本号段
            </Button>
            <Button
              size="small"
              icon={<CopyOutlined />}
              disabled={negativeSamplePrefixes.length === 0}
              onClick={() => copyText(negativeSamplePrefixes.join(','), '有失败样本的号段（逗号分隔）')}
            >
              复制失败样本号段
            </Button>
          </Space>
          {prefixSummaryItems.length > 0 ? (
            <Table
              size="small"
              rowKey={(item: any) => String(item?.prefix || '')}
              columns={prefixSummaryColumns}
              dataSource={prefixSummaryItems}
              pagination={false}
              scroll={{ x: 790, y: 220 }}
            />
          ) : null}
        </>
      ) : null}

      {showSuccessfulLines ? (
        successfulRawLines.length > 0 ? (
          <Input.TextArea value={successfulRawLines.join('\n')} autoSize={{ minRows: 2, maxRows: 6 }} readOnly />
        ) : (
          <Text type="secondary">{emptyText}</Text>
        )
      ) : null}

      {showSuccessfulLines ? (
        <Space wrap>
          <Button
            size="small"
            icon={<CopyOutlined />}
            disabled={successfulPhones.length === 0}
            onClick={() => copyText(successfulPhones.join('\n'), '成功手机号')}
          >
            复制成功手机号
          </Button>
          <Button
            size="small"
            icon={<CopyOutlined />}
            disabled={successfulRawLines.length === 0}
            onClick={() => copyText(successfulRawLines.join('\n'), '成功原始行')}
          >
            复制原始行
          </Button>
        </Space>
      ) : null}

      {safeResults.length > 0 ? (
        <Table
          size="small"
          rowKey={(item: any, index) => `${item?.phone || 'phone'}-${item?.email || 'account'}-${index}`}
          columns={phoneResultColumns}
          dataSource={safeResults}
          pagination={false}
          scroll={{ x: 1220, y: 240 }}
        />
      ) : !showSuccessfulLines ? (
        <Text type="secondary">暂无手机号绑定结果</Text>
      ) : null}
    </Space>
  )
}
