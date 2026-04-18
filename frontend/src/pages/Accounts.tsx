import { useEffect, useState, useCallback, useRef } from 'react'
import { useParams } from 'react-router-dom'
import {
  Table,
  Button,
  Checkbox,
  Input,
  InputNumber,
  Select,
  Tag,
  Space,
  Modal,
  Form,
  message,
  Popconfirm,
  Dropdown,
  Typography,
  Alert,
  theme,
} from 'antd'
import type { MenuProps } from 'antd'
import {
  ReloadOutlined,
  CopyOutlined,
  LinkOutlined,
  PlusOutlined,
  DownloadOutlined,
  UploadOutlined,
  MoreOutlined,
  DeleteOutlined,
  SyncOutlined,
} from '@ant-design/icons'
import { ChatGPTRegistrationModeSwitch } from '@/components/ChatGPTRegistrationModeSwitch'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import { TaskVerificationPanel } from '@/components/TaskVerificationPanel'
import { usePersistentChatGPTRegistrationMode } from '@/hooks/usePersistentChatGPTRegistrationMode'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import { buildChatGPTRegistrationRequestAdapter } from '@/lib/chatgptRegistrationRequestAdapter'
import { CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN } from '@/lib/chatgptRegistrationMode'
import { apiFetch } from '@/lib/utils'
import { normalizeExecutorForPlatform } from '@/lib/platformExecutorOptions'

const { Text } = Typography

const REGISTER_FORM_SETTINGS_STORAGE_PREFIX = 'any-auto-register.register-form-settings.'

const STATUS_COLORS: Record<string, string> = {
  registered: 'default',
  trial: 'success',
  subscribed: 'success',
  expired: 'warning',
  invalid: 'error',
}

function pendingInviteStatusMeta(status?: string) {
  switch (status) {
    case 'completed':
      return { color: 'success', label: '已完成' }
    case 'failed_retryable':
    case 'failed':
      return { color: 'warning', label: '可重试失败' }
    case 'failed_terminal':
      return { color: 'error', label: '终止失败' }
    case 'abandoned':
      return { color: 'default', label: '已放弃' }
    case 'activation_fetching_invite_mail':
      return { color: 'processing', label: '拉取邀请邮件' }
    case 'activation_auth_login':
      return { color: 'processing', label: '登录准备中' }
    case 'activation_consuming_invite':
      return { color: 'processing', label: '消费邀请中' }
    case 'activation_capturing_workspace':
      return { color: 'processing', label: '抓取空间中' }
    case 'invite_sent_pending_activation':
      return { color: 'blue', label: '待激活' }
    default:
      return { color: 'default', label: status || '未知' }
  }
}

function parseExtraJson(raw: string | undefined) {
  if (!raw) return {}
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function getRegisterFormSettingsStorageKey(platform: string) {
  return `${REGISTER_FORM_SETTINGS_STORAGE_PREFIX}${String(platform || 'unknown').trim().toLowerCase() || 'unknown'}`
}

function loadRegisterFormSettings(platform: string) {
  if (typeof window === 'undefined') return {}
  try {
    const raw = window.localStorage.getItem(getRegisterFormSettingsStorageKey(platform))
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function saveRegisterFormSettings(platform: string, values: Record<string, unknown>) {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(getRegisterFormSettingsStorageKey(platform), JSON.stringify(values))
}

function normalizeAccount(account: any) {
  const extra = parseExtraJson(account.extra_json)
  const syncStatuses = extra.sync_statuses && typeof extra.sync_statuses === 'object' ? extra.sync_statuses : {}
  const cliproxySync = syncStatuses.cliproxyapi && typeof syncStatuses.cliproxyapi === 'object' ? syncStatuses.cliproxyapi : {}
  const sub2apiSync = syncStatuses.sub2api && typeof syncStatuses.sub2api === 'object' ? syncStatuses.sub2api : {}
  const chatgptLocal = extra.chatgpt_local && typeof extra.chatgpt_local === 'object' ? extra.chatgpt_local : {}
  const teamInviteSource = account.team_invite_source && typeof account.team_invite_source === 'object'
    ? account.team_invite_source
    : null
  return { ...account, extra, cliproxySync, sub2apiSync, chatgptLocal, teamInviteSource }
}

function formatSyncTime(value?: string) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function formatCreatedAt(value?: string) {
  if (!value) return { date: '-', time: '' }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return { date: value, time: '' }
  }
  return {
    date: date.toLocaleDateString(),
    time: date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
  }
}

function getTeamInviteOwnerLabel(source: any) {
  if (!source || typeof source !== 'object') return ''
  return String(
    source.team_email
    || source.primary_account_name
    || source.primary_account_id
    || source.team_account_id
    || source.team_name
    || ''
  ).trim()
}

function getTeamInviteActionLabel(source: any) {
  const inviteStatus = String(source?.invite_status || '').trim().toLowerCase()
  if (inviteStatus && inviteStatus !== 'completed') return '撤销邀请'
  return '移除队伍'
}

function authStateMeta(state?: string) {
  switch (state) {
    case 'refresh_token_valid':
      return { color: 'success', label: 'RT有效' }
    case 'access_token_valid':
      return { color: 'success', label: 'AT有效' }
    case 'account_deactivated':
      return { color: 'error', label: '已失效' }
    case 'refresh_token_invalidated':
      return { color: 'error', label: 'RT失效' }
    case 'access_token_invalidated':
      return { color: 'error', label: 'AT失效' }
    case 'unauthorized':
      return { color: 'error', label: '未授权' }
    case 'missing_refresh_token':
      return { color: 'default', label: '缺少RT' }
    case 'banned_like':
      return { color: 'error', label: '疑似封禁' }
    case 'probe_failed':
      return { color: 'warning', label: '探测失败' }
    default:
      return { color: 'default', label: '未探测' }
  }
}

function codexStateMeta(state?: string) {
  switch (state) {
    case 'usable':
      return { color: 'success', label: '可用' }
    case 'account_deactivated':
      return { color: 'error', label: '已失效' }
    case 'refresh_token_invalidated':
      return { color: 'error', label: 'RT失效' }
    case 'access_token_invalidated':
      return { color: 'error', label: 'AT失效' }
    case 'unauthorized':
      return { color: 'error', label: '未授权' }
    case 'payment_required':
      return { color: 'warning', label: '需付费/权限' }
    case 'quota_exhausted':
      return { color: 'warning', label: '额度耗尽' }
    case 'skipped_auth_invalid':
      return { color: 'default', label: '未测' }
    case 'probe_failed':
      return { color: 'warning', label: '探测失败' }
    default:
      return { color: 'default', label: '未探测' }
  }
}

function planMeta(plan?: string) {
  switch ((plan || '').toLowerCase()) {
    case 'plus':
      return { color: 'success', label: 'Plus' }
    case 'team':
      return { color: 'processing', label: 'Team' }
    case 'enterprise':
      return { color: 'processing', label: 'Enterprise' }
    case 'pro':
      return { color: 'processing', label: 'Pro' }
    case 'free':
      return { color: 'default', label: 'Free' }
    default:
      return { color: 'default', label: '未知' }
  }
}

function formatStructuredText(value?: string) {
  if (!value) return ''
  const trimmed = String(value).trim()
  if (!trimmed) return ''
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    try {
      return JSON.stringify(JSON.parse(trimmed), null, 2)
    } catch {
      return trimmed
    }
  }
  return trimmed
}

function SummaryField({
  label,
  value,
  code = false,
}: {
  label: string
  value?: string
  code?: boolean
}) {
  const { token } = theme.useToken()
  if (!value) return null

  const content = code ? formatStructuredText(value) : value
  const isBlock = code || content.length > 96 || content.includes('\n')

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: '104px minmax(0, 1fr)',
        gap: 12,
        alignItems: 'start',
      }}
    >
      <Text type="secondary" style={{ fontSize: 12, lineHeight: '20px' }}>
        {label}
      </Text>
      {isBlock ? (
        <pre
          style={{
            margin: 0,
            padding: code ? '8px 10px' : 0,
            borderRadius: code ? token.borderRadius : 0,
            border: code ? `1px solid ${token.colorBorder}` : 'none',
            background: code ? token.colorBgElevated : 'transparent',
            color: code ? token.colorText : token.colorTextSecondary,
            fontFamily: code ? 'SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace' : 'inherit',
            fontSize: 12,
            lineHeight: 1.6,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            overflowWrap: 'anywhere',
            maxHeight: code ? 160 : 'none',
            overflow: code ? 'auto' : 'visible',
          }}
        >
          {content}
        </pre>
      ) : (
        <Text style={{ display: 'block', color: token.colorTextSecondary, lineHeight: '20px' }}>
          {content}
        </Text>
      )}
    </div>
  )
}

function DetailSection({ title, children }: { title: string; children: React.ReactNode }) {
  const { token } = theme.useToken()

  return (
    <div
      style={{
        marginTop: 16,
        padding: 14,
        borderRadius: token.borderRadiusLG,
        border: `1px solid ${token.colorBorder}`,
        background: token.colorFillAlter,
      }}
    >
      <div style={{ marginBottom: 10, fontWeight: 600, color: token.colorText }}>{title}</div>
      {children}
    </div>
  )
}

function LocalProbeSummary({ probe }: { probe: any }) {
  const checkedAt = probe?.checked_at || probe?.auth?.checked_at || probe?.subscription?.checked_at || probe?.codex?.checked_at
  const auth = probe?.auth || {}
  const subscription = probe?.subscription || {}
  const codex = probe?.codex || {}

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <Tag color={authStateMeta(auth.state).color}>认证: {authStateMeta(auth.state).label}</Tag>
        <Tag color={planMeta(subscription.plan).color}>订阅: {planMeta(subscription.plan).label}</Tag>
        <Tag color={codexStateMeta(codex.state).color}>Codex: {codexStateMeta(codex.state).label}</Tag>
      </div>
      <SummaryField label="探测时间" value={checkedAt ? formatSyncTime(checkedAt) : ''} />
      <SummaryField label="认证信息" value={auth.message} code />
      <SummaryField label="工作区套餐" value={subscription.workspace_plan_type} />
      <SummaryField label="Codex 信息" value={codex.message} code />
    </div>
  )
}

function cliproxyStateMeta(sync: any) {
  if (!sync || Object.keys(sync).length === 0) {
    return { color: 'default', label: '未同步' }
  }
  if (sync.remote_state === 'unreachable') {
    return { color: 'error', label: '不可连接' }
  }
  if (sync.remote_state === 'not_found') {
    return { color: 'default', label: '远端未发现' }
  }
  if (!sync.uploaded) {
    return { color: 'default', label: '未发现' }
  }
  if (sync.remote_state === 'usable') {
    return { color: 'success', label: '远端可用' }
  }
  if (sync.remote_state === 'account_deactivated') {
    return { color: 'error', label: '远端已失效' }
  }
  if (sync.remote_state === 'access_token_invalidated') {
    return { color: 'error', label: '远端AT失效' }
  }
  if (sync.remote_state === 'unauthorized') {
    return { color: 'error', label: '远端未授权' }
  }
  if (sync.remote_state === 'payment_required') {
    return { color: 'warning', label: '远端需付费/权限' }
  }
  if (sync.remote_state === 'quota_exhausted') {
    return { color: 'warning', label: '远端额度耗尽' }
  }
  if (sync.status === 'active') {
    return { color: 'processing', label: '远端Active' }
  }
  if (sync.status === 'refreshing') {
    return { color: 'processing', label: '远端刷新中' }
  }
  if (sync.status === 'pending') {
    return { color: 'default', label: '远端待处理' }
  }
  if (sync.status === 'error') {
    return { color: 'error', label: '远端错误' }
  }
  if (sync.status === 'disabled') {
    return { color: 'default', label: '远端禁用' }
  }
  return { color: 'default', label: '未同步' }
}

function CliproxySyncSummary({ sync }: { sync: any }) {
  const meta = cliproxyStateMeta(sync)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <Tag color={meta.color}>{meta.label}</Tag>
        {sync?.status ? <Tag>{`status: ${sync.status}`}</Tag> : null}
      </div>
      <SummaryField label="状态信息" value={sync?.status_message} code />
      <SummaryField label="auth-file" value={sync?.name} />
      <SummaryField label="API URL" value={sync?.base_url} />
      <SummaryField label="同步时间" value={sync?.last_synced_at ? formatSyncTime(sync.last_synced_at) : ''} />
      <SummaryField label="远端刷新时间" value={sync?.last_refresh ? formatSyncTime(sync.last_refresh) : ''} />
      <SummaryField label="下次重试时间" value={sync?.next_retry_after ? formatSyncTime(sync.next_retry_after) : ''} />
      <SummaryField label="探测信息" value={sync?.last_probe_message} code />
    </div>
  )
}

function sub2apiStateMeta(sync: any) {
  if (!sync || Object.keys(sync).length === 0) {
    return { color: 'default', label: '未同步' }
  }
  if (sync.remote_state === 'unreachable') {
    return { color: 'error', label: 'DB不可达' }
  }
  if (sync.remote_state === 'ambiguous') {
    return { color: 'warning', label: '多条候选' }
  }
  if (sync.remote_state === 'cross_workspace_only') {
    return { color: 'processing', label: '其他工作区已存在' }
  }
  if (sync.remote_state === 'not_found') {
    return { color: 'default', label: '远端未发现' }
  }
  if (sync.remote_state === 'exists') {
    return { color: 'success', label: '远端已存在' }
  }
  if (sync.status === 'active') {
    return { color: 'processing', label: '远端Active' }
  }
  if (sync.status === 'error') {
    return { color: 'error', label: '远端错误' }
  }
  return { color: 'default', label: '未同步' }
}

function Sub2ApiSyncSummary({ sync }: { sync: any }) {
  const meta = sub2apiStateMeta(sync)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <Tag color={meta.color}>{meta.label}</Tag>
        {sync?.status ? <Tag>{`status: ${sync.status}`}</Tag> : null}
        {sync?.matched_by ? <Tag>{`matched_by: ${sync.matched_by}`}</Tag> : null}
      </div>
      <SummaryField label="远端账号 ID" value={sync?.remote_account_id ? String(sync.remote_account_id) : ''} />
      <SummaryField label="匹配方式" value={sync?.matched_by} />
      <SummaryField label="候选数量" value={sync?.candidate_count ? String(sync.candidate_count) : ''} />
      <SummaryField label="最近探测" value={sync?.checked_at ? formatSyncTime(sync.checked_at) : ''} />
      <SummaryField label="最近尝试" value={sync?.last_attempt_at ? formatSyncTime(sync.last_attempt_at) : ''} />
      <SummaryField label="状态信息" value={sync?.message || sync?.last_message} code />
      <SummaryField label="候选明细" value={sync?.candidates ? JSON.stringify(sync.candidates, null, 2) : ''} code />
    </div>
  )
}

function summarizeSub2ApiStates(items: any[]) {
  const summary = { exists: 0, notFound: 0, crossWorkspace: 0, ambiguous: 0, unreachable: 0, unknown: 0, pending: 0 }
  for (const item of items || []) {
    const sync = item?.sub2apiSync || {}
    const remoteState = String(sync?.remote_state || '').trim().toLowerCase()
    if (!sync || Object.keys(sync).length === 0) {
      summary.unknown += 1
      summary.pending += 1
    } else if (remoteState === 'exists') {
      summary.exists += 1
    } else if (remoteState === 'not_found') {
      summary.notFound += 1
      summary.pending += 1
    } else if (remoteState === 'cross_workspace_only') {
      summary.crossWorkspace += 1
      summary.pending += 1
    } else if (remoteState === 'ambiguous') {
      summary.ambiguous += 1
    } else if (remoteState === 'unreachable') {
      summary.unreachable += 1
    } else {
      summary.unknown += 1
      summary.pending += 1
    }
  }
  return summary
}

function ActionMenu({ acc, onRefresh, actions }: { acc: any; onRefresh: () => Promise<void> | void; actions: any[] }) {
  const [resultOpen, setResultOpen] = useState(false)
  const [resultTitle, setResultTitle] = useState('')
  const [resultStatus, setResultStatus] = useState<'success' | 'error'>('success')
  const [resultText, setResultText] = useState('')
  const [resultUrl, setResultUrl] = useState('')
  const [resultProbe, setResultProbe] = useState<any>(null)
  const [resultCliproxySync, setResultCliproxySync] = useState<any>(null)

  const showResult = (title: string, status: 'success' | 'error', text: string, url = '', probe: any = null, cliproxySync: any = null) => {
    setResultTitle(title)
    setResultStatus(status)
    setResultText(text)
    setResultUrl(url)
    setResultProbe(probe)
    setResultCliproxySync(cliproxySync)
    setResultOpen(true)
  }

  const copyResultUrl = async () => {
    if (!resultUrl) return
    try {
      await navigator.clipboard.writeText(resultUrl)
      message.success('链接已复制')
    } catch {
      message.error('复制失败')
    }
  }

  const handleAction = async (actionId: string) => {
    const actionLabel = actions.find((item) => item.id === actionId)?.label || actionId

    try {
      const r = await apiFetch(`/actions/${acc.platform}/${acc.id}/${actionId}`, {
        method: 'POST',
        body: JSON.stringify({ params: {} }),
      })
      if (!r.ok) {
        const data = r.data || {}
        const probe = typeof data === 'object' && data ? data.probe || null : null
        const cliproxySync = typeof data === 'object' && data ? data.sync || null : null
        showResult(actionLabel, 'error', r.error || data.message || '操作失败', '', probe, cliproxySync)
        await onRefresh()
        return
      }
      const data = r.data || {}
      if (data.url || data.checkout_url || data.cashier_url) {
        const targetUrl = data.url || data.checkout_url || data.cashier_url
        message.success('链接已生成')
        showResult(actionLabel, 'success', '操作成功，请在弹窗中打开或复制链接。', targetUrl)
      } else {
        message.success(data.message || '操作成功')
        const probe = typeof data === 'object' && data ? data.probe || null : null
        const cliproxySync = typeof data === 'object' && data ? data.sync || null : null
        const text =
          probe
            ? String(data.message || '操作成功')
            : cliproxySync
            ? String(data.message || '操作成功')
            : typeof data === 'string'
            ? data
            : Object.keys(data).length > 0
              ? JSON.stringify(data, null, 2)
              : '操作成功'
        showResult(actionLabel, 'success', text, '', probe, cliproxySync)
      }
      await onRefresh()
    } catch (e: any) {
      const detail = e?.message ? String(e.message) : '请求失败'
      message.error(detail)
      showResult(actionLabel, 'error', detail)
    }
  }

  const menuItems: MenuProps['items'] = actions.map((a) => ({
    key: a.id,
    label: a.label,
  }))

  if (actions.length === 0) return null

  return (
    <>
      <Dropdown
        menu={{
          items: menuItems,
          onClick: ({ key }) => handleAction(String(key)),
        }}
      >
        <Button type="link" size="small" icon={<MoreOutlined />} />
      </Dropdown>
      <Modal
        title={resultTitle}
        open={resultOpen}
        onCancel={() => setResultOpen(false)}
        footer={[
          resultUrl ? (
            <Button key="copy" onClick={copyResultUrl}>
              复制链接
            </Button>
          ) : null,
          resultUrl ? (
            <Button
              key="open"
              type="primary"
              onClick={() => window.open(resultUrl, '_blank', 'noopener,noreferrer')}
            >
              打开链接
            </Button>
          ) : null,
          <Button key="ok" type={resultUrl ? 'default' : 'primary'} onClick={() => setResultOpen(false)}>
            确定
          </Button>,
        ].filter(Boolean)}
        maskClosable={false}
      >
        <Alert
          type={resultStatus}
          showIcon
          message={resultStatus === 'success' ? '操作完成' : '操作失败'}
          style={{ marginBottom: 12 }}
        />
        {resultProbe ? (
          <div style={{ marginBottom: 12 }}>
            <LocalProbeSummary probe={resultProbe} />
          </div>
        ) : null}
        {resultCliproxySync ? (
          <div style={{ marginBottom: 12 }}>
            <CliproxySyncSummary sync={resultCliproxySync} />
          </div>
        ) : null}
        {resultUrl ? (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Text copyable={{ text: resultUrl }} style={{ wordBreak: 'break-all' }}>
              {resultUrl}
            </Text>
          </Space>
        ) : null}
        {resultText ? (
          <pre
            style={{
              margin: 0,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              fontFamily: 'monospace',
              fontSize: 12,
            }}
          >
            {resultText}
          </pre>
        ) : null}
      </Modal>
    </>
  )
}

export default function Accounts() {
  const { platform } = useParams<{ platform: string }>()
  const { token } = theme.useToken()
  const [currentPlatform, setCurrentPlatform] = useState(platform || 'chatgpt')
  const [accounts, setAccounts] = useState<any[]>([])
  const [platformActions, setPlatformActions] = useState<any[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([])

  const [registerModalOpen, setRegisterModalOpen] = useState(false)
  const [addModalOpen, setAddModalOpen] = useState(false)
  const [importModalOpen, setImportModalOpen] = useState(false)
  const [detailModalOpen, setDetailModalOpen] = useState(false)
  const [businessDeferredModalOpen, setBusinessDeferredModalOpen] = useState(false)
  const [currentAccount, setCurrentAccount] = useState<any>(null)
  const [removingTeamAccountId, setRemovingTeamAccountId] = useState<number | null>(null)

  const [registerForm] = Form.useForm()
  const [addForm] = Form.useForm()
  const [detailForm] = Form.useForm()
  const [registerMailProvider, setRegisterMailProvider] = useState('luckmail')
  const registerProviderOverride = Form.useWatch('mail_provider_override', registerForm)
  const effectiveRegisterMailProvider =
    currentPlatform === 'chatgpt' && registerProviderOverride && registerProviderOverride !== '__global__'
      ? registerProviderOverride
      : registerMailProvider
  const { mode: chatgptRegistrationMode, setMode: setChatgptRegistrationMode } =
    usePersistentChatGPTRegistrationMode()
  const [importText, setImportText] = useState('')
  const [importLoading, setImportLoading] = useState(false)
  const [taskId, setTaskId] = useState<string | null>(null)
  const [taskSnapshot, setTaskSnapshot] = useState<any>(null)
  const [registerLoading, setRegisterLoading] = useState(false)
  const [registerSettingsSaving, setRegisterSettingsSaving] = useState(false)
  const [pendingBusinessInvites, setPendingBusinessInvites] = useState<any[]>([])
  const [pendingBusinessInvitesLoading, setPendingBusinessInvitesLoading] = useState(false)
  const [selectedPendingInviteRowKeys, setSelectedPendingInviteRowKeys] = useState<React.Key[]>([])
  const [activatingPendingInviteId, setActivatingPendingInviteId] = useState<number | null>(null)
  const [abandoningPendingInviteId, setAbandoningPendingInviteId] = useState<number | null>(null)
  const [activatingAllPendingInvites, setActivatingAllPendingInvites] = useState(false)
  const [backfillLoading, setBackfillLoading] = useState<'' | 'cliproxyapi_pending' | 'cliproxyapi_selected' | 'sub2api_pending' | 'sub2api_selected'>('')
  const [statusSyncLoading, setStatusSyncLoading] = useState<
    'probe_selected' | 'probe_all' | 'remote_selected' | 'remote_all' | 'sub2api_selected' | 'sub2api_all' | ''
  >('')
  const [sub2apiOverviewSyncing, setSub2apiOverviewSyncing] = useState(false)
  const [deleteInvalidLoading, setDeleteInvalidLoading] = useState(false)
  const sub2apiOverviewSyncKeyRef = useRef('')

  useEffect(() => {
    if (platform) setCurrentPlatform(platform)
  }, [platform])

  useEffect(() => {
    if (!detailModalOpen || !currentAccount) return
    detailForm.setFieldsValue({
      status: currentAccount.status,
      token: currentAccount.token,
    })
  }, [detailModalOpen, currentAccount, detailForm])

  const fetchAccountDetail = useCallback(async (accountId: number) => {
    const data = await apiFetch(`/accounts/${accountId}`)
    const normalized = normalizeAccount(data)
    setCurrentAccount(normalized)
    return normalized
  }, [])

  useEffect(() => {
    if (!detailModalOpen || !currentAccount?.id) return
    const latest = accounts.find((item) => item.id === currentAccount.id)
    if (latest && latest !== currentAccount) {
      setCurrentAccount(latest)
    }
    fetchAccountDetail(currentAccount.id).catch(() => {})
  }, [accounts, detailModalOpen, currentAccount?.id, fetchAccountDetail])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams({ platform: currentPlatform, page: '1', page_size: '100' })
      if (search) params.set('email', search)
      if (filterStatus) params.set('status', filterStatus)
      const data = await apiFetch(`/accounts?${params}`)
      setAccounts((data.items || []).map(normalizeAccount))
      setTotal(data.total)
    } finally {
      setLoading(false)
    }
  }, [currentPlatform, search, filterStatus])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    apiFetch(`/actions/${currentPlatform}`)
      .then((data) => setPlatformActions(data.actions || []))
      .catch(() => setPlatformActions([]))
  }, [currentPlatform])

  const loadPendingBusinessInvites = useCallback(async () => {
    if (currentPlatform !== 'chatgpt') return
    setPendingBusinessInvitesLoading(true)
    try {
      const data = await apiFetch('/chatgpt/pending-business-invites?limit=200')
      setPendingBusinessInvites(data.items || [])
      setSelectedPendingInviteRowKeys((prev) => prev.filter((key) => (data.items || []).some((item: any) => item.id === key)))
    } catch (e: any) {
      message.error(`加载 pending invite 失败: ${e.message}`)
    } finally {
      setPendingBusinessInvitesLoading(false)
    }
  }, [currentPlatform])

  useEffect(() => {
    if (!businessDeferredModalOpen || currentPlatform !== 'chatgpt') return
    loadPendingBusinessInvites()
  }, [businessDeferredModalOpen, currentPlatform, loadPendingBusinessInvites])

  const handleActivatePendingInvite = async (inviteId: number) => {
    setActivatingPendingInviteId(inviteId)
    try {
      const res = await apiFetch(`/chatgpt/pending-business-invites/${inviteId}/activate`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      message.success(`激活成功：${res.email || inviteId}`)
      await loadPendingBusinessInvites()
      load()
    } catch (e: any) {
      message.error(`激活失败: ${e.message}`)
      await loadPendingBusinessInvites()
    } finally {
      setActivatingPendingInviteId(null)
    }
  }

  const getSelectedPendingInviteIds = () =>
    Array.from(selectedPendingInviteRowKeys)
      .map((value) => Number(value))
      .filter((value) => Number.isInteger(value) && value > 0)

  const getRetryablePendingInviteIds = () =>
    pendingBusinessInvites
      .filter((item: any) => item.can_activate !== false)
      .map((item: any) => Number(item.id))
      .filter((value: number) => Number.isInteger(value) && value > 0)

  const handleBatchActivatePendingInvites = async (inviteIds?: number[]) => {
    const resolvedIds = (inviteIds || []).filter((value) => Number.isInteger(value) && value > 0)
    if (resolvedIds.length === 0) {
      message.info('没有可补激活的记录')
      return
    }
    setActivatingAllPendingInvites(true)
    try {
      const res = await apiFetch('/chatgpt/pending-business-invites/batch-activate', {
        method: 'POST',
        body: JSON.stringify({ invite_ids: resolvedIds, limit: 200 }),
      })
      message.success(`批量激活完成：成功 ${res.success || 0} / ${res.total || 0}`)
      await loadPendingBusinessInvites()
      load()
    } catch (e: any) {
      message.error(`批量激活失败: ${e.message}`)
      await loadPendingBusinessInvites()
    } finally {
      setActivatingAllPendingInvites(false)
    }
  }

  const handleActivateAllPendingInvites = async () => {
    await handleBatchActivatePendingInvites(getRetryablePendingInviteIds())
  }

  const handleActivateSelectedPendingInvites = async () => {
    const inviteIds = getSelectedPendingInviteIds()
    if (inviteIds.length === 0) {
      message.warning('请先选择要补激活的记录')
      return
    }
    await handleBatchActivatePendingInvites(inviteIds)
  }

  const handleAbandonPendingInvite = async (inviteId: number) => {
    setAbandoningPendingInviteId(inviteId)
    try {
      await apiFetch(`/chatgpt/pending-business-invites/${inviteId}/abandon`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      message.success(`已标记放弃：${inviteId}`)
      await loadPendingBusinessInvites()
    } catch (e: any) {
      message.error(`标记放弃失败: ${e.message}`)
    } finally {
      setAbandoningPendingInviteId(null)
    }
  }

  useEffect(() => {
    if (!registerModalOpen) return
    apiFetch('/config')
      .then((cfg) => {
        const provider = String(cfg?.mail_provider || 'luckmail').trim() || 'luckmail'
        const savedSettings = loadRegisterFormSettings(currentPlatform)
        const savedEmail = window.localStorage.getItem('any-auto-register.manual_email_otp.email') || ''
        setRegisterMailProvider(provider)
        registerForm.setFieldsValue({
          count: Number(savedSettings.count || 1) || 1,
          concurrency: Number(savedSettings.concurrency || 1) || 1,
          register_delay_seconds: Number(savedSettings.register_delay_seconds || 0) || 0,
          mail_provider_override: String(savedSettings.mail_provider_override || '__global__'),
          email: String(savedSettings.email || savedEmail || '').trim(),
          chatgpt_enable_team_invite:
            savedSettings.chatgpt_enable_team_invite ?? parseBooleanConfigValue(cfg.chatgpt_enable_team_invite),
          chatgpt_team_invite_deferred_activation:
            savedSettings.chatgpt_team_invite_deferred_activation ?? parseBooleanConfigValue(cfg.chatgpt_team_invite_deferred_activation),
          chatgpt_capture_business_workspace:
            savedSettings.chatgpt_capture_business_workspace
            ?? (cfg.chatgpt_capture_business_workspace === '' ? true : parseBooleanConfigValue(cfg.chatgpt_capture_business_workspace)),
          chatgpt_capture_free_workspace:
            savedSettings.chatgpt_capture_free_workspace
            ?? (cfg.chatgpt_capture_free_workspace === '' ? true : parseBooleanConfigValue(cfg.chatgpt_capture_free_workspace)),
        })
      })
      .catch(() => {
        setRegisterMailProvider('luckmail')
        const savedSettings = loadRegisterFormSettings(currentPlatform)
        const savedEmail = window.localStorage.getItem('any-auto-register.manual_email_otp.email') || ''
        registerForm.setFieldsValue({
          count: Number(savedSettings.count || 1) || 1,
          concurrency: Number(savedSettings.concurrency || 1) || 1,
          register_delay_seconds: Number(savedSettings.register_delay_seconds || 0) || 0,
          mail_provider_override: String(savedSettings.mail_provider_override || '__global__'),
          email: String(savedSettings.email || savedEmail || '').trim(),
          chatgpt_enable_team_invite: savedSettings.chatgpt_enable_team_invite ?? false,
          chatgpt_team_invite_deferred_activation: savedSettings.chatgpt_team_invite_deferred_activation ?? false,
          chatgpt_capture_business_workspace: savedSettings.chatgpt_capture_business_workspace ?? true,
          chatgpt_capture_free_workspace: savedSettings.chatgpt_capture_free_workspace ?? true,
        })
      })
  }, [registerModalOpen, currentPlatform, registerForm])

  useEffect(() => {
    if (!taskId) {
      setTaskSnapshot(null)
      return
    }

    let cancelled = false
    let timer: number | null = null

    const pull = async () => {
      try {
        const snapshot = await apiFetch(`/tasks/${taskId}`)
        if (cancelled) return
        setTaskSnapshot(snapshot)
        if (!['done', 'failed', 'stopped'].includes(String(snapshot?.status || ''))) {
          timer = window.setTimeout(pull, 1000)
        }
      } catch {
        if (cancelled) return
        timer = window.setTimeout(pull, 1500)
      }
    }

    void pull()

    return () => {
      cancelled = true
      if (timer != null) {
        window.clearTimeout(timer)
      }
    }
  }, [taskId])

  const copyText = (text: string) => {
    navigator.clipboard.writeText(text)
    message.success('已复制')
  }

  const getRefreshToken = (record: any): string => {
    try {
      const extra = JSON.parse(record.extra_json || '{}')
      return extra.refresh_token || ''
    } catch {
      return ''
    }
  }

  const exportCsv = () => {
    const header = 'email,password,status,region,cashier_url,created_at'
    const rows = accounts.map((a) => [a.email, a.password, a.status, a.region, a.cashier_url, a.created_at].join(','))
    const blob = new Blob([[header, ...rows].join('\n')], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${currentPlatform}_accounts.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  const handleDelete = async (id: number) => {
    await apiFetch(`/accounts/${id}`, { method: 'DELETE' })
    message.success('删除成功')
    load()
  }

  const handleRemoveFromTeam = async (record: any) => {
    setRemovingTeamAccountId(record.id)
    try {
      const res = await apiFetch(`/accounts/${record.id}/chatgpt-team-remove`, { method: 'POST' })
      message.success(res.message || `${getTeamInviteActionLabel(record.teamInviteSource)}成功`)
      const sourceFromResponse = res?.team_invite_source
      const teamInviteSource =
        sourceFromResponse && typeof sourceFromResponse === 'object'
          ? sourceFromResponse
          : {
            ...(record.teamInviteSource || {}),
            removed_from_team_at: (res?.team_invite_source?.removed_from_team_at || new Date().toISOString()),
            removable: false,
          }

      setCurrentAccount((prev: any) => {
        if (!prev || prev.id !== record.id) return prev
        return { ...prev, teamInviteSource }
      })

      setAccounts((prev) =>
        prev.map((item) =>
          item.id === record.id
            ? { ...item, teamInviteSource }
            : item,
        ),
      )


      await load()
    } catch (e: any) {
      message.error(e.message || '移除队伍失败')
    } finally {
      setRemovingTeamAccountId(null)
    }
  }

  const handleBatchDelete = async () => {
    if (selectedRowKeys.length === 0) return
    await apiFetch('/accounts/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ ids: Array.from(selectedRowKeys) }),
    })
    message.success('批量删除成功')
    setSelectedRowKeys([])
    load()
  }

  const handleDeleteInvalid = async () => {
    setDeleteInvalidLoading(true)
    try {
      const res = await apiFetch('/accounts/batch-delete-by-filter', {
        method: 'POST',
        body: JSON.stringify({
          platform: currentPlatform,
          status: 'invalid',
        }),
      })
      message.success(`已删除 ${res.deleted || 0} 个无效账号`)
      setSelectedRowKeys([])
      load()
    } catch (e: any) {
      message.error(`删除无效账号失败: ${e.message}`)
    } finally {
      setDeleteInvalidLoading(false)
    }
  }

  const handleAdd = async () => {
    const values = await addForm.validateFields()
    await apiFetch('/accounts', {
      method: 'POST',
      body: JSON.stringify({ ...values, platform: currentPlatform }),
    })
    message.success('添加成功')
    setAddModalOpen(false)
    addForm.resetFields()
    load()
  }

  const handleImport = async () => {
    if (!importText.trim()) return
    setImportLoading(true)
    try {
      const lines = importText.trim().split('\n').filter(Boolean)
      const res = await apiFetch('/accounts/import', {
        method: 'POST',
        body: JSON.stringify({ platform: currentPlatform, lines }),
      })
      message.success(`导入成功 ${res.created} 个`)
      setImportModalOpen(false)
      setImportText('')
      load()
    } catch (e: any) {
      message.error(`导入失败: ${e.message}`)
    } finally {
      setImportLoading(false)
    }
  }

  const handleSaveRegisterSettings = async () => {
    const values = registerForm.getFieldsValue(true)
    const settingsPayload = {
      count: Number(values.count || 1) || 1,
      concurrency: Number(values.concurrency || 1) || 1,
      register_delay_seconds: Number(values.register_delay_seconds || 0) || 0,
      mail_provider_override: String(values.mail_provider_override || '__global__'),
      email: String(values.email || '').trim(),
      chatgpt_enable_team_invite: Boolean(values.chatgpt_enable_team_invite),
      chatgpt_team_invite_deferred_activation: Boolean(values.chatgpt_team_invite_deferred_activation),
      chatgpt_capture_free_workspace:
        values.chatgpt_capture_free_workspace === undefined ? true : Boolean(values.chatgpt_capture_free_workspace),
      chatgpt_capture_business_workspace:
        values.chatgpt_capture_business_workspace === undefined ? true : Boolean(values.chatgpt_capture_business_workspace),
    }

    setRegisterSettingsSaving(true)
    try {
      saveRegisterFormSettings(currentPlatform, settingsPayload)
      if (settingsPayload.mail_provider_override === 'manual_email_otp' && settingsPayload.email) {
        window.localStorage.setItem('any-auto-register.manual_email_otp.email', settingsPayload.email)
      }
      message.success('注册设置已保存')
    } catch (e: any) {
      message.error(e?.message || '保存注册设置失败')
    } finally {
      setRegisterSettingsSaving(false)
    }
  }

  const handleRegister = async () => {
    const values = await registerForm.validateFields()
    setRegisterLoading(true)
    try {
      const cfg = await apiFetch('/config')
      const selectedProviderOverride = String(values.mail_provider_override || '').trim()
      const resolvedMailProvider =
        selectedProviderOverride && selectedProviderOverride !== '__global__'
          ? selectedProviderOverride
          : (String(cfg.mail_provider || 'luckmail').trim() || 'luckmail')
      setRegisterMailProvider(resolvedMailProvider)
      const executorType = normalizeExecutorForPlatform(currentPlatform, cfg.default_executor)
      const registerExtra = {
        mail_provider: resolvedMailProvider,
        applemail_base_url: cfg.applemail_base_url,
        applemail_pool_dir: cfg.applemail_pool_dir,
        applemail_pool_file: cfg.applemail_pool_file,
        applemail_mailboxes: cfg.applemail_mailboxes,
        laoudo_auth: cfg.laoudo_auth,
        laoudo_email: cfg.laoudo_email,
        laoudo_account_id: cfg.laoudo_account_id,
        gptmail_base_url: cfg.gptmail_base_url,
        gptmail_api_key: cfg.gptmail_api_key,
        gptmail_domain: cfg.gptmail_domain,
        maliapi_base_url: cfg.maliapi_base_url,
        maliapi_api_key: cfg.maliapi_api_key,
        maliapi_domain: cfg.maliapi_domain,
        maliapi_auto_domain_strategy: cfg.maliapi_auto_domain_strategy,
        yescaptcha_key: cfg.yescaptcha_key,
        moemail_api_url: cfg.moemail_api_url,
        moemail_api_key: cfg.moemail_api_key,
        tempmail_api_url: cfg.tempmail_api_url,
        tempmail_api_key: cfg.tempmail_api_key,
        tempmail_api_key_header: cfg.tempmail_api_key_header,
        tempmail_mode: cfg.tempmail_mode,
        tempmail_primary_domain: cfg.tempmail_primary_domain,
        tempmail_wait_timeout_seconds: cfg.tempmail_wait_timeout_seconds,
        tempmail_ttl_minutes: cfg.tempmail_ttl_minutes,
        tempmail_reuse_window_minutes: cfg.tempmail_reuse_window_minutes,
        tempmail_permanent: parseBooleanConfigValue(cfg.tempmail_permanent),
        tempmail_platform: cfg.tempmail_platform,
        skymail_api_base: cfg.skymail_api_base,
        skymail_token: cfg.skymail_token,
        skymail_domain: cfg.skymail_domain,
        cloudmail_api_base: cfg.cloudmail_api_base,
        cloudmail_admin_email: cfg.cloudmail_admin_email,
        cloudmail_admin_password: cfg.cloudmail_admin_password,
        cloudmail_domain: cfg.cloudmail_domain,
        cloudmail_subdomain: cfg.cloudmail_subdomain,
        cloudmail_timeout: cfg.cloudmail_timeout,
        duckmail_address: cfg.duckmail_address,
        duckmail_password: cfg.duckmail_password,
        duckmail_api_url: cfg.duckmail_api_url,
        duckmail_provider_url: cfg.duckmail_provider_url,
        duckmail_bearer: cfg.duckmail_bearer,
        freemail_api_url: cfg.freemail_api_url,
        freemail_admin_token: cfg.freemail_admin_token,
        freemail_username: cfg.freemail_username,
        freemail_password: cfg.freemail_password,
        freemail_domain: cfg.freemail_domain,
        cfworker_api_url: cfg.cfworker_api_url,
        cfworker_admin_token: cfg.cfworker_admin_token,
        cfworker_custom_auth: cfg.cfworker_custom_auth,
        cfworker_domain: cfg.cfworker_domain,
        cfworker_subdomain: cfg.cfworker_subdomain,
        cfworker_random_subdomain: parseBooleanConfigValue(cfg.cfworker_random_subdomain),
        cfworker_fingerprint: cfg.cfworker_fingerprint,
        smstome_cookie: cfg.smstome_cookie,
        smstome_country_slugs: cfg.smstome_country_slugs,
        smstome_phone_attempts: cfg.smstome_phone_attempts,
        smstome_otp_timeout_seconds: cfg.smstome_otp_timeout_seconds,
        smstome_poll_interval_seconds: cfg.smstome_poll_interval_seconds,
        smstome_sync_max_pages_per_country: cfg.smstome_sync_max_pages_per_country,
        luckmail_base_url: cfg.luckmail_base_url,
        luckmail_api_key: cfg.luckmail_api_key,
        luckmail_email_type: cfg.luckmail_email_type,
        luckmail_domain: cfg.luckmail_domain,
        chatgpt_enable_team_invite: currentPlatform === 'chatgpt' ? Boolean(values.chatgpt_enable_team_invite) : undefined,
        chatgpt_capture_free_workspace:
          currentPlatform === 'chatgpt'
            ? Boolean(values.chatgpt_capture_free_workspace)
            : undefined,
        chatgpt_capture_business_workspace:
          currentPlatform === 'chatgpt' && values.chatgpt_enable_team_invite
            ? values.chatgpt_capture_business_workspace
            : undefined,
        chatgpt_team_invite_deferred_activation:
          currentPlatform === 'chatgpt' && values.chatgpt_enable_team_invite
            ? Boolean(values.chatgpt_team_invite_deferred_activation)
            : undefined,
      }
      const chatgptRegistrationRequestAdapter =
        buildChatGPTRegistrationRequestAdapter(
          currentPlatform,
          chatgptRegistrationMode,
        )
      const adaptedRegisterExtra = chatgptRegistrationRequestAdapter
        ? chatgptRegistrationRequestAdapter.extendExtra(registerExtra)
        : registerExtra

      if (resolvedMailProvider === 'manual_email_otp' && currentPlatform === 'chatgpt') {
        const normalizedEmail = String(values.email || '').trim()
        if (!normalizedEmail) {
          throw new Error('手动邮箱模式必须填写邮箱地址')
        }
        window.localStorage.setItem('any-auto-register.manual_email_otp.email', normalizedEmail)
      }

      saveRegisterFormSettings(currentPlatform, {
        count: Number(values.count || 1) || 1,
        concurrency: Number(values.concurrency || 1) || 1,
        register_delay_seconds: Number(values.register_delay_seconds || 0) || 0,
        mail_provider_override: selectedProviderOverride || '__global__',
        email: String(values.email || '').trim(),
        chatgpt_enable_team_invite: Boolean(values.chatgpt_enable_team_invite),
        chatgpt_team_invite_deferred_activation: Boolean(values.chatgpt_team_invite_deferred_activation),
        chatgpt_capture_free_workspace: Boolean(values.chatgpt_capture_free_workspace),
        chatgpt_capture_business_workspace:
          values.chatgpt_capture_business_workspace === undefined ? true : Boolean(values.chatgpt_capture_business_workspace),
      })

      const res = await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify({
          platform: currentPlatform,
          email:
            resolvedMailProvider === 'manual_email_otp' && currentPlatform === 'chatgpt'
              ? (String(values.email || '').trim() || null)
              : null,
          count: values.count,
          concurrency: values.concurrency,
          register_delay_seconds: values.register_delay_seconds || 0,
          executor_type: executorType,
          captcha_solver: cfg.default_captcha_solver || 'yescaptcha',
          proxy: null,
          extra: adaptedRegisterExtra,
        }),
      })
      setTaskId(res.task_id)
    } catch (e: any) {
      message.error(e?.message || '创建注册任务失败')
    } finally {
      setRegisterLoading(false)
    }
  }

  const handleDetailSave = async () => {
    const values = await detailForm.validateFields()
    await apiFetch(`/accounts/${currentAccount.id}`, {
      method: 'PATCH',
      body: JSON.stringify(values),
    })
    message.success('保存成功')
    setDetailModalOpen(false)
    load()
  }

  const showBackfillResult = (title: string, result: any) => {
    const lines = (result.items || [])
      .flatMap((item: any) =>
        (item.results || []).map((syncResult: any) => ({
          email: item.email,
          platform: item.platform,
          ok: Boolean(syncResult.ok),
          name: syncResult.name || 'CPA',
          msg: syncResult.msg || '',
        })),
      )
      .filter((item: any) => !item.ok)
      .map((item: any) => `[${item.platform}] ${item.email || '-'} / ${item.name}: ${item.msg || '失败'}`)

    if (lines.length === 0) return

    Modal.info({
      title,
      width: 760,
      content: (
        <pre
          style={{
            margin: 0,
            maxHeight: 360,
            overflow: 'auto',
            padding: 12,
            borderRadius: 8,
            background: 'rgba(127,127,127,0.08)',
            fontSize: 12,
            lineHeight: 1.5,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {lines.join('\n')}
        </pre>
      ),
    })
  }

  const showBatchActionResult = (title: string, result: any) => {
    const lines = (result.items || [])
      .filter((item: any) => !item.ok)
      .map((item: any) => `[${item.id || '-'}] ${item.email || '-'}: ${item.message || '失败'}`)

    if (lines.length === 0) return

    Modal.info({
      title,
      width: 760,
      content: (
        <pre
          style={{
            margin: 0,
            maxHeight: 360,
            overflow: 'auto',
            padding: 12,
            borderRadius: 8,
            background: 'rgba(127,127,127,0.08)',
            fontSize: 12,
            lineHeight: 1.5,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {lines.join('\n')}
        </pre>
      ),
    })
  }

  const handleBackfill = async (destination: 'cliproxyapi' | 'sub2api', mode: 'pending' | 'selected') => {
    if (currentPlatform !== 'chatgpt') return

    const body: Record<string, unknown> = {
      platforms: ['chatgpt'],
      destination,
    }

    if (mode === 'selected') {
      const accountIds = Array.from(selectedRowKeys)
        .map((value) => Number(value))
        .filter((value) => Number.isInteger(value) && value > 0)

      if (accountIds.length === 0) {
        message.warning('请先选择要上传的账号')
        return
      }
      body.account_ids = accountIds
    } else {
      body.pending_only = true
      if (filterStatus) body.status = filterStatus
      if (search) body.email = search
    }

    const destinationLabel = destination === 'sub2api' ? 'Sub2API' : 'CLIProxyAPI'
    const loadingKey = `${destination}_${mode}` as typeof backfillLoading
    const actionLabel = mode === 'selected' ? `所选账号补传到 ${destinationLabel}` : `${destinationLabel} 待补传处理`
    const toastKey = `backfill:${loadingKey}`

    setBackfillLoading(loadingKey)
    message.loading({ content: `${actionLabel}进行中...`, key: toastKey, duration: 0 })
    try {
      const result = await apiFetch('/integrations/backfill', {
        method: 'POST',
        body: JSON.stringify(body),
      })

      const total = Number(result?.total || 0)
      const success = Number(result?.success || 0)
      const skipped = Number(result?.skipped || 0)
      const failed = Number(result?.failed || 0)

      if (!total) {
        message.info({ content: '没有可处理的账号', key: toastKey })
      } else if (success > 0 && failed === 0) {
        const summary = skipped > 0
          ? `${actionLabel}上传成功 ${success} 个，跳过 ${skipped} 个 / 共 ${total} 个`
          : `${actionLabel}上传成功 ${success} 个 / 共 ${total} 个`
        message.success({ content: summary, key: toastKey })
      } else if (success > 0) {
        message.warning({
          content: `${actionLabel}已上传成功 ${success} 个，跳过 ${skipped} 个，失败 ${failed} 个 / 共 ${total} 个`,
          key: toastKey,
          duration: 4,
        })
      } else if (skipped > 0 && failed === 0) {
        message.info({ content: `${actionLabel}未执行上传：跳过 ${skipped} 个 / 共 ${total} 个`, key: toastKey })
      } else {
        message.error({ content: `${actionLabel}上传失败：成功 ${success} 个，跳过 ${skipped} 个，失败 ${failed} 个 / 共 ${total} 个`, key: toastKey })
      }

      showBackfillResult(`${actionLabel}结果`, result)
      await load()
    } catch (e: any) {
      message.error({ content: `${destinationLabel} 补传失败: ${e.message}`, key: toastKey })
    } finally {
      setBackfillLoading('')
    }
  }

  const handleBatchStatusSync = async (kind: 'probe' | 'remote' | 'sub2api', scope: 'selected' | 'all') => {
    if (currentPlatform !== 'chatgpt') return

    const loadingKey = `${kind}_${scope}` as typeof statusSyncLoading
    const actionId =
      kind === 'probe'
        ? 'probe_local_status'
        : kind === 'sub2api'
          ? 'sync_sub2api_status'
          : 'sync_cliproxyapi_status'
    const actionLabel =
      kind === 'probe'
        ? '本地状态同步'
        : kind === 'sub2api'
          ? 'Sub2API 状态同步'
          : 'CLIProxyAPI 状态同步'
    const scopeLabel = scope === 'selected' ? '所选账号' : '当前筛选账号'
    const toastKey = `status-sync:${loadingKey}`

    const body: Record<string, unknown> = {
      params: {},
    }

    if (scope === 'selected') {
      const accountIds = Array.from(selectedRowKeys)
        .map((value) => Number(value))
        .filter((value) => Number.isInteger(value) && value > 0)

      if (accountIds.length === 0) {
        message.warning('请先选择要同步的账号')
        return
      }
      body.account_ids = accountIds
    } else {
      body.all_filtered = true
      if (search) body.email = search
      if (filterStatus) body.status = filterStatus
    }

    setStatusSyncLoading(loadingKey)
    message.loading({ content: `${scopeLabel}${actionLabel}进行中...`, key: toastKey, duration: 0 })
    try {
      const result = await apiFetch(`/actions/${currentPlatform}/${actionId}/batch`, {
        method: 'POST',
        body: JSON.stringify(body),
      })

      if (!result.total) {
        message.info({ content: '没有可处理的账号', key: toastKey })
      } else if (!result.failed) {
        message.success({ content: `${scopeLabel}${actionLabel}完成：成功 ${result.success} / ${result.total}`, key: toastKey })
      } else if (!result.success) {
        message.error({ content: `${scopeLabel}${actionLabel}失败：成功 ${result.success} / ${result.total}`, key: toastKey })
      } else {
        message.warning({ content: `${scopeLabel}${actionLabel}部分完成：成功 ${result.success} / ${result.total}`, key: toastKey })
      }

      showBatchActionResult(`${scopeLabel}${actionLabel}结果`, result)
      await load()
    } catch (e: any) {
      message.error({ content: `${actionLabel}失败: ${e.message}`, key: toastKey })
    } finally {
      setStatusSyncLoading('')
    }
  }

  const getStatusSyncScope = (): 'selected' | 'all' => (selectedRowKeys.length > 0 ? 'selected' : 'all')

  const getBackfillScope = (): 'selected' | 'pending' => (selectedRowKeys.length > 0 ? 'selected' : 'pending')

  const getPendingBackfillCount = (destination: 'cliproxyapi' | 'sub2api') => {
    if (destination === 'sub2api') {
      return summarizeSub2ApiStates(accounts).pending
    }
    return accounts.filter((item: any) => {
      const sync = item?.cliproxySync || {}
      if (!sync || Object.keys(sync).length === 0) return true
      return String(sync?.remote_state || '').trim().toLowerCase() === 'not_found'
    }).length
  }

  const buildBackfillLabel = (destination: 'cliproxyapi' | 'sub2api') => {
    const scope = getBackfillScope()
    const count = scope === 'selected' ? selectedRowKeys.length : getPendingBackfillCount(destination)
    const destinationLabel = destination === 'sub2api' ? 'Sub2API' : 'CLIProxyAPI'
    return scope === 'selected'
      ? `补传所选到 ${destinationLabel} (${count})`
      : `补传 ${destinationLabel} 待补传 (${count})`
  }

  const isBackfillActionLoading = (destination: 'cliproxyapi' | 'sub2api', scope: 'selected' | 'pending') => backfillLoading === `${destination}_${scope}`

  const buildBackfillMenuLabel = (destination: 'cliproxyapi' | 'sub2api') => {
    const scope = getBackfillScope()
    const loading = isBackfillActionLoading(destination, scope)
    return (
      <Space size={8}>
        {loading ? <SyncOutlined spin /> : <UploadOutlined />}
        <span>{buildBackfillLabel(destination)}</span>
      </Space>
    )
  }

  const isChatgptPlatform = currentPlatform === 'chatgpt'
  const monospaceStyle: React.CSSProperties = {
    fontFamily: 'SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
    fontSize: 12,
  }
  const secondaryTextStyle: React.CSSProperties = {
    fontSize: 12,
    color: token.colorTextSecondary,
  }
  const cellStackStyle: React.CSSProperties = {
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
    minWidth: 0,
  }
  const secretPreviewStyle: React.CSSProperties = {
    ...monospaceStyle,
    filter: 'blur(4px)',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    maxWidth: '100%',
    opacity: 0.9,
  }
  const compactPanelStyle: React.CSSProperties = {
    padding: '8px 10px',
    borderRadius: token.borderRadiusLG,
    border: `1px solid ${token.colorBorder}`,
    background: token.colorFillAlter,
  }
  const remoteOverviewStyle: React.CSSProperties = {
    display: 'flex',
    flexWrap: 'wrap',
    gap: 8,
    alignItems: 'center',
    padding: '8px 10px',
    borderRadius: token.borderRadiusLG,
    border: `1px solid ${token.colorBorder}`,
    background: token.colorBgContainer,
  }

  const sub2apiOverview = summarizeSub2ApiStates(accounts)

  useEffect(() => {
    if (currentPlatform !== 'chatgpt' || accounts.length === 0 || sub2apiOverview.unknown === 0 || sub2apiOverviewSyncing) {
      return
    }

    const syncKey = `${currentPlatform}|${search}|${filterStatus}|${accounts.map((item: any) => item.id).join(',')}`
    if (sub2apiOverviewSyncKeyRef.current === syncKey) {
      return
    }
    sub2apiOverviewSyncKeyRef.current = syncKey
    setSub2apiOverviewSyncing(true)

    const body: Record<string, unknown> = {
      all_filtered: true,
      params: {},
    }
    if (search) body.email = search
    if (filterStatus) body.status = filterStatus

    apiFetch(`/actions/${currentPlatform}/sync_sub2api_status/batch`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
      .then(() => load())
      .catch(() => {})
      .finally(() => setSub2apiOverviewSyncing(false))
  }, [accounts, currentPlatform, filterStatus, load, search, sub2apiOverview.unknown, sub2apiOverviewSyncing])

  const columns: any[] = [
    {
      title: '邮箱',
      dataIndex: 'email',
      key: 'email',
      width: 260,
      render: (text: string, record: any) => {
        const teamInviteOwner = getTeamInviteOwnerLabel(record.teamInviteSource)
        const teamInviteMeta = [
          record.teamInviteSource?.team_name ? `Team: ${record.teamInviteSource.team_name}` : '',
          record.teamInviteSource?.team_id ? `#${record.teamInviteSource.team_id}` : '',
        ].filter(Boolean).join(' · ')

        return (
          <div style={cellStackStyle}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
              <Text
                style={{ ...monospaceStyle, flex: 1, minWidth: 0, whiteSpace: 'nowrap' }}
                ellipsis={{ tooltip: text }}
              >
                {text}
              </Text>
              {record.extra?.chatgpt_workspace_label ? (
                <Tag color={record.extra.chatgpt_workspace_scope === 'business' ? 'processing' : 'default'}>
                  {record.extra.chatgpt_workspace_label}
                </Tag>
              ) : null}
              <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(text)} />
            </div>
            <Text
              type="secondary"
              style={secondaryTextStyle}
              ellipsis={{ tooltip: record.extra?.chatgpt_workspace_display_name || record.user_id || `账号 #${record.id}` }}
            >
              {record.extra?.chatgpt_workspace_display_name
                ? `名称: ${record.extra.chatgpt_workspace_display_name}`
                : (record.user_id ? `UID: ${record.user_id}` : `账号 #${record.id}`)}
            </Text>
            {teamInviteOwner ? (
              <Text
                type="secondary"
                style={secondaryTextStyle}
                ellipsis={{ tooltip: `${teamInviteOwner}${teamInviteMeta ? ` · ${teamInviteMeta}` : ''}` }}
              >
                {`母号: ${teamInviteOwner}${teamInviteMeta ? ` · ${teamInviteMeta}` : ''}`}
              </Text>
            ) : null}
          </div>
        )
      },
    },
    {
      title: '密码',
      dataIndex: 'password',
      key: 'password',
      width: 150,
      render: (text: string) => (
        <Space size={6} style={{ width: '100%', justifyContent: 'space-between' }}>
          <Text style={{ ...secretPreviewStyle, maxWidth: 90 }} title={text}>
            {text}
          </Text>
          <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(text)} />
        </Space>
      ),
    },
    {
      title: 'RT',
      key: 'refresh_token',
      width: 120,
      render: (_: any, record: any) => {
        const rt = getRefreshToken(record)
        if (!rt) return <span style={{ color: '#ccc' }}>-</span>
        return (
          <Space size={6} style={{ width: '100%', justifyContent: 'space-between' }}>
            <Text style={{ ...secretPreviewStyle, fontSize: 11, maxWidth: 58 }} title={rt}>
              {rt}
            </Text>
            <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(rt)} />
          </Space>
        )
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (status: string) => <Tag color={STATUS_COLORS[status] || 'default'}>{status}</Tag>,
    },
  ]

  if (isChatgptPlatform) {
    columns.push(
      {
        title: '本地状态',
        key: 'chatgpt_local_state',
        width: 220,
        render: (_: any, record: any) => {
          const auth = record.chatgptLocal?.auth || {}
          const subscription = record.chatgptLocal?.subscription || {}
          const codex = record.chatgptLocal?.codex || {}
          const authMeta = authStateMeta(auth.state)
          const planTag = planMeta(subscription.plan)
          const codexMeta = codexStateMeta(codex.state)

          return (
            <div style={{ ...cellStackStyle, ...compactPanelStyle }}>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                <Tag color={authMeta.color}>{authMeta.label}</Tag>
                <Tag color={planTag.color}>{planTag.label}</Tag>
                <Tag color={codexMeta.color}>Codex {codexMeta.label}</Tag>
              </div>
            </div>
          )
        },
      },
      {
        title: 'Sub2API',
        key: 'sub2api_sync',
        width: 170,
        render: (_: any, record: any) => {
          const sync = record.sub2apiSync || {}
          const meta = sub2apiStateMeta(sync)

          return (
            <div style={{ ...cellStackStyle, ...compactPanelStyle }}>
              <Tag color={meta.color}>{meta.label}</Tag>
            </div>
          )
        },
      },
    )
  } else {
    columns.push(
      {
        title: '地区',
        dataIndex: 'region',
        key: 'region',
        width: 100,
        render: (text: string) => text || '-',
      },
      {
        title: '试用链接',
        dataIndex: 'cashier_url',
        key: 'cashier_url',
        width: 120,
        render: (url: string) =>
          url ? (
            <Space size={0}>
              <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(url)} />
              <Button type="text" size="small" icon={<LinkOutlined />} onClick={() => window.open(url, '_blank')} />
            </Space>
          ) : (
            '-'
          ),
      },
    )
  }

  columns.push(
    {
      title: '注册时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 132,
      render: (text: string) => {
        const formatted = formatCreatedAt(text)
        return (
          <div style={cellStackStyle}>
            <Text style={{ fontSize: 13 }}>{formatted.date}</Text>
            {formatted.time ? <Text type="secondary" style={secondaryTextStyle}>{formatted.time}</Text> : null}
          </div>
        )
      },
    },
    {
      title: '操作',
      key: 'action',
      width: 220,
      fixed: isChatgptPlatform ? 'right' : undefined,
      render: (_: any, record: any) => (
        <Space size={4} wrap>
          <Button type="link" size="small" onClick={() => { setCurrentAccount(record); setDetailModalOpen(true); }}>
            详情
          </Button>
          {record.teamInviteSource?.removable ? (
            <Popconfirm
              title={`确认${getTeamInviteActionLabel(record.teamInviteSource)}？`}
              description={record.teamInviteSource?.team_name ? `目标 Team: ${record.teamInviteSource.team_name}` : undefined}
              onConfirm={() => handleRemoveFromTeam(record)}
            >
              <Button
                type="link"
                size="small"
                danger
                loading={removingTeamAccountId === record.id}
              >
                {getTeamInviteActionLabel(record.teamInviteSource)}
              </Button>
            </Popconfirm>
          ) : null}
          <Popconfirm title="确认删除？" onConfirm={() => handleDelete(record.id)}>
            <Button type="link" size="small" danger>
              删除
            </Button>
          </Popconfirm>
          <ActionMenu acc={record} onRefresh={load} actions={platformActions} />
        </Space>
      ),
    },
  )

  const statusSyncMenuItems: MenuProps['items'] = [
    {
      key: `probe:${getStatusSyncScope()}`,
      label:
        getStatusSyncScope() === 'selected'
          ? `同步所选本地状态 (${selectedRowKeys.length})`
          : `同步当前筛选本地状态 (${total})`,
      disabled: getStatusSyncScope() === 'selected' ? selectedRowKeys.length === 0 : total === 0,
    },
    {
      key: `remote:${getStatusSyncScope()}`,
      label:
        getStatusSyncScope() === 'selected'
          ? `同步所选 CLIProxyAPI 状态 (${selectedRowKeys.length})`
          : `同步当前筛选 CLIProxyAPI 状态 (${total})`,
      disabled: getStatusSyncScope() === 'selected' ? selectedRowKeys.length === 0 : total === 0,
    },
    {
      key: `sub2api:${getStatusSyncScope()}`,
      label:
        getStatusSyncScope() === 'selected'
          ? `同步所选 Sub2API 状态 (${selectedRowKeys.length})`
          : `同步当前筛选 Sub2API 状态 (${total})`,
      disabled: getStatusSyncScope() === 'selected' ? selectedRowKeys.length === 0 : total === 0,
    },
  ]

  const backfillScope = getBackfillScope()
  const backfillDisabled = backfillScope === 'selected' ? selectedRowKeys.length === 0 : getPendingBackfillCount('sub2api') === 0 && getPendingBackfillCount('cliproxyapi') === 0
  const sub2apiOverviewBackfillScope: 'selected' | 'pending' = selectedRowKeys.length > 0 ? 'selected' : 'pending'
  const sub2apiOverviewPendingCount = getPendingBackfillCount('sub2api')
  const sub2apiOverviewUploading = backfillLoading === `sub2api_${sub2apiOverviewBackfillScope}`
  const sub2apiOverviewUploadDisabled = sub2apiOverviewBackfillScope === 'selected' ? selectedRowKeys.length === 0 : sub2apiOverviewPendingCount === 0
  const backfillMenuItems: MenuProps['items'] = [
    {
      key: `cliproxyapi:${backfillScope}`,
      label: buildBackfillMenuLabel('cliproxyapi'),
      disabled: backfillDisabled,
    },
    {
      key: `sub2api:${backfillScope}`,
      label: buildBackfillMenuLabel('sub2api'),
      disabled: backfillDisabled,
    },
  ]

  return (
    <div>
      <div style={{ marginBottom: 16, display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
        <Space>
          <Input.Search
            placeholder="搜索邮箱..."
            allowClear
            onSearch={setSearch}
            style={{ width: 200 }}
          />
          <Select
            placeholder="状态筛选"
            allowClear
            style={{ width: 120 }}
            onChange={setFilterStatus}
            options={[
              { value: 'registered', label: '已注册' },
              { value: 'trial', label: '试用中' },
              { value: 'subscribed', label: '已订阅' },
              { value: 'expired', label: '已过期' },
              { value: 'invalid', label: '已失效' },
            ]}
          />
          <Text type="secondary">{total} 个账号</Text>
          {selectedRowKeys.length > 0 && (
            <Text type="success">已选 {selectedRowKeys.length} 个</Text>
          )}
        </Space>
        <Space>
          {currentPlatform === 'chatgpt' && (
            <Dropdown
              trigger={['click']}
              menu={{
                items: statusSyncMenuItems,
                onClick: ({ key }) => {
                  const [kind, scope] = String(key).split(':') as ['probe' | 'remote' | 'sub2api', 'selected' | 'all']
                  handleBatchStatusSync(kind, scope)
                },
              }}
            >
              <Button
                icon={<SyncOutlined />}
                loading={statusSyncLoading !== ''}
                disabled={total === 0}
              >
                状态同步
              </Button>
            </Dropdown>
          )}
          {currentPlatform === 'chatgpt' && (
            <Dropdown
              trigger={['click']}
              menu={{
                items: backfillMenuItems,
                onClick: ({ key }) => {
                  const [destination, scope] = String(key).split(':') as ['cliproxyapi' | 'sub2api', 'selected' | 'pending']
                  Modal.confirm({
                    title:
                      scope === 'selected'
                        ? `确认补传所选 ${selectedRowKeys.length} 个账号到 ${destination === 'sub2api' ? 'Sub2API' : 'CLIProxyAPI'}？`
                        : `确认处理当前筛选范围内 ${getPendingBackfillCount(destination)} 个 ${destination === 'sub2api' ? 'Sub2API' : 'CLIProxyAPI'} 待补传账号？`,
                    onOk: () => handleBackfill(destination, scope),
                  })
                },
              }}
            >
              <Button
                loading={backfillLoading !== ''}
                icon={backfillLoading !== '' ? <SyncOutlined spin /> : <UploadOutlined />}
                disabled={backfillDisabled}
              >
                {backfillLoading !== '' ? '远端补传中...' : '远端补传'}
              </Button>
            </Dropdown>
          )}
          {currentPlatform === 'chatgpt' && (
            <Button icon={<LinkOutlined />} onClick={() => setBusinessDeferredModalOpen(true)}>
              Business 补激活
            </Button>
          )}
          <Popconfirm
            title={`确认删除当前平台的全部无效账号？`}
            description="只会删除 status=invalid 的账号，操作不可恢复。"
            onConfirm={handleDeleteInvalid}
          >
            <Button danger icon={<DeleteOutlined />} loading={deleteInvalidLoading} disabled={total === 0}>
              一键删无效
            </Button>
          </Popconfirm>
          {selectedRowKeys.length > 0 && (
            <Popconfirm title={`确认删除选中的 ${selectedRowKeys.length} 个账号？`} onConfirm={handleBatchDelete}>
              <Button danger icon={<DeleteOutlined />}>删除 {selectedRowKeys.length} 个</Button>
            </Popconfirm>
          )}
          <Button icon={<UploadOutlined />} onClick={() => setImportModalOpen(true)}>导入</Button>
          <Button icon={<DownloadOutlined />} onClick={exportCsv} disabled={accounts.length === 0}>导出</Button>
          <Button icon={<PlusOutlined />} onClick={() => setAddModalOpen(true)}>新增</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setRegisterModalOpen(true)}>注册</Button>
          <Button icon={<ReloadOutlined spin={loading} />} onClick={load} />
        </Space>
      </div>

      {currentPlatform === 'chatgpt' && accounts.length > 0 && (
        <div style={{ ...remoteOverviewStyle, marginBottom: 16, justifyContent: 'space-between' }}>
          <Space wrap size={[8, 8]}>
            <Text strong style={{ fontSize: 13 }}>Sub2API 远端概览</Text>
            <Tag color="success">已存在 {sub2apiOverview.exists}</Tag>
            <Tag>未发现 {sub2apiOverview.notFound}</Tag>
            <Tag color="processing">其他工作区已存在 {sub2apiOverview.crossWorkspace}</Tag>
            <Tag color="warning">多候选 {sub2apiOverview.ambiguous}</Tag>
            <Tag color="error">不可达 {sub2apiOverview.unreachable}</Tag>
            <Tag>未同步 {sub2apiOverview.unknown}</Tag>
            <Tag color="processing">待补传 {sub2apiOverview.pending}</Tag>
            {sub2apiOverviewSyncing ? <Tag color="processing">正在自动刷新</Tag> : null}
            <Text type="secondary" style={{ fontSize: 12 }}>基于当前列表 {accounts.length} 个账号</Text>
          </Space>
          <Space wrap size={8}>
            <Button
              size="small"
              icon={<ReloadOutlined spin={statusSyncLoading === 'sub2api_all' || sub2apiOverviewSyncing} />}
              loading={statusSyncLoading === 'sub2api_all'}
              onClick={() => handleBatchStatusSync('sub2api', 'all')}
            >
              刷新
            </Button>
            <Button
              size="small"
              type="primary"
              icon={sub2apiOverviewUploading ? <SyncOutlined spin /> : <UploadOutlined />}
              loading={sub2apiOverviewUploading}
              disabled={sub2apiOverviewUploadDisabled}
              onClick={() => {
                Modal.confirm({
                  title:
                    sub2apiOverviewBackfillScope === 'selected'
                      ? `确认补传所选 ${selectedRowKeys.length} 个账号到 Sub2API？`
                      : `确认补传当前筛选范围内 ${sub2apiOverviewPendingCount} 个 Sub2API 待补传账号？`,
                  onOk: () => handleBackfill('sub2api', sub2apiOverviewBackfillScope),
                })
              }}
            >
              {sub2apiOverviewUploading
                ? (sub2apiOverviewBackfillScope === 'selected' ? `上传所选中... (${selectedRowKeys.length})` : `上传待补传中... (${sub2apiOverviewPendingCount})`)
                : (sub2apiOverviewBackfillScope === 'selected' ? `上传所选 (${selectedRowKeys.length})` : `上传待补传 (${sub2apiOverviewPendingCount})`)}
            </Button>
          </Space>
        </div>
      )}

      <Table
        rowKey="id"
        columns={columns}
        dataSource={accounts}
        loading={loading}
        size="middle"
        rowSelection={{
          selectedRowKeys,
          onChange: setSelectedRowKeys,
        }}
        pagination={{ pageSize: 20, showSizeChanger: false }}
        scroll={{ x: isChatgptPlatform ? 1450 : 980 }}
        onRow={(record) => ({
          onDoubleClick: () => {
            setCurrentAccount(record)
            setDetailModalOpen(true)
          },
        })}
      />

      <Modal
        title="ChatGPT Business 补激活中心"
        open={businessDeferredModalOpen}
        onCancel={() => { setBusinessDeferredModalOpen(false); setSelectedPendingInviteRowKeys([]) }}
        footer={[
          <Button key="refresh" onClick={loadPendingBusinessInvites} loading={pendingBusinessInvitesLoading}>
            刷新
          </Button>,
          <Button key="activate-selected" onClick={handleActivateSelectedPendingInvites} loading={activatingAllPendingInvites} disabled={selectedPendingInviteRowKeys.length === 0}>
            激活所选 {selectedPendingInviteRowKeys.length > 0 ? `(${selectedPendingInviteRowKeys.length})` : ''}
          </Button>,
          <Button key="activate-all" onClick={handleActivateAllPendingInvites} loading={activatingAllPendingInvites}>
            激活可恢复项
          </Button>,
          <Button key="close" type="primary" onClick={() => { setBusinessDeferredModalOpen(false); setSelectedPendingInviteRowKeys([]) }}>
            确定
          </Button>,
        ]}
        width={1180}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Alert
            type="info"
            showIcon
            message="这里承载的是“延迟邀请 / 激活中断恢复 / 手动补激活”流程。"
            description="如果注册阶段已发出邀请，但激活阶段中断、报错或容器重启，这里可以从保存的检查点继续补激活；不会重新注册账号。"
          />

          <Table
            rowKey="id"
            size="small"
            loading={pendingBusinessInvitesLoading}
            pagination={{ pageSize: 8, showSizeChanger: false }}
            rowSelection={{
              selectedRowKeys: selectedPendingInviteRowKeys,
              onChange: setSelectedPendingInviteRowKeys,
              getCheckboxProps: (record: any) => ({ disabled: record.can_activate === false }),
            }}
            dataSource={pendingBusinessInvites}
            columns={[
              {
                title: '邮箱',
                dataIndex: 'email',
                key: 'email',
                width: 220,
                render: (value: string) => <Text copyable={{ text: value }}>{value}</Text>,
              },
              {
                title: 'Team',
                key: 'team',
                width: 120,
                render: (_: any, record: any) => record.team_name || (record.team_id ? `team=${record.team_id}` : '-'),
              },
              {
                title: '状态',
                dataIndex: 'status',
                key: 'status',
                width: 132,
                render: (value: string) => {
                  const meta = pendingInviteStatusMeta(value)
                  return <Tag color={meta.color}>{meta.label}</Tag>
                },
              },
              {
                title: '检查点',
                dataIndex: 'last_checkpoint_label',
                key: 'last_checkpoint_label',
                width: 120,
                render: (value: string) => value || '-',
              },
              {
                title: '尝试',
                dataIndex: 'activation_attempt_count',
                key: 'activation_attempt_count',
                width: 80,
                render: (value: number) => value || 0,
              },
              {
                title: '最近尝试',
                dataIndex: 'last_attempt_at',
                key: 'last_attempt_at',
                width: 168,
                render: (value: string) => formatSyncTime(value) || '-',
              },
              {
                title: '邀请时间',
                dataIndex: 'invited_at',
                key: 'invited_at',
                width: 168,
                render: (value: string) => formatSyncTime(value) || value || '-',
              },
              {
                title: '错误',
                dataIndex: 'last_error',
                key: 'last_error',
                render: (value: string, record: any) => value || record.last_error_code || '-',
              },
              {
                title: '操作',
                key: 'action',
                width: 220,
                render: (_: any, record: any) => (
                  <Space size={4} wrap>
                    <Button
                      type="primary"
                      size="small"
                      disabled={record.can_activate === false}
                      loading={activatingPendingInviteId === record.id}
                      onClick={() => handleActivatePendingInvite(record.id)}
                    >
                      重新激活
                    </Button>
                    <Popconfirm
                      title="确认标记放弃？"
                      description="放弃后这条记录不会再参与自动补激活。"
                      onConfirm={() => handleAbandonPendingInvite(record.id)}
                      disabled={record.status === 'completed' || record.status === 'abandoned'}
                    >
                      <Button
                        size="small"
                        danger
                        disabled={record.status === 'completed' || record.status === 'abandoned'}
                        loading={abandoningPendingInviteId === record.id}
                      >
                        标记放弃
                      </Button>
                    </Popconfirm>
                  </Space>
                ),
              },
            ]}
          />
        </Space>
      </Modal>

      <Modal
        title={`注册 ${currentPlatform}`}
        open={registerModalOpen}
        onCancel={() => { setRegisterModalOpen(false); setTaskId(null); setTaskSnapshot(null); registerForm.resetFields(); }}
        footer={null}
        width={500}
        maskClosable={false}
      >
        {!taskId ? (
          <Form form={registerForm} layout="vertical" onFinish={handleRegister}>
            {currentPlatform === 'chatgpt' ? (
              <Form.Item name="mail_provider_override" label="邮箱服务" initialValue="__global__">
                <Select
                  options={[
                    {
                      value: '__global__',
                      label: `跟随全局默认（当前：${registerMailProvider === 'manual_email_otp' ? '手动邮箱 + 手输验证码' : registerMailProvider || 'luckmail'}）`,
                    },
                    { value: 'manual_email_otp', label: '手动邮箱 + 手输验证码' },
                  ]}
                />
              </Form.Item>
            ) : null}
            {currentPlatform === 'chatgpt' && effectiveRegisterMailProvider === 'manual_email_otp' ? (
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 16 }}
                message="当前注册将使用手动邮箱模式"
                description="请先填写你的邮箱地址；真正需要验证码时，弹窗会切到任务日志面板，再出现验证码输入卡片。"
              />
            ) : null}
            {currentPlatform === 'chatgpt' && effectiveRegisterMailProvider === 'manual_email_otp' ? (
              <Form.Item
                name="email"
                label="手填邮箱地址"
                rules={[{ required: true, message: '请输入邮箱地址' }]}
                extra="会自动记住你上次填写的邮箱。"
              >
                <Input placeholder="name@gmail.com" autoComplete="email" />
              </Form.Item>
            ) : null}
            <Form.Item name="count" label="注册数量" initialValue={1} rules={[{ required: true }]}>
              <Input type="number" min={1} disabled={currentPlatform === 'chatgpt' && effectiveRegisterMailProvider === 'manual_email_otp'} />
            </Form.Item>
            <Form.Item name="concurrency" label="并发数" initialValue={1} rules={[{ required: true }]}>
              <Input type="number" min={1} max={5} disabled={currentPlatform === 'chatgpt' && effectiveRegisterMailProvider === 'manual_email_otp'} />
            </Form.Item>
            <Form.Item name="register_delay_seconds" label="每个注册延迟(秒)" initialValue={0}>
              <InputNumber min={0} precision={1} step={0.5} style={{ width: '100%' }} placeholder="0 = 不延迟" />
            </Form.Item>
            {currentPlatform === 'chatgpt' && (
              <>
                <Form.Item label="ChatGPT Token 方案">
                  <ChatGPTRegistrationModeSwitch
                    mode={chatgptRegistrationMode}
                    onChange={setChatgptRegistrationMode}
                  />
                </Form.Item>
                {chatgptRegistrationMode === CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN ? (
                  <>
                    <Form.Item
                      label="工作空间抓取"
                      extra="free 勾选独立生效；business 依赖 team invite。若两项都勾，会分别获取并按名称区分保存。"
                    >
                      <Space direction="vertical" size={6}>
                        <Form.Item name="chatgpt_capture_free_workspace" valuePropName="checked" initialValue={true} noStyle>
                          <Checkbox>抓取 free 工作空间</Checkbox>
                        </Form.Item>
                      </Space>
                    </Form.Item>
                    <Form.Item
                      name="chatgpt_enable_team_invite"
                      valuePropName="checked"
                      initialValue={false}
                      label="Business Team Invite"
                      extra="关闭时走原始注册/登录链路；开启后才会进入 business recovery / team invite。"
                    >
                      <Checkbox>启用 team invite / business 恢复</Checkbox>
                    </Form.Item>
                    <Form.Item
                      noStyle
                      shouldUpdate={(prev, next) => prev.chatgpt_enable_team_invite !== next.chatgpt_enable_team_invite}
                    >
                      {({ getFieldValue }) =>
                        getFieldValue('chatgpt_enable_team_invite') ? (
                          <>
                            <Form.Item
                              name="chatgpt_team_invite_deferred_activation"
                              valuePropName="checked"
                              initialValue={false}
                              extra="开启后：先完成全部账号注册并发出邀请，再统一进入激活阶段；不会在单账号刚注册完时立刻进入 business/free。窗口里的“Business 延迟邀请”只作为补救/重试入口。"
                            >
                              <Checkbox>延迟邀请（先统一发邀请，再统一激活）</Checkbox>
                            </Form.Item>
                            <Form.Item>
                              <Space direction="vertical" size={6}>
                                <Form.Item name="chatgpt_capture_business_workspace" valuePropName="checked" initialValue={true} noStyle>
                                  <Checkbox>抓取 business 工作空间</Checkbox>
                                </Form.Item>
                              </Space>
                            </Form.Item>
                          </>
                        ) : (
                          <Alert
                            type="info"
                            showIcon
                            message="当前关闭 team invite"
                            description="普通模式下会直接走 free 主链；business 与延迟邀请配置在开启 team invite 后才生效。"
                          />
                        )
                      }
                    </Form.Item>
                  </>
                ) : null}
              </>
            )}
            <Form.Item>
              <Space direction="vertical" style={{ width: '100%' }} size={8}>
                {currentPlatform === 'chatgpt' ? (
                  <Button block onClick={handleSaveRegisterSettings} loading={registerSettingsSaving}>
                    保存设置
                  </Button>
                ) : null}
                <Button type="primary" htmlType="submit" block loading={registerLoading}>
                  开始注册
                </Button>
              </Space>
            </Form.Item>
          </Form>
        ) : (
          <Space direction="vertical" style={{ width: '100%' }} size={12}>
            {taskSnapshot?.pending_verification ? (
              <TaskVerificationPanel
                taskId={taskId}
                verification={taskSnapshot.pending_verification}
              />
            ) : null}
            <TaskLogPanel taskId={taskId} onDone={() => { load(); }} />
          </Space>
        )}
      </Modal>

      <Modal
        title="手动新增账号"
        open={addModalOpen}
        onCancel={() => { setAddModalOpen(false); addForm.resetFields(); }}
        onOk={handleAdd}
        maskClosable={false}
      >
        <Form form={addForm} layout="vertical">
          <Form.Item name="email" label="邮箱" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true }]}>
            <Input.Password />
          </Form.Item>
          <Form.Item name="token" label="Token">
            <Input />
          </Form.Item>
          <Form.Item name="cashier_url" label="试用链接">
            <Input />
          </Form.Item>
          <Form.Item name="status" label="状态" initialValue="registered">
            <Select
              options={[
                { value: 'registered', label: '已注册' },
                { value: 'trial', label: '试用中' },
                { value: 'subscribed', label: '已订阅' },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="批量导入"
        open={importModalOpen}
        onCancel={() => { setImportModalOpen(false); setImportText(''); }}
        onOk={handleImport}
        confirmLoading={importLoading}
        maskClosable={false}
      >
        <p style={{ marginBottom: 8, fontSize: 12, color: '#7a8ba3' }}>
          每行格式: <code style={{ background: 'rgba(255,255,255,0.1)', padding: '2px 4px', borderRadius: 4 }}>email password [cashier_url]</code>
        </p>
        <Input.TextArea
          value={importText}
          onChange={(e) => setImportText(e.target.value)}
          rows={8}
          style={{ fontFamily: 'monospace' }}
        />
      </Modal>

      <Modal
        title="账号详情"
        open={detailModalOpen}
        onCancel={() => setDetailModalOpen(false)}
        onOk={handleDetailSave}
        maskClosable={false}
        width={760}
        styles={{ body: { maxHeight: '72vh', overflowY: 'auto' } }}
      >
        {currentAccount && (
          <>
            <Form form={detailForm} layout="vertical" initialValues={currentAccount}>
              <Form.Item name="status" label="状态">
                <Select
                  options={[
                    { value: 'registered', label: '已注册' },
                    { value: 'trial', label: '试用中' },
                    { value: 'subscribed', label: '已订阅' },
                    { value: 'expired', label: '已过期' },
                    { value: 'invalid', label: '已失效' },
                  ]}
                />
              </Form.Item>
              <Form.Item name="token" label="Access Token">
                <Input.TextArea rows={2} style={{ fontFamily: 'monospace' }} />
              </Form.Item>
            </Form>
            {(() => {
              const rt = getRefreshToken(currentAccount)
              if (!rt) return null
              return (
                <div style={{ marginTop: 8 }}>
                  <div style={{ marginBottom: 4, fontWeight: 500, fontSize: 13 }}>Refresh Token</div>
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'flex-start',
                      gap: 8,
                      background: token.colorFillAlter,
                      border: `1px solid ${token.colorBorder}`,
                      borderRadius: token.borderRadius,
                      padding: '8px 10px',
                    }}
                  >
                    <Text
                      style={{ fontFamily: 'monospace', fontSize: 11, wordBreak: 'break-all', flex: 1, userSelect: 'text' }}
                      copyable={{ text: rt, tooltips: ['复制 RT', '已复制'] }}
                    >
                      {rt}
                    </Text>
                  </div>
                </div>
              )
            })()}
            {currentPlatform === 'chatgpt' && currentAccount.teamInviteSource ? (
              <DetailSection title="Business / Team Invite 来源">
                <SummaryField label="母号邮箱" value={currentAccount.teamInviteSource.team_email} />
                <SummaryField label="母号 Account ID" value={currentAccount.teamInviteSource.team_account_id || currentAccount.teamInviteSource.primary_account_id} />
                <SummaryField label="母号名称" value={currentAccount.teamInviteSource.primary_account_name} />
                <SummaryField label="Team 名称" value={currentAccount.teamInviteSource.team_name} />
                <SummaryField label="Team ID" value={currentAccount.teamInviteSource.team_id ? String(currentAccount.teamInviteSource.team_id) : ''} />
                <SummaryField label="Invite 状态" value={currentAccount.teamInviteSource.invite_status} />
                <SummaryField label="邀请时间" value={currentAccount.teamInviteSource.invited_at ? formatSyncTime(currentAccount.teamInviteSource.invited_at) : ''} />
                <SummaryField label="加入时间" value={currentAccount.teamInviteSource.joined_at ? formatSyncTime(currentAccount.teamInviteSource.joined_at) : ''} />
                <SummaryField label="移除时间" value={currentAccount.teamInviteSource.removed_from_team_at ? formatSyncTime(currentAccount.teamInviteSource.removed_from_team_at) : ''} />
              </DetailSection>
            ) : null}
            {currentPlatform === 'chatgpt' ? (
              <DetailSection title="本地真实状态">
                {currentAccount.chatgptLocal && Object.keys(currentAccount.chatgptLocal).length > 0 ? (
                  <LocalProbeSummary probe={currentAccount.chatgptLocal} />
                ) : (
                  <Text type="secondary">尚未探测。可在操作菜单中点击“探测本地状态”。</Text>
                )}
              </DetailSection>
            ) : null}
            {currentPlatform === 'chatgpt' ? (
              <DetailSection title="CLIProxyAPI 状态">
                {currentAccount.cliproxySync && Object.keys(currentAccount.cliproxySync).length > 0 ? (
                  <CliproxySyncSummary sync={currentAccount.cliproxySync} />
                ) : (
                  <Text type="secondary">尚未同步。可在操作菜单中点击“同步 CLIProxyAPI 状态”。</Text>
                )}
              </DetailSection>
            ) : null}
            {currentPlatform === 'chatgpt' ? (
              <DetailSection title="Sub2API 状态">
                {currentAccount.sub2apiSync && Object.keys(currentAccount.sub2apiSync).length > 0 ? (
                  <Sub2ApiSyncSummary sync={currentAccount.sub2apiSync} />
                ) : (
                  <Text type="secondary">尚未同步。可在“状态同步”里先执行一次 Sub2API 探测，或直接走补传。</Text>
                )}
              </DetailSection>
            ) : null}
          </>
        )}
      </Modal>
    </div>
  )
}
