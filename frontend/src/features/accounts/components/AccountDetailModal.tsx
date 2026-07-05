import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { Alert, Button, Checkbox, Drawer, Form, Input, InputNumber, Modal, Select, Space, Tag, Typography, theme } from 'antd'
import { CopyOutlined } from '@ant-design/icons'

const { Text } = Typography

type AccountSecretField = 'access_token' | 'refresh_token' | 'id_token' | 'session_token' | 'cookies' | 'password'

type AccountSecretResponse = {
  account_id?: number
  fields?: string[]
  secrets?: Record<string, string>
  present?: Record<string, boolean>
  lengths?: Record<string, number>
}

type K12RecaptureValues = {
  workspace_ids?: string
  save_all_spaces?: boolean
  strict_join?: boolean
  proxy?: string
  join_timeout_seconds?: number
  join_retry_count?: number
  post_join_poll_seconds?: string
}

type K12RecaptureResult = {
  ok?: boolean
  summary?: Record<string, any>
  artifacts?: any[]
  saved_accounts?: any[]
  changed_account_ids?: number[]
  logs?: Array<{ level?: string; message?: string }>
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
      <SummaryField label="工作区套餐" value={subscription.workspace_plan_type} />
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
    if (sync.remote_state === 'cross_workspace_only') return { color: 'processing', label: '其他工作区已存在' }
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

function firstScalarText(...values: any[]): string {
  for (const value of values) {
    if (value === undefined || value === null) continue
    if (typeof value === 'object') continue
    const text = String(value || '').trim()
    if (text) return text
  }
  return ''
}

function boolText(value: any): string {
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (value === undefined || value === null || value === '') return ''
  const normalized = String(value).trim().toLowerCase()
  if (['1', 'true', 'yes', 'y', 'on'].includes(normalized)) return 'true'
  if (['0', 'false', 'no', 'n', 'off'].includes(normalized)) return 'false'
  return String(value).trim()
}

function safeWorkspaceSummaryText(value: string): string {
  const text = String(value || '').trim()
  if (!text) return ''
  if (/bearer|cookie|session|access[_-]?token|refresh[_-]?token|id[_-]?token|__secure-|next-auth|authjs/i.test(text)) {
    return '[redacted]'
  }
  return text.length > 160 ? `${text.slice(0, 80)}…${text.slice(-20)}` : text
}

type WorkspaceVariantSummaryItem = {
  key: string
  scope: string
  workspaceId: string
  label: string
  displayName: string
  authLevel: string
  partialAuth: string
  source: string
}

function appendWorkspaceVariantCandidates(target: any[], value: any) {
  if (!value) return
  if (Array.isArray(value)) {
    value.forEach((item) => appendWorkspaceVariantCandidates(target, item))
    return
  }
  if (typeof value !== 'object') return
  target.push(value)
}

function toWorkspaceVariantSummary(raw: any, index: number): WorkspaceVariantSummaryItem | null {
  if (!raw || typeof raw !== 'object') return null
  const nestedExtra = raw.extra && typeof raw.extra === 'object' ? raw.extra : {}
  const source = { ...raw, ...nestedExtra }
  const scope = firstScalarText(source.scope, source.chatgpt_workspace_scope, source.workspace_scope)
  const workspaceId = firstScalarText(source.workspace_id, source.workspaceId, source.id, source.organization_id)
  const label = firstScalarText(source.label, source.chatgpt_workspace_label, source.workspace_label)
  const displayName = firstScalarText(
    source.display_name,
    source.displayName,
    source.chatgpt_workspace_display_name,
    source.workspace_display_name,
  )
  const authLevel = firstScalarText(source.auth_level, source.authLevel, source.level)
  const partialAuth = boolText(source.partial_auth ?? source.partialAuth)
  const sourceText = safeWorkspaceSummaryText(firstScalarText(source.source, source.chatgpt_token_source, source.auth_source))
  if (!scope && !workspaceId && !label && !displayName && !authLevel && !partialAuth && !sourceText) return null
  return {
    key: [
      scope || 'scope',
      workspaceId || 'workspace',
      label || displayName || 'label',
      sourceText || 'source',
      index,
    ].join(':'),
    scope,
    workspaceId,
    label,
    displayName,
    authLevel,
    partialAuth,
    source: sourceText,
  }
}

function collectWorkspaceVariants(
  account: any,
  extra: Record<string, any>,
  workspace: Record<string, any>,
  capabilities: Record<string, any>,
  authSummary: Record<string, any>,
): WorkspaceVariantSummaryItem[] {
  const candidates: any[] = []
  appendWorkspaceVariantCandidates(candidates, extra.chatgpt_workspace_variants)
  appendWorkspaceVariantCandidates(candidates, account?.chatgpt_workspace_variants)
  appendWorkspaceVariantCandidates(candidates, account?.workspace_variants)
  appendWorkspaceVariantCandidates(candidates, capabilities.workspace_variants)
  appendWorkspaceVariantCandidates(candidates, extra.workspace_variants)
  appendWorkspaceVariantCandidates(candidates, extra.workspace_artifact_summaries)
  appendWorkspaceVariantCandidates(candidates, extra.chatgpt_workspace_artifacts)
  appendWorkspaceVariantCandidates(candidates, account?.workspace_artifact_summaries)
  appendWorkspaceVariantCandidates(
    candidates,
    Array.isArray(extra._linked_accounts_to_save)
      ? extra._linked_accounts_to_save.map((item: any) => item?.extra).filter(Boolean)
      : [],
  )
  appendWorkspaceVariantCandidates(candidates, {
    scope: workspace.scope || account?.workspace_scope || extra.chatgpt_workspace_scope,
    label: workspace.label || account?.workspace_label || extra.chatgpt_workspace_label,
    display_name: workspace.display_name || account?.workspace_display_name || extra.chatgpt_workspace_display_name,
    workspace_id: workspace.id || extra.workspace_id || extra.organization_id || capabilities.workspace_id,
    auth_level: account?.auth_level || authSummary.level || capabilities.auth_level || extra.auth_level,
    partial_auth: extra.partial_auth,
    source: authSummary.source || extra.chatgpt_token_source,
  })

  const seen = new Set<string>()
  const variants: WorkspaceVariantSummaryItem[] = []
  candidates.forEach((candidate, index) => {
    const item = toWorkspaceVariantSummary(candidate, index)
    if (!item) return
    const dedupeKey = [
      item.scope,
      item.workspaceId,
      item.label,
      item.displayName,
      item.authLevel,
      item.partialAuth,
      item.source,
    ].join('|')
    if (seen.has(dedupeKey)) return
    seen.add(dedupeKey)
    variants.push(item)
  })
  return variants
}

function WorkspaceVariantsSummary({ variants }: { variants: WorkspaceVariantSummaryItem[] }) {
  const { token } = theme.useToken()
  if (variants.length === 0) {
    return <Text type="secondary">尚未记录 workspace variants。</Text>
  }

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 320px), 1fr))', gap: 10 }}>
      {variants.map((variant, index) => (
        <div
          key={variant.key}
          style={{
            border: `1px solid ${token.colorBorder}`,
            borderRadius: token.borderRadiusLG,
            background: token.colorBgContainer,
            padding: 12,
            minWidth: 0,
          }}
        >
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 10 }}>
            <Tag color={variant.scope === 'k12' ? 'purple' : variant.scope === 'business' ? 'blue' : variant.scope === 'free' ? 'green' : 'default'} style={{ marginInlineEnd: 0 }}>
              {variant.scope || `variant ${index + 1}`}
            </Tag>
            <Tag style={{ marginInlineEnd: 0 }}>{`auth_level: ${variant.authLevel || '-'}`}</Tag>
            <Tag color={variant.partialAuth === 'true' ? 'orange' : 'default'} style={{ marginInlineEnd: 0 }}>
              {`partial_auth: ${variant.partialAuth || '-'}`}
            </Tag>
            <Tag style={{ marginInlineEnd: 0 }}>{`source: ${variant.source || '-'}`}</Tag>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            <SummaryField label="workspace_id" value={variant.workspaceId || '-'} />
            <SummaryField label="label" value={variant.label || '-'} />
            <SummaryField label="display_name" value={variant.displayName || '-'} />
            <SummaryField label="source" value={variant.source || '-'} />
          </div>
        </div>
      ))}
    </div>
  )
}

function defaultK12WorkspaceIds(variants: WorkspaceVariantSummaryItem[]): string {
  const seen = new Set<string>()
  const ids: string[] = []
  variants.forEach((variant) => {
    const scope = String(variant.scope || '').toLowerCase()
    const workspaceId = String(variant.workspaceId || '').trim()
    if (scope !== 'k12' || !workspaceId || seen.has(workspaceId)) return
    seen.add(workspaceId)
    ids.push(workspaceId)
  })
  return ids.join('\n')
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
  importingTeamAccountId: number | null
  onImportAccountToTeam: (record: any) => Promise<void> | void
  formatSyncTime: (value?: string) => string
  getRefreshToken: (record: any) => string
  getAccessToken: (record: any) => string
  onCopyAccessToken: (record: any) => Promise<void> | void
  onCopySecret: (record: any, field: AccountSecretField, label: string) => Promise<void> | void
  onFetchSecret: (accountId: number, fields: AccountSecretField[]) => Promise<AccountSecretResponse>
  onRecaptureK12: (record: any, values: K12RecaptureValues) => Promise<K12RecaptureResult>
  recapturingK12AccountId?: number | null
  isAccessTokenCopied: (record: any) => boolean
  canImportAccountToTeam: (record: any) => boolean
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
  importingTeamAccountId,
  onImportAccountToTeam,
  formatSyncTime,
  getRefreshToken,
  getAccessToken,
  onCopyAccessToken,
  onCopySecret,
  onFetchSecret,
  onRecaptureK12,
  recapturingK12AccountId,
  isAccessTokenCopied,
  canImportAccountToTeam,
  authStateMeta,
  planMeta,
  codexStateMeta,
}: AccountDetailModalProps) {
  const [k12Form] = Form.useForm<K12RecaptureValues>()
  const [k12ModalOpen, setK12ModalOpen] = useState(false)
  const [k12Result, setK12Result] = useState<K12RecaptureResult | null>(null)
  const extra = parseAccountExtra(currentAccount)
  const workspace = currentAccount?.workspace && typeof currentAccount.workspace === 'object' ? currentAccount.workspace : {}
  const capabilities = currentAccount?.chatgptCapabilities && typeof currentAccount.chatgptCapabilities === 'object' ? currentAccount.chatgptCapabilities : {}
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
  const workspaceVariants = collectWorkspaceVariants(currentAccount, extra, workspace, capabilities, authSummary)
  const k12Recapturing = Boolean(currentAccount?.id && Number(recapturingK12AccountId || 0) === Number(currentAccount.id))
  const openK12RecaptureModal = () => {
    k12Form.setFieldsValue({
      workspace_ids: defaultK12WorkspaceIds(workspaceVariants),
      save_all_spaces: true,
      strict_join: false,
      proxy: '',
      join_timeout_seconds: 60,
      join_retry_count: 2,
      post_join_poll_seconds: '3,8,15',
    })
    setK12Result(null)
    setK12ModalOpen(true)
  }
  const submitK12Recapture = async () => {
    if (!currentAccount) return
    const values = await k12Form.validateFields()
    const result = await onRecaptureK12(currentAccount, values)
    setK12Result(result || {})
  }
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
                {currentAccount.auth_level ? <Tag>{`auth_level: ${currentAccount.auth_level}`}</Tag> : null}
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 10 }}>
                <SummaryField label="邮箱" value={currentAccount.email} />
                <SummaryField label="账号 ID" value={currentAccount.id ? String(currentAccount.id) : ''} />
                <SummaryField label="OpenAI 用户" value={currentAccount.user_id || capabilities.account_id || workspace.account_id || ''} />
                <SummaryField label="状态" value={currentAccount.status} />
                <SummaryField label="Workspace" value={workspace.display_name || workspace.label || currentAccount.workspace_display_name || currentAccount.workspace_label || ''} />
                <SummaryField label="Workspace ID" value={workspace.id || extra.workspace_id || extra.organization_id || capabilities.workspace_id || ''} />
                <SummaryField label="Workspace Scope" value={workspace.scope || currentAccount.workspace_scope || extra.chatgpt_workspace_scope || ''} />
                <SummaryField label="创建时间" value={currentAccount.created_at ? formatSyncTime(currentAccount.created_at) : ''} />
                <SummaryField label="更新时间" value={currentAccount.updated_at ? formatSyncTime(currentAccount.updated_at) : ''} />
              </div>
              {canImportAccountToTeam(currentAccount) ? (
                <div>
                  <Button
                    type="primary"
                    loading={importingTeamAccountId === currentAccount.id}
                    onClick={() => onImportAccountToTeam(currentAccount)}
                  >
                    设为 Team 母号
                  </Button>
                </div>
              ) : null}
            </div>
          </DetailSection>

          <DetailSection
            title="所有空间 / Workspace variants"
            extra={(
              <Space size={8} wrap>
                <Text type="secondary" style={{ fontSize: 12 }}>仅展示空间摘要，不展开 token/cookies</Text>
                {currentAccount?.platform === 'chatgpt' ? (
                  <Button size="small" type="primary" loading={k12Recapturing} onClick={openK12RecaptureModal}>
                    重新进入/导出 K12
                  </Button>
                ) : null}
              </Space>
            )}
          >
            <WorkspaceVariantsSummary variants={workspaceVariants} />
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

          {currentAccount.teamInviteSource ? (
            <DetailSection title="Business / Team Invite 来源">
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                <SummaryField label="母号邮箱" value={currentAccount.teamInviteSource.team_email} />
                <SummaryField label="母号 Account ID" value={currentAccount.teamInviteSource.team_account_id || currentAccount.teamInviteSource.primary_account_id} />
                <SummaryField label="母号名称" value={currentAccount.teamInviteSource.primary_account_name} />
                <SummaryField label="Team 名称" value={currentAccount.teamInviteSource.team_name} />
                <SummaryField label="Team ID" value={currentAccount.teamInviteSource.team_id ? String(currentAccount.teamInviteSource.team_id) : ''} />
                <SummaryField label="Invite 状态" value={currentAccount.teamInviteSource.invite_status} />
                <SummaryField label="邀请时间" value={currentAccount.teamInviteSource.invited_at ? formatSyncTime(currentAccount.teamInviteSource.invited_at) : ''} />
                <SummaryField label="加入时间" value={currentAccount.teamInviteSource.joined_at ? formatSyncTime(currentAccount.teamInviteSource.joined_at) : ''} />
                <SummaryField label="移除时间" value={currentAccount.teamInviteSource.removed_from_team_at ? formatSyncTime(currentAccount.teamInviteSource.removed_from_team_at) : ''} />
              </div>
            </DetailSection>
          ) : null}

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
      <Modal
        title="重新进入并导出 K12 / Workspace"
        open={k12ModalOpen}
        onOk={submitK12Recapture}
        onCancel={() => setK12ModalOpen(false)}
        confirmLoading={k12Recapturing}
        okText="开始执行"
        cancelText="关闭"
        width={720}
        destroyOnHidden
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <Alert
            type="warning"
            showIcon
            message="将复用当前账号已保存的 AccessToken + cookies/session_token"
            description="该操作会重新执行 K12 join、拉取 ChatGPT accounts/check 空间列表、为每个可进入空间交换新的 workspace AccessToken，并把结果写回当前账号与对应 workspace variant 账号。不会在弹窗中展示 token/cookies 原文。"
          />
          <Form form={k12Form} layout="vertical">
            <Form.Item
              name="workspace_ids"
              label="目标 K12 workspace_id"
              extra="留空时只导出当前可见空间；填写后会先重新 join 这些 K12 空间。多个 ID 支持换行、逗号或空格分隔。"
            >
              <Input.TextArea rows={4} placeholder={'ws_xxx\nws_yyy'} />
            </Form.Item>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 12 }}>
              <Form.Item name="save_all_spaces" valuePropName="checked">
                <Checkbox>同时导出所有可见空间</Checkbox>
              </Form.Item>
              <Form.Item name="strict_join" valuePropName="checked">
                <Checkbox>严格 join（失败即异常）</Checkbox>
              </Form.Item>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 12 }}>
              <Form.Item name="join_timeout_seconds" label="Join 超时秒数">
                <InputNumber min={5} max={180} precision={0} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="join_retry_count" label="Join 重试次数">
                <InputNumber min={0} max={5} precision={0} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="post_join_poll_seconds" label="Join 后轮询秒">
                <Input placeholder="3,8,15" />
              </Form.Item>
            </div>
            <Form.Item name="proxy" label="代理（可选）" extra="留空使用直连/运行态默认网络；需要指定出口时填 http/socks 代理。">
              <Input placeholder="http://user:pass@host:port" />
            </Form.Item>
          </Form>
          {k12Result ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <Tag color={k12Result.ok ? 'success' : 'warning'}>{k12Result.ok ? '执行完成' : '部分完成/异常'}</Tag>
                <Tag>{`导出空间: ${Number(k12Result.summary?.saved_spaces || k12Result.artifacts?.length || 0)}`}</Tag>
                <Tag>{`写入账号: ${Number(k12Result.saved_accounts?.length || 0)}`}</Tag>
                <Tag>{`变更ID: ${(k12Result.changed_account_ids || []).join(',') || '-'}`}</Tag>
              </div>
              <pre
                style={{
                  margin: 0,
                  padding: '10px 12px',
                  borderRadius: 8,
                  background: '#0000000a',
                  border: '1px solid #d9d9d9',
                  maxHeight: 220,
                  overflow: 'auto',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  fontSize: 12,
                }}
              >
                {JSON.stringify({
                  summary: k12Result.summary || {},
                  artifacts: k12Result.artifacts || [],
                  saved_accounts: k12Result.saved_accounts || [],
                  logs: (k12Result.logs || []).slice(-20),
                }, null, 2)}
              </pre>
            </div>
          ) : null}
        </div>
      </Modal>
    </Drawer>
  )
}
