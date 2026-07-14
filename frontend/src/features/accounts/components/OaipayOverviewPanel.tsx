import { Button, Space, Tag, Typography } from 'antd'
import { ReloadOutlined, SyncOutlined, UploadOutlined } from '@ant-design/icons'

type OaipayOverview = {
  exists: number
  notFound: number
  deletedExact: number
  ambiguous: number
  unreachable: number
  unknown: number
  pending: number
}

type OaipayOverviewPanelProps = {
  accountsCount: number
  overview: OaipayOverview
  syncing: boolean
  statusSyncLoading: string
  uploadLoading: boolean
  uploadDisabled: boolean
  pendingCount: number
  scope: 'selected' | 'pending'
  selectedCount: number
  onRefresh: () => Promise<void> | void
  onUpload: () => Promise<void> | void
}

const { Text } = Typography

export function OaipayOverviewPanel({
  accountsCount,
  overview,
  syncing,
  statusSyncLoading,
  uploadLoading,
  uploadDisabled,
  pendingCount,
  scope,
  selectedCount,
  onRefresh,
  onUpload,
}: OaipayOverviewPanelProps) {
  const hasAttention =
    overview.pending > 0 ||
    overview.unreachable > 0 ||
    overview.ambiguous > 0 ||
    syncing ||
    statusSyncLoading === 'oaipay_all'

  return (
    <div
      style={{
        display: 'flex',
        flexWrap: 'wrap',
        gap: 10,
        alignItems: 'center',
        padding: '7px 2px 9px',
        borderTop: '1px solid rgba(127,127,127,0.16)',
        borderBottom: '1px solid rgba(127,127,127,0.16)',
        marginBottom: 10,
        justifyContent: 'space-between',
      }}
    >
      <Space wrap size={[6, 6]}>
        <Text strong style={{ fontSize: 12 }}>OAIPay</Text>
        <Text type="secondary" style={{ fontSize: 12 }}>当前列表 {accountsCount}</Text>
        <Tag color="success" style={{ marginInlineEnd: 0 }}>已存在 {overview.exists}</Tag>
        <Tag color={overview.pending ? 'processing' : 'default'} style={{ marginInlineEnd: 0 }}>
          待补传 {overview.pending}
        </Tag>
        {overview.unreachable ? (
          <Tag color="error" style={{ marginInlineEnd: 0 }}>不可达 {overview.unreachable}</Tag>
        ) : null}
        {overview.ambiguous ? (
          <Tag color="warning" style={{ marginInlineEnd: 0 }}>多候选 {overview.ambiguous}</Tag>
        ) : null}
        {hasAttention ? null : (
          <Text type="secondary" style={{ fontSize: 12 }}>远端状态正常</Text>
        )}
        {syncing ? <Tag color="processing" style={{ marginInlineEnd: 0 }}>自动刷新中</Tag> : null}
      </Space>
      <Space wrap size={6}>
        <Button
          size="small"
          icon={<ReloadOutlined spin={statusSyncLoading === 'oaipay_all' || syncing} />}
          loading={statusSyncLoading === 'oaipay_all'}
          onClick={onRefresh}
        >
          刷新
        </Button>
        <Button
          size="small"
          type="primary"
          icon={uploadLoading ? <SyncOutlined spin /> : <UploadOutlined />}
          loading={uploadLoading}
          disabled={uploadDisabled}
          onClick={() => onUpload()}
        >
          {uploadLoading
            ? (scope === 'selected' ? `上传所选中... (${selectedCount})` : `上传待补传中... (${pendingCount})`)
            : (scope === 'selected' ? `上传所选 (${selectedCount})` : `上传待补传 (${pendingCount})`)}
        </Button>
      </Space>
    </div>
  )
}
