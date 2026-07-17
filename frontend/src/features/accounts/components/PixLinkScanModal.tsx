import { Alert, Button, Modal, Space, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { DeleteOutlined, ReloadOutlined } from '@ant-design/icons'

const { Text } = Typography

export type PixLinkCleanupMode = 'expired' | 'paid' | 'cancelled'

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
  onCleanup: (mode: PixLinkCleanupMode) => void
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
  const rows: PixLinkScanRow[] = [
    {
      key: 'valid',
      label: '有效',
      color: 'success',
      count: Number(report?.valid_links || 0),
      cleanupMode: null,
    },
    {
      key: 'paid',
      label: '已支付',
      color: 'blue',
      count: Number(report?.paid_links || 0),
      cleanupMode: 'paid',
    },
    {
      key: 'expired',
      label: '过期',
      color: 'orange',
      count: Number(report?.expired_links || 0),
      cleanupMode: 'expired',
    },
    {
      key: 'cancelled',
      label: '支付已取消',
      color: 'red',
      count: Number(report?.cancelled_links || 0),
      cleanupMode: 'cancelled',
    },
  ]
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
            onClick={() => onCleanup(mode)}
          >
            清理
          </Button>
        )
      },
    },
  ]
  const validMissing = Number(report?.valid_missing_expiry_links || 0)
  const totalLinks = Number(report?.current_pix_links || 0)
  const directSuccess = Number(report?.direct_scan_success_links || 0)
  const directFallback = Number(report?.direct_scan_fallback_links || 0)

  return (
    <Modal
      title="PIX 链接扫描"
      open={open}
      onCancel={onClose}
      width={520}
      footer={<Button onClick={onClose}>关闭</Button>}
      maskClosable={!loading && cleanupMode === null}
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
          <Space size={8}>
            <Text type="secondary">总 PIX 链接</Text>
            <Text strong style={{ fontSize: 18, fontVariantNumeric: 'tabular-nums' }}>
              {report ? totalLinks : '-'}
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
            <Text strong style={{ fontVariantNumeric: 'tabular-nums' }}>{directSuccess} / {totalLinks}</Text>
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
        <Table<PixLinkScanRow>
          rowKey="key"
          size="small"
          tableLayout="fixed"
          pagination={false}
          loading={loading && !report}
          columns={columns}
          dataSource={rows}
        />
      </Space>
    </Modal>
  )
}
