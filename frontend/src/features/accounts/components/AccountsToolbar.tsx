import type { CSSProperties, ReactNode } from 'react'
import { useState } from 'react'
import { Button, Dropdown, Popconfirm } from 'antd'
import type { MenuProps } from 'antd'
import {
  DeleteOutlined,
  CreditCardOutlined,
  DatabaseOutlined,
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
  accountsCount?: number
  selectedRowKeys: React.Key[]
  columnVisibilityControl?: ReactNode
  activeTasksLoading: boolean
  activeTasks: any[]
  onOpenTaskSnapshot: (snapshot: any) => void
  onRefreshActiveTasks: () => Promise<void> | void
  onActiveTasksOpen: () => void
  isChatgptPlatform: boolean
  batchGopayLoading: boolean
  batchPaymentLinkLoading: boolean
  batchInvalidRecheckLoading: boolean
  batchK12RecaptureLoading: boolean
  phoneBindingTestLoading: boolean
  paypalBindingLoading: boolean
  baxiCdkSubmitLoading: boolean
  onBatchPaymentLink: (options?: { forceRefresh?: boolean }) => void
  onBatchInvalidRecheck: () => void
  onOpenBatchK12Recapture: () => void
  onOpenPhoneBindingTest: () => void
  onOpenPaypalBinding: () => void
  onOpenBaxiCdkSubmit: () => void
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
  selectedRowKeys,
  columnVisibilityControl,
  activeTasksLoading,
  activeTasks,
  onOpenTaskSnapshot,
  onRefreshActiveTasks,
  onActiveTasksOpen,
  isChatgptPlatform,
  batchGopayLoading,
  batchPaymentLinkLoading,
  batchInvalidRecheckLoading,
  batchK12RecaptureLoading,
  phoneBindingTestLoading,
  paypalBindingLoading,
  baxiCdkSubmitLoading,
  onBatchPaymentLink,
  onBatchInvalidRecheck,
  onOpenBatchK12Recapture,
  onOpenPhoneBindingTest,
  onOpenPaypalBinding,
  onOpenBaxiCdkSubmit,
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
  const [mobileOpsOpen, setMobileOpsOpen] = useState(false)
  const buttonStyle: CSSProperties = isMobile
    ? { flex: '1 1 calc(50% - 4px)', minWidth: 132 }
    : {}
  const activeTasksStyle: CSSProperties = isMobile
    ? { flex: '1 1 100%', width: '100%', minWidth: 0 }
    : { minWidth: 210 }
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
  const showOperationGroups = !isMobile || mobileOpsOpen

  return (
    <div className={`accounts-toolbar ${isMobile ? 'accounts-toolbar-mobile' : ''}`}>
      <div className="accounts-toolbar-head">
        <div className="accounts-toolbar-title">
          <h1 style={{ fontSize: isMobile ? 20 : 24, fontWeight: 'bold', margin: 0 }}>ChatGPT 账号</h1>
        </div>
        <div className="accounts-toolbar-primary-actions">
          <Button block={isMobile} style={buttonStyle} type="primary" icon={<PlusOutlined />} onClick={onOpenRegister}>注册</Button>
          <Button block={isMobile} style={buttonStyle} icon={<PlusOutlined />} onClick={onOpenAdd}>新增</Button>
          <Button block={isMobile} style={buttonStyle} icon={<UploadOutlined />} onClick={onOpenImport}>导入</Button>
          <Button block={isMobile} style={buttonStyle} icon={<DownloadOutlined />} onClick={onExportCsv} disabled={total === 0}>导出</Button>
          <Button block={isMobile} style={buttonStyle} icon={<ReloadOutlined spin={loading} />} onClick={onRefresh}>
            {isMobile ? '刷新' : null}
          </Button>
          {isMobile ? (
            <Button block style={buttonStyle} onClick={() => setMobileOpsOpen((value) => !value)}>
              {mobileOpsOpen ? '收起批量' : '批量操作'}
            </Button>
          ) : null}
          <ActiveTasksPanel
            loading={activeTasksLoading}
            items={activeTasks}
            onRefresh={onRefreshActiveTasks}
            onOpen={onActiveTasksOpen}
            onOpenTaskSnapshot={onOpenTaskSnapshot}
            style={activeTasksStyle}
          />
        </div>
      </div>

      {showOperationGroups ? (
        <div className="accounts-toolbar-ops">
          <span className="accounts-toolbar-total">总数：{total}</span>
          {isChatgptPlatform ? (
            <>
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
              {columnVisibilityControl ? <div className="accounts-toolbar-column-control">{columnVisibilityControl}</div> : null}
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
                icon={batchK12RecaptureLoading ? <SyncOutlined spin /> : <SyncOutlined />}
                loading={batchK12RecaptureLoading}
                disabled={selectedRowKeys.length === 0 && total === 0}
                onClick={onOpenBatchK12Recapture}
              >
                批量K12重跑
              </Button>
              <Button
                block={isMobile}
                style={buttonStyle}
                icon={phoneBindingTestLoading ? <SyncOutlined spin /> : <MobileOutlined />}
                loading={phoneBindingTestLoading}
                onClick={onOpenPhoneBindingTest}
              >
                手机号绑定
              </Button>
              <Button
                block={isMobile}
                style={buttonStyle}
                icon={paypalBindingLoading ? <SyncOutlined spin /> : <CreditCardOutlined />}
                loading={paypalBindingLoading}
                onClick={onOpenPaypalBinding}
              >
                PayPal绑定
              </Button>

              <Button
                block={isMobile}
                style={buttonStyle}
                icon={baxiCdkSubmitLoading ? <SyncOutlined spin /> : <DatabaseOutlined />}
                loading={baxiCdkSubmitLoading}
                onClick={onOpenBaxiCdkSubmit}
              >
                idea批量提交
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
            </>
          ) : null}

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
          {selectedRowKeys.length > 0 ? (
            <Popconfirm title={`确认删除选中的 ${selectedRowKeys.length} 个账号？`} onConfirm={onBatchDelete}>
              <Button block={isMobile} style={buttonStyle} danger icon={<DeleteOutlined />}>删除 {selectedRowKeys.length} 个</Button>
            </Popconfirm>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
