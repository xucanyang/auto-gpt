import { Alert, Button, Modal, Space, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { DeleteOutlined, ReloadOutlined } from '@ant-design/icons'

const { Text } = Typography

export type PixLinkCleanupMode = 'expired' | 'paid' | 'cancelled'
export type PaymentLinkScanType = 'pix' | 'upi'

export type PixLinkScanReport = {
  instance_id?: string
  cleanup_mode?: PixLinkCleanupMode
  cleanup_label?: string
  cutoff_display?: string
  current_pix_links?: number
  valid_links?: number
  expired_links?: number
  paid_links?: number
  cancelled_links?: number
  eligible_links?: number
  retained_links?: number
  active_links?: number
  missing_expiry_links?: number
  valid_missing_expiry_links?: number
  direct_scan_source?: string
  direct_scan_attempted_links?: number
  direct_scan_success_links?: number
  direct_scan_fallback_links?: number
  direct_scan_state_counts?: Record<string, number>
  cleaned_links?: number
  concurrent_skipped_links?: number
  current_upi_links?: number
  payment_type_counts?: Record<string, number>
  payment_types?: string[]
  upi_qr_expiry_links?: number
  pix_valid_links?: number
  pix_expired_links?: number
  pix_paid_links?: number
  pix_cancelled_links?: number
  upi_valid_links?: number
  upi_expired_links?: number
  upi_paid_links?: number
  upi_cancelled_links?: number
}

type PixLinkScanRow = {
  key: 'valid' | PixLinkCleanupMode
  label: string
  color: string
  count: number
  cleanupMode: PixLinkCleanupMode | null
}

type PixLinkScanModalProps = {
  open: boolean
  report: PixLinkScanReport | null
  loading: boolean
  error: string
  cleanupMode: PixLinkCleanupMode | null
  onClose: () => void
  onScan: () => void
  onCleanup: (mode: PixLinkCleanupMode, paymentType: PaymentLinkScanType) => void
}

export function PixLinkScanModal({
  open,
  report,
  loading,
  error,
  cleanupMode,
  onClose,
  onScan,
  onCleanup,
}: PixLinkScanModalProps) {
  const buildRows = (paymentType: PaymentLinkScanType): PixLinkScanRow[] => {
    const prefix = paymentType === 'upi' ? 'upi' : 'pix'
    const legacy = paymentType === 'pix'
    return [
      { key: 'valid', label: '有效', color: 'success', count: Number(report?.[`${prefix}_valid_links` as keyof PixLinkScanReport] ?? (legacy ? report?.valid_links : 0) ?? 0), cleanupMode: null },
      { key: 'paid', label: '已支付', color: 'blue', count: Number(report?.[`${prefix}_paid_links` as keyof PixLinkScanReport] ?? (legacy ? report?.paid_links : 0) ?? 0), cleanupMode: 'paid' },
      { key: 'expired', label: '过期', color: 'orange', count: Number(report?.[`${prefix}_expired_links` as keyof PixLinkScanReport] ?? (legacy ? report?.expired_links : 0) ?? 0), cleanupMode: 'expired' },
      { key: 'cancelled', label: '支付已取消', color: 'red', count: Number(report?.[`${prefix}_cancelled_links` as keyof PixLinkScanReport] ?? (legacy ? report?.cancelled_links : 0) ?? 0), cleanupMode: 'cancelled' },
    ]
  }
  const columns: ColumnsType<PixLinkScanRow> = [
    {
      title: '状态',
      dataIndex: 'label',
      width: 132,
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
        if (!mode) return null
        return (
          <Button
            size="small"
            danger
            icon={<DeleteOutlined />}
            disabled={loading || cleanupMode !== null || row.count <= 0}
            loading={cleanupMode === mode}
            onClick={() => onCleanup(mode, 'pix')}
          >
            清理
          </Button>
        )
      },
    },
  ]
  const validMissing = Number(report?.valid_missing_expiry_links || 0)
  const pixTotal = Number(report?.current_pix_links || report?.payment_type_counts?.pix || 0)
  const upiTotal = Number(report?.current_upi_links || report?.payment_type_counts?.upi || 0)
  const showUpi = upiTotal > 0 || report?.payment_types?.includes('upi')
  const directSuccess = Number(report?.direct_scan_success_links || 0)
  const directFallback = Number(report?.direct_scan_fallback_links || 0)

  return (
    <Modal
      title="PIX / UPI 链接扫描"
      open={open}
      onCancel={onClose}
      width={520}
      footer={<Button onClick={onClose}>关闭</Button>}
      maskClosable={!loading && cleanupMode === null}
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
          <Space size={8}>
            <Text type="secondary">当前 PIX / UPI 链接</Text>
            <Text strong style={{ fontSize: 18, fontVariantNumeric: 'tabular-nums' }}>
              {report ? pixTotal + upiTotal : '-'}
            </Text>
          </Space>
          <Button icon={<ReloadOutlined />} loading={loading} disabled={cleanupMode !== null} onClick={onScan}>
            重新扫描
          </Button>
        </div>
        {error ? <Alert type="error" showIcon message="扫描失败" description={error} /> : null}
        {report ? (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
            <Text type="secondary">Stripe 实时查询</Text>
            <Text strong style={{ fontVariantNumeric: 'tabular-nums' }}>{directSuccess} / {pixTotal + upiTotal}</Text>
          </div>
        ) : null}
        {directFallback > 0 ? (
          <Alert
            type="warning"
            showIcon
            message={`${directFallback} 条实时查询失败，已使用本地记录兜底`}
          />
        ) : null}
        {validMissing > 0 ? (
          <Alert
            type="warning"
            showIcon
            message={`${validMissing} 条保留链接缺少有效过期时间`}
          />
        ) : null}
        {(['pix', ...(showUpi ? ['upi'] : [])] as PaymentLinkScanType[]).map((paymentType) => {
          const rows = buildRows(paymentType)
          const total = paymentType === 'upi' ? upiTotal : pixTotal
          return (
            <div key={paymentType}>
              <Space size={8} style={{ marginBottom: 6 }}>
                <Text strong>{paymentType === 'upi' ? 'UPI 链接扫描' : 'PIX 链接扫描'}</Text>
                <Text type="secondary">{total} 条</Text>
                {paymentType === 'upi' ? <Text type="secondary">有效期 5 分钟，以 qr_code.expires_at 为准</Text> : null}
              </Space>
              <Table<PixLinkScanRow>
                rowKey="key"
                size="small"
                tableLayout="fixed"
                pagination={false}
                loading={loading && !report}
                columns={columns.map((column) => column.key === 'action'
                  ? { ...column, render: (_, row) => row.cleanupMode ? (
                    <Button
                      size="small"
                      danger
                      icon={<DeleteOutlined />}
                      disabled={loading || cleanupMode !== null || row.count <= 0}
                      loading={cleanupMode === row.cleanupMode}
                      onClick={() => onCleanup(row.cleanupMode as PixLinkCleanupMode, paymentType)}
                    >清理</Button>
                  ) : null }
                  : column)}
                dataSource={rows}
              />
            </div>
          )
        })}
      </Space>
    </Modal>
  )
}
