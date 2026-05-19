import { Button, Dropdown, Input, Popconfirm, Select, Space } from 'antd'
import type { MenuProps } from 'antd'
import {
  DeleteOutlined,
  DownloadOutlined,
  LinkOutlined,
  PlusOutlined,
  ReloadOutlined,
  SyncOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import { ActiveTasksPanel } from './ActiveTasksPanel'

type AccountsToolbarProps = {
  search: string
  onSearchChange: (value: string) => void
  filterStatus: string
  onFilterStatusChange: (value: string) => void
  total: number
  accountsCount: number
  selectedRowKeys: React.Key[]
  activeTasksLoading: boolean
  activeTasks: any[]
  onOpenTaskSnapshot: (snapshot: any) => void
  onRefreshActiveTasks: () => Promise<void> | void
  onActiveTasksOpen: () => void
  isChatgptPlatform: boolean
  batchGopayLoading: boolean
  batchPaymentLinkLoading: boolean
  onBatchPaymentLink: () => void
  onOpenBatchGopay: () => void
  onOpenBusinessDeferred: () => void
  deleteInvalidLoading: boolean
  onDeleteInvalid: () => Promise<void> | void
  onBatchDelete: () => Promise<void> | void
  onOpenImport: () => void
  onExportCsv: () => void
  onOpenAdd: () => void
  loading: boolean
  onRefresh: () => Promise<void> | void
  onOpenRegister: () => void
  statusSyncMenuItems: MenuProps['items']
  onStatusSyncClick: MenuProps['onClick']
  statusSyncLoading: string
  resumeAuthMenuItems: MenuProps['items']
  onResumeAuthClick: MenuProps['onClick']
  resumeAuthLoading: string
  backfillMenuItems: MenuProps['items']
  onBackfillClick: MenuProps['onClick']
  backfillLoading: string
}

const STATUS_FILTER_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'registered', label: '已注册' },
  { value: 'pending_payment', label: '待支付' },
  { value: 'payment_failed', label: '支付失败' },
  { value: 'trial', label: '试用中' },
  { value: 'subscribed', label: '已订阅' },
  { value: 'expired', label: '已过期' },
  { value: 'invalid', label: '已失效' },
]

export function AccountsToolbar({
  search,
  onSearchChange,
  filterStatus,
  onFilterStatusChange,
  total,
  accountsCount,
  selectedRowKeys,
  activeTasksLoading,
  activeTasks,
  onOpenTaskSnapshot,
  onRefreshActiveTasks,
  onActiveTasksOpen,
  isChatgptPlatform,
  batchGopayLoading,
  batchPaymentLinkLoading,
  onBatchPaymentLink,
  onOpenBatchGopay,
  onOpenBusinessDeferred,
  deleteInvalidLoading,
  onDeleteInvalid,
  onBatchDelete,
  onOpenImport,
  onExportCsv,
  onOpenAdd,
  loading,
  onRefresh,
  onOpenRegister,
  statusSyncMenuItems,
  onStatusSyncClick,
  statusSyncLoading,
  resumeAuthMenuItems,
  onResumeAuthClick,
  resumeAuthLoading,
  backfillMenuItems,
  onBackfillClick,
  backfillLoading,
}: AccountsToolbarProps) {
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap', alignItems: 'flex-start' }}>
        <div>
          <h1 style={{ fontSize: 24, fontWeight: 'bold', margin: 0 }}>ChatGPT 账号</h1>
          <p style={{ color: '#7a8ba3', marginTop: 4 }}>
            当前展示 {accountsCount} 个账号，共 {total} 个
          </p>
        </div>
        <Space wrap>
          <Input.Search
            allowClear
            placeholder="搜索邮箱"
            style={{ width: 220 }}
            value={search}
            onChange={(event) => onSearchChange(event.target.value)}
          />
          <Select
            value={filterStatus}
            onChange={onFilterStatusChange}
            style={{ width: 140 }}
            options={STATUS_FILTER_OPTIONS}
          />
          {isChatgptPlatform && (
            <Dropdown menu={{ items: statusSyncMenuItems, onClick: onStatusSyncClick }}>
              <Button loading={statusSyncLoading !== ''} icon={statusSyncLoading !== '' ? <SyncOutlined spin /> : <ReloadOutlined />}>
                状态同步
              </Button>
            </Dropdown>
          )}
          {isChatgptPlatform && (
            <Dropdown menu={{ items: resumeAuthMenuItems, onClick: onResumeAuthClick }}>
              <Button
                loading={resumeAuthLoading !== ''}
                icon={resumeAuthLoading !== '' ? <SyncOutlined spin /> : <ReloadOutlined />}
              >
                批量补抓Auth
              </Button>
            </Dropdown>
          )}
          {isChatgptPlatform && (
            <Dropdown menu={{ items: backfillMenuItems, onClick: onBackfillClick }}>
              <Button
                loading={backfillLoading !== ''}
                icon={backfillLoading !== '' ? <SyncOutlined spin /> : <UploadOutlined />}
              >
                远端补传
              </Button>
            </Dropdown>
          )}
          {isChatgptPlatform && (
            <Button icon={<LinkOutlined />} loading={batchPaymentLinkLoading} onClick={onBatchPaymentLink}>
              批量订阅链接
            </Button>
          )}
          {isChatgptPlatform && (
            <Button icon={<LinkOutlined />} loading={batchGopayLoading} onClick={onOpenBatchGopay}>
              批量 GoPay
            </Button>
          )}
          {isChatgptPlatform && (
            <Button icon={<LinkOutlined />} onClick={onOpenBusinessDeferred}>
              Business 补激活
            </Button>
          )}
          <Popconfirm
            title="确认删除当前平台的全部无效账号？"
            description="只会删除 status=invalid 的账号，操作不可恢复。"
            onConfirm={onDeleteInvalid}
          >
            <Button danger icon={<DeleteOutlined />} loading={deleteInvalidLoading} disabled={total === 0}>
              一键删无效
            </Button>
          </Popconfirm>
          {selectedRowKeys.length > 0 && (
            <Popconfirm title={`确认删除选中的 ${selectedRowKeys.length} 个账号？`} onConfirm={onBatchDelete}>
              <Button danger icon={<DeleteOutlined />}>删除 {selectedRowKeys.length} 个</Button>
            </Popconfirm>
          )}
          <Button icon={<UploadOutlined />} onClick={onOpenImport}>导入</Button>
          <Button icon={<DownloadOutlined />} onClick={onExportCsv} disabled={accountsCount === 0}>导出</Button>
          <Button icon={<PlusOutlined />} onClick={onOpenAdd}>新增</Button>
          <ActiveTasksPanel
            loading={activeTasksLoading}
            items={activeTasks}
            onRefresh={onRefreshActiveTasks}
            onOpen={onActiveTasksOpen}
            onOpenTaskSnapshot={onOpenTaskSnapshot}
          />
          <Button type="primary" icon={<PlusOutlined />} onClick={onOpenRegister}>注册</Button>
          <Button icon={<ReloadOutlined spin={loading} />} onClick={onRefresh} />
        </Space>
      </div>
    </div>
  )
}
