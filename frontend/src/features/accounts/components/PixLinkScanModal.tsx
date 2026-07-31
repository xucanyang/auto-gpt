import { Alert, Button, Collapse, Modal, Skeleton, Space, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { DeleteOutlined, ReloadOutlined } from '@ant-design/icons'

const { Text } = Typography

export type PixLinkCleanupMode = 'valid' | 'paid' | 'expired' | 'cancelled' | 'unknown'
export type PaymentLinkScanType =
  | 'hosted'
  | 'paypal'
  | 'ideal'
  | 'upi'
  | 'pix'
  | 'twint'
  | 'kakao_pay'
  | 'team'
  | 'other'
export type PaymentLinkCleanupType = PaymentLinkScanType

export type PixLinkScanReport = {
  instance_id?: string
  now?: string
  cleanup_mode?: PixLinkCleanupMode
  cleanup_label?: string
  cutoff_display?: string
  total_links?: number
  current_payment_links?: number
  current_pix_links?: number
  current_upi_links?: number
  valid_links?: number
  expired_links?: number
  paid_links?: number
  cancelled_links?: number
  unknown_links?: number
  eligible_links?: number
  retained_links?: number
  active_links?: number
  missing_expiry_links?: number
  unknown_missing_expiry_links?: number
  direct_scan_source?: string
  direct_scan_supported_links?: number
  direct_scan_attempted_links?: number
  direct_scan_success_links?: number
  direct_scan_fallback_links?: number
  direct_scan_state_counts?: Record<string, number>
  cleaned_links?: number
  concurrent_skipped_links?: number
  payment_type_counts?: Partial<Record<PaymentLinkScanType, number>>
  payment_types?: PaymentLinkScanType[]
  payment_types_with_links?: PaymentLinkScanType[]
  upi_qr_expiry_links?: number
  upi_qr_validity_seconds?: number
  ideal_derived_expiry_links?: number
  ideal_validity_seconds?: number
  team_derived_expiry_links?: number
  team_validity_seconds?: number
  [key: string]: unknown
}

type PixLinkScanRow = {
  key: PixLinkCleanupMode
  label: string
  color?: string
  count: number
  cleanupMode: PixLinkCleanupMode | null
}

type ScanSection = {
  type: PaymentLinkScanType
  label: string
  note?: string
  cleanupModes: PixLinkCleanupMode[]
}

const ALL_PAYMENT_LINK_CLEANUP_MODES: PixLinkCleanupMode[] = [
  'valid',
  'paid',
  'expired',
  'cancelled',
  'unknown',
]

const PAYMENT_LINK_SCAN_SECTIONS: ScanSection[] = [
  { type: 'hosted', label: 'Hosted Checkout', cleanupModes: ALL_PAYMENT_LINK_CLEANUP_MODES },
  { type: 'paypal', label: 'PayPal', cleanupModes: ALL_PAYMENT_LINK_CLEANUP_MODES },
  { type: 'ideal', label: 'iDEAL', note: '提取后 15 分钟到期', cleanupModes: ALL_PAYMENT_LINK_CLEANUP_MODES },
  { type: 'upi', label: 'UPI', note: 'QR 有效期 5 分钟', cleanupModes: ALL_PAYMENT_LINK_CLEANUP_MODES },
  { type: 'pix', label: 'PIX', cleanupModes: ALL_PAYMENT_LINK_CLEANUP_MODES },
  { type: 'twint', label: 'TWINT', cleanupModes: ALL_PAYMENT_LINK_CLEANUP_MODES },
  { type: 'kakao_pay', label: 'Kakao Pay', cleanupModes: ALL_PAYMENT_LINK_CLEANUP_MODES },
  { type: 'team', label: 'ChatGPT Team', note: '提取后 24 小时到期', cleanupModes: ALL_PAYMENT_LINK_CLEANUP_MODES },
  { type: 'other', label: '其他支付链接', cleanupModes: ALL_PAYMENT_LINK_CLEANUP_MODES },
]

type PixLinkScanModalProps = {
  open: boolean
  report: PixLinkScanReport | null
  loading: boolean
  error: string
  cleanupMode: PixLinkCleanupMode | null
  cleanupPaymentType: PaymentLinkCleanupType | null
  onClose: () => void
  onScan: () => void
  onCleanup: (mode: PixLinkCleanupMode, paymentType: PaymentLinkCleanupType) => void
}

function reportCount(report: PixLinkScanReport | null, key: string, fallback = 0): number {
  const value = report?.[key]
  const count = Number(value ?? fallback)
  return Number.isFinite(count) && count > 0 ? count : 0
}

function paymentTypeTotal(report: PixLinkScanReport | null, paymentType: PaymentLinkScanType): number {
  const legacy = paymentType === 'pix'
    ? Number(report?.current_pix_links || 0)
    : paymentType === 'upi'
      ? Number(report?.current_upi_links || 0)
      : 0
  return reportCount(
    report,
    `${paymentType}_links`,
    Number(report?.payment_type_counts?.[paymentType] || legacy),
  )
}

export function PixLinkScanModal({
  open,
  report,
  loading,
  error,
  cleanupMode,
  cleanupPaymentType,
  onClose,
  onScan,
  onCleanup,
}: PixLinkScanModalProps) {
  const buildRows = (section: ScanSection): PixLinkScanRow[] => {
    const prefix = section.type
    const legacy = section.type === 'pix'
    const cleanupModeFor = (mode: PixLinkCleanupMode) => (
      section.cleanupModes.includes(mode) ? mode : null
    )
    return [
      {
        key: 'valid',
        label: '有效',
        color: 'success',
        count: reportCount(report, `${prefix}_valid_links`, legacy ? Number(report?.valid_links || 0) : 0),
        cleanupMode: cleanupModeFor('valid'),
      },
      {
        key: 'paid',
        label: '已支付',
        color: 'blue',
        count: reportCount(report, `${prefix}_paid_links`, legacy ? Number(report?.paid_links || 0) : 0),
        cleanupMode: cleanupModeFor('paid'),
      },
      {
        key: 'expired',
        label: '过期',
        color: 'orange',
        count: reportCount(report, `${prefix}_expired_links`, legacy ? Number(report?.expired_links || 0) : 0),
        cleanupMode: cleanupModeFor('expired'),
      },
      {
        key: 'cancelled',
        label: '支付已取消',
        color: 'red',
        count: reportCount(report, `${prefix}_cancelled_links`, legacy ? Number(report?.cancelled_links || 0) : 0),
        cleanupMode: cleanupModeFor('cancelled'),
      },
      {
        key: 'unknown',
        label: '状态未知',
        count: reportCount(report, `${prefix}_unknown_links`),
        cleanupMode: cleanupModeFor('unknown'),
      },
    ]
  }

  const buildColumns = (section: ScanSection): ColumnsType<PixLinkScanRow> => [
    {
      title: '状态',
      dataIndex: 'label',
      width: 144,
      render: (label: string, row) => <Tag color={row.color}>{label}</Tag>,
    },
    {
      title: '链接数',
      dataIndex: 'count',
      width: 88,
      align: 'right',
      render: (count: number) => (
        <Text strong style={{ fontVariantNumeric: 'tabular-nums' }}>{count}</Text>
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 104,
      align: 'right',
      render: (_, row) => {
        const mode = row.cleanupMode
        if (!mode || row.count <= 0) return null
        const paymentType = section.type as PaymentLinkCleanupType
        const isTarget = cleanupMode === mode && cleanupPaymentType === paymentType
        return (
          <Button
            size="small"
            danger
            icon={<DeleteOutlined />}
            disabled={loading || cleanupMode !== null}
            loading={isTarget}
            onClick={() => onCleanup(mode, paymentType)}
          >
            删除
          </Button>
        )
      },
    },
  ]

  const totalLinks = reportCount(
    report,
    'total_links',
    PAYMENT_LINK_SCAN_SECTIONS.reduce((total, section) => total + paymentTypeTotal(report, section.type), 0),
  )
  const unknownTotal = reportCount(report, 'unknown_links')
  const directSupported = reportCount(
    report,
    'direct_scan_supported_links',
    paymentTypeTotal(report, 'pix') + paymentTypeTotal(report, 'upi'),
  )
  const directSuccess = reportCount(report, 'direct_scan_success_links')
  const directFallback = reportCount(report, 'direct_scan_fallback_links')
  const collapseItems = PAYMENT_LINK_SCAN_SECTIONS.map((section) => {
    const total = paymentTypeTotal(report, section.type)
    return {
      key: section.type,
      label: (
        <Space size={8} wrap>
          <Text strong>{section.label}</Text>
          <Text type="secondary" style={{ fontVariantNumeric: 'tabular-nums' }}>{total} 条</Text>
          {section.note ? <Text type="secondary">{section.note}</Text> : null}
        </Space>
      ),
      children: (
        <Table<PixLinkScanRow>
          rowKey="key"
          size="small"
          tableLayout="fixed"
          pagination={false}
          columns={buildColumns(section)}
          dataSource={buildRows(section)}
        />
      ),
    }
  })

  return (
    <Modal
      title="支付链接扫描"
      open={open}
      onCancel={onClose}
      width={640}
      footer={<Button onClick={onClose}>关闭</Button>}
      maskClosable={!loading && cleanupMode === null}
      styles={{ body: { maxHeight: '72vh', overflowY: 'auto' } }}
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
          <Space size={8}>
            <Text type="secondary">当前支付链接</Text>
            <Text strong style={{ fontSize: 18, fontVariantNumeric: 'tabular-nums' }}>
              {report ? totalLinks : '-'}
            </Text>
          </Space>
          <Button icon={<ReloadOutlined />} loading={loading} disabled={cleanupMode !== null} onClick={onScan}>
            重新扫描
          </Button>
        </div>
        {error ? <Alert type="error" showIcon message="扫描失败" description={error} /> : null}
        {loading && !report ? <Skeleton active paragraph={{ rows: 4 }} /> : null}
        {report && directSupported > 0 ? (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
            <Text type="secondary">PIX / UPI Stripe 实时查询</Text>
            <Text strong style={{ fontVariantNumeric: 'tabular-nums' }}>{directSuccess} / {directSupported}</Text>
          </div>
        ) : null}
        {directFallback > 0 ? (
          <Alert
            type="warning"
            showIcon
            message={`${directFallback} 条实时查询未返回可信状态，已按本地证据归类`}
          />
        ) : null}
        {unknownTotal > 0 ? (
          <Alert
            type="info"
            showIcon
            message={`${unknownTotal} 条链接状态暂时无法确认，默认保留；可展开对应类型后人工删除`}
          />
        ) : null}
        {report ? (
          <Collapse
            key={String(report.now || 'payment-link-scan')}
            size="small"
            defaultActiveKey={[]}
            items={collapseItems}
          />
        ) : null}
      </Space>
    </Modal>
  )
}
