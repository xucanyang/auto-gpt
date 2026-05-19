import { lazy, Suspense, useEffect, useState, useCallback, useRef } from 'react'
import type { CSSProperties } from 'react'
import {
  Button,
  Dropdown,
  Input,
  Tag,
  Space,
  Modal,
  Form,
  message,
  Typography,
  Alert,
  Popconfirm,
  theme,
  Steps,
} from 'antd'
import type { MenuProps } from 'antd'
import {
  CopyOutlined,
  LinkOutlined,
  MoreOutlined,
  UploadOutlined,
  SyncOutlined,
} from '@ant-design/icons'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import { AddAccountModal } from '@/features/accounts/components/AddAccountModal'
import { AccountsTable } from '@/features/accounts/components/AccountsTable'
import { AccountDetailModal } from '@/features/accounts/components/AccountDetailModal'
import { AccountsToolbar } from '@/features/accounts/components/AccountsToolbar'
import { BatchGopayWorkbench } from '@/features/accounts/components/BatchGopayWorkbench'
import { ImportAccountsModal } from '@/features/accounts/components/ImportAccountsModal'
import { PendingInvitesModal } from '@/features/accounts/components/PendingInvitesModal'
import { Sub2ApiOverviewPanel } from '@/features/accounts/components/Sub2ApiOverviewPanel'
import { useAccountDetailQuery } from '@/features/accounts/hooks/useAccountDetailQuery'
import { useActiveTasksQuery } from '@/features/accounts/hooks/useActiveTasksQuery'
import { RegisterTaskModal } from '@/features/auth/components/RegisterTaskModal'
import { useAccountsQuery } from '@/features/accounts/hooks/useAccountsQuery'
import { usePendingInvitesQuery } from '@/features/accounts/hooks/usePendingInvitesQuery'
import { usePersistentChatGPTRegistrationMode } from '@/hooks/usePersistentChatGPTRegistrationMode'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import { buildChatGPTRegistrationRequestAdapter } from '@/lib/chatgptRegistrationRequestAdapter'
import { CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN } from '@/lib/chatgptRegistrationMode'
import { apiFetch } from '@/lib/utils'
import { normalizeExecutorForPlatform } from '@/lib/platformExecutorOptions'

const { Text } = Typography

const AccountActionSurface = lazy(() =>
  import('@/features/accounts/components/AccountActionSurface').then((module) => ({
    default: module.AccountActionSurface,
  })),
)

const GOPAY_ACTIVE_PHASES = new Set(['created', 'starting', 'waiting_otp', 'waiting_link_pin', 'waiting_payment_pin', 'verifying'])
const TASK_MODAL_STORAGE_KEY = 'auto-chatgpt.accounts.task-modal.current-task'

const GOPAY_PHASE_META: Record<string, { title: string; description: string; step: number; status?: 'wait' | 'process' | 'finish' | 'error' }> = {
  created: { title: '已创建', description: '准备开始 GoPay 支付', step: 0, status: 'process' },
  starting: { title: '初始化', description: '正在准备 ChatGPT/Stripe/Midtrans 支付会话', step: 0, status: 'process' },
  waiting_otp: { title: 'OTP', description: '输入 GoPay 短信验证码', step: 1, status: 'process' },
  waiting_link_pin: { title: '绑定 PIN', description: '输入 GoPay PIN 以绑定支付方式', step: 2, status: 'process' },
  waiting_payment_pin: { title: '支付 PIN', description: '再次输入 GoPay PIN 完成扣款', step: 3, status: 'process' },
  verifying: { title: '验证', description: '正在等待 ChatGPT 确认订阅', step: 4, status: 'process' },
  succeeded: { title: '完成', description: '订阅支付已完成', step: 4, status: 'finish' },
  failed: { title: '失败', description: '支付流程失败', step: 4, status: 'error' },
  cancelled: { title: '已取消', description: '支付流程已取消', step: 4, status: 'error' },
}

function gopayPhaseMeta(phase?: string) {
  return GOPAY_PHASE_META[String(phase || '').trim()] || { title: '未知', description: String(phase || '未知阶段'), step: 0, status: 'process' as const }
}

const REGISTER_FORM_SETTINGS_STORAGE_PREFIX = 'auto-chatgpt.register-form-settings.'
const DEFAULT_CHECKOUT_COUNTRY = 'ID'
const DEFAULT_CHECKOUT_CURRENCY = 'IDR'
const DEFAULT_GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS = 120
const ACCOUNTS_PAGE_SIZE = 10
const EMPTY_LIST: any[] = []

function normalizeCheckoutCountry(value: unknown) {
  return String(value || DEFAULT_CHECKOUT_COUNTRY).trim().toUpperCase() || DEFAULT_CHECKOUT_COUNTRY
}

function normalizeCheckoutCurrency(value: unknown) {
  return String(value || DEFAULT_CHECKOUT_CURRENCY).trim().toUpperCase() || DEFAULT_CHECKOUT_CURRENCY
}

function normalizeGopayOtpAutoResendDelay(value: unknown) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return DEFAULT_GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS
  return Math.max(0, Math.min(Math.trunc(parsed), 3600))
}

function parseMaybeJsonObject(value: unknown) {
  if (value && typeof value === 'object') return value as Record<string, any>
  const text = String(value || '').trim()
  if (!text) return {}
  try {
    const parsed = JSON.parse(text)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

type GopayPhoneCandidate = {
  id: string
  label: string
  phone_country_code: string
  phone_number: string
  enabled?: boolean
  last_used_at?: string
}

type BatchGopayItem = {
  account: any
  phone: GopayPhoneCandidate
  batchIndex: number
  round: number
  status: 'queued' | 'starting' | 'running' | 'done' | 'failed' | 'cancelled'
  snapshot?: any
  error?: string
  logsOpen?: boolean
  configOpen?: boolean
  submitting?: boolean
}

function parseMaybeJsonArray(value: unknown) {
  if (Array.isArray(value)) return value
  const text = String(value || '').trim()
  if (!text) return []
  try {
    const parsed = JSON.parse(text)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

function normalizeGopayPhonePart(value: unknown) {
  return String(value || '').replace(/\D/g, '')
}

function getGopayPhoneKey(phone: Pick<GopayPhoneCandidate, 'phone_country_code' | 'phone_number'>) {
  return `${normalizeGopayPhonePart(phone.phone_country_code)}:${normalizeGopayPhonePart(phone.phone_number)}`
}

function formatGopayPhoneLabel(phone: Partial<GopayPhoneCandidate>) {
  const label = String(phone.label || '').trim()
  const countryCode = normalizeGopayPhonePart(phone.phone_country_code)
  const number = normalizeGopayPhonePart(phone.phone_number)
  const value = countryCode && number ? `+${countryCode} ${number}` : number
  return label ? `${label} · ${value}` : value
}

function normalizeGopayPhoneCandidate(value: any, index = 0): GopayPhoneCandidate | null {
  const phone_country_code = normalizeGopayPhonePart(value?.phone_country_code || value?.country_code || value?.code || '86')
  const phone_number = normalizeGopayPhonePart(value?.phone_number || value?.number || value?.phone || '')
  if (!phone_country_code || !phone_number) return null
  const key = `${phone_country_code}:${phone_number}`
  return {
    id: String(value?.id || `phone_${key}_${index}`).replace(/[^A-Za-z0-9:_-]/g, '_'),
    label: String(value?.label || value?.name || `GoPay ${index + 1}`).trim(),
    phone_country_code,
    phone_number,
    enabled: value?.enabled !== false,
    last_used_at: String(value?.last_used_at || '').trim(),
  }
}

function normalizeGopayPhoneCandidates(value: unknown): GopayPhoneCandidate[] {
  const items = parseMaybeJsonArray(value)
  const seen = new Set<string>()
  const normalized: GopayPhoneCandidate[] = []
  items.forEach((item, index) => {
    const candidate = normalizeGopayPhoneCandidate(item, index)
    if (!candidate) return
    const key = getGopayPhoneKey(candidate)
    if (seen.has(key)) return
    seen.add(key)
    normalized.push(candidate)
  })
  return normalized
}

function upsertGopayPhoneCandidate(candidates: GopayPhoneCandidate[], phone: Partial<GopayPhoneCandidate>) {
  const candidate = normalizeGopayPhoneCandidate(
    {
      ...phone,
      last_used_at: new Date().toISOString(),
    },
    candidates.length,
  )
  if (!candidate) return candidates
  const key = getGopayPhoneKey(candidate)
  const rest = candidates.filter((item) => getGopayPhoneKey(item) !== key)
  return [candidate, ...rest]
}

function removeGopayPhoneCandidate(candidates: GopayPhoneCandidate[], phoneId: string) {
  return candidates.filter((item) => item.id !== phoneId)
}

function moveGopayPhoneCandidate(
  candidates: GopayPhoneCandidate[],
  phoneId: string,
  direction: 'up' | 'down' | 'top' | 'bottom',
) {
  const index = candidates.findIndex((item) => item.id === phoneId)
  if (index < 0) return candidates
  const next = [...candidates]
  const [item] = next.splice(index, 1)
  if (direction === 'top') {
    next.unshift(item)
  } else if (direction === 'bottom') {
    next.push(item)
  } else if (direction === 'up') {
    next.splice(Math.max(0, index - 1), 0, item)
  } else {
    next.splice(Math.min(next.length, index + 1), 0, item)
  }
  return next
}

function compareGopayBatchAccounts(a: any, b: any) {
  const emailA = String(a?.email || '').trim().toLowerCase()
  const emailB = String(b?.email || '').trim().toLowerCase()
  if (emailA !== emailB) return emailA.localeCompare(emailB)
  return Number(a?.id || 0) - Number(b?.id || 0)
}

function buildBatchGopayItems(accounts: any[], phones: GopayPhoneCandidate[]) {
  const sortedAccounts = [...accounts].sort(compareGopayBatchAccounts)
  return sortedAccounts.map((account, index) => ({
    account,
    phone: phones[index % phones.length],
    batchIndex: index + 1,
    round: Math.floor(index / phones.length) + 1,
    status: 'queued' as const,
    logsOpen: false,
    configOpen: false,
  }))
}

function reassignBatchGopayPhones(items: BatchGopayItem[], phones: GopayPhoneCandidate[]) {
  if (phones.length === 0) return items
  const sortedItems = [...items].sort((a, b) => compareGopayBatchAccounts(a.account, b.account))
  return sortedItems.map((item, index) => ({
    ...item,
    phone: phones[index % phones.length],
    batchIndex: index + 1,
    round: Math.floor(index / phones.length) + 1,
  }))
}

function normalizeTaskStatus(value: unknown) {
  const normalized = String(value || '').trim().toLowerCase()
  if (normalized === 'success') return 'done'
  if (normalized === 'skipped') return 'stopped'
  if (normalized === 'pending' || normalized === 'running' || normalized === 'done' || normalized === 'failed' || normalized === 'stopped') {
    return normalized
  }
  return 'pending'
}

function isActiveTaskStatus(value: unknown) {
  return !['done', 'failed', 'stopped'].includes(normalizeTaskStatus(value))
}

function clearTaskModalStorage() {
  if (typeof window === 'undefined') return
  window.localStorage.removeItem(TASK_MODAL_STORAGE_KEY)
}

const STATUS_COLORS: Record<string, string> = {
  registered: 'default',
  pending_payment: 'warning',
  payment_failed: 'error',
  trial: 'success',
  subscribed: 'success',
  expired: 'warning',
  invalid: 'error',
}

const STATUS_LABELS: Record<string, string> = {
  registered: '已注册',
  pending_payment: '待支付',
  payment_failed: '支付失败',
  trial: '试用中',
  subscribed: '已订阅',
  expired: '已过期',
  invalid: '已失效',
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
    case 'subscription_pending_auth':
      return { color: 'blue', label: '订阅待补抓' }
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

function pendingActivationKindMeta(kind?: string) {
  switch (String(kind || '').trim()) {
    case 'subscription_auth':
      return { color: 'purple', label: '订阅补抓' }
    default:
      return { color: 'cyan', label: '邀请激活' }
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
  const parsedExtra = parseExtraJson(account.extra_json)
  const extra = account.extra && typeof account.extra === 'object'
    ? { ...parsedExtra, ...account.extra }
    : parsedExtra
  const syncStatuses = extra.sync_statuses && typeof extra.sync_statuses === 'object' ? extra.sync_statuses : {}
  const cliproxySync = syncStatuses.cliproxyapi && typeof syncStatuses.cliproxyapi === 'object' ? syncStatuses.cliproxyapi : {}
  const sub2apiSync = syncStatuses.sub2api && typeof syncStatuses.sub2api === 'object' ? syncStatuses.sub2api : {}
  const chatgptLocal = account.chatgptLocal && typeof account.chatgptLocal === 'object'
    ? account.chatgptLocal
    : extra.chatgpt_local && typeof extra.chatgpt_local === 'object'
      ? extra.chatgpt_local
      : {}
  const chatgptCapabilities = account.chatgptCapabilities && typeof account.chatgptCapabilities === 'object'
    ? account.chatgptCapabilities
    : extra.chatgpt_capabilities && typeof extra.chatgpt_capabilities === 'object'
      ? extra.chatgpt_capabilities
      : {}
  const chatgptPendingSubscriptionAuth = extra.chatgpt_pending_subscription_auth && typeof extra.chatgpt_pending_subscription_auth === 'object'
    ? extra.chatgpt_pending_subscription_auth
    : {}
  const chatgptGopay = extra.chatgpt_gopay && typeof extra.chatgpt_gopay === 'object' ? extra.chatgpt_gopay : null
  const chatgptGopayDefaults = extra.chatgpt_gopay_defaults && typeof extra.chatgpt_gopay_defaults === 'object' ? extra.chatgpt_gopay_defaults : {}
  const chatgptLastPaymentLink = extra.chatgpt_last_payment_link && typeof extra.chatgpt_last_payment_link === 'object'
    ? extra.chatgpt_last_payment_link
    : {}
  const chatgptPaymentLinkDefaults = extra.chatgpt_payment_link_defaults && typeof extra.chatgpt_payment_link_defaults === 'object'
    ? extra.chatgpt_payment_link_defaults
    : {}
  const teamInviteSource = account.team_invite_source && typeof account.team_invite_source === 'object'
    ? account.team_invite_source
    : null
  return {
    ...account,
    extra,
    cliproxySync,
    sub2apiSync,
    chatgptLocal,
    chatgptCapabilities,
    chatgptPendingSubscriptionAuth,
    chatgptGopay,
    chatgptGopayDefaults,
    chatgptLastPaymentLink,
    chatgptPaymentLinkDefaults,
    teamInviteSource,
    manuallyUsed: account.manually_used !== undefined ? Boolean(account.manually_used) : Boolean(extra.manually_used),
  }
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
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  const hour = String(date.getHours()).padStart(2, '0')
  const minute = String(date.getMinutes()).padStart(2, '0')
  return {
    date: `${month}-${day}`,
    time: `${hour}:${minute}`,
  }
}

function copyTextToClipboardFallback(text: string) {
  if (typeof document === 'undefined' || !document.body) return false
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', 'true')
  textarea.style.position = 'fixed'
  textarea.style.top = '0'
  textarea.style.left = '-9999px'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.focus()
  textarea.select()
  textarea.setSelectionRange(0, textarea.value.length)
  let ok = false
  try {
    ok = document.execCommand('copy')
  } catch {
    ok = false
  } finally {
    document.body.removeChild(textarea)
  }
  return ok
}

async function copyTextToClipboard(text: string) {
  const value = String(text || '')
  if (!value) return false
  if (typeof window !== 'undefined' && typeof navigator !== 'undefined' && navigator.clipboard?.writeText && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(value)
      return true
    } catch {
      // Fall through to the legacy clipboard path.
    }
  }
  return copyTextToClipboardFallback(value)
}

async function copyText(text: string) {
  const ok = await copyTextToClipboard(text)
  if (ok) {
    message.success('已复制')
    return true
  }
  message.error('复制失败')
  return false
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

function authStateMeta(state?: string) {
  switch (state) {
    case 'refresh_token_valid':
      return { color: 'success', label: 'RT有效' }
    case 'access_token_valid':
      return { color: 'blue', label: '仅AT有效' }
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

function authLevelMeta(level?: string, fallbackState?: string) {
  switch (String(level || '').toLowerCase()) {
    case 'refresh_token':
      return { color: 'success', label: 'RT有效' }
    case 'access_token_only':
      return { color: 'blue', label: '仅AT有效' }
    case 'invalid':
      return { color: 'error', label: '认证失效' }
    default:
      return authStateMeta(fallbackState)
  }
}

function workspaceMeta(capabilities: any) {
  if (capabilities?.has_workspace && capabilities?.has_account_id) return { color: 'success', label: '材料完整' }
  if (capabilities?.has_workspace) return { color: 'warning', label: '缺account_id' }
  if (capabilities?.has_account_id) return { color: 'warning', label: '缺workspace' }
  return { color: 'default', label: '缺材料' }
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

function statusLabel(status?: string) {
  const normalized = String(status || '').trim()
  return STATUS_LABELS[normalized] || normalized || '未知'
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
  if (sync.remote_state === 'deleted_exact_match') {
    return { color: 'warning', label: '已删可重传' }
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

function summarizeSub2ApiStates(items: any[]) {
  const summary = { exists: 0, notFound: 0, crossWorkspace: 0, deletedExact: 0, ambiguous: 0, unreachable: 0, unknown: 0, pending: 0 }
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
    } else if (remoteState === 'deleted_exact_match') {
      summary.deletedExact += 1
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

function shouldShowResumeAuthButton(record: any) {
  const capabilities = record?.chatgptCapabilities || {}
  const authLevel = String(record?.auth_level || capabilities.auth_level || '').trim().toLowerCase()
  const uploadGate = String(capabilities.upload_gate || '').trim().toLowerCase()
  const status = String(record?.status || '').trim().toLowerCase()
  const pendingStatus = String(record?.chatgptPendingSubscriptionAuth?.status || '').trim().toLowerCase()
  if (status === 'pending_payment') return true
  if (pendingStatus && pendingStatus !== 'completed' && pendingStatus !== 'abandoned') return true
  if (authLevel === 'access_token_only' || authLevel === 'invalid') return true
  if (uploadGate === 'blocked_missing_rt' || uploadGate === 'blocked_missing_workspace') return true
  return false
}

function taskModalModeFromSource(source: unknown): 'register' | 'resume_auth' | 'payment_link' {
  const normalized = String(source || '').trim().toLowerCase()
  if (normalized === 'resume_auth' || normalized === 'resume_subscription_auth' || normalized === 'batch_resume_subscription_auth') return 'resume_auth'
  if (normalized === 'payment_link' || normalized === 'batch_payment_link') return 'payment_link'
  return 'register'
}

const accountActionTextStyles: Record<string, CSSProperties> = {
  payment: { color: '#1677ff' },
  refresh: { color: '#389e0d' },
  resume: { color: '#d48806' },
  more: { color: '#722ed1' },
}

export default function Accounts() {
  const { token } = theme.useToken()
  const currentPlatform = 'chatgpt'
  const [accounts, setAccounts] = useState<any[]>([])
  const [platformActions, setPlatformActions] = useState<any[]>([])
  const [platformActionsLoading, setPlatformActionsLoading] = useState(false)
  const [total, setTotal] = useState(0)
  const [currentPage, setCurrentPage] = useState(1)
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [pageVisible, setPageVisible] = useState(
    typeof document === 'undefined' ? true : document.visibilityState === 'visible',
  )
  const [filterStatus, setFilterStatus] = useState('')
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([])

  const [registerModalOpen, setRegisterModalOpen] = useState(false)
  const [taskModalMode, setTaskModalMode] = useState<'register' | 'resume_auth' | 'payment_link'>('register')
  const [taskModalAccount, setTaskModalAccount] = useState<any>(null)
  const [addModalOpen, setAddModalOpen] = useState(false)
  const [importModalOpen, setImportModalOpen] = useState(false)
  const [detailModalOpen, setDetailModalOpen] = useState(false)
  const [actionSurfaceOpen, setActionSurfaceOpen] = useState(false)
  const [businessDeferredModalOpen, setBusinessDeferredModalOpen] = useState(false)
  const [detailAccount, setDetailAccount] = useState<any>(null)
  const [actionAccount, setActionAccount] = useState<any>(null)
  const [actionSurfaceInitialActionId, setActionSurfaceInitialActionId] = useState<string | null>(null)
  const [actionSurfaceInitialActionMode, setActionSurfaceInitialActionMode] = useState<'direct' | 'dialog'>('dialog')
  const [importingTeamAccountId, setImportingTeamAccountId] = useState<number | null>(null)
  const [resumeAuthAccountId, setResumeAuthAccountId] = useState<number | null>(null)

  const [registerForm] = Form.useForm()
  const [addForm] = Form.useForm()
  const [detailForm] = Form.useForm()
  const [registerMailProvider, setRegisterMailProvider] = useState('luckmail')
  const [configCache, setConfigCache] = useState<Record<string, any> | null>(null)
  const { mode: chatgptRegistrationMode, setMode: setChatgptRegistrationMode } =
    usePersistentChatGPTRegistrationMode()
  const [importText, setImportText] = useState('')
  const [importLoading, setImportLoading] = useState(false)
  const [taskId, setTaskId] = useState<string | null>(null)
  const [taskSnapshot, setTaskSnapshot] = useState<any>(null)
  const [activeTasksPanelOpen, setActiveTasksPanelOpen] = useState(false)
  const [registerLoading, setRegisterLoading] = useState(false)
  const [registerSettingsSaving, setRegisterSettingsSaving] = useState(false)
  const [selectedPendingInviteRowKeys, setSelectedPendingInviteRowKeys] = useState<React.Key[]>([])
  const [activatingPendingInviteId, setActivatingPendingInviteId] = useState<number | null>(null)
  const [abandoningPendingInviteId, setAbandoningPendingInviteId] = useState<number | null>(null)
  const [activatingAllPendingInvites, setActivatingAllPendingInvites] = useState(false)
  const [backfillLoading, setBackfillLoading] = useState<'' | 'cliproxyapi_pending' | 'cliproxyapi_selected' | 'sub2api_pending' | 'sub2api_selected'>('')
  const [batchResumeAuthLoading, setBatchResumeAuthLoading] = useState<'' | 'selected' | 'filtered'>('')
  const [batchPaymentLinkLoading, setBatchPaymentLinkLoading] = useState(false)
  const [statusSyncLoading, setStatusSyncLoading] = useState<
    'probe_selected' | 'probe_all' | 'remote_selected' | 'remote_all' | 'sub2api_selected' | 'sub2api_all' | ''
  >('')
  const [deleteInvalidLoading, setDeleteInvalidLoading] = useState(false)
  const [batchGopayOpen, setBatchGopayOpen] = useState(false)
  const [batchGopayItems, setBatchGopayItems] = useState<BatchGopayItem[]>([])
  const [batchGopayPhones, setBatchGopayPhones] = useState<GopayPhoneCandidate[]>([])
  const [batchGopayDefaults, setBatchGopayDefaults] = useState<Record<string, any>>({})
  const [batchGopayLoading, setBatchGopayLoading] = useState(false)
  const [batchGopayPhoneCountryCode, setBatchGopayPhoneCountryCode] = useState('86')
  const [batchGopayPhoneNumber, setBatchGopayPhoneNumber] = useState('')
  const [batchGopayPhoneSaving, setBatchGopayPhoneSaving] = useState(false)
  const [batchGopayStarted, setBatchGopayStarted] = useState(false)
  const [batchGopayRoundInterval, setBatchGopayRoundInterval] = useState(60)
  const [batchGopayOtpAutoResendDelay, setBatchGopayOtpAutoResendDelay] = useState(DEFAULT_GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS)
  const [batchGopayOtpDelaySaving, setBatchGopayOtpDelaySaving] = useState(false)
  const [batchGopayNextRoundAt, setBatchGopayNextRoundAt] = useState<number | null>(null)
  const batchGopayStartingRef = useRef(false)
  const batchGopayCancelRequestedRef = useRef(false)
  const accountsQuery = useAccountsQuery({
    email: debouncedSearch,
    status: filterStatus,
    page: currentPage,
    pageSize: ACCOUNTS_PAGE_SIZE,
  })
  const accountDetailQuery = useAccountDetailQuery(detailAccount?.id ? Number(detailAccount.id) : null, detailModalOpen)
  const activeTasksQuery = useActiveTasksQuery(activeTasksPanelOpen)
  const pendingInvitesQuery = usePendingInvitesQuery(businessDeferredModalOpen && currentPlatform === 'chatgpt')
  const activeTasks = activeTasksQuery.data ?? EMPTY_LIST
  const activeTasksLoading = activeTasksQuery.isLoading || activeTasksQuery.isFetching
  const pendingBusinessInvites = pendingInvitesQuery.data ?? EMPTY_LIST
  const pendingBusinessInvitesLoading = pendingInvitesQuery.isLoading || pendingInvitesQuery.isFetching
  const loading = accountsQuery.isLoading || accountsQuery.isFetching

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedSearch(search.trim())
    }, 300)
    return () => window.clearTimeout(timer)
  }, [search])

  useEffect(() => {
    setCurrentPage(1)
  }, [debouncedSearch, filterStatus])

  useEffect(() => {
    if (typeof document === 'undefined') return
    const updateVisibility = () => {
      setPageVisible(document.visibilityState === 'visible')
    }
    updateVisibility()
    document.addEventListener('visibilitychange', updateVisibility)
    return () => {
      document.removeEventListener('visibilitychange', updateVisibility)
    }
  }, [])

  useEffect(() => {
    if (!detailModalOpen || !detailAccount) return
    detailForm.setFieldsValue({
      status: detailAccount.status,
      token: detailAccount.token,
    })
  }, [detailModalOpen, detailAccount, detailForm])

  const openAccountPaymentLinkAction = useCallback((record: any) => {
    setActionAccount(record)
    setActionSurfaceInitialActionId('payment_link')
    setActionSurfaceInitialActionMode('direct')
    setActionSurfaceOpen(true)
  }, [])

  const openAccountPaymentLinkRegenerateAction = useCallback((record: any) => {
    setActionAccount(record)
    setActionSurfaceInitialActionId('payment_link_regenerate')
    setActionSurfaceInitialActionMode('direct')
    setActionSurfaceOpen(true)
  }, [])

  const openAccountProbeStatusAction = useCallback((record: any) => {
    setActionAccount(record)
    setActionSurfaceInitialActionId('probe_local_status')
    setActionSurfaceInitialActionMode('direct')
    setActionSurfaceOpen(true)
  }, [])

  const openAccountInlineAction = useCallback((record: any, actionId: string, mode: 'direct' | 'dialog' = 'direct') => {
    setActionAccount(record)
    setActionSurfaceInitialActionId(actionId)
    setActionSurfaceInitialActionMode(mode)
    setActionSurfaceOpen(true)
  }, [])

  const loadConfigCache = useCallback(async (options: { force?: boolean } = {}) => {
    if (!options.force && configCache) return configCache
    const cfg = await apiFetch('/config')
    setConfigCache(cfg)
    return cfg
  }, [configCache])

  const load = useCallback(async () => {
    await accountsQuery.refetch()
  }, [accountsQuery.refetch])

  useEffect(() => {
    const data = accountsQuery.data
    if (!data) return
    const nextTotal = data.total || 0
    setAccounts((data.items || []).map(normalizeAccount))
    setTotal(nextTotal)

    const maxPage = Math.max(1, Math.ceil(nextTotal / ACCOUNTS_PAGE_SIZE))
    if (currentPage > maxPage) {
      setCurrentPage(maxPage)
    }
  }, [accountsQuery.data, currentPage])

  useEffect(() => {
    if (!detailAccount?.id) return
    const latest = accounts.find((item) => item.id === detailAccount.id)
    if (latest) {
      setDetailAccount((prev: any) => (
        prev && prev.id === latest.id
          ? { ...prev, ...latest }
          : prev
      ))
    }
  }, [accounts, detailAccount?.id])

  useEffect(() => {
    if (!actionAccount?.id) return
    const latest = accounts.find((item) => item.id === actionAccount.id)
    if (latest) {
      setActionAccount((prev: any) => (
        prev && prev.id === latest.id
          ? { ...prev, ...latest }
          : prev
      ))
    }
  }, [accounts, actionAccount?.id])

  useEffect(() => {
    const data = accountDetailQuery.data
    if (!data || !detailModalOpen) return
    setDetailAccount(normalizeAccount(data))
  }, [accountDetailQuery.data, detailModalOpen])

  const markAccountUsed = useCallback(async (accountId: number) => {
    if (!accountId) return
    await apiFetch(`/accounts/${accountId}/mark-used`, {
      method: 'POST',
      body: JSON.stringify({ used: true }),
    })
    setAccounts((prev) =>
      prev.map((item) => (
        item.id === accountId
          ? normalizeAccount({
              ...item,
              extra_json: JSON.stringify({ ...(item.extra || {}), manually_used: true }),
            })
          : item
      )),
    )
    if (detailAccount?.id === accountId) {
      setDetailAccount((prev: any) => prev ? normalizeAccount({
        ...prev,
        extra_json: JSON.stringify({ ...(prev.extra || {}), manually_used: true }),
      }) : prev)
    }
    if (actionAccount?.id === accountId) {
      setActionAccount((prev: any) => prev ? normalizeAccount({
        ...prev,
        extra_json: JSON.stringify({ ...(prev.extra || {}), manually_used: true }),
      }) : prev)
    }
  }, [detailAccount?.id, actionAccount?.id])

  const ensurePlatformActionsLoaded = useCallback(async () => {
    if (platformActionsLoading || platformActions.length > 0) return
    setPlatformActionsLoading(true)
    try {
      const data = await apiFetch(`/actions/${currentPlatform}`)
      setPlatformActions(data.actions || [])
    } catch {
      setPlatformActions([])
    } finally {
      setPlatformActionsLoading(false)
    }
  }, [currentPlatform, platformActions.length, platformActionsLoading])

  useEffect(() => {
    void ensurePlatformActionsLoaded()
  }, [ensurePlatformActionsLoaded])

  const refreshActiveTasks = useCallback(async () => {
    setActiveTasksPanelOpen(true)
    await activeTasksQuery.refetch()
  }, [activeTasksQuery])

  const openTaskFromSnapshot = (snapshot: any) => {
    const id = String(snapshot?.id || snapshot?.task_id || '').trim()
    if (!id) return
    const normalizedSnapshot = {
      ...snapshot,
      id,
      task_id: id,
      status: normalizeTaskStatus(snapshot?.status || snapshot?.status_snapshot),
    }
    setTaskId(id)
    setTaskSnapshot(normalizedSnapshot)
    setTaskModalMode(taskModalModeFromSource(snapshot?.source))
    setTaskModalAccount(null)
    setRegisterModalOpen(true)
  }

  useEffect(() => {
    setSelectedPendingInviteRowKeys((prev) => {
      const next = prev.filter((key) => pendingBusinessInvites.some((item: any) => item.id === key))
      if (next.length === prev.length && next.every((value, index) => value === prev[index])) {
        return prev
      }
      return next
    })
  }, [pendingBusinessInvites])

  const handleActivatePendingInvite = async (inviteId: number) => {
    setActivatingPendingInviteId(inviteId)
    try {
      const res = await apiFetch(`/chatgpt/pending-business-invites/${inviteId}/activate`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      message.success(`激活成功：${res.email || inviteId}`)
      await pendingInvitesQuery.refetch()
      load()
    } catch (e: any) {
      message.error(`激活失败: ${e.message}`)
      await pendingInvitesQuery.refetch()
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
      await pendingInvitesQuery.refetch()
      load()
    } catch (e: any) {
      message.error(`批量激活失败: ${e.message}`)
      await pendingInvitesQuery.refetch()
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
      await pendingInvitesQuery.refetch()
    } catch (e: any) {
      message.error(`标记放弃失败: ${e.message}`)
    } finally {
      setAbandoningPendingInviteId(null)
    }
  }

  useEffect(() => {
    if (!registerModalOpen) return
    loadConfigCache()
      .then((cfg) => {
        const provider = String(cfg?.mail_provider || 'luckmail').trim() || 'luckmail'
        const savedSettings = loadRegisterFormSettings(currentPlatform)
        const savedEmail = window.localStorage.getItem('auto-chatgpt.manual_email_otp.email') || ''
        setRegisterMailProvider(provider)
        registerForm.setFieldsValue({
          count: Number(savedSettings.count || 1) || 1,
          concurrency: Number(savedSettings.concurrency || 1) || 1,
          register_delay_seconds: Number(savedSettings.register_delay_seconds || 0) || 0,
          mail_provider_override: String(savedSettings.mail_provider_override || '__global__'),
          email: String(savedSettings.email || savedEmail || '').trim(),
          login_password: String(cfg.chatgpt_existing_account_login_password || '').trim(),
          chatgpt_existing_account_capture: savedSettings.chatgpt_existing_account_capture ?? false,
          chatgpt_enable_team_invite:
            savedSettings.chatgpt_enable_team_invite ?? parseBooleanConfigValue(cfg.chatgpt_enable_team_invite),
          chatgpt_team_invite_deferred_activation:
            savedSettings.chatgpt_team_invite_deferred_activation ?? parseBooleanConfigValue(cfg.chatgpt_team_invite_deferred_activation),
          chatgpt_capture_business_workspace:
            savedSettings.chatgpt_capture_business_workspace
            ?? (cfg.chatgpt_capture_business_workspace === '' ? false : parseBooleanConfigValue(cfg.chatgpt_capture_business_workspace)),
          chatgpt_capture_free_workspace:
            savedSettings.chatgpt_capture_free_workspace
            ?? (cfg.chatgpt_capture_free_workspace === '' ? true : parseBooleanConfigValue(cfg.chatgpt_capture_free_workspace)),
          chatgpt_save_registration_access_token_account:
            savedSettings.chatgpt_save_registration_access_token_account ?? false,
        })
      })
      .catch(() => {
        setRegisterMailProvider('luckmail')
        const savedSettings = loadRegisterFormSettings(currentPlatform)
        const savedEmail = window.localStorage.getItem('auto-chatgpt.manual_email_otp.email') || ''
        registerForm.setFieldsValue({
          count: Number(savedSettings.count || 1) || 1,
          concurrency: Number(savedSettings.concurrency || 1) || 1,
          register_delay_seconds: Number(savedSettings.register_delay_seconds || 0) || 0,
          mail_provider_override: String(savedSettings.mail_provider_override || '__global__'),
          email: String(savedSettings.email || savedEmail || '').trim(),
          login_password: '',
          chatgpt_existing_account_capture: savedSettings.chatgpt_existing_account_capture ?? false,
          chatgpt_enable_team_invite: savedSettings.chatgpt_enable_team_invite ?? false,
          chatgpt_team_invite_deferred_activation: savedSettings.chatgpt_team_invite_deferred_activation ?? false,
          chatgpt_capture_business_workspace: savedSettings.chatgpt_capture_business_workspace ?? false,
          chatgpt_capture_free_workspace: savedSettings.chatgpt_capture_free_workspace ?? true,
          chatgpt_save_registration_access_token_account: savedSettings.chatgpt_save_registration_access_token_account ?? false,
        })
      })
  }, [registerModalOpen, currentPlatform, registerForm, loadConfigCache])

  useEffect(() => {
    if (!taskId || !registerModalOpen) {
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
        setActiveTasksPanelOpen(true)
        void activeTasksQuery.refetch()
        if (isActiveTaskStatus(snapshot?.status)) {
          timer = window.setTimeout(pull, 1000)
        } else {
          clearTaskModalStorage()
        }
      } catch {
        if (cancelled) return
        clearTaskModalStorage()
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
  }, [taskId, registerModalOpen, refreshActiveTasks])

  useEffect(() => {
    if (typeof window === 'undefined') return
    if (!registerModalOpen || !taskId) return
    window.localStorage.setItem(TASK_MODAL_STORAGE_KEY, JSON.stringify({
      taskId,
      taskModalMode,
      taskModalAccount: taskModalAccount
        ? {
            id: taskModalAccount.id,
            email: taskModalAccount.email,
          }
        : null,
    }))
  }, [registerModalOpen, taskId, taskModalMode, taskModalAccount])

  useEffect(() => {
    if (typeof window === 'undefined') return
    const raw = window.localStorage.getItem(TASK_MODAL_STORAGE_KEY)
    if (!raw) return
    let cancelled = false

    const restoreSavedTask = async () => {
      try {
        const saved = JSON.parse(raw)
        const restoredTaskId = String(saved?.taskId || '').trim()
        if (!restoredTaskId) {
          clearTaskModalStorage()
          return
        }
        const snapshot = await apiFetch(`/tasks/${restoredTaskId}`)
        if (cancelled) return
        if (!isActiveTaskStatus(snapshot?.status)) {
          clearTaskModalStorage()
          return
        }
        setTaskId(restoredTaskId)
        setTaskSnapshot(snapshot)
        setTaskModalMode(taskModalModeFromSource(saved?.taskModalMode))
        setTaskModalAccount(saved?.taskModalAccount || null)
        setRegisterModalOpen(true)
        setActiveTasksPanelOpen(true)
        void activeTasksQuery.refetch()
      } catch {
        clearTaskModalStorage()
      }
    }

    void restoreSavedTask()
    return () => {
      cancelled = true
    }
    // restore once on mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const getRefreshToken = (record: any): string => {
    if (record?.refresh_token) return String(record.refresh_token || '')
    try {
      const extra = JSON.parse(record.extra_json || '{}')
      return extra.refresh_token || ''
    } catch {
      return ''
    }
  }

  const canImportAccountToTeam = (record: any): boolean => {
    if (currentPlatform !== 'chatgpt') return false
    if (String(record?.workspace_scope || record?.extra?.chatgpt_workspace_scope || '').trim().toLowerCase() !== 'business') return false
    const rt = getRefreshToken(record)
    const accessToken = String(record?.token || '').trim()
    const sessionToken = String(record?.session_token || record?.extra?.session_token || '').trim()
    return Boolean(rt || accessToken || sessionToken)
  }

  const exportCsv = async () => {
    if (currentPlatform === 'chatgpt') {
      try {
        const token = localStorage.getItem('auth_token') || ''
        if (!token) {
          throw new Error('未认证，请先登录')
        }
        const selectedIds = selectedRowKeys
          .map((key) => Number(key))
          .filter((id) => Number.isFinite(id) && id > 0)
        const res = await fetch('/api/chatgpt/export-sub2api-ticket', {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${token}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ ids: selectedIds }),
        })
        if (!res.ok) {
          let detail = ''
          try {
            const data = await res.json()
            detail = String(data?.detail || data?.message || '')
          } catch {
            detail = await res.text()
          }
          throw new Error(detail || `导出失败: HTTP ${res.status}`)
        }
        const data = await res.json()
        const ticket = String(data?.ticket || '').trim()
        if (!ticket) {
          throw new Error('导出失败：后端未返回下载票据')
        }
        window.location.assign(`/api/chatgpt/export-sub2api-download?ticket=${encodeURIComponent(ticket)}`)
        return
      } catch (e: any) {
        message.error(e?.message || '导出失败')
        return
      }
    }

    const header = 'email,password,status,region,cashier_url,created_at'
    const rows = accounts.map((a) => [a.email, a.password, a.status, a.region, a.cashier_url, a.created_at].join(','))
    const blob = new Blob([[header, ...rows].join('\n')], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${currentPlatform}_accounts.csv`
    a.style.display = 'none'
    document.body.appendChild(a)
    a.click()
    a.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  const handleImportAccountToTeam = async (record: any) => {
    setImportingTeamAccountId(record.id)
    try {
      const res = await apiFetch(`/team-lite/teams/import-from-account/${record.id}`, { method: 'POST' })
      message.success(res.message || '已导入 Team 列表')
    } catch (e: any) {
      message.error(e.message || '导入 Team 失败')
    } finally {
      setImportingTeamAccountId(null)
    }
  }

  const handleResumeSubscriptionAuth = async (record: any) => {
    setResumeAuthAccountId(record.id)
    try {
      const res = await apiFetch('/tasks/chatgpt/resume-subscription-auth', {
        method: 'POST',
        body: JSON.stringify({ account_id: Number(record.id) }),
      })
      if (!res?.task_id) {
        throw new Error('任务创建失败：未返回 task_id')
      }
      setTaskModalMode('resume_auth')
      setTaskModalAccount(record)
      setTaskId(res.task_id)
      setRegisterModalOpen(true)
      message.success('补抓Auth任务已启动')
    } catch (e: any) {
      message.error(e?.message || '补抓 Auth 失败')
      await pendingInvitesQuery.refetch()
    } finally {
      setResumeAuthAccountId(null)
    }
  }

  const getResumeAuthScope = (): 'selected' | 'filtered' => (selectedRowKeys.length > 0 ? 'selected' : 'filtered')

  const getResumeAuthSelectedIds = () =>
    Array.from(selectedRowKeys)
      .map((value) => Number(value))
      .filter((value) => Number.isInteger(value) && value > 0)

  const buildResumeAuthMenuLabel = () => {
    const scope = getResumeAuthScope()
    return scope === 'selected'
      ? `补抓所选 Auth (${selectedRowKeys.length})`
      : `补抓当前筛选待补抓账号 (${total})`
  }

  const handleBatchResumeSubscriptionAuth = async (scope: 'selected' | 'filtered') => {
    const toastKey = `resume-auth:${scope}`
    const body: Record<string, unknown> = {}
    let requestedCount = total

    if (scope === 'selected') {
      const accountIds = getResumeAuthSelectedIds()
      if (accountIds.length === 0) {
        message.warning('请先选择要补抓的账号')
        return
      }
      requestedCount = accountIds.length
      body.account_ids = accountIds
    } else {
      body.all_filtered = true
      if (search) body.email = search
      if (filterStatus) body.status = filterStatus
    }

    setBatchResumeAuthLoading(scope)
    message.loading({ content: '批量补抓Auth任务创建中...', key: toastKey, duration: 0 })
    try {
      const res = await apiFetch('/tasks/chatgpt/resume-subscription-auth/batch', {
        method: 'POST',
        body: JSON.stringify(body),
      })

      const eligible = Number(res?.eligible || 0)
      const skipped = Number(res?.skipped || 0)
      const missing = Number(res?.missing || 0)
      const taskIdFromResponse = String(res?.task_id || '').trim()

      if (!taskIdFromResponse) {
        message.info({
          content: `没有可执行补抓的账号。请求 ${requestedCount} 个，跳过 ${skipped} 个${missing > 0 ? `，缺失 ${missing} 个` : ''}`,
          key: toastKey,
        })
        if (res && typeof res === 'object') {
          showBatchActionResult('批量补抓Auth结果', res)
        }
        return
      }

      const snapshot = await apiFetch(`/tasks/${taskIdFromResponse}`)
      setTaskModalMode('resume_auth')
      setTaskModalAccount(
        scope === 'selected'
          ? null
          : { email: `当前筛选 ${eligible} 个账号` },
      )
      setTaskId(taskIdFromResponse)
      setTaskSnapshot(snapshot)
      setRegisterModalOpen(true)
      setActiveTasksPanelOpen(true)
      void activeTasksQuery.refetch()
      message.success({
        content: `批量补抓Auth任务已启动：可执行 ${eligible} 个，跳过 ${skipped} 个${missing > 0 ? `，缺失 ${missing} 个` : ''}`,
        key: toastKey,
      })
      showBatchActionResult('批量补抓Auth结果', res)
    } catch (e: any) {
      message.error({ content: `批量补抓Auth失败: ${e.message}`, key: toastKey })
    } finally {
      setBatchResumeAuthLoading('')
    }
  }

  const handleBatchPaymentLink = async () => {
    const scope: 'selected' | 'filtered' = selectedRowKeys.length > 0 ? 'selected' : 'filtered'
    const toastKey = `payment-link:${scope}`
    const body: Record<string, unknown> = {
      skip_existing: true,
      force_refresh: false,
      params: {},
    }
    let requestedCount = total

    if (scope === 'selected') {
      const accountIds = Array.from(selectedRowKeys)
        .map((value) => Number(value))
        .filter((value) => Number.isInteger(value) && value > 0)
      if (accountIds.length === 0) {
        message.warning('请先选择要生成订阅链接的账号')
        return
      }
      requestedCount = accountIds.length
      body.account_ids = accountIds
    } else {
      body.all_filtered = true
      if (search) body.email = search
      if (filterStatus) body.status = filterStatus
    }

    setBatchPaymentLinkLoading(true)
    message.loading({ content: '批量订阅链接任务创建中...', key: toastKey, duration: 0 })
    try {
      const res = await apiFetch('/tasks/chatgpt/payment-links/batch', {
        method: 'POST',
        body: JSON.stringify(body),
      })

      const eligible = Number(res?.eligible || 0)
      const skipped = Number(res?.skipped || 0)
      const missing = Number(res?.missing || 0)
      const taskIdFromResponse = String(res?.task_id || '').trim()

      if (!taskIdFromResponse) {
        message.info({
          content: `没有可生成订阅链接的账号。请求 ${requestedCount} 个，跳过 ${skipped} 个${missing > 0 ? `，缺失 ${missing} 个` : ''}`,
          key: toastKey,
        })
        if (res && typeof res === 'object') {
          showBatchActionResult('批量订阅链接结果', res)
        }
        return
      }

      const snapshot = await apiFetch(`/tasks/${taskIdFromResponse}`)
      setTaskModalMode('payment_link')
      setTaskModalAccount(scope === 'selected' ? null : { email: `当前筛选 ${eligible} 个账号` })
      setTaskId(taskIdFromResponse)
      setTaskSnapshot(snapshot)
      setRegisterModalOpen(true)
      setActiveTasksPanelOpen(true)
      void activeTasksQuery.refetch()
      message.success({
        content: `批量订阅链接任务已启动：可执行 ${eligible} 个，跳过 ${skipped} 个${missing > 0 ? `，缺失 ${missing} 个` : ''}`,
        key: toastKey,
      })
      showBatchActionResult('批量订阅链接结果', res)
    } catch (e: any) {
      message.error({ content: `批量订阅链接失败: ${e.message}`, key: toastKey })
    } finally {
      setBatchPaymentLinkLoading(false)
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

  const handleDeleteAccount = async (record: any) => {
    const accountId = Number(record?.id || 0)
    if (!accountId) return
    const label = String(record?.email || accountId)
    await apiFetch(`/accounts/${accountId}`, { method: 'DELETE' })
    message.success(`已删除账号：${label}`)
    setSelectedRowKeys((keys) => keys.filter((key) => Number(key) !== accountId))
    load()
  }

  const getSelectedChatgptAccounts = () => {
    const selected = new Set(selectedRowKeys.map((key) => Number(key)))
    return accounts.filter((account) => account.platform === 'chatgpt' && selected.has(Number(account.id)))
  }

  const loadGopayBatchConfig = async () => {
    const [data, otpSettings] = await Promise.all([
      apiFetch('/config'),
      apiFetch('/integrations/gopay-otp').catch(() => ({})),
    ])
    const phones = normalizeGopayPhoneCandidates(data.chatgpt_gopay_phone_candidates).filter((phone) => phone.enabled !== false)
    const defaults = parseMaybeJsonObject(data.chatgpt_gopay_defaults)
    setBatchGopayOtpAutoResendDelay(normalizeGopayOtpAutoResendDelay(otpSettings.otp_auto_resend_delay_seconds))
    setBatchGopayPhones(phones)
    setBatchGopayDefaults(defaults)
    if (batchGopayOpen && !batchGopayStarted && batchGopayItems.length > 0 && phones.length > 0) {
      setBatchGopayItems((items) => reassignBatchGopayPhones(items, phones))
    }
    return { phones, defaults }
  }

  const saveBatchGopayOtpAutoResendDelay = async (
    value: unknown,
    options: { notify?: boolean; throwOnError?: boolean } = {},
  ) => {
    const delay = normalizeGopayOtpAutoResendDelay(value)
    setBatchGopayOtpDelaySaving(true)
    try {
      const data = await apiFetch('/integrations/gopay-otp/settings', {
        method: 'PUT',
        body: JSON.stringify({ otp_auto_resend_delay_seconds: delay }),
      })
      const savedDelay = normalizeGopayOtpAutoResendDelay(data.otp_auto_resend_delay_seconds)
      setBatchGopayOtpAutoResendDelay(savedDelay)
      if (options.notify !== false) message.success('GoPay OTP 自动重发延迟已保存')
      return savedDelay
    } catch (e: any) {
      if (options.notify !== false) message.error(e?.message || '保存 GoPay OTP 自动重发延迟失败')
      if (options.throwOnError) throw e
      return delay
    } finally {
      setBatchGopayOtpDelaySaving(false)
    }
  }

  const saveBatchGopayPhoneCandidates = async (nextCandidates: GopayPhoneCandidate[]) => {
    const normalized = normalizeGopayPhoneCandidates(nextCandidates)
    setBatchGopayPhones(normalized)
    await apiFetch('/config', {
      method: 'PUT',
      body: JSON.stringify({
        data: {
          chatgpt_gopay_phone_candidates: JSON.stringify(normalized),
        },
      }),
    })
    if (batchGopayOpen && !batchGopayStarted && batchGopayItems.length > 0 && normalized.length > 0) {
      setBatchGopayItems((items) => reassignBatchGopayPhones(items, normalized))
    }
    return normalized
  }

  const deleteBatchGopayPhone = async (phoneId: string) => {
    await saveBatchGopayPhoneCandidates(removeGopayPhoneCandidate(batchGopayPhones, phoneId))
  }

  const moveBatchGopayPhone = async (phoneId: string, direction: 'up' | 'down' | 'top' | 'bottom') => {
    await saveBatchGopayPhoneCandidates(moveGopayPhoneCandidate(batchGopayPhones, phoneId, direction))
  }

  const openBatchGopayWorkbench = async () => {
    const selectedAccounts = getSelectedChatgptAccounts()
    if (selectedAccounts.length === 0) {
      message.warning('请先选择要批量支付的 ChatGPT 账号')
      return
    }
    setBatchGopayOpen(true)
    setBatchGopayLoading(true)
    setBatchGopayPhoneCountryCode('86')
    setBatchGopayPhoneNumber('')
    setBatchGopayPhoneSaving(false)
    setBatchGopayItems([])
    setBatchGopayPhones([])
    setBatchGopayDefaults({})
    setBatchGopayStarted(false)
    setBatchGopayNextRoundAt(null)
    try {
      const { phones } = await loadGopayBatchConfig()
      if (phones.length === 0) {
        message.warning('手机号池为空，请先在单账号 GoPay 中保存手机号候选')
        return
      }
      const items = buildBatchGopayItems(selectedAccounts, phones)
      setBatchGopayItems(items)
    } catch (e: any) {
      message.error(e?.message || '加载 GoPay 批量配置失败')
    } finally {
      setBatchGopayLoading(false)
    }
  }

  const addBatchGopayPhoneToPool = async () => {
    const phone_country_code = normalizeGopayPhonePart(batchGopayPhoneCountryCode)
    const phone_number = normalizeGopayPhonePart(batchGopayPhoneNumber)
    if (!phone_country_code || !phone_number) {
      message.warning('请输入区号和手机号')
      return
    }
    setBatchGopayPhoneSaving(true)
    try {
      await saveBatchGopayPhoneCandidates(upsertGopayPhoneCandidate(batchGopayPhones, {
        label: `GoPay ${batchGopayPhones.length + 1}`,
        phone_country_code,
        phone_number,
        enabled: true,
      }))
      setBatchGopayPhoneNumber('')
      message.success('手机号已加入候选池')
    } catch (e: any) {
      message.error(e?.message || '加入手机号池失败')
    } finally {
      setBatchGopayPhoneSaving(false)
    }
  }

  const updateBatchGopayItem = (accountId: number, patch: Partial<BatchGopayItem>) => {
    setBatchGopayItems((items) => items.map((item) => (
      Number(item.account.id) === Number(accountId)
        ? { ...item, ...patch }
        : item
    )))
  }

  const buildBatchGopayPayload = (item: BatchGopayItem) => {
    const accountEmail = String(item.account.email || '').trim()
    const phoneCountryCode = String(item.phone.phone_country_code || '').trim()
    const phoneNumber = String(item.phone.phone_number || '').trim()
    return {
      phone_country_code: phoneCountryCode,
      phone_number: phoneNumber,
      pin: String(batchGopayDefaults.pin || '').trim(),
      proxy: String(batchGopayDefaults.proxy || '').trim(),
      browser_profile_mode: String(batchGopayDefaults.browser_profile_mode || 'fresh_payment').trim() || 'fresh_payment',
      save_defaults: false,
      plan: 'plus',
      country: normalizeCheckoutCountry(batchGopayDefaults.country || DEFAULT_CHECKOUT_COUNTRY),
      currency: normalizeCheckoutCurrency(batchGopayDefaults.currency || DEFAULT_CHECKOUT_CURRENCY),
      checkout_url: '',
      billing_name: String(batchGopayDefaults.billing_name || '').trim(),
      billing_email: accountEmail || String(batchGopayDefaults.billing_email || '').trim(),
      billing_country: String(batchGopayDefaults.billing_country || 'US').trim(),
      billing_line1: String(batchGopayDefaults.billing_line1 || '').trim(),
      billing_city: String(batchGopayDefaults.billing_city || '').trim(),
      billing_state: String(batchGopayDefaults.billing_state || '').trim(),
      billing_postal_code: String(batchGopayDefaults.billing_postal_code || '').trim(),
      billing_generation_context: [
        `batch_index=${item.batchIndex || ''}`,
        `round=${item.round || ''}`,
        `account_id=${item.account.id || ''}`,
        `account_email=${accountEmail}`,
        `phone=+${phoneCountryCode} ${phoneNumber}`,
      ].filter((part) => !part.endsWith('=') && !part.endsWith('=+ ')).join('; '),
    }
  }

  const startBatchGopayItem = async (item: BatchGopayItem) => {
    if (batchGopayCancelRequestedRef.current) {
      updateBatchGopayItem(item.account.id, { status: 'cancelled', error: '' })
      return null
    }
    updateBatchGopayItem(item.account.id, { status: 'starting', error: '' })
    try {
      const data = await apiFetch(`/chatgpt/${item.account.id}/gopay/start`, {
        method: 'POST',
        body: JSON.stringify(buildBatchGopayPayload(item)),
      })
      if (batchGopayCancelRequestedRef.current) {
        const cancelled = await apiFetch(`/chatgpt/${item.account.id}/gopay/${encodeURIComponent(data.session_id)}/cancel`, {
          method: 'POST',
        })
        updateBatchGopayItem(item.account.id, { status: 'cancelled', snapshot: cancelled, error: '' })
        return cancelled
      }
      updateBatchGopayItem(item.account.id, { status: 'running', snapshot: data, error: '' })
      return data
    } catch (e: any) {
      updateBatchGopayItem(item.account.id, { status: 'failed', error: e?.message || '启动失败' })
      return null
    }
  }

  const startBatchGopayRound = async (round: number) => {
    if (batchGopayStartingRef.current) return
    batchGopayStartingRef.current = true
    try {
      const roundItems = batchGopayItems.filter((item) => item.round === round && item.status === 'queued')
      await Promise.all(roundItems.map((item) => startBatchGopayItem(item)))
    } finally {
      batchGopayStartingRef.current = false
    }
  }

  const startBatchGopay = async () => {
    if (batchGopayItems.length === 0) return
    try {
      await saveBatchGopayOtpAutoResendDelay(batchGopayOtpAutoResendDelay, { notify: false, throwOnError: true })
      batchGopayCancelRequestedRef.current = false
      setBatchGopayStarted(true)
      setBatchGopayNextRoundAt(null)
      await startBatchGopayRound(1)
    } catch (e: any) {
      message.error(e?.message || '启动批量 GoPay 失败')
    }
  }

  const batchGopayActiveItems = batchGopayItems.filter((item) => {
    const phase = String(item.snapshot?.phase || '')
    return item.snapshot?.session_id && GOPAY_ACTIVE_PHASES.has(phase)
  })

  useEffect(() => {
    if (!pageVisible || !batchGopayOpen || batchGopayActiveItems.length === 0) return
    const timer = window.setInterval(async () => {
      await Promise.all(batchGopayActiveItems.map(async (item) => {
        try {
          const data = await apiFetch(`/chatgpt/${item.account.id}/gopay/${encodeURIComponent(item.snapshot.session_id)}`)
          const phase = String(data.phase || '')
          updateBatchGopayItem(item.account.id, {
            snapshot: data,
            status: phase === 'succeeded' ? 'done' : phase === 'failed' ? 'failed' : phase === 'cancelled' ? 'cancelled' : 'running',
            error: data.last_error || '',
          })
        } catch (e: any) {
          updateBatchGopayItem(item.account.id, { error: e?.message || '刷新状态失败' })
        }
      }))
    }, 3000)
    return () => window.clearInterval(timer)
  }, [pageVisible, batchGopayOpen, batchGopayActiveItems.map((item) => `${item.account.id}:${item.snapshot?.session_id}:${item.snapshot?.phase}`).join('|')])

  useEffect(() => {
    if (!batchGopayOpen || !batchGopayStarted || batchGopayItems.length === 0) return
    const rounds = Array.from(new Set(batchGopayItems.map((item) => item.round))).sort((a, b) => a - b)
    const currentRound = rounds.find((round) => batchGopayItems.some((item) => item.round === round && ['queued', 'starting', 'running'].includes(item.status)))
    if (!currentRound) return
    const currentItems = batchGopayItems.filter((item) => item.round === currentRound)
    const hasQueuedCurrent = currentItems.some((item) => item.status === 'queued')
    const hasActiveCurrent = currentItems.some((item) => item.status === 'starting' || item.status === 'running')
    if (hasQueuedCurrent && !hasActiveCurrent && batchGopayNextRoundAt == null && currentRound === 1) {
      startBatchGopayRound(currentRound)
      return
    }
    if (hasQueuedCurrent || hasActiveCurrent) return
    const nextRound = rounds.find((round) => round > currentRound && batchGopayItems.some((item) => item.round === round && item.status === 'queued'))
    if (!nextRound) return
    if (batchGopayNextRoundAt == null) {
      setBatchGopayNextRoundAt(Date.now() + Math.max(0, Number(batchGopayRoundInterval || 0)) * 1000)
    }
  }, [batchGopayOpen, batchGopayStarted, batchGopayItems, batchGopayNextRoundAt, batchGopayRoundInterval])

  useEffect(() => {
    if (!batchGopayOpen || !batchGopayStarted || batchGopayNextRoundAt == null) return
    const delay = Math.max(0, batchGopayNextRoundAt - Date.now())
    const timer = window.setTimeout(async () => {
      const next = Math.min(...batchGopayItems.filter((item) => item.status === 'queued').map((item) => item.round))
      setBatchGopayNextRoundAt(null)
      if (Number.isFinite(next)) {
        await startBatchGopayRound(next)
      }
    }, delay)
    return () => window.clearTimeout(timer)
  }, [batchGopayOpen, batchGopayStarted, batchGopayNextRoundAt, batchGopayItems])

  const submitBatchGopayInput = async (item: BatchGopayItem, value: string) => {
    if (!item.snapshot?.session_id) return
    const phase = String(item.snapshot.phase || '')
    const path = phase === 'waiting_otp' ? 'otp' : 'pin'
    const key = phase === 'waiting_otp' ? 'otp' : 'pin'
    updateBatchGopayItem(item.account.id, { submitting: true })
    try {
      const data = await apiFetch(`/chatgpt/${item.account.id}/gopay/${encodeURIComponent(item.snapshot.session_id)}/${path}`, {
        method: 'POST',
        body: JSON.stringify({ [key]: String(value || '').trim() }),
      })
      updateBatchGopayItem(item.account.id, { snapshot: data, submitting: false, error: '' })
      message.success(`已提交 ${item.account.email}`)
    } catch (e: any) {
      updateBatchGopayItem(item.account.id, { submitting: false, error: e?.message || '提交失败' })
      message.error(e?.message || '提交失败')
    }
  }

  const resendBatchGopayOtp = async (item: BatchGopayItem) => {
    if (!item.snapshot?.session_id) return
    updateBatchGopayItem(item.account.id, { submitting: true, error: '' })
    try {
      const data = await apiFetch(`/chatgpt/${item.account.id}/gopay/${encodeURIComponent(item.snapshot.session_id)}/resend-otp`, {
        method: 'POST',
      })
      updateBatchGopayItem(item.account.id, { snapshot: data, submitting: false, error: '' })
      message.success(`GoPay OTP 重发请求已提交：${item.account.email}`)
    } catch (e: any) {
      updateBatchGopayItem(item.account.id, { submitting: false, error: e?.message || '重发 OTP 失败' })
      message.error(e?.message || '重发 OTP 失败')
    }
  }

  const cancelBatchGopayItem = async (item: BatchGopayItem) => {
    if (!item.snapshot?.session_id) {
      updateBatchGopayItem(item.account.id, { status: 'cancelled' })
      return
    }
    try {
      const data = await apiFetch(`/chatgpt/${item.account.id}/gopay/${encodeURIComponent(item.snapshot.session_id)}/cancel`, {
        method: 'POST',
      })
      updateBatchGopayItem(item.account.id, { status: 'cancelled', snapshot: data })
    } catch (e: any) {
      message.error(e?.message || '取消 GoPay 会话失败')
    }
  }

  const cancelBatchGopayAll = async () => {
    const cancellableItems = batchGopayItems.filter((item) => (
      ['queued', 'starting', 'running'].includes(item.status)
      || (item.snapshot?.session_id && GOPAY_ACTIVE_PHASES.has(String(item.snapshot?.phase || '')))
    ))
    if (cancellableItems.length === 0) {
      message.info('当前没有可取消的批量支付任务')
      return
    }
    batchGopayCancelRequestedRef.current = true
    setBatchGopayStarted(false)
    setBatchGopayNextRoundAt(null)
    await Promise.all(cancellableItems.map((item) => cancelBatchGopayItem(item)))
    message.success(`已取消 ${cancellableItems.length} 个批量支付任务`)
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
      chatgpt_existing_account_capture: Boolean(values.chatgpt_existing_account_capture),
      chatgpt_enable_team_invite: Boolean(values.chatgpt_enable_team_invite),
      chatgpt_team_invite_deferred_activation: Boolean(values.chatgpt_team_invite_deferred_activation),
      chatgpt_capture_free_workspace:
        values.chatgpt_capture_free_workspace === undefined ? true : Boolean(values.chatgpt_capture_free_workspace),
      chatgpt_capture_business_workspace:
        values.chatgpt_capture_business_workspace === undefined ? false : Boolean(values.chatgpt_capture_business_workspace),
      chatgpt_save_registration_access_token_account: Boolean(values.chatgpt_save_registration_access_token_account),
    }

    setRegisterSettingsSaving(true)
    try {
      saveRegisterFormSettings(currentPlatform, settingsPayload)
      if (settingsPayload.mail_provider_override === 'manual_email_otp' && settingsPayload.email) {
        window.localStorage.setItem('auto-chatgpt.manual_email_otp.email', settingsPayload.email)
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
      setTaskModalMode('register')
      setTaskModalAccount(null)
      const cfg = await loadConfigCache({ force: true })
      const selectedProviderOverride = String(values.mail_provider_override || '').trim()
      const resolvedMailProvider =
        selectedProviderOverride && selectedProviderOverride !== '__global__'
          ? selectedProviderOverride
          : (String(cfg.mail_provider || 'luckmail').trim() || 'luckmail')
      setRegisterMailProvider(resolvedMailProvider)
      const executorType = normalizeExecutorForPlatform(currentPlatform, cfg.default_executor)
      const existingAccountCapture =
        currentPlatform === 'chatgpt'
        && chatgptRegistrationMode === CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN
        && resolvedMailProvider === 'manual_email_otp'
        && Boolean(values.chatgpt_existing_account_capture)
      const normalizedLoginPassword = String(values.login_password || '').trim()
      if (existingAccountCapture && !values.chatgpt_capture_business_workspace && !values.chatgpt_capture_free_workspace) {
        throw new Error('已有账号抓 auth 模式至少要选择一个工作空间')
      }
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
        chatgpt_existing_account_capture: currentPlatform === 'chatgpt' ? existingAccountCapture : undefined,
        chatgpt_enable_team_invite:
          currentPlatform === 'chatgpt'
            ? (existingAccountCapture ? false : Boolean(values.chatgpt_enable_team_invite))
            : undefined,
        chatgpt_capture_free_workspace:
          currentPlatform === 'chatgpt'
            ? Boolean(values.chatgpt_capture_free_workspace)
            : undefined,
        chatgpt_capture_business_workspace:
          currentPlatform === 'chatgpt' && (values.chatgpt_enable_team_invite || existingAccountCapture)
            ? values.chatgpt_capture_business_workspace
            : undefined,
        chatgpt_team_invite_deferred_activation:
          currentPlatform === 'chatgpt' && values.chatgpt_enable_team_invite && !existingAccountCapture
            ? Boolean(values.chatgpt_team_invite_deferred_activation)
            : undefined,
        chatgpt_save_registration_access_token_account:
          currentPlatform === 'chatgpt'
            ? Boolean(values.chatgpt_save_registration_access_token_account)
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
        window.localStorage.setItem('auto-chatgpt.manual_email_otp.email', normalizedEmail)
      }

      saveRegisterFormSettings(currentPlatform, {
        count: Number(values.count || 1) || 1,
        concurrency: Number(values.concurrency || 1) || 1,
        register_delay_seconds: Number(values.register_delay_seconds || 0) || 0,
        mail_provider_override: selectedProviderOverride || '__global__',
        email: String(values.email || '').trim(),
        chatgpt_existing_account_capture: Boolean(values.chatgpt_existing_account_capture),
        chatgpt_enable_team_invite: Boolean(values.chatgpt_enable_team_invite),
        chatgpt_team_invite_deferred_activation: Boolean(values.chatgpt_team_invite_deferred_activation),
        chatgpt_capture_free_workspace: Boolean(values.chatgpt_capture_free_workspace),
        chatgpt_capture_business_workspace:
          values.chatgpt_capture_business_workspace === undefined ? false : Boolean(values.chatgpt_capture_business_workspace),
        chatgpt_save_registration_access_token_account: Boolean(values.chatgpt_save_registration_access_token_account),
      })

      const res = await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify({
          platform: currentPlatform,
          email:
            resolvedMailProvider === 'manual_email_otp' && currentPlatform === 'chatgpt'
              ? (String(values.email || '').trim() || null)
              : null,
          password: existingAccountCapture ? (normalizedLoginPassword || null) : null,
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
    await apiFetch(`/accounts/${detailAccount.id}`, {
      method: 'PATCH',
      body: JSON.stringify(values),
    })
    message.success('保存成功')
    setDetailModalOpen(false)
    await accountDetailQuery.refetch()
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
      .filter((item: any) => Object.prototype.hasOwnProperty.call(item || {}, 'ok') && !item.ok)
      .map((item: any) => `[${item.id || '-'}] ${item.email || '-'}: ${item.message || '失败'}`)
    const skippedLines = (result.skipped_items || [])
      .map((item: any) => `[${item.account_id || '-'}] ${item.email || '-'}: ${item.reason || '已跳过'}`)
    const missingLines = (result.missing_ids || [])
      .map((id: any) => `[${id || '-'}] 账号不存在`)
    const allLines = [...lines, ...skippedLines, ...missingLines]

    if (allLines.length === 0) return

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
          {allLines.join('\n')}
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
    gap: isChatgptPlatform ? 4 : 6,
    minWidth: 0,
  }
  const compactPanelStyle: React.CSSProperties = {
    padding: '6px 8px',
    borderRadius: token.borderRadiusLG,
    border: `1px solid ${token.colorBorder}`,
    background: token.colorFillAlter,
  }
  const sub2apiOverview = summarizeSub2ApiStates(accounts)

  const columns: any[] = [
    {
      title: '邮箱',
      dataIndex: 'email',
      key: 'email',
      width: 220,
      render: (text: string, record: any) => {
        const teamInviteOwner = getTeamInviteOwnerLabel(record.teamInviteSource)
        const teamInviteMeta = [
          record.teamInviteSource?.team_name ? `Team: ${record.teamInviteSource.team_name}` : '',
          record.teamInviteSource?.team_id ? `#${record.teamInviteSource.team_id}` : '',
        ].filter(Boolean).join(' · ')

        return (
          <div style={cellStackStyle}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 4, minWidth: 0 }}>
              <Text
                style={{ ...monospaceStyle, flex: 1, minWidth: 0, whiteSpace: 'nowrap', fontSize: 12 }}
                ellipsis={{ tooltip: text }}
              >
                {text}
              </Text>
              {record.manuallyUsed ? (
                <Tag color="orange">已使用</Tag>
              ) : null}
              {record.workspace_label || record.extra?.chatgpt_workspace_label ? (
                <Tag color={(record.workspace_scope || record.extra?.chatgpt_workspace_scope) === 'business' ? 'processing' : 'default'}>
                  {record.workspace_label || record.extra?.chatgpt_workspace_label}
                </Tag>
              ) : null}
              <Button
                type="text"
                size="small"
                icon={<CopyOutlined />}
                onClick={async () => {
                  const ok = await copyText(text)
                  if (ok) {
                    await markAccountUsed(Number(record.id || 0))
                  }
                }}
              />
            </div>
            <Text
              type="secondary"
              style={{ ...secondaryTextStyle, fontSize: 11, lineHeight: '18px' }}
              ellipsis={{ tooltip: record.workspace_display_name || record.extra?.chatgpt_workspace_display_name || record.user_id || `账号 #${record.id}` }}
            >
              {record.workspace_display_name
                || record.extra?.chatgpt_workspace_display_name
                || record.user_id
                || `#${record.id}`}
            </Text>
            {teamInviteOwner ? (
              <Text
                type="secondary"
                style={{ ...secondaryTextStyle, fontSize: 11, lineHeight: '18px' }}
                ellipsis={{ tooltip: `${teamInviteOwner}${teamInviteMeta ? ` · ${teamInviteMeta}` : ''}` }}
              >
                {`${teamInviteOwner}${teamInviteMeta ? ` · ${teamInviteMeta}` : ''}`}
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
      width: 96,
      render: (text: string) => {
        const hasPassword = Boolean(String(text || '').trim())
        return (
          <Space size={2} style={{ width: '100%', justifyContent: 'center' }}>
            <Tag color={hasPassword ? 'success' : 'default'}>{hasPassword ? '有密码' : '无密码'}</Tag>
            {hasPassword ? (
              <Button title="复制密码" type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(text)} />
            ) : null}
          </Space>
        )
      },
    },
    {
      title: 'RT',
      key: 'refresh_token',
      width: 92,
      render: (_: any, record: any) => {
        const rt = getRefreshToken(record)
        if (!rt) {
          return (
            <Space size={2} style={{ width: '100%', justifyContent: 'center' }}>
              <Tag color="default">无RT</Tag>
            </Space>
          )
        }
        return (
          <Space size={2} style={{ width: '100%', justifyContent: 'center' }}>
            <Tag color="success">有RT</Tag>
            <Button title="复制RT" type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(rt)} />
          </Space>
        )
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 84,
      render: (status: string) => <Tag color={STATUS_COLORS[status] || 'default'}>{statusLabel(status)}</Tag>,
    },
  ]

  if (isChatgptPlatform) {
    columns.push(
      {
        title: '本地状态',
        key: 'chatgpt_local_state',
        width: isChatgptPlatform ? 164 : 220,
        render: (_: any, record: any) => {
          const auth = record.chatgptLocal?.auth || {}
          const subscription = record.chatgptLocal?.subscription || {}
          const codex = record.chatgptLocal?.codex || {}
          const capabilities = record.chatgptCapabilities || {}
          const authMeta = authLevelMeta(record.auth_level || capabilities.auth_level, auth.state)
          const materialMeta = workspaceMeta(capabilities)
          const planTag = planMeta(record.subscription_plan || capabilities.subscription_plan || subscription.plan)
          const codexMeta = codexStateMeta(record.codex_state || codex.state)

          return (
            <div style={{ ...cellStackStyle, ...compactPanelStyle }}>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                <Tag color={authMeta.color}>{authMeta.label}</Tag>
                <Tag color={materialMeta.color}>{materialMeta.label}</Tag>
                <Tag color={planTag.color}>{planTag.label}</Tag>
                <Tag color={codexMeta.color}>Codex{codexMeta.label}</Tag>
              </div>
            </div>
          )
        },
      },
      {
        title: 'Sub2API',
        key: 'sub2api_sync',
        width: 88,
        render: (_: any, record: any) => {
          const sync = record.sub2apiSync || {}
          const meta = sub2apiStateMeta(
            record.sub2api_remote_state
              ? { ...sync, remote_state: record.sub2api_remote_state }
              : sync,
          )

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
      width: 64,
      render: (text: string) => {
        const formatted = formatCreatedAt(text)
        return (
          <Text style={{ display: 'block', fontSize: 11, lineHeight: '20px' }} title={`${formatted.date} ${formatted.time}`.trim()}>
            {formatted.date}
          </Text>
        )
      },
    },
    {
      title: '操作',
      key: 'action',
      width: 236,
      fixed: isChatgptPlatform ? 'right' : undefined,
      render: (_: any, record: any) => {
        const commonActions = Array.isArray(platformActions) ? platformActions : []
        const paymentLinkAction = commonActions.find((action: any) => String(action?.id || '').trim().toLowerCase() === 'payment_link')
        const showResumeAuth = isChatgptPlatform && shouldShowResumeAuthButton(record)
        const hiddenIds = new Set([
          paymentLinkAction ? String(paymentLinkAction.id) : '',
          'probe_local_status',
          'resume_subscription_auth',
        ].filter(Boolean))
        const moreActions = commonActions.filter((action: any) => !hiddenIds.has(String(action?.id || '')))
        const moreMenuItems: MenuProps['items'] = [
          { key: '__detail__', label: '账号详情' },
          ...(paymentLinkAction ? [{ key: '__payment_link_config__', label: '订阅链接配置' }] : []),
          ...(paymentLinkAction ? [{ key: '__payment_link_regenerate__', label: '重新生成订阅链接' }] : []),
          ...moreActions.map((action: any) => ({
            key: String(action.id),
            label: String(action.label || action.id),
          })),
          {
            key: '__delete_account__',
            danger: true,
            label: (
              <Popconfirm
                title="确认删除这个账号？"
                description={String(record.email || record.id || '')}
                okText="删除"
                cancelText="取消"
                okButtonProps={{ danger: true }}
                onConfirm={() => handleDeleteAccount(record)}
              >
                <span onClick={(event) => event.stopPropagation()}>删除账号</span>
              </Popconfirm>
            ),
          },
        ]

        return (
          <Space direction="vertical" size={4} style={{ width: '100%' }}>
            <Space size={4} wrap>
              {paymentLinkAction ? (
                <Button
                  type="link"
                  size="small"
                  style={accountActionTextStyles.payment}
                  onClick={() => openAccountPaymentLinkAction(record)}
                >
                  订阅链接
                </Button>
              ) : null}
              <Button
                type="link"
                size="small"
                style={accountActionTextStyles.refresh}
                onClick={() => openAccountProbeStatusAction(record)}
              >
                刷新状态
              </Button>
            </Space>
            <Space size={4} wrap>
              {showResumeAuth ? (
                <Button
                  type="link"
                  size="small"
                  loading={resumeAuthAccountId === record.id}
                  style={accountActionTextStyles.resume}
                  onClick={() => handleResumeSubscriptionAuth(record)}
                >
                  补抓Auth
                </Button>
              ) : null}
            <Dropdown
              menu={{
                items: moreMenuItems,
                onClick: ({ key }) => {
                  if (String(key) === '__detail__') {
                    setDetailAccount(record)
                    setDetailModalOpen(true)
                    return
                  }
                  if (String(key) === '__payment_link_config__') {
                    openAccountInlineAction(record, 'payment_link', 'dialog')
                    return
                  }
                  if (String(key) === '__payment_link_regenerate__') {
                    openAccountPaymentLinkRegenerateAction(record)
                    return
                  }
                  if (String(key) === '__delete_account__') {
                    return
                  }
                  openAccountInlineAction(record, String(key))
                },
              }}
            >
              <Button
                type="link"
                size="small"
                icon={<MoreOutlined />}
                loading={platformActionsLoading}
                style={accountActionTextStyles.more}
              >
                更多
              </Button>
            </Dropdown>
            </Space>
          </Space>
        )
      },
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
  const resumeAuthScope = getResumeAuthScope()
  const resumeAuthMenuItems: MenuProps['items'] = [
    {
      key: resumeAuthScope,
      label: buildResumeAuthMenuLabel(),
      disabled: resumeAuthScope === 'selected' ? selectedRowKeys.length === 0 : total === 0,
    },
  ]

  const renderBatchGopayItem = (item: BatchGopayItem) => {
    const phase = String(item.snapshot?.phase || (item.status === 'queued' ? 'created' : item.status))
    const meta = gopayPhaseMeta(phase)
    const needsInput = phase === 'waiting_otp' || phase === 'waiting_link_pin' || phase === 'waiting_payment_pin'
    const canResendOtp = phase === 'waiting_otp' && Boolean(item.snapshot?.session_id)
    const isTerminal = ['done', 'failed', 'cancelled'].includes(item.status) || ['succeeded', 'failed', 'cancelled'].includes(phase)
    return (
      <div
        key={item.account.id}
        style={{
          border: `1px solid ${token.colorBorder}`,
          borderRadius: token.borderRadiusLG,
          padding: 14,
          background: token.colorBgContainer,
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap', marginBottom: 10 }}>
          <Space wrap>
            <Text strong>{item.account.email || item.account.id}</Text>
            <Tag color="blue">第 {item.round} 轮</Tag>
            <Tag>{formatGopayPhoneLabel(item.phone)}</Tag>
            <Tag color={isTerminal ? (phase === 'succeeded' || item.status === 'done' ? 'success' : item.status === 'cancelled' ? 'default' : 'error') : 'processing'}>
              {meta.title}
            </Tag>
          </Space>
          {!isTerminal ? (
            <Button size="small" danger onClick={() => cancelBatchGopayItem(item)}>
              取消
            </Button>
          ) : null}
        </div>
        <Steps
          size="small"
          current={meta.step}
          status={meta.status === 'error' ? 'error' : meta.status === 'finish' ? 'finish' : 'process'}
          items={[
            { title: '初始化' },
            { title: 'OTP' },
            { title: '绑定 PIN' },
            { title: '支付 PIN' },
            { title: '确认' },
          ]}
        />
          <Alert
            type={item.error || meta.status === 'error' ? 'error' : meta.status === 'finish' ? 'success' : 'info'}
            showIcon
            message={item.error || item.snapshot?.last_error || meta.description}
            style={{ marginTop: 12 }}
          />
        {canResendOtp ? (
          <div style={{ marginTop: 10, display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <Tag color={item.snapshot?.otp_auto_resend_done ? 'success' : 'default'}>
              自动重发 {item.snapshot?.otp_auto_resend_done ? '已触发' : '待触发'}
            </Tag>
            <Text type="secondary">延迟 {item.snapshot?.otp_auto_resend_delay_seconds ?? batchGopayOtpAutoResendDelay} 秒</Text>
            <Text type="secondary">已重发 {item.snapshot?.otp_resend_count ?? 0} 次</Text>
            <Button size="small" loading={item.submitting} onClick={() => resendBatchGopayOtp(item)}>
              重发 OTP
            </Button>
          </div>
        ) : null}
        {needsInput ? (
          <div style={{ marginTop: 12 }}>
            <Text style={{ display: 'block', marginBottom: 6 }}>
              {phase === 'waiting_otp' ? 'GoPay OTP' : phase === 'waiting_link_pin' ? 'GoPay 绑定 PIN' : 'GoPay 支付 PIN'}
            </Text>
            <Input.Search
              enterButton={phase === 'waiting_otp' ? '提交 OTP' : phase === 'waiting_link_pin' ? '提交绑定 PIN' : '提交支付 PIN'}
              loading={item.submitting}
              inputMode="numeric"
              maxLength={8}
              onSearch={(value: string) => {
                const trimmed = String(value || '').trim()
                if (!trimmed) return
                void submitBatchGopayInput(item, trimmed)
              }}
            />
          </div>
        ) : null}
        <div style={{ marginTop: 10 }}>
          <Space>
            <Button
              type="link"
              size="small"
              style={{ paddingLeft: 0 }}
              onClick={() => updateBatchGopayItem(item.account.id, { configOpen: !item.configOpen })}
            >
              {item.configOpen ? '收起参数配置' : '展开参数配置'}
            </Button>
            <Button
              type="link"
              size="small"
              onClick={() => updateBatchGopayItem(item.account.id, { logsOpen: !item.logsOpen })}
            >
              {item.logsOpen ? '收起日志' : '展开日志'}
            </Button>
          </Space>
        </div>
        {item.configOpen ? (
          <pre
            style={{
              margin: '8px 0 0',
              padding: 10,
              borderRadius: token.borderRadius,
              background: token.colorFillAlter,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              fontFamily: 'monospace',
              fontSize: 12,
            }}
          >
            {JSON.stringify(buildBatchGopayPayload(item), null, 2)}
          </pre>
        ) : null}
        {item.logsOpen && item.snapshot?.task_id ? (
          <div style={{ marginTop: 8 }}>
            <TaskLogPanel taskId={String(item.snapshot.task_id)} onDone={load} />
          </div>
        ) : item.logsOpen && item.snapshot?.logs?.length ? (
          <pre
            style={{
              margin: '8px 0 0',
              maxHeight: 180,
              overflow: 'auto',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              fontFamily: 'monospace',
              fontSize: 12,
            }}
          >
            {item.snapshot.logs.join('\n')}
          </pre>
        ) : null}
      </div>
    )
  }

  return (
    <div>
      <AccountsToolbar
        search={search}
        onSearchChange={setSearch}
        filterStatus={filterStatus}
        onFilterStatusChange={setFilterStatus}
        total={total}
        accountsCount={accounts.length}
        selectedRowKeys={selectedRowKeys}
        activeTasksLoading={activeTasksLoading}
        activeTasks={activeTasks}
        onOpenTaskSnapshot={openTaskFromSnapshot}
        onRefreshActiveTasks={refreshActiveTasks}
        onActiveTasksOpen={() => setActiveTasksPanelOpen(true)}
        isChatgptPlatform={currentPlatform === 'chatgpt'}
        batchGopayLoading={batchGopayLoading}
        batchPaymentLinkLoading={batchPaymentLinkLoading}
        onBatchPaymentLink={handleBatchPaymentLink}
        onOpenBatchGopay={openBatchGopayWorkbench}
        onOpenBusinessDeferred={() => setBusinessDeferredModalOpen(true)}
        deleteInvalidLoading={deleteInvalidLoading}
        onDeleteInvalid={handleDeleteInvalid}
        onBatchDelete={handleBatchDelete}
        onOpenImport={() => setImportModalOpen(true)}
        onExportCsv={exportCsv}
        onOpenAdd={() => setAddModalOpen(true)}
        loading={loading}
        onRefresh={load}
        onOpenRegister={() => {
          clearTaskModalStorage()
          setTaskModalMode('register')
          setTaskModalAccount(null)
          setTaskId(null)
          setTaskSnapshot(null)
          setRegisterModalOpen(true)
        }}
        statusSyncMenuItems={statusSyncMenuItems}
        onStatusSyncClick={({ key }) => {
          const [kind, scope] = String(key).split(':') as ['probe' | 'remote' | 'sub2api', 'selected' | 'all']
          void handleBatchStatusSync(kind, scope)
        }}
        statusSyncLoading={statusSyncLoading}
        resumeAuthMenuItems={resumeAuthMenuItems}
        onResumeAuthClick={({ key }) => {
          const scope = String(key) === 'selected' ? 'selected' : 'filtered'
          void handleBatchResumeSubscriptionAuth(scope)
        }}
        resumeAuthLoading={batchResumeAuthLoading}
        backfillMenuItems={backfillMenuItems}
        onBackfillClick={({ key }) => {
          const [destination, scope] = String(key).split(':') as ['cliproxyapi' | 'sub2api', 'selected' | 'pending']
          Modal.confirm({
            title:
              scope === 'selected'
                ? `确认补传所选 ${selectedRowKeys.length} 个账号到 ${destination === 'sub2api' ? 'Sub2API' : 'CLIProxyAPI'}？`
                : `确认处理当前筛选范围内 ${getPendingBackfillCount(destination)} 个 ${destination === 'sub2api' ? 'Sub2API' : 'CLIProxyAPI'} 待补传账号？`,
            onOk: () => handleBackfill(destination, scope),
          })
        }}
        backfillLoading={backfillLoading}
      />

      {currentPlatform === 'chatgpt' && accounts.length > 0 && (
        <Sub2ApiOverviewPanel
          accountsCount={accounts.length}
          overview={sub2apiOverview}
          syncing={false}
          statusSyncLoading={statusSyncLoading}
          uploadLoading={sub2apiOverviewUploading}
          uploadDisabled={sub2apiOverviewUploadDisabled}
          pendingCount={sub2apiOverviewPendingCount}
          scope={sub2apiOverviewBackfillScope}
          selectedCount={selectedRowKeys.length}
          onRefresh={() => handleBatchStatusSync('sub2api', 'all')}
          onUpload={() => handleBackfill('sub2api', sub2apiOverviewBackfillScope)}
        />
      )}

      <AccountsTable
        columns={columns}
        accounts={accounts}
        loading={loading}
        total={total}
        currentPage={currentPage}
        pageSize={ACCOUNTS_PAGE_SIZE}
        onPageChange={setCurrentPage}
        selectedRowKeys={selectedRowKeys}
        setSelectedRowKeys={setSelectedRowKeys}
        isChatgptPlatform={isChatgptPlatform}
        onOpenDetail={(record) => {
          setDetailAccount(record)
          setDetailModalOpen(true)
        }}
      />

      <BatchGopayWorkbench
        open={batchGopayOpen}
        onClose={() => {
          setBatchGopayOpen(false)
          setBatchGopayPhoneCountryCode('86')
          setBatchGopayPhoneNumber('')
          setBatchGopayPhoneSaving(false)
        }}
        token={token}
        items={batchGopayItems}
        phones={batchGopayPhones}
        loading={batchGopayLoading}
        phoneSaving={batchGopayPhoneSaving}
        started={batchGopayStarted}
        roundInterval={batchGopayRoundInterval}
        otpAutoResendDelay={batchGopayOtpAutoResendDelay}
        otpDelaySaving={batchGopayOtpDelaySaving}
        nextRoundAt={batchGopayNextRoundAt}
        phoneCountryCode={batchGopayPhoneCountryCode}
        phoneNumber={batchGopayPhoneNumber}
        onPhoneCountryCodeChange={(value) => setBatchGopayPhoneCountryCode(normalizeGopayPhonePart(value))}
        onPhoneNumberChange={(value) => setBatchGopayPhoneNumber(normalizeGopayPhonePart(value))}
        onSaveOtpDelay={() => saveBatchGopayOtpAutoResendDelay(batchGopayOtpAutoResendDelay)}
        onRefreshConfig={loadGopayBatchConfig}
        onStart={startBatchGopay}
        onCancelAll={cancelBatchGopayAll}
        onAddPhone={addBatchGopayPhoneToPool}
        onMovePhone={moveBatchGopayPhone}
        onDeletePhone={deleteBatchGopayPhone}
        onRoundIntervalChange={(value) => setBatchGopayRoundInterval(Number(value || 0))}
        onOtpAutoResendDelayChange={(value) => setBatchGopayOtpAutoResendDelay(value)}
        formatGopayPhoneLabel={formatGopayPhoneLabel}
        renderBatchGopayItem={renderBatchGopayItem}
        normalizeGopayOtpAutoResendDelay={normalizeGopayOtpAutoResendDelay}
        activePhaseMatcher={(item) => Boolean(item.snapshot?.session_id && GOPAY_ACTIVE_PHASES.has(String(item.snapshot?.phase || '')))}
      />

      <PendingInvitesModal
        open={businessDeferredModalOpen}
        onClose={() => {
          setBusinessDeferredModalOpen(false)
          setSelectedPendingInviteRowKeys([])
        }}
        loading={pendingBusinessInvitesLoading}
        items={pendingBusinessInvites}
        selectedRowKeys={selectedPendingInviteRowKeys}
        onSelectedRowKeysChange={setSelectedPendingInviteRowKeys}
        activatingAll={activatingAllPendingInvites}
        activatingId={activatingPendingInviteId}
        abandoningId={abandoningPendingInviteId}
        onRefresh={async () => {
          await pendingInvitesQuery.refetch()
        }}
        onActivateSelected={handleActivateSelectedPendingInvites}
        onActivateAll={handleActivateAllPendingInvites}
        onActivateOne={handleActivatePendingInvite}
        onAbandonOne={handleAbandonPendingInvite}
        pendingActivationKindMeta={pendingActivationKindMeta}
        pendingInviteStatusMeta={pendingInviteStatusMeta}
        formatSyncTime={formatSyncTime}
      />

      <RegisterTaskModal
        open={registerModalOpen}
        currentPlatform={currentPlatform}
        taskModalMode={taskModalMode}
        taskModalAccount={taskModalAccount}
        taskId={taskId}
        taskSnapshot={taskSnapshot}
        registerForm={registerForm}
        registerMailProvider={registerMailProvider}
        chatgptRegistrationMode={chatgptRegistrationMode}
        setChatgptRegistrationMode={setChatgptRegistrationMode}
        registerLoading={registerLoading}
        registerSettingsSaving={registerSettingsSaving}
        onClose={() => {
          clearTaskModalStorage()
          setRegisterModalOpen(false)
          setTaskId(null)
          setTaskSnapshot(null)
          setTaskModalMode('register')
          setTaskModalAccount(null)
          registerForm.resetFields()
        }}
        onSaveRegisterSettings={handleSaveRegisterSettings}
        onRegister={handleRegister}
        onTaskDone={() => {
          clearTaskModalStorage()
          load()
          pendingInvitesQuery.refetch()
        }}
      />

      <AddAccountModal
        open={addModalOpen}
        addForm={addForm}
        onClose={() => {
          setAddModalOpen(false)
          addForm.resetFields()
        }}
        onSubmit={handleAdd}
      />

      <ImportAccountsModal
        open={importModalOpen}
        importLoading={importLoading}
        importText={importText}
        onClose={() => {
          setImportModalOpen(false)
          setImportText('')
        }}
        onSubmit={handleImport}
        onImportTextChange={setImportText}
      />

      <Suspense fallback={null}>
        <AccountActionSurface
        account={actionAccount}
        open={actionSurfaceOpen && Boolean(actionAccount)}
        showShell={false}
        onClose={() => {
          setActionSurfaceOpen(false)
          setActionAccount(null)
          setActionSurfaceInitialActionId(null)
          setActionSurfaceInitialActionMode('dialog')
        }}
        onRefresh={load}
        onOpenDetail={(record) => {
          setDetailAccount(record)
          setDetailModalOpen(true)
        }}
        actionsLoading={platformActionsLoading}
        actions={platformActions}
        onEnsureActionsLoaded={ensurePlatformActionsLoaded}
        initialActionId={actionSurfaceInitialActionId}
        initialActionMode={actionSurfaceInitialActionMode}
        onInitialActionHandled={() => setActionSurfaceInitialActionId(null)}
        onResumeAuthTask={handleResumeSubscriptionAuth}
        authStateMeta={authStateMeta}
        planMeta={planMeta}
          codexStateMeta={codexStateMeta}
          formatSyncTime={formatSyncTime}
        />
      </Suspense>

      <AccountDetailModal
        open={detailModalOpen}
        onClose={() => {
          setDetailModalOpen(false)
          setDetailAccount(null)
        }}
        onSave={handleDetailSave}
        currentAccount={detailAccount}
        detailForm={detailForm}
        token={token}
        importingTeamAccountId={importingTeamAccountId}
        onImportAccountToTeam={handleImportAccountToTeam}
        formatSyncTime={formatSyncTime}
        getRefreshToken={getRefreshToken}
        canImportAccountToTeam={canImportAccountToTeam}
        authStateMeta={authStateMeta}
        planMeta={planMeta}
        codexStateMeta={codexStateMeta}
      />
    </div>
  )
}
