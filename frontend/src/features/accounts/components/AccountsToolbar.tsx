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
  | 'phoneBindingTest'
  | 'paypalBinding'
  | 'baxiCdkSubmit'
  | 'paymentLink'
  | 'gopay'

export type AccountExportMode = 'sub2api' | 'access_token' | 'pix_payment_links'
export type AccountExportScope = 'selected' | 'filtered'
export type PixLinkCleanupMode = 'expired' | 'paid' | 'cancelled'

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
  'phoneBindingTest',
  'paypalBinding',
  'baxiCdkSubmit',
  'paymentLink',
  'gopay',
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

const pixCleanupModeFromMenuKey = (key: React.Key): PixLinkCleanupMode | null => {
  const normalized = String(key)
  if (normalized === 'pix_cleanup_expired') return 'expired'
  if (normalized === 'pix_cleanup_paid') return 'paid'
  if (normalized === 'pix_cleanup_cancelled') return 'cancelled'
  return null
}

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
  pixLinkCleanupLoading: boolean
  batchInvalidRecheckLoading: boolean
  phoneBindingTestLoading: boolean
  paypalBindingLoading: boolean
  baxiCdkSubmitLoading: boolean
  onBatchPaymentLink: (options?: { forceRefresh?: boolean }) => void
  onCleanupPixLinks: (mode: PixLinkCleanupMode) => void
  onBatchInvalidRecheck: () => void
  onOpenPhoneBindingTest: () => void
  onOpenPaypalBinding: () => void
  onOpenBaxiCdkSubmit: () => void
  onOpenBatchGopay: () => void
  deleteInvalidLoading: boolean
  onDeleteInvalid: () => Promise<void> | void
  onBatchDelete: () => Promise<void> | void
  onOpenImport: () => void
  onExportCsv: (mode?: AccountExportMode, scope?: AccountExportScope) => void
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
  pixLinkCleanupLoading,
  batchInvalidRecheckLoading,
  phoneBindingTestLoading,
  paypalBindingLoading,
  baxiCdkSubmitLoading,
  onBatchPaymentLink,
  onCleanupPixLinks,
  onBatchInvalidRecheck,
  onOpenPhoneBindingTest,
  onOpenPaypalBinding,
  onOpenBaxiCdkSubmit,
  onOpenBatchGopay,
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
  const exportMenuItems: MenuProps['items'] = [
    { key: 'sub2api', label: 'Sub2API JSON（默认）' },
    { key: 'access_token', label: '仅 AccessToken（每行一个）' },
    { type: 'divider' },
    {
      key: 'pix_selected',
      label: `PIX 支付链接（已选账号 ${selectedRowKeys.length}）`,
      disabled: selectedRowKeys.length === 0,
    },
    {
      key: 'pix_filtered',
      label: `PIX 支付链接（当前筛选 ${total}）`,
      disabled: total === 0,
    },
  ]
  const handleExportMenuClick: MenuProps['onClick'] = ({ key }) => {
    const exportKey = String(key)
    if (exportKey === 'pix_selected') {
      onExportCsv('pix_payment_links', 'selected')
      return
    }
    if (exportKey === 'pix_filtered') {
      onExportCsv('pix_payment_links', 'filtered')
      return
    }
    onExportCsv(exportKey === 'access_token' ? 'access_token' : 'sub2api')
  }
  const paymentLinkMenuItems: MenuProps['items'] = [
    {
      key: 'normal',
      label: '支付链接生成（可复用缓存）',
      disabled: paymentLinkDisabled,
    },
    {
      key: 'force',
      label: '强制重新生成',
      disabled: paymentLinkDisabled,
    },
    { type: 'divider' },
    {
      key: 'pix_cleanup_expired',
      label: '清理过期 PIX 链接',
      icon: <DeleteOutlined />,
      danger: true,
      disabled: pixLinkCleanupLoading,
    },
    {
      key: 'pix_cleanup_paid',
      label: '清理已支付 PIX 链接',
      icon: <DeleteOutlined />,
      danger: true,
      disabled: pixLinkCleanupLoading,
    },
    {
      key: 'pix_cleanup_cancelled',
      label: '清理支付已取消 PIX 链接',
      icon: <DeleteOutlined />,
      danger: true,
      disabled: pixLinkCleanupLoading,
    },
  ]
  const paymentLinkActionLoading = batchPaymentLinkLoading || pixLinkCleanupLoading
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
          label: 'iDEAL / PIX 批量提交',
          icon: baxiCdkSubmitLoading ? <SyncOutlined spin /> : <DatabaseOutlined />,
          disabled: baxiCdkSubmitLoading,
        } as ToolbarMenuItem
      case 'paymentLink':
        return buildNestedMenuItem(
          actionId,
          '支付链接生成',
          paymentLinkActionLoading ? <SyncOutlined spin /> : <LinkOutlined />,
          paymentLinkMenuItems,
          paymentLinkActionLoading,
        )
      case 'gopay':
        return {
          key: actionId,
          label: '批量 GoPay',
          icon: batchGopayLoading ? <SyncOutlined spin /> : <LinkOutlined />,
          disabled: batchGopayLoading,
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
        const cleanupMode = pixCleanupModeFromMenuKey(originalKey)
        if (cleanupMode) {
          onCleanupPixLinks(cleanupMode)
          return
        }
        onBatchPaymentLink({ forceRefresh: originalKey === 'force' })
      }
      return
    }

    switch (rawKey) {
      case 'invalidRecheck':
        onBatchInvalidRecheck()
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
            iDEAL / PIX 批量提交
          </Button>
        )
      case 'paymentLink':
        return (
          <Dropdown
            key={actionId}
            disabled={paymentLinkActionLoading}
            menu={{
              items: paymentLinkMenuItems,
              onClick: ({ key }) => {
                const cleanupMode = pixCleanupModeFromMenuKey(key)
                if (cleanupMode) {
                  onCleanupPixLinks(cleanupMode)
                  return
                }
                onBatchPaymentLink({ forceRefresh: String(key) === 'force' })
              },
            }}
          >
            <Button
              block={isMobile}
              style={operationButtonStyle}
              icon={<LinkOutlined />}
              loading={paymentLinkActionLoading}
            >
              支付链接生成 <DownOutlined />
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
