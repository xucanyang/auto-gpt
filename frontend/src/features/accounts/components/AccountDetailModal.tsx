import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { Alert, Button, Drawer, Form, Input, Select, Space, Tag, Typography, theme } from 'antd'
import { CopyOutlined, LinkOutlined, ReloadOutlined } from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'

const { Text } = Typography

type AccountSecretField = 'access_token' | 'refresh_token' | 'id_token' | 'session_token' | 'cookies' | 'password'

type AccountSecretResponse = {
  account_id?: number
  fields?: string[]
  secrets?: Record<string, string>
  present?: Record<string, boolean>
  lengths?: Record<string, number>
}

type PaymentLinkGeneration = {
  id?: number
  task_id?: string
  remote_batch_id?: string
  remote_job_id?: string
  profile_hash?: string
  link_type?: string
  status?: string
  url?: string
  submitted_at?: string
  started_at?: string
  generated_at?: string
  persisted_at?: string
  error?: string
}

function paymentLinkStatusMeta(value: unknown) {
  const status = String(value || '').trim().toLowerCase()
  if (status === 'succeeded' || status === 'done') return { color: 'success', label: '已生成' }
  if (status === 'queued') return { color: 'default', label: '已提交' }
  if (status === 'running') return { color: 'processing', label: '生成中' }
  if (status === 'interrupted') return { color: 'warning', label: '远端中断' }
  if (status === 'failed' || status === 'error') return { color: 'error', label: '失败' }
  return { color: 'default', label: status || '无记录' }
}

function paymentLinkTypeLabel(value: unknown, format?: unknown) {
  const direct = String(value || '').trim().toUpperCase()
  if (direct) return direct
  const normalizedFormat = String(format || '').trim().toLowerCase()
  if (normalizedFormat === 'paypal_url') return 'PAYPAL'
  if (normalizedFormat === 'long_link') return 'LONG-LINK'
  return '历史链接'
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

  const content = code ? value : value
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

function DetailSection({ title, children, extra }: { title: string; children: ReactNode; extra?: ReactNode }) {
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
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, marginBottom: 10 }}>
        <div style={{ fontWeight: 600, color: token.colorText }}>{title}</div>
        {extra}
      </div>
      {children}
    </div>
  )
}

export function LocalProbeSummary({
  probe,
  authStateMeta,
  planMeta,
  codexStateMeta,
  formatSyncTime,
}: {
  probe: any
  authStateMeta: (state?: string) => { color: string; label: string }
  planMeta: (plan?: string) => { color: string; label: string }
  codexStateMeta: (state?: string) => { color: string; label: string }
  formatSyncTime: (value?: string) => string
}) {
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
      <SummaryField label="订阅到期" value={subscription.subscription_active_until ? formatSyncTime(subscription.subscription_active_until) : ''} />
      <SummaryField label="Codex 信息" value={codex.message} code />
    </div>
  )
}

export function CliproxySyncSummary({ sync, formatSyncTime }: { sync: any; formatSyncTime: (value?: string) => string }) {
  const meta = (() => {
    if (!sync || Object.keys(sync).length === 0) return { color: 'default', label: '未同步' }
    if (sync.remote_state === 'unreachable') return { color: 'error', label: '不可连接' }
    if (sync.remote_state === 'not_found') return { color: 'default', label: '远端未发现' }
    if (!sync.uploaded) return { color: 'default', label: '未发现' }
    if (sync.remote_state === 'usable') return { color: 'success', label: '远端可用' }
    if (sync.remote_state === 'account_deactivated') return { color: 'error', label: '远端已失效' }
    if (sync.remote_state === 'access_token_invalidated') return { color: 'error', label: '远端AT失效' }
    if (sync.remote_state === 'unauthorized') return { color: 'error', label: '远端未授权' }
    if (sync.remote_state === 'payment_required') return { color: 'warning', label: '远端需付费/权限' }
    if (sync.remote_state === 'quota_exhausted') return { color: 'warning', label: '远端额度耗尽' }
    if (sync.status === 'active') return { color: 'processing', label: '远端Active' }
    if (sync.status === 'refreshing') return { color: 'processing', label: '远端刷新中' }
    if (sync.status === 'pending') return { color: 'default', label: '远端待处理' }
    if (sync.status === 'error') return { color: 'error', label: '远端错误' }
    if (sync.status === 'disabled') return { color: 'default', label: '远端禁用' }
    return { color: 'default', label: '未同步' }
  })()

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

function Sub2ApiSyncSummary({ sync, formatSyncTime }: { sync: any; formatSyncTime: (value?: string) => string }) {
  const meta = (() => {
    if (!sync || Object.keys(sync).length === 0) return { color: 'default', label: '未同步' }
    if (sync.remote_state === 'unreachable') return { color: 'error', label: 'DB不可达' }
    if (sync.remote_state === 'ambiguous') return { color: 'warning', label: '多条候选' }
    if (sync.remote_state === 'cross_workspace_only') return { color: 'processing', label: '远端其他记录已存在' }
    if (sync.remote_state === 'deleted_exact_match') return { color: 'warning', label: '已删可重传' }
    if (sync.remote_state === 'not_found') return { color: 'default', label: '远端未发现' }
    if (sync.remote_state === 'exists') return { color: 'success', label: '远端已存在' }
    if (sync.status === 'active') return { color: 'processing', label: '远端Active' }
    if (sync.status === 'error') return { color: 'error', label: '远端错误' }
    return { color: 'default', label: '未同步' }
  })()

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        <Tag color={meta.color}>{meta.label}</Tag>
        {sync?.status ? <Tag>{`status: ${sync.status}`}</Tag> : null}
        {sync?.matched_by ? <Tag>{`matched_by: ${sync.matched_by}`}</Tag> : null}
      </div>
      <SummaryField label="远端账号 ID" value={sync?.remote_account_id ? String(sync.remote_account_id) : ''} />
      <SummaryField label="匹配方式" value={sync?.matched_by} />
      <SummaryField label="探测来源" value={sync?.probe_source ? String(sync.probe_source).toUpperCase() : ''} />
      <SummaryField label="候选数量" value={sync?.candidate_count ? String(sync.candidate_count) : ''} />
      <SummaryField label="最近探测" value={sync?.checked_at ? formatSyncTime(sync.checked_at) : ''} />
      <SummaryField label="最近尝试" value={sync?.last_attempt_at ? formatSyncTime(sync.last_attempt_at) : ''} />
      <SummaryField label="最近上传记录" value={sync?.last_upload ? JSON.stringify(sync.last_upload, null, 2) : ''} code />
      <SummaryField label="状态信息" value={sync?.message || sync?.last_message} code />
      <SummaryField label="候选明细" value={sync?.candidates ? JSON.stringify(sync.candidates, null, 2) : ''} code />
    </div>
  )
}

function parseAccountExtra(record: any): Record<string, any> {
  if (record?.extra && typeof record.extra === 'object') return record.extra
  try {
    const parsed = JSON.parse(record?.extra_json || '{}')
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function firstText(...values: any[]): string {
  for (const value of values) {
    if (value === undefined || value === null) continue
    if (typeof value === 'object') {
      try {
        const text = JSON.stringify(value)
        if (text && text !== '{}' && text !== '[]') return text
      } catch {
        // Ignore malformed object values.
      }
      continue
    }
    const text = String(value || '').trim()
    if (text) return text
  }
  return ''
}

function explicitSecretFlag(...values: any[]): boolean | undefined {
  for (const value of values) {
    if (typeof value === 'boolean') return value
  }
  return undefined
}

function accountHasSecret(
  account: any,
  field: AccountSecretField,
  getAccessToken: (record: any) => string,
  getRefreshToken: (record: any) => string,
) {
  const extra = parseAccountExtra(account)
  const credentials = account?.credentials && typeof account.credentials === 'object' ? account.credentials : {}
  if (field === 'access_token') {
    const flag = explicitSecretFlag(account?.has_access_token, credentials.has_access_token, account?.auth?.has_access_token)
    return flag !== undefined ? flag : Boolean(getAccessToken(account))
  }
  if (field === 'refresh_token') {
    const flag = explicitSecretFlag(account?.has_refresh_token, credentials.has_refresh_token, account?.auth?.has_refresh_token)
    return flag !== undefined ? flag : Boolean(getRefreshToken(account))
  }
  if (field === 'session_token') {
    const flag = explicitSecretFlag(account?.has_session_token, credentials.has_session_token, account?.auth?.has_session_token)
    return flag !== undefined ? flag : Boolean(firstText(account?.session_token, extra.session_token, extra.sessionToken, extra.nextauth_session_token))
  }
  if (field === 'cookies') {
    const flag = explicitSecretFlag(account?.has_cookies, credentials.has_cookies, account?.auth?.has_cookies)
    return flag !== undefined ? flag : Boolean(firstText(extra.cookies, extra.cookie, extra.cookie_jar, extra.cookie_header))
  }
  if (field === 'id_token') {
    const flag = explicitSecretFlag(account?.has_id_token, credentials.has_id_token, account?.auth?.has_id_token)
    return flag !== undefined ? flag : Boolean(firstText(extra.id_token, extra.idToken))
  }
  if (field === 'password') {
    const flag = explicitSecretFlag(account?.has_password, account?.password_present, credentials.has_password, account?.auth?.password_present)
    return flag !== undefined ? flag : Boolean(firstText(account?.password))
  }
  return false
}

function parseIdeaSubmitSummary(account: any, extra: Record<string, any>) {
  const topLevel = account?.idea_submit && typeof account.idea_submit === 'object' ? account.idea_submit : {}
  if (Object.keys(topLevel).length > 0) return topLevel
  const camel = account?.ideaSubmit && typeof account.ideaSubmit === 'object' ? account.ideaSubmit : {}
  if (Object.keys(camel).length > 0) return camel
  return extra.idea_submit && typeof extra.idea_submit === 'object' ? extra.idea_submit : {}
}

function ideaSubmitTag(summary: any) {
  const unavailable = Boolean(summary?.unavailable) || String(summary?.status || '').trim().toLowerCase() === 'unavailable'
  const status = String(summary?.status || '').trim().toLowerCase()
  if (unavailable) return { color: 'error', label: 'Idea 不可用' }
  if (status === 'paid') return { color: 'success', label: 'Idea 已开通' }
  if (status === 'submitted' || status === 'processing') return { color: 'processing', label: 'Idea 处理中' }
  if (status === 'failed') return { color: 'warning', label: 'Idea 失败' }
  if (status === 'timeout') return { color: 'warning', label: 'Idea 待人工复核' }
  return { color: 'default', label: 'Idea 未提交' }
}

function SecretMaterialPanel({
  account,
  getAccessToken,
  getRefreshToken,
  onFetchSecret,
  onCopySecret,
  isAccessTokenCopied,
}: {
  account: any
  getAccessToken: (record: any) => string
  getRefreshToken: (record: any) => string
  onFetchSecret: (accountId: number, fields: AccountSecretField[]) => Promise<AccountSecretResponse>
  onCopySecret: (record: any, field: AccountSecretField, label: string) => Promise<void> | void
  isAccessTokenCopied: (record: any) => boolean
}) {
  const { token } = theme.useToken()
  const [revealed, setRevealed] = useState<Record<string, string>>({})
  const [loadingField, setLoadingField] = useState<string>('')
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const accountId = Number(account?.id || 0)

  useEffect(() => {
    setRevealed({})
    setLoadingField('')
    setFieldErrors({})
  }, [accountId])

  const items = useMemo(() => ([
    { field: 'access_token' as const, label: 'Access Token', shortLabel: 'AT', hint: 'API / 后续同步常用凭证' },
    { field: 'refresh_token' as const, label: 'Refresh Token', shortLabel: 'RT', hint: '刷新认证材料，优先于仅 AT 账号' },
    { field: 'id_token' as const, label: 'ID Token', shortLabel: 'ID Token', hint: '完整注册/刷新流程补获的身份令牌' },
    { field: 'session_token' as const, label: 'Session Token', shortLabel: 'Session Token', hint: 'NextAuth / ChatGPT Web 会话核心字段' },
    { field: 'cookies' as const, label: '完整 Cookies', shortLabel: 'Cookies', hint: '注册阶段保存的完整 Web cookies 或 cookie jar' },
    { field: 'password' as const, label: '登录密码', shortLabel: '密码', hint: '账号密码，仅按需显示或复制' },
  ]), [])

  const revealSecret = async (field: AccountSecretField) => {
    if (!accountId) return
    setLoadingField(field)
    setFieldErrors((prev) => ({ ...prev, [field]: '' }))
    try {
      const data = await onFetchSecret(accountId, [field])
      const value = String(data?.secrets?.[field] || '')
      if (!value) {
        setFieldErrors((prev) => ({ ...prev, [field]: '后端未返回该字段内容' }))
        return
      }
      setRevealed((prev) => ({ ...prev, [field]: value }))
    } catch (e: any) {
      setFieldErrors((prev) => ({ ...prev, [field]: e?.message || '读取失败' }))
    } finally {
      setLoadingField('')
    }
  }

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 10 }}>
      {items.map((item) => {
        const present = accountHasSecret(account, item.field, getAccessToken, getRefreshToken)
        const value = revealed[item.field] || ''
        const isRevealed = Boolean(value)
        const isLoading = loadingField === item.field
        const error = fieldErrors[item.field] || ''
        const copiedAt = item.field === 'access_token' && isAccessTokenCopied(account)
        return (
          <div
            key={item.field}
            style={{
              border: `1px solid ${token.colorBorder}`,
              borderRadius: token.borderRadiusLG,
              background: token.colorBgContainer,
              padding: 12,
              minWidth: 0,
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 8 }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                  <Text strong>{item.label}</Text>
                  <Tag color={present ? 'success' : 'default'} style={{ marginInlineEnd: 0 }}>{present ? '已保存' : '未保存'}</Tag>
                  {copiedAt ? <Tag color="orange" style={{ marginInlineEnd: 0 }}>已复制AT</Tag> : null}
                </div>
                <Text type="secondary" style={{ display: 'block', fontSize: 12, marginTop: 3 }}>
                  {item.hint}{isRevealed ? ` · ${value.length} 字符` : ''}
                </Text>
              </div>
              <Space size={6} wrap>
                <Button
                  size="small"
                  disabled={!present || !accountId}
                  loading={isLoading}
                  onClick={() => {
                    if (isRevealed) {
                      setRevealed((prev) => {
                        const next = { ...prev }
                        delete next[item.field]
                        return next
                      })
                      return
                    }
                    void revealSecret(item.field)
                  }}
                >
                  {isRevealed ? '隐藏' : '显示'}
                </Button>
                <Button
                  size="small"
                  icon={<CopyOutlined />}
                  disabled={!present || !accountId}
                  onClick={() => onCopySecret(account, item.field, item.shortLabel)}
                >
                  复制
                </Button>
              </Space>
            </div>
            {error ? (
              <Text type="danger" style={{ display: 'block', marginTop: 8, fontSize: 12 }}>
                {error}
              </Text>
            ) : null}
            {isRevealed ? (
              <pre
                style={{
                  margin: '10px 0 0',
                  padding: '9px 10px',
                  borderRadius: token.borderRadius,
                  border: `1px solid ${token.colorBorder}`,
                  background: token.colorFillAlter,
                  color: token.colorText,
                  fontFamily: 'SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
                  fontSize: 12,
                  lineHeight: 1.55,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  overflowWrap: 'anywhere',
                  maxHeight: item.field === 'cookies' ? 240 : 160,
                  overflow: 'auto',
                }}
              >
                {value}
              </pre>
            ) : null}
          </div>
        )
      })}
    </div>
  )
}

type AccountDetailModalProps = {
  open: boolean
  onClose: () => void
  onSave: () => Promise<void> | void
  currentAccount: any
  detailForm: any
  token: {
    colorFillAlter: string
    colorBorder: string
    borderRadius: number
  }
  formatSyncTime: (value?: string) => string
  getRefreshToken: (record: any) => string
  getAccessToken: (record: any) => string
  onCopyAccessToken: (record: any) => Promise<void> | void
  onCopySecret: (record: any, field: AccountSecretField, label: string) => Promise<void> | void
  onFetchSecret: (accountId: number, fields: AccountSecretField[]) => Promise<AccountSecretResponse>
  isAccessTokenCopied: (record: any) => boolean
  authStateMeta: (state?: string) => { color: string; label: string }
  planMeta: (plan?: string) => { color: string; label: string }
  codexStateMeta: (state?: string) => { color: string; label: string }
}

export function AccountDetailModal({
  open,
  onClose,
  onSave,
  currentAccount,
  detailForm,
  formatSyncTime,
  getRefreshToken,
  getAccessToken,
  onCopyAccessToken,
  onCopySecret,
  onFetchSecret,
  isAccessTokenCopied,
  authStateMeta,
  planMeta,
  codexStateMeta,
}: AccountDetailModalProps) {
  const { token } = theme.useToken()
  const extra = parseAccountExtra(currentAccount)
  const accountId = Number(currentAccount?.id || 0)
  const [paymentLinkHistory, setPaymentLinkHistory] = useState<PaymentLinkGeneration[]>([])
  const [paymentLinkHistoryLoading, setPaymentLinkHistoryLoading] = useState(false)
  const [paymentLinkHistoryError, setPaymentLinkHistoryError] = useState('')
  const loadPaymentLinkHistory = useCallback(async () => {
    if (!accountId) {
      setPaymentLinkHistory([])
      return
    }
    setPaymentLinkHistoryLoading(true)
    setPaymentLinkHistoryError('')
    try {
      const payload = await apiFetch(`/tasks/chatgpt/payment-links/history?account_id=${encodeURIComponent(String(accountId))}&limit=12`) as {
        items?: PaymentLinkGeneration[]
      }
      setPaymentLinkHistory(Array.isArray(payload?.items) ? payload.items : [])
    } catch (e: any) {
      setPaymentLinkHistory([])
      setPaymentLinkHistoryError(String(e?.message || '读取支付链接历史失败'))
    } finally {
      setPaymentLinkHistoryLoading(false)
    }
  }, [accountId])

  useEffect(() => {
    if (!open) return
    void loadPaymentLinkHistory()
  }, [open, loadPaymentLinkHistory])

  const currentPaymentLink = currentAccount?.chatgptLastPaymentLink && typeof currentAccount.chatgptLastPaymentLink === 'object'
    ? currentAccount.chatgptLastPaymentLink
    : extra.chatgpt_last_payment_link && typeof extra.chatgpt_last_payment_link === 'object'
      ? extra.chatgpt_last_payment_link
      : extra.chatgpt_paypal_url && typeof extra.chatgpt_paypal_url === 'object'
        ? extra.chatgpt_paypal_url
        : {}
  const currentPaymentLinkUrl = String(currentPaymentLink.url || currentPaymentLink.paypal_url || '').trim()
  const currentPaymentLinkStatus = paymentLinkStatusMeta(currentPaymentLink.link_status || (currentPaymentLinkUrl ? 'succeeded' : ''))
  const authSummary = currentAccount?.chatgptLocal?.auth && typeof currentAccount.chatgptLocal.auth === 'object'
    ? currentAccount.chatgptLocal.auth
    : currentAccount?.auth && typeof currentAccount.auth === 'object'
      ? currentAccount.auth
      : {}
  const subscriptionSummary = currentAccount?.chatgptLocal?.subscription && typeof currentAccount.chatgptLocal.subscription === 'object'
    ? currentAccount.chatgptLocal.subscription
    : currentAccount?.subscription && typeof currentAccount.subscription === 'object'
      ? currentAccount.subscription
      : {}
  const codexSummary = currentAccount?.chatgptLocal?.codex && typeof currentAccount.chatgptLocal.codex === 'object'
    ? currentAccount.chatgptLocal.codex
    : currentAccount?.codex && typeof currentAccount.codex === 'object'
      ? currentAccount.codex
      : {}
  const capabilitiesSummary = currentAccount?.chatgptCapabilities && typeof currentAccount.chatgptCapabilities === 'object'
    ? currentAccount.chatgptCapabilities
    : {}
  const workspaceSummary = currentAccount?.workspace && typeof currentAccount.workspace === 'object'
    ? currentAccount.workspace
    : {}
  const openAiUserId = firstText(currentAccount?.user_id, authSummary.user_id, extra.user_id)
  const protocolAccountId = firstText(
    capabilitiesSummary.account_id,
    workspaceSummary.account_id,
    extra.chatgpt_account_id,
    extra.account_id,
    currentAccount?.user_id,
  )
  const protocolWorkspaceId = firstText(
    workspaceSummary.id,
    capabilitiesSummary.workspace_id,
    extra.workspace_id,
    extra.organization_id,
  )
  const ideaSubmitSummary = parseIdeaSubmitSummary(currentAccount, extra)
  const ideaSubmitDisplay = ideaSubmitTag(ideaSubmitSummary)
  const drawerTitle = currentAccount ? (
    <Space size={8} wrap>
      <span>账号详情</span>
      <Text type="secondary" style={{ fontSize: 12 }}>{currentAccount.email || `ID ${currentAccount.id}`}</Text>
    </Space>
  ) : '账号详情'

  return (
    <Drawer
      title={drawerTitle}
      open={open}
      onClose={onClose}
      maskClosable={false}
      width="min(1040px, 100vw)"
      styles={{
        body: { paddingTop: 12, overflowY: 'auto' },
        footer: { padding: '10px 16px' },
      }}
      footer={(
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            凭证内容只在点击“显示/复制”时从 secrets 接口读取，不使用列表缓存。
          </Text>
          <Space>
            <Button onClick={onClose}>关闭</Button>
            <Button type="primary" onClick={onSave}>保存基础信息</Button>
          </Space>
        </div>
      )}
    >
      {currentAccount && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <DetailSection title="账号身份">
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                <Tag color={authStateMeta(authSummary.state).color}>认证: {authStateMeta(authSummary.state).label}</Tag>
                <Tag color={planMeta(subscriptionSummary.plan || currentAccount.subscription_plan).color}>
                  套餐: {planMeta(subscriptionSummary.plan || currentAccount.subscription_plan).label}
                </Tag>
                <Tag color={codexStateMeta(codexSummary.state || currentAccount.codex_state).color}>
                  Codex: {codexStateMeta(codexSummary.state || currentAccount.codex_state).label}
                </Tag>
                <Tag color={ideaSubmitDisplay.color}>
                  {ideaSubmitDisplay.label}
                </Tag>
                {currentAccount.auth_level ? <Tag>{`auth_level: ${currentAccount.auth_level}`}</Tag> : null}
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 10 }}>
                <SummaryField label="邮箱" value={currentAccount.email} />
                <SummaryField label="账号 ID" value={currentAccount.id ? String(currentAccount.id) : ''} />
                <SummaryField label="OpenAI 用户 ID" value={openAiUserId} />
                <SummaryField label="协议账号 ID" value={protocolAccountId} />
                <SummaryField label="当前 OAuth Workspace ID" value={protocolWorkspaceId} />
                <SummaryField label="状态" value={currentAccount.status} />
                <SummaryField label="Idea 标记" value={ideaSubmitDisplay.label} />
                <SummaryField label="Idea 原因" value={String(ideaSubmitSummary.reason || '')} />
                <SummaryField label="Idea 标记时间" value={ideaSubmitSummary.marked_at ? formatSyncTime(String(ideaSubmitSummary.marked_at)) : ''} />
                <SummaryField label="Idea Order" value={String(ideaSubmitSummary.order_id || ideaSubmitSummary.display_id || '')} />
                <SummaryField label="创建时间" value={currentAccount.created_at ? formatSyncTime(currentAccount.created_at) : ''} />
                <SummaryField label="更新时间" value={currentAccount.updated_at ? formatSyncTime(currentAccount.updated_at) : ''} />
              </div>
            </div>
          </DetailSection>

          <DetailSection
            title="支付链接"
            extra={(
              <Button
                type="text"
                size="small"
                title="刷新支付链接历史"
                icon={<ReloadOutlined spin={paymentLinkHistoryLoading} />}
                loading={paymentLinkHistoryLoading}
                onClick={() => void loadPaymentLinkHistory()}
              />
            )}
          >
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {currentPaymentLinkUrl ? (
                <div style={{ display: 'grid', gridTemplateColumns: '104px minmax(0, 1fr)', gap: 12, alignItems: 'start' }}>
                  <Text type="secondary" style={{ fontSize: 12, lineHeight: '20px' }}>当前链接</Text>
                  <div style={{ minWidth: 0 }}>
                    <Space size={6} wrap style={{ marginBottom: 4 }}>
                      <Tag color="blue">{paymentLinkTypeLabel(currentPaymentLink.link_type, currentPaymentLink.payment_link_format)}</Tag>
                      <Tag color={currentPaymentLinkStatus.color}>{currentPaymentLinkStatus.label}</Tag>
                      {currentPaymentLink.generated_at || currentPaymentLink.created_at ? (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          生成于 {formatSyncTime(currentPaymentLink.generated_at || currentPaymentLink.created_at)}
                        </Text>
                      ) : null}
                    </Space>
                    <Space size={6} wrap style={{ width: '100%' }}>
                      <Text copyable={{ text: currentPaymentLinkUrl }} style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
                        {currentPaymentLinkUrl}
                      </Text>
                      <Button
                        title="打开支付链接"
                        type="text"
                        size="small"
                        icon={<LinkOutlined />}
                        href={currentPaymentLinkUrl}
                        target="_blank"
                        rel="noreferrer"
                      />
                    </Space>
                  </div>
                </div>
              ) : (
                <Text type="secondary">尚未生成支付链接。</Text>
              )}

              {paymentLinkHistoryError ? (
                <Alert type="warning" showIcon message="支付链接历史读取失败" description={paymentLinkHistoryError} />
              ) : null}

              {paymentLinkHistory.length > 0 ? (
                <div style={{ borderTop: `1px solid ${token.colorBorder}`, paddingTop: 10, display: 'flex', flexDirection: 'column', gap: 8 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>最近生成记录</Text>
                  {paymentLinkHistory.map((item, index) => {
                    const status = paymentLinkStatusMeta(item.status)
                    const generatedAt = item.generated_at || item.persisted_at || item.submitted_at
                    const url = String(item.url || '').trim()
                    return (
                      <div
                        key={String(item.id || `${item.task_id || 'payment-link'}:${index}`)}
                        style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto', gap: 10, alignItems: 'start' }}
                      >
                        <div style={{ minWidth: 0 }}>
                          <Space size={6} wrap>
                            <Tag color="blue">{paymentLinkTypeLabel(item.link_type, 'long_link')}</Tag>
                            <Tag color={status.color}>{status.label}</Tag>
                            {generatedAt ? <Text type="secondary" style={{ fontSize: 12 }}>{formatSyncTime(generatedAt)}</Text> : null}
                            {item.profile_hash ? <Text code title={item.profile_hash}>{item.profile_hash.slice(0, 12)}</Text> : null}
                          </Space>
                          {url ? (
                            <Text copyable={{ text: url }} style={{ display: 'block', marginTop: 4, overflowWrap: 'anywhere' }}>
                              {url}
                            </Text>
                          ) : item.error ? (
                            <Text type="danger" style={{ display: 'block', marginTop: 4, overflowWrap: 'anywhere' }}>{item.error}</Text>
                          ) : null}
                        </div>
                        {url ? (
                          <Button
                            title="打开支付链接"
                            type="text"
                            size="small"
                            icon={<LinkOutlined />}
                            href={url}
                            target="_blank"
                            rel="noreferrer"
                          />
                        ) : null}
                      </div>
                    )
                  })}
                </div>
              ) : !paymentLinkHistoryLoading ? (
                <Text type="secondary" style={{ fontSize: 12 }}>暂无持久化生成记录。</Text>
              ) : null}
            </div>
          </DetailSection>

          <DetailSection
            title="凭证材料"
            extra={<Text type="secondary" style={{ fontSize: 12 }}>默认隐藏，按字段显示或复制</Text>}
          >
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <Alert
                type="info"
                showIcon
                message="Web session 材料已单独归档"
                description="Session Token、完整 Cookies、ID Token 与 AT/RT 分开查看，避免把 extra_json 原始内容整块摊开。"
              />
              <SecretMaterialPanel
                account={currentAccount}
                getAccessToken={getAccessToken}
                getRefreshToken={getRefreshToken}
                onFetchSecret={onFetchSecret}
                onCopySecret={onCopySecret}
                isAccessTokenCopied={isAccessTokenCopied}
              />
            </div>
          </DetailSection>

          <DetailSection title="基础编辑">
            <Form form={detailForm} layout="vertical" initialValues={currentAccount}>
              <div style={{ display: 'grid', gridTemplateColumns: '220px minmax(0, 1fr)', gap: 12, alignItems: 'start' }}>
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
                <Form.Item
                  name="token"
                  label={(
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <span>Access Token（手动覆盖）</span>
                      {accountHasSecret(currentAccount, 'access_token', getAccessToken, getRefreshToken) ? (
                        <Button
                          title="复制AT"
                          type="link"
                          size="small"
                          icon={<CopyOutlined />}
                          style={{ paddingInline: 0, height: 20 }}
                          onClick={(event) => {
                            event.preventDefault()
                            onCopyAccessToken(currentAccount)
                          }}
                        >
                          复制AT
                        </Button>
                      ) : null}
                      {isAccessTokenCopied(currentAccount) ? <Tag color="orange" style={{ marginInlineEnd: 0 }}>已复制AT</Tag> : null}
                    </div>
                  )}
                  extra="这里只用于维护账号主表 token 字段；完整凭证优先在上方“凭证材料”中按需读取。"
                >
                  <Input.TextArea rows={2} style={{ fontFamily: 'monospace' }} />
                </Form.Item>
              </div>
            </Form>
          </DetailSection>

          <DetailSection title="本地真实状态">
            {currentAccount.chatgptLocal && Object.keys(currentAccount.chatgptLocal).length > 0 ? (
              <LocalProbeSummary
                probe={currentAccount.chatgptLocal}
                authStateMeta={authStateMeta}
                planMeta={planMeta}
                codexStateMeta={codexStateMeta}
                formatSyncTime={formatSyncTime}
              />
            ) : (
              <Text type="secondary">尚未探测。可在操作菜单中点击“探测本地状态”。</Text>
            )}
          </DetailSection>
          <DetailSection title="CLIProxyAPI 状态">
            {currentAccount.cliproxySync && Object.keys(currentAccount.cliproxySync).length > 0 ? (
              <CliproxySyncSummary sync={currentAccount.cliproxySync} formatSyncTime={formatSyncTime} />
            ) : (
              <Text type="secondary">尚未同步。可在操作菜单中点击“同步 CLIProxyAPI 状态”。</Text>
            )}
          </DetailSection>
          <DetailSection title="Sub2API 状态">
            {currentAccount.sub2apiSync && Object.keys(currentAccount.sub2apiSync).length > 0 ? (
              <Sub2ApiSyncSummary sync={currentAccount.sub2apiSync} formatSyncTime={formatSyncTime} />
            ) : (
              <Text type="secondary">尚未同步。可在“状态同步”里先执行一次 Sub2API 探测，或直接走补传。</Text>
            )}
          </DetailSection>
        </div>
      )}
    </Drawer>
  )
}
