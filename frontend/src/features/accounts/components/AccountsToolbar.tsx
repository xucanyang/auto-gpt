import type { CSSProperties } from 'react'
import { Button, Dropdown, Popconfirm } from 'antd'
import type { MenuProps } from 'antd'
import {
  DeleteOutlined,
  DownOutlined,
  DownloadOutlined,
  LinkOutlined,
  MobileOutlined,
  PlusOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SyncOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import { ActiveTasksPanel } from './ActiveTasksPanel'

type AccountsToolbarProps = {
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
  batchInvalidRecheckLoading: boolean
  phoneBindingTestLoading: boolean
  onBatchPaymentLink: (options?: { forceRefresh?: boolean }) => void
  onBatchInvalidRecheck: () => void
  onOpenPhoneBindingTest: () => void
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
  isMobile?: boolean
}

export function AccountsToolbar({
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
  batchInvalidRecheckLoading,
  phoneBindingTestLoading,
  onBatchPaymentLink,
  onBatchInvalidRecheck,
  onOpenPhoneBindingTest,
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
  isMobile = false,
}: AccountsToolbarProps) {
  const toolbarStyle: CSSProperties = {
    display: 'flex',
    justifyContent: 'space-between',
    gap: isMobile ? 10 : 12,
    flexWrap: 'wrap',
    alignItems: 'flex-start',
  }
  const controlsStyle: CSSProperties = {
    display: 'flex',
    flexWrap: 'wrap',
    gap: 8,
    justifyContent: isMobile ? 'stretch' : 'flex-end',
    width: isMobile ? '100%' : undefined,
    flex: isMobile ? '1 1 100%' : undefined,
  }
  const buttonStyle: CSSProperties = isMobile
    ? { flex: '1 1 calc(50% - 4px)', minWidth: 132 }
    : {}
  const groupStyle: CSSProperties = {
    display: 'flex',
    flexWrap: 'wrap',
    gap: 8,
    alignItems: 'center',
    justifyContent: isMobile ? 'stretch' : 'flex-end',
    width: isMobile ? '100%' : undefined,
  }
  const separatedGroupStyle: CSSProperties = {
    ...groupStyle,
    paddingLeft: isMobile ? 0 : 10,
    borderLeft: isMobile ? undefined : '1px solid rgba(127,127,127,0.18)',
  }
  const paymentLinkDisabled = selectedRowKeys.length === 0 && total === 0
  const paymentLinkMenuItems: MenuProps['items'] = [
    {
      key: 'normal',
      label: '生成订阅链接（可复用缓存）',
      disabled: paymentLinkDisabled,
    },
    {
      key: 'force',
      label: '强制重新生成订阅链接',
      disabled: paymentLinkDisabled,
    },
  ]

  return (
    <div style={{ marginBottom: isMobile ? 12 : 16, flexShrink: 0 }}>
      <div style={toolbarStyle}>
        <div style={{ minWidth: 0 }}>
          <h1 style={{ fontSize: isMobile ? 20 : 24, fontWeight: 'bold', margin: 0 }}>ChatGPT 账号</h1>
          <p style={{ color: '#7a8ba3', marginTop: 4, fontSize: isMobile ? 12 : 14 }}>
            当前展示 {accountsCount} 个账号，共 {total} 个
          </p>
        </div>
        <div style={controlsStyle}>
          <div style={groupStyle}>
            <Button block={isMobile} style={buttonStyle} type="primary" icon={<PlusOutlined />} onClick={onOpenRegister}>注册</Button>
            <Button block={isMobile} style={buttonStyle} icon={<PlusOutlined />} onClick={onOpenAdd}>新增</Button>
            <Button block={isMobile} style={buttonStyle} icon={<UploadOutlined />} onClick={onOpenImport}>导入</Button>
            <Button block={isMobile} style={buttonStyle} icon={<DownloadOutlined />} onClick={onExportCsv} disabled={accountsCount === 0}>导出</Button>
            <Button block={isMobile} style={buttonStyle} icon={<ReloadOutlined spin={loading} />} onClick={onRefresh}>
              {isMobile ? '刷新' : null}
            </Button>
            <ActiveTasksPanel
              loading={activeTasksLoading}
              items={activeTasks}
              onRefresh={onRefreshActiveTasks}
              onOpen={onActiveTasksOpen}
              onOpenTaskSnapshot={onOpenTaskSnapshot}
              style={isMobile ? { flex: '1 1 100%', width: '100%', minWidth: 0 } : undefined}
            />
          </div>
          {isChatgptPlatform && (
            <div style={separatedGroupStyle}>
              <Dropdown menu={{ items: statusSyncMenuItems, onClick: onStatusSyncClick }}>
                <Button
                  block={isMobile}
                  style={buttonStyle}
                  loading={statusSyncLoading !== ''}
                  icon={statusSyncLoading !== '' ? <SyncOutlined spin /> : <ReloadOutlined />}
                >
                  状态同步
                </Button>
              </Dropdown>
              <Dropdown menu={{ items: resumeAuthMenuItems, onClick: onResumeAuthClick }}>
                <Button
                  block={isMobile}
                  style={buttonStyle}
                  loading={resumeAuthLoading !== ''}
                  icon={resumeAuthLoading !== '' ? <SyncOutlined spin /> : <ReloadOutlined />}
                >
                  批量补抓Auth
                </Button>
              </Dropdown>
              <Dropdown menu={{ items: backfillMenuItems, onClick: onBackfillClick }}>
                <Button
                  block={isMobile}
                  style={buttonStyle}
                  loading={backfillLoading !== ''}
                  icon={backfillLoading !== '' ? <SyncOutlined spin /> : <UploadOutlined />}
                >
                  远端补传
                </Button>
              </Dropdown>
              <Button
                block={isMobile}
                style={buttonStyle}
                icon={batchInvalidRecheckLoading ? <SyncOutlined spin /> : <SafetyCertificateOutlined />}
                loading={batchInvalidRecheckLoading}
                onClick={onBatchInvalidRecheck}
              >
                批量失效测活
              </Button>
              <Button
                block={isMobile}
                style={buttonStyle}
                icon={phoneBindingTestLoading ? <SyncOutlined spin /> : <MobileOutlined />}
                loading={phoneBindingTestLoading}
                onClick={onOpenPhoneBindingTest}
              >
                号码绑定测试
              </Button>
              <Dropdown
                disabled={paymentLinkDisabled || batchPaymentLinkLoading}
                menu={{
                  items: paymentLinkMenuItems,
                  onClick: ({ key }) => onBatchPaymentLink({ forceRefresh: String(key) === 'force' }),
                }}
              >
                <Button
                  block={isMobile}
                  style={buttonStyle}
                  icon={<LinkOutlined />}
                  loading={batchPaymentLinkLoading}
                  disabled={paymentLinkDisabled}
                >
                  批量订阅链接 <DownOutlined />
                </Button>
              </Dropdown>
              <Button
                block={isMobile}
                style={buttonStyle}
                icon={<LinkOutlined />}
                loading={batchGopayLoading}
                onClick={onOpenBatchGopay}
              >
                批量 GoPay
              </Button>
              <Button block={isMobile} style={buttonStyle} icon={<LinkOutlined />} onClick={onOpenBusinessDeferred}>
                Business 补激活
              </Button>
            </div>
          )}
          <div style={separatedGroupStyle}>
            <Popconfirm
              title="确认删除当前平台的全部无效账号？"
              description="只会删除 status=invalid 的账号，操作不可恢复。"
              onConfirm={onDeleteInvalid}
            >
              <Button
                block={isMobile}
                style={buttonStyle}
                danger
                icon={<DeleteOutlined />}
                loading={deleteInvalidLoading}
                disabled={total === 0}
              >
                一键删无效
              </Button>
            </Popconfirm>
            {selectedRowKeys.length > 0 && (
              <Popconfirm title={`确认删除选中的 ${selectedRowKeys.length} 个账号？`} onConfirm={onBatchDelete}>
                <Button block={isMobile} style={buttonStyle} danger icon={<DeleteOutlined />}>删除 {selectedRowKeys.length} 个</Button>
              </Popconfirm>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
