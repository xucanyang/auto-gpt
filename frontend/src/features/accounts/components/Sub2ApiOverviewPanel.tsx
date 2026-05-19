import { Button, Modal, Space, Tag, Typography } from 'antd'
import { ReloadOutlined, SyncOutlined, UploadOutlined } from '@ant-design/icons'

type Sub2ApiOverview = {
  exists: number
  notFound: number
  crossWorkspace: number
  deletedExact: number
  ambiguous: number
  unreachable: number
  unknown: number
  pending: number
}

type Sub2ApiOverviewPanelProps = {
  accountsCount: number
  overview: Sub2ApiOverview
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

export function Sub2ApiOverviewPanel({
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
}: Sub2ApiOverviewPanelProps) {
  return (
    <div
      style={{
        display: 'flex',
        flexWrap: 'wrap',
        gap: 8,
        alignItems: 'center',
        padding: '8px 10px',
        borderRadius: 8,
        border: '1px solid rgba(127,127,127,0.18)',
        background: 'rgba(255,255,255,0.02)',
        marginBottom: 16,
        justifyContent: 'space-between',
      }}
    >
      <Space wrap size={[8, 8]}>
        <Text strong style={{ fontSize: 13 }}>Sub2API 远端概览</Text>
        <Tag color="success">已存在 {overview.exists}</Tag>
        <Tag>未发现 {overview.notFound}</Tag>
        <Tag color="processing">其他工作区已存在 {overview.crossWorkspace}</Tag>
        <Tag color="warning">已删可重传 {overview.deletedExact}</Tag>
        <Tag color="warning">多候选 {overview.ambiguous}</Tag>
        <Tag color="error">不可达 {overview.unreachable}</Tag>
        <Tag>未同步 {overview.unknown}</Tag>
        <Tag color="processing">待补传 {overview.pending}</Tag>
        {syncing ? <Tag color="processing">正在自动刷新</Tag> : null}
        <Text type="secondary" style={{ fontSize: 12 }}>基于当前列表 {accountsCount} 个账号</Text>
      </Space>
      <Space wrap size={8}>
        <Button
          size="small"
          icon={<ReloadOutlined spin={statusSyncLoading === 'sub2api_all' || syncing} />}
          loading={statusSyncLoading === 'sub2api_all'}
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
          onClick={() => {
            Modal.confirm({
              title:
                scope === 'selected'
                  ? `确认补传所选 ${selectedCount} 个账号到 Sub2API？`
                  : `确认补传当前筛选范围内 ${pendingCount} 个 Sub2API 待补传账号？`,
              onOk: () => onUpload(),
            })
          }}
        >
          {uploadLoading
            ? (scope === 'selected' ? `上传所选中... (${selectedCount})` : `上传待补传中... (${pendingCount})`)
            : (scope === 'selected' ? `上传所选 (${selectedCount})` : `上传待补传 (${pendingCount})`)}
        </Button>
      </Space>
    </div>
  )
}
