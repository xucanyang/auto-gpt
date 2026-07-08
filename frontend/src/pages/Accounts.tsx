import { lazy, Suspense, useEffect, useState, useCallback, useMemo, useRef } from 'react'
import type { CSSProperties } from 'react'
import { FilterPresetBar } from '../features/accounts/components/FilterPresetBar'
import { SelectedAccountsSummary } from '../features/accounts/components/SelectedAccountsSummary'
import {
  Button,
  Checkbox,
  Dropdown,
  Input,
  InputNumber,
  Tag,
  Space,
  Modal,
  Form,
  Select,
  message,
  Typography,
  Alert,
  Popconfirm,
  theme,
  Grid,
  Segmented,
  Radio,
  Steps,
  Switch,
  Progress,
} from 'antd'
import type { CheckboxOptionType } from 'antd/es/checkbox/Group'
import type { MenuProps } from 'antd'
import {
  CopyOutlined,
  DeleteOutlined,
  DownOutlined,
  EditOutlined,
  LinkOutlined,
  MoreOutlined,
  PlusOutlined,
  UploadOutlined,
  SyncOutlined,
  SettingOutlined,
  SaveOutlined,
} from '@ant-design/icons'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import { AddAccountModal } from '@/features/accounts/components/AddAccountModal'
import { AccountsTable } from '@/features/accounts/components/AccountsTable'
import { AccountDetailModal } from '@/features/accounts/components/AccountDetailModal'
import { AccountsToolbar } from '@/features/accounts/components/AccountsToolbar'
import { BatchGopayWorkbench } from '@/features/accounts/components/BatchGopayWorkbench'
import { ImportAccountsModal } from '@/features/accounts/components/ImportAccountsModal'
import { PendingInvitesModal } from '@/features/accounts/components/PendingInvitesModal'
import { useAccountDetailQuery } from '@/features/accounts/hooks/useAccountDetailQuery'
import { useActiveTasksQuery } from '@/features/accounts/hooks/useActiveTasksQuery'
import { RegisterTaskModal } from '@/features/auth/components/RegisterTaskModal'
import { useAccountsQuery } from '@/features/accounts/hooks/useAccountsQuery'
import { usePendingInvitesQuery } from '@/features/accounts/hooks/usePendingInvitesQuery'
import { usePersistentChatGPTRegistrationMode } from '@/hooks/usePersistentChatGPTRegistrationMode'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import { buildChatGPTRegistrationRequestAdapter } from '@/lib/chatgptRegistrationRequestAdapter'
import { CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN } from '@/lib/chatgptRegistrationMode'
import { buildChatGPTK12Payload } from '@/lib/chatgptK12Config'
import {
  DEFAULT_GOPAY_PHONE_COUNTRY_CODE,
  normalizeGopayPhonePart,
  normalizeGopayRecognizedCountryCodes,
  splitGopayPhoneInput,
} from '@/lib/gopayPhone'
import { apiFetch } from '@/lib/utils'
import { buildTaskProxyPayload, saveTaskProxySettingsToConfig, taskProxySettingsFromConfig, validateTaskProxySettings } from '@/lib/taskProxySettings'
import { normalizeExecutorForPlatform } from '@/lib/platformExecutorOptions'

const { Text } = Typography

const AccountActionSurface = lazy(() =>
  import('@/features/accounts/components/AccountActionSurface').then((module) => ({
    default: module.AccountActionSurface,
  })),
)

const GOPAY_ACTIVE_PHASES = new Set(['created', 'starting', 'waiting_otp', 'waiting_link_pin', 'waiting_payment_pin', 'verifying'])
const TASK_MODAL_STORAGE_KEY = 'auto-chatgpt.accounts.task-modal.current-task'
const ACCOUNT_COLUMN_VISIBILITY_STORAGE_KEY = 'auto-chatgpt.accounts.visible-columns.v2'
const PHONE_BINDING_SETTINGS_STORAGE_KEY = 'auto-chatgpt.accounts.phone-binding-settings.v1'
const BAXIGPT_CDK_SETTINGS_STORAGE_KEY = 'auto-chatgpt.accounts.baxigpt-cdk-settings.v1'
const PAYPAL_BINDING_SETTINGS_STORAGE_KEY = 'auto-chatgpt.accounts.paypal-binding-settings.v1'

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
const DEFAULT_PAYMENT_LINK_FORMAT = 'long_hosted'
const PAYMENT_LINK_FORMAT_OPTIONS = [
  { label: '长支付链接', value: 'long_hosted' },
  { label: '短连接路径', value: 'short_chatgpt' },
]
const DEFAULT_GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS = 120
const ACCOUNTS_PAGE_SIZE_STORAGE_KEY = 'auto-chatgpt.accounts.page-size.v1'
const DEFAULT_ACCOUNTS_PAGE_SIZE = 20
const ACCOUNT_PAGE_SIZE_OPTIONS = [10, 20, 50]
const EMPTY_LIST: any[] = []
const SUBSCRIPTION_EXPIRY_SORT_FIELD = 'subscription_active_until'

type PhonePoolMode = 'normal' | 'prefix_limited' | 'prefix_sample'
type PhonePoolPrefixStatus = 'available' | 'unavailable' | 'temporary' | 'exhausted'
type PhonePoolPrefixItem = {
  prefix: string
  status: PhonePoolPrefixStatus | string
  total?: number
  available_count?: number
  remaining_capacity?: number
  rejected_count?: number
  bind_limit_count?: number
  [key: string]: unknown
}

type PhonePoolPrefixGroup = {
  key: PhonePoolPrefixStatus
  label: string
  color: string
  items: PhonePoolPrefixItem[]
}

function loadAccountsPageSize() {
  if (typeof window === 'undefined') return DEFAULT_ACCOUNTS_PAGE_SIZE
  const value = Number(window.localStorage.getItem(ACCOUNTS_PAGE_SIZE_STORAGE_KEY) || '')
  return ACCOUNT_PAGE_SIZE_OPTIONS.includes(value) ? value : DEFAULT_ACCOUNTS_PAGE_SIZE
}

const DEFAULT_PHONE_BINDING_SETTINGS = {
  use_pool: true,
  phone_pool_mode: 'normal' as PhonePoolMode,
  selected_prefixes: [] as string[],
  prefix_sample_enabled: false,
  prefix_sample_size: 1,
  prefix_sample_filter: 'all',
  prefix_sms_probe_only: false,
  timeout_seconds: 180,
  poll_interval_seconds: 5,
  max_resend_attempts: 0,
  resend_interval_seconds: 30,
  account_interval_seconds: 60,
  reuse_phone_until_unusable: false,
  proxy_mode: 'pool',
  proxy: '',
  proxy_country_code: '',
  proxy_failover: true,
  proxy_max_candidates: 10,
  proxy_min_score: 50,
}

const DEFAULT_BAXIGPT_CDK_SETTINGS = {
  use_pool: true,
  precheck: true,
  failure_continue: true,
  submit_interval_seconds: 5,
  auto_poll_status: true,
  status_poll_interval_seconds: 5,
  status_poll_timeout_seconds: 1800,
}

const DEFAULT_PAYPAL_BINDING_SETTINGS = {
  base_url: 'https://plus.iceaix.com',
  proxy: '',
  proxy_jp: '',
  phone: '',
  paypal_email: '',
  sms_api: '',
  sms_api_test_mode: false,
  otp_timeout: 180,
  pplink_retry: 3,
  timeout: 30,
  event_timeout: 60,
  account_interval_seconds: 0,
  failure_continue: true,
}

const PAYPAL_BINDING_ALLOWED_SUBSCRIPTION_TYPES = new Set(['free'])
const PAYPAL_BINDING_ALLOWED_ACCOUNT_VALIDITY = new Set(['valid'])
const PAYPAL_BINDING_EMPTY_FILTER = '__paypal_no_match__'

type PhoneBindingSettings = typeof DEFAULT_PHONE_BINDING_SETTINGS
type BaxiGptCdkSettings = typeof DEFAULT_BAXIGPT_CDK_SETTINGS
type PaypalBindingSettings = typeof DEFAULT_PAYPAL_BINDING_SETTINGS
type PhonePoolSummary = {
  total?: number
  available?: number
  remaining_capacity?: number
  rate_limited?: number
  unavailable?: number
  cannot_send?: number
  cooldown?: number
  exhausted?: number
  disabled?: number
  available_prefix_count?: number
  available_prefix_sample_1?: number
  available_prefix_sample_2?: number
  prefix_sample_prefix_count?: number
  prefix_sample_phone_count?: number
  prefix_sample_count_1?: number
  prefix_sample_count_2?: number
  rejected_prefix_count?: number
  rejected_prefix_sample_1?: number
  rejected_prefix_sample_2?: number
  available_prefixes?: Array<Record<string, unknown>>
  rejected_prefixes?: Array<Record<string, unknown>>
  exhausted_prefix_count?: number
  exhausted_prefixes?: Array<Record<string, unknown>>
  temporary_prefix_count?: number
  temporary_prefixes?: Array<Record<string, unknown>>
  prefix_health?: {
    available?: Array<Record<string, unknown>>
    unavailable?: Array<Record<string, unknown>>
    exhausted?: Array<Record<string, unknown>>
    temporary?: Array<Record<string, unknown>>
  }
}

type BaxiGptCdkPoolSummary = {
  total?: number
  available?: number
  reserved?: number
  submitted?: number
  processing?: number
  paid?: number
  failed?: number
  disabled?: number
}

type BaxiGptCdkPoolItem = {
  id: number
  code_value?: string
  code_masked?: string
  label?: string
  status?: string
  code_info_remaining?: number
  code_info_total?: number
  last_error_message?: string
  last_checked_at?: string | null
  updated_at?: string | null
}

type SubscriptionExpirySortOrder = '' | 'asc' | 'desc'

type AccountColumnKey =
  | 'manually_used'
  | 'phone_binding'
  | 'password'
  | 'auth_type'
  | 'status'
  | 'subscription_type'
  | 'subscription_active_until'
  | 'account_validity'
  | 'idea_submit_status'
  | 'codex_usage'
  | 'sub2api_state'
  | 'sub2api_upload_record'
  | 'oaipay_state'
  | 'oaipay_upload_record'
  | 'created_at'

const ACCOUNT_COLUMN_OPTIONS: Array<{ value: AccountColumnKey; text: string; chatgptOnly?: boolean }> = [
  { value: 'manually_used', text: '使用状态' },
  { value: 'phone_binding', text: '手机号/API', chatgptOnly: true },
  { value: 'password', text: '密码' },
  { value: 'auth_type', text: '认证材料', chatgptOnly: true },
  { value: 'status', text: '业务状态' },
  { value: 'subscription_type', text: '当前订阅', chatgptOnly: true },
  { value: 'subscription_active_until', text: '订阅到期', chatgptOnly: true },
  { value: 'account_validity', text: '认证状态', chatgptOnly: true },
  { value: 'idea_submit_status', text: 'Idea提交', chatgptOnly: true },
  { value: 'codex_usage', text: 'Codex用量', chatgptOnly: true },
  { value: 'sub2api_state', text: 'Sub2API', chatgptOnly: true },
  { value: 'sub2api_upload_record', text: 'Sub2API上传', chatgptOnly: true },
  { value: 'oaipay_state', text: 'OAIPay', chatgptOnly: true },
  { value: 'oaipay_upload_record', text: 'OAIPay上传', chatgptOnly: true },
  { value: 'created_at', text: '注册时间' },
]

const DEFAULT_VISIBLE_ACCOUNT_COLUMNS: AccountColumnKey[] = [
  'manually_used',
  'phone_binding',
  'auth_type',
  'status',
  'subscription_type',
  'subscription_active_until',
  'account_validity',
  'idea_submit_status',
  'codex_usage',
  'sub2api_state',
  'sub2api_upload_record',
  'oaipay_state',
  'oaipay_upload_record',
  'created_at',
]

const ACCOUNT_COLUMN_OPTION_KEYS = new Set<AccountColumnKey>(ACCOUNT_COLUMN_OPTIONS.map((item) => item.value))

type AccountColumnFilters = {
  email: string
  status: string[]
  manuallyUsed: string[]
  authType: string[]
  subscriptionType: string[]
  accountValidity: string[]
  codexState: string[]
  sub2apiState: string[]
  oaipayState: string[]
  ideaSubmitState: string[]
}

const EMPTY_ACCOUNT_FILTERS: AccountColumnFilters = {
  email: '',
  status: [],
  manuallyUsed: [],
  authType: [],
  subscriptionType: [],
  accountValidity: [],
  codexState: [],
  sub2apiState: [],
  oaipayState: [],
  ideaSubmitState: [],
}

export type AccountFilterPresetFilters = {
  search?: string
  status?: string[]
  columnFilters?: Partial<Record<keyof AccountColumnFilters, string[] | string>>
  sortOrder?: SubscriptionExpirySortOrder
  pageSize?: number
}

export type AccountFilterPreset = {
  id: string
  name: string
  description?: string
  summary?: string
  filters: AccountFilterPresetFilters
  pinned?: boolean
  built_in?: boolean
  created_at?: string
  updated_at?: string
}

const STATUS_FILTER_OPTIONS = [
  { value: 'registered', text: '已注册' },
  { value: 'pending_payment', text: '待支付' },
  { value: 'payment_failed', text: '支付失败' },
  { value: 'rate_limited', text: '限流' },
  { value: 'trial', text: '试用中' },
  { value: 'subscribed', text: '已订阅' },
  { value: 'expired', text: '已过期' },
  { value: 'invalid', text: '已失效' },
]

const MANUAL_USE_FILTER_OPTIONS = [
  { value: 'true', text: '已使用' },
  { value: 'false', text: '未使用' },
]

const AUTH_TYPE_FILTER_OPTIONS = [
  { value: 'refresh_token', text: '有 RT' },
  { value: 'access_token_only', text: '仅 AT' },
  { value: 'unknown', text: '无认证材料' },
]

const SUBSCRIPTION_TYPE_FILTER_OPTIONS = [
  { value: 'free', text: 'Free' },
  { value: 'plus', text: 'Plus' },
  { value: 'team', text: 'Team / Business' },
  { value: 'pro', text: 'Pro' },
  { value: 'enterprise', text: 'Enterprise' },
  { value: 'unknown', text: '未知 / 待刷新' },
]

const ACCOUNT_VALIDITY_FILTER_OPTIONS = [
  { value: 'valid', text: '认证通过' },
  { value: 'invalid', text: '认证失效' },
  { value: 'refresh_failed', text: '刷新失败' },
  { value: 'not_checked', text: '未验证' },
]

const CODEX_STATE_FILTER_OPTIONS = [
  { value: 'usable', text: '可用' },
  { value: 'quota_exhausted', text: '额度耗尽' },
  { value: 'account_deactivated', text: '已失效' },
  { value: 'refresh_token_invalidated', text: 'RT失效' },
  { value: 'access_token_invalidated', text: 'AT失效' },
  { value: 'unauthorized', text: '未授权' },
  { value: 'payment_required', text: '需付费/权限' },
]

const SUB2API_FILTER_OPTIONS = [
  { value: 'exists', text: '已存在' },
  { value: 'not_found', text: '未发现' },
  { value: 'cross_workspace_only', text: '其他工作区已存在' },
  { value: 'deleted_exact_match', text: '已删可重传' },
  { value: 'ambiguous', text: '多候选' },
  { value: 'unreachable', text: '不可达' },
  { value: 'unknown', text: '未同步' },
]

const OAIPAY_FILTER_OPTIONS = [
  { value: 'exists', text: '已存在' },
  { value: 'not_found', text: '未发现' },
  { value: 'cross_workspace_only', text: '其他工作区已存在' },
  { value: 'deleted_exact_match', text: '已删可重传' },
  { value: 'ambiguous', text: '多候选' },
  { value: 'unreachable', text: '不可达' },
  { value: 'unknown', text: '未同步' },
]

const IDEA_SUBMIT_FILTER_OPTIONS = [
  { value: 'unsubmitted', text: '未提交' },
  { value: 'unavailable', text: '不可用' },
  { value: 'submitting', text: '提交中' },
  { value: 'paid', text: '已开通' },
  { value: 'failed', text: '提交失败' },
]

const IDEA_SUBMIT_FILTER_VALUE_ALIASES: Record<string, string> = {
  available: 'unsubmitted',
  not_submitted: 'unsubmitted',
  pending_submit: 'unsubmitted',
  submitted: 'submitting',
  processing: 'submitting',
  pending: 'submitting',
  polling: 'submitting',
  success: 'paid',
  completed: 'paid',
  fail: 'failed',
  error: 'failed',
}

function normalizeIdeaSubmitFilterValue(value: unknown) {
  const text = String(value || '').trim().toLowerCase()
  return IDEA_SUBMIT_FILTER_VALUE_ALIASES[text] || text
}

const SUBSCRIPTION_EXPIRY_SORT_OPTIONS = [
  { value: 'asc', text: '到期最早' },
  { value: 'desc', text: '到期最晚' },
]

const ACCOUNT_FILTER_PRESET_COLUMN_KEYS: Array<keyof AccountColumnFilters> = [
  'email',
  'status',
  'manuallyUsed',
  'authType',
  'subscriptionType',
  'accountValidity',
  'codexState',
  'sub2apiState',
  'oaipayState',
  'ideaSubmitState',
]

function normalizePresetList(value: unknown): string[] {
  const rawItems = Array.isArray(value) ? value : String(value || '').split(',')
  const seen = new Set<string>()
  const items: string[] = []
  rawItems.forEach((item) => {
    const text = String(item || '').trim()
    if (!text || seen.has(text)) return
    seen.add(text)
    items.push(text)
  })
  return items
}

function cloneAccountColumnFilters(value?: Partial<Record<keyof AccountColumnFilters, unknown>>): AccountColumnFilters {
  const source = value && typeof value === 'object' ? value : {}
  const next: AccountColumnFilters = {
    email: '',
    status: [],
    manuallyUsed: [],
    authType: [],
    subscriptionType: [],
    accountValidity: [],
    codexState: [],
    sub2apiState: [],
    oaipayState: [],
    ideaSubmitState: [],
  }
  ACCOUNT_FILTER_PRESET_COLUMN_KEYS.forEach((key) => {
    if (key === 'email') {
      next.email = String(source.email || '').trim()
    } else {
      const values = normalizePresetList(source[key])
      ;(next[key] as string[]) = key === 'ideaSubmitState'
        ? values.reduce((acc, item) => {
            const normalized = normalizeIdeaSubmitFilterValue(item)
            if (normalized && !acc.includes(normalized)) acc.push(normalized)
            return acc
          }, [] as string[])
        : values
    }
  })
  return next
}

function normalizeAccountFilterPresetFilters(filters?: AccountFilterPresetFilters): Required<AccountFilterPresetFilters> & { columnFilters: AccountColumnFilters } {
  const source = filters && typeof filters === 'object' ? filters : {}
  const sourceColumnFilters = source.columnFilters && typeof source.columnFilters === 'object' ? source.columnFilters : {}
  const search = String(source.search || sourceColumnFilters.email || '').trim()
  const columnFilters = cloneAccountColumnFilters(sourceColumnFilters)
  columnFilters.email = search
  const status = normalizePresetList(source.status && source.status.length ? source.status : columnFilters.status)
  columnFilters.status = status
  const sortOrder = source.sortOrder === 'asc' || source.sortOrder === 'desc' ? source.sortOrder : ''
  const pageSize = ACCOUNT_PAGE_SIZE_OPTIONS.includes(Number(source.pageSize || 0))
    ? Number(source.pageSize)
    : DEFAULT_ACCOUNTS_PAGE_SIZE
  return {
    search,
    status,
    columnFilters,
    sortOrder,
    pageSize,
  }
}

function buildAccountFilterPresetFilters(
  search: string,
  columnFilters: AccountColumnFilters,
  sortOrder: SubscriptionExpirySortOrder,
  pageSize: number,
): AccountFilterPresetFilters {
  const normalizedColumnFilters = cloneAccountColumnFilters(columnFilters)
  const normalizedSearch = String(search || '').trim()
  normalizedColumnFilters.email = normalizedSearch
  const normalizedStatus = normalizePresetList(normalizedColumnFilters.status)
  normalizedColumnFilters.status = normalizedStatus
  return {
    search: normalizedSearch,
    status: normalizedStatus,
    columnFilters: normalizedColumnFilters,
    sortOrder,
    pageSize: ACCOUNT_PAGE_SIZE_OPTIONS.includes(pageSize) ? pageSize : DEFAULT_ACCOUNTS_PAGE_SIZE,
  }
}

function accountFilterPresetSignature(filters?: AccountFilterPresetFilters) {
  const normalized = normalizeAccountFilterPresetFilters(filters)
  return JSON.stringify({
    search: normalized.search,
    status: normalized.status,
    columnFilters: normalized.columnFilters,
    sortOrder: normalized.sortOrder,
    pageSize: normalized.pageSize,
  })
}

function labelForOption(options: Array<{ value: string; text: string }>, value: string) {
  return options.find((item) => item.value === value)?.text || value
}

function summarizePresetValues(options: Array<{ value: string; text: string }>, values: string[]) {
  if (values.length === 0) return ''
  return values.map((value) => labelForOption(options, value)).join('/')
}

export function buildAccountFilterPresetSummary(filters?: AccountFilterPresetFilters) {
  const normalized = normalizeAccountFilterPresetFilters(filters)
  const columnFilters = normalized.columnFilters
  const parts = [
    normalized.search ? `搜索：${normalized.search}` : '',
    summarizePresetValues(STATUS_FILTER_OPTIONS, columnFilters.status) ? `业务状态：${summarizePresetValues(STATUS_FILTER_OPTIONS, columnFilters.status)}` : '',
    summarizePresetValues(MANUAL_USE_FILTER_OPTIONS, columnFilters.manuallyUsed) ? `使用：${summarizePresetValues(MANUAL_USE_FILTER_OPTIONS, columnFilters.manuallyUsed)}` : '',
    summarizePresetValues(AUTH_TYPE_FILTER_OPTIONS, columnFilters.authType) ? `材料：${summarizePresetValues(AUTH_TYPE_FILTER_OPTIONS, columnFilters.authType)}` : '',
    summarizePresetValues(SUBSCRIPTION_TYPE_FILTER_OPTIONS, columnFilters.subscriptionType) ? `当前订阅：${summarizePresetValues(SUBSCRIPTION_TYPE_FILTER_OPTIONS, columnFilters.subscriptionType)}` : '',
    summarizePresetValues(ACCOUNT_VALIDITY_FILTER_OPTIONS, columnFilters.accountValidity) ? `认证状态：${summarizePresetValues(ACCOUNT_VALIDITY_FILTER_OPTIONS, columnFilters.accountValidity)}` : '',
    summarizePresetValues(SUB2API_FILTER_OPTIONS, columnFilters.sub2apiState) ? `Sub2API：${summarizePresetValues(SUB2API_FILTER_OPTIONS, columnFilters.sub2apiState)}` : '',
    summarizePresetValues(OAIPAY_FILTER_OPTIONS, columnFilters.oaipayState) ? `OAIPay：${summarizePresetValues(OAIPAY_FILTER_OPTIONS, columnFilters.oaipayState)}` : '',
    summarizePresetValues(IDEA_SUBMIT_FILTER_OPTIONS, columnFilters.ideaSubmitState) ? `Idea提交：${summarizePresetValues(IDEA_SUBMIT_FILTER_OPTIONS, columnFilters.ideaSubmitState)}` : '',
    normalized.sortOrder ? `到期：${labelForOption(SUBSCRIPTION_EXPIRY_SORT_OPTIONS, normalized.sortOrder)}` : '',
  ].filter(Boolean)
  return parts.length ? parts.join(' · ') : '无筛选条件'
}

function normalizeCheckoutCountry(value: unknown) {
  return String(value || DEFAULT_CHECKOUT_COUNTRY).trim().toUpperCase() || DEFAULT_CHECKOUT_COUNTRY
}

function normalizeCheckoutCurrency(value: unknown) {
  return String(value || DEFAULT_CHECKOUT_CURRENCY).trim().toUpperCase() || DEFAULT_CHECKOUT_CURRENCY
}

function normalizePaymentLinkFormat(value: unknown) {
  const normalized = String(value || '').trim().toLowerCase().replace(/-/g, '_')
  return normalized === 'short' || normalized === 'short_chatgpt' || normalized === 'chatgpt' || normalized === 'custom'
    ? 'short_chatgpt'
    : DEFAULT_PAYMENT_LINK_FORMAT
}

function paymentLinkFormatLabel(value: unknown) {
  return normalizePaymentLinkFormat(value) === 'short_chatgpt' ? '短连接路径' : '长支付链接'
}

function normalizeGopayOtpAutoResendDelay(value: unknown) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return DEFAULT_GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS
  return Math.max(0, Math.min(Math.trunc(parsed), 3600))
}

function parseMaybeJsonObject(value: unknown) {
  if (!value) return {}
  if (typeof value === 'object') return !Array.isArray(value) ? value as Record<string, any> : {}
  const text = String(value || '').trim()
  if (!text) return {}
  try {
    const parsed = JSON.parse(text)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
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
  api_expired_date?: string
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

function formatGopayPhoneExpiryLabel(phone: Partial<GopayPhoneCandidate>) {
  return String((phone as any).api_expired_date || (phone as any).apiExpiredDate || '').trim()
}

function normalizeGopayPhoneCandidate(value: any, index = 0): GopayPhoneCandidate | null {
  const phone_country_code = normalizeGopayPhonePart(value?.phone_country_code || value?.country_code || value?.code || DEFAULT_GOPAY_PHONE_COUNTRY_CODE)
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
    api_expired_date: String(value?.api_expired_date || value?.apiExpiredDate || '').trim(),
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

export const STATUS_COLORS: Record<string, string> = {
  registered: 'default',
  pending_payment: 'warning',
  payment_failed: 'error',
  rate_limited: 'warning',
  trial: 'success',
  subscribed: 'success',
  expired: 'warning',
  invalid: 'error',
}

const STATUS_LABELS: Record<string, string> = {
  registered: '已注册',
  pending_payment: '待支付',
  payment_failed: '支付失败',
  rate_limited: '限流',
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

function intWithDefault(value: unknown, fallback: number, min = 0) {
  const next = Number(value)
  if (!Number.isFinite(next)) return fallback
  return Math.max(Math.floor(next), min)
}

function normalizePhonePoolMode(value: unknown, raw?: Record<string, unknown>): PhonePoolMode {
  const mode = String(value || '').trim()
  if (mode === 'prefix_limited' || mode === 'prefix_sample' || mode === 'normal') {
    return mode
  }
  if (raw && Boolean(raw.prefix_bind_enabled)) return 'prefix_limited'
  if (raw && Boolean(raw.prefix_sample_enabled)) return 'prefix_sample'
  return 'normal'
}

function normalizeSelectedPrefixes(value: unknown): string[] {
  const rawValues = Array.isArray(value)
    ? value
    : typeof value === 'string'
      ? value.split(/[\s,，;；、]+/)
      : []
  const result: string[] = []
  const seen = new Set<string>()
  for (const raw of rawValues) {
    const prefix = String(raw ?? '').replace(/\D/g, '').slice(0, 4)
    if (prefix.length !== 4 || seen.has(prefix)) continue
    seen.add(prefix)
    result.push(prefix)
    if (result.length >= 500) break
  }
  return result
}

function normalizePhonePoolPrefixItem(raw: unknown, fallbackStatus: PhonePoolPrefixStatus): PhonePoolPrefixItem | null {
  const source = raw && typeof raw === 'object' ? raw as Record<string, unknown> : { prefix: raw }
  const prefix = String(source.prefix ?? source.prefix4 ?? source.phone_prefix ?? '').replace(/\D/g, '').slice(0, 4)
  if (prefix.length !== 4) return null
  const item: PhonePoolPrefixItem = {
    ...source,
    prefix,
    status: String(source.status || fallbackStatus),
  }
  for (const key of ['total', 'available_count', 'remaining_capacity', 'rejected_count', 'bind_limit_count']) {
    const value = Number(source[key])
    if (Number.isFinite(value)) {
      item[key] = value
    }
  }
  return item
}

function uniquePhonePoolPrefixItems(values: unknown, fallbackStatus: PhonePoolPrefixStatus): PhonePoolPrefixItem[] {
  const rawItems = Array.isArray(values) ? values : []
  const result: PhonePoolPrefixItem[] = []
  const seen = new Set<string>()
  for (const raw of rawItems) {
    const item = normalizePhonePoolPrefixItem(raw, fallbackStatus)
    if (!item || seen.has(item.prefix)) continue
    seen.add(item.prefix)
    result.push(item)
  }
  return result
}

function normalizePhoneBindingSettings(value: unknown): PhoneBindingSettings {
  const raw = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  const phonePoolMode = normalizePhonePoolMode(raw.phone_pool_mode, raw)
  return {
    use_pool: raw.use_pool === undefined ? DEFAULT_PHONE_BINDING_SETTINGS.use_pool : Boolean(raw.use_pool),
    phone_pool_mode: phonePoolMode,
    selected_prefixes: normalizeSelectedPrefixes(raw.selected_prefixes),
    prefix_sample_enabled: phonePoolMode === 'prefix_sample',
    prefix_sample_size: intWithDefault(raw.prefix_sample_size, DEFAULT_PHONE_BINDING_SETTINGS.prefix_sample_size, 1) === 2 ? 2 : 1,
    prefix_sample_filter: (() => {
      const value = String(raw.prefix_sample_filter || DEFAULT_PHONE_BINDING_SETTINGS.prefix_sample_filter)
      if (value === 'available' || value === 'available_only' || value === 'healthy' || value === 'healthy_only') return 'available'
      if (value === 'rejected' || value === 'rejected_only' || value === 'unavailable' || value === 'unavailable_only') return 'rejected'
      return 'all'
    })(),
    prefix_sms_probe_only: Boolean(raw.prefix_sms_probe_only || raw.sms_probe_only),
    timeout_seconds: intWithDefault(raw.timeout_seconds, DEFAULT_PHONE_BINDING_SETTINGS.timeout_seconds, 10),
    poll_interval_seconds: intWithDefault(raw.poll_interval_seconds, DEFAULT_PHONE_BINDING_SETTINGS.poll_interval_seconds, 1),
    max_resend_attempts: intWithDefault(raw.max_resend_attempts, DEFAULT_PHONE_BINDING_SETTINGS.max_resend_attempts, 0),
    resend_interval_seconds: intWithDefault(raw.resend_interval_seconds, DEFAULT_PHONE_BINDING_SETTINGS.resend_interval_seconds, 0),
    account_interval_seconds: intWithDefault(raw.account_interval_seconds, DEFAULT_PHONE_BINDING_SETTINGS.account_interval_seconds, 1),
    reuse_phone_until_unusable: Boolean(raw.reuse_phone_until_unusable),
    proxy_mode: (() => {
      const value = String(raw.proxy_mode || DEFAULT_PHONE_BINDING_SETTINGS.proxy_mode).trim()
      return value === 'direct' || value === 'specified' || value === 'pool' || value === 'dynamic' ? value : 'pool'
    })(),
    proxy: String(raw.proxy || ''),
    proxy_country_code: String(raw.proxy_country_code || '').trim().toUpperCase(),
    proxy_failover: raw.proxy_failover === undefined ? DEFAULT_PHONE_BINDING_SETTINGS.proxy_failover : Boolean(raw.proxy_failover),
    proxy_max_candidates: intWithDefault(raw.proxy_max_candidates, DEFAULT_PHONE_BINDING_SETTINGS.proxy_max_candidates, 1),
    proxy_min_score: intWithDefault(raw.proxy_min_score, DEFAULT_PHONE_BINDING_SETTINGS.proxy_min_score, 0),
  }
}

function loadPhoneBindingSettings() {
  if (typeof window === 'undefined') return { ...DEFAULT_PHONE_BINDING_SETTINGS }
  try {
    const raw = window.localStorage.getItem(PHONE_BINDING_SETTINGS_STORAGE_KEY)
    if (!raw) return { ...DEFAULT_PHONE_BINDING_SETTINGS }
    return normalizePhoneBindingSettings(JSON.parse(raw))
  } catch {
    return { ...DEFAULT_PHONE_BINDING_SETTINGS }
  }
}

function savePhoneBindingSettings(values: Record<string, unknown>) {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(PHONE_BINDING_SETTINGS_STORAGE_KEY, JSON.stringify(normalizePhoneBindingSettings(values)))
}

function normalizeBaxiGptCdkSettings(value: unknown): BaxiGptCdkSettings {
  const raw = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  return {
    use_pool: raw.use_pool === undefined ? DEFAULT_BAXIGPT_CDK_SETTINGS.use_pool : Boolean(raw.use_pool),
    precheck: raw.precheck === undefined ? DEFAULT_BAXIGPT_CDK_SETTINGS.precheck : Boolean(raw.precheck),
    failure_continue: raw.failure_continue === undefined ? DEFAULT_BAXIGPT_CDK_SETTINGS.failure_continue : Boolean(raw.failure_continue),
    submit_interval_seconds: intWithDefault(raw.submit_interval_seconds, DEFAULT_BAXIGPT_CDK_SETTINGS.submit_interval_seconds, 0),
    auto_poll_status: raw.auto_poll_status === undefined ? DEFAULT_BAXIGPT_CDK_SETTINGS.auto_poll_status : Boolean(raw.auto_poll_status),
    status_poll_interval_seconds: intWithDefault(raw.status_poll_interval_seconds, DEFAULT_BAXIGPT_CDK_SETTINGS.status_poll_interval_seconds, 1),
    status_poll_timeout_seconds: intWithDefault(raw.status_poll_timeout_seconds, DEFAULT_BAXIGPT_CDK_SETTINGS.status_poll_timeout_seconds, 1800),
  }
}

function loadBaxiGptCdkSettings() {
  if (typeof window === 'undefined') return { ...DEFAULT_BAXIGPT_CDK_SETTINGS }
  try {
    const raw = window.localStorage.getItem(BAXIGPT_CDK_SETTINGS_STORAGE_KEY)
    if (!raw) return { ...DEFAULT_BAXIGPT_CDK_SETTINGS }
    return normalizeBaxiGptCdkSettings(JSON.parse(raw))
  } catch {
    return { ...DEFAULT_BAXIGPT_CDK_SETTINGS }
  }
}

function saveBaxiGptCdkSettings(values: Record<string, unknown>) {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(BAXIGPT_CDK_SETTINGS_STORAGE_KEY, JSON.stringify(normalizeBaxiGptCdkSettings(values)))
}

function normalizeBaxiCdkIdList(value: unknown): number[] {
  const rawItems = Array.isArray(value) ? value : String(value || '').split(',')
  const ids: number[] = []
  const seen = new Set<number>()
  rawItems.forEach((item) => {
    const id = Number(item)
    if (Number.isFinite(id) && id > 0 && !seen.has(id)) {
      seen.add(id)
      ids.push(id)
    }
  })
  return ids
}

function baxiCdkRemainingValue(item: Partial<BaxiGptCdkPoolItem>) {
  const value = Number(item.code_info_remaining || 0)
  return Number.isFinite(value) ? Math.max(value, 0) : 0
}

function baxiCdkTotalValue(item: Partial<BaxiGptCdkPoolItem>) {
  const value = Number(item.code_info_total || 0)
  return Number.isFinite(value) ? Math.max(value, 0) : 0
}

function baxiCdkSubmitCapacity(item: Partial<BaxiGptCdkPoolItem>) {
  const remaining = baxiCdkRemainingValue(item)
  return remaining > 0 ? remaining : 1
}

function baxiCdkQuotaLabel(item: Partial<BaxiGptCdkPoolItem>) {
  const remaining = baxiCdkRemainingValue(item)
  const total = baxiCdkTotalValue(item)
  return total > 0 ? `剩余 ${remaining}/${total}` : '剩余额度未查'
}

function normalizePaypalBindingSettings(value: unknown): PaypalBindingSettings {
  const raw = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  return {
    base_url: String(raw.base_url || DEFAULT_PAYPAL_BINDING_SETTINGS.base_url).trim() || DEFAULT_PAYPAL_BINDING_SETTINGS.base_url,
    proxy: String(raw.proxy || '').trim(),
    proxy_jp: String(raw.proxy_jp || '').trim(),
    phone: String(raw.phone || '').trim(),
    paypal_email: String(raw.paypal_email || '').trim(),
    sms_api: String(raw.sms_api || '').trim(),
    sms_api_test_mode: raw.sms_api_test_mode === undefined ? DEFAULT_PAYPAL_BINDING_SETTINGS.sms_api_test_mode : Boolean(raw.sms_api_test_mode),
    otp_timeout: intWithDefault(raw.otp_timeout, DEFAULT_PAYPAL_BINDING_SETTINGS.otp_timeout, 30),
    pplink_retry: intWithDefault(raw.pplink_retry, DEFAULT_PAYPAL_BINDING_SETTINGS.pplink_retry, 1),
    timeout: intWithDefault(raw.timeout, DEFAULT_PAYPAL_BINDING_SETTINGS.timeout, 5),
    event_timeout: intWithDefault(raw.event_timeout, DEFAULT_PAYPAL_BINDING_SETTINGS.event_timeout, 10),
    account_interval_seconds: intWithDefault(raw.account_interval_seconds, DEFAULT_PAYPAL_BINDING_SETTINGS.account_interval_seconds, 0),
    failure_continue: raw.failure_continue === undefined ? DEFAULT_PAYPAL_BINDING_SETTINGS.failure_continue : Boolean(raw.failure_continue),
  }
}

function loadPaypalBindingSettings() {
  if (typeof window === 'undefined') return { ...DEFAULT_PAYPAL_BINDING_SETTINGS }
  try {
    const raw = window.localStorage.getItem(PAYPAL_BINDING_SETTINGS_STORAGE_KEY)
    if (!raw) return { ...DEFAULT_PAYPAL_BINDING_SETTINGS }
    return normalizePaypalBindingSettings(JSON.parse(raw))
  } catch {
    return { ...DEFAULT_PAYPAL_BINDING_SETTINGS }
  }
}

function savePaypalBindingSettings(values: Record<string, unknown>) {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(PAYPAL_BINDING_SETTINGS_STORAGE_KEY, JSON.stringify(normalizePaypalBindingSettings(values)))
}

function splitCommaFilterValues(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value
      .flatMap((item) => splitCommaFilterValues(item))
      .filter(Boolean)
  }
  return String(value || '')
    .split(',')
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean)
}

function constrainCommaFilterToAllowed(value: unknown, allowed: Set<string>): string {
  const current = splitCommaFilterValues(value)
  if (current.length === 0) return Array.from(allowed).join(',')
  const constrained = current.filter((item) => allowed.has(item))
  return constrained.length > 0 ? constrained.join(',') : PAYPAL_BINDING_EMPTY_FILTER
}

function applyPaypalBindingEligibilityFilters(body: Record<string, unknown>): void {
  body.account_validity = constrainCommaFilterToAllowed(
    body.account_validity,
    PAYPAL_BINDING_ALLOWED_ACCOUNT_VALIDITY,
  )
  body.subscription_type = constrainCommaFilterToAllowed(
    body.subscription_type,
    PAYPAL_BINDING_ALLOWED_SUBSCRIPTION_TYPES,
  )
}

function normalizeVisibleAccountColumns(value: unknown): AccountColumnKey[] {
  if (!Array.isArray(value)) return [...DEFAULT_VISIBLE_ACCOUNT_COLUMNS]
  const normalized = value
    .map((item) => String(item || '').trim())
    .filter((item): item is AccountColumnKey => ACCOUNT_COLUMN_OPTION_KEYS.has(item as AccountColumnKey))
  return Array.from(new Set(normalized))
}

function loadVisibleAccountColumnKeys() {
  if (typeof window === 'undefined') return [...DEFAULT_VISIBLE_ACCOUNT_COLUMNS]
  try {
    const raw = window.localStorage.getItem(ACCOUNT_COLUMN_VISIBILITY_STORAGE_KEY)
    if (!raw) return [...DEFAULT_VISIBLE_ACCOUNT_COLUMNS]
    return normalizeVisibleAccountColumns(JSON.parse(raw))
  } catch {
    return [...DEFAULT_VISIBLE_ACCOUNT_COLUMNS]
  }
}

function saveVisibleAccountColumnKeys(keys: AccountColumnKey[]) {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(ACCOUNT_COLUMN_VISIBILITY_STORAGE_KEY, JSON.stringify(normalizeVisibleAccountColumns(keys)))
}

function normalizeAccount(account: any) {
  const parsedExtra = parseExtraJson(account.extra_json)
  const extra = account.extra && typeof account.extra === 'object'
    ? { ...parsedExtra, ...account.extra }
    : parsedExtra
  const phoneBinding = account.phone_binding && typeof account.phone_binding === 'object'
    ? account.phone_binding
    : extra.chatgpt_phone_binding && typeof extra.chatgpt_phone_binding === 'object'
      ? extra.chatgpt_phone_binding
      : {}
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
  const accountRateLimit = account.rate_limit && typeof account.rate_limit === 'object'
    ? account.rate_limit
    : {}
  const rateLimit = {
    started_at: account.rate_limit_started_at || accountRateLimit.started_at || extra.rate_limit_started_at || '',
    recover_at: account.rate_limit_recover_at || accountRateLimit.recover_at || extra.rate_limit_recover_at || '',
    previous_status: account.rate_limit_previous_status || accountRateLimit.previous_status || extra.rate_limit_previous_status || '',
    seconds_remaining: Number(accountRateLimit.seconds_remaining || 0),
  }
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
    phoneBinding,
    rateLimit,
    rate_limit: rateLimit,
    rate_limit_started_at: rateLimit.started_at,
    rate_limit_recover_at: rateLimit.recover_at,
    rate_limit_previous_status: rateLimit.previous_status,
    teamInviteSource,
    manuallyUsed: account.manually_used !== undefined ? Boolean(account.manually_used) : Boolean(extra.manually_used),
  }
}

function getPhoneBinding(record: any) {
  const binding = record?.phoneBinding && typeof record.phoneBinding === 'object'
    ? record.phoneBinding
    : record?.phone_binding && typeof record.phone_binding === 'object'
      ? record.phone_binding
      : record?.extra?.chatgpt_phone_binding && typeof record.extra.chatgpt_phone_binding === 'object'
        ? record.extra.chatgpt_phone_binding
        : {}
  return {
    phone: String(binding.phone || '').trim(),
    apiUrl: String(binding.api_url || binding.apiUrl || '').trim(),
    rawLine: String(binding.raw_line || binding.rawLine || '').trim(),
    boundAt: String(binding.bound_at || binding.boundAt || '').trim(),
    apiExpiredDate: String(binding.api_expired_date || binding.apiExpiredDate || '').trim(),
    codeTime: String(binding.code_time || binding.codeTime || '').trim(),
  }
}

function parseFlexibleDateValue(value?: string | number) {
  if (!value) return null
  if (typeof value === 'number' && Number.isFinite(value)) {
    const timestampMs = value > 1_000_000_000_000 ? value : value * 1000
    const date = new Date(timestampMs)
    return Number.isNaN(date.getTime()) ? null : date
  }
  const text = String(value || '').trim()
  if (!text) return null
  if (/^\d+(\.\d+)?$/.test(text)) {
    const numeric = Number(text)
    if (Number.isFinite(numeric)) {
      const timestampMs = numeric > 1_000_000_000_000 ? numeric : numeric * 1000
      const date = new Date(timestampMs)
      return Number.isNaN(date.getTime()) ? null : date
    }
  }
  const date = new Date(text)
  return Number.isNaN(date.getTime()) ? null : date
}

function formatCompactDateTime(value?: string) {
  if (!value) return null
  const date = parseFlexibleDateValue(value)
  if (!date) {
    const text = String(value || '').trim()
    return text ? { compact: text, title: text } : null
  }
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  const hour = String(date.getHours()).padStart(2, '0')
  const minute = String(date.getMinutes()).padStart(2, '0')
  return {
    compact: `${month}-${day} ${hour}:${minute}`,
    title: date.toLocaleString(),
  }
}

function formatSyncTime(value?: string | number) {
  if (!value) return ''
  const date = parseFlexibleDateValue(value)
  if (!date) return String(value || '')
  return date.toLocaleString()
}

function getSubscriptionExpiryValue(record: any) {
  const subscription = record?.chatgptLocal?.subscription && typeof record.chatgptLocal.subscription === 'object'
    ? record.chatgptLocal.subscription
    : {}
  const extra = record?.extra && typeof record.extra === 'object' ? record.extra : {}
  const candidates = [
    record?.subscription_active_until,
    subscription.subscription_active_until,
    subscription.subscription_expires_at_iso,
    subscription.subscription_expires_at,
    extra.subscription_expires_at,
    extra.chatgpt_subscription_active_until,
  ]
  for (const value of candidates) {
    const text = String(value || '').trim()
    if (text) return value
  }
  return ''
}

function formatSubscriptionExpiry(record: any) {
  const value = getSubscriptionExpiryValue(record)
  if (!value) return null
  const date = parseFlexibleDateValue(value)
  if (!date) {
    const text = String(value || '').trim()
    return text ? { date: text, time: '', title: text, expired: false, compact: text } : null
  }
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  const hour = String(date.getHours()).padStart(2, '0')
  const minute = String(date.getMinutes()).padStart(2, '0')
  const dateText = `${year}-${month}-${day}`
  const timeText = `${hour}:${minute}`
  return {
    date: dateText,
    time: timeText,
    title: date.toLocaleString(),
    expired: date.getTime() < Date.now(),
    compact: `${month}-${day} ${timeText}`,
  }
}

function getRateLimitRecoverValue(record: any) {
  const rateLimit = record?.rateLimit && typeof record.rateLimit === 'object'
    ? record.rateLimit
    : record?.rate_limit && typeof record.rate_limit === 'object'
      ? record.rate_limit
      : {}
  return record?.rate_limit_recover_at || rateLimit.recover_at || record?.extra?.rate_limit_recover_at || ''
}

function formatRateLimitRecoverAt(record: any) {
  const value = getRateLimitRecoverValue(record)
  if (!value) return null
  const date = parseFlexibleDateValue(value)
  if (!date) {
    const text = String(value || '').trim()
    return text ? { compact: text, title: text, expired: false } : null
  }
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  const hour = String(date.getHours()).padStart(2, '0')
  const minute = String(date.getMinutes()).padStart(2, '0')
  return {
    compact: `${month}-${day} ${hour}:${minute}`,
    title: date.toLocaleString(),
    expired: date.getTime() <= Date.now(),
  }
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

function getRefreshToken(record: any): string {
  if (record?.refresh_token) return String(record.refresh_token || '')
  if (record?.extra?.refresh_token) return String(record.extra.refresh_token || '')
  try {
    const extra = JSON.parse(record.extra_json || '{}')
    return extra.refresh_token || ''
  } catch {
    return ''
  }
}

function getAccessToken(record: any): string {
  if (record?.token) return String(record.token || '')
  if (record?.access_token) return String(record.access_token || '')
  if (record?.extra?.access_token) return String(record.extra.access_token || '')
  try {
    const extra = JSON.parse(record.extra_json || '{}')
    return String(extra.access_token || '')
  } catch {
    return ''
  }
}

type AccountSecretField = 'access_token' | 'refresh_token' | 'id_token' | 'session_token' | 'cookies' | 'password'

type AccountSecretResponse = {
  account_id?: number
  fields?: string[]
  secrets?: Record<string, string>
  present?: Record<string, boolean>
  lengths?: Record<string, number>
}

function accountExtraObject(record: any): Record<string, any> {
  if (record?.extra && typeof record.extra === 'object') return record.extra
  try {
    const parsed = JSON.parse(record?.extra_json || '{}')
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function boolFlag(...values: any[]): boolean | undefined {
  for (const value of values) {
    if (typeof value === 'boolean') return value
  }
  return undefined
}

function objectSecretToText(value: any): string {
  if (value === undefined || value === null) return ''
  if (typeof value === 'object') {
    try {
      const text = JSON.stringify(value)
      return text === '{}' || text === '[]' ? '' : text
    } catch {
      return String(value || '').trim()
    }
  }
  return String(value || '').trim()
}

function firstSecretText(...values: any[]): string {
  for (const value of values) {
    const text = objectSecretToText(value)
    if (text) return text
  }
  return ''
}

function legacyAccountSecret(record: any, field: AccountSecretField): string {
  const extra = accountExtraObject(record)
  if (field === 'access_token') return getAccessToken(record)
  if (field === 'refresh_token') return getRefreshToken(record)
  if (field === 'session_token') return firstSecretText(record?.session_token, extra.session_token, extra.sessionToken, extra.nextauth_session_token)
  if (field === 'cookies') return firstSecretText(extra.cookies, extra.cookie, extra.cookie_jar, extra.cookie_header)
  if (field === 'id_token') return firstSecretText(extra.id_token, extra.idToken)
  if (field === 'password') return firstSecretText(record?.password)
  return ''
}

function hasAccountSecret(record: any, field: AccountSecretField): boolean {
  const credentials = record?.credentials && typeof record.credentials === 'object' ? record.credentials : {}
  const auth = record?.auth && typeof record.auth === 'object' ? record.auth : {}
  const flag = (() => {
    if (field === 'access_token') return boolFlag(record?.has_access_token, credentials.has_access_token, auth.has_access_token)
    if (field === 'refresh_token') return boolFlag(record?.has_refresh_token, credentials.has_refresh_token, auth.has_refresh_token)
    if (field === 'session_token') return boolFlag(record?.has_session_token, credentials.has_session_token, auth.has_session_token)
    if (field === 'cookies') return boolFlag(record?.has_cookies, credentials.has_cookies, auth.has_cookies)
    if (field === 'id_token') return boolFlag(record?.has_id_token, credentials.has_id_token, auth.has_id_token)
    if (field === 'password') return boolFlag(record?.has_password, record?.password_present, credentials.has_password, auth.password_present)
    return undefined
  })()
  return flag !== undefined ? flag : Boolean(legacyAccountSecret(record, field))
}

async function fetchAccountSecrets(accountId: number, fields: AccountSecretField[]): Promise<AccountSecretResponse> {
  const normalizedFields = fields.filter(Boolean)
  if (!accountId || normalizedFields.length === 0) {
    return { account_id: accountId, fields: normalizedFields, secrets: {}, present: {}, lengths: {} }
  }
  const params = new URLSearchParams({ fields: normalizedFields.join(',') })
  return apiFetch(`/accounts/${accountId}/secrets?${params.toString()}`)
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

function getCodexUsage(record: any) {
  const codex = record?.chatgptLocal?.codex && typeof record.chatgptLocal.codex === 'object'
    ? record.chatgptLocal.codex
    : (record?.codex && typeof record.codex === 'object' ? record.codex : {})
  const usage = codex.usage && typeof codex.usage === 'object'
    ? { ...codex.usage }
    : {}
  const progress = codex.progress && typeof codex.progress === 'object' ? codex.progress : {}
  if (usage.codex_5h_used_percent === undefined) {
    if (progress?.five_hour?.used_percent !== undefined && progress?.five_hour?.used_percent !== null) {
      const winMins = readNumberValue(progress.five_hour.window_minutes)
      if (winMins === null || winMins <= 360) {
        usage.codex_5h_used_percent = progress.five_hour.used_percent
        usage.codex_5h_reset_after_seconds = progress.five_hour.reset_after_seconds
        usage.codex_5h_reset_at = progress.five_hour.reset_at
        usage.codex_5h_window_minutes = progress.five_hour.window_minutes
      }
    } else {
      const pWin = readNumberValue(usage.codex_primary_window_minutes)
      const sWin = readNumberValue(usage.codex_secondary_window_minutes)
      if (usage.codex_primary_used_percent !== undefined && pWin !== null && pWin <= 360) {
        usage.codex_5h_used_percent = usage.codex_primary_used_percent
        usage.codex_5h_reset_after_seconds = usage.codex_primary_reset_after_seconds
        usage.codex_5h_reset_at = usage.codex_primary_reset_at
        usage.codex_5h_window_minutes = usage.codex_primary_window_minutes
      } else if (usage.codex_secondary_used_percent !== undefined && (sWin !== null ? sWin <= 360 : (pWin !== null && pWin > 360))) {
        usage.codex_5h_used_percent = usage.codex_secondary_used_percent
        usage.codex_5h_reset_after_seconds = usage.codex_secondary_reset_after_seconds
        usage.codex_5h_reset_at = usage.codex_secondary_reset_at
        usage.codex_5h_window_minutes = usage.codex_secondary_window_minutes
      }
    }
  }
  if (usage.codex_7d_used_percent === undefined) {
    if (progress?.seven_day?.used_percent !== undefined && progress?.seven_day?.used_percent !== null) {
      const winMins = readNumberValue(progress.seven_day.window_minutes)
      if (winMins === null || winMins > 360) {
        usage.codex_7d_used_percent = progress.seven_day.used_percent
        usage.codex_7d_reset_after_seconds = progress.seven_day.reset_after_seconds
        usage.codex_7d_reset_at = progress.seven_day.reset_at
        usage.codex_7d_window_minutes = progress.seven_day.window_minutes
      }
    } else {
      const pWin = readNumberValue(usage.codex_primary_window_minutes)
      const sWin = readNumberValue(usage.codex_secondary_window_minutes)
      if (usage.codex_primary_used_percent !== undefined && (pWin === null || pWin > 360)) {
        usage.codex_7d_used_percent = usage.codex_primary_used_percent
        usage.codex_7d_reset_after_seconds = usage.codex_primary_reset_after_seconds
        usage.codex_7d_reset_at = usage.codex_primary_reset_at
        usage.codex_7d_window_minutes = usage.codex_primary_window_minutes
      } else if (usage.codex_secondary_used_percent !== undefined && sWin !== null && sWin > 360) {
        usage.codex_7d_used_percent = usage.codex_secondary_used_percent
        usage.codex_7d_reset_after_seconds = usage.codex_secondary_reset_after_seconds
        usage.codex_7d_reset_at = usage.codex_secondary_reset_at
        usage.codex_7d_window_minutes = usage.codex_secondary_window_minutes
      }
    }
  }
  if (!usage.codex_usage_updated_at && (progress.updated_at || codex.checked_at)) {
    usage.codex_usage_updated_at = progress.updated_at || codex.checked_at
  }
  return { codex, usage }
}

function readNumberValue(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string') {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return null
}

function formatCodexPercent(value: unknown) {
  const number = readNumberValue(value)
  if (number === null) return '-'
  const rounded = Math.round(number * 10) / 10
  return `${Number.isInteger(rounded) ? rounded.toFixed(0) : rounded.toFixed(1)}%`
}

function codexRemainingPercent(usedPercent: unknown): number | null {
  const used = readNumberValue(usedPercent)
  if (used === null) return null
  return Math.max(0, Math.min(100, 100 - used))
}

function formatCodexResetShort(resetAfter: unknown, resetAt: unknown) {
  let seconds: number | null = null
  if (resetAt) {
    const date = parseFlexibleDateValue(resetAt as any)
    if (date) seconds = Math.max(0, Math.round((date.getTime() - Date.now()) / 1000))
  }
  if (seconds === null) seconds = readNumberValue(resetAfter)
  if (seconds === null) return ''
  if (seconds <= 0) return '已重置'
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  if (days > 0) return `${days}天${hours}h`
  if (hours > 0) return `${hours}h${minutes}m`
  return `${Math.max(1, minutes)}m`
}

export function statusLabel(status?: string) {
  const normalized = String(status || '').trim()
  return STATUS_LABELS[normalized] || normalized || '未知'
}

function getBaxiGptCdk(record: any) {
  const topLevel = parseMaybeJsonObject(record?.baxigpt_cdk)
  if (Object.keys(topLevel).length > 0) return topLevel
  return parseMaybeJsonObject(record?.extra?.baxigpt_cdk)
}

function getBaxiGptCdkStatus(recordOrCdk: any) {
  const cdk = recordOrCdk?.status !== undefined || recordOrCdk?.order_id !== undefined
    ? recordOrCdk
    : getBaxiGptCdk(recordOrCdk)
  return String(cdk?.status || '').trim().toLowerCase()
}

function hasBaxiGptOrderId(cdk: any) {
  return Boolean(String(cdk?.order_id || cdk?.orderId || '').trim())
}

function isBaxiGptPendingOrder(record: any) {
  const cdk = getBaxiGptCdk(record)
  const status = getBaxiGptCdkStatus(cdk)
  return (status === 'submitted' || status === 'processing') && hasBaxiGptOrderId(cdk)
}

function isBaxiGptTerminalCdkStatus(status?: string) {
  return ['paid', 'failed', 'disabled'].includes(String(status || '').trim().toLowerCase())
}

function isBaxiGptTerminalAccountStatus(status?: string) {
  return ['subscribed', 'payment_failed', 'invalid'].includes(String(status || '').trim().toLowerCase())
}

function isBaxiGptWatchTerminal(record: any) {
  return isBaxiGptTerminalAccountStatus(record?.status) || isBaxiGptTerminalCdkStatus(getBaxiGptCdkStatus(record))
}

function collectBaxiPollingAccountIdsFromTaskSnapshot(snapshot: any) {
  const ids = new Set<number>()
  const seen = new WeakSet<object>()
  const visit = (value: any) => {
    if (!value) return
    if (Array.isArray(value)) {
      value.forEach(visit)
      return
    }
    if (typeof value !== 'object') return
    if (seen.has(value)) return
    seen.add(value)
    if (value.status_polling === true) {
      const accountId = Number(value.account_id || value.accountId || value.id || 0)
      if (Number.isFinite(accountId) && accountId > 0) ids.add(accountId)
    }
    Object.values(value).forEach(visit)
  }
  visit(snapshot?.runtime_results)
  visit(snapshot?.meta?.runtime_results)
  return ids
}

function mergeBaxiSnapshotIntoAccount(account: any, item: any) {
  if (!item || typeof item !== 'object') return account
  const extraPatch = item.extra && typeof item.extra === 'object' && !Array.isArray(item.extra) ? item.extra : {}
  const snapshotCdk = parseMaybeJsonObject(item.baxigpt_cdk)
  const extraSnapshotCdk = parseMaybeJsonObject(extraPatch.baxigpt_cdk)
  const currentCdk = getBaxiGptCdk(account)
  const nextCdk = Object.keys(snapshotCdk).length > 0
    ? snapshotCdk
    : Object.keys(extraSnapshotCdk).length > 0
      ? extraSnapshotCdk
      : currentCdk
  return {
    ...account,
    status: item.status !== undefined ? item.status : account.status,
    updated_at: item.updated_at !== undefined ? item.updated_at : account.updated_at,
    baxigpt_cdk: nextCdk,
    extra: {
      ...(account.extra || {}),
      ...extraPatch,
      baxigpt_cdk: nextCdk,
    },
  }
}

function normalizeSubscriptionPlanValue(value?: unknown) {
  const plan = String(value || '').trim().toLowerCase().replace('-', '_')
  if (!plan) return 'unknown'
  if (plan.includes('enterprise')) return 'enterprise'
  if (plan.includes('team') || plan.includes('business')) return 'team'
  if (plan.includes('pro')) return 'pro'
  if (plan.includes('plus')) return 'plus'
  if (plan.includes('free')) return 'free'
  return 'unknown'
}

function planMeta(plan?: string) {
  switch (normalizeSubscriptionPlanValue(plan)) {
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

function authTypeValue(record: any) {
  const capabilities = record?.chatgptCapabilities || {}
  const authLevel = String(record?.auth_level || capabilities.auth_level || '').trim().toLowerCase()
  if (authLevel === 'refresh_token') return 'refresh_token'
  if (hasAccountSecret(record, 'refresh_token')) return 'refresh_token'
  if (authLevel === 'access_token_only') return 'access_token_only'
  if (hasAccountSecret(record, 'access_token')) return 'access_token_only'
  return 'unknown'
}

function authTypeMeta(record: any) {
  switch (authTypeValue(record)) {
    case 'refresh_token':
      return { color: 'success', label: '有RT' }
    case 'access_token_only':
      return { color: 'blue', label: '仅AT' }
    default:
      return { color: 'default', label: '无认证' }
  }
}

function subscriptionTypeValue(record: any) {
  const capabilities = record?.chatgptCapabilities || {}
  const localSubscription = record?.chatgptLocal?.subscription || {}
  for (const value of [localSubscription.plan, record?.subscription_plan, capabilities.subscription_plan]) {
    const plan = normalizeSubscriptionPlanValue(value)
    if (plan !== 'unknown') return plan
  }
  return 'unknown'
}

function lastKnownSubscriptionTypeValue(record: any) {
  const capabilities = record?.chatgptCapabilities || {}
  const localSubscription = record?.chatgptLocal?.subscription || {}
  for (const value of [
    localSubscription.last_known_plan,
    record?.last_known_subscription_plan,
    capabilities.last_known_subscription_plan,
    record?.extra?.last_known_subscription_plan,
    record?.extra?.chatgpt_plan_type,
    record?.extra?.chatgpt_subscription_plan,
  ]) {
    const plan = normalizeSubscriptionPlanValue(value)
    if (plan !== 'unknown') return plan
  }
  return 'unknown'
}

function subscriptionTypeMeta(record: any) {
  const current = subscriptionTypeValue(record)
  const lastKnown = lastKnownSubscriptionTypeValue(record)
  const refreshState = String(record?.subscription_refresh_state || record?.chatgptLocal?.subscription?.refresh_state || record?.chatgptCapabilities?.subscription_refresh_state || '').trim().toLowerCase()
  const stale = current === 'unknown' && lastKnown !== 'unknown'
  if (stale) {
    const last = planMeta(lastKnown)
    if (accountValidityValue(record) === 'invalid' || refreshState === 'auth_invalid') {
      return { color: 'error', label: '不可确认', subLabel: `上次 ${last.label}`, title: `当前订阅因认证失效不可确认；上次确认：${last.label}` }
    }
    return { color: 'warning', label: '待刷新', subLabel: `上次 ${last.label}`, title: `当前订阅未确认；上次确认：${last.label}` }
  }
  switch (current) {
    case 'free':
      return { color: 'default', label: 'Free', subLabel: '', title: '当前确认订阅：Free' }
    case 'plus':
      return { color: 'success', label: 'Plus', subLabel: '', title: '当前确认订阅：Plus' }
    case 'team':
      return { color: 'processing', label: 'Team', subLabel: '', title: '当前确认订阅：Team / Business' }
    case 'pro':
      return { color: 'processing', label: 'Pro', subLabel: '', title: '当前确认订阅：Pro' }
    case 'enterprise':
      return { color: 'processing', label: 'Enterprise', subLabel: '', title: '当前确认订阅：Enterprise' }
    default:
      return { color: 'default', label: refreshState === 'not_checked' ? '未验证' : '未知', subLabel: '', title: '当前订阅未确认' }
  }
}

function accountValidityValue(record: any) {
  const capabilities = record?.chatgptCapabilities || {}
  const authState = String(record?.chatgptLocal?.auth?.state || '').trim().toLowerCase()
  const codexState = String(record?.chatgptLocal?.codex?.state || '').trim().toLowerCase()
  const invalidStates = new Set([
    'refresh_token_invalidated',
    'access_token_invalidated',
    'unauthorized',
    'account_deactivated',
    'banned_like',
    'invalid',
  ])
  if (String(record?.status || '').trim().toLowerCase() === 'invalid') return 'invalid'
  if (String(record?.auth_level || capabilities.auth_level || '').trim().toLowerCase() === 'invalid') return 'invalid'
  if (String(capabilities.upload_gate || '').trim().toLowerCase() === 'blocked_auth_invalid') return 'invalid'
  if (invalidStates.has(authState) || invalidStates.has(codexState)) return 'invalid'
  if (authState === 'probe_failed' || codexState === 'probe_failed') return 'refresh_failed'
  if (!authState && !String(record?.auth_level || capabilities.auth_level || '').trim()) return 'not_checked'
  return 'valid'
}

function accountValidityMeta(record: any) {
  switch (accountValidityValue(record)) {
    case 'invalid':
      return { color: 'error', label: '认证失效' }
    case 'refresh_failed':
      return { color: 'warning', label: '刷新失败' }
    case 'not_checked':
      return { color: 'default', label: '未验证' }
    default:
      return { color: 'success', label: '认证通过' }
  }
}

function getIdeaSubmitSummary(record: any) {
  const topLevel = record?.idea_submit && typeof record.idea_submit === 'object' ? record.idea_submit : {}
  if (Object.keys(topLevel).length > 0) return topLevel
  const camel = record?.ideaSubmit && typeof record.ideaSubmit === 'object' ? record.ideaSubmit : {}
  if (Object.keys(camel).length > 0) return camel
  return record?.extra?.idea_submit && typeof record.extra.idea_submit === 'object' ? record.extra.idea_submit : {}
}

function ideaSubmitMeta(record: any) {
  const summary = getIdeaSubmitSummary(record)
  const unavailable = Boolean(summary?.unavailable)
  const status = String(summary?.status || '').trim().toLowerCase()
  if (unavailable || status === 'unavailable') {
    return { color: 'error', label: '不可用', reason: String(summary?.reason || '').trim() }
  }
  if (status === 'paid') return { color: 'success', label: '已开通', reason: '' }
  if (status === 'submitted' || status === 'processing') return { color: 'processing', label: '提交中', reason: '' }
  if (status === 'failed') return { color: 'warning', label: '提交失败', reason: String(summary?.reason || '').trim() }
  return { color: 'default', label: '未提交', reason: '' }
}

function isPaypalBindingEligibleAccount(record: any) {
  const status = String(record?.status || '').trim().toLowerCase()
  return status !== 'subscribed'
    && PAYPAL_BINDING_ALLOWED_ACCOUNT_VALIDITY.has(accountValidityValue(record))
    && PAYPAL_BINDING_ALLOWED_SUBSCRIPTION_TYPES.has(subscriptionTypeValue(record))
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

function shouldShowInvalidRecheckButton(record: any) {
  return String(record?.status || '').trim().toLowerCase() === 'invalid'
}

function taskModalModeFromSource(source: unknown): 'register' | 'resume_auth' | 'payment_link' | 'sub2api_upload' | 'oaipay_upload' | 'baxigpt_cdk' | 'paypal_bind' | 'probe_local_status' | 'k12_recapture' {
  const normalized = String(source || '').trim().toLowerCase()
  if (normalized === 'baxigpt_cdk' || normalized === 'baxigpt_cdk_submit') return 'baxigpt_cdk'
  if (normalized === 'chatgpt_paypal_bind' || normalized === 'paypal_bind') return 'paypal_bind'
  if (normalized === 'phone_binding_test') return 'resume_auth'
  if (normalized === 'resume_auth' || normalized === 'resume_subscription_auth' || normalized === 'batch_resume_subscription_auth') return 'resume_auth'
  if (normalized === 'batch_probe_local_status' || normalized === 'probe_local_status') return 'probe_local_status'
  if (normalized === 'batch_sub2api_upload') return 'sub2api_upload'
  if (normalized === 'batch_oaipay_upload') return 'oaipay_upload'
  if (normalized === 'invalid_recheck' || normalized === 'batch_invalid_recheck') return 'resume_auth'
  if (normalized === 'k12_workspace_recapture' || normalized === 'batch_k12_workspace_recapture') return 'k12_recapture'
  if (normalized === 'payment_link' || normalized === 'batch_payment_link') return 'payment_link'
  return 'register'
}

function toSelectOptions(options: Array<{ value: string; text: string }>) {
  return options.map((option) => ({ value: option.value, label: option.text }))
}

function toCheckboxOptions(options: Array<{ value: string; text: string }>): CheckboxOptionType<string>[] {
  return options.map((option) => ({ value: option.value, label: option.text }))
}

const accountActionTextStyles: Record<string, CSSProperties> = {
  payment: { color: '#1677ff' },
  refresh: { color: '#389e0d' },
  resume: { color: '#d48806' },
  more: { color: '#722ed1' },
}

export default function Accounts() {
  const { token } = theme.useToken()
  const screens = Grid.useBreakpoint()
  const isMobile = screens.lg === false
  const isCompactDesktop = !isMobile && screens.xl === false
  const currentPlatform = 'chatgpt'
  const [accounts, setAccounts] = useState<any[]>([])
  const [watchingBaxiAccountIds, setWatchingBaxiAccountIds] = useState<Set<number>>(() => new Set())
  const [platformActions, setPlatformActions] = useState<any[]>([])
  const [platformActionsLoading, setPlatformActionsLoading] = useState(false)
  const [total, setTotal] = useState(0)
  const [currentPage, setCurrentPage] = useState(1)
  const [accountsPageSize, setAccountsPageSize] = useState(loadAccountsPageSize)
  const [columnFilters, setColumnFilters] = useState<AccountColumnFilters>(EMPTY_ACCOUNT_FILTERS)
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [pageVisible, setPageVisible] = useState(
    typeof document === 'undefined' ? true : document.visibilityState === 'visible',
  )
  const [filterStatus, setFilterStatus] = useState('')
  const [subscriptionExpirySortOrder, setSubscriptionExpirySortOrder] = useState<SubscriptionExpirySortOrder>('')
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([])
  const [selectedAccountSnapshots, setSelectedAccountSnapshots] = useState<Record<string, any>>({})
  const [filterPresets, setFilterPresets] = useState<AccountFilterPreset[]>([])
  const [filterPresetLoading, setFilterPresetLoading] = useState(false)
  const [filterPresetSaving, setFilterPresetSaving] = useState(false)
  const [activeFilterPresetId, setActiveFilterPresetId] = useState('')
  const [filterPresetManageOpen, setFilterPresetManageOpen] = useState(false)
  const [filterPresetEditorOpen, setFilterPresetEditorOpen] = useState(false)
  const [filterPresetEditing, setFilterPresetEditing] = useState<AccountFilterPreset | null>(null)
  const [filterPresetEditorMode, setFilterPresetEditorMode] = useState<'create-current' | 'edit-meta' | 'copy-preset'>('create-current')

  const [registerModalOpen, setRegisterModalOpen] = useState(false)
  const [taskModalMode, setTaskModalMode] = useState<'register' | 'resume_auth' | 'payment_link' | 'sub2api_upload' | 'oaipay_upload' | 'baxigpt_cdk' | 'paypal_bind' | 'probe_local_status' | 'k12_recapture'>('register')
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
  const [resumeAuthConfigOpen, setResumeAuthConfigOpen] = useState(false)
  const [resumeAuthConfigMode, setResumeAuthConfigMode] = useState<'single' | 'batch'>('single')
  const [resumeAuthConfigAccount, setResumeAuthConfigAccount] = useState<any>(null)
  const [resumeAuthConfigScope, setResumeAuthConfigScope] = useState<'selected' | 'filtered'>('selected')
  const [phoneBindingTestOpen, setPhoneBindingTestOpen] = useState(false)
  const [phoneBindingTestLoading, setPhoneBindingTestLoading] = useState(false)
  const [phoneBindingTestScope, setPhoneBindingTestScope] = useState<'selected' | 'filtered'>('selected')
  const [phoneBindingManualOpen, setPhoneBindingManualOpen] = useState(false)
  const [phoneBindingPrefixPickerOpen, setPhoneBindingPrefixPickerOpen] = useState(false)
  const [phoneBindingAdvancedOpen, setPhoneBindingAdvancedOpen] = useState(false)
  const [phonePoolSummary, setPhonePoolSummary] = useState<PhonePoolSummary | null>(null)
  const [phonePoolSummaryLoading, setPhonePoolSummaryLoading] = useState(false)
  const [baxiCdkSubmitOpen, setBaxiCdkSubmitOpen] = useState(false)
  const [baxiCdkSubmitLoading, setBaxiCdkSubmitLoading] = useState(false)
  const [baxiCdkSubmitScope, setBaxiCdkSubmitScope] = useState<'selected' | 'filtered'>('selected')
  const [baxiCdkManualOpen, setBaxiCdkManualOpen] = useState(false)
  const [baxiCdkAdvancedOpen, setBaxiCdkAdvancedOpen] = useState(false)
  const [baxiCdkPoolItems, setBaxiCdkPoolItems] = useState<BaxiGptCdkPoolItem[]>([])
  const [baxiCdkPoolItemsLoading, setBaxiCdkPoolItemsLoading] = useState(false)
  const [baxiCdkQuotaRefreshing, setBaxiCdkQuotaRefreshing] = useState(false)
  const [baxiCdkSavingToPool, setBaxiCdkSavingToPool] = useState(false)
  const [paypalBindingOpen, setPaypalBindingOpen] = useState(false)
  const [paypalBindingLoading, setPaypalBindingLoading] = useState(false)
  const [paypalBindingScope, setPaypalBindingScope] = useState<'selected' | 'filtered'>('selected')
  const [paypalFilteredEligibleCount, setPaypalFilteredEligibleCount] = useState<number | null>(null)
  const [paypalFilteredEligibleLoading, setPaypalFilteredEligibleLoading] = useState(false)
  const [baxiCdkPoolSummary, setBaxiCdkPoolSummary] = useState<BaxiGptCdkPoolSummary | null>(null)
  const [baxiCdkPoolSummaryLoading, setBaxiCdkPoolSummaryLoading] = useState(false)
  const [batchPaymentLinkConfigOpen, setBatchPaymentLinkConfigOpen] = useState(false)
  const [batchPaymentLinkForceRefresh, setBatchPaymentLinkForceRefresh] = useState(false)
  const [batchProbeStatusConfigOpen, setBatchProbeStatusConfigOpen] = useState(false)
  const [batchProbeStatusConfigScope, setBatchProbeStatusConfigScope] = useState<'selected' | 'all'>('selected')
  const [batchK12RecaptureOpen, setBatchK12RecaptureOpen] = useState(false)
  const [batchK12RecaptureScope, setBatchK12RecaptureScope] = useState<'selected' | 'filtered'>('selected')
  const [batchK12RecaptureLoading, setBatchK12RecaptureLoading] = useState(false)

  const [registerForm] = Form.useForm()
  const [addForm] = Form.useForm()
  const [detailForm] = Form.useForm()
  const [resumeAuthConfigForm] = Form.useForm()
  const [phoneBindingTestForm] = Form.useForm()
  const [baxiCdkSubmitForm] = Form.useForm()
  const [paypalBindingForm] = Form.useForm()
  const [batchPaymentLinkConfigForm] = Form.useForm()
  const [batchProbeStatusConfigForm] = Form.useForm()
  const [batchK12RecaptureForm] = Form.useForm()
  const [filterPresetForm] = Form.useForm()
  const phoneBindingUsePoolValue = Form.useWatch('use_pool', phoneBindingTestForm)
  const phoneBindingPoolModeValue = Form.useWatch('phone_pool_mode', phoneBindingTestForm)
  const phoneBindingSelectedPrefixesValue = Form.useWatch('selected_prefixes', phoneBindingTestForm)
  const phoneBindingPrefixSampleSizeValue = Form.useWatch('prefix_sample_size', phoneBindingTestForm)
  const phoneBindingPrefixSampleFilterValue = Form.useWatch('prefix_sample_filter', phoneBindingTestForm)
  const phoneBindingSmsProbeOnlyValue = Form.useWatch('prefix_sms_probe_only', phoneBindingTestForm)
  const phoneBindingReusePhoneValue = Form.useWatch('reuse_phone_until_unusable', phoneBindingTestForm)
  const phoneBindingPhoneLinesValue = Form.useWatch('phone_lines', phoneBindingTestForm)
  const phoneBindingProxyModeValue = Form.useWatch('proxy_mode', phoneBindingTestForm)
  const phoneBindingProxyFailoverValue = Form.useWatch('proxy_failover', phoneBindingTestForm)
  const probeProxyModeValue = Form.useWatch('proxy_mode', batchProbeStatusConfigForm)
  const probeProxyFailoverValue = Form.useWatch('proxy_failover', batchProbeStatusConfigForm)
  const batchK12ProxyModeValue = Form.useWatch('proxy_mode', batchK12RecaptureForm)
  const batchK12ProxyFailoverValue = Form.useWatch('proxy_failover', batchK12RecaptureForm)
  const baxiCdkUsePoolValue = Form.useWatch('use_pool', baxiCdkSubmitForm)
  const baxiCdkCodeLinesValue = Form.useWatch('code_lines', baxiCdkSubmitForm)
  const baxiCdkSelectedIdsValue = Form.useWatch('cdk_ids', baxiCdkSubmitForm)
  const baxiCdkTargetSuccessValue = Form.useWatch('target_success_count', baxiCdkSubmitForm)
  const [registerMailProvider, setRegisterMailProvider] = useState('luckmail')
  const [configCache, setConfigCache] = useState<Record<string, any> | null>(null)
  const { mode: chatgptRegistrationMode, setMode: setChatgptRegistrationMode } =
    usePersistentChatGPTRegistrationMode()
  const [importText, setImportText] = useState('')
  const [importLoading, setImportLoading] = useState(false)
  const [taskId, setTaskId] = useState<string | null>(null)
  const [taskSnapshot, setTaskSnapshot] = useState<any>(null)

  const [oaipayUploadModalOpen, setOaipayUploadModalOpen] = useState(false)
  const [oaipayUploadScope, setOaipayUploadScope] = useState<'selected' | 'pending'>('selected')
  const [oaipayCategories, setOaipayCategories] = useState<{id: number, name: string}[]>([])
  const [oaipayCategoryLoading, setOaipayCategoryLoading] = useState(false)
  const [oaipaySelectedCategory, setOaipaySelectedCategory] = useState<number | undefined>()
  const [oaipayFallbackCategory, setOaipayFallbackCategory] = useState<number | undefined>()
  const [oaipayCategoryMode, setOaipayCategoryMode] = useState<'auto' | 'manual'>('auto')

  const openOaipayUploadModal = async (scope: 'selected' | 'pending') => {
    setOaipayUploadScope(scope)
    setOaipayCategoryMode('auto')
    setOaipayUploadModalOpen(true)
    setOaipayCategoryLoading(true)
    try {
      const res = await apiFetch('/integrations/oaipay-categories')
      if (Array.isArray(res)) {
        setOaipayCategories(res)
      } else if (res?.categories && Array.isArray(res.categories)) {
        setOaipayCategories(res.categories)
      } else if (res?.data && Array.isArray(res.data)) {
        setOaipayCategories(res.data)
      } else if (res?.error) {
        message.error('无法获取 OAIPay 分组: ' + res.error)
      }
    } catch (e) {
      message.error('请求 OAIPay 分组失败: ' + String(e))
    } finally {
      setOaipayCategoryLoading(false)
    }
  }
  const [activeTasksPanelOpen, setActiveTasksPanelOpen] = useState(false)
  const [registerLoading, setRegisterLoading] = useState(false)
  const [registerSettingsSaving, setRegisterSettingsSaving] = useState(false)
  const [selectedPendingInviteRowKeys, setSelectedPendingInviteRowKeys] = useState<React.Key[]>([])
  const [activatingPendingInviteId, setActivatingPendingInviteId] = useState<number | null>(null)
  const [abandoningPendingInviteId, setAbandoningPendingInviteId] = useState<number | null>(null)
  const [activatingAllPendingInvites, setActivatingAllPendingInvites] = useState(false)
  const [backfillLoading, setBackfillLoading] = useState<'' | 'cliproxyapi_pending' | 'cliproxyapi_selected' | 'sub2api_pending' | 'sub2api_selected'>('')
  const [batchResumeAuthLoading, setBatchResumeAuthLoading] = useState<'' | 'selected' | 'filtered' | 'selected_phone' | 'filtered_phone'>('')
  const [batchPaymentLinkLoading, setBatchPaymentLinkLoading] = useState(false)
  const [batchInvalidRecheckLoading, setBatchInvalidRecheckLoading] = useState(false)
  const [visibleColumnKeys, setVisibleColumnKeys] = useState<AccountColumnKey[]>(() => loadVisibleAccountColumnKeys())
  const [statusSyncLoading, setStatusSyncLoading] = useState<
    'probe_selected' | 'probe_all' | 'remote_selected' | 'remote_all' | 'sub2api_selected' | 'sub2api_all' | ''
  >('')
  const [deleteInvalidLoading, setDeleteInvalidLoading] = useState(false)
  const [batchGopayOpen, setBatchGopayOpen] = useState(false)
  const [batchGopayItems, setBatchGopayItems] = useState<BatchGopayItem[]>([])
  const [batchGopayPhones, setBatchGopayPhones] = useState<GopayPhoneCandidate[]>([])
  const [batchGopayDefaults, setBatchGopayDefaults] = useState<Record<string, any>>({})
  const [batchGopayLoading, setBatchGopayLoading] = useState(false)
  const [batchGopayPhoneCountryCode, setBatchGopayPhoneCountryCode] = useState(DEFAULT_GOPAY_PHONE_COUNTRY_CODE)
  const [batchGopayPhoneNumber, setBatchGopayPhoneNumber] = useState('')
  const [batchGopayRecognizedCountryCodes, setBatchGopayRecognizedCountryCodes] = useState<string[]>([DEFAULT_GOPAY_PHONE_COUNTRY_CODE])
  const [batchGopayPhoneSaving, setBatchGopayPhoneSaving] = useState(false)
  const [batchGopayStarted, setBatchGopayStarted] = useState(false)
  const [batchGopayRoundInterval, setBatchGopayRoundInterval] = useState(60)
  const [batchGopayOtpAutoResendDelay, setBatchGopayOtpAutoResendDelay] = useState(DEFAULT_GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS)
  const [batchGopayOtpDelaySaving, setBatchGopayOtpDelaySaving] = useState(false)
  const [batchGopayNextRoundAt, setBatchGopayNextRoundAt] = useState<number | null>(null)
  const [accessTokenCopiedAccountIds, setAccessTokenCopiedAccountIds] = useState<Set<number>>(() => new Set())
  const [codexUsageRefreshingIds, setCodexUsageRefreshingIds] = useState<Set<number>>(() => new Set())
  const batchGopayStartingRef = useRef(false)
  const batchGopayCancelRequestedRef = useRef(false)
  const accountsQuery = useAccountsQuery({
    email: debouncedSearch,
    status: filterStatus,
    manuallyUsed: columnFilters.manuallyUsed.join(','),
    authType: columnFilters.authType.join(','),
    subscriptionType: columnFilters.subscriptionType.join(','),
    accountValidity: columnFilters.accountValidity.join(','),
    sub2apiState: columnFilters.sub2apiState.join(','),
    oaipayState: columnFilters.oaipayState.join(','),
    ideaSubmitState: columnFilters.ideaSubmitState.join(','),
    sortBy: subscriptionExpirySortOrder ? SUBSCRIPTION_EXPIRY_SORT_FIELD : '',
    sortOrder: subscriptionExpirySortOrder,
    page: currentPage,
    pageSize: accountsPageSize,
  })
  const accountDetailQuery = useAccountDetailQuery(detailAccount?.id ? Number(detailAccount.id) : null, detailModalOpen)
  const activeTasksQuery = useActiveTasksQuery(activeTasksPanelOpen)
  const pendingInvitesQuery = usePendingInvitesQuery(businessDeferredModalOpen && currentPlatform === 'chatgpt')
  const activeTasks = activeTasksQuery.data ?? EMPTY_LIST
  const activeTasksLoading = activeTasksQuery.isLoading || activeTasksQuery.isFetching
  const pendingBusinessInvites = pendingInvitesQuery.data ?? EMPTY_LIST
  const pendingBusinessInvitesLoading = pendingInvitesQuery.isLoading || pendingInvitesQuery.isFetching
  const loading = accountsQuery.isLoading || accountsQuery.isFetching
  const visibleAccountIds = useMemo(() => new Set(
    accounts.map((account) => Number(account?.id || 0)).filter((id) => Number.isFinite(id) && id > 0),
  ), [accounts])
  const watchingBaxiAccountIdsKey = useMemo(() => (
    Array.from(watchingBaxiAccountIds)
      .filter((id) => visibleAccountIds.has(Number(id)))
      .sort((a, b) => a - b)
      .join(',')
  ), [visibleAccountIds, watchingBaxiAccountIds])
  const currentFilterPresetFilters = useMemo(
    () => buildAccountFilterPresetFilters(search, columnFilters, subscriptionExpirySortOrder, accountsPageSize),
    [search, columnFilters, subscriptionExpirySortOrder, accountsPageSize],
  )
  const activeFilterPreset = useMemo(
    () => filterPresets.find((item) => item.id === activeFilterPresetId) || null,
    [activeFilterPresetId, filterPresets],
  )
  const activeFilterPresetDirty = useMemo(() => {
    if (!activeFilterPreset) return false
    return accountFilterPresetSignature(activeFilterPreset.filters) !== accountFilterPresetSignature(currentFilterPresetFilters)
  }, [activeFilterPreset, currentFilterPresetFilters])
  const pinnedFilterPresets = useMemo(() => {
    const items = filterPresets.filter((item) => item.pinned)
    return items.slice(0, isMobile ? 4 : 8)
  }, [filterPresets, isMobile])

  const loadFilterPresets = useCallback(async (silent = false) => {
    setFilterPresetLoading(true)
    try {
      const data = await apiFetch('/accounts/filter-presets')
      const items = Array.isArray(data?.items) ? data.items : []
      setFilterPresets(items.map((item: any) => ({
        id: String(item?.id || ''),
        name: String(item?.name || ''),
        description: String(item?.description || ''),
        summary: String(item?.summary || ''),
        filters: normalizeAccountFilterPresetFilters(item?.filters),
        pinned: Boolean(item?.pinned),
        built_in: Boolean(item?.built_in),
        created_at: String(item?.created_at || ''),
        updated_at: String(item?.updated_at || ''),
      })).filter((item: AccountFilterPreset) => item.id && item.name))
    } catch (e: any) {
      if (!silent) message.error(e?.message || '读取筛选组合失败')
    } finally {
      setFilterPresetLoading(false)
    }
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedSearch(search.trim())
    }, 300)
    return () => window.clearTimeout(timer)
  }, [search])

  useEffect(() => {
    void loadFilterPresets(true)
  }, [loadFilterPresets])

  useEffect(() => {
    setCurrentPage(1)
  }, [debouncedSearch, filterStatus, columnFilters.manuallyUsed, columnFilters.authType, columnFilters.subscriptionType, columnFilters.accountValidity, columnFilters.sub2apiState, columnFilters.oaipayState, columnFilters.ideaSubmitState, subscriptionExpirySortOrder])

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

  const refreshCodexUsage = useCallback(async (record: any) => {
    const accountId = Number(record?.id || 0)
    if (!Number.isFinite(accountId) || accountId <= 0) return
    setCodexUsageRefreshingIds((prev) => new Set(prev).add(accountId))
    try {
      const result = await apiFetch(`/chatgpt/${accountId}/codex-usage/refresh`, {
        method: 'POST',
        body: JSON.stringify({ force: true }),
      })
      const state = String(result?.codex?.state || result?.probe?.state || '').trim()
      if (state === 'usable' || state === 'quota_exhausted') {
        message.success(state === 'quota_exhausted' ? 'Codex 用量已刷新：额度耗尽' : 'Codex 用量已刷新')
      } else {
        message.warning(result?.codex?.message || result?.probe?.message || 'Codex 用量已刷新，但状态异常')
      }
      await accountsQuery.refetch()
    } catch (e: any) {
      message.error(e?.message || '刷新 Codex 用量失败')
    } finally {
      setCodexUsageRefreshingIds((prev) => {
        const next = new Set(prev)
        next.delete(accountId)
        return next
      })
    }
  }, [accountsQuery.refetch])

  const loadConfigCache = useCallback(async (options: { force?: boolean } = {}) => {
    if (!options.force && configCache) return configCache
    const cfg = await apiFetch('/config')
    setConfigCache(cfg)
    return cfg
  }, [configCache])

  const load = useCallback(async () => {
    await accountsQuery.refetch()
  }, [accountsQuery.refetch])

  const loadPhonePoolSummary = useCallback(async (silent = true) => {
    setPhonePoolSummaryLoading(true)
    try {
      const data = await apiFetch('/phone-pool')
      const summary = data?.summary && typeof data.summary === 'object' ? data.summary : data
      setPhonePoolSummary(summary && typeof summary === 'object' ? summary : {})
    } catch (e: any) {
      if (!silent) message.error(e?.message || '读取手机号池状态失败')
    } finally {
      setPhonePoolSummaryLoading(false)
    }
  }, [])

  const loadBaxiCdkPoolSummary = useCallback(async (silent = true) => {
    setBaxiCdkPoolSummaryLoading(true)
    try {
      const data = await apiFetch('/baxigpt-cdk-pool/summary')
      const summary = data?.summary && typeof data.summary === 'object' ? data.summary : data
      setBaxiCdkPoolSummary(summary && typeof summary === 'object' ? summary : {})
    } catch (e: any) {
      if (!silent) message.error(e?.message || '读取卡密池状态失败')
    } finally {
      setBaxiCdkPoolSummaryLoading(false)
    }
  }, [])

  const loadBaxiCdkPoolItems = useCallback(async (silent = true) => {
    setBaxiCdkPoolItemsLoading(true)
    try {
      const data = await apiFetch('/baxigpt-cdk-pool?status=available')
      const nextItems = Array.isArray(data?.items) ? data.items : []
      setBaxiCdkPoolItems(nextItems)
      setBaxiCdkPoolSummary((prev) => {
        const summary = data?.summary && typeof data.summary === 'object' ? data.summary : null
        return summary ? { ...(prev || {}), ...summary } : prev
      })
      const availableIds = new Set(nextItems.map((item: BaxiGptCdkPoolItem) => Number(item.id)).filter((id: number) => Number.isFinite(id) && id > 0))
      const currentIds = normalizeBaxiCdkIdList(baxiCdkSubmitForm.getFieldValue('cdk_ids'))
      const filteredIds = currentIds.filter((id) => availableIds.has(Number(id)))
      if (filteredIds.length !== currentIds.length) baxiCdkSubmitForm.setFieldsValue({ cdk_ids: filteredIds })
    } catch (e: any) {
      if (!silent) message.error(e?.message || '读取可用卡密失败')
    } finally {
      setBaxiCdkPoolItemsLoading(false)
    }
  }, [baxiCdkSubmitForm])

  const refreshAllBaxiCdkQuota = useCallback(async () => {
    if (baxiCdkQuotaRefreshing) return
    const toastKey = 'baxigpt-cdk-quota-refresh'
    setBaxiCdkQuotaRefreshing(true)
    message.loading({ content: '正在查询全部 CDK 剩余额度...', key: toastKey, duration: 0 })
    try {
      const data = await apiFetch('/baxigpt-cdk-pool')
      const allItems = Array.isArray(data?.items) ? data.items : []
      const ids = normalizeBaxiCdkIdList(allItems.map((item: any) => item?.id))
      if (ids.length <= 0) {
        message.info({ content: '卡密池暂无可查询的 CDK', key: toastKey })
        return
      }

      const chunks: number[][] = []
      for (let index = 0; index < ids.length; index += 100) {
        chunks.push(ids.slice(index, index + 100))
      }
      const rows: any[] = []
      let failedChunks = 0
      let lastError = ''
      for (const chunk of chunks) {
        try {
          const result = await apiFetch('/baxigpt-cdk-pool/quota', {
            method: 'POST',
            body: JSON.stringify({ ids: chunk, limit: chunk.length, include_query: true }),
          })
          rows.push(...(Array.isArray(result?.items) ? result.items : []))
        } catch (chunkError: any) {
          failedChunks += 1
          lastError = chunkError?.message || String(chunkError || '查询失败')
        }
      }
      if (rows.length <= 0 && failedChunks > 0) {
        throw new Error(lastError || '查询全部 CDK 剩余额度失败')
      }
      await Promise.all([loadBaxiCdkPoolSummary(true), loadBaxiCdkPoolItems(true)])
      const okCount = rows.filter((item) => item?.ok).length
      const nextItems = rows
        .map((row) => (row?.item && typeof row.item === 'object' ? row.item : null))
        .filter(Boolean)
      const availableCount = nextItems.filter((item) => String(item?.status || '') === 'available').length
      const remainingCapacity = nextItems
        .filter((item) => String(item?.status || '') === 'available')
        .reduce((sum, item) => sum + baxiCdkSubmitCapacity(item), 0)
      const suffix = failedChunks > 0 ? `，${failedChunks} 批失败` : ''
      message.success({
        content: `CDK 剩余额度查询完成：查询 ${rows.length}/${ids.length} 个，成功 ${okCount} 个，可用 ${availableCount} 个，剩余额度 ${remainingCapacity}${suffix}`,
        key: toastKey,
      })
    } catch (e: any) {
      message.error({ content: e?.message || '查询全部 CDK 剩余额度失败', key: toastKey })
    } finally {
      setBaxiCdkQuotaRefreshing(false)
    }
  }, [baxiCdkQuotaRefreshing, loadBaxiCdkPoolItems, loadBaxiCdkPoolSummary])

  const applyCurrentFiltersToBody = (body: Record<string, unknown>) => {
    if (search) body.email = search
    if (filterStatus) body.status = filterStatus
    if (columnFilters.manuallyUsed.length) body.manually_used = columnFilters.manuallyUsed.join(',')
    if (columnFilters.authType.length) body.auth_type = columnFilters.authType.join(',')
    if (columnFilters.subscriptionType.length) body.subscription_type = columnFilters.subscriptionType.join(',')
    if (columnFilters.accountValidity.length) body.account_validity = columnFilters.accountValidity.join(',')
    if (columnFilters.sub2apiState.length) body.sub2api_state = columnFilters.sub2apiState.join(',')
    if (columnFilters.oaipayState.length) body.oaipay_state = columnFilters.oaipayState.join(',')
    if (columnFilters.ideaSubmitState.length) body.idea_submit_state = columnFilters.ideaSubmitState.join(',')
  }

  const buildPaypalFilteredEligibleParams = useCallback(() => {
    const body: Record<string, unknown> = {}
    applyCurrentFiltersToBody(body)
    applyPaypalBindingEligibilityFilters(body)
    const params = new URLSearchParams({
      platform: 'chatgpt',
      page: '1',
      page_size: '1',
      detail: 'false',
    })
    if (body.email) params.set('email', String(body.email))
    if (body.status) params.set('status', String(body.status))
    if (body.manually_used) params.set('manually_used', String(body.manually_used))
    if (body.auth_type) params.set('auth_type', String(body.auth_type))
    if (body.subscription_type) params.set('subscription_type', String(body.subscription_type))
    if (body.account_validity) params.set('account_validity', String(body.account_validity))
    if (body.sub2api_state) params.set('sub2api_state', String(body.sub2api_state))
    if (body.oaipay_state) params.set('oaipay_state', String(body.oaipay_state))
    if (body.idea_submit_state) params.set('idea_submit_state', String(body.idea_submit_state))
    return params
  }, [
    search,
    filterStatus,
    columnFilters.manuallyUsed,
    columnFilters.authType,
    columnFilters.subscriptionType,
    columnFilters.accountValidity,
    columnFilters.sub2apiState,
    columnFilters.oaipayState,
    columnFilters.ideaSubmitState,
  ])

  useEffect(() => {
    if (!phoneBindingTestOpen) return
    void loadPhonePoolSummary()
  }, [phoneBindingTestOpen, loadPhonePoolSummary])

  useEffect(() => {
    if (!baxiCdkSubmitOpen) return
    void loadBaxiCdkPoolSummary()
    void loadBaxiCdkPoolItems()
  }, [baxiCdkSubmitOpen, loadBaxiCdkPoolItems, loadBaxiCdkPoolSummary])

  useEffect(() => {
    if (!paypalBindingOpen || paypalBindingScope !== 'filtered') return
    let cancelled = false
    setPaypalFilteredEligibleLoading(true)
    const params = buildPaypalFilteredEligibleParams()
    apiFetch(`/accounts?${params}`)
      .then((data) => {
        if (cancelled) return
        setPaypalFilteredEligibleCount(Number(data?.total || 0))
      })
      .catch(() => {
        if (cancelled) return
        setPaypalFilteredEligibleCount(null)
      })
      .finally(() => {
        if (!cancelled) setPaypalFilteredEligibleLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [paypalBindingOpen, paypalBindingScope, buildPaypalFilteredEligibleParams])

  useEffect(() => {
    const data = accountsQuery.data
    if (!data) return
    const nextTotal = data.total || 0
    setAccounts((data.items || []).map(normalizeAccount))
    setTotal(nextTotal)

    const maxPage = Math.max(1, Math.ceil(nextTotal / accountsPageSize))
    if (currentPage > maxPage) {
      setCurrentPage(maxPage)
    }
  }, [accountsQuery.data, accountsPageSize, currentPage])

  const handleAccountsPageSizeChange = useCallback((pageSize: number) => {
    const nextPageSize = ACCOUNT_PAGE_SIZE_OPTIONS.includes(pageSize) ? pageSize : DEFAULT_ACCOUNTS_PAGE_SIZE
    setAccountsPageSize(nextPageSize)
    setCurrentPage(1)
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(ACCOUNTS_PAGE_SIZE_STORAGE_KEY, String(nextPageSize))
    }
  }, [])

  const applyFilterPreset = useCallback((preset: AccountFilterPreset, options?: { silent?: boolean }) => {
    const filters = normalizeAccountFilterPresetFilters(preset.filters)
    setSearch(filters.search)
    setDebouncedSearch(filters.search)
    setColumnFilters(filters.columnFilters)
    setFilterStatus(filters.status.join(','))
    setSubscriptionExpirySortOrder(filters.sortOrder)
    handleAccountsPageSizeChange(filters.pageSize)
    setCurrentPage(1)
    setSelectedRowKeys([])
    setSelectedAccountSnapshots({})
    setActiveFilterPresetId(preset.id)
    if (!options?.silent) {
      message.success(`已应用筛选组合：${preset.name}`)
    }
  }, [handleAccountsPageSizeChange])

  const clearFilterPreset = useCallback((options?: { silent?: boolean }) => {
    setSearch('')
    setDebouncedSearch('')
    setColumnFilters(EMPTY_ACCOUNT_FILTERS)
    setFilterStatus('')
    setSubscriptionExpirySortOrder('')
    setCurrentPage(1)
    setSelectedRowKeys([])
    setSelectedAccountSnapshots({})
    setActiveFilterPresetId('')
    if (!options?.silent) {
      message.success('已释放所有筛选条件')
    }
  }, [])

  const fillFilterFormFields = useCallback((filters?: AccountFilterPresetFilters) => {
    const normalized = normalizeAccountFilterPresetFilters(filters)
    filterPresetForm.setFieldsValue({
      search: normalized.search,
      status: normalized.status,
      authType: normalized.columnFilters.authType,
      subscriptionType: normalized.columnFilters.subscriptionType,
      accountValidity: normalized.columnFilters.accountValidity,
      codexState: normalized.columnFilters.codexState,
      sub2apiState: normalized.columnFilters.sub2apiState,
      oaipayState: normalized.columnFilters.oaipayState,
      ideaSubmitState: normalized.columnFilters.ideaSubmitState,
      sortOrder: normalized.sortOrder || undefined,
      pageSize: normalized.pageSize,
    })
  }, [filterPresetForm])

  const openCreateCurrentFilterPreset = useCallback(() => {
    setFilterPresetEditing(null)
    setFilterPresetEditorMode('create-current')
    filterPresetForm.setFieldsValue({
      name: '',
      description: buildAccountFilterPresetSummary(currentFilterPresetFilters),
      pinned: true,
    })
    fillFilterFormFields(currentFilterPresetFilters)
    setFilterPresetEditorOpen(true)
  }, [currentFilterPresetFilters, fillFilterFormFields, filterPresetForm])

  const openCopyFilterPreset = useCallback((preset: AccountFilterPreset) => {
    setFilterPresetEditing(preset)
    setFilterPresetEditorMode('copy-preset')
    filterPresetForm.setFieldsValue({
      name: `${preset.name} 副本`,
      description: preset.description || buildAccountFilterPresetSummary(preset.filters),
      pinned: true,
    })
    fillFilterFormFields(preset.filters)
    setFilterPresetEditorOpen(true)
  }, [fillFilterFormFields, filterPresetForm])

  const openEditFilterPresetMeta = useCallback((preset: AccountFilterPreset) => {
    setFilterPresetEditing(preset)
    setFilterPresetEditorMode('edit-meta')
    filterPresetForm.setFieldsValue({
      name: preset.name,
      description: preset.description || '',
      pinned: Boolean(preset.pinned),
    })
    fillFilterFormFields(preset.filters)
    setFilterPresetEditorOpen(true)
  }, [fillFilterFormFields, filterPresetForm])

  const saveFilterPresetForm = useCallback(async () => {
    const values = await filterPresetForm.validateFields()
    const name = String(values.name || '').trim()
    if (!name) {
      message.warning('请输入筛选组合名称')
      return
    }
    const editingPreset = filterPresetEditing
    if (filterPresetEditorMode === 'edit-meta' && !editingPreset) {
      message.error('未找到要编辑的筛选组合')
      return
    }
    const isEditMeta = filterPresetEditorMode === 'edit-meta'
    const filters = normalizeAccountFilterPresetFilters({
      search: values.search,
      status: values.status,
      columnFilters: {
        authType: values.authType,
        subscriptionType: values.subscriptionType,
        accountValidity: values.accountValidity,
        codexState: values.codexState,
        sub2apiState: values.sub2apiState,
        oaipayState: values.oaipayState,
        ideaSubmitState: values.ideaSubmitState,
      },
      sortOrder: values.sortOrder,
      pageSize: values.pageSize,
    })
    const body = {
      name,
      description: String(values.description || '').trim(),
      pinned: Boolean(values.pinned),
      filters,
    }
    setFilterPresetSaving(true)
    try {
      const endpoint = isEditMeta ? `/accounts/filter-presets/${editingPreset!.id}` : '/accounts/filter-presets'
      const data = await apiFetch(endpoint, {
        method: isEditMeta ? 'PUT' : 'POST',
        body: JSON.stringify(body),
      })
      const items = Array.isArray(data?.items) ? data.items : []
      setFilterPresets(items.map((item: any) => ({
        id: String(item?.id || ''),
        name: String(item?.name || ''),
        description: String(item?.description || ''),
        summary: String(item?.summary || ''),
        filters: normalizeAccountFilterPresetFilters(item?.filters),
        pinned: Boolean(item?.pinned),
        built_in: Boolean(item?.built_in),
        created_at: String(item?.created_at || ''),
        updated_at: String(item?.updated_at || ''),
      })).filter((item: AccountFilterPreset) => item.id && item.name))
      const saved = data?.item
      if (saved?.id) {
        setActiveFilterPresetId(String(saved.id))
      }
      setFilterPresetEditorOpen(false)
      message.success(isEditMeta ? '筛选组合已更新' : '筛选组合已保存')
    } catch (e: any) {
      message.error(e?.message || '保存筛选组合失败')
    } finally {
      setFilterPresetSaving(false)
    }
  }, [filterPresetEditing, filterPresetEditorMode, filterPresetForm])

  const overwritePresetWithCurrent = useCallback(async (targetPreset?: AccountFilterPreset | null) => {
    const target = targetPreset || activeFilterPreset
    if (!target) return
    setFilterPresetSaving(true)
    try {
      const data = await apiFetch(`/accounts/filter-presets/${target.id}`, {
        method: 'PUT',
        body: JSON.stringify({
          name: target.name,
          description: target.description || '',
          pinned: Boolean(target.pinned),
          filters: normalizeAccountFilterPresetFilters(currentFilterPresetFilters),
        }),
      })
      const items = Array.isArray(data?.items) ? data.items : []
      setFilterPresets(items.map((item: any) => ({
        id: String(item?.id || ''),
        name: String(item?.name || ''),
        description: String(item?.description || ''),
        summary: String(item?.summary || ''),
        filters: normalizeAccountFilterPresetFilters(item?.filters),
        pinned: Boolean(item?.pinned),
        built_in: Boolean(item?.built_in),
        created_at: String(item?.created_at || ''),
        updated_at: String(item?.updated_at || ''),
      })).filter((item: AccountFilterPreset) => item.id && item.name))
      message.success(`已用当前筛选覆盖组合「${target.name}」`)
    } catch (e: any) {
      message.error(e?.message || '覆盖筛选组合失败')
    } finally {
      setFilterPresetSaving(false)
    }
  }, [activeFilterPreset, currentFilterPresetFilters])

  const overwriteActiveFilterPreset = useCallback(() => overwritePresetWithCurrent(activeFilterPreset), [activeFilterPreset, overwritePresetWithCurrent])

  const deleteFilterPreset = useCallback(async (preset: AccountFilterPreset) => {
    setFilterPresetSaving(true)
    try {
      const data = await apiFetch(`/accounts/filter-presets/${preset.id}`, { method: 'DELETE' })
      const items = Array.isArray(data?.items) ? data.items : []
      setFilterPresets(items.map((item: any) => ({
        id: String(item?.id || ''),
        name: String(item?.name || ''),
        description: String(item?.description || ''),
        summary: String(item?.summary || ''),
        filters: normalizeAccountFilterPresetFilters(item?.filters),
        pinned: Boolean(item?.pinned),
        built_in: Boolean(item?.built_in),
        created_at: String(item?.created_at || ''),
        updated_at: String(item?.updated_at || ''),
      })).filter((item: AccountFilterPreset) => item.id && item.name))
      if (activeFilterPresetId === preset.id) setActiveFilterPresetId('')
      message.success('筛选组合已删除')
    } catch (e: any) {
      message.error(e?.message || '删除筛选组合失败')
    } finally {
      setFilterPresetSaving(false)
    }
  }, [activeFilterPresetId])

  useEffect(() => {
    if (!accounts.length) return
    setWatchingBaxiAccountIds((prev) => {
      const next = new Set(prev)
      let changed = false
      accounts.forEach((record) => {
        const accountId = Number(record?.id || 0)
        if (!Number.isFinite(accountId) || accountId <= 0) return
        if (isBaxiGptWatchTerminal(record)) {
          if (next.delete(accountId)) changed = true
          return
        }
        if (isBaxiGptPendingOrder(record) && !next.has(accountId)) {
          next.add(accountId)
          changed = true
        }
      })
      return changed ? next : prev
    })
  }, [accounts])

  useEffect(() => {
    const ids = new Set<number>()
    collectBaxiPollingAccountIdsFromTaskSnapshot(taskSnapshot).forEach((id) => ids.add(id))
    activeTasks.forEach((task: any) => {
      collectBaxiPollingAccountIdsFromTaskSnapshot(task).forEach((id) => ids.add(id))
    })
    if (ids.size === 0) return
    setWatchingBaxiAccountIds((prev) => {
      const next = new Set(prev)
      let changed = false
      ids.forEach((id) => {
        if (!next.has(id)) {
          next.add(id)
          changed = true
        }
      })
      return changed ? next : prev
    })
  }, [taskSnapshot, activeTasks])

  useEffect(() => {
    if (!pageVisible || !watchingBaxiAccountIdsKey) return
    let cancelled = false
    let timer: number | null = null
    const pull = async () => {
      const ids = watchingBaxiAccountIdsKey
        .split(',')
        .map((item: string) => Number(item))
        .filter((id: number) => Number.isFinite(id) && id > 0)
      if (ids.length === 0) return
      try {
        const data = await apiFetch('/accounts/snapshot', {
          method: 'POST',
          body: JSON.stringify({ ids }),
        })
        if (cancelled) return
        const items = Array.isArray(data?.items) ? data.items : []
        const requestedIds = ids
        const returnedIds = new Set<number>()
        items.forEach((item: any) => {
          const id = Number(item?.id || 0)
          if (Number.isFinite(id) && id > 0) returnedIds.add(id)
        })
        if (items.length > 0) {
          const byId = new Map<number, any>()
          items.forEach((item: any) => {
            const id = Number(item?.id || 0)
            if (Number.isFinite(id) && id > 0) byId.set(id, item)
          })
          setAccounts((prev) => prev.map((account) => {
            const item = byId.get(Number(account?.id || 0))
            return item ? mergeBaxiSnapshotIntoAccount(account, item) : account
          }))
          setWatchingBaxiAccountIds((prev) => {
            const next = new Set(prev)
            let changed = false
            requestedIds.forEach((id: number) => {
              if (!returnedIds.has(Number(id)) && next.delete(Number(id))) changed = true
            })
            items.forEach((item: any) => {
              const id = Number(item?.id || 0)
              if (!Number.isFinite(id) || id <= 0) return
              const patched = mergeBaxiSnapshotIntoAccount({ id, extra: {} }, item)
              if (isBaxiGptWatchTerminal(patched)) {
                if (next.delete(id)) changed = true
              }
            })
            return changed ? next : prev
          })
        } else {
          setWatchingBaxiAccountIds((prev) => {
            const next = new Set(prev)
            let changed = false
            requestedIds.forEach((id: number) => {
              if (next.delete(Number(id))) changed = true
            })
            return changed ? next : prev
          })
        }
      } catch {
        // 后端 snapshot 接口可能还没上线，或者字段临时缺失；账号页不要因此中断。
      } finally {
        if (!cancelled) {
          timer = window.setTimeout(pull, 4000)
        }
      }
    }
    void pull()
    return () => {
      cancelled = true
      if (timer != null) window.clearTimeout(timer)
    }
  }, [pageVisible, watchingBaxiAccountIdsKey])

  useEffect(() => {
    if (selectedRowKeys.length === 0) {
      setSelectedAccountSnapshots({})
      return
    }
    const currentAccountsById = new Map(accounts.map((account) => [String(account.id), account]))
    setSelectedAccountSnapshots((prev) => {
      const next: Record<string, any> = {}
      selectedRowKeys.forEach((key) => {
        const id = String(key)
        next[id] = currentAccountsById.get(id) || prev[id] || { id }
      })
      return next
    })
  }, [accounts, selectedRowKeys])

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

  const markAccountUsed = useCallback(async (accountId: number, used = true) => {
    if (!accountId) return
    await apiFetch(`/accounts/${accountId}/mark-used`, {
      method: 'POST',
      body: JSON.stringify({ used }),
    })
    setAccounts((prev) =>
      prev.map((item) => (
        item.id === accountId
          ? normalizeAccount({
              ...item,
              extra_json: JSON.stringify({ ...(item.extra || {}), manually_used: used }),
            })
          : item
      )),
    )
    if (detailAccount?.id === accountId) {
      setDetailAccount((prev: any) => prev ? normalizeAccount({
        ...prev,
        extra_json: JSON.stringify({ ...(prev.extra || {}), manually_used: used }),
      }) : prev)
    }
    if (actionAccount?.id === accountId) {
      setActionAccount((prev: any) => prev ? normalizeAccount({
        ...prev,
        extra_json: JSON.stringify({ ...(prev.extra || {}), manually_used: used }),
      }) : prev)
    }
  }, [detailAccount?.id, actionAccount?.id])

  const unmarkAccountUsed = useCallback(async (record: any) => {
    const accountId = Number(record?.id || 0)
    if (!accountId) return
    await markAccountUsed(accountId, false)
    message.success('已取消已使用标记')
  }, [markAccountUsed])

  const copyAccountSecret = useCallback(async (record: any, field: AccountSecretField, label: string) => {
    const accountId = Number(record?.id || 0)
    let value = ''
    try {
      if (accountId) {
        const data = await fetchAccountSecrets(accountId, [field])
        value = String(data?.secrets?.[field] || '')
      }
    } catch (e: any) {
      message.error(`读取${label}失败: ${e?.message || '未知错误'}`)
      return
    }
    if (!value) {
      value = legacyAccountSecret(record, field)
    }
    if (!value) {
      message.warning(`当前账号没有${label}`)
      return
    }
    const ok = await copyText(value)
    if (!ok) return
    if (field === 'access_token' && accountId) {
      setAccessTokenCopiedAccountIds((prev) => new Set(prev).add(accountId))
    }
  }, [])

  const copyAccessToken = useCallback(async (record: any) => {
    await copyAccountSecret(record, 'access_token', 'AT')
  }, [copyAccountSecret])

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
        const proxySettings = taskProxySettingsFromConfig(cfg, savedSettings)
        const savedEmail = window.localStorage.getItem('auto-chatgpt.manual_email_otp.email') || ''
        setRegisterMailProvider(provider)
        registerForm.setFieldsValue({
          count: Number(savedSettings.count || 1) || 1,
          concurrency: Number(savedSettings.concurrency || 1) || 1,
          register_delay_seconds: Number(savedSettings.register_delay_seconds || 0) || 0,
          ...proxySettings,
          mail_provider_override: String(savedSettings.mail_provider_override || '__global__'),
          email_api_lines: String(cfg.email_api_lines || '').trim(),
          email_api_poll_interval_seconds: cfg.email_api_poll_interval_seconds || 3,
          email_api_request_timeout_seconds: cfg.email_api_request_timeout_seconds || 15,
          email_api_gmail_dot_variant_enabled: cfg.email_api_gmail_dot_variant_enabled === '' ? true : parseBooleanConfigValue(cfg.email_api_gmail_dot_variant_enabled),
          email_api_gmail_variant_count: Number(cfg.email_api_gmail_variant_count || 2) || 2,
          email_api_gmail_variant_rules: cfg.email_api_gmail_variant_rules || 'all',
          email_api_gmail_plus_tag_template: cfg.email_api_gmail_plus_tag_template || 'r{rand}',
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
            savedSettings.chatgpt_save_registration_access_token_account
            ?? (cfg.chatgpt_save_registration_access_token_account === ''
              ? true
              : cfg.chatgpt_save_registration_access_token_account === undefined
                ? true
                : parseBooleanConfigValue(cfg.chatgpt_save_registration_access_token_account)),
          chatgpt_existing_account_login_route_enabled:
            savedSettings.chatgpt_existing_account_login_route_enabled
            ?? (cfg.chatgpt_existing_account_login_route_enabled === ''
              ? true
              : cfg.chatgpt_existing_account_login_route_enabled === undefined
                ? true
                : parseBooleanConfigValue(cfg.chatgpt_existing_account_login_route_enabled)),
          chatgpt_register_otp_wait_seconds:
            savedSettings.chatgpt_register_otp_wait_seconds ?? cfg.chatgpt_register_otp_wait_seconds ?? 120,
          chatgpt_register_otp_resend_wait_seconds:
            savedSettings.chatgpt_register_otp_resend_wait_seconds ?? cfg.chatgpt_register_otp_resend_wait_seconds ?? 90,
          chatgpt_register_otp_account_budget_seconds:
            savedSettings.chatgpt_register_otp_account_budget_seconds ?? cfg.chatgpt_register_otp_account_budget_seconds ?? 210,
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
          ...taskProxySettingsFromConfig({}, savedSettings),
          mail_provider_override: String(savedSettings.mail_provider_override || '__global__'),
          email_api_lines: '',
          email_api_poll_interval_seconds: 3,
          email_api_request_timeout_seconds: 15,
          email_api_gmail_dot_variant_enabled: true,
          email_api_gmail_variant_count: 2,
          email_api_gmail_variant_rules: 'all',
          email_api_gmail_plus_tag_template: 'r{rand}',
          email: String(savedSettings.email || savedEmail || '').trim(),
          login_password: '',
          chatgpt_existing_account_capture: savedSettings.chatgpt_existing_account_capture ?? false,
          chatgpt_enable_team_invite: savedSettings.chatgpt_enable_team_invite ?? false,
          chatgpt_team_invite_deferred_activation: savedSettings.chatgpt_team_invite_deferred_activation ?? false,
          chatgpt_capture_business_workspace: savedSettings.chatgpt_capture_business_workspace ?? false,
          chatgpt_capture_free_workspace: savedSettings.chatgpt_capture_free_workspace ?? true,
          chatgpt_save_registration_access_token_account: savedSettings.chatgpt_save_registration_access_token_account ?? true,
          chatgpt_existing_account_login_route_enabled: savedSettings.chatgpt_existing_account_login_route_enabled ?? true,
          chatgpt_register_otp_wait_seconds: savedSettings.chatgpt_register_otp_wait_seconds ?? 120,
          chatgpt_register_otp_resend_wait_seconds: savedSettings.chatgpt_register_otp_resend_wait_seconds ?? 90,
          chatgpt_register_otp_account_budget_seconds: savedSettings.chatgpt_register_otp_account_budget_seconds ?? 210,
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

  const canImportAccountToTeam = (record: any): boolean => {
    if (currentPlatform !== 'chatgpt') return false
    if (String(record?.workspace_scope || record?.extra?.chatgpt_workspace_scope || '').trim().toLowerCase() !== 'business') return false
    return hasAccountSecret(record, 'refresh_token') || hasAccountSecret(record, 'access_token') || hasAccountSecret(record, 'session_token')
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

  const getResumeAuthGlobalDefaults = async () => {
    const cfg = await loadConfigCache({ force: true })
    return {
      allow_phone_verification: parseBooleanConfigValue(cfg.chatgpt_resume_auth_allow_phone_verification),
    }
  }

  const handleResumeSubscriptionAuth = async (record: any, allowPhoneVerification?: boolean) => {
    setResumeAuthAccountId(record.id)
    try {
      const body: Record<string, unknown> = {
        account_id: Number(record.id),
      }
      if (typeof allowPhoneVerification === 'boolean') {
        body.allow_phone_verification = allowPhoneVerification
      }
      const res = await apiFetch('/tasks/chatgpt/resume-subscription-auth', {
        method: 'POST',
        body: JSON.stringify(body),
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

  const handleK12WorkspaceRecapture = async (record: any, params: Record<string, unknown> = {}) => {
    const accountId = Number(record?.id || 0)
    if (!accountId) {
      message.error('账号 ID 无效')
      return
    }
    try {
      const res = await apiFetch('/tasks/chatgpt/k12-workspace-recapture', {
        method: 'POST',
        body: JSON.stringify({
          account_id: accountId,
          ...params,
        }),
      })
      if (!res?.task_id) {
        throw new Error('任务创建失败：未返回 task_id')
      }
      const snapshot = await apiFetch(`/tasks/${res.task_id}`)
      setTaskModalMode('k12_recapture')
      setTaskModalAccount(record)
      setTaskId(res.task_id)
      setTaskSnapshot(snapshot)
      setRegisterModalOpen(true)
      setActiveTasksPanelOpen(true)
      void activeTasksQuery.refetch()
      message.success('K12 / Workspace 重跑任务已启动')
    } catch (e: any) {
      message.error(e?.message || 'K12 / Workspace 重跑任务创建失败')
    }
  }

  const openResumeAuthConfig = async (record: any) => {
    let defaults = { allow_phone_verification: false }
    try {
      defaults = await getResumeAuthGlobalDefaults()
    } catch (e: any) {
      message.warning(e?.message || '读取全局补抓 Auth 配置失败，已使用默认参数')
    }
    setResumeAuthConfigMode('single')
    setResumeAuthConfigAccount(record)
    setResumeAuthConfigScope('selected')
    resumeAuthConfigForm.setFieldsValue({
      allow_phone_verification: defaults.allow_phone_verification,
    })
    setResumeAuthConfigOpen(true)
  }

  const openBatchResumeAuthConfig = async (scope: 'selected' | 'filtered') => {
    let defaults = { allow_phone_verification: false }
    try {
      defaults = await getResumeAuthGlobalDefaults()
    } catch (e: any) {
      message.warning(e?.message || '读取全局补抓 Auth 配置失败，已使用默认参数')
    }
    setResumeAuthConfigMode('batch')
    setResumeAuthConfigAccount(null)
    setResumeAuthConfigScope(scope)
    resumeAuthConfigForm.setFieldsValue({
      allow_phone_verification: defaults.allow_phone_verification,
    })
    setResumeAuthConfigOpen(true)
  }

  const submitResumeAuthConfig = async () => {
    const values = await resumeAuthConfigForm.validateFields()
    const allowPhoneVerification = Boolean(values.allow_phone_verification)
    setResumeAuthConfigOpen(false)
    if (resumeAuthConfigMode === 'single') {
      if (!resumeAuthConfigAccount) return
      await handleResumeSubscriptionAuth(resumeAuthConfigAccount, allowPhoneVerification)
      return
    }
    await handleBatchResumeSubscriptionAuth(resumeAuthConfigScope, allowPhoneVerification)
  }

  const openBatchProbeStatusConfig = async (scope: 'selected' | 'all') => {
    const cfg = await loadConfigCache({ force: true }).catch(() => configCache || {})
    setBatchProbeStatusConfigScope(scope)
    batchProbeStatusConfigForm.resetFields()
    batchProbeStatusConfigForm.setFieldsValue({
      register_delay_seconds: 0,
      register_delay_max_seconds: 0,
      ...taskProxySettingsFromConfig(cfg || {}),
    })
    setBatchProbeStatusConfigOpen(true)
  }

  const submitBatchProbeStatusConfig = async () => {
    const values = await batchProbeStatusConfigForm.validateFields()
    validateTaskProxySettings(values)
    await saveTaskProxySettingsToConfig(values)
    await loadConfigCache({ force: true }).catch(() => null)
    setBatchProbeStatusConfigOpen(false)
    const customParams: Record<string, unknown> = {
      ...buildTaskProxyPayload(values),
      register_delay_seconds: Number(values.register_delay_seconds ?? 0),
      register_delay_max_seconds: Number(values.register_delay_max_seconds ?? 0),
      probe_delay_seconds: Number(values.register_delay_seconds ?? 0),
      probe_delay_max_seconds: Number(values.register_delay_max_seconds ?? 0),
      delay_seconds: Number(values.register_delay_seconds ?? 0),
      delay_max_seconds: Number(values.register_delay_max_seconds ?? 0),
    }
    await handleBatchStatusSync('probe', batchProbeStatusConfigScope, customParams)
  }

  const openBatchK12Recapture = async () => {
    const cfg = await loadConfigCache({ force: true }).catch(() => configCache || {})
    const scope: 'selected' | 'filtered' = selectedRowKeys.length > 0 ? 'selected' : 'filtered'
    setBatchK12RecaptureScope(scope)
    batchK12RecaptureForm.resetFields()
    batchK12RecaptureForm.setFieldsValue({
      scope,
      workspace_ids: '',
      save_all_spaces: true,
      strict_join: false,
      join_timeout_seconds: 60,
      join_retry_count: 2,
      post_join_poll_seconds: '3,8,15',
      delay_seconds: 0,
      delay_max_seconds: 0,
      ...taskProxySettingsFromConfig(cfg || {}, { proxy_mode: 'pool', proxy_failover: true }),
    })
    setBatchK12RecaptureOpen(true)
  }

  const submitBatchK12Recapture = async () => {
    const values = await batchK12RecaptureForm.validateFields()
    validateTaskProxySettings(values)
    await saveTaskProxySettingsToConfig(values)
    await loadConfigCache({ force: true }).catch(() => null)
    const scope = (values.scope === 'filtered' ? 'filtered' : 'selected') as 'selected' | 'filtered'
    const body: Record<string, unknown> = {
      params: {
        ...buildTaskProxyPayload(values),
        workspace_ids: String(values.workspace_ids || '').trim(),
        save_all_spaces: values.save_all_spaces !== false,
        strict_join: Boolean(values.strict_join),
        join_timeout_seconds: Number(values.join_timeout_seconds || 60),
        join_retry_count: Number(values.join_retry_count || 2),
        post_join_poll_seconds: String(values.post_join_poll_seconds || '3,8,15').trim(),
        delay_seconds: Number(values.delay_seconds || 0),
        delay_max_seconds: Number(values.delay_max_seconds || 0),
      },
    }
    const scopeLabel = scope === 'selected' ? '所选账号' : '当前筛选账号'
    const toastKey = `k12-recapture:${scope}`

    if (scope === 'selected') {
      const accountIds = Array.from(selectedRowKeys)
        .map((value) => Number(value))
        .filter((value) => Number.isInteger(value) && value > 0)
      if (accountIds.length === 0) {
        message.warning('请先选择要重新进入/导出 K12 的账号，或切换为当前筛选范围')
        return
      }
      body.account_ids = accountIds
    } else {
      body.all_filtered = true
      applyCurrentFiltersToBody(body)
    }

    setBatchK12RecaptureLoading(true)
    setBatchK12RecaptureOpen(false)
    message.loading({ content: `${scopeLabel} K12 / Workspace 重跑任务创建中...`, key: toastKey, duration: 0 })
    try {
      const result = await apiFetch('/tasks/chatgpt/k12-workspace-recapture/batch', {
        method: 'POST',
        body: JSON.stringify(body),
      })
      const eligible = Number(result?.eligible || 0)
      const skipped = Number(result?.skipped || 0)
      const missing = Number(result?.missing || 0)
      const taskIdFromResponse = String(result?.task_id || '').trim()
      if (!taskIdFromResponse) {
        message.info({
          content: `没有可执行 K12 重跑的账号。请求 ${result?.total_requested || 0} 个，跳过 ${skipped} 个${missing > 0 ? `，缺失 ${missing} 个` : ''}`,
          key: toastKey,
        })
        showBatchActionResult(`${scopeLabel} K12 / Workspace 重跑结果`, result)
        return
      }
      const snapshot = await apiFetch(`/tasks/${taskIdFromResponse}`)
      setTaskModalMode('k12_recapture')
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
        content: `${scopeLabel} K12 / Workspace 重跑任务已启动：可执行 ${eligible} 个，跳过 ${skipped} 个${missing > 0 ? `，缺失 ${missing} 个` : ''}`,
        key: toastKey,
      })
      showBatchActionResult(`${scopeLabel} K12 / Workspace 重跑结果`, result)
      await load()
    } catch (e: any) {
      message.error({ content: `K12 / Workspace 重跑失败: ${e.message}`, key: toastKey })
      setBatchK12RecaptureOpen(true)
    } finally {
      setBatchK12RecaptureLoading(false)
    }
  }

  const getResumeAuthScope = (): 'selected' | 'filtered' => (selectedRowKeys.length > 0 ? 'selected' : 'filtered')

  const getResumeAuthSelectedIds = () =>
    Array.from(selectedRowKeys)
      .map((value) => Number(value))
      .filter((value) => Number.isInteger(value) && value > 0)

  const buildResumeAuthMenuLabel = (useTemporaryConfig = false) => {
    const scope = getResumeAuthScope()
    const base = scope === 'selected'
      ? `补抓所选 Auth (${selectedRowKeys.length})`
      : `补抓当前筛选待补抓账号 (${total})`
    return useTemporaryConfig ? `${base}，临时配置` : base
  }

  const handleBatchResumeSubscriptionAuth = async (scope: 'selected' | 'filtered', allowPhoneVerification?: boolean) => {
    const toastKey = `resume-auth:${scope}:${allowPhoneVerification === true ? 'phone' : allowPhoneVerification === false ? 'no-phone' : 'global'}`
    const body: Record<string, unknown> = {}
    if (typeof allowPhoneVerification === 'boolean') {
      body.allow_phone_verification = allowPhoneVerification
    }
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
      applyCurrentFiltersToBody(body)
    }

    const loadingKey = `${scope}${allowPhoneVerification ? '_phone' : ''}` as 'selected' | 'filtered' | 'selected_phone' | 'filtered_phone'
    setBatchResumeAuthLoading(loadingKey)
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

  const handleBatchPaymentLink = async (options: { forceRefresh?: boolean } = {}) => {
    const forceRefresh = Boolean(options.forceRefresh)
    let defaults: Record<string, any> = {}
    try {
      const cfg = await loadConfigCache({ force: true })
      defaults = parseMaybeJsonObject(cfg.chatgpt_payment_link_defaults)
    } catch {
      defaults = {}
    }
    batchPaymentLinkConfigForm.setFieldsValue({
      payment_link_format: normalizePaymentLinkFormat(defaults.payment_link_format),
    })
    setBatchPaymentLinkForceRefresh(forceRefresh)
    setBatchPaymentLinkConfigOpen(true)
  }

  const submitBatchPaymentLinkConfig = async () => {
    const values = await batchPaymentLinkConfigForm.validateFields()
    const scope: 'selected' | 'filtered' = selectedRowKeys.length > 0 ? 'selected' : 'filtered'
    const forceRefresh = Boolean(batchPaymentLinkForceRefresh)
    const paymentLinkFormat = normalizePaymentLinkFormat(values.payment_link_format)
    const toastKey = `payment-link:${scope}:${forceRefresh ? 'force' : 'normal'}`
    const body: Record<string, unknown> = {
      skip_existing: !forceRefresh,
      force_refresh: forceRefresh,
      params: {
        payment_link_format: paymentLinkFormat,
      },
    }
    const actionLabel = forceRefresh ? '强制重新生成订阅链接' : '批量订阅链接'
    let requestedCount = total

    if (scope === 'selected') {
      const accountIds = Array.from(selectedRowKeys)
        .map((value) => Number(value))
        .filter((value) => Number.isInteger(value) && value > 0)
      if (accountIds.length === 0) {
        message.warning(`请先选择要${forceRefresh ? '强制重新生成' : '生成'}订阅链接的账号`)
        return
      }
      requestedCount = accountIds.length
      body.account_ids = accountIds
    } else {
      body.all_filtered = true
      applyCurrentFiltersToBody(body)
    }

    setBatchPaymentLinkLoading(true)
    setBatchPaymentLinkConfigOpen(false)
    message.loading({ content: `${actionLabel}任务创建中...`, key: toastKey, duration: 0 })
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
          content: `没有可${forceRefresh ? '重新生成' : '生成'}订阅链接的账号。请求 ${requestedCount} 个，跳过 ${skipped} 个${missing > 0 ? `，缺失 ${missing} 个` : ''}`,
          key: toastKey,
        })
        if (res && typeof res === 'object') {
          showBatchActionResult(`${actionLabel}结果`, res)
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
        content: `${actionLabel}任务已启动：${paymentLinkFormatLabel(paymentLinkFormat)}，可执行 ${eligible} 个，跳过 ${skipped} 个${missing > 0 ? `，缺失 ${missing} 个` : ''}`,
        key: toastKey,
      })
      showBatchActionResult(`${actionLabel}结果`, res)
    } catch (e: any) {
      message.error({ content: `${actionLabel}失败: ${e.message}`, key: toastKey })
      setBatchPaymentLinkConfigOpen(true)
    } finally {
      setBatchPaymentLinkLoading(false)
    }
  }

  const handleInvalidRecheck = async (record: any) => {
    const accountId = Number(record?.id || 0)
    if (!accountId) return
    const toastKey = `invalid-recheck:${accountId}`
    message.loading({ content: '失效测活任务创建中...', key: toastKey, duration: 0 })
    try {
      const res = await apiFetch('/tasks/chatgpt/invalid-recheck', {
        method: 'POST',
        body: JSON.stringify({ account_id: accountId }),
      })
      const taskIdFromResponse = String(res?.task_id || '').trim()
      if (taskIdFromResponse) {
        const snapshot = await apiFetch(`/tasks/${taskIdFromResponse}`)
        setTaskModalMode('resume_auth')
        setTaskModalAccount(record)
        setTaskId(taskIdFromResponse)
        setTaskSnapshot(snapshot)
        setRegisterModalOpen(true)
        setActiveTasksPanelOpen(true)
        void activeTasksQuery.refetch()
      }
      message.success({ content: '失效测活任务已启动', key: toastKey })
    } catch (e: any) {
      message.error({ content: `失效测活失败: ${e.message}`, key: toastKey })
    }
  }

  const handleBatchInvalidRecheck = async () => {
    const scope: 'selected' | 'filtered' = selectedRowKeys.length > 0 ? 'selected' : 'filtered'
    const toastKey = `invalid-recheck:${scope}`
    const body: Record<string, unknown> = {}
    let requestedCount = total

    if (scope === 'selected') {
      const accountIds = Array.from(selectedRowKeys)
        .map((value) => Number(value))
        .filter((value) => Number.isInteger(value) && value > 0)
      if (accountIds.length === 0) {
        message.warning('请先选择要测活的失效账号')
        return
      }
      requestedCount = accountIds.length
      body.account_ids = accountIds
    } else {
      body.all_filtered = true
      applyCurrentFiltersToBody(body)
    }

    setBatchInvalidRecheckLoading(true)
    message.loading({ content: '批量失效测活任务创建中...', key: toastKey, duration: 0 })
    try {
      const res = await apiFetch('/tasks/chatgpt/invalid-recheck/batch', {
        method: 'POST',
        body: JSON.stringify(body),
      })

      const eligible = Number(res?.eligible || 0)
      const skipped = Number(res?.skipped || 0)
      const missing = Number(res?.missing || 0)
      const taskIdFromResponse = String(res?.task_id || '').trim()

      if (!taskIdFromResponse) {
        message.info({
          content: `没有可执行失效测活的账号。请求 ${requestedCount} 个，跳过 ${skipped} 个${missing > 0 ? `，缺失 ${missing} 个` : ''}`,
          key: toastKey,
        })
        if (res && typeof res === 'object') {
          showBatchActionResult('批量失效测活结果', res)
        }
        return
      }

      const snapshot = await apiFetch(`/tasks/${taskIdFromResponse}`)
      setTaskModalMode('resume_auth')
      setTaskModalAccount(scope === 'selected' ? null : { email: `当前筛选 ${eligible} 个失效账号` })
      setTaskId(taskIdFromResponse)
      setTaskSnapshot(snapshot)
      setRegisterModalOpen(true)
      setActiveTasksPanelOpen(true)
      void activeTasksQuery.refetch()
      message.success({
        content: `批量失效测活任务已启动：可执行 ${eligible} 个，跳过 ${skipped} 个${missing > 0 ? `，缺失 ${missing} 个` : ''}`,
        key: toastKey,
      })
      showBatchActionResult('批量失效测活结果', res)
    } catch (e: any) {
      message.error({ content: `批量失效测活失败: ${e.message}`, key: toastKey })
    } finally {
      setBatchInvalidRecheckLoading(false)
    }
  }

  const openPhoneBindingTest = async () => {
    const cfg = await loadConfigCache({ force: true }).catch(() => configCache || {})
    const scope: 'selected' | 'filtered' = selectedRowKeys.length > 0 ? 'selected' : 'filtered'
    const savedSettings = loadPhoneBindingSettings()
    const proxySettings = taskProxySettingsFromConfig(cfg || {}, savedSettings as Partial<any>)
    setPhoneBindingTestScope(scope)
    phoneBindingTestForm.setFieldsValue({
      scope,
      ...savedSettings,
      ...proxySettings,
      phone_lines: '',
    })
    setPhoneBindingManualOpen(false)
    setPhoneBindingPrefixPickerOpen(false)
    setPhoneBindingAdvancedOpen(false)
    setPhoneBindingTestOpen(true)
    void loadPhonePoolSummary()
  }

  const submitPhoneBindingTest = async () => {
    const values = await phoneBindingTestForm.validateFields()
    validateTaskProxySettings(values)
    const scope = (values.scope === 'filtered' ? 'filtered' : 'selected') as 'selected' | 'filtered'
    const phoneLines = String(values.phone_lines || '').trim()
    // 手动粘贴优先：避免默认“使用手机号池”开启时忽略用户粘贴的号码。
    const phonePoolMode = normalizePhonePoolMode(values.phone_pool_mode, values as Record<string, unknown>)
    const selectedPrefixes = normalizeSelectedPrefixes(values.selected_prefixes)
    const prefixBindEnabled = !phoneLines && phonePoolMode === 'prefix_limited'
    const prefixSampleEnabled = !phoneLines && phonePoolMode === 'prefix_sample'
    const smsProbeOnly = Boolean(values.prefix_sms_probe_only || values.sms_probe_only)
    const rawPrefixSampleFilter = String(values.prefix_sample_filter || 'all')
    const prefixSampleFilter = rawPrefixSampleFilter === 'available' ? 'available' : rawPrefixSampleFilter === 'rejected' ? 'rejected' : 'all'
    const usePool = !phoneLines && (Boolean(values.use_pool) || prefixBindEnabled || prefixSampleEnabled)
    const normalizedValues = {
      ...values,
      phone_pool_mode: phonePoolMode,
      selected_prefixes: selectedPrefixes,
      prefix_sample_enabled: prefixSampleEnabled,
      prefix_sms_probe_only: smsProbeOnly,
      sms_probe_only: smsProbeOnly,
      reuse_phone_until_unusable: prefixSampleEnabled || smsProbeOnly ? false : Boolean(values.reuse_phone_until_unusable),
    }
    savePhoneBindingSettings(normalizedValues)
    await saveTaskProxySettingsToConfig(values)
    await loadConfigCache({ force: true }).catch(() => null)
    if (!usePool && !phoneLines) {
      message.warning('请粘贴手机号/API，或启用手机号池')
      return
    }
    if (prefixBindEnabled && selectedPrefixes.length === 0) {
      message.warning('限定号段绑定需要先选择至少一个号段')
      return
    }
    const body: Record<string, unknown> = {
      phone_lines: phoneLines,
      use_pool: usePool,
      prefix_bind_enabled: prefixBindEnabled,
      prefix_sample_enabled: prefixSampleEnabled,
      selected_prefixes: (prefixBindEnabled || prefixSampleEnabled) ? selectedPrefixes : [],
      prefix_sms_probe_only: smsProbeOnly,
      sms_probe_only: smsProbeOnly,
      prefix_sample_size: Number(values.prefix_sample_size) === 2 ? 2 : 1,
      prefix_sample_filter: prefixSampleFilter,
      timeout_seconds: Number(values.timeout_seconds || 180),
      poll_interval_seconds: Number(values.poll_interval_seconds || 5),
      max_resend_attempts: Number(values.max_resend_attempts || 0),
      resend_interval_seconds: Number(values.resend_interval_seconds || 0),
      account_interval_seconds: Number(values.account_interval_seconds || 60),
      reuse_phone_until_unusable: prefixSampleEnabled || smsProbeOnly ? false : Boolean(values.reuse_phone_until_unusable),
      ...buildTaskProxyPayload(values),
    }
    let requestedAccounts = total
    if (scope === 'selected') {
      const accountIds = getResumeAuthSelectedIds()
      if (accountIds.length === 0) {
        message.warning('请先选择用于手机号绑定的账号，或切换为当前筛选范围')
        return
      }
      requestedAccounts = accountIds.length
      body.account_ids = accountIds
    } else {
      body.all_filtered = true
      applyCurrentFiltersToBody(body)
    }

    const toastKey = `phone-binding-test:${scope}`
    setPhoneBindingTestLoading(true)
    message.loading({ content: '手机号绑定任务创建中...', key: toastKey, duration: 0 })
    try {
      const res = await apiFetch('/tasks/chatgpt/phone-binding-test', {
        method: 'POST',
        body: JSON.stringify(body),
      })
      const taskIdFromResponse = String(res?.task_id || '').trim()
      const eligible = Number(res?.eligible_accounts || 0)
      const phoneCount = Number(res?.phone_count || 0)
      const prefixSample = res?.prefix_sample && typeof res.prefix_sample === 'object' ? res.prefix_sample : null
      const prefixBind = res?.prefix_bind && typeof res.prefix_bind === 'object' ? res.prefix_bind : null
      const smsProbeOnly = Boolean(res?.sms_probe_only || prefixSample?.sms_probe_only || prefixBind?.sms_probe_only)
      const parseErrors = Array.isArray(res?.parse_errors) ? res.parse_errors : []

      if (!taskIdFromResponse) {
        message.info({
          content: `没有可用于绑定的账号。请求 ${requestedAccounts} 个账号，待用手机号 ${phoneCount} 个`,
          key: toastKey,
        })
        if (res && typeof res === 'object') {
          showBatchActionResult('手机号绑定结果', res)
        }
        return
      }

      const snapshot = await apiFetch(`/tasks/${taskIdFromResponse}`)
      setPhoneBindingTestOpen(false)
      setTaskModalMode('resume_auth')
      setTaskModalAccount({ email: `手机号绑定：${phoneCount} 个号码 / ${eligible} 个账号` })
      setTaskId(taskIdFromResponse)
      setTaskSnapshot(snapshot)
      setRegisterModalOpen(true)
      setActiveTasksPanelOpen(true)
      void activeTasksQuery.refetch()
      message.success({
        content: prefixBind?.enabled
          ? `限定号段绑定已启动：${Number(prefixBind.prefix_count || 0)} 个号段，${phoneCount} 个号码，${eligible} 个账号${smsProbeOnly ? '，仅测发码/收码' : ''}`
          : prefixSample?.enabled
            ? `号段抽样已启动：${Array.isArray(prefixSample.requested_prefixes) && prefixSample.requested_prefixes.length > 0 ? '指定号段，' : String(prefixSample.filter || 'all') === 'rejected' ? '仅不可用号段，' : ''}${Number(prefixSample.prefix_count || 0)} 个号段，${phoneCount} 个号码，${eligible} 个账号${smsProbeOnly ? '，仅测发码/收码' : ''}`
            : `${smsProbeOnly ? '手机号发码/收码探测已启动' : '手机号绑定已启动'}：${phoneCount} 个号码，${eligible} 个账号${parseErrors.length > 0 ? `，解析跳过 ${parseErrors.length} 行` : ''}`,
        key: toastKey,
      })
      if (parseErrors.length > 0) {
        showBatchActionResult('临时号码解析结果', { items: parseErrors, total: parseErrors.length })
      }
    } catch (e: any) {
      message.error({ content: `手机号绑定失败: ${e.message}`, key: toastKey })
    } finally {
      setPhoneBindingTestLoading(false)
    }
  }

  const openBaxiCdkSubmit = () => {
    const scope: 'selected' | 'filtered' = selectedRowKeys.length > 0 ? 'selected' : 'filtered'
    const savedSettings = loadBaxiGptCdkSettings()
    setBaxiCdkSubmitScope(scope)
    baxiCdkSubmitForm.setFieldsValue({
      scope,
      ...savedSettings,
      code_lines: '',
      cdk_ids: [],
      target_success_count: 0,
    })
    setBaxiCdkManualOpen(false)
    setBaxiCdkAdvancedOpen(false)
    setBaxiCdkSubmitOpen(true)
    void loadBaxiCdkPoolSummary()
    void loadBaxiCdkPoolItems()
  }

  const saveBaxiCdkManualCodesToPool = async () => {
    const codeLines = String(baxiCdkSubmitForm.getFieldValue('code_lines') || '').trim()
    if (!codeLines) {
      message.warning('请先粘贴要保存的卡密')
      return
    }
    setBaxiCdkSavingToPool(true)
    try {
      const result = await apiFetch('/baxigpt-cdk-pool/import', {
        method: 'POST',
        body: JSON.stringify({ text: codeLines }),
      })
      const importedItems = Array.isArray(result?.items) ? result.items : []
      const importedIds = normalizeBaxiCdkIdList(importedItems.map((item: any) => item?.id))
      const jobTotal = Number(result?.query_job?.total || 0)
      message.success(
        `已保存到卡密池：新增 ${Number(result?.added || 0)}，更新 ${Number(result?.updated || 0)}，跳过 ${Number(result?.skipped || 0)}`
        + (jobTotal > 0 ? `；已开始后台校验 ${jobTotal} 个` : ''),
      )
      baxiCdkSubmitForm.setFieldsValue({
        code_lines: '',
        use_pool: true,
        cdk_ids: importedIds,
      })
      setBaxiCdkManualOpen(false)
      await Promise.all([loadBaxiCdkPoolSummary(false), loadBaxiCdkPoolItems(false)])
      if (Array.isArray(result?.errors) && result.errors.length > 0) {
        showBatchActionResult('卡密解析结果', { items: result.errors, total: result.errors.length })
      }
    } catch (e: any) {
      message.error(e?.message || '保存卡密失败')
    } finally {
      setBaxiCdkSavingToPool(false)
    }
  }

  const submitBaxiCdkSubmit = async () => {
    let values
    try {
      values = await baxiCdkSubmitForm.validateFields()
    } catch (e) {
      console.error('Validation failed:', e)
      message.error('表单参数有误，请检查（展开高级参数查看详细错误）')
      return
    }
    saveBaxiGptCdkSettings(values)
    const scope = (values.scope === 'filtered' ? 'filtered' : 'selected') as 'selected' | 'filtered'
    const codeLines = String(values.code_lines || '').trim()
    const usePool = !codeLines && Boolean(values.use_pool)
    const selectedCdkIds = normalizeBaxiCdkIdList(values.cdk_ids)
    if (!usePool && !codeLines) {
      message.warning('请粘贴卡密，或启用卡密池')
      return
    }
    const body: Record<string, unknown> = {
      code_lines: codeLines,
      use_pool: usePool,
      precheck: Boolean(values.precheck),
      failure_continue: Boolean(values.failure_continue),
      submit_interval_seconds: Number(values.submit_interval_seconds || 0),
      auto_poll_status: values.auto_poll_status !== false,
      status_poll_interval_seconds: Number(values.status_poll_interval_seconds || 5),
      status_poll_timeout_seconds: Number(values.status_poll_timeout_seconds || 1800),
      target_success_count: Math.max(Number(values.target_success_count || 0), 0),
    }
    if (usePool && selectedCdkIds.length > 0) body.cdk_ids = selectedCdkIds
    let requestedAccounts = total
    if (scope === 'selected') {
      const accountIds = getResumeAuthSelectedIds()
      if (accountIds.length === 0) {
        message.warning('请先选择用于 idea批量提交的账号，或切换为当前筛选范围')
        return
      }
      requestedAccounts = accountIds.length
      body.account_ids = accountIds
    } else {
      body.all_filtered = true
      applyCurrentFiltersToBody(body)
    }

    const toastKey = `baxigpt-cdk-submit:${scope}`
    setBaxiCdkSubmitLoading(true)
    message.loading({ content: 'idea批量提交任务创建中...', key: toastKey, duration: 0 })
    try {
      const res = await apiFetch('/tasks/chatgpt/baxigpt-cdk-submit', {
        method: 'POST',
        body: JSON.stringify(body),
      })
      const taskIdFromResponse = String(res?.task_id || '').trim()
      const pairCount = Number(res?.pair_count || 0)
      const eligible = Number(res?.eligible_accounts || 0)
      const availableCodes = Number(res?.available_codes || 0)
      const spareCodes = Number(res?.spare_codes || 0)
      const targetSuccess = Number(res?.effective_target_success_count || 0)
      const importInfo = res?.cdk_pool_import && typeof res.cdk_pool_import === 'object' ? res.cdk_pool_import : {}
      const importErrors = Array.isArray(importInfo?.errors) ? importInfo.errors : []

      if (!taskIdFromResponse) {
        message.info({
          content: `没有可提交的卡密/账号配对。请求 ${requestedAccounts} 个账号，可用卡密 ${availableCodes} 个`,
          key: toastKey,
        })
        if (res && typeof res === 'object') {
          showBatchActionResult('idea批量提交结果', res)
        }
        await loadBaxiCdkPoolSummary()
        return
      }

      const snapshot = await apiFetch(`/tasks/${taskIdFromResponse}`)
      setBaxiCdkSubmitOpen(false)
      setTaskModalMode('baxigpt_cdk')
      setTaskModalAccount({ email: targetSuccess > 0 ? `idea批量提交：目标成功 ${targetSuccess} / 候选 ${pairCount}` : `idea批量提交：${pairCount} 对 / 库存余 ${spareCodes}` })
      setTaskId(taskIdFromResponse)
      setTaskSnapshot(snapshot)
      setRegisterModalOpen(true)
      setActiveTasksPanelOpen(true)
      void activeTasksQuery.refetch()
      void loadBaxiCdkPoolSummary()
      message.success({
        content: `idea批量提交已启动：${pairCount} 个候选配对${targetSuccess > 0 ? `，目标成功 ${targetSuccess} 个` : ''}，候选账号 ${eligible} 个，可用卡密 ${availableCodes} 个${spareCodes > 0 ? `，剩余入库 ${spareCodes} 个` : ''}${importErrors.length > 0 ? `，解析跳过 ${importErrors.length} 行` : ''}`,
        key: toastKey,
      })
      if (importErrors.length > 0) {
        showBatchActionResult('卡密解析结果', { items: importErrors, total: importErrors.length })
      }
    } catch (e: any) {
      message.error({ content: `idea批量提交失败: ${e.message}`, key: toastKey })
    } finally {
      setBaxiCdkSubmitLoading(false)
    }
  }

  const getPaypalBindingSelectedItems = () =>
    Array.from(selectedRowKeys)
      .map((key) => {
        const id = String(key)
        return selectedAccountSnapshots[id] || accounts.find((account) => String(account.id) === id) || { id }
      })


  const getPaypalBindingSelectedIds = () =>
    getPaypalBindingSelectedItems()
      .filter((account) => isPaypalBindingEligibleAccount(account))
      .map((account) => Number(account?.id || 0))
      .filter((value) => Number.isInteger(value) && value > 0)

  const openPaypalBinding = () => {
    const scope: 'selected' | 'filtered' = selectedRowKeys.length > 0 ? 'selected' : 'filtered'
    const savedSettings = loadPaypalBindingSettings()
    setPaypalBindingScope(scope)
    setPaypalFilteredEligibleCount(null)
    paypalBindingForm.setFieldsValue({
      scope,
      ...savedSettings,
    })
    setPaypalBindingOpen(true)
  }

  const submitPaypalBinding = async () => {
    const values = await paypalBindingForm.validateFields()
    savePaypalBindingSettings(values)
    const scope = (values.scope === 'filtered' ? 'filtered' : 'selected') as 'selected' | 'filtered'
    const body: Record<string, unknown> = {
      base_url: String(values.base_url || DEFAULT_PAYPAL_BINDING_SETTINGS.base_url).trim(),
      proxy: String(values.proxy || '').trim(),
      proxy_jp: String(values.proxy_jp || '').trim(),
      phone: String(values.phone || '').trim(),
      paypal_email: String(values.paypal_email || '').trim(),
      sms_api: String(values.sms_api || '').trim(),
      sms_api_test_mode: Boolean(values.sms_api_test_mode),
      otp_timeout: Number(values.otp_timeout || DEFAULT_PAYPAL_BINDING_SETTINGS.otp_timeout),
      pplink_retry: Number(values.pplink_retry || DEFAULT_PAYPAL_BINDING_SETTINGS.pplink_retry),
      timeout: Number(values.timeout || DEFAULT_PAYPAL_BINDING_SETTINGS.timeout),
      event_timeout: Number(values.event_timeout || DEFAULT_PAYPAL_BINDING_SETTINGS.event_timeout),
      account_interval_seconds: Number(values.account_interval_seconds || 0),
      failure_continue: values.failure_continue !== false,
    }
    if (scope === 'selected') {
      const accountIds = getPaypalBindingSelectedIds()
      if (accountIds.length === 0) {
        message.warning('当前选中账号里没有未订阅且有效的账号，请重新选择或切换为当前筛选范围')
        return
      }
      body.account_ids = accountIds
    } else {
      body.all_filtered = true
      applyCurrentFiltersToBody(body)
      applyPaypalBindingEligibilityFilters(body)
    }

    const toastKey = `paypal-binding:${scope}`
    setPaypalBindingLoading(true)
    message.loading({ content: 'PayPal绑定任务创建中...', key: toastKey, duration: 0 })
    try {
      const res = await apiFetch('/tasks/chatgpt/paypal-bind', {
        method: 'POST',
        body: JSON.stringify(body),
      })
      const taskIdFromResponse = String(res?.task_id || '').trim()
      const eligible = Number(res?.eligible_accounts || 0)
      const skipped = Array.isArray(res?.skipped_accounts) ? res.skipped_accounts.length : 0
      const missing = Array.isArray(res?.missing_ids) ? res.missing_ids.length : 0

      if (!taskIdFromResponse) {
        message.info({
          content: `没有可提交 PayPal 绑定的账号。符合条件 ${eligible} 个，跳过 ${skipped} 个${missing > 0 ? `，缺失 ${missing} 个` : ''}`,
          key: toastKey,
        })
        if (res && typeof res === 'object') {
          showBatchActionResult('PayPal绑定结果', res)
        }
        return
      }

      const snapshot = await apiFetch(`/tasks/${taskIdFromResponse}`)
      setPaypalBindingOpen(false)
      setTaskModalMode('paypal_bind')
      setTaskModalAccount({ email: `PayPal绑定：${eligible} 个账号` })
      setTaskId(taskIdFromResponse)
      setTaskSnapshot(snapshot)
      setRegisterModalOpen(true)
      setActiveTasksPanelOpen(true)
      void activeTasksQuery.refetch()
      message.success({
        content: `PayPal绑定已启动：可执行 ${eligible} 个，跳过 ${skipped} 个${missing > 0 ? `，缺失 ${missing} 个` : ''}`,
        key: toastKey,
      })
      showBatchActionResult('PayPal绑定任务', res)
    } catch (e: any) {
      message.error({ content: `PayPal绑定失败: ${e.message}`, key: toastKey })
    } finally {
      setPaypalBindingLoading(false)
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
    setSelectedAccountSnapshots({})
    load()
  }

  const handleDeleteAccount = async (record: any) => {
    const accountId = Number(record?.id || 0)
    if (!accountId) return
    const label = String(record?.email || accountId)
    await apiFetch(`/accounts/${accountId}`, { method: 'DELETE' })
    message.success(`已删除账号：${label}`)
    setSelectedRowKeys((keys) => keys.filter((key) => Number(key) !== accountId))
    setSelectedAccountSnapshots((prev) => {
      const next = { ...prev }
      delete next[String(accountId)]
      return next
    })
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
    const recognizedCodes = normalizeGopayRecognizedCountryCodes(otpSettings.recognized_country_codes)
    setBatchGopayOtpAutoResendDelay(normalizeGopayOtpAutoResendDelay(otpSettings.otp_auto_resend_delay_seconds))
    setBatchGopayRecognizedCountryCodes(recognizedCodes)
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
    setBatchGopayPhoneCountryCode(DEFAULT_GOPAY_PHONE_COUNTRY_CODE)
    setBatchGopayPhoneNumber('')
    setBatchGopayRecognizedCountryCodes([DEFAULT_GOPAY_PHONE_COUNTRY_CODE])
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
    const { phone_country_code, phone_number } = splitGopayPhoneInput(
      batchGopayPhoneCountryCode,
      batchGopayPhoneNumber,
      batchGopayRecognizedCountryCodes,
    )
    setBatchGopayPhoneCountryCode(phone_country_code)
    setBatchGopayPhoneNumber(phone_number)
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

  const updateBatchGopayPhoneNumberInput = (value: string) => {
    const normalizedPhone = splitGopayPhoneInput(
      batchGopayPhoneCountryCode,
      value,
      batchGopayRecognizedCountryCodes,
    )
    setBatchGopayPhoneCountryCode(normalizedPhone.phone_country_code)
    setBatchGopayPhoneNumber(normalizedPhone.phone_number)
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
      setSelectedAccountSnapshots({})
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
        proxy_mode: String(values.proxy_mode || 'pool'),
        proxy: String(values.proxy || '').trim(),
        proxy_country_code: String(values.proxy_country_code || '').trim().toUpperCase(),
        proxy_failover: Boolean(values.proxy_failover),
        proxy_max_candidates: Number(values.proxy_max_candidates || 5) || 5,
        proxy_min_score: Number(values.proxy_min_score || 50) || 50,
        mail_provider_override: String(values.mail_provider_override || '__global__'),
      email: String(values.email || '').trim(),
      chatgpt_existing_account_capture: Boolean(values.chatgpt_existing_account_capture),
      chatgpt_enable_team_invite: Boolean(values.chatgpt_enable_team_invite),
      chatgpt_team_invite_deferred_activation: Boolean(values.chatgpt_team_invite_deferred_activation),
      chatgpt_capture_free_workspace:
        values.chatgpt_capture_free_workspace === undefined ? true : Boolean(values.chatgpt_capture_free_workspace),
      chatgpt_capture_business_workspace:
        values.chatgpt_capture_business_workspace === undefined ? false : Boolean(values.chatgpt_capture_business_workspace),
      chatgpt_save_registration_access_token_account:
        values.chatgpt_save_registration_access_token_account === undefined
          ? true
          : Boolean(values.chatgpt_save_registration_access_token_account),
      chatgpt_existing_account_login_route_enabled:
        values.chatgpt_existing_account_login_route_enabled === undefined
          ? true
          : Boolean(values.chatgpt_existing_account_login_route_enabled),
      chatgpt_register_otp_wait_seconds: Number(values.chatgpt_register_otp_wait_seconds || 120) || 120,
      chatgpt_register_otp_resend_wait_seconds: Number(values.chatgpt_register_otp_resend_wait_seconds || 90) || 90,
      chatgpt_register_otp_account_budget_seconds: Number(values.chatgpt_register_otp_account_budget_seconds || 210) || 210,
    }

    setRegisterSettingsSaving(true)
    try {
      validateTaskProxySettings(settingsPayload)
      saveRegisterFormSettings(currentPlatform, settingsPayload)
      await saveTaskProxySettingsToConfig(settingsPayload)
      await loadConfigCache({ force: true }).catch(() => null)
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
        email_api_lines: resolvedMailProvider === 'email_api' ? String(values.email_api_lines || cfg.email_api_lines || '').trim() : undefined,
        email_api_poll_interval_seconds: cfg.email_api_poll_interval_seconds || values.email_api_poll_interval_seconds || 3,
        email_api_request_timeout_seconds: cfg.email_api_request_timeout_seconds || values.email_api_request_timeout_seconds || 15,
        email_api_gmail_dot_variant_enabled: parseBooleanConfigValue(
          values.email_api_gmail_dot_variant_enabled ?? cfg.email_api_gmail_dot_variant_enabled ?? true,
        ),
        email_api_gmail_variant_count: Number(values.email_api_gmail_variant_count ?? cfg.email_api_gmail_variant_count ?? 2) || 2,
        email_api_gmail_variant_rules: String(values.email_api_gmail_variant_rules ?? cfg.email_api_gmail_variant_rules ?? 'all').trim() || 'all',
        email_api_gmail_plus_tag_template: String(values.email_api_gmail_plus_tag_template ?? cfg.email_api_gmail_plus_tag_template ?? 'r{rand}').trim() || 'r{rand}',
        email_api_default_scheme: cfg.email_api_default_scheme || 'https',
        email_api_use_all_identities: resolvedMailProvider === 'email_api' ? true : undefined,
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
            ? (values.chatgpt_save_registration_access_token_account === undefined
              ? true
              : Boolean(values.chatgpt_save_registration_access_token_account))
            : undefined,
        chatgpt_existing_account_login_route_enabled:
          currentPlatform === 'chatgpt'
            ? (values.chatgpt_existing_account_login_route_enabled === undefined
              ? true
              : Boolean(values.chatgpt_existing_account_login_route_enabled))
            : undefined,
        chatgpt_register_otp_wait_seconds:
          currentPlatform === 'chatgpt' ? values.chatgpt_register_otp_wait_seconds : undefined,
        chatgpt_register_otp_resend_wait_seconds:
          currentPlatform === 'chatgpt' ? values.chatgpt_register_otp_resend_wait_seconds : undefined,
        chatgpt_register_otp_account_budget_seconds:
          currentPlatform === 'chatgpt' ? values.chatgpt_register_otp_account_budget_seconds : undefined,
        ...(currentPlatform === 'chatgpt' ? buildChatGPTK12Payload(values) : {}),
      }
      const chatgptRegistrationRequestAdapter =
        buildChatGPTRegistrationRequestAdapter(
          currentPlatform,
          chatgptRegistrationMode,
        )
      const adaptedRegisterExtra = chatgptRegistrationRequestAdapter
        ? chatgptRegistrationRequestAdapter.extendExtra(registerExtra)
        : registerExtra

      if (resolvedMailProvider === 'email_api' && currentPlatform === 'chatgpt') {
        const rawLines = String(values.email_api_lines || cfg.email_api_lines || '').trim()
        if (!rawLines) {
          throw new Error('邮箱验证码 API 模式必须填写 email----api 行')
        }
      }

      if (resolvedMailProvider === 'manual_email_otp' && currentPlatform === 'chatgpt') {
        const normalizedEmail = String(values.email || '').trim()
        if (!normalizedEmail) {
          throw new Error('手动邮箱模式必须填写邮箱地址')
        }
        window.localStorage.setItem('auto-chatgpt.manual_email_otp.email', normalizedEmail)
      }

      validateTaskProxySettings(values)
      saveRegisterFormSettings(currentPlatform, {
        count: Number(values.count || 1) || 1,
        concurrency: Number(values.concurrency || 1) || 1,
        register_delay_seconds: Number(values.register_delay_seconds || 0) || 0,
        proxy_mode: String(values.proxy_mode || 'pool'),
        proxy: String(values.proxy || '').trim(),
        proxy_country_code: String(values.proxy_country_code || '').trim().toUpperCase(),
        proxy_failover: Boolean(values.proxy_failover),
        proxy_max_candidates: Number(values.proxy_max_candidates || 5) || 5,
        proxy_min_score: Number(values.proxy_min_score || 50) || 50,
        mail_provider_override: selectedProviderOverride || '__global__',
        email: String(values.email || '').trim(),
        chatgpt_existing_account_capture: Boolean(values.chatgpt_existing_account_capture),
        chatgpt_enable_team_invite: Boolean(values.chatgpt_enable_team_invite),
        chatgpt_team_invite_deferred_activation: Boolean(values.chatgpt_team_invite_deferred_activation),
        chatgpt_capture_free_workspace: Boolean(values.chatgpt_capture_free_workspace),
        chatgpt_capture_business_workspace:
          values.chatgpt_capture_business_workspace === undefined ? false : Boolean(values.chatgpt_capture_business_workspace),
        chatgpt_save_registration_access_token_account:
          values.chatgpt_save_registration_access_token_account === undefined
            ? true
            : Boolean(values.chatgpt_save_registration_access_token_account),
        chatgpt_existing_account_login_route_enabled:
          values.chatgpt_existing_account_login_route_enabled === undefined
            ? true
            : Boolean(values.chatgpt_existing_account_login_route_enabled),
        chatgpt_register_otp_wait_seconds: Number(values.chatgpt_register_otp_wait_seconds || 120) || 120,
        chatgpt_register_otp_resend_wait_seconds: Number(values.chatgpt_register_otp_resend_wait_seconds || 90) || 90,
        chatgpt_register_otp_account_budget_seconds: Number(values.chatgpt_register_otp_account_budget_seconds || 210) || 210,
      })

      await saveTaskProxySettingsToConfig(values)
      await loadConfigCache({ force: true }).catch(() => null)
      const proxyPayload = buildTaskProxyPayload(values)

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
          ...proxyPayload,
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
    const payload = { ...values }
    if (Object.prototype.hasOwnProperty.call(payload, 'token')) {
      const nextToken = String(payload.token || '').trim()
      if (nextToken) {
        payload.token = nextToken
      } else {
        delete payload.token
      }
    }
    await apiFetch(`/accounts/${detailAccount.id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
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

  const handleBackfill = async (
    destination: 'cliproxyapi' | 'sub2api' | 'oaipay',
    mode: 'pending' | 'selected',
    oaipayOptions?: { categoryMode?: 'auto' | 'manual'; categoryId?: number; fallbackCategoryId?: number },
  ) => {
    if (currentPlatform !== 'chatgpt') return

    const destinationLabel = destination === 'oaipay' ? 'OAIPay' : destination === 'sub2api' ? 'Sub2API' : 'CLIProxyAPI'
    const loadingKey = `${destination}_${mode}` as typeof backfillLoading
    const actionLabel = mode === 'selected' ? `所选账号补传到 ${destinationLabel}` : `${destinationLabel} 待补传处理`
    const toastKey = `backfill:${loadingKey}`

    setBackfillLoading(loadingKey)
    message.loading({ content: `${actionLabel}进行中...`, key: toastKey, duration: 0 })
    try {
      let result: any

      if (destination === 'sub2api' || destination === 'oaipay') {
        const isSub2Api = destination === 'sub2api'
        const body: Record<string, unknown> = {
          params: {},
        }
        if (!isSub2Api) {
          const categoryMode = oaipayOptions?.categoryMode || 'auto'
          body.category_mode = categoryMode
          if (categoryMode === 'manual') {
            if (oaipayOptions?.categoryId !== undefined) body.category_id = oaipayOptions.categoryId
          } else if (oaipayOptions?.fallbackCategoryId !== undefined) {
            body.fallback_category_id = oaipayOptions.fallbackCategoryId
          }
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
          body.all_filtered = true
          applyCurrentFiltersToBody(body)

          if (isSub2Api) {
            const pendingSub2ApiStates = ['unknown', 'not_found', 'cross_workspace_only', 'deleted_exact_match']
            body.sub2api_state = pendingSub2ApiStates.join(',')
          } else {
            const pendingOaipayStates = ['unknown', 'not_found', 'cross_workspace_only', 'deleted_exact_match']
            body.oaipay_state = pendingOaipayStates.join(',')
          }
        }

        const endpoint = isSub2Api ? '/tasks/chatgpt/sub2api-upload/batch' : '/tasks/chatgpt/oaipay-upload/batch'
        const taskResult = await apiFetch(endpoint, {
          method: 'POST',
          body: JSON.stringify(body),
        })
        const startedTaskId = String(taskResult?.task_id || '').trim()
        if (!startedTaskId) {
          message.info({ content: '没有可处理的账号', key: toastKey })
          showBatchActionResult(`${actionLabel}结果`, taskResult)
          return
        }

        setTaskModalMode(isSub2Api ? 'sub2api_upload' : 'oaipay_upload')
        setTaskModalAccount(mode === 'selected' ? null : { email: `当前筛选 ${Number(taskResult?.eligible || 0)} 个待补传账号` })
        setTaskId(startedTaskId)
        setTaskSnapshot({
          id: startedTaskId,
          task_id: startedTaskId,
          status: 'pending',
          source: isSub2Api ? 'batch_sub2api_upload' : 'batch_oaipay_upload',
          progress: `0/${Number(taskResult?.eligible || 0) || 1}`,
          meta: {
            eligible: Number(taskResult?.eligible || 0),
            matched: Number(taskResult?.matched || 0),
            total_requested: Number(taskResult?.total_requested || 0),
          },
        })
        setRegisterModalOpen(true)
        setActiveTasksPanelOpen(true)
        void activeTasksQuery.refetch()
        message.success({ content: `${actionLabel}任务已启动`, key: toastKey })
        return
      } else {
        const body: Record<string, unknown> = {
          platforms: ['chatgpt'],
          destination,
        }
        if (oaipayOptions?.categoryId) {
          body.category_id = oaipayOptions.categoryId
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
          applyCurrentFiltersToBody(body)
        }

        result = await apiFetch('/integrations/backfill', {
          method: 'POST',
          body: JSON.stringify(body),
        })
      }

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

  const handleBatchStatusSync = async (
    kind: 'probe' | 'remote' | 'sub2api' | 'oaipay',
    scope: 'selected' | 'all',
    customParams?: Record<string, unknown>,
  ) => {
    if (currentPlatform !== 'chatgpt') return

    const loadingKey = `${kind}_${scope}` as typeof statusSyncLoading
    const actionId =
      kind === 'probe'
        ? 'probe_local_status'
        : kind === 'sub2api'
          ? 'sync_sub2api_status'
          : kind === 'oaipay'
            ? 'sync_oaipay_status'
            : 'sync_cliproxyapi_status'
    const actionLabel =
      kind === 'probe'
        ? '本地状态同步'
        : kind === 'sub2api'
          ? 'Sub2API 状态同步'
          : kind === 'oaipay'
            ? 'OAIPay 状态同步'
            : 'CLIProxyAPI 状态同步'
    const scopeLabel = scope === 'selected' ? '所选账号' : '当前筛选账号'
    const toastKey = `status-sync:${loadingKey}`

    const body: Record<string, unknown> = {
      params: customParams || {},
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
      applyCurrentFiltersToBody(body)
    }

    setStatusSyncLoading(loadingKey)
    message.loading({ content: `${scopeLabel}${actionLabel}进行中...`, key: toastKey, duration: 0 })
    try {
      if (kind === 'probe') {
        const res = await apiFetch('/tasks/chatgpt/probe-local-status/batch', {
          method: 'POST',
          body: JSON.stringify(body),
        })
        const eligible = Number(res?.eligible || 0)
        const skipped = Number(res?.skipped || 0)
        const missing = Number(res?.missing || 0)
        const taskIdFromResponse = String(res?.task_id || '').trim()

        if (!taskIdFromResponse) {
          const reqCount = Array.isArray(body.account_ids) ? body.account_ids.length : '所有'
          message.info({
            content: `没有可同步本地状态的账号。请求 ${reqCount} 个，跳过 ${skipped} 个${missing > 0 ? `，缺失 ${missing} 个` : ''}`,
            key: toastKey,
          })
          if (res && typeof res === 'object') {
            showBatchActionResult(`${scopeLabel}${actionLabel}结果`, res)
          }
          return
        }

        const snapshot = await apiFetch(`/tasks/${taskIdFromResponse}`)
        setTaskModalMode('probe_local_status')
        setTaskModalAccount(scope === 'selected' ? null : { email: `当前筛选 ${eligible} 个账号` })
        setTaskId(taskIdFromResponse)
        setTaskSnapshot(snapshot)
        setRegisterModalOpen(true)
        setActiveTasksPanelOpen(true)
        void activeTasksQuery.refetch()
        message.success({
          content: `批量同步本地状态任务已启动：可执行 ${eligible} 个，跳过 ${skipped} 个${missing > 0 ? `，缺失 ${missing} 个` : ''}`,
          key: toastKey,
        })
        showBatchActionResult(`${scopeLabel}${actionLabel}结果`, res)
        return
      }

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

  const getPendingBackfillCount = (destination: 'cliproxyapi' | 'sub2api' | 'oaipay') => {
    if (destination === 'sub2api') {
      return summarizeSub2ApiStates(accounts).pending
    }
    if (destination === 'oaipay') {
      return accounts.filter((item: any) => {
        const sync = item?.oaipaySync || {}
        if (!sync || Object.keys(sync).length === 0) return true
        return String(sync?.remote_state || '').trim().toLowerCase() === 'not_found'
      }).length
    }
    return accounts.filter((item: any) => {
      const sync = item?.cliproxySync || {}
      if (!sync || Object.keys(sync).length === 0) return true
      return String(sync?.remote_state || '').trim().toLowerCase() === 'not_found'
    }).length
  }

  const buildBackfillLabel = (destination: 'cliproxyapi' | 'sub2api' | 'oaipay') => {
    const scope = getBackfillScope()
    const count = scope === 'selected' ? selectedRowKeys.length : getPendingBackfillCount(destination)
    const destinationLabel = destination === 'oaipay' ? 'OAIPay' : destination === 'sub2api' ? 'Sub2API' : 'CLIProxyAPI'
    return scope === 'selected'
      ? `补传所选到 ${destinationLabel} (${count})`
      : `补传 ${destinationLabel} 待补传 (${count})`
  }

  const isBackfillActionLoading = (destination: 'cliproxyapi' | 'sub2api' | 'oaipay', scope: 'selected' | 'pending') => backfillLoading === `${destination}_${scope}`

  const buildBackfillMenuLabel = (destination: 'cliproxyapi' | 'sub2api' | 'oaipay') => {
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
  const cellStackStyle: React.CSSProperties = {
    display: 'flex',
    flexDirection: 'column',
    gap: isChatgptPlatform ? 4 : 6,
    minWidth: 0,
  }
  const compactTagStyle: React.CSSProperties = {
    marginInlineEnd: 0,
    whiteSpace: 'nowrap',
  }
  const visibleColumnKeySet = new Set(visibleColumnKeys)
  const isColumnVisible = (key: string) => {
    if (!ACCOUNT_COLUMN_OPTION_KEYS.has(key as AccountColumnKey)) return true
    return visibleColumnKeySet.has(key as AccountColumnKey)
  }
  const columnVisibilityOptions = ACCOUNT_COLUMN_OPTIONS
    .filter((option) => !option.chatgptOnly || isChatgptPlatform)
    .map((option) => ({ value: option.value, text: option.text }))
  const updateVisibleColumns = (next: string[]) => {
    const normalized = normalizeVisibleAccountColumns(next)
    setVisibleColumnKeys(normalized)
    saveVisibleAccountColumnKeys(normalized)
  }
  const resetVisibleColumns = () => {
    const defaults = normalizeVisibleAccountColumns(DEFAULT_VISIBLE_ACCOUNT_COLUMNS)
    setVisibleColumnKeys(defaults)
    saveVisibleAccountColumnKeys(defaults)
  }

  const renderColumnVisibilityControl = () => {
    const overlay = (
      <div
        onClick={(event) => event.stopPropagation()}
        style={{
          minWidth: isMobile ? 240 : 280,
          maxWidth: isMobile ? 'calc(100vw - 48px)' : 320,
          padding: 12,
          borderRadius: 8,
          background: token.colorBgElevated,
          boxShadow: token.boxShadowSecondary,
        }}
      >
        <Checkbox.Group
          value={visibleColumnKeys}
          options={toCheckboxOptions(columnVisibilityOptions)}
          onChange={(checkedValues) => updateVisibleColumns(checkedValues.map((item) => String(item)))}
          style={{
            display: 'grid',
            gridTemplateColumns: isMobile ? '1fr' : 'repeat(2, minmax(0, 1fr))',
            gap: 8,
          }}
        />
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 12 }}>
          <Button size="small" onClick={() => updateVisibleColumns(columnVisibilityOptions.map((option) => option.value))}>
            全选
          </Button>
          <Button size="small" onClick={resetVisibleColumns}>
            默认
          </Button>
        </div>
      </div>
    )

    return (
      <Dropdown dropdownRender={() => overlay} trigger={['click']}>
        <Button size="small" icon={<SettingOutlined />}>
          列显示
        </Button>
      </Dropdown>
    )
  }

  const renderAccountIdentity = (text: string, record: any) => {
    return (
      <div style={cellStackStyle}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 4, minWidth: 0 }}>
          <Text
            style={{ ...monospaceStyle, flex: 1, minWidth: 0, whiteSpace: 'nowrap', fontSize: 12 }}
            ellipsis={{ tooltip: text }}
          >
            {text}
          </Text>
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
      </div>
    )
  }

  const renderPasswordState = (_text: string, record: any) => {
    const hasPassword = hasAccountSecret(record, 'password')
    return (
      <Space size={4} style={{ width: '100%', justifyContent: 'center' }}>
        <Tag color={hasPassword ? 'success' : 'default'} style={compactTagStyle}>{hasPassword ? '有密码' : '无密码'}</Tag>
        {hasPassword ? (
          <Button title="复制密码" type="text" size="small" icon={<CopyOutlined />} onClick={() => copyAccountSecret(record, 'password', '密码')} />
        ) : null}
      </Space>
    )
  }

  const renderPhoneBindingState = (record: any) => {
    const binding = getPhoneBinding(record)
    if (!binding.phone && !binding.apiUrl) {
      return <Text type="secondary" style={{ fontSize: 12 }}>-</Text>
    }
    const rawLine = binding.rawLine || [binding.phone, binding.apiUrl].filter(Boolean).join('----')
    const secondary = binding.codeTime || binding.boundAt || binding.apiExpiredDate

    return (
      <div style={{ ...cellStackStyle, gap: 3, maxWidth: 260 }}>
        <Space size={4} style={{ minWidth: 0 }}>
          <Text
            style={{ ...monospaceStyle, maxWidth: 132 }}
            ellipsis={{ tooltip: binding.phone || '' }}
          >
            {binding.phone || '-'}
          </Text>
          {binding.phone ? (
            <Button title="复制手机号" type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(binding.phone)} />
          ) : null}
        </Space>
        {binding.apiUrl ? (
          <Space size={4} style={{ minWidth: 0 }}>
            <Text
              type="secondary"
              style={{ ...monospaceStyle, maxWidth: 168, fontSize: 11 }}
              ellipsis={{ tooltip: binding.apiUrl }}
            >
              {binding.apiUrl}
            </Text>
            <Button title="复制完整 API" type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(binding.apiUrl)} />
          </Space>
        ) : null}
        <Space size={6} wrap>
          {rawLine ? (
            <Button size="small" type="link" icon={<CopyOutlined />} style={{ paddingInline: 0 }} onClick={() => copyText(rawLine)}>
              复制整行
            </Button>
          ) : null}
          {secondary ? (
            <Text type="secondary" style={{ fontSize: 11 }} ellipsis={{ tooltip: secondary }}>
              {secondary}
            </Text>
          ) : null}
        </Space>
      </div>
    )
  }

  const renderAuthTypeState = (record: any) => {
    const meta = authTypeMeta(record)
    const hasAccessToken = hasAccountSecret(record, 'access_token')
    const hasRefreshToken = hasAccountSecret(record, 'refresh_token')
    const accountId = Number(record?.id || 0)
    const accessTokenCopied = accountId > 0 && accessTokenCopiedAccountIds.has(accountId)
    return (
      <Space size={4} wrap style={{ width: '100%', justifyContent: 'center' }}>
        <Tag color={meta.color} style={compactTagStyle}>{meta.label}</Tag>
        {hasAccessToken ? (
          <Button title="复制AT" type="text" size="small" icon={<CopyOutlined />} onClick={() => copyAccessToken(record)}>
            AT
          </Button>
        ) : null}
        {hasRefreshToken ? (
          <Button title="复制RT" type="text" size="small" icon={<CopyOutlined />} onClick={() => copyAccountSecret(record, 'refresh_token', 'RT')} />
        ) : null}
        {accessTokenCopied ? <Tag color="orange" style={compactTagStyle}>已复制AT</Tag> : null}
      </Space>
    )
  }

  const renderManualUsedState = (record: any) => (
    <Space size={4} wrap style={{ width: '100%', justifyContent: 'center' }}>
      <Tag color={record.manuallyUsed ? 'orange' : 'default'} style={compactTagStyle}>
        {record.manuallyUsed ? '已使用' : '未使用'}
      </Tag>
      {record.manuallyUsed ? (
        <Button
          type="link"
          size="small"
          style={{ paddingInline: 0 }}
          onClick={(event) => {
            event.stopPropagation()
            void unmarkAccountUsed(record)
          }}
        >
          取消
        </Button>
      ) : null}
    </Space>
  )

  const renderSubscriptionTypeState = (record: any) => {
    const meta = subscriptionTypeMeta(record)
    return (
      <div title={meta.title} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2, lineHeight: '16px' }}>
        <Tag color={meta.color} style={compactTagStyle}>{meta.label}</Tag>
        {meta.subLabel ? (
          <Text type="secondary" style={{ fontSize: 11, whiteSpace: 'nowrap' }}>{meta.subLabel}</Text>
        ) : null}
      </div>
    )
  }

  const renderAccountStatusState = (status: string, record?: any, options?: { inline?: boolean }) => {
    const recoverAt = String(status || '').trim() === 'rate_limited' ? formatRateLimitRecoverAt(record) : null
    const title = recoverAt?.title ? `预计恢复: ${recoverAt.title}` : undefined
    return (
      <Space
        size={4}
        wrap
        style={{
          width: options?.inline ? 'auto' : '100%',
          maxWidth: '100%',
          justifyContent: options?.inline ? 'flex-end' : 'center',
        }}
      >
        <Tag color={STATUS_COLORS[status] || 'default'} title={title} style={compactTagStyle}>
          {statusLabel(status)}
          {recoverAt ? (
            <Text type={recoverAt.expired ? 'warning' : 'secondary'} style={{ marginLeft: 4, fontSize: 11 }}>
              {`恢复 ${recoverAt.compact}`}
            </Text>
          ) : null}
        </Tag>
      </Space>
    )
  }

  const renderSubscriptionExpiryState = (record: any) => {
    const expiry = formatSubscriptionExpiry(record)
    if (!expiry) {
      return <Text type="secondary" style={{ fontSize: 11, lineHeight: '18px' }}>-</Text>
    }
    return (
      <div title={expiry.title} style={{ lineHeight: '16px', minWidth: 0 }}>
        <Text
          type={expiry.expired ? 'danger' : undefined}
          style={{
            display: 'block',
            fontSize: 11,
            whiteSpace: 'nowrap',
          }}
        >
          {expiry.date}
        </Text>
        {expiry.time ? (
          <Text type="secondary" style={{ display: 'block', fontSize: 11, whiteSpace: 'nowrap' }}>
            {expiry.time}
          </Text>
        ) : null}
      </div>
    )
  }

  const renderAccountValidityState = (record: any) => {
    const meta = accountValidityMeta(record)
    return <Tag color={meta.color} style={compactTagStyle}>{meta.label}</Tag>
  }

  const renderIdeaSubmitState = (record: any) => {
    const meta = ideaSubmitMeta(record)
    const summary = getIdeaSubmitSummary(record)
    const title = [
      meta.reason ? `原因：${meta.reason}` : '',
      summary?.marked_at ? `标记：${formatCompactDateTime(String(summary.marked_at))}` : '',
      summary?.order_id ? `order：${summary.order_id}` : '',
      summary?.code_masked ? `卡密：${summary.code_masked}` : '',
    ].filter(Boolean).join('\n')
    return <Tag color={meta.color} title={title || meta.label} style={compactTagStyle}>{meta.label}</Tag>
  }

  const renderCodexUsageState = (record: any) => {
    const accountId = Number(record?.id || 0)
    const { codex, usage } = getCodexUsage(record)
    const meta = codexStateMeta(String(codex?.state || record?.codex_state || '').trim())
    const updated = formatCompactDateTime(String(usage?.codex_usage_updated_at || codex?.checked_at || '').trim())
    const refreshing = codexUsageRefreshingIds.has(accountId)
    const hasUsage = usage && (
      usage.codex_5h_used_percent !== undefined
      || usage.codex_7d_used_percent !== undefined
      || usage.codex_primary_used_percent !== undefined
      || usage.codex_secondary_used_percent !== undefined
    )

    const renderWindow = (label: string, prefix: 'codex_5h' | 'codex_7d', fallbackPrefix?: 'codex_primary' | 'codex_secondary') => {
      const used = readNumberValue(usage?.[`${prefix}_used_percent`]) ?? (fallbackPrefix ? readNumberValue(usage?.[`${fallbackPrefix}_used_percent`]) : null)
      const remaining = codexRemainingPercent(used)
      const resetText = formatCodexResetShort(
        usage?.[`${prefix}_reset_after_seconds`] ?? (fallbackPrefix ? usage?.[`${fallbackPrefix}_reset_after_seconds`] : undefined),
        usage?.[`${prefix}_reset_at`] ?? (fallbackPrefix ? usage?.[`${fallbackPrefix}_reset_at`] : undefined),
      )
      const percent = remaining === null ? 0 : Math.round(remaining)
      const strokeColor = percent <= 10 ? token.colorError : percent <= 30 ? token.colorWarning : token.colorSuccess
      return (
        <div
          key={label}
          title={`${label} 已用 ${formatCodexPercent(used)}${resetText ? `，重置 ${resetText}` : ''}`}
          style={{ display: 'grid', gridTemplateColumns: '28px minmax(64px, 1fr) 44px', gap: 6, alignItems: 'center' }}
        >
          <Text type="secondary" style={{ fontSize: 11 }}>{label}</Text>
          <Progress
            percent={percent}
            showInfo={false}
            size="small"
            strokeColor={strokeColor}
            trailColor={token.colorFillSecondary}
          />
          <Text style={{ fontSize: 11, textAlign: 'right', color: strokeColor }}>
            {remaining === null ? '-' : formatCodexPercent(remaining)}
          </Text>
        </div>
      )
    }

    const longWinMins = readNumberValue(usage?.codex_7d_window_minutes) ?? readNumberValue(usage?.codex_primary_window_minutes) ?? readNumberValue(usage?.codex_secondary_window_minutes)
    const longLabel = (longWinMins !== null && longWinMins >= 20000) ? '30d' : '7d'

    return (
      <div style={{ minWidth: 0, width: '100%' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 6, marginBottom: 3 }}>
          <Tag color={meta.color} style={compactTagStyle}>{meta.label}</Tag>
          <Button
            type="text"
            size="small"
            title="刷新 Codex 用量"
            icon={<SyncOutlined spin={refreshing} />}
            loading={refreshing}
            onClick={(event) => {
              event.stopPropagation()
              void refreshCodexUsage(record)
            }}
            style={{ paddingInline: 4 }}
          />
        </div>
        {hasUsage ? (
          <Space direction="vertical" size={2} style={{ width: '100%' }}>
            {renderWindow('5h', 'codex_5h')}
            {renderWindow(longLabel, 'codex_7d')}
            {updated ? (
              <Text type="secondary" style={{ fontSize: 11 }} title={`更新时间: ${updated?.title || ''}`}>
                更新 {updated?.compact || ''}
              </Text>
            ) : null}
          </Space>
        ) : (
          <Text type="secondary" style={{ fontSize: 11 }}>
            无用量缓存
          </Text>
        )}
      </div>
    )
  }

  const applySubscriptionExpirySortOrder = useCallback((next: SubscriptionExpirySortOrder) => {
    setSubscriptionExpirySortOrder(next)
    setCurrentPage(1)
  }, [])

  const handleAccountsTableChange = useCallback((_pagination: any, _filters: Record<string, any>, sorter: any) => {
    const activeSorter = Array.isArray(sorter)
      ? sorter.find((item) => String(item?.columnKey || item?.field || '') === SUBSCRIPTION_EXPIRY_SORT_FIELD)
      : sorter
    const sorterKey = String(activeSorter?.columnKey || activeSorter?.field || '')
    const order = sorterKey === SUBSCRIPTION_EXPIRY_SORT_FIELD ? String(activeSorter?.order || '') : ''
    const nextOrder: SubscriptionExpirySortOrder = order === 'ascend' ? 'asc' : order === 'descend' ? 'desc' : ''
    applySubscriptionExpirySortOrder(nextOrder)
  }, [applySubscriptionExpirySortOrder])

  const renderMobileFilterControls = () => {
    if (!isMobile) return null
    return (
      <div
        style={{
          display: 'grid',
          gap: 8,
          padding: 10,
          border: `1px solid ${token.colorBorderSecondary}`,
          borderRadius: 8,
          background: token.colorBgContainer,
        }}
      >
        <Input.Search
          allowClear
          size="small"
          placeholder="搜索邮箱"
          value={search}
          onChange={(event) => {
            setSearch(event.target.value)
            setColumnFilters((prev) => ({ ...prev, email: event.target.value }))
          }}
          onSearch={(value) => setDebouncedSearch(String(value || '').trim())}
        />
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 8 }}>
          <Select
            allowClear
            size="small"
            placeholder="使用状态"
            value={columnFilters.manuallyUsed[0]}
            options={toSelectOptions(MANUAL_USE_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({ ...prev, manuallyUsed: value ? [value] : [] }))}
          />
          <Select
            allowClear
            size="small"
            placeholder="业务状态"
            value={columnFilters.status[0]}
            options={toSelectOptions(STATUS_FILTER_OPTIONS)}
            onChange={(value) => {
              const next = value ? [value] : []
              setColumnFilters((prev) => ({ ...prev, status: next }))
              setFilterStatus(next.join(','))
            }}
          />
          <Select
            allowClear
            size="small"
            placeholder="认证材料"
            value={columnFilters.authType[0]}
            options={toSelectOptions(AUTH_TYPE_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({ ...prev, authType: value ? [value] : [] }))}
          />
          <Select
            allowClear
            size="small"
            placeholder="当前订阅"
            value={columnFilters.subscriptionType[0]}
            options={toSelectOptions(SUBSCRIPTION_TYPE_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({ ...prev, subscriptionType: value ? [value] : [] }))}
          />
          <Select
            allowClear
            size="small"
            placeholder="认证状态"
            value={columnFilters.accountValidity[0]}
            options={toSelectOptions(ACCOUNT_VALIDITY_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({ ...prev, accountValidity: value ? [value] : [] }))}
          />
          <Select
            allowClear
            size="small"
            placeholder="Codex状态"
            value={columnFilters.codexState[0]}
            options={toSelectOptions(CODEX_STATE_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({ ...prev, codexState: value ? [value] : [] }))}
          />
          <Select
            allowClear
            size="small"
            placeholder="Sub2API"
            value={columnFilters.sub2apiState[0]}
            options={toSelectOptions(SUB2API_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({ ...prev, sub2apiState: value ? [value] : [] }))}
          />
          <Select
            allowClear
            size="small"
            placeholder="OAIPay"
            value={columnFilters.oaipayState[0]}
            options={toSelectOptions(OAIPAY_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({ ...prev, oaipayState: value ? [value] : [] }))}
          />
          <Select
            allowClear
            size="small"
            placeholder="Idea提交"
            value={columnFilters.ideaSubmitState[0]}
            options={toSelectOptions(IDEA_SUBMIT_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({ ...prev, ideaSubmitState: value ? [value] : [] }))}
          />
          <Select
            allowClear
            size="small"
            placeholder="到期排序"
            value={subscriptionExpirySortOrder || undefined}
            options={toSelectOptions(SUBSCRIPTION_EXPIRY_SORT_OPTIONS)}
            onChange={(value) => applySubscriptionExpirySortOrder((value || '') as SubscriptionExpirySortOrder)}
          />
        </div>
      </div>
    )
  }

  const renderSub2ApiState = (record: any) => {
    const sync = record.sub2apiSync || {}
    const meta = sub2apiStateMeta(
      record.sub2api_remote_state
        ? { ...sync, remote_state: record.sub2api_remote_state }
        : sync,
    )

    return (
      <Tag color={meta.color} style={compactTagStyle}>{meta.label}</Tag>
    )
  }

  const renderSub2ApiUploadRecord = (record: any) => {
    const sync = record.sub2apiSync && typeof record.sub2apiSync === 'object' ? record.sub2apiSync : {}
    const lastUpload = sync.last_upload && typeof sync.last_upload === 'object' ? sync.last_upload : {}
    const status = String(lastUpload.status || '').trim().toLowerCase()
    const remoteId = lastUpload.remote_account_id || sync.remote_account_id
    const messageText = String(lastUpload.message || sync.message || sync.last_message || '').trim()
    const timeText = formatSyncTime(lastUpload.finished_at || lastUpload.attempted_at || sync.uploaded_at || sync.last_attempt_at || sync.checked_at)
    const source = String(sync.probe_source || '').trim().toUpperCase()
    const meta = (() => {
      if (status === 'success') return { color: 'success', label: '成功' }
      if (status === 'failed') return { color: 'error', label: '失败' }
      if (status === 'blocked') return { color: 'warning', label: '阻断' }
      if (status === 'skipped') return { color: 'processing', label: '跳过' }
      if (sync.uploaded) return { color: 'success', label: '已确认' }
      if (sync.remote_state) return { color: 'default', label: '仅探测' }
      return { color: 'default', label: '无记录' }
    })()

    if (!status && !sync.remote_state && !messageText && !timeText) {
      return <Typography.Text type="secondary">-</Typography.Text>
    }

    return (
      <Space direction="vertical" size={2} style={{ maxWidth: 180 }}>
        <Space size={5} wrap>
          <Tag color={meta.color} style={compactTagStyle}>{meta.label}</Tag>
          {remoteId ? <Typography.Text style={{ fontSize: 12 }}>#{String(remoteId)}</Typography.Text> : null}
          {source ? <Tag style={{ ...compactTagStyle, fontSize: 11 }}>{source}</Tag> : null}
        </Space>
        {timeText ? <Typography.Text type="secondary" style={{ fontSize: 12 }}>{timeText}</Typography.Text> : null}
        {messageText ? (
          <Typography.Text type={status === 'failed' || status === 'blocked' ? 'danger' : 'secondary'} ellipsis={{ tooltip: messageText }} style={{ fontSize: 12, maxWidth: 180 }}>
            {messageText}
          </Typography.Text>
        ) : null}
      </Space>
    )
  }

  const renderOaipayState = (record: any) => {
    const sync = record.oaipaySync || {}
    const meta = sub2apiStateMeta(sync)
    return <Tag color={meta.color} style={compactTagStyle}>{meta.label}</Tag>
  }

  const renderOaipayUploadRecord = (record: any) => {
    const sync = record.oaipaySync && typeof record.oaipaySync === 'object' ? record.oaipaySync : {}
    const lastUpload = sync.last_upload && typeof sync.last_upload === 'object' ? sync.last_upload : {}
    const status = String(lastUpload.status || '').trim().toLowerCase()
    const remoteId = lastUpload.remote_account_id || sync.remote_account_id
    const messageText = String(lastUpload.message || sync.message || sync.last_message || '').trim()
    const timeText = formatSyncTime(lastUpload.finished_at || lastUpload.attempted_at || sync.uploaded_at || sync.last_attempt_at || sync.checked_at)
    const source = String(sync.probe_source || '').trim().toUpperCase()
    const categoryId = lastUpload.category_id || sync.category_id || lastUpload.remote_category_id || sync.remote_category_id
    const categoryName = String(lastUpload.category_name || sync.category_name || '').trim()
    const categorySource = String(lastUpload.category_source || sync.category_source || '').trim()
    const categorySourceLabel = ({
      auto: '自动',
      manual: '固定',
      fallback: '兜底',
      global_default: '全局',
      remote_probe: '远端',
    } as Record<string, string>)[categorySource] || categorySource
    const categoryText = categoryId && categoryName
      ? `#${categoryId} ${categoryName}`
      : categoryName || (categoryId ? `#${categoryId}` : '')
    const categoryTooltip = [
      categoryText ? `最终分类：${categoryText}` : '',
      categorySourceLabel ? `来源：${categorySourceLabel}` : '',
      lastUpload.category_rule || sync.category_rule ? `规则：${lastUpload.category_rule || sync.category_rule}` : '',
    ].filter(Boolean).join('；')
    const meta = (() => {
      if (status === 'success') return { color: 'success', label: '成功' }
      if (status === 'failed') return { color: 'error', label: '失败' }
      if (status === 'blocked') return { color: 'warning', label: '阻断' }
      if (status === 'skipped') return { color: 'processing', label: '跳过' }
      if (sync.uploaded) return { color: 'success', label: '已确认' }
      if (sync.remote_state) return { color: 'default', label: '仅探测' }
      return { color: 'default', label: '无记录' }
    })()

    if (!status && !sync.remote_state && !messageText && !timeText) {
      return <Typography.Text type="secondary">-</Typography.Text>
    }

    return (
      <Space direction="vertical" size={2} style={{ maxWidth: 220 }}>
        <Space size={5} wrap>
          <Tag color={meta.color} style={compactTagStyle}>{meta.label}</Tag>
          {remoteId ? <Typography.Text style={{ fontSize: 12 }}>#{String(remoteId)}</Typography.Text> : null}
          {source ? <Tag style={{ ...compactTagStyle, fontSize: 11 }}>{source}</Tag> : null}
          {categoryText ? (
            <Tag color="blue" style={{ ...compactTagStyle, fontSize: 11 }} title={categoryTooltip || categoryText}>
              {categoryText}
            </Tag>
          ) : null}
          {categorySourceLabel && categoryText ? <Tag style={{ ...compactTagStyle, fontSize: 11 }}>{categorySourceLabel}</Tag> : null}
        </Space>
        {timeText ? <Typography.Text type="secondary" style={{ fontSize: 12 }}>{timeText}</Typography.Text> : null}
        {messageText ? (
          <Typography.Text type={status === 'failed' || status === 'blocked' ? 'danger' : 'secondary'} ellipsis={{ tooltip: messageText }} style={{ fontSize: 12, maxWidth: 220 }}>
            {messageText}
          </Typography.Text>
        ) : null}
      </Space>
    )
  }

  const renderColumnFilterTitle = (
    label: string,
    values: string[],
    options: Array<{ value: string; text: string }>,
    onChange: (next: string[]) => void,
  ) => {
    const selectedCount = values.length
    const overlay = (
      <div
        onClick={(event) => event.stopPropagation()}
        style={{
          minWidth: 168,
          padding: 10,
          borderRadius: 8,
          background: token.colorBgElevated,
          boxShadow: token.boxShadowSecondary,
        }}
      >
        <Checkbox.Group
          value={values}
          options={toCheckboxOptions(options)}
          onChange={(checkedValues) => onChange(checkedValues.map((item) => String(item)))}
          style={{ display: 'grid', gap: 8 }}
        />
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 10 }}>
          <Button size="small" onClick={() => onChange([])}>
            清空
          </Button>
        </div>
      </div>
    )

    return (
      <Dropdown dropdownRender={() => overlay} trigger={['click']}>
        <button
          type="button"
          style={{
            width: '100%',
            minWidth: 0,
            border: 0,
            padding: 0,
            background: 'transparent',
            color: selectedCount ? token.colorPrimary : 'inherit',
            cursor: 'pointer',
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 4,
            font: 'inherit',
            fontWeight: 600,
          }}
          onClick={(event) => event.preventDefault()}
        >
          <span>{label}</span>
          {selectedCount ? <span style={{ fontSize: 11 }}>({selectedCount})</span> : null}
          <DownOutlined style={{ fontSize: 10 }} />
        </button>
      </Dropdown>
    )
  }

  const buildAccountMoreMenuItems = (record: any): MenuProps['items'] => {
    const commonActions = Array.isArray(platformActions) ? platformActions : []
    const paymentLinkAction = commonActions.find((action: any) => String(action?.id || '').trim().toLowerCase() === 'payment_link')
    const invalidRecheckAction = commonActions.find((action: any) => String(action?.id || '').trim().toLowerCase() === 'invalid_recheck')
    const k12RecaptureAction = commonActions.find((action: any) => String(action?.id || '').trim().toLowerCase() === 'k12_workspace_recapture')
    const hiddenIds = new Set([
      paymentLinkAction ? String(paymentLinkAction.id) : '',
      invalidRecheckAction ? String(invalidRecheckAction.id) : '',
      k12RecaptureAction ? String(k12RecaptureAction.id) : '',
      'probe_local_status',
      'resume_subscription_auth',
    ].filter(Boolean))
    const moreActions = commonActions.filter((action: any) => !hiddenIds.has(String(action?.id || '')))

    return [
      { key: '__detail__', label: '账号详情' },
      ...(paymentLinkAction ? [{ key: '__payment_link_config__', label: '订阅链接配置' }] : []),
      ...(paymentLinkAction ? [{ key: '__payment_link_regenerate__', label: '重新生成订阅链接' }] : []),
      ...(isChatgptPlatform && shouldShowResumeAuthButton(record) ? [{ key: '__resume_auth_config__', label: '补抓Auth临时配置' }] : []),
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
  }

  const handleAccountMoreMenuClick = (record: any, key: React.Key) => {
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
    if (String(key) === '__resume_auth_config__') {
      void openResumeAuthConfig(record)
      return
    }
    if (String(key) === '__delete_account__') {
      return
    }
    openAccountInlineAction(record, String(key))
  }

  const renderAccountActions = (record: any, compact = false) => {
    const commonActions = Array.isArray(platformActions) ? platformActions : []
    const paymentLinkAction = commonActions.find((action: any) => String(action?.id || '').trim().toLowerCase() === 'payment_link')
    const k12RecaptureAction = commonActions.find((action: any) => String(action?.id || '').trim().toLowerCase() === 'k12_workspace_recapture')
    const showResumeAuth = isChatgptPlatform && shouldShowResumeAuthButton(record)
    const showInvalidRecheck = isChatgptPlatform && shouldShowInvalidRecheckButton(record)
    const moreMenuItems = buildAccountMoreMenuItems(record)

    if (compact) {
      const mobileActionButtonStyle = (style: CSSProperties): React.CSSProperties => ({
        width: '100%',
        justifyContent: 'center',
        color: style.color,
        borderColor: style.color,
      })

      return (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 8 }}>
          {paymentLinkAction ? (
            <Button size="small" style={mobileActionButtonStyle(accountActionTextStyles.payment)} onClick={() => openAccountPaymentLinkAction(record)}>
              订阅链接
            </Button>
          ) : null}
          <Button size="small" style={mobileActionButtonStyle(accountActionTextStyles.refresh)} onClick={() => openAccountProbeStatusAction(record)}>
            刷新状态
          </Button>
          {showResumeAuth ? (
            <Button
              size="small"
              loading={resumeAuthAccountId === record.id}
              style={mobileActionButtonStyle(accountActionTextStyles.resume)}
              onClick={() => handleResumeSubscriptionAuth(record)}
            >
              补抓Auth
            </Button>
          ) : null}
          {isChatgptPlatform ? (
            <Button size="small" style={mobileActionButtonStyle(accountActionTextStyles.payment)} onClick={() => openAccountInlineAction(record, 'gopay', 'direct')}>
              GoPay支付
            </Button>
          ) : null}
          {showInvalidRecheck ? (
            <Button size="small" style={mobileActionButtonStyle(accountActionTextStyles.resume)} onClick={() => handleInvalidRecheck(record)}>
              失效测活
            </Button>
          ) : null}
          {k12RecaptureAction ? (
            <Button size="small" style={mobileActionButtonStyle(accountActionTextStyles.refresh)} onClick={() => openAccountInlineAction(record, 'k12_workspace_recapture', 'dialog')}>
              K12重跑
            </Button>
          ) : null}
          <Dropdown
            menu={{
              items: moreMenuItems,
              onClick: ({ key }) => handleAccountMoreMenuClick(record, key),
            }}
          >
            <Button size="small" icon={<MoreOutlined />} loading={platformActionsLoading} style={mobileActionButtonStyle(accountActionTextStyles.more)}>
              更多
            </Button>
          </Dropdown>
        </div>
      )
    }

    return (
      <Space direction="vertical" size={compact ? 6 : 4} style={{ width: '100%' }}>
        <Space size={compact ? 8 : 4} wrap style={{ width: '100%' }}>
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
        <Space size={compact ? 8 : 4} wrap style={{ width: '100%' }}>
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
          {isChatgptPlatform ? (
            <Button
              type="link"
              size="small"
              style={accountActionTextStyles.payment}
              onClick={() => openAccountInlineAction(record, 'gopay', 'direct')}
            >
              GoPay支付
            </Button>
          ) : null}
          {showInvalidRecheck ? (
            <Button
              type="link"
              size="small"
              style={accountActionTextStyles.resume}
              onClick={() => handleInvalidRecheck(record)}
            >
              失效测活
            </Button>
          ) : null}
          {k12RecaptureAction ? (
            <Button
              type="link"
              size="small"
              style={accountActionTextStyles.refresh}
              onClick={() => openAccountInlineAction(record, 'k12_workspace_recapture', 'dialog')}
            >
              K12重跑
            </Button>
          ) : null}
          <Dropdown
            menu={{
              items: moreMenuItems,
              onClick: ({ key }) => handleAccountMoreMenuClick(record, key),
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
  }

  const renderAccountMobileCard = (record: any, helpers: { checked: boolean; onCheckedChange: (checked: boolean) => void }) => {
    const formatted = formatCreatedAt(record.created_at)
    const subscriptionExpiry = formatSubscriptionExpiry(record)
    const email = String(record.email || '').trim()
    const teamInviteOwner = getTeamInviteOwnerLabel(record.teamInviteSource)
    const teamInviteMeta = [
      record.teamInviteSource?.team_name ? `Team: ${record.teamInviteSource.team_name}` : '',
      record.teamInviteSource?.team_id ? `#${record.teamInviteSource.team_id}` : '',
    ].filter(Boolean).join(' · ')
    const phoneBinding = getPhoneBinding(record)
    const hasPhoneBinding = Boolean(phoneBinding.phone || phoneBinding.apiUrl)
    const rawPhoneLine = phoneBinding.rawLine || [phoneBinding.phone, phoneBinding.apiUrl].filter(Boolean).join('----')
    const phoneSecondary = phoneBinding.codeTime || phoneBinding.boundAt || phoneBinding.apiExpiredDate
    const registeredAt = `${formatted.date}${formatted.time ? ` ${formatted.time}` : ''}`
    const expiresAt = subscriptionExpiry?.compact || '-'
    const mobileMetaParts = [
      isColumnVisible('created_at') ? `注册 ${registeredAt}` : '',
      isColumnVisible('subscription_active_until') ? `到期 ${expiresAt}` : '',
    ].filter(Boolean)
    const mobileStatusLabelStyle: React.CSSProperties = {
      display: 'block',
      marginBottom: 4,
      color: token.colorTextSecondary,
      fontSize: 11,
      lineHeight: '16px',
    }
    const mobileStatusPillStyle: React.CSSProperties = {
      marginInlineEnd: 0,
      padding: '2px 9px',
      borderRadius: 8,
      fontSize: 13,
      lineHeight: '22px',
      fontWeight: 600,
    }
    const renderMobileStatusPill = (
      key: string,
      label: React.ReactNode,
      color: string,
      extra?: React.ReactNode,
    ) => (
      <span key={key} style={{ display: 'inline-flex', alignItems: 'center', gap: 5, maxWidth: '100%' }}>
        <Tag color={color} style={mobileStatusPillStyle}>{label}</Tag>
        {extra}
      </span>
    )
    const authMetaForMobile = authTypeMeta(record)
    const hasAccessTokenForMobile = hasAccountSecret(record, 'access_token')
    const accessTokenCopiedForMobile = Number(record?.id || 0) > 0 && accessTokenCopiedAccountIds.has(Number(record.id || 0))
    const hasRefreshTokenForMobile = hasAccountSecret(record, 'refresh_token')
    const subscriptionMetaForMobile = subscriptionTypeMeta(record)
    const validityMetaForMobile = accountValidityMeta(record)
    const ideaMetaForMobile = ideaSubmitMeta(record)
    const hasPasswordForMobile = hasAccountSecret(record, 'password')
    const mobileStatusItems = [
      isColumnVisible('auth_type') ? renderMobileStatusPill(
        'auth_type',
        authMetaForMobile.label,
        authMetaForMobile.color,
        <>
          {hasAccessTokenForMobile ? (
            <Button title="复制AT" type="text" size="small" icon={<CopyOutlined />} onClick={() => copyAccessToken(record)}>
              AT
            </Button>
          ) : null}
          {hasRefreshTokenForMobile ? (
            <Button title="复制RT" type="text" size="small" icon={<CopyOutlined />} onClick={() => copyAccountSecret(record, 'refresh_token', 'RT')} />
          ) : null}
          {accessTokenCopiedForMobile ? <Tag color="orange" style={{ ...compactTagStyle, fontSize: 11 }}>已复制AT</Tag> : null}
        </>,
      ) : null,
      isColumnVisible('manually_used') ? renderMobileStatusPill(
        'manually_used',
        record.manuallyUsed ? '已使用' : '未使用',
        record.manuallyUsed ? 'orange' : 'default',
      ) : null,
      isColumnVisible('subscription_type') ? renderMobileStatusPill('subscription_type', subscriptionMetaForMobile.label, subscriptionMetaForMobile.color) : null,
      isColumnVisible('account_validity') ? renderMobileStatusPill('account_validity', validityMetaForMobile.label, validityMetaForMobile.color) : null,
      isColumnVisible('idea_submit_status') ? renderMobileStatusPill('idea_submit_status', ideaMetaForMobile.label, ideaMetaForMobile.color) : null,
      isColumnVisible('password') ? renderMobileStatusPill(
        'password',
        hasPasswordForMobile ? '有密码' : '无密码',
        hasPasswordForMobile ? 'success' : 'default',
        hasPasswordForMobile ? <Button title="复制密码" type="text" size="small" icon={<CopyOutlined />} onClick={() => copyAccountSecret(record, 'password', '密码')} /> : null,
      ) : null,
      isChatgptPlatform && isColumnVisible('codex_usage') ? (
        <span key="codex_usage" style={{ minWidth: 180 }}>
          {renderCodexUsageState(record)}
        </span>
      ) : null,
      isChatgptPlatform && isColumnVisible('sub2api_state') ? renderSub2ApiState(record) : null,
      isChatgptPlatform && isColumnVisible('sub2api_upload_record') ? renderSub2ApiUploadRecord(record) : null,
      isChatgptPlatform && isColumnVisible('oaipay_state') ? renderOaipayState(record) : null,
      isChatgptPlatform && isColumnVisible('oaipay_upload_record') ? renderOaipayUploadRecord(record) : null,
    ].filter(Boolean)

    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10, minWidth: 0 }}>
          <Checkbox
            checked={helpers.checked}
            onChange={(event) => helpers.onCheckedChange(event.target.checked)}
            style={{ marginTop: 3, flex: '0 0 auto' }}
          />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
              <Tag color="default" style={{ ...compactTagStyle, flex: '0 0 auto' }}>{record.id}</Tag>
              <Text
                strong
                style={{
                  ...monospaceStyle,
                  flex: '1 1 auto',
                  minWidth: 0,
                  maxWidth: '100%',
                  fontSize: 13,
                  lineHeight: '22px',
                }}
                ellipsis={{ tooltip: email || '-' }}
              >
                {email || '-'}
              </Text>
              {email ? (
                <Button
                  title="复制邮箱"
                  type="text"
                  size="small"
                  icon={<CopyOutlined />}
                  style={{ flex: '0 0 auto' }}
                  onClick={async () => {
                    const ok = await copyText(email)
                    if (ok) {
                      await markAccountUsed(Number(record.id || 0))
                    }
                  }}
                />
              ) : null}
            </div>
          </div>
        </div>

        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, paddingLeft: 22 }}>
          {renderAccountStatusState(record.status, record, { inline: true })}
        </div>

        {teamInviteOwner ? (
          <Text type="secondary" style={{ paddingLeft: 22, fontSize: 11, lineHeight: '18px' }} ellipsis={{ tooltip: `${teamInviteOwner}${teamInviteMeta ? ` · ${teamInviteMeta}` : ''}` }}>
            {`${teamInviteOwner}${teamInviteMeta ? ` · ${teamInviteMeta}` : ''}`}
          </Text>
        ) : null}

        <div
          style={{
            minWidth: 0,
            border: `1px solid ${token.colorBorderSecondary}`,
            borderRadius: 10,
            padding: '8px 10px',
            background: token.colorFillAlter,
          }}
        >
          {mobileMetaParts.length > 0 ? (
            <div
              title={[
                isColumnVisible('created_at') ? `注册 ${registeredAt}` : '',
                isColumnVisible('subscription_active_until') ? `到期 ${subscriptionExpiry?.title || expiresAt}` : '',
              ].filter(Boolean).join(' · ')}
              style={{
                marginBottom: mobileStatusItems.length > 0 ? 9 : 0,
                color: token.colorTextSecondary,
                fontSize: 12,
                lineHeight: '20px',
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
            >
              {mobileMetaParts.join(' · ')}
            </div>
          ) : null}
          {mobileStatusItems.length > 0 ? (
            <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '9px 10px' }}>
              {mobileStatusItems.map((item, index) => (
                <span key={index} style={{ display: 'inline-flex', maxWidth: '100%' }}>{item}</span>
              ))}
            </div>
          ) : null}
        </div>

        {isColumnVisible('phone_binding') && hasPhoneBinding ? (
          <div
            style={{
              minWidth: 0,
              border: `1px solid ${token.colorBorderSecondary}`,
              borderRadius: 10,
              padding: '8px 10px',
              background: token.colorFillAlter,
            }}
          >
            <span style={mobileStatusLabelStyle}>手机号 / API</span>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 5, minWidth: 0 }}>
              {phoneBinding.phone ? (
                <Space size={4} style={{ minWidth: 0 }}>
                  <Text style={{ ...monospaceStyle, fontSize: 12 }} ellipsis={{ tooltip: phoneBinding.phone }}>
                    {phoneBinding.phone}
                  </Text>
                  <Button title="复制手机号" type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(phoneBinding.phone)} />
                </Space>
              ) : null}
              {phoneBinding.apiUrl ? (
                <Space size={4} style={{ minWidth: 0, maxWidth: '100%' }}>
                  <Text type="secondary" style={{ ...monospaceStyle, flex: 1, minWidth: 0, fontSize: 11 }} ellipsis={{ tooltip: phoneBinding.apiUrl }}>
                    {phoneBinding.apiUrl}
                  </Text>
                  <Button title="复制完整 API" type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(phoneBinding.apiUrl)} />
                </Space>
              ) : null}
              <Space size={8} wrap>
                {rawPhoneLine ? (
                  <Button size="small" type="link" icon={<CopyOutlined />} style={{ paddingInline: 0 }} onClick={() => copyText(rawPhoneLine)}>
                    复制整行
                  </Button>
                ) : null}
                {phoneSecondary ? <Text type="secondary" style={{ fontSize: 11 }}>{phoneSecondary}</Text> : null}
              </Space>
            </div>
          </div>
        ) : null}

        <div style={{ borderTop: `1px solid ${token.colorBorderSecondary}`, paddingTop: 8 }}>
          {renderAccountActions(record, true)}
        </div>
      </div>
    )
  }

  const selectedAccountItems = selectedRowKeys.map((key) => {
    const id = String(key)
    return selectedAccountSnapshots[id] || accounts.find((account) => String(account.id) === id) || { id }
  })

  const removeSelectedAccount = (accountId: React.Key) => {
    const id = String(accountId)
    setSelectedRowKeys((keys) => keys.filter((key) => String(key) !== id))
    setSelectedAccountSnapshots((prev) => {
      const next = { ...prev }
      delete next[id]
      return next
    })
  }

  const clearSelectedAccounts = () => {
    setSelectedRowKeys([])
    setSelectedAccountSnapshots({})
  }



  const subscriptionExpiryTableSortOrder =
    subscriptionExpirySortOrder === 'asc'
      ? 'ascend'
      : subscriptionExpirySortOrder === 'desc'
        ? 'descend'
        : null



  const columns: any[] = [
    {
      title: 'ID',
      dataIndex: 'id',
      key: 'id',
      width: 68,
      align: 'center',
      render: (id: number) => (
        <Text style={{ ...monospaceStyle, fontSize: 12 }}>
          {id}
        </Text>
      ),
    },
    {
      title: (
        <Input.Search
          allowClear
          size="small"
          placeholder="搜索邮箱"
          value={search}
          onChange={(event) => {
            const value = event.target.value
            setSearch(value)
            setColumnFilters((prev) => ({ ...prev, email: value }))
          }}
          onSearch={(value) => {
            const next = String(value || '').trim()
            setSearch(next)
            setColumnFilters((prev) => ({ ...prev, email: next }))
            setDebouncedSearch(next)
          }}
          onClick={(event) => event.stopPropagation()}
        />
      ),
      dataIndex: 'email',
      key: 'email',
      width: isCompactDesktop ? 210 : 230,
      render: (text: string, record: any) => renderAccountIdentity(text, record),
    },
    {
      title: renderColumnFilterTitle(
        '使用状态',
        columnFilters.manuallyUsed,
        MANUAL_USE_FILTER_OPTIONS,
        (next) => setColumnFilters((prev) => ({ ...prev, manuallyUsed: next })),
      ),
      dataIndex: 'manually_used',
      key: 'manually_used',
      width: 126,
      render: (_: any, record: any) => renderManualUsedState(record),
    },
    {
      title: '密码',
      dataIndex: 'password',
      key: 'password',
      width: 96,
      render: (text: string, record: any) => renderPasswordState(text, record),
    },
    {
      title: '手机号/API',
      key: 'phone_binding',
      width: 280,
      render: (_: any, record: any) => renderPhoneBindingState(record),
    },
    {
      title: renderColumnFilterTitle(
        '认证材料',
        columnFilters.authType,
        AUTH_TYPE_FILTER_OPTIONS,
        (next) => setColumnFilters((prev) => ({ ...prev, authType: next })),
      ),
      key: 'auth_type',
      width: 152,
      render: (_: any, record: any) => renderAuthTypeState(record),
    },
    {
      title: renderColumnFilterTitle(
        '业务状态',
        columnFilters.status,
        STATUS_FILTER_OPTIONS,
        (next) => {
          setColumnFilters((prev) => ({ ...prev, status: next }))
          setFilterStatus(next.join(','))
        },
      ),
      dataIndex: 'status',
      key: 'status',
      width: 190,
      render: (status: string, record: any) => renderAccountStatusState(status, record),
    },
  ]

  if (isChatgptPlatform) {
    columns.push(
      {
        title: renderColumnFilterTitle(
          '当前订阅',
          columnFilters.subscriptionType,
          SUBSCRIPTION_TYPE_FILTER_OPTIONS,
          (next) => setColumnFilters((prev) => ({ ...prev, subscriptionType: next })),
        ),
        key: 'subscription_type',
        width: 112,
        render: (_: any, record: any) => renderSubscriptionTypeState(record),
      },
      {
        title: '订阅到期',
        dataIndex: SUBSCRIPTION_EXPIRY_SORT_FIELD,
        key: 'subscription_active_until',
        width: 118,
        sorter: true,
        sortOrder: subscriptionExpiryTableSortOrder,
        render: (_: any, record: any) => renderSubscriptionExpiryState(record),
      },
      {
        title: renderColumnFilterTitle(
          '认证状态',
          columnFilters.accountValidity,
          ACCOUNT_VALIDITY_FILTER_OPTIONS,
          (next) => setColumnFilters((prev) => ({ ...prev, accountValidity: next })),
        ),
        key: 'account_validity',
        width: 116,
        render: (_: any, record: any) => renderAccountValidityState(record),
      },
      {
        title: renderColumnFilterTitle(
          'Idea提交',
          columnFilters.ideaSubmitState,
          IDEA_SUBMIT_FILTER_OPTIONS,
          (next) => setColumnFilters((prev) => ({ ...prev, ideaSubmitState: next })),
        ),
        key: 'idea_submit_status',
        width: 128,
        render: (_: any, record: any) => renderIdeaSubmitState(record),
      },
      {
        title: 'Codex用量',
        key: 'codex_usage',
        width: 206,
        render: (_: any, record: any) => renderCodexUsageState(record),
      },
      {
        title: renderColumnFilterTitle(
          'Sub2API',
          columnFilters.sub2apiState,
          SUB2API_FILTER_OPTIONS,
          (next) => setColumnFilters((prev) => ({ ...prev, sub2apiState: next })),
        ),
        key: 'sub2api_state',
        width: 106,
        render: (_: any, record: any) => renderSub2ApiState(record),
      },
      {
        title: 'Sub2API上传',
        key: 'sub2api_upload_record',
        width: 196,
        render: (_: any, record: any) => renderSub2ApiUploadRecord(record),
      },
      {
        title: renderColumnFilterTitle(
          'OAIPay',
          columnFilters.oaipayState,
          OAIPAY_FILTER_OPTIONS,
          (next) => setColumnFilters((prev) => ({ ...prev, oaipayState: next })),
        ),
        key: 'oaipay_state',
        width: 106,
        render: (_: any, record: any) => renderOaipayState(record),
      },
      {
        title: 'OAIPay上传',
        key: 'oaipay_upload_record',
        width: 196,
        render: (_: any, record: any) => renderOaipayUploadRecord(record),
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
      width: 76,
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
      width: isCompactDesktop ? 214 : 236,
      fixed: isChatgptPlatform ? 'right' : undefined,
      render: (_: any, record: any) => renderAccountActions(record),
    },
  )
  const visibleColumns = columns.filter((column) => isColumnVisible(String(column?.key || column?.dataIndex || '')))

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
      key: `probe:${getStatusSyncScope()}:config`,
      label:
        getStatusSyncScope() === 'selected'
          ? `同步所选本地状态 (${selectedRowKeys.length})，配置代理与延时`
          : `同步当前筛选本地状态 (${total})，配置代理与延时`,
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
    {
      key: `oaipay:${getStatusSyncScope()}`,
      label:
        getStatusSyncScope() === 'selected'
          ? `同步所选 OAIPay 状态 (${selectedRowKeys.length})`
          : `同步当前筛选 OAIPay 状态 (${total})`,
      disabled: getStatusSyncScope() === 'selected' ? selectedRowKeys.length === 0 : total === 0,
    },
  ]

  const backfillScope = getBackfillScope()
  const cliproxyapiBackfillDisabled = backfillScope === 'selected'
    ? selectedRowKeys.length === 0
    : getPendingBackfillCount('cliproxyapi') === 0
  const sub2apiBackfillDisabled = backfillScope === 'selected' ? selectedRowKeys.length === 0 : total === 0
  const oaipayBackfillDisabled = backfillScope === 'selected' ? selectedRowKeys.length === 0 : total === 0
  const backfillMenuItems: MenuProps['items'] = [
    {
      key: `cliproxyapi:${backfillScope}`,
      label: buildBackfillMenuLabel('cliproxyapi'),
      disabled: cliproxyapiBackfillDisabled,
    },
    {
      key: `sub2api:${backfillScope}`,
      label: buildBackfillMenuLabel('sub2api'),
      disabled: sub2apiBackfillDisabled,
    },
    {
      key: `oaipay:${backfillScope}`,
      label: buildBackfillMenuLabel('oaipay'),
      disabled: oaipayBackfillDisabled,
    },
  ]
  const resumeAuthScope = getResumeAuthScope()
  const resumeAuthMenuItems: MenuProps['items'] = [
    {
      key: resumeAuthScope,
      label: buildResumeAuthMenuLabel(),
      disabled: resumeAuthScope === 'selected' ? selectedRowKeys.length === 0 : total === 0,
    },
    {
      key: `${resumeAuthScope}:config`,
      label: buildResumeAuthMenuLabel(true),
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
            {formatGopayPhoneExpiryLabel(item.phone) ? <Tag color="processing">有效期 {formatGopayPhoneExpiryLabel(item.phone)}</Tag> : <Tag>有效期 -</Tag>}
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

  const phoneBindingPoolMode = normalizePhonePoolMode(phoneBindingPoolModeValue)
  const phoneBindingPrefixBindEnabled = phoneBindingPoolMode === 'prefix_limited'
  const phoneBindingPrefixSampleEnabled = phoneBindingPoolMode === 'prefix_sample'
  const phoneBindingPrefixModeActive = phoneBindingPrefixBindEnabled || phoneBindingPrefixSampleEnabled
  const phoneBindingSelectedPrefixes = normalizeSelectedPrefixes(phoneBindingSelectedPrefixesValue)
  const phoneBindingProxyMode = String(phoneBindingProxyModeValue || DEFAULT_PHONE_BINDING_SETTINGS.proxy_mode)
  const phoneBindingPrefixSampleSize = Number(phoneBindingPrefixSampleSizeValue) === 2 ? 2 : 1
  const phoneBindingPrefixSampleFilter = (() => {
    const value = String(phoneBindingPrefixSampleFilterValue || 'all')
    if (value === 'available') return 'available'
    if (value === 'rejected') return 'rejected'
    return 'all'
  })()
  const phoneBindingUsePool = phoneBindingPrefixModeActive || phoneBindingUsePoolValue !== false
  const phoneBindingManualText = String(phoneBindingPhoneLinesValue || '').trim()
  const phoneBindingShowManualInput = !phoneBindingPrefixModeActive && (phoneBindingManualOpen || !phoneBindingUsePool || Boolean(phoneBindingManualText))
  const phoneBindingPoolSummary = phonePoolSummary || {}
  const phoneBindingTargetCount = phoneBindingTestScope === 'selected' ? selectedRowKeys.length : total
  const phoneBindingSummaryAvailable = Number(phoneBindingPoolSummary.available || 0)
  const phoneBindingSummaryRemaining = Number(phoneBindingPoolSummary.remaining_capacity || 0)
  const phoneBindingSummaryRateLimited = Number(phoneBindingPoolSummary.rate_limited || 0)
  const phoneBindingSummaryUnavailable = Number(phoneBindingPoolSummary.unavailable || phoneBindingPoolSummary.cannot_send || 0)
  const phoneBindingSummaryExhausted = Number(phoneBindingPoolSummary.exhausted || 0)
  const phoneBindingSummaryDisabled = Number(phoneBindingPoolSummary.disabled || 0)
  const phoneBindingPrefixGroups: PhonePoolPrefixGroup[] = useMemo(() => {
    const summary = phonePoolSummary || {}
    const health = summary.prefix_health || {}
    return [
      {
        key: 'available' as const,
        label: '可用',
        color: 'success',
        items: uniquePhonePoolPrefixItems(health.available || summary.available_prefixes, 'available'),
      },
      {
        key: 'unavailable' as const,
        label: '不可用',
        color: 'error',
        items: uniquePhonePoolPrefixItems(health.unavailable || summary.rejected_prefixes, 'unavailable'),
      },
      {
        key: 'temporary' as const,
        label: '暂不可用',
        color: 'warning',
        items: uniquePhonePoolPrefixItems(health.temporary || summary.temporary_prefixes, 'temporary'),
      },
      {
        key: 'exhausted' as const,
        label: '已绑满',
        color: 'default',
        items: uniquePhonePoolPrefixItems(health.exhausted || summary.exhausted_prefixes, 'exhausted'),
      },
    ]
  }, [phonePoolSummary])
  const phoneBindingPrefixMap = useMemo(() => {
    const map = new Map<string, PhonePoolPrefixItem>()
    for (const group of phoneBindingPrefixGroups) {
      for (const item of group.items) {
        map.set(item.prefix, item)
      }
    }
    return map
  }, [phoneBindingPrefixGroups])
  const phoneBindingSelectedPrefixItems = phoneBindingSelectedPrefixes.map((prefix) => phoneBindingPrefixMap.get(prefix)).filter(Boolean) as PhonePoolPrefixItem[]
  const phoneBindingSelectedPrefixSet = useMemo(() => new Set(phoneBindingSelectedPrefixes), [phoneBindingSelectedPrefixes.join('|')])
  const phoneBindingLimitedAvailablePhones = phoneBindingSelectedPrefixItems.reduce((sum, item) => sum + Number(item.available_count || 0), 0)
  const phoneBindingLimitedRemainingCapacity = phoneBindingSelectedPrefixItems.reduce((sum, item) => sum + Number(item.remaining_capacity || 0), 0)
  const phoneBindingLimitedCapacity = phoneBindingReusePhoneValue ? phoneBindingLimitedRemainingCapacity : phoneBindingLimitedAvailablePhones
  const phoneBindingSelectedSampleCount = phoneBindingSelectedPrefixItems.reduce((sum, item) => {
    const candidateCount = Math.max(
      Number(item.available_count || 0),
      Number(item.remaining_capacity || 0) > 0 ? 1 : 0,
      Number(item.total || 0),
    )
    return sum + Math.min(candidateCount, phoneBindingPrefixSampleSize)
  }, 0)
  const phoneBindingSummaryPrefixCount = Number(
    phoneBindingSelectedPrefixes.length > 0 && phoneBindingPrefixSampleEnabled
      ? phoneBindingSelectedPrefixes.length
      : phoneBindingPrefixSampleFilter === 'available'
        ? (phoneBindingPoolSummary.available_prefix_count ?? phoneBindingPrefixGroups.find((group) => group.key === 'available')?.items.length ?? 0)
        : phoneBindingPrefixSampleFilter === 'rejected'
          ? (phoneBindingPoolSummary.rejected_prefix_count ?? phoneBindingPrefixGroups.find((group) => group.key === 'unavailable')?.items.length ?? 0)
          : (phoneBindingPoolSummary.prefix_sample_prefix_count ?? phoneBindingPrefixGroups.reduce((sum, group) => sum + group.items.length, 0)),
  )
  const phoneBindingSummarySampleCount = phoneBindingSelectedPrefixes.length > 0 && phoneBindingPrefixSampleEnabled
    ? phoneBindingSelectedSampleCount
    : Number(
        phoneBindingPrefixSampleFilter === 'available'
          ? (
              phoneBindingPrefixSampleSize === 2
                ? (phoneBindingPoolSummary.available_prefix_sample_2 ?? phoneBindingPoolSummary.available_prefix_sample_1)
                : (phoneBindingPoolSummary.available_prefix_sample_1 ?? phoneBindingPoolSummary.available_prefix_sample_2)
            )
          : phoneBindingPrefixSampleFilter === 'rejected'
          ? (
              phoneBindingPrefixSampleSize === 2
                ? phoneBindingPoolSummary.rejected_prefix_sample_2
                : phoneBindingPoolSummary.rejected_prefix_sample_1
            )
          : (
              phoneBindingPrefixSampleSize === 2
                ? (phoneBindingPoolSummary.prefix_sample_count_2 ?? phoneBindingPoolSummary.available_prefix_sample_2)
                : (phoneBindingPoolSummary.prefix_sample_count_1 ?? phoneBindingPoolSummary.available_prefix_sample_1)
            ),
      ) || 0
  const setPhoneBindingSelectedPrefixes = (nextPrefixes: unknown) => {
    const selected = normalizeSelectedPrefixes(nextPrefixes)
    phoneBindingTestForm.setFieldsValue({ selected_prefixes: selected })
    savePhoneBindingSettings({ ...phoneBindingTestForm.getFieldsValue(true), selected_prefixes: selected })
  }
  const togglePhoneBindingPrefix = (prefix: string) => {
    const next = phoneBindingSelectedPrefixSet.has(prefix)
      ? phoneBindingSelectedPrefixes.filter((item) => item !== prefix)
      : [...phoneBindingSelectedPrefixes, prefix]
    setPhoneBindingSelectedPrefixes(next)
  }
  const togglePhoneBindingPrefixGroup = (prefixes: string[]) => {
    const normalized = normalizeSelectedPrefixes(prefixes)
    if (normalized.length === 0) return
    const allSelected = normalized.every((prefix) => phoneBindingSelectedPrefixSet.has(prefix))
    const nextSet = new Set(phoneBindingSelectedPrefixes)
    for (const prefix of normalized) {
      if (allSelected) {
        nextSet.delete(prefix)
      } else {
        nextSet.add(prefix)
      }
    }
    setPhoneBindingSelectedPrefixes(Array.from(nextSet))
  }
  const formatPhoneBindingPrefixMeta = (item: PhonePoolPrefixItem) => {
    const parts: string[] = []
    const available = Number(item.available_count || 0)
    const remaining = Number(item.remaining_capacity || 0)
    const rejected = Number(item.rejected_count || 0)
    if (available > 0) parts.push(`可用 ${available}`)
    if (remaining > 0) parts.push(`容量 ${remaining}`)
    if (rejected > 0) parts.push(`拒 ${rejected}`)
    return parts.join(' · ')
  }
  const phoneBindingPoolEmpty =
    phoneBindingUsePool
    && phoneBindingManualText === ''
    && phoneBindingTargetCount > 0
    && phonePoolSummary !== null
    && !phonePoolSummaryLoading
    && (phoneBindingPrefixSampleEnabled
      ? phoneBindingSummarySampleCount <= 0
      : phoneBindingPrefixBindEnabled
        ? phoneBindingSelectedPrefixes.length > 0 && phoneBindingLimitedCapacity <= 0
        : phoneBindingSummaryRemaining <= 0)
  const phoneBindingPoolShortage =
    !phoneBindingPrefixSampleEnabled
    && phoneBindingUsePool
    && phoneBindingManualText === ''
    && phoneBindingTargetCount > 0
    && (phoneBindingPrefixBindEnabled
      ? phoneBindingSelectedPrefixes.length > 0 && phoneBindingLimitedCapacity > 0 && phoneBindingLimitedCapacity < phoneBindingTargetCount
      : phoneBindingSummaryRemaining > 0 && phoneBindingSummaryRemaining < phoneBindingTargetCount)
  const baxiCdkManualText = String(baxiCdkCodeLinesValue || '').trim()
  const baxiCdkUsePool = !baxiCdkManualText && baxiCdkUsePoolValue !== false
  const baxiCdkShowManualInput = baxiCdkManualOpen || Boolean(baxiCdkManualText) || !baxiCdkUsePool
  const baxiCdkSummary = baxiCdkPoolSummary || {}
  const baxiCdkTargetCount = baxiCdkSubmitScope === 'selected' ? selectedRowKeys.length : total
  const baxiCdkTargetSuccessLimit = Math.max(Number(baxiCdkTargetSuccessValue || 0), 0)
  const baxiCdkPlannedSuccessTarget = baxiCdkTargetSuccessLimit > 0 && baxiCdkTargetCount > 0
    ? Math.min(baxiCdkTargetSuccessLimit, baxiCdkTargetCount)
    : baxiCdkTargetCount
  const baxiCdkAvailable = Number(baxiCdkSummary.available || 0)
  const baxiCdkSubmitted = Number(baxiCdkSummary.submitted || 0) + Number(baxiCdkSummary.processing || 0)
  const baxiCdkPaid = Number(baxiCdkSummary.paid || 0)
  const baxiCdkFailed = Number(baxiCdkSummary.failed || 0)
  const baxiCdkDisabled = Number(baxiCdkSummary.disabled || 0)
  const baxiCdkManualCount = baxiCdkManualText
    ? baxiCdkManualText.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).length
    : 0
  const baxiCdkSelectedIds = normalizeBaxiCdkIdList(baxiCdkSelectedIdsValue)
  const baxiCdkPoolItemById = new Map(baxiCdkPoolItems.map((item) => [Number(item.id), item]))
  const baxiCdkSelectedItems = baxiCdkSelectedIds
    .map((id) => baxiCdkPoolItemById.get(Number(id)))
    .filter((item): item is BaxiGptCdkPoolItem => Boolean(item))
  const baxiCdkLoadedCapacity = baxiCdkPoolItems.reduce((sum, item) => sum + baxiCdkSubmitCapacity(item), 0)
  const baxiCdkSelectedCapacity = baxiCdkSelectedItems.reduce((sum, item) => sum + baxiCdkSubmitCapacity(item), 0)
  const baxiCdkEffectivePoolCount = baxiCdkSelectedIds.length > 0 ? baxiCdkSelectedItems.length : baxiCdkAvailable
  const baxiCdkEffectiveCapacity = baxiCdkSelectedIds.length > 0
    ? baxiCdkSelectedCapacity
    : (baxiCdkLoadedCapacity > 0 ? baxiCdkLoadedCapacity : baxiCdkAvailable)
  const baxiCdkRemainingAfterSubmit = baxiCdkPlannedSuccessTarget > 0
    ? Math.max(baxiCdkEffectiveCapacity - baxiCdkPlannedSuccessTarget, 0)
    : baxiCdkEffectiveCapacity
  const baxiCdkPoolOptions = baxiCdkPoolItems.map((item) => {
    const codeText = String(item.code_value || item.code_masked || `#${item.id}`)
    const labelText = String(item.label || '').trim()
    const quotaText = baxiCdkQuotaLabel(item)
    const errorText = String(item.last_error_message || '').trim()
    return {
      value: Number(item.id),
      searchText: `${codeText} ${labelText} ${item.code_masked || ''}`.trim(),
      label: (
        <Space size={6} wrap style={{ maxWidth: 760 }}>
          <Text code ellipsis={{ tooltip: codeText }} style={{ maxWidth: 340 }}>{codeText}</Text>
          {labelText ? <Text type="secondary" ellipsis={{ tooltip: labelText }} style={{ maxWidth: 190 }}>{labelText}</Text> : null}
          <Tag color={baxiCdkRemainingValue(item) > 0 ? 'success' : 'default'}>{quotaText}</Tag>
          {errorText ? <Tag color="warning">有错误</Tag> : null}
        </Space>
      ),
    }
  })
  const baxiCdkPoolEmpty =
    baxiCdkUsePool
    && !baxiCdkManualText
    && baxiCdkTargetCount > 0
    && baxiCdkPoolSummary !== null
    && !baxiCdkPoolSummaryLoading
    && !baxiCdkPoolItemsLoading
    && baxiCdkEffectiveCapacity <= 0
  const baxiCdkPoolShortage =
    baxiCdkUsePool
    && !baxiCdkManualText
    && baxiCdkPlannedSuccessTarget > 0
    && baxiCdkEffectiveCapacity > 0
    && baxiCdkEffectiveCapacity < baxiCdkPlannedSuccessTarget
  const baxiCdkManualOverflow = baxiCdkManualCount > 0 && baxiCdkPlannedSuccessTarget > 0 && baxiCdkManualCount > baxiCdkPlannedSuccessTarget
  const paypalFilteredEligibleCountLabel = paypalFilteredEligibleLoading
    ? '统计中'
    : paypalFilteredEligibleCount === null
      ? '-'
      : String(paypalFilteredEligibleCount)

  return (
    <div
      style={{
        width: '100%',
        maxWidth: '100%',
        minWidth: 0,
        height: isMobile ? 'auto' : 'calc(100vh - 48px)',
        overflow: isMobile ? 'visible' : 'hidden',
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      <AccountsToolbar
        total={total}
        selectedRowKeys={selectedRowKeys}
        columnVisibilityControl={renderColumnVisibilityControl()}
        activeTasksLoading={activeTasksLoading}
        activeTasks={activeTasks}
        onOpenTaskSnapshot={openTaskFromSnapshot}
        onRefreshActiveTasks={refreshActiveTasks}
        onActiveTasksOpen={() => setActiveTasksPanelOpen(true)}
        isChatgptPlatform={currentPlatform === 'chatgpt'}
        batchGopayLoading={batchGopayLoading}
        batchPaymentLinkLoading={batchPaymentLinkLoading}
        batchInvalidRecheckLoading={batchInvalidRecheckLoading}
        batchK12RecaptureLoading={batchK12RecaptureLoading}
        phoneBindingTestLoading={phoneBindingTestLoading}
        paypalBindingLoading={paypalBindingLoading}
        baxiCdkSubmitLoading={baxiCdkSubmitLoading}
        onBatchPaymentLink={handleBatchPaymentLink}
        onBatchInvalidRecheck={handleBatchInvalidRecheck}
        onOpenBatchK12Recapture={() => { void openBatchK12Recapture() }}
        onOpenPhoneBindingTest={() => { void openPhoneBindingTest() }}
        onOpenPaypalBinding={openPaypalBinding}
        onOpenBaxiCdkSubmit={openBaxiCdkSubmit}
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
          const rawKey = String(key)
          const [kind, scopeKey, modeKey] = rawKey.split(':')
          const scope = scopeKey as 'selected' | 'all'
          if (kind === 'probe' && modeKey === 'config') {
            void openBatchProbeStatusConfig(scope)
            return
          }
          void handleBatchStatusSync(kind as any, scope)
        }}
        statusSyncLoading={statusSyncLoading}
        resumeAuthMenuItems={resumeAuthMenuItems}
        onResumeAuthClick={({ key }) => {
          const rawKey = String(key)
          const [scopeKey, modeKey] = rawKey.split(':')
          const scope = scopeKey === 'selected' ? 'selected' : 'filtered'
          if (modeKey === 'config') {
            void openBatchResumeAuthConfig(scope)
            return
          }
          void handleBatchResumeSubscriptionAuth(scope)
        }}
        resumeAuthLoading={batchResumeAuthLoading}
        backfillMenuItems={backfillMenuItems}
        onBackfillClick={({ key }) => {
          const [destination, scope] = String(key).split(':') as ['cliproxyapi' | 'sub2api' | 'oaipay', 'selected' | 'pending']
          if (destination === 'oaipay') {
            void openOaipayUploadModal(scope)
          } else {
            void handleBackfill(destination, scope)
          }
        }}
        backfillLoading={backfillLoading}
        isMobile={isMobile}
      />

      <FilterPresetBar
        isMobile={isMobile}
        token={token}
        filterPresetLoading={filterPresetLoading}
        activeFilterPresetId={activeFilterPresetId}
        filterPresets={filterPresets}
        pinnedFilterPresets={pinnedFilterPresets}
        activeFilterPreset={activeFilterPreset}
        currentFilterPresetFilters={currentFilterPresetFilters}
        total={total}
        activeFilterPresetDirty={activeFilterPresetDirty}
        filterPresetSaving={filterPresetSaving}
        applyFilterPreset={applyFilterPreset}
        clearFilterPreset={clearFilterPreset}
        openCreateCurrentFilterPreset={openCreateCurrentFilterPreset}
        setFilterPresetManageOpen={setFilterPresetManageOpen}
        loadFilterPresets={loadFilterPresets}
        overwriteActiveFilterPreset={overwriteActiveFilterPreset}
        openCopyFilterPreset={openCopyFilterPreset}
      />

      <SelectedAccountsSummary
        isMobile={isMobile}
        token={token}
        selectedAccountItems={selectedAccountItems}
        removeSelectedAccount={removeSelectedAccount}
        clearSelectedAccounts={clearSelectedAccounts}
      />

      <AccountsTable
        columns={visibleColumns}
        accounts={accounts}
        loading={loading}
        total={total}
        currentPage={currentPage}
        pageSize={accountsPageSize}
        onPageChange={setCurrentPage}
        onPageSizeChange={handleAccountsPageSizeChange}
        pageSizeOptions={ACCOUNT_PAGE_SIZE_OPTIONS}
        selectedRowKeys={selectedRowKeys}
        setSelectedRowKeys={setSelectedRowKeys}
        onTableChange={handleAccountsTableChange}
        filterSummary={renderMobileFilterControls()}
        isMobile={isMobile}
        renderMobileCard={renderAccountMobileCard}
        onOpenDetail={(record) => {
          setDetailAccount(record)
          setDetailModalOpen(true)
        }}
      />

      <Modal
        title={filterPresetEditorMode === 'edit-meta' ? '编辑筛选组合与条件' : filterPresetEditorMode === 'copy-preset' ? '复制筛选组合' : '保存当前筛选'}
        open={filterPresetEditorOpen}
        onOk={() => { void saveFilterPresetForm() }}
        onCancel={() => setFilterPresetEditorOpen(false)}
        okText={filterPresetEditorMode === 'edit-meta' ? '更新组合' : '创建组合'}
        confirmLoading={filterPresetSaving}
        destroyOnClose
        width={700}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Alert
            type="info"
            showIcon
            message="筛选组合只保存条件，不保存账号 ID"
            description="您可以编辑名称、描述及置顶状态，也可以直接在下方配置修改筛选条件；点击右上角“从当前页面填充”可一键同步页面上当前的筛选状态。"
          />
          <Form form={filterPresetForm} layout="vertical" preserve={false}>
            <Form.Item
              name="name"
              label="组合名称"
              rules={[{ required: true, message: '请输入筛选组合名称' }, { max: 80, message: '名称最多 80 个字符' }]}
            >
              <Input placeholder="例如：Plus 长效未上传 OAIPay" />
            </Form.Item>
            <Form.Item name="description" label="描述" rules={[{ max: 240, message: '描述最多 240 个字符' }]}>
              <Input.TextArea rows={2} placeholder="说明这组条件的用途，便于后续区分" />
            </Form.Item>
            <Form.Item name="pinned" valuePropName="checked" style={{ marginBottom: 12 }}>
              <Checkbox>置顶到账号页快捷筛选</Checkbox>
            </Form.Item>
            <div
              style={{
                padding: 14,
                borderRadius: 10,
                background: token.colorFillAlter,
                border: `1px solid ${token.colorBorderSecondary}`,
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                <Text strong style={{ fontSize: 13 }}>筛选条件配置（可自由修改）</Text>
                <Button
                  size="small"
                  type="link"
                  icon={<SyncOutlined />}
                  onClick={() => fillFilterFormFields(currentFilterPresetFilters)}
                  style={{ padding: 0 }}
                >
                  从当前页面筛选填充
                </Button>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: isMobile ? '1fr' : '1fr 1fr', gap: 12 }}>
                <Form.Item name="search" label="关键词搜索" style={{ marginBottom: 0 }}>
                  <Input placeholder="邮箱或关键词" allowClear />
                </Form.Item>
                <Form.Item name="status" label="业务状态" style={{ marginBottom: 0 }}>
                  <Select mode="multiple" placeholder="全部业务状态" options={STATUS_FILTER_OPTIONS} allowClear />
                </Form.Item>
                <Form.Item name="authType" label="认证材料" style={{ marginBottom: 0 }}>
                  <Select mode="multiple" placeholder="全部认证材料" options={AUTH_TYPE_FILTER_OPTIONS} allowClear />
                </Form.Item>
                <Form.Item name="subscriptionType" label="当前订阅" style={{ marginBottom: 0 }}>
                  <Select mode="multiple" placeholder="全部当前订阅" options={SUBSCRIPTION_TYPE_FILTER_OPTIONS} allowClear />
                </Form.Item>
                <Form.Item name="accountValidity" label="认证状态" style={{ marginBottom: 0 }}>
                  <Select mode="multiple" placeholder="全部认证状态" options={ACCOUNT_VALIDITY_FILTER_OPTIONS} allowClear />
                </Form.Item>
                <Form.Item name="codexState" label="Codex 状态" style={{ marginBottom: 0 }}>
                  <Select mode="multiple" placeholder="全部 Codex 状态" options={CODEX_STATE_FILTER_OPTIONS} allowClear />
                </Form.Item>
                <Form.Item name="sub2apiState" label="Sub2Api 状态" style={{ marginBottom: 0 }}>
                  <Select mode="multiple" placeholder="全部 Sub2Api 状态" options={SUB2API_FILTER_OPTIONS} allowClear />
                </Form.Item>
                <Form.Item name="oaipayState" label="OAIPay 状态" style={{ marginBottom: 0 }}>
                  <Select mode="multiple" placeholder="全部 OAIPay 状态" options={OAIPAY_FILTER_OPTIONS} allowClear />
                </Form.Item>
                <Form.Item name="ideaSubmitState" label="Idea 提交" style={{ marginBottom: 0 }}>
                  <Select mode="multiple" placeholder="全部 Idea 提交状态" options={IDEA_SUBMIT_FILTER_OPTIONS} allowClear />
                </Form.Item>
                <Form.Item name="sortOrder" label="到期时间排序" style={{ marginBottom: 0 }}>
                  <Select placeholder="默认排序" options={SUBSCRIPTION_EXPIRY_SORT_OPTIONS} allowClear />
                </Form.Item>
                <Form.Item name="pageSize" label="每页条数" style={{ marginBottom: 0 }}>
                  <Select options={ACCOUNT_PAGE_SIZE_OPTIONS.map((n) => ({ value: n, label: `${n} 条/页` }))} />
                </Form.Item>
              </div>
            </div>
          </Form>
        </Space>
      </Modal>

      <Modal
        title="筛选组合管理"
        open={filterPresetManageOpen}
        onCancel={() => setFilterPresetManageOpen(false)}
        footer={[
          <Button key="refresh" icon={<SyncOutlined spin={filterPresetLoading} />} onClick={() => void loadFilterPresets(false)}>
            刷新
          </Button>,
          <Button key="new" type="primary" icon={<PlusOutlined />} onClick={openCreateCurrentFilterPreset}>
            保存当前筛选
          </Button>,
          <Button key="close" onClick={() => setFilterPresetManageOpen(false)}>
            关闭
          </Button>,
        ]}
        width={860}
      >
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          {filterPresets.map((preset) => (
            <div
              key={preset.id}
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 12,
                padding: '10px 12px',
                border: `1px solid ${preset.id === activeFilterPresetId ? token.colorPrimaryBorder : token.colorBorderSecondary}`,
                borderRadius: 12,
                background: preset.id === activeFilterPresetId ? token.colorPrimaryBg : token.colorFillAlter,
              }}
            >
              <div style={{ minWidth: 0, flex: 1 }}>
                <Space size={6} wrap style={{ marginBottom: 4 }}>
                  <Text strong>{preset.name}</Text>
                  {preset.built_in ? <Tag style={{ marginInlineEnd: 0 }}>内置</Tag> : null}
                  {preset.pinned ? <Tag color="processing" style={{ marginInlineEnd: 0 }}>置顶</Tag> : null}
                  {preset.id === activeFilterPresetId ? <Tag color="success" style={{ marginInlineEnd: 0 }}>当前</Tag> : null}
                </Space>
                {preset.description ? (
                  <Text type="secondary" style={{ display: 'block', fontSize: 12, marginBottom: 4 }}>
                    {preset.description}
                  </Text>
                ) : null}
                <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>
                  {preset.summary || buildAccountFilterPresetSummary(preset.filters)}
                </Text>
              </div>
              <Space size={6} wrap>
                <Button size="small" onClick={() => {
                  applyFilterPreset(preset)
                  setFilterPresetManageOpen(false)
                }}>
                  应用
                </Button>
                <Button size="small" icon={<EditOutlined />} onClick={() => openEditFilterPresetMeta(preset)}>
                  编辑
                </Button>
                <Popconfirm
                  title={`确认将当前页面的筛选条件覆盖保存到「${preset.name}」？`}
                  onConfirm={() => { void overwritePresetWithCurrent(preset) }}
                >
                  <Button size="small" icon={<SyncOutlined />} loading={filterPresetSaving}>
                    覆盖条件
                  </Button>
                </Popconfirm>
                <Button size="small" icon={<CopyOutlined />} onClick={() => openCopyFilterPreset(preset)}>
                  复制
                </Button>
                <Popconfirm
                  title={preset.built_in ? `确认删除内置筛选组合「${preset.name}」？删除后会在当前实例隐藏。` : `确认删除筛选组合「${preset.name}」？`}
                  onConfirm={() => { void deleteFilterPreset(preset) }}
                >
                  <Button size="small" danger icon={<DeleteOutlined />} loading={filterPresetSaving}>
                    删除
                  </Button>
                </Popconfirm>
              </Space>
            </div>
          ))}
          {!filterPresetLoading && filterPresets.length === 0 ? (
            <Alert type="info" showIcon message="暂无筛选组合" description="点击“保存当前筛选”即可把当前账号列表条件保存为一组。" />
          ) : null}
        </Space>
      </Modal>

      <BatchGopayWorkbench
        open={batchGopayOpen}
        onClose={() => {
          setBatchGopayOpen(false)
          setBatchGopayPhoneCountryCode(DEFAULT_GOPAY_PHONE_COUNTRY_CODE)
          setBatchGopayPhoneNumber('')
          setBatchGopayRecognizedCountryCodes([DEFAULT_GOPAY_PHONE_COUNTRY_CODE])
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
        onPhoneNumberChange={updateBatchGopayPhoneNumberInput}
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
        formatGopayPhoneExpiryLabel={formatGopayPhoneExpiryLabel}
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

      <Modal
        title={batchPaymentLinkForceRefresh ? '强制重新生成订阅链接' : '批量生成订阅链接'}
        open={batchPaymentLinkConfigOpen}
        onCancel={() => setBatchPaymentLinkConfigOpen(false)}
        onOk={submitBatchPaymentLinkConfig}
        confirmLoading={batchPaymentLinkLoading}
        okText={batchPaymentLinkForceRefresh ? '开始重新生成' : '开始生成'}
        cancelText="取消"
        maskClosable={false}
      >
        <Form form={batchPaymentLinkConfigForm} layout="vertical">
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={
              selectedRowKeys.length > 0
                ? `范围：当前选中 ${selectedRowKeys.length} 个账号`
                : `范围：当前筛选结果 ${total} 个账号`
            }
            description={
              batchPaymentLinkForceRefresh
                ? '会绕过已有缓存重新生成，但账号已失效时仍会跳过。其他支付参数继续沿用全局或账号默认配置。'
                : '默认可复用缓存；如果缓存参数不匹配，会按这里选择的路径重新生成。其他支付参数继续沿用全局或账号默认配置。'
            }
          />
          <Form.Item
            name="payment_link_format"
            label="生成路径"
            initialValue={DEFAULT_PAYMENT_LINK_FORMAT}
            rules={[{ required: true, message: '请选择生成路径' }]}
          >
            <Select options={PAYMENT_LINK_FORMAT_OPTIONS} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={resumeAuthConfigMode === 'single' ? '补抓 Auth 配置' : '批量补抓 Auth 配置'}
        open={resumeAuthConfigOpen}
        onCancel={() => setResumeAuthConfigOpen(false)}
        onOk={submitResumeAuthConfig}
        confirmLoading={Boolean(resumeAuthAccountId || batchResumeAuthLoading)}
        okText="启动补抓"
        cancelText="取消"
        maskClosable={false}
      >
        <Form form={resumeAuthConfigForm} layout="vertical">
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={
              resumeAuthConfigMode === 'single'
                ? `账号：${resumeAuthConfigAccount?.email || resumeAuthConfigAccount?.id || '-'}`
                : resumeAuthConfigScope === 'selected'
                  ? `范围：当前选中 ${selectedRowKeys.length} 个账号`
                  : `范围：当前筛选结果 ${total} 个账号`
            }
            description="这里是本次任务的临时覆盖项；默认值来自“全局配置 > ChatGPT > 补抓 Auth”。勾选后，遇到 add_phone 会调用已配置的手机验证码 API。"
          />
          <Form.Item name="allow_phone_verification" valuePropName="checked" initialValue={false}>
            <Checkbox>允许 add_phone 后使用手机验证码 API</Checkbox>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="批量同步本地状态配置"
        open={batchProbeStatusConfigOpen}
        onCancel={() => setBatchProbeStatusConfigOpen(false)}
        onOk={submitBatchProbeStatusConfig}
        confirmLoading={Boolean(statusSyncLoading)}
        okText="开始同步"
        cancelText="取消"
        width={720}
        maskClosable={false}
      >
        <Form form={batchProbeStatusConfigForm} layout="vertical">
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message={
              batchProbeStatusConfigScope === 'selected'
                ? `范围：当前选中 ${selectedRowKeys.length} 个账号`
                : `范围：当前筛选结果 ${total} 个账号`
            }
            description="可参考注册任务设置请求代理模式、可用代理失败后重试以及每次处理账号之间的随机延时时间。"
          />
          <Form.Item label="每次请求间延时 (秒)">
            <Space style={{ display: 'flex' }}>
              <Form.Item name="register_delay_seconds" noStyle initialValue={0}>
                <InputNumber min={0} max={3600} step={1} style={{ width: 140 }} placeholder="最小延时" />
              </Form.Item>
              <span>至</span>
              <Form.Item name="register_delay_max_seconds" noStyle initialValue={0}>
                <InputNumber min={0} max={3600} step={1} style={{ width: 140 }} placeholder="最大延时" />
              </Form.Item>
              <span style={{ color: '#888', marginLeft: 8 }}>（都填 0 为无延时，填不同数值则在区间内随机）</span>
            </Space>
          </Form.Item>

          <Form.Item label="代理模式" name="proxy_mode" initialValue="pool">
            <Select style={{ width: 260 }}>
              <Select.Option value="pool">代理池自动选取</Select.Option>
              <Select.Option value="specified">手动指定代理</Select.Option>
              <Select.Option value="dynamic">动态代理</Select.Option>
              <Select.Option value="direct">直连 (不使用代理)</Select.Option>
            </Select>
          </Form.Item>

          {(probeProxyModeValue === 'specified' || probeProxyModeValue === 'dynamic') && (
            <Form.Item
              label={probeProxyModeValue === 'dynamic' ? '动态代理模板（可选覆盖）' : '代理地址'}
              name="proxy"
              rules={probeProxyModeValue === 'specified' ? [{ required: true, message: '请输入代理地址' }] : undefined}
              extra={probeProxyModeValue === 'dynamic' ? '留空使用全局动态代理模板；填写后仅本次同步覆盖全局模板。' : undefined}
            >
              <Input placeholder={probeProxyModeValue === 'dynamic' ? '可留空；或填 socks5://user-region-JP-sid-xxxx-t-15:pass@host:port' : 'http://user:pass@host:port 或 socks5://...'} />
            </Form.Item>
          )}

          {(probeProxyModeValue === 'pool' || probeProxyModeValue === 'dynamic' || (probeProxyModeValue === 'specified' && probeProxyFailoverValue)) && (
            <Space style={{ display: 'flex', flexWrap: 'wrap', gap: 16 }} align="baseline">
              <Form.Item
                label="目标国家 (ISO 缩写)"
                name="proxy_country_code"
                rules={probeProxyModeValue === 'dynamic' ? [{ required: true, message: '请输入动态代理出口国家' }] : undefined}
              >
                <Input style={{ width: 140 }} placeholder={probeProxyModeValue === 'dynamic' ? '必填，如 US' : '如 US, JP, 不填则不限'} />
              </Form.Item>
              {probeProxyModeValue !== 'dynamic' ? (
                <>
              <Form.Item label="最低健康度分数" name="proxy_min_score" initialValue={50}>
                <InputNumber min={0} max={100} step={5} style={{ width: 140 }} />
              </Form.Item>
              <Form.Item label="候选代理数量" name="proxy_max_candidates" initialValue={5}>
                <InputNumber min={1} max={20} step={1} style={{ width: 140 }} />
              </Form.Item>
                </>
              ) : null}
            </Space>
          )}

          {probeProxyModeValue !== 'direct' && (
            <Form.Item name="proxy_failover" valuePropName="checked" initialValue={false}>
              <Checkbox>{probeProxyModeValue === 'dynamic' ? '失败后刷新 sid 重试' : '使用多个候选代理，遇到网络失败时自动切换'}</Checkbox>
            </Form.Item>
          )}
        </Form>
      </Modal>

      <Modal
        title="批量重新进入并导出 K12 / Workspace"
        open={batchK12RecaptureOpen}
        onCancel={() => setBatchK12RecaptureOpen(false)}
        onOk={submitBatchK12Recapture}
        confirmLoading={batchK12RecaptureLoading}
        okText="开始重跑"
        cancelText="取消"
        width={760}
        maskClosable={false}
      >
        <Form form={batchK12RecaptureForm} layout="vertical">
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message={
              batchK12RecaptureScope === 'selected'
                ? `范围：当前选中 ${selectedRowKeys.length} 个账号`
                : `范围：当前筛选结果 ${total} 个账号`
            }
            description="复用每个账号已保存的 AccessToken + cookies/session_token，重新 join K12、拉取 accounts/check 空间并写回 workspace variants；不会在结果里展示 token/cookies 原文。"
          />
          <Form.Item name="scope" label="账号范围" initialValue={batchK12RecaptureScope}>
            <Select
              value={batchK12RecaptureScope}
              onChange={(value) => {
                setBatchK12RecaptureScope(value)
                batchK12RecaptureForm.setFieldsValue({ scope: value })
              }}
              options={[
                { value: 'selected', label: `当前选中账号（${selectedRowKeys.length}）`, disabled: selectedRowKeys.length === 0 },
                { value: 'filtered', label: `当前筛选账号（${total}）` },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="workspace_ids"
            label="目标 K12 workspace_id"
            extra="留空时只导出当前可见空间；多个 ID 支持换行、逗号或空格分隔。"
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
          <Form.Item label="账号间延时 (秒)">
            <Space style={{ display: 'flex' }}>
              <Form.Item name="delay_seconds" noStyle initialValue={0}>
                <InputNumber min={0} max={3600} step={1} style={{ width: 140 }} placeholder="最小延时" />
              </Form.Item>
              <span>至</span>
              <Form.Item name="delay_max_seconds" noStyle initialValue={0}>
                <InputNumber min={0} max={3600} step={1} style={{ width: 140 }} placeholder="最大延时" />
              </Form.Item>
              <span style={{ color: '#888', marginLeft: 8 }}>（都填 0 为无延时，填不同数值则在区间内随机）</span>
            </Space>
          </Form.Item>
          <Form.Item label="代理模式" name="proxy_mode" initialValue="pool">
            <Select style={{ width: 260 }}>
              <Select.Option value="pool">代理池自动选取</Select.Option>
              <Select.Option value="specified">手动指定代理</Select.Option>
              <Select.Option value="dynamic">动态代理</Select.Option>
              <Select.Option value="direct">直连 (不使用代理)</Select.Option>
            </Select>
          </Form.Item>

          {(batchK12ProxyModeValue === 'specified' || batchK12ProxyModeValue === 'dynamic') && (
            <Form.Item
              label={batchK12ProxyModeValue === 'dynamic' ? '动态代理模板（可选覆盖）' : '代理地址'}
              name="proxy"
              rules={batchK12ProxyModeValue === 'specified' ? [{ required: true, message: '请输入代理地址' }] : undefined}
              extra={batchK12ProxyModeValue === 'dynamic' ? '留空使用全局动态代理模板；填写后仅本次重跑覆盖全局模板。' : undefined}
            >
              <Input placeholder={batchK12ProxyModeValue === 'dynamic' ? '可留空；或填 socks5://user-region-JP-sid-xxxx-t-15:pass@host:port' : 'http://user:pass@host:port 或 socks5://...'} />
            </Form.Item>
          )}

          {(batchK12ProxyModeValue === 'pool' || batchK12ProxyModeValue === 'dynamic' || (batchK12ProxyModeValue === 'specified' && batchK12ProxyFailoverValue)) && (
            <Space style={{ display: 'flex', flexWrap: 'wrap', gap: 16 }} align="baseline">
              <Form.Item
                label="目标国家 (ISO 缩写)"
                name="proxy_country_code"
                rules={batchK12ProxyModeValue === 'dynamic' ? [{ required: true, message: '请输入动态代理出口国家' }] : undefined}
              >
                <Input style={{ width: 140 }} placeholder={batchK12ProxyModeValue === 'dynamic' ? '必填，如 US' : '如 US, JP, 不填则不限'} />
              </Form.Item>
              {batchK12ProxyModeValue !== 'dynamic' ? (
                <>
                  <Form.Item label="最低健康度分数" name="proxy_min_score" initialValue={50}>
                    <InputNumber min={0} max={100} step={5} style={{ width: 140 }} />
                  </Form.Item>
                  <Form.Item label="候选代理数量" name="proxy_max_candidates" initialValue={5}>
                    <InputNumber min={1} max={20} step={1} style={{ width: 140 }} />
                  </Form.Item>
                </>
              ) : null}
            </Space>
          )}

          {batchK12ProxyModeValue !== 'direct' && (
            <Form.Item name="proxy_failover" valuePropName="checked" initialValue={true}>
              <Checkbox>{batchK12ProxyModeValue === 'dynamic' ? '失败后刷新 sid 重试' : '使用多个候选代理，遇到网络失败时自动切换'}</Checkbox>
            </Form.Item>
          )}
        </Form>
      </Modal>

      <Modal
        title="OpenAI 手机号绑定"
        open={phoneBindingTestOpen}
        onCancel={() => setPhoneBindingTestOpen(false)}
        onOk={submitPhoneBindingTest}
        confirmLoading={phoneBindingTestLoading}
        okText="开始绑定"
        cancelText="取消"
        width={900}
        maskClosable={false}
      >
        <Form
          form={phoneBindingTestForm}
          layout="vertical"
          onValuesChange={(_, allValues) => savePhoneBindingSettings(allValues)}
        >
          <Form.Item name="selected_prefixes" hidden>
            <Select mode="multiple" options={[]} />
          </Form.Item>
          <Form.Item name="prefix_sample_enabled" valuePropName="checked" hidden>
            <Switch />
          </Form.Item>
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={
              phoneBindingTestScope === 'selected'
                ? `范围：当前选中 ${selectedRowKeys.length} 个账号`
                : `范围：当前筛选结果 ${total} 个账号`
            }
            description="会真实提交手机号验证码并绑定到 OpenAI 账号。"
          />
          <Form.Item name="scope" label="账号范围" initialValue={phoneBindingTestScope}>
            <Select
              value={phoneBindingTestScope}
              onChange={(value) => setPhoneBindingTestScope(value)}
              options={[
                { value: 'selected', label: `当前选中账号（${selectedRowKeys.length}）`, disabled: selectedRowKeys.length === 0 },
                { value: 'filtered', label: `当前筛选账号（${total}）` },
              ]}
            />
          </Form.Item>

          <Form.Item label="号码来源">
            <div
              style={{
                display: 'flex',
                gap: 10,
                alignItems: 'center',
                justifyContent: 'space-between',
                flexWrap: 'wrap',
              }}
            >
              <Space wrap>
                <Form.Item name="use_pool" valuePropName="checked" noStyle>
                  <Switch
                    checkedChildren="手机号池"
                    unCheckedChildren="临时号码"
                    disabled={phoneBindingPrefixModeActive}
                    onChange={(checked) => {
                      if (!checked) setPhoneBindingManualOpen(true)
                    }}
                  />
                </Form.Item>
                <Button
                  size="small"
                  icon={phonePoolSummaryLoading ? <SyncOutlined spin /> : <SyncOutlined />}
                  onClick={() => {
                    void loadPhonePoolSummary(false)
                  }}
                >
                  刷新库存
                </Button>
              </Space>
              <Space wrap size={[4, 6]}>
                <Tag color="blue">可用 {phoneBindingSummaryAvailable}</Tag>
                <Tag color="cyan">容量 {phoneBindingSummaryRemaining}</Tag>
                <Tag color={phoneBindingSummaryRateLimited > 0 ? 'warning' : 'default'}>限流 {phoneBindingSummaryRateLimited}</Tag>
                <Tag color={phoneBindingSummaryUnavailable > 0 ? 'error' : 'default'}>不可用 {phoneBindingSummaryUnavailable}</Tag>
                <Tag>已绑满 {phoneBindingSummaryExhausted}</Tag>
                <Tag>停用 {phoneBindingSummaryDisabled}</Tag>
              </Space>
            </div>
            <Text type="secondary" style={{ display: 'block', marginTop: 6 }}>
              普通绑定可用手机号池或临时粘贴号码；限定号段绑定和号段抽样固定从手机号池取号。
            </Text>
            <div
              style={{
                marginTop: 10,
                paddingTop: 10,
                borderTop: `1px solid ${token.colorBorderSecondary}`,
              }}
            >
              <div style={{ display: 'flex', gap: 12, alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap' }}>
                <Space wrap size={[8, 8]}>
                  <Text strong>取号策略</Text>
                  <Form.Item name="phone_pool_mode" noStyle>
                    <Segmented
                      size="small"
                      options={[
                        { label: '普通绑定', value: 'normal' },
                        { label: '限定号段绑定', value: 'prefix_limited' },
                        { label: '号段抽样测试', value: 'prefix_sample' },
                      ]}
                      onChange={(value) => {
                        const mode = normalizePhonePoolMode(value)
                        phoneBindingTestForm.setFieldsValue({
                          phone_pool_mode: mode,
                          prefix_sample_enabled: mode === 'prefix_sample',
                          use_pool: mode === 'normal' ? phoneBindingUsePoolValue !== false : true,
                          phone_lines: mode === 'normal' ? phoneBindingPhoneLinesValue : '',
                          reuse_phone_until_unusable: mode === 'prefix_sample' ? false : phoneBindingReusePhoneValue,
                        })
                        if (mode !== 'normal') {
                          setPhoneBindingManualOpen(false)
                        }
                      }}
                    />
                  </Form.Item>
                </Space>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {phoneBindingPrefixBindEnabled
                    ? `只使用所选号段正式绑定；当前选择 ${phoneBindingSelectedPrefixes.length} 个号段，可覆盖 ${phoneBindingLimitedCapacity} 个账号`
                    : phoneBindingPrefixSampleEnabled
                      ? phoneBindingSelectedPrefixes.length > 0
                        ? `按所选号段抽样，预计测试 ${phoneBindingSummarySampleCount} 个号码`
                        : `按范围抽样 ${phoneBindingSummaryPrefixCount} 个号段，预计 ${phoneBindingSummarySampleCount} 个号码`
                      : '普通绑定按手机号池可用容量自动取号，也可展开临时粘贴号码。'}
                </Text>
              </div>

              {phoneBindingPrefixModeActive ? (
                <div
                  style={{
                    marginTop: 10,
                    border: `1px solid ${token.colorBorderSecondary}`,
                    borderRadius: token.borderRadiusLG,
                    background: token.colorFillTertiary,
                    overflow: 'hidden',
                  }}
                >
                  <div
                    style={{
                      display: 'flex',
                      gap: 10,
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      flexWrap: 'wrap',
                      padding: '8px 10px',
                    }}
                  >
                    <Space wrap size={[6, 6]}>
                      <Text strong>号段范围</Text>
                      <Tag color={phoneBindingSelectedPrefixes.length > 0 ? 'processing' : 'default'}>已选择 {phoneBindingSelectedPrefixes.length}</Tag>
                      {phoneBindingPrefixGroups.map((group) => (
                        <Tag
                          key={group.key}
                          color={group.color}
                          style={{ cursor: group.items.length > 0 ? 'pointer' : 'default', userSelect: 'none' }}
                          onClick={() => togglePhoneBindingPrefixGroup(group.items.map((item) => item.prefix))}
                        >
                          {group.label} {group.items.length}
                        </Tag>
                      ))}
                    </Space>
                    <Space size={6}>
                      {phoneBindingSelectedPrefixes.length > 0 ? (
                        <Button size="small" type="link" onClick={() => setPhoneBindingSelectedPrefixes([])}>
                          清空
                        </Button>
                      ) : null}
                      <Button size="small" type="link" onClick={() => setPhoneBindingPrefixPickerOpen((open) => !open)}>
                        {phoneBindingPrefixPickerOpen ? '收起' : '展开'}
                      </Button>
                    </Space>
                  </div>

                  {phoneBindingSelectedPrefixes.length > 0 ? (
                    <div style={{ padding: '0 10px 8px' }}>
                      <Space wrap size={[4, 4]}>
                        {phoneBindingSelectedPrefixes.slice(0, 24).map((prefix) => (
                          <Tag key={prefix} color="processing" closable onClose={(event) => { event.preventDefault(); togglePhoneBindingPrefix(prefix) }}>
                            {prefix}
                          </Tag>
                        ))}
                        {phoneBindingSelectedPrefixes.length > 24 ? <Tag>+{phoneBindingSelectedPrefixes.length - 24}</Tag> : null}
                      </Space>
                    </div>
                  ) : null}

                  {phoneBindingPrefixPickerOpen ? (
                    <div
                      style={{
                        padding: '0 10px 10px',
                        maxHeight: 220,
                        overflow: 'auto',
                        borderTop: `1px solid ${token.colorBorderSecondary}`,
                      }}
                    >
                      {phoneBindingPrefixGroups.some((group) => group.items.length > 0) ? phoneBindingPrefixGroups.map((group) => (
                        <div key={group.key} style={{ marginTop: 10 }}>
                          <Space wrap size={[6, 6]} align="center">
                            <Tag color={group.color}>{group.label} {group.items.length}</Tag>
                            {group.items.map((item) => {
                              const selected = phoneBindingSelectedPrefixSet.has(item.prefix)
                              const meta = formatPhoneBindingPrefixMeta(item)
                              return (
                                <Tag
                                  key={`${group.key}:${item.prefix}`}
                                  onClick={() => togglePhoneBindingPrefix(item.prefix)}
                                  style={{
                                    cursor: 'pointer',
                                    userSelect: 'none',
                                    borderColor: selected ? token.colorPrimary : token.colorBorder,
                                    background: selected ? token.colorPrimaryBg : token.colorBgContainer,
                                    color: selected ? token.colorPrimaryText : token.colorText,
                                  }}
                                >
                                  <span>{item.prefix}</span>
                                  {meta ? <span style={{ marginLeft: 4, opacity: 0.72 }}>{meta}</span> : null}
                                </Tag>
                              )
                            })}
                          </Space>
                        </div>
                      )) : (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          当前手机号池摘要没有返回号段明细；先刷新库存，或到手机号池页确认号码是否已导入。
                        </Text>
                      )}
                    </div>
                  ) : null}
                </div>
              ) : null}

              {phoneBindingPrefixSampleEnabled ? (
                <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', marginTop: 10 }}>
                  <Form.Item name="prefix_sample_size" noStyle>
                    <Segmented
                      size="small"
                      options={[
                        { label: '每段 1 个', value: 1 },
                        { label: '每段 2 个', value: 2 },
                      ]}
                    />
                  </Form.Item>
                  <Form.Item name="prefix_sample_filter" noStyle>
                    <Segmented
                      size="small"
                      disabled={phoneBindingSelectedPrefixes.length > 0}
                      options={[
                        { label: '全部号段', value: 'all' },
                        { label: '测试可用号段', value: 'available' },
                        { label: '仅不可用号段', value: 'rejected' },
                      ]}
                    />
                  </Form.Item>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {phoneBindingSelectedPrefixes.length > 0
                      ? '已手动选择号段，抽样范围筛选本次不参与。'
                      : phoneBindingPrefixSampleFilter === 'available'
                        ? '只从当前可用号段每段抽 1/2 个号码。'
                        : phoneBindingPrefixSampleFilter === 'rejected'
                          ? '只复测 OpenAI 拒绝过的号段。'
                          : '从全部号段按每段 1/2 个号码抽样。'}
                  </Text>
                </div>
              ) : null}
            </div>
          </Form.Item>

          {phoneBindingPoolEmpty ? (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 12 }}
              message={phoneBindingPrefixSampleEnabled
                ? phoneBindingPrefixSampleFilter === 'available'
                  ? '手机号池没有可测试的可用号段，请先启用/导入不含 OpenAI 拒绝记录的号码。'
                  : phoneBindingPrefixSampleFilter === 'rejected'
                  ? '手机号池没有可复测的 OpenAI 拒绝号段，请先确认存在被 OpenAI 明确拒绝的号码。'
                  : '手机号池没有可用于号段抽样的号码，请先启用/导入带 API 且未绑满的号码。'
                : phoneBindingPrefixBindEnabled
                ? '所选号段当前没有可用绑定容量；可以继续提交让后端按实时数据判定，或改选其他号段。'
                : '手机号池没有可用绑定容量，请先导入/重置号码，或展开临时粘贴号码。'}
            />
          ) : phoneBindingPoolShortage ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message={phoneBindingPrefixBindEnabled
                ? `所选号段容量可能不足：当前范围 ${phoneBindingTargetCount} 个账号，所选号段可覆盖 ${phoneBindingLimitedCapacity}`
                : `手机号池容量不足：当前范围 ${phoneBindingTargetCount} 个账号，剩余绑定容量 ${phoneBindingSummaryRemaining}`}
            />
          ) : null}

          <div
            style={{
              border: `1px solid ${token.colorBorderSecondary}`,
              borderRadius: token.borderRadiusLG,
              padding: 12,
              marginBottom: 12,
              background: token.colorFillAlter,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
              <div>
                <Text strong>临时粘贴号码</Text>
                <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>
                  {phoneBindingPrefixBindEnabled
                    ? '限定号段绑定只使用手机号池；如需粘贴固定号码，请切回普通绑定。'
                    : phoneBindingPrefixSampleEnabled
                    ? '号段抽样测试只使用手机号池；如需粘贴固定号码，请切回普通绑定。'
                    : '每行：手机号 + 收码API；推荐 +手机号----https://...，也兼容 手机号---https://...，会自动导入手机号池并回写结果。'}
                </Text>
              </div>
              <Button
                size="small"
                type={phoneBindingShowManualInput ? 'default' : 'dashed'}
                icon={<UploadOutlined />}
                disabled={phoneBindingPrefixModeActive}
                onClick={() => setPhoneBindingManualOpen((open) => !open)}
              >
                {phoneBindingShowManualInput ? '收起' : '展开'}
              </Button>
            </div>
            {phoneBindingShowManualInput ? (
              <Form.Item
                name="phone_lines"
                style={{ marginTop: 12, marginBottom: 0 }}
                extra="填写后会优先使用这里的号码并忽略号池。"
              >
                <Input.TextArea
                  autoSize={{ minRows: 4, maxRows: 10 }}
                  placeholder={'+13434832954----https://api.sms8.net/api/record?token=...\n17632154294---https://phonenum.example.com/7632154294'}
                  style={{ fontFamily: 'monospace' }}
                />
              </Form.Item>
            ) : null}
          </div>

          <div
            style={{
              border: `1px solid ${phoneBindingSmsProbeOnlyValue ? token.colorWarningBorder : token.colorBorderSecondary}`,
              borderRadius: token.borderRadius,
              padding: '7px 10px',
              marginBottom: 12,
              background: phoneBindingSmsProbeOnlyValue ? token.colorWarningBg : token.colorFillTertiary,
            }}
          >
            <Space align="center" wrap size={[8, 4]}>
              <Form.Item name="prefix_sms_probe_only" valuePropName="checked" noStyle>
                <Switch
                  size="small"
                  checkedChildren="只测收发码"
                  unCheckedChildren="完整绑定"
                  onChange={(checked) => {
                    if (checked) {
                      phoneBindingTestForm.setFieldsValue({ reuse_phone_until_unusable: false })
                    }
                  }}
                />
              </Form.Item>
              <Text strong style={{ fontSize: 13 }}>只测发码/收码，不提交验证码</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                粘贴号码、普通手机号池、限定号段、号段抽样都生效；不保存账号绑定状态，不补抓 Auth/RT。
              </Text>
            </Space>
          </div>

          <div
            style={{
              border: `1px solid ${token.colorBorderSecondary}`,
              borderRadius: token.borderRadiusLG,
              padding: 12,
              background: token.colorBgContainer,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
              <div>
                <Text strong>参数设置</Text>
                <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>
                  修改后自动保存，下次打开继续沿用。
                </Text>
              </div>
              <Button
                size="small"
                icon={<SettingOutlined />}
                onClick={() => setPhoneBindingAdvancedOpen((open) => !open)}
              >
                {phoneBindingAdvancedOpen ? '收起' : '展开'}
              </Button>
            </div>
            <div
              style={{
                display: phoneBindingAdvancedOpen ? 'grid' : 'none',
                gridTemplateColumns: isMobile ? '1fr' : 'repeat(3, 1fr)',
                gap: 12,
                marginTop: 12,
              }}
            >
              <Form.Item name="timeout_seconds" label="等待验证码">
                <InputNumber min={10} max={900} step={5} addonAfter="秒" style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="poll_interval_seconds" label="轮询间隔">
                <InputNumber min={1} max={60} step={1} addonAfter="秒" style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="account_interval_seconds" label="账号/号码间隔">
                <InputNumber min={1} max={3600} step={5} addonAfter="秒" style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item
                name="proxy_mode"
                label="OpenAI 出口"
                extra="控制 OAuth、add-phone、OTP 提交使用哪个出口。"
              >
                <Select
                  options={[
                    { value: 'pool', label: '代理池' },
                    { value: 'specified', label: '指定代理' },
                    { value: 'dynamic', label: '动态代理' },
                    { value: 'direct', label: '直连' },
                  ]}
                />
              </Form.Item>
              {phoneBindingProxyMode === 'specified' || phoneBindingProxyMode === 'dynamic' ? (
                <Form.Item
                  name="proxy"
                  label={phoneBindingProxyMode === 'dynamic' ? '动态代理模板（可选覆盖）' : '指定代理'}
                  rules={phoneBindingProxyMode === 'specified' ? [{ required: true, message: '请填写代理地址' }] : undefined}
                  extra={phoneBindingProxyMode === 'dynamic' ? '留空使用全局动态代理模板；填写后仅本次手机号绑定覆盖全局模板。模板需包含 region-XX。' : '容器内建议使用 http://host.docker.internal:110xx。'}
                >
                  <Input placeholder={phoneBindingProxyMode === 'dynamic' ? '可留空；或填 socks5://user-region-JP-sid-xxxx-t-15:pass@host:port' : 'http://host.docker.internal:11021'} />
                </Form.Item>
              ) : null}
              {phoneBindingProxyMode !== 'direct' ? (
                <Form.Item
                  name="proxy_failover"
                  label="代理失败切换"
                  valuePropName="checked"
                  extra={phoneBindingProxyMode === 'dynamic' ? '开启后在号码未被 OpenAI 触碰前刷新 sid 重试，避免重复消耗号码。' : '仅在手机号还没被 OpenAI 触碰时切换，避免重复消耗号码。'}
                >
                  <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                </Form.Item>
              ) : null}
              {(phoneBindingProxyMode === 'pool' || phoneBindingProxyMode === 'dynamic' || (phoneBindingProxyMode === 'specified' && phoneBindingProxyFailoverValue)) ? (
                <>
                  <Form.Item
                    name="proxy_country_code"
                    label="代理国家"
                    rules={phoneBindingProxyMode === 'dynamic' ? [{ required: true, message: '请输入动态代理出口国家' }] : undefined}
                    extra={phoneBindingProxyMode === 'dynamic' ? '动态代理必填；例如 US、JP。' : '可留空；例如 US、JP。'}
                  >
                    <Input placeholder={phoneBindingProxyMode === 'dynamic' ? 'US' : '不限'} maxLength={2} />
                  </Form.Item>
                  {phoneBindingProxyMode !== 'dynamic' ? (
                    <>
                      <Form.Item name="proxy_max_candidates" label="代理候选数">
                        <InputNumber min={1} max={20} step={1} style={{ width: '100%' }} />
                      </Form.Item>
                      <Form.Item name="proxy_min_score" label="最低评分">
                        <InputNumber min={0} max={100} step={5} style={{ width: '100%' }} />
                      </Form.Item>
                    </>
                  ) : null}
                </>
              ) : null}
              <Form.Item name="max_resend_attempts" label="同号重发次数">
                <InputNumber min={0} max={10} step={1} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="resend_interval_seconds" label="重发间隔">
                <InputNumber min={0} max={3600} step={5} addonAfter="秒" style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item
                name="reuse_phone_until_unusable"
                label="尽量用满同一个手机号"
                valuePropName="checked"
                extra={phoneBindingPrefixSampleEnabled
                  ? '号段抽样模式下固定关闭，每个抽中的号码只测试一次。'
                  : phoneBindingSmsProbeOnlyValue
                  ? '只测发码/收码模式下固定关闭，避免同一号码重复探测。'
                  : '开启后，同一个号码会连续绑定多个账号，直到达到上限、限流、无法发码或接口异常。'}
              >
                <Switch checkedChildren="开启" unCheckedChildren="关闭" disabled={phoneBindingPrefixSampleEnabled || Boolean(phoneBindingSmsProbeOnlyValue)} />
              </Form.Item>
            </div>
          </div>
        </Form>
      </Modal>

      <Modal
        title="PayPal绑定"
        open={paypalBindingOpen}
        onCancel={() => setPaypalBindingOpen(false)}
        onOk={submitPaypalBinding}
        confirmLoading={paypalBindingLoading}
        okText="提交外部验证"
        cancelText="取消"
        width={820}
        maskClosable={false}
      >
        <Form
          form={paypalBindingForm}
          layout="vertical"
          onValuesChange={(_, allValues) => savePaypalBindingSettings(allValues)}
        >
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={
              paypalBindingScope === 'selected'
                ? `范围：当前选中 ${selectedRowKeys.length} 个账号，可提交 ${getPaypalBindingSelectedIds().length} 个`
                : `范围：当前筛选可提交 ${paypalFilteredEligibleCountLabel} 个账号`
            }
            description="PayPal绑定只会提交账号有效且订阅状态明确为 Free 的账号；已订阅、失效、订阅未知的账号不会提交给外部 plus.iceaix.com。外部返回 otp_needed 时，本地任务面板会等待你输入 PayPal 短信 OTP。"
          />

          <Form.Item name="scope" label="账号范围" initialValue={paypalBindingScope}>
            <Select
              value={paypalBindingScope}
              onChange={(value) => setPaypalBindingScope(value)}
              options={[
                {
                  value: 'selected',
                  label: `当前选中账号（${selectedRowKeys.length}，可提交 ${getPaypalBindingSelectedIds().length}）`,
                  disabled: selectedRowKeys.length === 0,
                },
                { value: 'filtered', label: `当前筛选账号（可提交 ${paypalFilteredEligibleCountLabel}）` },
              ]}
            />
          </Form.Item>

          <div
            style={{
              display: 'grid',
              gridTemplateColumns: isMobile ? '1fr' : '1fr 1fr',
              columnGap: 12,
            }}
          >
            <Form.Item
              name="base_url"
              label="外部接口"
              rules={[{ required: true, message: '请填写外部接口地址' }]}
            >
              <Input placeholder="https://plus.iceaix.com" />
            </Form.Item>
            <Form.Item
              name="phone"
              label="PayPal 手机号"
              extra="支持 +81 / 080 / 空格分隔格式，后端会按外部脚本规则归一成 808xxxxxxx。"
              rules={[{ required: true, message: '请填写 PayPal 手机号' }]}
            >
              <Input placeholder="+81 8083291906" />
            </Form.Item>
          </div>

          <Form.Item
            name="proxy"
            label="绑定代理"
            extra="外部服务检测出口和执行 PayPal 绑定使用。任务元数据只保存脱敏代理。"
            rules={[{ required: true, message: '请填写代理' }]}
          >
            <Input placeholder="http://user:pass@host:port" />
          </Form.Item>
          <Form.Item
            name="proxy_jp"
            label="日本代理（可选）"
            extra="对应脚本里的 proxy_jp；没有就留空。"
          >
            <Input placeholder="http://user:pass@jp-host:port" />
          </Form.Item>

          <div
            style={{
              display: 'grid',
              gridTemplateColumns: isMobile ? '1fr' : '1fr 1fr',
              columnGap: 12,
            }}
          >
            <Form.Item
              name="paypal_email"
              label="PayPal 邮箱（可选）"
              extra="留空时由外部服务决定或生成。"
            >
              <Input placeholder="可留空" />
            </Form.Item>
            <Form.Item
              name="sms_api"
              label="收码 API（可选）"
              extra="填写后由本地轮询并解析短信；留空时任务会等待你在面板里手动输入 OTP。"
            >
              <Input placeholder="可留空" />
            </Form.Item>
            <Form.Item
              name="sms_api_test_mode"
              label="接码 API 测试模式"
              valuePropName="checked"
              extra="开启后仍会本地轮询并解析验证码，但不会自动提交；你需要在任务面板手动输入。"
            >
              <Switch checkedChildren="测试" unCheckedChildren="自动提交" />
            </Form.Item>
          </div>

          <div
            style={{
              border: `1px solid ${token.colorBorderSecondary}`,
              borderRadius: token.borderRadiusLG,
              padding: 12,
              background: token.colorBgContainer,
            }}
          >
            <Text strong>任务参数</Text>
            <Text type="secondary" style={{ display: 'block', fontSize: 12, marginBottom: 12 }}>
              这些参数会自动保存到浏览器本地，下次打开继续沿用。
            </Text>
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: isMobile ? '1fr' : 'repeat(3, 1fr)',
                gap: 12,
              }}
            >
              <Form.Item name="otp_timeout" label="OTP等待">
                <InputNumber min={30} max={1800} step={10} addonAfter="秒" style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="pplink_retry" label="pplink重试">
                <InputNumber min={1} max={10} step={1} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="account_interval_seconds" label="账号间隔">
                <InputNumber min={0} max={3600} step={5} addonAfter="秒" style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="timeout" label="请求超时">
                <InputNumber min={5} max={180} step={5} addonAfter="秒" style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="event_timeout" label="事件流空闲超时">
                <InputNumber min={10} max={300} step={5} addonAfter="秒" style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item
                name="failure_continue"
                label="失败后继续"
                valuePropName="checked"
                extra="关闭后，任一账号失败就停止后续提交。"
              >
                <Switch checkedChildren="继续" unCheckedChildren="停止" />
              </Form.Item>
            </div>
          </div>
        </Form>
      </Modal>

      <Modal
        title="idea批量提交"
        open={baxiCdkSubmitOpen}
        onCancel={() => setBaxiCdkSubmitOpen(false)}
        onOk={submitBaxiCdkSubmit}
        confirmLoading={baxiCdkSubmitLoading}
        okText="开始提交"
        cancelText="取消"
        width={820}
        maskClosable={false}
      >
        <Form
          form={baxiCdkSubmitForm}
          layout="vertical"
          onValuesChange={(_, allValues) => saveBaxiGptCdkSettings(allValues)}
        >
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={
              baxiCdkSubmitScope === 'selected'
                ? `范围：当前选中 ${selectedRowKeys.length} 个账号`
                : `范围：当前筛选结果 ${total} 个账号`
            }
            description="提交成功指上游 /api/submit 返回 ok 和 order_id；不会阻塞等待 paid，默认会把订单加入后台轮询，查到状态后同步卡密池和绑定账号。"
          />
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: isMobile ? '1fr' : 'minmax(0, 1.2fr) minmax(220px, 0.8fr)',
              gap: 12,
            }}
          >
            <Form.Item name="scope" label="账号范围" initialValue={baxiCdkSubmitScope}>
              <Select
                value={baxiCdkSubmitScope}
                onChange={(value) => setBaxiCdkSubmitScope(value)}
                options={[
                  { value: 'selected', label: `当前选中账号（${selectedRowKeys.length}）`, disabled: selectedRowKeys.length === 0 },
                  { value: 'filtered', label: `当前筛选账号（${total}）` },
                ]}
              />
            </Form.Item>
            <Form.Item
              name="target_success_count"
              label="本次目标成功数量"
              extra="0 表示按账号范围尽量全部提交；填写后达到目标 paid 数就停止继续提交新账号。"
            >
              <InputNumber
                min={0}
                max={baxiCdkTargetCount > 0 ? baxiCdkTargetCount : undefined}
                precision={0}
                step={1}
                addonAfter="个"
                placeholder="0 = 不限制"
                style={{ width: '100%' }}
              />
            </Form.Item>
          </div>

          <Form.Item label="卡密来源">
            <div
              style={{
                display: 'flex',
                gap: 10,
                alignItems: 'center',
                justifyContent: 'space-between',
                flexWrap: 'wrap',
              }}
            >
              <Space wrap>
                <Form.Item name="use_pool" valuePropName="checked" noStyle>
                  <Switch
                    checkedChildren="卡密池"
                    unCheckedChildren="仅粘贴"
                    disabled={Boolean(baxiCdkManualText)}
                    onChange={(checked) => {
                      if (!checked) setBaxiCdkManualOpen(true)
                    }}
                  />
                </Form.Item>
                <Button
                  size="small"
                  icon={baxiCdkPoolSummaryLoading ? <SyncOutlined spin /> : <SyncOutlined />}
                  disabled={baxiCdkQuotaRefreshing}
                  onClick={() => {
                    void loadBaxiCdkPoolSummary(false)
                    void loadBaxiCdkPoolItems(false)
                  }}
                >
                  刷新库存
                </Button>
                <Button
                  size="small"
                  type="primary"
                  ghost
                  icon={baxiCdkQuotaRefreshing ? <SyncOutlined spin /> : <SyncOutlined />}
                  loading={baxiCdkQuotaRefreshing}
                  onClick={() => void refreshAllBaxiCdkQuota()}
                >
                  查询全部剩余
                </Button>
              </Space>
              <Space wrap size={[4, 6]}>
                <Tag color="blue">可用 {baxiCdkAvailable}</Tag>
                <Tag color={baxiCdkEffectiveCapacity > 0 ? 'cyan' : 'default'}>剩余额度 {baxiCdkEffectiveCapacity}</Tag>
                <Tag color={baxiCdkSubmitted > 0 ? 'processing' : 'default'}>已提交 {baxiCdkSubmitted}</Tag>
                <Tag color="success">已成功 {baxiCdkPaid}</Tag>
                <Tag color={baxiCdkFailed > 0 ? 'error' : 'default'}>失败 {baxiCdkFailed}</Tag>
                <Tag>停用 {baxiCdkDisabled}</Tag>
              </Space>
            </div>
            <Text type="secondary" style={{ display: 'block', marginTop: 6 }}>
              粘贴卡密会先全部导入库存，再按账号顺序提交并用 code-info 校验配额；无配额/不存在会进入任务日志提示，卡密多出来会留在库存。
            </Text>
          </Form.Item>

          {baxiCdkUsePool ? (
            <div
              style={{
                border: `1px solid ${token.colorBorderSecondary}`,
                borderRadius: token.borderRadiusLG,
                padding: 12,
                marginBottom: 12,
                background: token.colorBgContainer,
              }}
            >
              <Form.Item
                name="cdk_ids"
                label="使用已保存卡密"
                style={{ marginBottom: 8 }}
                extra="不选择时自动使用全部可用卡密；选择后只使用选中的卡密，列表会展示卡密、备注和剩余额度。"
              >
                <Select
                  mode="multiple"
                  size="large"
                  allowClear
                  showSearch
                  loading={baxiCdkPoolItemsLoading}
                  placeholder={baxiCdkPoolItemsLoading ? '正在读取卡密池...' : '默认：全部可用卡密'}
                  options={baxiCdkPoolOptions}
                  optionFilterProp="searchText"
                  maxTagCount="responsive"
                  listHeight={360}
                  popupMatchSelectWidth={false}
                  style={{ width: '100%' }}
                  notFoundContent={baxiCdkPoolItemsLoading ? '正在读取...' : '暂无可用卡密'}
                />
              </Form.Item>
              <Space size={[6, 6]} wrap>
                <Tag color={baxiCdkSelectedIds.length > 0 ? 'blue' : 'default'}>
                  {baxiCdkSelectedIds.length > 0 ? `已选 ${baxiCdkEffectivePoolCount} 个` : `默认全部 ${baxiCdkEffectivePoolCount} 个`}
                </Tag>
                <Tag color={baxiCdkTargetSuccessLimit > 0 ? 'processing' : 'default'}>
                  目标成功 {baxiCdkTargetSuccessLimit > 0 ? baxiCdkPlannedSuccessTarget : '不限'}
                </Tag>
                <Tag color={baxiCdkEffectiveCapacity >= baxiCdkPlannedSuccessTarget || baxiCdkPlannedSuccessTarget <= 0 ? 'success' : 'warning'}>
                  可提交额度 {baxiCdkEffectiveCapacity}
                </Tag>
                <Tag color={baxiCdkRemainingAfterSubmit > 0 ? 'cyan' : 'default'}>
                  本轮后预计剩余 {baxiCdkRemainingAfterSubmit}
                </Tag>
              </Space>
            </div>
          ) : null}

          {baxiCdkPoolEmpty ? (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 12 }}
              message="卡密池没有可用额度，请先粘贴保存卡密，或调整已选卡密。"
            />
          ) : baxiCdkPoolShortage ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message={`卡密池额度不足：本次计划目标 ${baxiCdkPlannedSuccessTarget} 个，可提交额度 ${baxiCdkEffectiveCapacity}，本轮会优先把可用额度全部提交。`}
            />
          ) : baxiCdkManualOverflow ? (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message={`粘贴卡密多于本次计划：${baxiCdkManualCount} 个卡密 / 目标 ${baxiCdkPlannedSuccessTarget} 个，多余卡密会留在库存。`}
            />
          ) : null}

          <div
            style={{
              border: `1px solid ${token.colorBorderSecondary}`,
              borderRadius: token.borderRadiusLG,
              padding: 12,
              marginBottom: 12,
              background: token.colorFillAlter,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
              <div>
                <Text strong>粘贴卡密入池</Text>
                <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>
                  一行一个卡密。支持“卡密----备注”，备注只用于库存显示。
                </Text>
              </div>
              <Space size={6} wrap>
                {baxiCdkShowManualInput ? (
                  <Button
                    size="small"
                    type="primary"
                    ghost
                    icon={<SaveOutlined />}
                    loading={baxiCdkSavingToPool}
                    disabled={!baxiCdkManualText}
                    onClick={() => void saveBaxiCdkManualCodesToPool()}
                  >
                    保存到卡密池
                  </Button>
                ) : null}
                <Button
                  size="small"
                  type={baxiCdkShowManualInput ? 'default' : 'dashed'}
                  icon={<UploadOutlined />}
                  onClick={() => setBaxiCdkManualOpen((open) => !open)}
                >
                  {baxiCdkShowManualInput ? '收起' : '展开'}
                </Button>
              </Space>
            </div>
            {baxiCdkShowManualInput ? (
              <Form.Item
                name="code_lines"
                style={{ marginTop: 12, marginBottom: 0 }}
                extra="可先点“保存到卡密池”入库并在上方选择；也可直接点“开始提交”，系统会先导入再按粘贴顺序配对账号。"
              >
                <Input.TextArea
                  autoSize={{ minRows: 4, maxRows: 10 }}
                  placeholder="CDK-AAAA-BBBB\nCDK-CCCC-DDDD----备注"
                  style={{ fontFamily: 'monospace' }}
                />
              </Form.Item>
            ) : null}
          </div>

          <div
            style={{
              border: `1px solid ${token.colorBorderSecondary}`,
              borderRadius: token.borderRadiusLG,
              padding: 12,
              background: token.colorBgContainer,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
              <div>
                <Text strong>提交参数</Text>
                <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>
                  修改后自动保存，下次打开继续沿用。
                </Text>
              </div>
              <Button
                size="small"
                icon={<SettingOutlined />}
                onClick={() => setBaxiCdkAdvancedOpen((open) => !open)}
              >
                {baxiCdkAdvancedOpen ? '收起' : '展开'}
              </Button>
            </div>
            <div
              style={{
                display: baxiCdkAdvancedOpen ? 'grid' : 'none',
                gridTemplateColumns: isMobile ? '1fr' : 'repeat(3, 1fr)',
                gap: 12,
                marginTop: 12,
              }}
            >
              <Form.Item
                name="submit_interval_seconds"
                label="提交间隔"
                extra="只在上一个账号 /api/submit 成功后等待；预查失败、无配额或提交失败不会等待。"
              >
                <InputNumber min={0} max={3600} step={1} addonAfter="秒" style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item
                name="auto_poll_status"
                label="自动轮询状态"
                valuePropName="checked"
                extra="不阻塞下一个账号提交；后台按上游任务 ID 查询到 paid/failed 后同步卡密池和账号。"
              >
                <Switch checkedChildren="开启" unCheckedChildren="关闭" />
              </Form.Item>
              <Form.Item name="status_poll_interval_seconds" label="轮询间隔">
                <InputNumber min={1} max={3600} step={1} addonAfter="秒" style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="status_poll_timeout_seconds" label="未返回提醒" extra="到点只写提醒日志，任务会继续等待上游终态，不再提前结束。">
                <InputNumber min={1800} max={86400} step={60} addonAfter="秒" style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="precheck" label="提交前预查" valuePropName="checked">
                <Switch checkedChildren="开启" unCheckedChildren="关闭" />
              </Form.Item>
              <Form.Item
                name="failure_continue"
                label="失败后继续"
                valuePropName="checked"
                extra="开启后，单个卡密或账号失败不会阻断后续配对。"
              >
                <Switch checkedChildren="继续" unCheckedChildren="停止" />
              </Form.Item>
            </div>
          </div>
        </Form>
      </Modal>

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
        onInvalidRecheckTask={handleInvalidRecheck}
        onK12RecaptureTask={handleK12WorkspaceRecapture}
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
        getAccessToken={getAccessToken}
        onCopyAccessToken={copyAccessToken}
        onCopySecret={copyAccountSecret}
        onFetchSecret={fetchAccountSecrets}
        isAccessTokenCopied={(record) => {
          const accountId = Number(record?.id || 0)
          return accountId > 0 && accessTokenCopiedAccountIds.has(accountId)
        }}
        canImportAccountToTeam={canImportAccountToTeam}
        authStateMeta={authStateMeta}
        planMeta={planMeta}
        codexStateMeta={codexStateMeta}
      />
      
      <Modal
        title={oaipayUploadScope === 'selected' ? `确认上传所选 ${selectedRowKeys.length} 个账号到 OAIPay` : `确认上传待补传账号到 OAIPay`}
        open={oaipayUploadModalOpen}
        onOk={() => {
          if (oaipayCategoryMode === 'manual' && !oaipaySelectedCategory) {
            message.warning('固定分类模式下请选择 OAIPay 分类')
            return
          }
          setOaipayUploadModalOpen(false)
          void handleBackfill('oaipay', oaipayUploadScope, {
            categoryMode: oaipayCategoryMode,
            categoryId: oaipayCategoryMode === 'manual' ? oaipaySelectedCategory : undefined,
            fallbackCategoryId: oaipayCategoryMode === 'auto' ? oaipayFallbackCategory : undefined,
          })
        }}
        onCancel={() => setOaipayUploadModalOpen(false)}
        okText="开始上传"
      >
        <Space direction="vertical" size={14} style={{ width: '100%' }}>
          <Alert
            type="info"
            showIcon
            message="默认使用自动分类"
            description="系统会按账号当前状态选择 OAIPay 分类，并在任务日志里逐账号记录最终分类；不是随机上传。"
          />
          <Radio.Group
            value={oaipayCategoryMode}
            onChange={(event) => setOaipayCategoryMode(event.target.value)}
            style={{ width: '100%' }}
          >
            <Space direction="vertical" size={10} style={{ width: '100%' }}>
              <Radio value="auto">
                <Space direction="vertical" size={2}>
                  <Typography.Text strong>自动分类（推荐）</Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    Plus + RT → PLUS--已接美国长效；Plus + 无 RT → PLUS--未接码；Free + RT → FREE--已接码带RT。
                  </Typography.Text>
                </Space>
              </Radio>
              <Radio value="manual">
                <Space direction="vertical" size={2}>
                  <Typography.Text strong>固定分类</Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    所有账号都上传到所选分类，不再按账号状态自动分流。
                  </Typography.Text>
                </Space>
              </Radio>
            </Space>
          </Radio.Group>
          {oaipayCategoryMode === 'manual' ? (
            <Select
              style={{ width: '100%' }}
              showSearch
              allowClear
              optionFilterProp="label"
              placeholder={oaipayCategoryLoading ? '正在获取 OAIPay 分类...' : '选择固定上传分类'}
              loading={oaipayCategoryLoading}
              value={oaipaySelectedCategory}
              onChange={(val) => setOaipaySelectedCategory(val)}
              options={oaipayCategories.map(c => ({ label: `${c.id} - ${c.name}`, value: c.id }))}
            />
          ) : (
            <Select
              style={{ width: '100%' }}
              showSearch
              allowClear
              optionFilterProp="label"
              placeholder={oaipayCategoryLoading ? '正在获取 OAIPay 分类...' : '可选：自动分类未命中时使用的兜底分类'}
              loading={oaipayCategoryLoading}
              value={oaipayFallbackCategory}
              onChange={(val) => setOaipayFallbackCategory(val)}
              options={oaipayCategories.map(c => ({ label: `${c.id} - ${c.name}`, value: c.id }))}
            />
          )}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            上传完成后，任务日志和账号列表的“OAIPay上传”列会显示每个账号最终进入的分类。
          </Typography.Text>
        </Space>
      </Modal>

    </div>
  )
}
