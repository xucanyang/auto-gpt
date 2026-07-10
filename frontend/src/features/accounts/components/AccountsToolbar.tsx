import type { CSSProperties, ReactNode } from 'react'
import { useState } from 'react'
import { Button, Dropdown, Modal, Space } from 'antd'
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

export type AccountsToolbarActionId =
  | 'statusSync'
  | 'resumeAuth'
  | 'backfill'
  | 'invalidRecheck'
  | 'k12Recapture'
  | 'phoneBindingTest'
  | 'paypalBinding'
  | 'baxiCdkSubmit'
  | 'paymentLink'
  | 'gopay'
  | 'businessDeferred'

export type AccountExportMode = 'sub2api' | 'access_token'

type AccountsToolbarDangerActionId = 'deleteInvalid' | 'batchDelete'
type MoreMenuClickInfo = Parameters<NonNullable<MenuProps['onClick']>>[0]
type ToolbarMenuItem = Exclude<NonNullable<MenuProps['items']>[number], null>
type ActiveTaskSnapshot = {
  id?: string | number
  task_id?: string | number
  source?: string
  progress?: string | number
  meta?: Record<string, unknown>
  email?: string
  platform?: string
  [key: string]: unknown
}

const DEFAULT_PINNED_ACTION_IDS: AccountsToolbarActionId[] = ['statusSync', 'paymentLink']
const CHATGPT_SYNC_ACTION_IDS: AccountsToolbarActionId[] = ['statusSync', 'resumeAuth', 'backfill']
const CHATGPT_BATCH_ACTION_IDS: AccountsToolbarActionId[] = [
  'invalidRecheck',
  'k12Recapture',
  'phoneBindingTest',
  'paypalBinding',
  'baxiCdkSubmit',
  'paymentLink',
  'gopay',
  'businessDeferred',
]
const CHATGPT_ACTION_IDS: AccountsToolbarActionId[] = [
  ...CHATGPT_SYNC_ACTION_IDS,
  ...CHATGPT_BATCH_ACTION_IDS,
]
const DANGER_ACTION_IDS: AccountsToolbarDangerActionId[] = ['deleteInvalid', 'batchDelete']
const MORE_MENU_CHILD_KEY_SEPARATOR = '::'

const isAccountsToolbarActionId = (actionId: string): actionId is AccountsToolbarActionId => (
  (CHATGPT_ACTION_IDS as string[]).includes(actionId)
)

const normalizePinnedActionIds = (actionIds: string[]): AccountsToolbarActionId[] => {
  const seen = new Set<string>()
  const normalized: AccountsToolbarActionId[] = []

  for (const actionId of actionIds) {
    if (!isAccountsToolbarActionId(actionId) || seen.has(actionId)) {
      continue
    }
    seen.add(actionId)
    normalized.push(actionId)
  }

  return normalized
}

const makeMoreMenuChildKey = (actionId: AccountsToolbarActionId, key: React.Key) => (
  `${actionId}${MORE_MENU_CHILD_KEY_SEPARATOR}${String(key)}`
)

const prefixDropdownMenuItems = (
  actionId: AccountsToolbarActionId,
  items: MenuProps['items'],
): MenuProps['items'] => {
  if (!items?.length) {
    return []
  }

  return items.map((item) => {
    if (!item) {
      return item
    }

    const record = item as ToolbarMenuItem & {
      key?: React.Key
      children?: MenuProps['items']
      type?: string
    }

    if (record.type === 'divider') {
      return item
    }

    const nextItem = { ...record }
    if (record.key !== undefined) {
      nextItem.key = makeMoreMenuChildKey(actionId, record.key)
    }
    if (record.children?.length) {
      nextItem.children = prefixDropdownMenuItems(actionId, record.children)
    }

    return nextItem as ToolbarMenuItem
  })
}

const compactMenuItems = (items: MenuProps['items']): ToolbarMenuItem[] => (
  (items || []).filter(Boolean) as ToolbarMenuItem[]
)

type AccountsToolbarProps = {
  total: number
  accountsCount?: number
  selectedRowKeys: React.Key[]
  activeTasksLoading: boolean
  activeTasks: ActiveTaskSnapshot[]
  onOpenTaskSnapshot: (snapshot: ActiveTaskSnapshot) => void
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
  onExportCsv: (mode?: AccountExportMode) => void
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
  pinnedActionIds?: string[]
  isMobile?: boolean
  selectedAccountsControl?: ReactNode
  columnVisibilityControl?: ReactNode
  toolbarActionVisibilityControl?: ReactNode
}

export function AccountsToolbar({
  total,
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
  pinnedActionIds,
  isMobile = false,
  selectedAccountsControl,
  columnVisibilityControl,
  toolbarActionVisibilityControl,
}: AccountsToolbarProps) {
  const [mobileOpsOpen, setMobileOpsOpen] = useState(false)
  const buttonStyle: CSSProperties = isMobile
    ? { flex: '1 1 calc(50% - 4px)', minWidth: 132 }
    : {}
  const operationButtonStyle: CSSProperties = isMobile
    ? { flex: '1 1 0', width: '100%', minWidth: 0 }
    : {}
  const activeTasksStyle: CSSProperties = isMobile
    ? { flex: '1 1 100%', width: '100%', minWidth: 0 }
    : { minWidth: 210 }
  const hasNoSelectedAndNoResults = selectedRowKeys.length === 0 && total === 0
  const paymentLinkDisabled = hasNoSelectedAndNoResults
  const batchK12RecaptureDisabled = hasNoSelectedAndNoResults
  const exportMenuItems: MenuProps['items'] = [
    { key: 'sub2api', label: 'Sub2API JSON（默认）' },
    { key: 'access_token', label: '仅 AccessToken（每行一个）' },
  ]
  const handleExportMenuClick: MenuProps['onClick'] = ({ key }) => {
    onExportCsv(String(key) === 'access_token' ? 'access_token' : 'sub2api')
  }
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
  const pinnedActionIdsToRender = normalizePinnedActionIds(pinnedActionIds ?? DEFAULT_PINNED_ACTION_IDS)
  const pinnedActionIdSet = new Set<string>(pinnedActionIdsToRender)

  const buildConfirmDeleteInvalid = () => {
    Modal.confirm({
      title: '确认删除当前平台的全部无效账号？',
      content: '只会删除 status=invalid 的账号，操作不可恢复。',
      okText: '确认删除',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: () => onDeleteInvalid(),
    })
  }

  const buildConfirmBatchDelete = () => {
    Modal.confirm({
      title: `确认删除选中的 ${selectedRowKeys.length} 个账号？`,
      okText: '确认删除',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: () => onBatchDelete(),
    })
  }

  const buildNestedMenuItem = (
    actionId: AccountsToolbarActionId,
    label: string,
    icon: ReactNode,
    items: MenuProps['items'],
    disabled = false,
  ): ToolbarMenuItem | null => {
    const children = compactMenuItems(prefixDropdownMenuItems(actionId, items))
    if (children.length === 0) {
      return null
    }

    return {
      key: actionId,
      label,
      icon,
      disabled,
      children,
    } as ToolbarMenuItem
  }

  const buildMoreMenuItem = (actionId: AccountsToolbarActionId): ToolbarMenuItem | null => {
    switch (actionId) {
      case 'statusSync':
        return buildNestedMenuItem(
          actionId,
          '状态同步',
          statusSyncLoading !== '' ? <SyncOutlined spin /> : <ReloadOutlined />,
          statusSyncMenuItems,
          statusSyncLoading !== '',
        )
      case 'resumeAuth':
        return buildNestedMenuItem(
          actionId,
          '批量补抓Auth',
          resumeAuthLoading !== '' ? <SyncOutlined spin /> : <ReloadOutlined />,
          resumeAuthMenuItems,
          resumeAuthLoading !== '',
        )
      case 'backfill':
        return buildNestedMenuItem(
          actionId,
          '远端补传',
          backfillLoading !== '' ? <SyncOutlined spin /> : <UploadOutlined />,
          backfillMenuItems,
          backfillLoading !== '',
        )
      case 'invalidRecheck':
        return {
          key: actionId,
          label: '批量失效测活',
          icon: batchInvalidRecheckLoading ? <SyncOutlined spin /> : <SafetyCertificateOutlined />,
          disabled: batchInvalidRecheckLoading,
        } as ToolbarMenuItem
      case 'k12Recapture':
        return {
          key: actionId,
          label: '批量K12重跑',
          icon: batchK12RecaptureLoading ? <SyncOutlined spin /> : <SyncOutlined />,
          disabled: batchK12RecaptureLoading || batchK12RecaptureDisabled,
        } as ToolbarMenuItem
      case 'phoneBindingTest':
        return {
          key: actionId,
          label: '手机号绑定',
          icon: phoneBindingTestLoading ? <SyncOutlined spin /> : <MobileOutlined />,
          disabled: phoneBindingTestLoading,
        } as ToolbarMenuItem
      case 'paypalBinding':
        return {
          key: actionId,
          label: 'PayPal绑定',
          icon: paypalBindingLoading ? <SyncOutlined spin /> : <CreditCardOutlined />,
          disabled: paypalBindingLoading,
        } as ToolbarMenuItem
      case 'baxiCdkSubmit':
        return {
          key: actionId,
          label: 'idea批量提交',
          icon: baxiCdkSubmitLoading ? <SyncOutlined spin /> : <DatabaseOutlined />,
          disabled: baxiCdkSubmitLoading,
        } as ToolbarMenuItem
      case 'paymentLink':
        return buildNestedMenuItem(
          actionId,
          '批量订阅链接',
          batchPaymentLinkLoading ? <SyncOutlined spin /> : <LinkOutlined />,
          paymentLinkMenuItems,
          paymentLinkDisabled || batchPaymentLinkLoading,
        )
      case 'gopay':
        return {
          key: actionId,
          label: '批量 GoPay',
          icon: batchGopayLoading ? <SyncOutlined spin /> : <LinkOutlined />,
          disabled: batchGopayLoading,
        } as ToolbarMenuItem
      case 'businessDeferred':
        return {
          key: actionId,
          label: 'Business 补激活',
          icon: <LinkOutlined />,
        } as ToolbarMenuItem
      default:
        return null
    }
  }

  const buildDangerMenuItem = (actionId: AccountsToolbarDangerActionId): ToolbarMenuItem | null => {
    switch (actionId) {
      case 'deleteInvalid':
        return {
          key: actionId,
          label: '一键删无效',
          icon: deleteInvalidLoading ? <SyncOutlined spin /> : <DeleteOutlined />,
          danger: true,
          disabled: deleteInvalidLoading || total === 0,
        } as ToolbarMenuItem
      case 'batchDelete':
        return selectedRowKeys.length > 0
          ? {
              key: actionId,
              label: `删除 ${selectedRowKeys.length} 个`,
              icon: <DeleteOutlined />,
              danger: true,
            } as ToolbarMenuItem
          : null
      default:
        return null
    }
  }

  const appendMoreMenuGroup = (target: ToolbarMenuItem[], items: Array<ToolbarMenuItem | null>) => {
    const visibleItems = items.filter(Boolean) as ToolbarMenuItem[]
    if (visibleItems.length === 0) {
      return
    }
    if (target.length > 0) {
      target.push({ type: 'divider' } as ToolbarMenuItem)
    }
    target.push(...visibleItems)
  }

  const moreOperationMenuItems: MenuProps['items'] = []
  if (isChatgptPlatform) {
    appendMoreMenuGroup(
      moreOperationMenuItems as ToolbarMenuItem[],
      CHATGPT_SYNC_ACTION_IDS
        .filter((actionId) => !pinnedActionIdSet.has(actionId))
        .map((actionId) => buildMoreMenuItem(actionId)),
    )
    appendMoreMenuGroup(
      moreOperationMenuItems as ToolbarMenuItem[],
      CHATGPT_BATCH_ACTION_IDS
        .filter((actionId) => !pinnedActionIdSet.has(actionId))
        .map((actionId) => buildMoreMenuItem(actionId)),
    )
  }
  appendMoreMenuGroup(
    moreOperationMenuItems as ToolbarMenuItem[],
    DANGER_ACTION_IDS.map((actionId) => buildDangerMenuItem(actionId)),
  )

  const handleMoreOperationClick: MenuProps['onClick'] = (info) => {
    const rawKey = String(info.key)
    const childKeyIndex = rawKey.indexOf(MORE_MENU_CHILD_KEY_SEPARATOR)
    if (childKeyIndex >= 0) {
      const actionId = rawKey.slice(0, childKeyIndex)
      const originalKey = rawKey.slice(childKeyIndex + MORE_MENU_CHILD_KEY_SEPARATOR.length)
      const nestedInfo = { ...info, key: originalKey } as MoreMenuClickInfo

      if (actionId === 'statusSync') {
        onStatusSyncClick?.(nestedInfo)
        return
      }
      if (actionId === 'resumeAuth') {
        onResumeAuthClick?.(nestedInfo)
        return
      }
      if (actionId === 'backfill') {
        onBackfillClick?.(nestedInfo)
        return
      }
      if (actionId === 'paymentLink') {
        onBatchPaymentLink({ forceRefresh: originalKey === 'force' })
      }
      return
    }

    switch (rawKey) {
      case 'invalidRecheck':
        onBatchInvalidRecheck()
        return
      case 'k12Recapture':
        onOpenBatchK12Recapture()
        return
      case 'phoneBindingTest':
        onOpenPhoneBindingTest()
        return
      case 'paypalBinding':
        onOpenPaypalBinding()
        return
      case 'baxiCdkSubmit':
        onOpenBaxiCdkSubmit()
        return
      case 'gopay':
        onOpenBatchGopay()
        return
      case 'businessDeferred':
        onOpenBusinessDeferred()
        return
      case 'deleteInvalid':
        if (!deleteInvalidLoading && total > 0) {
          buildConfirmDeleteInvalid()
        }
        return
      case 'batchDelete':
        if (selectedRowKeys.length > 0) {
          buildConfirmBatchDelete()
        }
        return
      default:
        return
    }
  }

  const renderPinnedAction = (actionId: AccountsToolbarActionId) => {
    if (!isChatgptPlatform) {
      return null
    }

    switch (actionId) {
      case 'statusSync':
        return (
          <Dropdown key={actionId} menu={{ items: statusSyncMenuItems, onClick: onStatusSyncClick }}>
            <Button
              block={isMobile}
              style={operationButtonStyle}
              loading={statusSyncLoading !== ''}
              icon={statusSyncLoading !== '' ? <SyncOutlined spin /> : <ReloadOutlined />}
            >
              状态同步 <DownOutlined />
            </Button>
          </Dropdown>
        )
      case 'resumeAuth':
        return (
          <Dropdown key={actionId} menu={{ items: resumeAuthMenuItems, onClick: onResumeAuthClick }}>
            <Button
              block={isMobile}
              style={operationButtonStyle}
              loading={resumeAuthLoading !== ''}
              icon={resumeAuthLoading !== '' ? <SyncOutlined spin /> : <ReloadOutlined />}
            >
              批量补抓Auth <DownOutlined />
            </Button>
          </Dropdown>
        )
      case 'backfill':
        return (
          <Dropdown key={actionId} menu={{ items: backfillMenuItems, onClick: onBackfillClick }}>
            <Button
              block={isMobile}
              style={operationButtonStyle}
              loading={backfillLoading !== ''}
              icon={backfillLoading !== '' ? <SyncOutlined spin /> : <UploadOutlined />}
            >
              远端补传 <DownOutlined />
            </Button>
          </Dropdown>
        )
      case 'invalidRecheck':
        return (
          <Button
            key={actionId}
            block={isMobile}
            style={operationButtonStyle}
            icon={batchInvalidRecheckLoading ? <SyncOutlined spin /> : <SafetyCertificateOutlined />}
            loading={batchInvalidRecheckLoading}
            onClick={onBatchInvalidRecheck}
          >
            批量失效测活
          </Button>
        )
      case 'k12Recapture':
        return (
          <Button
            key={actionId}
            block={isMobile}
            style={operationButtonStyle}
            icon={batchK12RecaptureLoading ? <SyncOutlined spin /> : <SyncOutlined />}
            loading={batchK12RecaptureLoading}
            disabled={batchK12RecaptureDisabled}
            onClick={onOpenBatchK12Recapture}
          >
            批量K12重跑
          </Button>
        )
      case 'phoneBindingTest':
        return (
          <Button
            key={actionId}
            block={isMobile}
            style={operationButtonStyle}
            icon={phoneBindingTestLoading ? <SyncOutlined spin /> : <MobileOutlined />}
            loading={phoneBindingTestLoading}
            onClick={onOpenPhoneBindingTest}
          >
            手机号绑定
          </Button>
        )
      case 'paypalBinding':
        return (
          <Button
            key={actionId}
            block={isMobile}
            style={operationButtonStyle}
            icon={paypalBindingLoading ? <SyncOutlined spin /> : <CreditCardOutlined />}
            loading={paypalBindingLoading}
            onClick={onOpenPaypalBinding}
          >
            PayPal绑定
          </Button>
        )
      case 'baxiCdkSubmit':
        return (
          <Button
            key={actionId}
            block={isMobile}
            style={operationButtonStyle}
            icon={baxiCdkSubmitLoading ? <SyncOutlined spin /> : <DatabaseOutlined />}
            loading={baxiCdkSubmitLoading}
            onClick={onOpenBaxiCdkSubmit}
          >
            idea批量提交
          </Button>
        )
      case 'paymentLink':
        return (
          <Dropdown
            key={actionId}
            disabled={paymentLinkDisabled || batchPaymentLinkLoading}
            menu={{
              items: paymentLinkMenuItems,
              onClick: ({ key }) => onBatchPaymentLink({ forceRefresh: String(key) === 'force' }),
            }}
          >
            <Button
              block={isMobile}
              style={operationButtonStyle}
              icon={<LinkOutlined />}
              loading={batchPaymentLinkLoading}
              disabled={paymentLinkDisabled}
            >
              批量订阅链接 <DownOutlined />
            </Button>
          </Dropdown>
        )
      case 'gopay':
        return (
          <Button
            key={actionId}
            block={isMobile}
            style={operationButtonStyle}
            icon={<LinkOutlined />}
            loading={batchGopayLoading}
            onClick={onOpenBatchGopay}
          >
            批量 GoPay
          </Button>
        )
      case 'businessDeferred':
        return (
          <Button key={actionId} block={isMobile} style={operationButtonStyle} icon={<LinkOutlined />} onClick={onOpenBusinessDeferred}>
            Business 补激活
          </Button>
        )
      default:
        return null
    }
  }

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
          {isChatgptPlatform ? (
            <Space.Compact block={isMobile} style={buttonStyle}>
              <Button
                block={isMobile}
                icon={<DownloadOutlined />}
                onClick={() => onExportCsv('sub2api')}
                disabled={total === 0}
              >
                导出
              </Button>
              <Dropdown menu={{ items: exportMenuItems, onClick: handleExportMenuClick }} trigger={['click']}>
                <Button aria-label="选择导出模式" icon={<DownOutlined />} disabled={total === 0} />
              </Dropdown>
            </Space.Compact>
          ) : (
            <Button block={isMobile} style={buttonStyle} icon={<DownloadOutlined />} onClick={() => onExportCsv()} disabled={total === 0}>导出</Button>
          )}
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

      <div className="accounts-toolbar-action-row">
        <div className={`accounts-toolbar-ops${isMobile && !showOperationGroups ? ' accounts-toolbar-ops-controls-only' : ''}`}>
          {!isMobile ? selectedAccountsControl : null}
          {showOperationGroups ? (
            <>
            {pinnedActionIdsToRender.map((actionId) => renderPinnedAction(actionId))}
            <Dropdown
              menu={{ items: moreOperationMenuItems, onClick: handleMoreOperationClick }}
              trigger={['click']}
              disabled={!moreOperationMenuItems.length}
            >
              <Button block={isMobile} style={operationButtonStyle}>
                更多操作 <DownOutlined />
              </Button>
            </Dropdown>
            </>
          ) : null}
          <div className="accounts-toolbar-inline-controls">
            {toolbarActionVisibilityControl}
            {columnVisibilityControl}
          </div>
        </div>
      </div>
    </div>
  )
}
