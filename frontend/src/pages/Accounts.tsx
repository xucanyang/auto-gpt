import { lazy, Suspense, useEffect, useState, useCallback, useMemo, useRef } from 'react'
import type { CSSProperties } from 'react'
import { FilterPresetBar } from '../features/accounts/components/FilterPresetBar'
import {
  Button,
  App,
  Checkbox,
  Dropdown,
  Input,
  InputNumber,
  Tag,
  Popover,
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
  CheckOutlined,
  ArrowDownOutlined,
  ArrowUpOutlined,
  CopyOutlined,
  DeleteOutlined,
  DownOutlined,
  EditOutlined,
  LinkOutlined,
  LoginOutlined,
  MoreOutlined,
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
import type {
  AccountExportMode,
  AccountExportScope,
  AccountsToolbarActionId as AccountToolbarActionId,
} from '@/features/accounts/components/AccountsToolbar'
import { PixLinkScanModal } from '@/features/accounts/components/PixLinkScanModal'
import type { PaymentLinkCleanupType, PixLinkCleanupMode, PixLinkScanReport } from '@/features/accounts/components/PixLinkScanModal'
import { ImportAccountsModal } from '@/features/accounts/components/ImportAccountsModal'
import { SubscriptionStatusCounts } from '@/features/accounts/components/SubscriptionStatusCounts'
import {
  normalizeSubscriptionStatusCounts,
  type SubscriptionStatusCountsValue,
} from '@/features/accounts/subscriptionStatusCounts'
import { useAccountDetailQuery } from '@/features/accounts/hooks/useAccountDetailQuery'
import { useActiveTasksQuery } from '@/features/accounts/hooks/useActiveTasksQuery'
import { RegisterTaskModal } from '@/features/auth/components/RegisterTaskModal'
import { useAccountsQuery } from '@/features/accounts/hooks/useAccountsQuery'
import { usePersistentChatGPTRegistrationMode } from '@/hooks/usePersistentChatGPTRegistrationMode'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import { buildChatGPTRegistrationRequestAdapter } from '@/lib/chatgptRegistrationRequestAdapter'
import { CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN } from '@/lib/chatgptRegistrationMode'
import {
  normalizeRegisterConcurrency,
  normalizeRegisterDelaySettings,
  normalizeRegisterUniqueExitPolicy,
  type ChatGPTRegisterControlConfig,
} from '@/lib/chatgptRegisterTaskControls'
import { normalizeDomainList, parseStoredDomainList } from '@/lib/domainList'
import {
  DEFAULT_GOPAY_PHONE_COUNTRY_CODE,
  normalizeGopayPhonePart,
  normalizeGopayRecognizedCountryCodes,
  splitGopayPhoneInput,
} from '@/lib/gopayPhone'
import { apiFetch, apiRequest } from '@/lib/utils'
import { buildTaskProxyPayload, saveTaskProxySettingsToConfig, taskProxySettingsFromConfig, validateTaskProxySettings } from '@/lib/taskProxySettings'
import { normalizeExecutorForPlatform } from '@/lib/platformExecutorOptions'
import { normalizeRegistrationDiagnosticsMode } from '@/lib/registrationDiagnostics'
import { isActiveTaskStatus, normalizeTaskStatus } from '@/lib/taskStatus'

const { Text } = Typography

const AccountActionSurface = lazy(() =>
  import('@/features/accounts/components/AccountActionSurface').then((module) => ({
    default: module.AccountActionSurface,
  })),
)

const GOPAY_ACTIVE_PHASES = new Set(['created', 'starting', 'waiting_otp', 'waiting_link_pin', 'waiting_payment_pin', 'verifying'])
const TASK_MODAL_STORAGE_KEY = 'auto-chatgpt.accounts.task-modal.current-task'
const ACCOUNT_COLUMN_VISIBILITY_STORAGE_KEY = 'auto-chatgpt.accounts.visible-columns.v4'
const LEGACY_ACCOUNT_COLUMN_VISIBILITY_STORAGE_KEYS = [
  'auto-chatgpt.accounts.visible-columns.v3',
  'auto-chatgpt.accounts.visible-columns.v2',
] as const
const PHONE_BINDING_SETTINGS_STORAGE_KEY = 'auto-chatgpt.accounts.phone-binding-settings.v1'
const INVALID_RECHECK_CONCURRENCY_STORAGE_KEY = 'auto-chatgpt.accounts.invalid-recheck-concurrency.v1'
const WEB_SESSION_LOGIN_CONCURRENCY_STORAGE_KEY = 'auto-chatgpt.accounts.web-session-login-concurrency.v1'
const PAYMENT_ELIGIBILITY_CONCURRENCY_STORAGE_KEY = 'auto-chatgpt.accounts.payment-eligibility-concurrency.v1'
const PAYMENT_ELIGIBILITY_DEFAULT_CONCURRENCY = 2
const PAYMENT_ELIGIBILITY_MAX_CONCURRENCY = 10
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
  stopped: { title: '已停止', description: '未启动后续 GoPay 会话', step: 4, status: 'wait' },
}

function gopayPhaseMeta(phase?: string) {
  return GOPAY_PHASE_META[String(phase || '').trim()] || { title: '未知', description: String(phase || '未知阶段'), step: 0, status: 'process' as const }
}

const REGISTER_FORM_SETTINGS_STORAGE_PREFIX = 'auto-chatgpt.register-form-settings.v2.'
const REGISTER_MAIL_PROVIDER_OVERRIDES = new Set([
  '__global__',
  'hme_ready_api',
  'tempmail_local',
  'tempmail_api',
  'email_api',
  'manual_email_otp',
])
const DEFAULT_CHECKOUT_COUNTRY = 'ID'
const DEFAULT_CHECKOUT_CURRENCY = 'IDR'
const DEFAULT_GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS = 120
const ACCOUNTS_DEFAULT_PAGE_SIZE_STORAGE_KEY = 'auto-chatgpt.accounts.page-size.v1'
const ACCOUNTS_CUSTOM_PAGE_SIZE_OPTIONS_STORAGE_KEY = 'auto-chatgpt.accounts.custom-page-size-options.v1'
const ACCOUNT_TOOLBAR_ACTION_VISIBILITY_STORAGE_KEY = 'auto-chatgpt.accounts.toolbar-actions.v1'
const DEFAULT_ACCOUNTS_PAGE_SIZE = 20
const ACCOUNT_PAGE_SIZE_OPTIONS = [10, 20, 50]
const MIN_ACCOUNTS_PAGE_SIZE = 1
const MAX_ACCOUNTS_PAGE_SIZE = 200
const MAX_FIXED_FILTER_PRESET_ACCOUNT_IDS = 5000
const EMPTY_LIST: any[] = []
const SUBSCRIPTION_EXPIRY_SORT_FIELD = 'subscription_active_until'
const ACCOUNT_CREATED_AT_SORT_FIELD = 'created_at'
const DEFAULT_REGISTRATION_SORT_ORDER = 'desc' as const
const DEFAULT_TEAM_WORKSPACE_NAME = 'MyTeam'
const DEFAULT_TEAM_CHECKOUT_UI_MODE = 'hosted' as const
const DEFAULT_TEAM_BILLING_COUNTRY = 'US' as const
const DEFAULT_SHORT_PAYMENT_COUNTRY = 'US' as const
const DEFAULT_SHORT_PAYMENT_CURRENCY = 'USD' as const
const SHORT_PAYMENT_SOURCE = 'chatgpt_hosted' as const
const SHORT_PAYMENT_LINK_FORMAT = 'short_chatgpt' as const
const TEAM_PROXY_COUNTRY_CODES = [
  'US', 'GB', 'CA', 'AU', 'JP', 'SG', 'HK', 'TW', 'KR', 'ID', 'MY', 'TH',
  'TR', 'VN', 'PH', 'IN', 'DE', 'FR', 'IT', 'ES', 'NL', 'IE', 'PT', 'BE',
  'FI', 'AT', 'CH', 'SE', 'NO', 'DK', 'PL', 'CZ', 'MX', 'BR', 'NZ',
] as const

function teamProxyCountryLabel(code: string) {
  const normalized = String(code || '').trim().toUpperCase()
  try {
    const displayNames = new Intl.DisplayNames(['zh-CN'], { type: 'region' })
    return `${displayNames.of(normalized) || normalized} (${normalized})`
  } catch {
    return normalized
  }
}

function teamBillingCountryLabel(code: string, currency: string) {
  const normalized = String(code || '').trim().toUpperCase()
  try {
    const displayNames = new Intl.DisplayNames(['zh-CN'], { type: 'region' })
    return `${displayNames.of(normalized) || normalized} (${normalized}) · ${currency}`
  } catch {
    return `${normalized} · ${currency}`
  }
}

function normalizeTeamBillingCountryOptions(value: unknown): Array<{ value: string; label: string; currency: string }> {
  if (!Array.isArray(value)) return []
  const currencies = new Map<string, string>()
  value.forEach((item) => {
    if (!item || typeof item !== 'object') return
    const option = item as { country?: unknown; currency?: unknown }
    const country = String(option.country || '').trim().toUpperCase()
    const currency = String(option.currency || '').trim().toUpperCase()
    if (!/^[A-Z]{2}$/.test(country) || !/^[A-Z]{3}$/.test(currency) || currencies.has(country)) return
    currencies.set(country, currency)
  })
  return Array.from(currencies.entries())
    .sort(([left], [right]) => {
      if (left === DEFAULT_TEAM_BILLING_COUNTRY) return -1
      if (right === DEFAULT_TEAM_BILLING_COUNTRY) return 1
      return left.localeCompare(right)
    })
    .map(([country, currency]) => ({
      value: country,
      label: teamBillingCountryLabel(country, currency),
      currency,
    }))
}

const DEFAULT_PINNED_ACCOUNT_TOOLBAR_ACTIONS: AccountToolbarActionId[] = ['statusSync', 'paymentLink']

const ACCOUNT_TOOLBAR_ACTION_OPTIONS: Array<{ value: AccountToolbarActionId; text: string }> = [
  { value: 'statusSync', text: '状态同步' },
  { value: 'paymentLink', text: '支付链接生成' },
  { value: 'resumeAuth', text: '批量补抓Auth' },
  { value: 'backfill', text: '远端补传' },
  { value: 'webSessionLogin', text: '批量执行登录态' },
  { value: 'invalidRecheck', text: '批量失效测活' },
  { value: 'eligibilityChecks', text: '支付资格检测' },
  { value: 'phoneBindingTest', text: '手机号绑定' },
  { value: 'paypalBinding', text: 'PayPal绑定' },
  { value: 'baxiCdkSubmit', text: 'iDEAL / PIX 批量提交' },
]

type PhonePoolMode = 'normal' | 'prefix_limited' | 'prefix_sample' | 'unavailable_numbers'
type PhoneNumberFilter = 'available' | 'unavailable' | 'all'
type PhonePoolPrefixStatus = 'available' | 'partial' | 'unavailable' | 'temporary' | 'exhausted'
type PhonePoolPrefixItem = {
  prefix: string
  status: PhonePoolPrefixStatus | string
  total?: number
  available_count?: number
  remaining_capacity?: number
  rejected_count?: number
  cannot_send_count?: number
  bind_limit_count?: number
  [key: string]: unknown
}

type PhonePoolPrefixGroup = {
  key: PhonePoolPrefixStatus
  label: string
  color: string
  items: PhonePoolPrefixItem[]
}

function normalizeAccountsPageSize(value: unknown): number | null {
  const normalized = Number(value)
  if (!Number.isInteger(normalized)) return null
  if (normalized < MIN_ACCOUNTS_PAGE_SIZE || normalized > MAX_ACCOUNTS_PAGE_SIZE) return null
  return normalized
}

function normalizeCustomAccountsPageSizeOptions(value: unknown): number[] {
  if (!Array.isArray(value)) return []
  return Array.from(new Set(
    value
      .map(normalizeAccountsPageSize)
      .filter((item): item is number => item !== null && !ACCOUNT_PAGE_SIZE_OPTIONS.includes(item)),
  )).sort((left, right) => left - right)
}

function loadDefaultAccountsPageSize() {
  if (typeof window === 'undefined') return DEFAULT_ACCOUNTS_PAGE_SIZE
  try {
    return normalizeAccountsPageSize(window.localStorage.getItem(ACCOUNTS_DEFAULT_PAGE_SIZE_STORAGE_KEY))
      ?? DEFAULT_ACCOUNTS_PAGE_SIZE
  } catch {
    return DEFAULT_ACCOUNTS_PAGE_SIZE
  }
}

function loadCustomAccountsPageSizeOptions() {
  if (typeof window === 'undefined') return []
  let storedOptions: unknown = []
  try {
    storedOptions = JSON.parse(
      window.localStorage.getItem(ACCOUNTS_CUSTOM_PAGE_SIZE_OPTIONS_STORAGE_KEY) || '[]',
    )
  } catch {
    storedOptions = []
  }
  try {
    const currentPageSize = normalizeAccountsPageSize(
      window.localStorage.getItem(ACCOUNTS_DEFAULT_PAGE_SIZE_STORAGE_KEY),
    )
    return normalizeCustomAccountsPageSizeOptions([
      ...normalizeCustomAccountsPageSizeOptions(storedOptions),
      currentPageSize,
    ])
  } catch {
    return normalizeCustomAccountsPageSizeOptions(storedOptions)
  }
}

function saveDefaultAccountsPageSize(pageSize: number) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(ACCOUNTS_DEFAULT_PAGE_SIZE_STORAGE_KEY, String(pageSize))
  } catch {
    // Browser storage can be unavailable without blocking pagination.
  }
}

function saveCustomAccountsPageSizeOptions(pageSizes: number[]) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(
      ACCOUNTS_CUSTOM_PAGE_SIZE_OPTIONS_STORAGE_KEY,
      JSON.stringify(normalizeCustomAccountsPageSizeOptions(pageSizes)),
    )
  } catch {
    // Keep the in-memory options usable when browser storage is unavailable.
  }
}

const DEFAULT_PHONE_BINDING_SETTINGS = {
  use_pool: true,
  phone_pool_mode: 'normal' as PhonePoolMode,
  phone_number_filter: 'available' as PhoneNumberFilter,
  prefix_number_filter: 'available' as PhoneNumberFilter,
  unavailable_number_test_enabled: false,
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
  concurrency: 1,
  reuse_phone_until_unusable: false,
  proxy_mode: 'dynamic',
  proxy: '',
  proxy_country_code: '',
  proxy_failover: true,
  proxy_max_candidates: 10,
  proxy_min_score: 50,
}

const DEFAULT_BAXIGPT_STATUS_POLL_INTERVAL_SECONDS = 5
const BAXIGPT_STATUS_POLL_INTERVAL_MIN_SECONDS = 1
const BAXIGPT_STATUS_POLL_INTERVAL_MAX_SECONDS = 3600

const DEFAULT_BAXIGPT_CDK_SETTINGS = {
  payment_channel: 'ideal',
  pix_submit_mode: 'auto_extract' as 'auto_extract' | 'user_link',
  use_pool: true,
  precheck: true,
  failure_continue: true,
  submit_interval_seconds: 5,
  auto_poll_status: true,
  status_poll_interval_seconds: DEFAULT_BAXIGPT_STATUS_POLL_INTERVAL_SECONDS,
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
  healthy_prefix_count?: number
  partial_prefix_count?: number
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
  healthy_prefixes?: Array<Record<string, unknown>>
  partial_prefixes?: Array<Record<string, unknown>>
  rejected_prefixes?: Array<Record<string, unknown>>
  exhausted_prefix_count?: number
  exhausted_prefixes?: Array<Record<string, unknown>>
  temporary_prefix_count?: number
  temporary_prefixes?: Array<Record<string, unknown>>
  prefix_health?: {
    available?: Array<Record<string, unknown>>
    partial?: Array<Record<string, unknown>>
    unavailable?: Array<Record<string, unknown>>
    exhausted?: Array<Record<string, unknown>>
    temporary?: Array<Record<string, unknown>>
  }
}

type BaxiGptCdkPoolSummary = {
  total?: number
  available?: number
  submit_candidates?: number
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
type RegistrationSortOrder = 'asc' | 'desc'

type AccountFilterRequestBody = {
  email: string
  status: string
  manually_used: string
  auth_type: string
  phone_binding_state: string
  payment_link_platform: string
  payment_link_generated: string
  subscription_type: string
  account_validity: string
  sub2api_state: string
  oaipay_state: string
  zero_amount_eligibility_state: string
  gcash_payment_method_state: string
  submit_state: string
  has_submitted: string
}

type AccountTaskScope = 'selected' | 'filtered'
type FilteredScopeMarker = 'all_filtered' | 'pending_only'
type PaymentEligibilityKind = 'zero_amount_eligibility' | 'gcash_payment_method'

const ACCOUNT_FILTER_REQUEST_KEYS: Array<keyof AccountFilterRequestBody> = [
  'email',
  'status',
  'manually_used',
  'auth_type',
  'phone_binding_state',
  'payment_link_platform',
  'payment_link_generated',
  'subscription_type',
  'account_validity',
  'sub2api_state',
  'oaipay_state',
  'zero_amount_eligibility_state',
  'gcash_payment_method_state',
  'submit_state',
  'has_submitted',
]

function normalizeAccountIds(values: Iterable<unknown>): number[] {
  const result: number[] = []
  const seen = new Set<number>()
  for (const value of values) {
    const accountId = Number(value)
    if (!Number.isInteger(accountId) || accountId <= 0 || seen.has(accountId)) continue
    seen.add(accountId)
    result.push(accountId)
  }
  return result
}

function getFilterScopeChangedMessage(error: unknown): string {
  const errorRecord = error !== null && typeof error === 'object'
    ? error as Record<string, unknown>
    : null
  const detail = errorRecord?.detail
  const detailRecord = detail !== null && typeof detail === 'object' && !Array.isArray(detail)
    ? detail as Record<string, unknown>
    : null
  const code = String(errorRecord?.code || detailRecord?.code || '').trim()
  if (Number(errorRecord?.status || 0) !== 409 || code !== 'FILTER_SCOPE_CHANGED') return ''

  const backendMessage = String(detailRecord?.message || errorRecord?.message || '').trim()
  if (backendMessage) {
    return `${backendMessage.replace(/[\s。；;,，.]+$/, '')}。账号列表已刷新，请确认后重新提交。`
  }

  const expectedTotal = Number(detailRecord?.expected_total)
  const matchedTotal = Number(detailRecord?.matched_total)
  const countText = Number.isFinite(expectedTotal) && Number.isFinite(matchedTotal)
    ? `（页面 ${expectedTotal} 个，当前匹配 ${matchedTotal} 个）`
    : ''
  return `当前筛选范围已变化${countText}。账号列表已刷新，请确认后重新提交。`
}

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
  | 'payment_eligibility'
  | 'payment_link'
  | 'codex_usage'
  | 'sub2api_state'
  | 'sub2api_upload_record'
  | 'oaipay_state'
  | 'oaipay_upload_record'
  | 'created_at'

type PaymentLinkProfile = {
  link_type?: string
  country?: string
  billing_country?: string
  currency?: string
  checkout_ui_mode?: string
  payment_locale?: string
  client_fingerprint?: string
  proxy_chain_strategy?: string
  effective_concurrency?: number
  profile_hash?: string
  proxy_configured?: boolean
  plan?: 'plus' | 'team' | string
  generation_kind?: string
  plan_name?: string
  variant_key?: string
  promo_code_digest?: string
  billing_country_options?: Array<{
    country?: string
    currency?: string
  }>
  team?: {
    workspace_name?: string
    price_interval?: string
    seat_quantity?: number
    cancel_url?: string
    promo_code_configured?: boolean
  }
  regions?: Record<string, string>
  pix?: {
    request_preset?: string
    seed_pool_configured?: boolean
  }
}

type PaymentLinkGenerationMode = 'plus' | 'short' | 'team'

type PixLinkCleanupTaskResponse = {
  task_id?: string
  source?: string
  instance_id?: string
  already_running?: boolean
  cleanup_mode?: PixLinkCleanupMode
  requested_cleanup_mode?: PixLinkCleanupMode
  payment_type?: PaymentLinkCleanupType
}

const PIX_LINK_CLEANUP_META: Record<PixLinkCleanupMode, { label: string; title: string }> = {
  valid: { label: '有效', title: '有效链接' },
  expired: { label: '过期', title: '过期链接' },
  paid: { label: '已支付', title: '已支付链接' },
  cancelled: { label: '支付已取消', title: '支付已取消链接' },
  unknown: { label: '状态未知', title: '状态未知链接' },
}

const PAYMENT_LINK_SCAN_LABELS: Record<PaymentLinkCleanupType, string> = {
  hosted: 'Hosted Checkout',
  paypal: 'PayPal',
  ideal: 'iDEAL',
  upi: 'UPI',
  pix: 'PIX',
  twint: 'TWINT',
  kakao_pay: 'Kakao Pay',
  team: 'ChatGPT Team',
  other: '其他支付链接',
}

const PAYMENT_LINK_CLEANED_STATUS_META: Record<string, { color: string; label: string }> = {
  expired_cleaned: { color: 'warning', label: '已过期清理' },
  paid_cleaned: { color: 'success', label: '已支付清理' },
  cancelled_cleaned: { color: 'warning', label: '支付已取消清理' },
  upi_expired_cleaned: { color: 'warning', label: 'UPI 已过期清理' },
  upi_paid_cleaned: { color: 'success', label: 'UPI 已支付清理' },
  upi_cancelled_cleaned: { color: 'warning', label: 'UPI 支付已取消清理' },
  ideal_expired_cleaned: { color: 'warning', label: 'iDEAL 已过期清理' },
  ideal_paid_cleaned: { color: 'success', label: 'iDEAL 已支付清理' },
  ideal_cancelled_cleaned: { color: 'warning', label: 'iDEAL 支付已取消清理' },
  payment_link_deleted: { color: 'default', label: '支付链接已删除' },
}

const ACCOUNT_COLUMN_OPTIONS: Array<{ value: AccountColumnKey; text: string; chatgptOnly?: boolean }> = [
  { value: 'manually_used', text: '使用状态' },
  { value: 'phone_binding', text: '手机号/API', chatgptOnly: true },
  { value: 'password', text: '密码' },
  { value: 'auth_type', text: '认证材料', chatgptOnly: true },
  { value: 'status', text: '业务状态' },
  { value: 'subscription_type', text: '当前订阅', chatgptOnly: true },
  { value: 'subscription_active_until', text: '订阅到期', chatgptOnly: true },
  { value: 'account_validity', text: '认证状态', chatgptOnly: true },
  { value: 'idea_submit_status', text: '提交状态', chatgptOnly: true },
  { value: 'payment_eligibility', text: '支付资格', chatgptOnly: true },
  { value: 'payment_link', text: '支付链接', chatgptOnly: true },
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
  'payment_eligibility',
  'payment_link',
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
  phoneBindingState: string[]
  paymentLinkPlatform: string[]
  paymentLinkGenerated: string[]
  subscriptionType: string[]
  accountValidity: string[]
  codexState: string[]
  sub2apiState: string[]
  oaipayState: string[]
  zeroAmountEligibilityState: string[]
  gcashPaymentMethodState: string[]
  submitState: string[]
  hasSubmitted: string[]
}

const EMPTY_ACCOUNT_FILTERS: AccountColumnFilters = {
  email: '',
  status: [],
  manuallyUsed: [],
  authType: [],
  phoneBindingState: [],
  paymentLinkPlatform: [],
  paymentLinkGenerated: [],
  subscriptionType: [],
  accountValidity: [],
  codexState: [],
  sub2apiState: [],
  oaipayState: [],
  zeroAmountEligibilityState: [],
  gcashPaymentMethodState: [],
  submitState: [],
  hasSubmitted: [],
}

export type AccountFilterPresetFilters = {
  search?: string
  status?: string[]
  columnFilters?: Partial<Record<keyof AccountColumnFilters, string[] | string>> & {
    // Old saved presets used this camel-case key. Keep it as an input-only
    // compatibility alias and migrate it to submitState when loading.
    ideaSubmitState?: string[] | string
  }
  sortOrder?: SubscriptionExpirySortOrder
  registrationSortOrder?: RegistrationSortOrder
  // Compatibility-only field from presets created before v2.14.3.
  // Pagination is browser-local and is never applied from a preset.
  pageSize?: number
}

export type AccountFilterPreset = {
  id: string
  name: string
  description?: string
  summary?: string
  mode: 'dynamic' | 'fixed'
  parent_preset_id?: string
  filters: AccountFilterPresetFilters
  account_ids: number[]
  account_count: number
  subscription_counts: SubscriptionStatusCountsValue
  pinned?: boolean
  built_in?: boolean
  created_at?: string
  updated_at?: string
  revision?: number
}

function normalizeAccountFilterPresetItem(value: unknown): AccountFilterPreset | null {
  const item = value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
  const id = String(item?.id || '').trim()
  const name = String(item?.name || '').trim()
  if (!id || !name) return null
  const mode = String(item?.mode || '').trim().toLowerCase() === 'fixed' ? 'fixed' : 'dynamic'
  const accountIds = mode === 'fixed' && Array.isArray(item.account_ids)
    ? normalizeAccountIds(item.account_ids)
    : []
  return {
    id,
    name,
    description: String(item?.description || ''),
    summary: String(item?.summary || ''),
    mode,
    parent_preset_id: String(item?.parent_preset_id || ''),
    filters: normalizeAccountFilterPresetFilters(item.filters as AccountFilterPresetFilters | undefined),
    account_ids: accountIds,
    account_count: Math.max(Number(item?.account_count || 0), accountIds.length),
    subscription_counts: normalizeSubscriptionStatusCounts(item?.subscription_counts),
    pinned: Boolean(item?.pinned),
    built_in: Boolean(item?.built_in),
    created_at: String(item?.created_at || ''),
    updated_at: String(item?.updated_at || ''),
    revision: Number(item?.revision || 1),
  }
}

function normalizeAccountFilterPresetItems(value: unknown): AccountFilterPreset[] {
  if (!Array.isArray(value)) return []
  return value.reduce<AccountFilterPreset[]>((items, rawItem) => {
    const item = normalizeAccountFilterPresetItem(rawItem)
    if (item) items.push(item)
    return items
  }, [])
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

const PHONE_BINDING_STATE_FILTER_OPTIONS = [
  { value: 'confirmed', text: '已绑定' },
  { value: 'unbound', text: '未绑定' },
]

const PAYMENT_LINK_PLATFORM_FILTER_OPTIONS = [
  { value: 'has_link', text: '当前有链接' },
  { value: 'none', text: '当前无链接' },
  { value: 'hosted', text: 'Hosted Checkout' },
  { value: 'paypal', text: 'PayPal' },
  { value: 'ideal', text: 'iDEAL' },
  { value: 'upi', text: 'UPI' },
  { value: 'pix', text: 'PIX' },
  { value: 'twint', text: 'TWINT' },
  { value: 'kakao_pay', text: 'Kakao Pay' },
  { value: 'team', text: 'ChatGPT Team' },
  { value: 'other', text: '其他支付链接' },
]

const PAYMENT_LINK_PLATFORM_FILTER_VALUE_ALIASES: Record<string, string[]> = {
  payment: ['hosted'],
  pay: ['hosted'],
  long: ['hosted'],
  checkout: ['hosted'],
  chatgpt: ['hosted', 'team'],
  chatgpt_hosted: ['hosted'],
  stripe_hosted: ['hosted'],
  pp: ['paypal'],
  paypal_url: ['paypal'],
  'ideal-pay': ['ideal'],
  ideal_pay: ['ideal'],
  qr: ['pix'],
  pix_qr: ['pix'],
  upi_qr: ['upi'],
  upi_qr_code: ['upi'],
  kakao: ['kakao_pay'],
  kakaopay: ['kakao_pay'],
  'kakao-pay': ['kakao_pay'],
  team_checkout: ['team'],
  chatgptteamplan: ['team'],
  no_link: ['none'],
  no_payment_link: ['none'],
  without_link: ['none'],
  current_no_link: ['none'],
  missing: ['none'],
}

const PAYMENT_LINK_GENERATED_FILTER_OPTIONS = [
  { value: 'true', text: '已成功提取' },
  { value: 'false', text: '从未成功提取' },
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
  { value: 'unconfirmable', text: '不可确认' },
  { value: 'pending_refresh', text: '待刷新' },
]

const ACCOUNT_VALIDITY_FILTER_OPTIONS = [
  { value: 'valid', text: '认证通过' },
  { value: 'invalid', text: '认证失效' },
  { value: 'refresh_failed', text: '刷新失败' },
  { value: 'not_checked', text: '未验证' },
]

const INTEGRATION_UPLOAD_FILTER_OPTIONS = [
  { value: 'uploaded', text: '已上传' },
  { value: 'not_uploaded', text: '未上传' },
]

const SUB2API_FILTER_OPTIONS = INTEGRATION_UPLOAD_FILTER_OPTIONS
const OAIPAY_FILTER_OPTIONS = INTEGRATION_UPLOAD_FILTER_OPTIONS

const ZERO_AMOUNT_ELIGIBILITY_FILTER_OPTIONS = [
  { value: 'eligible', text: '0 元可用' },
  { value: 'ineligible', text: '非 0 元' },
  { value: 'unknown', text: '未检测' },
]

const GCASH_PAYMENT_METHOD_FILTER_OPTIONS = [
  { value: 'available', text: 'GCash 可用' },
  { value: 'unavailable', text: 'GCash 不可用' },
  { value: 'unknown', text: '未检测' },
]

const INTEGRATION_UPLOAD_FILTER_VALUE_ALIASES: Record<string, string> = {
  true: 'uploaded',
  uploaded: 'uploaded',
  exists: 'uploaded',
  false: 'not_uploaded',
  not_uploaded: 'not_uploaded',
  unknown: 'not_uploaded',
  not_found: 'not_uploaded',
  cross_workspace_only: 'not_uploaded',
  deleted_exact_match: 'not_uploaded',
  ambiguous: 'not_uploaded',
  unreachable: 'not_uploaded',
}

const SUBMISSION_STATE_FILTER_OPTIONS = [
  { value: 'unsubmitted', text: '未提交' },
  { value: 'unavailable', text: '不可用' },
  { value: 'submitting', text: '提交中' },
  { value: 'paid', text: '已完成' },
  { value: 'failed', text: '提交失败' },
  { value: 'timeout', text: '待人工复核' },
  { value: 'stopped', text: '已停止' },
]

const HAS_SUBMITTED_FILTER_OPTIONS = [
  { value: 'true', text: '有提交记录' },
  { value: 'false', text: '无提交记录' },
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
  manual_review: 'timeout',
  unknown_submit: 'timeout',
  stopped: 'stopped',
  fail: 'failed',
  error: 'failed',
}

function normalizeSubmissionStateFilterValue(value: unknown) {
  const text = String(value || '').trim().toLowerCase()
  return IDEA_SUBMIT_FILTER_VALUE_ALIASES[text] || text
}

function normalizeHasSubmittedFilterValue(value: unknown) {
  const text = String(value || '').trim().toLowerCase()
  if (['true', '1', 'yes', 'on', 'submitted'].includes(text)) return 'true'
  if (['false', '0', 'no', 'off', 'unsubmitted', 'not_submitted'].includes(text)) return 'false'
  return ''
}

function normalizeIntegrationUploadFilterValues(value: unknown): string[] {
  const normalized = normalizePresetList(value).reduce((items, item) => {
    const mapped = INTEGRATION_UPLOAD_FILTER_VALUE_ALIASES[item.toLowerCase()] || item.toLowerCase()
    if (mapped && !items.includes(mapped)) items.push(mapped)
    return items
  }, [] as string[])
  return normalized.includes('uploaded') && normalized.includes('not_uploaded') ? [] : normalized
}

const SUBSCRIPTION_EXPIRY_SORT_OPTIONS = [
  { value: 'asc', text: '到期最早' },
  { value: 'desc', text: '到期最晚' },
]

const REGISTRATION_SORT_OPTIONS = [
  { value: 'asc', text: '注册最早' },
  { value: 'desc', text: '注册最新' },
]

const ACCOUNT_FILTER_PRESET_COLUMN_KEYS: Array<keyof AccountColumnFilters> = [
  'email',
  'status',
  'manuallyUsed',
  'authType',
  'phoneBindingState',
  'paymentLinkPlatform',
  'paymentLinkGenerated',
  'subscriptionType',
  'accountValidity',
  'sub2apiState',
  'oaipayState',
  'zeroAmountEligibilityState',
  'gcashPaymentMethodState',
  'submitState',
  'hasSubmitted',
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

function normalizePaymentLinkPlatformFilterValues(value: unknown): string[] {
  return normalizePresetList(value).reduce((items, item) => {
    const text = item.toLowerCase()
    const mappedValues = PAYMENT_LINK_PLATFORM_FILTER_VALUE_ALIASES[text] || [text.replace(/-/g, '_')]
    mappedValues.forEach((mapped) => {
      if (mapped && !items.includes(mapped)) items.push(mapped)
    })
    return items
  }, [] as string[])
}

function normalizePaymentLinkGeneratedFilterValues(value: unknown): string[] {
  const normalized = normalizePresetList(value).reduce((items, item) => {
    const text = item.toLowerCase()
    const mapped = ['true', '1', 'yes', 'on', 'generated', 'succeeded'].includes(text)
      ? 'true'
      : ['false', '0', 'no', 'off', 'not_generated', 'never'].includes(text)
        ? 'false'
        : ''
    if (mapped && !items.includes(mapped)) items.push(mapped)
    return items
  }, [] as string[])
  return normalized.includes('true') && normalized.includes('false') ? [] : normalized
}

function normalizeSubscriptionTypeFilterValues(value: unknown): string[] {
  return normalizePresetList(value).reduce((items, item) => {
    const normalized = item.trim().toLowerCase().replace(/-/g, '_')
    const mapped = normalized === 'unknown'
      ? ['unconfirmable', 'pending_refresh']
      : ['unconfirmed', 'unavailable'].includes(normalized)
        ? ['unconfirmable']
        : ['waiting', 'waiting_refresh', 'stale'].includes(normalized)
          ? ['pending_refresh']
          : [normalized]
    mapped.forEach((status) => {
      if (status && !items.includes(status)) items.push(status)
    })
    return items
  }, [] as string[])
}

function cloneAccountColumnFilters(value?: Partial<Record<keyof AccountColumnFilters, unknown>>): AccountColumnFilters {
  const source = value && typeof value === 'object' ? value : {}
  const next: AccountColumnFilters = {
    email: '',
    status: [],
    manuallyUsed: [],
    authType: [],
    phoneBindingState: [],
    paymentLinkPlatform: [],
    paymentLinkGenerated: [],
    subscriptionType: [],
    accountValidity: [],
    codexState: [],
    sub2apiState: [],
    oaipayState: [],
    zeroAmountEligibilityState: [],
    gcashPaymentMethodState: [],
    submitState: [],
    hasSubmitted: [],
  }
  ACCOUNT_FILTER_PRESET_COLUMN_KEYS.forEach((key) => {
    if (key === 'email') {
      next.email = String(source.email || '').trim()
      return
    }
    const values = normalizePresetList(source[key])
    if (key === 'submitState') {
      next.submitState = values.reduce((acc, item) => {
        const normalized = normalizeSubmissionStateFilterValue(item)
        if (normalized && !acc.includes(normalized)) acc.push(normalized)
        return acc
      }, [] as string[])
      return
    }
    if (key === 'hasSubmitted') {
      next.hasSubmitted = values.reduce((acc, item) => {
        const normalized = normalizeHasSubmittedFilterValue(item)
        if (normalized && !acc.includes(normalized)) acc.push(normalized)
        return acc
      }, [] as string[])
      return
    }
    if (key === 'sub2apiState' || key === 'oaipayState') {
      next[key] = normalizeIntegrationUploadFilterValues(values)
      return
    }
    if (key === 'paymentLinkPlatform') {
      next.paymentLinkPlatform = normalizePaymentLinkPlatformFilterValues(values)
      return
    }
    if (key === 'paymentLinkGenerated') {
      next.paymentLinkGenerated = normalizePaymentLinkGeneratedFilterValues(values)
      return
    }
    if (key === 'subscriptionType') {
      next.subscriptionType = normalizeSubscriptionTypeFilterValues(values)
      return
    }
    ;(next[key] as string[]) = values
  })

  // Older presets stored `ideaSubmitState`; migrate it only when no canonical
  // submitState was supplied. Do not copy it back into the legacy request key.
  const legacyValues = normalizePresetList(
    (source as Record<string, unknown>).ideaSubmitState
      ?? (source as Record<string, unknown>).idea_submit_state,
  )
  if (next.submitState.length === 0 && legacyValues.length > 0) {
    next.submitState = legacyValues.reduce((acc, item) => {
      const normalized = normalizeSubmissionStateFilterValue(item)
      if (normalized && !acc.includes(normalized)) acc.push(normalized)
      return acc
    }, [] as string[])
  }
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
  const registrationSortOrder = source.registrationSortOrder === 'asc' || source.registrationSortOrder === 'desc'
    ? source.registrationSortOrder
    : DEFAULT_REGISTRATION_SORT_ORDER
  const pageSize = normalizeAccountsPageSize(source.pageSize) ?? DEFAULT_ACCOUNTS_PAGE_SIZE
  return {
    search,
    status,
    columnFilters,
    sortOrder,
    registrationSortOrder,
    pageSize,
  }
}

function buildAccountFilterPresetFilters(
  search: string,
  columnFilters: AccountColumnFilters,
  sortOrder: SubscriptionExpirySortOrder,
  registrationSortOrder: RegistrationSortOrder,
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
    registrationSortOrder,
  }
}

function accountFilterPresetSignature(filters?: AccountFilterPresetFilters) {
  const normalized = normalizeAccountFilterPresetFilters(filters)
  return JSON.stringify({
    search: normalized.search,
    status: normalized.status,
    columnFilters: normalized.columnFilters,
    sortOrder: normalized.sortOrder,
    registrationSortOrder: normalized.registrationSortOrder,
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
    summarizePresetValues(PHONE_BINDING_STATE_FILTER_OPTIONS, columnFilters.phoneBindingState) ? `手机号：${summarizePresetValues(PHONE_BINDING_STATE_FILTER_OPTIONS, columnFilters.phoneBindingState)}` : '',
    summarizePresetValues(PAYMENT_LINK_PLATFORM_FILTER_OPTIONS, columnFilters.paymentLinkPlatform) ? `当前链接：${summarizePresetValues(PAYMENT_LINK_PLATFORM_FILTER_OPTIONS, columnFilters.paymentLinkPlatform)}` : '',
    summarizePresetValues(PAYMENT_LINK_GENERATED_FILTER_OPTIONS, columnFilters.paymentLinkGenerated) ? `提取记录：${summarizePresetValues(PAYMENT_LINK_GENERATED_FILTER_OPTIONS, columnFilters.paymentLinkGenerated)}` : '',
    summarizePresetValues(SUBSCRIPTION_TYPE_FILTER_OPTIONS, columnFilters.subscriptionType) ? `当前订阅：${summarizePresetValues(SUBSCRIPTION_TYPE_FILTER_OPTIONS, columnFilters.subscriptionType)}` : '',
    summarizePresetValues(ACCOUNT_VALIDITY_FILTER_OPTIONS, columnFilters.accountValidity) ? `认证状态：${summarizePresetValues(ACCOUNT_VALIDITY_FILTER_OPTIONS, columnFilters.accountValidity)}` : '',
    summarizePresetValues(SUB2API_FILTER_OPTIONS, columnFilters.sub2apiState) ? `Sub2API：${summarizePresetValues(SUB2API_FILTER_OPTIONS, columnFilters.sub2apiState)}` : '',
    summarizePresetValues(OAIPAY_FILTER_OPTIONS, columnFilters.oaipayState) ? `OAIPay：${summarizePresetValues(OAIPAY_FILTER_OPTIONS, columnFilters.oaipayState)}` : '',
    summarizePresetValues(ZERO_AMOUNT_ELIGIBILITY_FILTER_OPTIONS, columnFilters.zeroAmountEligibilityState) ? `0 元资格：${summarizePresetValues(ZERO_AMOUNT_ELIGIBILITY_FILTER_OPTIONS, columnFilters.zeroAmountEligibilityState)}` : '',
    summarizePresetValues(GCASH_PAYMENT_METHOD_FILTER_OPTIONS, columnFilters.gcashPaymentMethodState) ? `GCash：${summarizePresetValues(GCASH_PAYMENT_METHOD_FILTER_OPTIONS, columnFilters.gcashPaymentMethodState)}` : '',
    summarizePresetValues(SUBMISSION_STATE_FILTER_OPTIONS, columnFilters.submitState) ? `提交状态：${summarizePresetValues(SUBMISSION_STATE_FILTER_OPTIONS, columnFilters.submitState)}` : '',
    summarizePresetValues(HAS_SUBMITTED_FILTER_OPTIONS, columnFilters.hasSubmitted) ? `提交记录：${summarizePresetValues(HAS_SUBMITTED_FILTER_OPTIONS, columnFilters.hasSubmitted)}` : '',
    normalized.sortOrder ? `到期：${labelForOption(SUBSCRIPTION_EXPIRY_SORT_OPTIONS, normalized.sortOrder)}` : '',
    normalized.registrationSortOrder !== DEFAULT_REGISTRATION_SORT_ORDER
      ? `注册：${labelForOption(REGISTRATION_SORT_OPTIONS, normalized.registrationSortOrder)}`
      : '',
  ].filter(Boolean)
  return parts.length ? parts.join(' · ') : '无筛选条件'
}

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
  status: 'queued' | 'starting' | 'running' | 'done' | 'failed' | 'cancelled' | 'stopped'
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

function mergeRegisterFormSettings(platform: string, values: Record<string, unknown>) {
  saveRegisterFormSettings(platform, {
    ...loadRegisterFormSettings(platform),
    ...values,
  })
}

function normalizeRegisterMailProviderOverride(value: unknown) {
  const normalized = String(value || '').trim().toLowerCase()
  if (normalized === 'tempmail_api') return 'tempmail_local'
  if (['icloud_hme', 'icloud_hme_ready', 'icloud_hme_helper_ready', 'helper_ready_api'].includes(normalized)) {
    return 'hme_ready_api'
  }
  return REGISTER_MAIL_PROVIDER_OVERRIDES.has(normalized) ? normalized : '__global__'
}

function normalizeRegisterTempMailMode(value: unknown, fallback = 'fixed_domain') {
  return String(value || '').trim().toLowerCase() === 'task_subdomain' ? 'task_subdomain' : fallback
}

function intWithDefault(value: unknown, fallback: number, min = 0) {
  const next = Number(value)
  if (!Number.isFinite(next)) return fallback
  return Math.max(Math.floor(next), min)
}

function boolWithDefault(value: unknown, fallback: boolean) {
  if (value === undefined || value === null || value === '') return fallback
  return parseBooleanConfigValue(value)
}

function normalizeBaxiStatusPollInterval(value: unknown) {
  return Math.min(
    intWithDefault(
      value,
      DEFAULT_BAXIGPT_STATUS_POLL_INTERVAL_SECONDS,
      BAXIGPT_STATUS_POLL_INTERVAL_MIN_SECONDS,
    ),
    BAXIGPT_STATUS_POLL_INTERVAL_MAX_SECONDS,
  )
}

function normalizePhonePoolMode(value: unknown, raw?: Record<string, unknown>): PhonePoolMode {
  const mode = String(value || '').trim().toLowerCase()
  const aliases: Record<string, PhonePoolMode> = {
    default: 'normal',
    prefix: 'prefix_limited',
    limited_prefix: 'prefix_limited',
    sample: 'prefix_sample',
    unavailable: 'unavailable_numbers',
    unavailable_all: 'unavailable_numbers',
    unavailable_only: 'unavailable_numbers',
    all_unavailable: 'unavailable_numbers',
  }
  const normalizedMode = aliases[mode] || mode
  if (normalizedMode === 'prefix_limited' || normalizedMode === 'prefix_sample' || normalizedMode === 'normal' || normalizedMode === 'unavailable_numbers') {
    return normalizedMode
  }
  if (raw && parseBooleanConfigValue(raw.prefix_bind_enabled)) return 'prefix_limited'
  if (raw && parseBooleanConfigValue(raw.prefix_sample_enabled)) return 'prefix_sample'
  if (raw && parseBooleanConfigValue(raw.unavailable_number_test_enabled)) return 'unavailable_numbers'
  return 'normal'
}

function normalizePhoneNumberFilter(value: unknown): PhoneNumberFilter {
  const filter = String(value || '').trim().toLowerCase()
  if (filter === 'unavailable' || filter === 'unavailable_only' || filter === 'rejected' || filter === 'rejected_only' || filter === 'cannot_send') {
    return 'unavailable'
  }
  if (filter === 'all' || filter === 'all_testable' || filter === 'testable' || filter === 'mixed') {
    return 'all'
  }
  return 'available'
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
  for (const key of ['total', 'available_count', 'remaining_capacity', 'rejected_count', 'cannot_send_count', 'bind_limit_count']) {
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
  const requestedPhoneNumberFilter = normalizePhoneNumberFilter(raw.prefix_number_filter || raw.phone_number_filter)
  const phoneNumberFilter: PhoneNumberFilter = phonePoolMode === 'prefix_limited'
    ? requestedPhoneNumberFilter
    : phonePoolMode === 'unavailable_numbers'
      ? 'unavailable'
      : 'available'
  const fixedSnapshotSelection = phonePoolMode === 'prefix_sample'
    || phonePoolMode === 'unavailable_numbers'
    || (phonePoolMode === 'prefix_limited' && phoneNumberFilter !== 'available')
  const reusePhoneUntilUnusable = !fixedSnapshotSelection && boolWithDefault(raw.reuse_phone_until_unusable, false)
  return {
    use_pool: boolWithDefault(raw.use_pool, DEFAULT_PHONE_BINDING_SETTINGS.use_pool),
    phone_pool_mode: phonePoolMode,
    phone_number_filter: phoneNumberFilter,
    prefix_number_filter: phoneNumberFilter,
    unavailable_number_test_enabled: phonePoolMode === 'unavailable_numbers',
    selected_prefixes: normalizeSelectedPrefixes(raw.selected_prefixes),
    prefix_sample_enabled: phonePoolMode === 'prefix_sample',
    prefix_sample_size: intWithDefault(raw.prefix_sample_size, DEFAULT_PHONE_BINDING_SETTINGS.prefix_sample_size, 1) === 2 ? 2 : 1,
    prefix_sample_filter: (() => {
      const value = String(raw.prefix_sample_filter || DEFAULT_PHONE_BINDING_SETTINGS.prefix_sample_filter)
      if (value === 'available' || value === 'available_only' || value === 'healthy' || value === 'healthy_only') return 'available'
      if (value === 'rejected' || value === 'rejected_only' || value === 'unavailable' || value === 'unavailable_only') return 'rejected'
      return 'all'
    })(),
    prefix_sms_probe_only: boolWithDefault(raw.prefix_sms_probe_only, boolWithDefault(raw.sms_probe_only, false)),
    timeout_seconds: intWithDefault(raw.timeout_seconds, DEFAULT_PHONE_BINDING_SETTINGS.timeout_seconds, 10),
    poll_interval_seconds: intWithDefault(raw.poll_interval_seconds, DEFAULT_PHONE_BINDING_SETTINGS.poll_interval_seconds, 1),
    max_resend_attempts: intWithDefault(raw.max_resend_attempts, DEFAULT_PHONE_BINDING_SETTINGS.max_resend_attempts, 0),
    resend_interval_seconds: intWithDefault(raw.resend_interval_seconds, DEFAULT_PHONE_BINDING_SETTINGS.resend_interval_seconds, 0),
    account_interval_seconds: intWithDefault(raw.account_interval_seconds, DEFAULT_PHONE_BINDING_SETTINGS.account_interval_seconds, 1),
    concurrency: reusePhoneUntilUnusable ? 1 : intWithDefault(raw.concurrency, DEFAULT_PHONE_BINDING_SETTINGS.concurrency, 1),
    reuse_phone_until_unusable: reusePhoneUntilUnusable,
    proxy_mode: (() => {
      const value = String(raw.proxy_mode || DEFAULT_PHONE_BINDING_SETTINGS.proxy_mode).trim()
      return value === 'direct' || value === 'specified' || value === 'pool' || value === 'dynamic' ? value : 'pool'
    })(),
    proxy: String(raw.proxy || ''),
    proxy_country_code: String(raw.proxy_country_code || '').trim().toUpperCase(),
    proxy_failover: boolWithDefault(raw.proxy_failover, DEFAULT_PHONE_BINDING_SETTINGS.proxy_failover),
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

function loadInvalidRecheckConcurrency() {
  if (typeof window === 'undefined') return 1
  const value = Number(window.localStorage.getItem(INVALID_RECHECK_CONCURRENCY_STORAGE_KEY) || 1)
  return Number.isFinite(value) ? Math.max(1, Math.floor(value)) : 1
}

function saveInvalidRecheckConcurrency(value: unknown) {
  if (typeof window === 'undefined') return
  const parsed = Number(value)
  const concurrency = Number.isFinite(parsed) ? Math.max(1, Math.floor(parsed)) : 1
  window.localStorage.setItem(INVALID_RECHECK_CONCURRENCY_STORAGE_KEY, String(concurrency))
}

function loadWebSessionLoginConcurrency() {
  if (typeof window === 'undefined') return 1
  const value = Number(window.localStorage.getItem(WEB_SESSION_LOGIN_CONCURRENCY_STORAGE_KEY) || 1)
  return Number.isFinite(value) ? Math.max(1, Math.floor(value)) : 1
}

function saveWebSessionLoginConcurrency(value: unknown) {
  if (typeof window === 'undefined') return
  const parsed = Number(value)
  const concurrency = Number.isFinite(parsed) ? Math.max(1, Math.floor(parsed)) : 1
  window.localStorage.setItem(WEB_SESSION_LOGIN_CONCURRENCY_STORAGE_KEY, String(concurrency))
}

function normalizePaymentEligibilityConcurrency(value: unknown) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return PAYMENT_ELIGIBILITY_DEFAULT_CONCURRENCY
  return Math.max(1, Math.min(PAYMENT_ELIGIBILITY_MAX_CONCURRENCY, Math.floor(parsed)))
}

function loadPaymentEligibilityConcurrency() {
  if (typeof window === 'undefined') return PAYMENT_ELIGIBILITY_DEFAULT_CONCURRENCY
  const stored = window.localStorage.getItem(PAYMENT_ELIGIBILITY_CONCURRENCY_STORAGE_KEY)
  return normalizePaymentEligibilityConcurrency(stored || PAYMENT_ELIGIBILITY_DEFAULT_CONCURRENCY)
}

function savePaymentEligibilityConcurrency(value: unknown) {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(
    PAYMENT_ELIGIBILITY_CONCURRENCY_STORAGE_KEY,
    String(normalizePaymentEligibilityConcurrency(value)),
  )
}

function normalizeBaxiGptCdkSettings(value: unknown): BaxiGptCdkSettings {
  const raw = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  return {
    payment_channel: raw.payment_channel === 'pix' ? 'pix' : 'ideal',
    pix_submit_mode: raw.pix_submit_mode === 'user_link' ? 'user_link' : 'auto_extract',
    use_pool: boolWithDefault(raw.use_pool, DEFAULT_BAXIGPT_CDK_SETTINGS.use_pool),
    precheck: boolWithDefault(raw.precheck, DEFAULT_BAXIGPT_CDK_SETTINGS.precheck),
    failure_continue: boolWithDefault(raw.failure_continue, DEFAULT_BAXIGPT_CDK_SETTINGS.failure_continue),
    submit_interval_seconds: intWithDefault(raw.submit_interval_seconds, DEFAULT_BAXIGPT_CDK_SETTINGS.submit_interval_seconds, 0),
    auto_poll_status: boolWithDefault(raw.auto_poll_status, DEFAULT_BAXIGPT_CDK_SETTINGS.auto_poll_status),
    status_poll_interval_seconds: normalizeBaxiStatusPollInterval(raw.status_poll_interval_seconds),
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
    sms_api_test_mode: boolWithDefault(raw.sms_api_test_mode, DEFAULT_PAYPAL_BINDING_SETTINGS.sms_api_test_mode),
    otp_timeout: intWithDefault(raw.otp_timeout, DEFAULT_PAYPAL_BINDING_SETTINGS.otp_timeout, 30),
    pplink_retry: intWithDefault(raw.pplink_retry, DEFAULT_PAYPAL_BINDING_SETTINGS.pplink_retry, 1),
    timeout: intWithDefault(raw.timeout, DEFAULT_PAYPAL_BINDING_SETTINGS.timeout, 5),
    event_timeout: intWithDefault(raw.event_timeout, DEFAULT_PAYPAL_BINDING_SETTINGS.event_timeout, 10),
    account_interval_seconds: intWithDefault(raw.account_interval_seconds, DEFAULT_PAYPAL_BINDING_SETTINGS.account_interval_seconds, 0),
    failure_continue: boolWithDefault(raw.failure_continue, DEFAULT_PAYPAL_BINDING_SETTINGS.failure_continue),
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

function loadVisibleAccountColumnKeys(): AccountColumnKey[] {
  if (typeof window === 'undefined') return [...DEFAULT_VISIBLE_ACCOUNT_COLUMNS]
  try {
    const raw = window.localStorage.getItem(ACCOUNT_COLUMN_VISIBILITY_STORAGE_KEY)
    if (!raw) {
      for (const legacyKey of LEGACY_ACCOUNT_COLUMN_VISIBILITY_STORAGE_KEYS) {
        const legacyRaw = window.localStorage.getItem(legacyKey)
        if (!legacyRaw) continue
        let legacyColumns = normalizeVisibleAccountColumns(JSON.parse(legacyRaw))
        if (legacyKey.endsWith('.v2') && !legacyColumns.includes('payment_link')) {
          legacyColumns = [...legacyColumns, 'payment_link']
        }
        if (!legacyColumns.includes('payment_eligibility')) {
          legacyColumns = [...legacyColumns, 'payment_eligibility']
        }
        return legacyColumns
      }
      return [...DEFAULT_VISIBLE_ACCOUNT_COLUMNS]
    }
    return normalizeVisibleAccountColumns(JSON.parse(raw))
  } catch {
    return [...DEFAULT_VISIBLE_ACCOUNT_COLUMNS]
  }
}

function saveVisibleAccountColumnKeys(keys: AccountColumnKey[]) {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(ACCOUNT_COLUMN_VISIBILITY_STORAGE_KEY, JSON.stringify(normalizeVisibleAccountColumns(keys)))
}

function normalizePinnedToolbarActions(value: unknown): AccountToolbarActionId[] {
  if (!Array.isArray(value)) return [...DEFAULT_PINNED_ACCOUNT_TOOLBAR_ACTIONS]
  const allowed = new Set(ACCOUNT_TOOLBAR_ACTION_OPTIONS.map((item) => item.value))
  const normalized = value
    .map((item) => String(item || '').trim())
    .filter((item): item is AccountToolbarActionId => allowed.has(item as AccountToolbarActionId))
  return Array.from(new Set(normalized))
}

function loadPinnedToolbarActions() {
  if (typeof window === 'undefined') return [...DEFAULT_PINNED_ACCOUNT_TOOLBAR_ACTIONS]
  try {
    const raw = window.localStorage.getItem(ACCOUNT_TOOLBAR_ACTION_VISIBILITY_STORAGE_KEY)
    if (!raw) return [...DEFAULT_PINNED_ACCOUNT_TOOLBAR_ACTIONS]
    return normalizePinnedToolbarActions(JSON.parse(raw))
  } catch {
    return [...DEFAULT_PINNED_ACCOUNT_TOOLBAR_ACTIONS]
  }
}

function savePinnedToolbarActions(keys: AccountToolbarActionId[]) {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(ACCOUNT_TOOLBAR_ACTION_VISIBILITY_STORAGE_KEY, JSON.stringify(normalizePinnedToolbarActions(keys)))
}

function hasPaymentLinkSuccessEvidence(account: any, paymentLink: any) {
  const explicit = account?.payment_link_generated ?? account?.paymentLinkGenerated
  const status = String(paymentLink?.link_status || '').trim().toLowerCase()
  if (String(paymentLink?.url || '').trim() || PAYMENT_LINK_CLEANED_STATUS_META[status]) return true
  if (explicit !== undefined && explicit !== null && String(explicit).trim() !== '') {
    return parseBooleanConfigValue(explicit)
  }
  return false
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
  const cliproxySync = account.cliproxySync && typeof account.cliproxySync === 'object'
    ? account.cliproxySync
    : syncStatuses.cliproxyapi && typeof syncStatuses.cliproxyapi === 'object'
      ? syncStatuses.cliproxyapi
      : {}
  const sub2apiSync = account.sub2apiSync && typeof account.sub2apiSync === 'object'
    ? account.sub2apiSync
    : syncStatuses.sub2api && typeof syncStatuses.sub2api === 'object'
      ? syncStatuses.sub2api
      : {}
  const oaipaySync = account.oaipaySync && typeof account.oaipaySync === 'object'
    ? account.oaipaySync
    : syncStatuses.oaipay && typeof syncStatuses.oaipay === 'object'
      ? syncStatuses.oaipay
      : {}
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
  const paymentLink = account.paymentLink && typeof account.paymentLink === 'object'
    ? account.paymentLink
    : account.payment_link && typeof account.payment_link === 'object'
      ? account.payment_link
      : account.chatgptLastPaymentLink && typeof account.chatgptLastPaymentLink === 'object'
        ? account.chatgptLastPaymentLink
        : extra.chatgpt_last_payment_link && typeof extra.chatgpt_last_payment_link === 'object'
          ? extra.chatgpt_last_payment_link
          : {}
  const zeroAmountEligibility = account.zero_amount_eligibility && typeof account.zero_amount_eligibility === 'object'
    ? account.zero_amount_eligibility
    : account.zeroAmountEligibility && typeof account.zeroAmountEligibility === 'object'
      ? account.zeroAmountEligibility
      : extra.chatgpt_zero_amount_eligibility && typeof extra.chatgpt_zero_amount_eligibility === 'object'
        ? extra.chatgpt_zero_amount_eligibility
        : {}
  const gcashPaymentMethod = account.gcash_payment_method && typeof account.gcash_payment_method === 'object'
    ? account.gcash_payment_method
    : account.gcashPaymentMethod && typeof account.gcashPaymentMethod === 'object'
      ? account.gcashPaymentMethod
      : extra.chatgpt_gcash_payment_method && typeof extra.chatgpt_gcash_payment_method === 'object'
        ? extra.chatgpt_gcash_payment_method
        : {}
  const chatgptPaymentLinkDefaults = extra.chatgpt_payment_link_defaults && typeof extra.chatgpt_payment_link_defaults === 'object'
    ? extra.chatgpt_payment_link_defaults
    : {}
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
    oaipaySync,
    chatgptLocal,
    chatgptCapabilities,
    chatgptPendingSubscriptionAuth,
    chatgptGopay,
    chatgptGopayDefaults,
    chatgptLastPaymentLink: paymentLink,
    paymentLink,
    zeroAmountEligibility,
    zero_amount_eligibility: zeroAmountEligibility,
    gcashPaymentMethod,
    gcash_payment_method: gcashPaymentMethod,
    paymentLinkPlatform: String(account.payment_link_platform || paymentLink.platform || '').trim().toLowerCase(),
    paymentLinkGenerated: hasPaymentLinkSuccessEvidence(account, paymentLink),
    chatgptPaymentLinkDefaults,
    phoneBinding,
    rateLimit,
    rate_limit: rateLimit,
    rate_limit_started_at: rateLimit.started_at,
    rate_limit_recover_at: rateLimit.recover_at,
    rate_limit_previous_status: rateLimit.previous_status,
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
  return ['paid', 'failed', 'disabled', 'stopped'].includes(String(status || '').trim().toLowerCase())
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

function subscriptionRefreshTime(record: any) {
  const localSubscription = record?.chatgptLocal?.subscription && typeof record.chatgptLocal.subscription === 'object'
    ? record.chatgptLocal.subscription
    : {}
  const compactSubscription = record?.subscription && typeof record.subscription === 'object'
    ? record.subscription
    : {}
  const legacyLocal = record?.extra?.chatgpt_local && typeof record.extra.chatgpt_local === 'object'
    ? record.extra.chatgpt_local
    : {}
  const legacySubscription = legacyLocal.subscription && typeof legacyLocal.subscription === 'object'
    ? legacyLocal.subscription
    : {}
  const value = [
    localSubscription.checked_at,
    compactSubscription.checked_at,
    legacySubscription.checked_at,
    legacyLocal.checked_at,
  ].find((candidate) => String(candidate || '').trim())
  return value ? formatCompactDateTime(String(value)) : null
}

type SubscriptionTypeMeta = {
  color: string
  label: string
  subLabel: string
  refreshTimeLabel: string
  refreshTimeTitle: string
  title: string
}

function subscriptionTypeMeta(record: any): SubscriptionTypeMeta {
  const current = subscriptionTypeValue(record)
  const lastKnown = lastKnownSubscriptionTypeValue(record)
  const refreshState = String(record?.subscription_refresh_state || record?.chatgptLocal?.subscription?.refresh_state || record?.chatgptCapabilities?.subscription_refresh_state || '').trim().toLowerCase()
  const stale = current === 'unknown' && lastKnown !== 'unknown'
  if (stale) {
    const last = planMeta(lastKnown)
    const refreshTime = subscriptionRefreshTime(record)
    const refreshTimeLabel = refreshTime?.compact || ''
    const refreshTimeTitle = refreshTime?.title || ''
    if (accountValidityValue(record) === 'invalid' || refreshState === 'auth_invalid') {
      return {
        color: 'error',
        label: '不可确认',
        subLabel: `上次 ${last.label}`,
        refreshTimeLabel,
        refreshTimeTitle,
        title: `当前订阅因认证失效不可确认；上次确认：${last.label}${refreshTimeTitle ? `；刷新时间：${refreshTimeTitle}` : ''}`,
      }
    }
    const staleLabel = refreshState === 'refreshing'
      ? '刷新中'
      : refreshState === 'refresh_failed' || refreshState === 'unknown_plan'
        ? '刷新失败'
        : '待刷新'
    const refreshError = String(
      record?.subscription?.refresh_last_error
      || record?.chatgptLocal?.subscription?.refresh_last_error
      || '',
    ).trim()
    return {
      color: 'warning',
      label: staleLabel,
      subLabel: `上次 ${last.label}`,
      refreshTimeLabel,
      refreshTimeTitle,
      title: `当前订阅${staleLabel === '刷新中' ? '正在刷新' : staleLabel === '刷新失败' ? '刷新失败' : '未确认'}；上次确认：${last.label}${refreshTimeTitle ? `；刷新时间：${refreshTimeTitle}` : ''}${refreshError ? `；原因：${refreshError}` : ''}`,
    }
  }
  const refreshHint = refreshState === 'refreshing'
    ? '刷新中'
    : refreshState === 'refresh_failed'
      ? '刷新失败'
      : ''
  switch (current) {
    case 'free':
      return { color: 'default', label: 'Free', subLabel: refreshHint, refreshTimeLabel: '', refreshTimeTitle: '', title: `当前确认订阅：Free${refreshHint ? `；${refreshHint}` : ''}` }
    case 'plus':
      return { color: 'success', label: 'Plus', subLabel: refreshHint, refreshTimeLabel: '', refreshTimeTitle: '', title: `当前确认订阅：Plus${refreshHint ? `；${refreshHint}` : ''}` }
    case 'team':
      return { color: 'processing', label: 'Team', subLabel: refreshHint, refreshTimeLabel: '', refreshTimeTitle: '', title: `当前确认订阅：Team / Business${refreshHint ? `；${refreshHint}` : ''}` }
    case 'pro':
      return { color: 'processing', label: 'Pro', subLabel: refreshHint, refreshTimeLabel: '', refreshTimeTitle: '', title: `当前确认订阅：Pro${refreshHint ? `；${refreshHint}` : ''}` }
    case 'enterprise':
      return { color: 'processing', label: 'Enterprise', subLabel: refreshHint, refreshTimeLabel: '', refreshTimeTitle: '', title: `当前确认订阅：Enterprise${refreshHint ? `；${refreshHint}` : ''}` }
    default:
      if (accountValidityValue(record) === 'invalid' || refreshState === 'auth_invalid') {
        return { color: 'error', label: '不可确认', subLabel: '', refreshTimeLabel: '', refreshTimeTitle: '', title: '当前订阅因认证失效不可确认' }
      }
      return {
        color: 'warning',
        label: refreshState === 'refreshing' ? '刷新中' : refreshState === 'refresh_failed' || refreshState === 'unknown_plan' ? '刷新失败' : '待刷新',
        subLabel: '',
        refreshTimeLabel: '',
        refreshTimeTitle: '',
        title: '当前订阅尚未确认，等待刷新',
      }
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

type SubmissionTag = { color: string; label: string }

function getSubmissionRecord(value: unknown): Record<string, any> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, any>
    : {}
}

function getSubmissionSummary(record: any): Record<string, any> {
  // New generic fields win, but merge legacy fields underneath so a partially
  // migrated list response still renders its order/reason metadata.
  const candidates = [
    record?.extra?.idea_submit,
    record?.ideaSubmit,
    record?.idea_submit,
    record?.extra?.submission_summary,
    record?.submission_summary,
    record?.submissionSummary,
    record?.extra?.submission,
    record?.submission,
  ].map(getSubmissionRecord)
  const merged = candidates.reduce<Record<string, any>>((result, item) => ({ ...result, ...item }), {})
  if (record?.submit_state !== undefined && merged.state === undefined && merged.status === undefined) {
    merged.state = record.submit_state
  }
  if (record?.has_submitted !== undefined && merged.has_submitted === undefined) {
    merged.has_submitted = record.has_submitted
  }
  return merged
}

function parseSubmissionBoolean(value: unknown): boolean | null {
  if (typeof value === 'boolean') return value
  const text = String(value ?? '').trim().toLowerCase()
  if (['true', '1', 'yes', 'on'].includes(text)) return true
  if (['false', '0', 'no', 'off', ''].includes(text)) return text === '' ? null : false
  return null
}

function submissionStateValue(record: any, summary: Record<string, any>) {
  const raw = String(
    summary.state
      ?? summary.status
      ?? summary.final_state
      ?? summary.result_state
      ?? record?.submit_state
      ?? '',
  ).trim().toLowerCase()
  return normalizeSubmissionStateFilterValue(raw)
}

function submissionHasSubmitted(record: any, summary: Record<string, any>, state: string) {
  const explicit = parseSubmissionBoolean(summary.has_submitted ?? record?.has_submitted)
  if (explicit !== null) return explicit
  const linkSubmitted = parseSubmissionBoolean(summary.link_submitted)
  if (linkSubmitted !== null) return linkSubmitted
  const linkStatus = String(summary.link_status || '').trim().toLowerCase()
  if (['pix_submitted', 'submitted', 'consumed', 'used'].includes(linkStatus)) return true
  if (String(summary.order_id || summary.orderId || summary.display_id || summary.displayId || '').trim()) return true
  return ['submitting', 'paid'].includes(state)
}

function submissionMeta(record: any) {
  const summary = getSubmissionSummary(record)
  const state = submissionStateValue(record, summary)
  const hasSubmitted = submissionHasSubmitted(record, summary, state)
  const unavailable = Boolean(
    parseSubmissionBoolean(summary.unavailable)
      || state === 'unavailable'
      || String(summary.eligibility_state || '').trim().toLowerCase() === 'unavailable',
  )
  const tags: SubmissionTag[] = []
  if (hasSubmitted) tags.push({ color: 'processing', label: '已提交' })
  if (unavailable) tags.push({ color: 'error', label: '不可用' })
  if (state === 'paid') {
    tags.push({ color: 'success', label: '已完成' })
  } else if (state === 'failed') {
    tags.push({ color: 'warning', label: '提交失败' })
  } else if (state === 'timeout') {
    tags.push({ color: 'warning', label: '待人工复核' })
  } else if (state === 'stopped') {
    tags.push({ color: 'default', label: '已停止' })
  } else if (state === 'submitting') {
    tags.push({ color: 'processing', label: '处理中' })
  } else if (!hasSubmitted && !unavailable) {
    tags.push({ color: 'default', label: '未提交' })
  }
  if (tags.length === 0) tags.push({ color: 'default', label: '未提交' })
  return {
    summary,
    state,
    hasSubmitted,
    tags,
    color: tags[tags.length - 1]?.color || 'default',
    label: tags.map((tag) => tag.label).join(' · '),
    reason: String(summary.reason || '').trim(),
  }
}

function ideaSubmitMeta(record: any) {
  return submissionMeta(record)
}

function isPaypalBindingEligibleAccount(record: any) {
  const status = String(record?.status || '').trim().toLowerCase()
  return status !== 'subscribed'
    && PAYPAL_BINDING_ALLOWED_ACCOUNT_VALIDITY.has(accountValidityValue(record))
    && PAYPAL_BINDING_ALLOWED_SUBSCRIPTION_TYPES.has(subscriptionTypeValue(record))
}

function integrationUploadStateMeta(sync: any) {
  const state = sync && typeof sync === 'object' ? sync : {}
  const remoteState = String(state.remote_state || '').trim().toLowerCase()
  const lastUpload = state.last_upload && typeof state.last_upload === 'object' ? state.last_upload : {}
  const uploaded = parseSubmissionBoolean(state.uploaded) === true
    || remoteState === 'uploaded'
    || remoteState === 'exists'
    || String(lastUpload.status || '').trim().toLowerCase() === 'success'
  return uploaded
    ? { color: 'success', label: '已上传' }
    : { color: 'default', label: '未上传' }
}

function shouldShowResumeAuthButton(record: any) {
  const capabilities = record?.chatgptCapabilities || {}
  const authLevel = String(record?.auth_level || capabilities.auth_level || '').trim().toLowerCase()
  const uploadGate = String(capabilities.upload_gate || '').trim().toLowerCase()
  const status = String(record?.status || '').trim().toLowerCase()
  const pendingStatus = String(record?.chatgptPendingSubscriptionAuth?.status || '').trim().toLowerCase()
  if (status === 'pending_payment') return true
  if (pendingStatus && pendingStatus !== 'completed' && pendingStatus !== 'abandoned') return true
  if (authLevel === 'registered_auth_pending' || authLevel === 'access_token_only' || authLevel === 'invalid') return true
  if (uploadGate === 'blocked_missing_at' || uploadGate === 'blocked_missing_rt' || uploadGate === 'blocked_missing_workspace') return true
  return false
}

function shouldShowInvalidRecheckButton(record: any) {
  return String(record?.status || '').trim().toLowerCase() === 'invalid'
}

function taskModalModeFromSource(source: unknown): 'register' | 'resume_auth' | 'web_session_login' | 'invalid_recheck' | 'payment_eligibility' | 'payment_link' | 'pix_cleanup' | 'sub2api_upload' | 'oaipay_upload' | 'baxigpt_cdk' | 'paypal_bind' | 'probe_local_status' {
  const normalized = String(source || '').trim().toLowerCase()
  if (normalized === 'baxigpt_cdk' || normalized === 'baxigpt_cdk_submit') return 'baxigpt_cdk'
  if (normalized === 'chatgpt_paypal_bind' || normalized === 'paypal_bind') return 'paypal_bind'
  if (normalized === 'phone_binding_test') return 'resume_auth'
  if (normalized === 'resume_auth' || normalized === 'resume_subscription_auth' || normalized === 'batch_resume_subscription_auth') return 'resume_auth'
  if (normalized === 'batch_probe_local_status' || normalized === 'probe_local_status') return 'probe_local_status'
  if (normalized === 'batch_sub2api_upload') return 'sub2api_upload'
  if (normalized === 'batch_oaipay_upload') return 'oaipay_upload'
  if (normalized === 'web_session_login' || normalized === 'batch_web_session_login') return 'web_session_login'
  if (normalized === 'invalid_recheck' || normalized === 'batch_invalid_recheck') return 'invalid_recheck'
  if (normalized === 'zero_amount_eligibility' || normalized === 'batch_zero_amount_eligibility' || normalized === 'gcash_payment_method' || normalized === 'batch_gcash_payment_method') return 'payment_eligibility'
  if (normalized === 'payment_link' || normalized === 'batch_payment_link') return 'payment_link'
  if (normalized === 'pix_cleanup' || normalized === 'pix_payment_link_cleanup' || normalized === 'upi_payment_link_cleanup' || normalized === 'ideal_payment_link_cleanup' || normalized === 'payment_link_cleanup') return 'pix_cleanup'
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
  const { message: appMessage, modal: appModal } = App.useApp()
  const { token } = theme.useToken()
  const subscriptionHistoryTextStyle: CSSProperties = {
    color: token.colorTextSecondary,
    fontSize: 12,
    lineHeight: '15px',
    whiteSpace: 'nowrap',
  }
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
  const [defaultAccountsPageSize, setDefaultAccountsPageSize] = useState(loadDefaultAccountsPageSize)
  const [accountsPageSize, setAccountsPageSize] = useState(defaultAccountsPageSize)
  const [customAccountsPageSizeOptions, setCustomAccountsPageSizeOptions] = useState(
    loadCustomAccountsPageSizeOptions,
  )
  const accountsPageSizeOptions = useMemo(
    () => Array.from(new Set([...ACCOUNT_PAGE_SIZE_OPTIONS, ...customAccountsPageSizeOptions]))
      .sort((left, right) => left - right),
    [customAccountsPageSizeOptions],
  )
  const [columnFilters, setColumnFilters] = useState<AccountColumnFilters>(EMPTY_ACCOUNT_FILTERS)
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [pageVisible, setPageVisible] = useState(
    typeof document === 'undefined' ? true : document.visibilityState === 'visible',
  )
  const [filterStatus, setFilterStatus] = useState('')
  const [subscriptionExpirySortOrder, setSubscriptionExpirySortOrder] = useState<SubscriptionExpirySortOrder>('')
  const [registrationSortOrder, setRegistrationSortOrder] = useState<RegistrationSortOrder>(DEFAULT_REGISTRATION_SORT_ORDER)
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([])
  const [selectedAccountSnapshots, setSelectedAccountSnapshots] = useState<Record<string, any>>({})
  const [filterPresets, setFilterPresets] = useState<AccountFilterPreset[]>([])
  const [fixedGroups, setFixedGroups] = useState<AccountFilterPreset[]>([])
  const [legacyFixedPresets, setLegacyFixedPresets] = useState<AccountFilterPreset[]>([])
  const [filterPresetLoading, setFilterPresetLoading] = useState(false)
  const [filterPresetSaving, setFilterPresetSaving] = useState(false)
  const [activeFilterPresetId, setActiveFilterPresetId] = useState('')
  const [filterPresetManageOpen, setFilterPresetManageOpen] = useState(false)
  const [filterPresetEditorOpen, setFilterPresetEditorOpen] = useState(false)
  const [filterPresetEditing, setFilterPresetEditing] = useState<AccountFilterPreset | null>(null)
  const [filterPresetEditorMode, setFilterPresetEditorMode] = useState<'create-current' | 'edit-meta' | 'copy-preset'>('create-current')
  const [filterPresetEditorAccountIds, setFilterPresetEditorAccountIds] = useState<number[]>([])
  const [secondaryFilterScope, setSecondaryFilterScope] = useState<'unassigned' | 'fixed'>('unassigned')
  const [activeFixedGroupId, setActiveFixedGroupId] = useState('')
  const [fixedMigrationOpen, setFixedMigrationOpen] = useState(false)
  const [fixedMigrationLoading, setFixedMigrationLoading] = useState(false)
  const [fixedMigrationParentById, setFixedMigrationParentById] = useState<Record<string, string>>({})
  const [fixedMigrationPriority, setFixedMigrationPriority] = useState<string[]>([])
  const [fixedMigrationPreview, setFixedMigrationPreview] = useState<any>(null)

  const [registerModalOpen, setRegisterModalOpen] = useState(false)
  const [taskModalMode, setTaskModalMode] = useState<'register' | 'resume_auth' | 'web_session_login' | 'invalid_recheck' | 'payment_eligibility' | 'payment_link' | 'pix_cleanup' | 'sub2api_upload' | 'oaipay_upload' | 'baxigpt_cdk' | 'paypal_bind' | 'probe_local_status'>('register')
  const [taskModalAccount, setTaskModalAccount] = useState<any>(null)
  const [addModalOpen, setAddModalOpen] = useState(false)
  const [importModalOpen, setImportModalOpen] = useState(false)
  const [detailModalOpen, setDetailModalOpen] = useState(false)
  const [actionSurfaceOpen, setActionSurfaceOpen] = useState(false)
  const [detailAccount, setDetailAccount] = useState<any>(null)
  const [actionAccount, setActionAccount] = useState<any>(null)
  const [actionSurfaceInitialActionId, setActionSurfaceInitialActionId] = useState<string | null>(null)
  const [actionSurfaceInitialActionMode, setActionSurfaceInitialActionMode] = useState<'direct' | 'dialog'>('dialog')
  const [resumeAuthAccountId, setResumeAuthAccountId] = useState<number | null>(null)
  const [resumeAuthConfigOpen, setResumeAuthConfigOpen] = useState(false)
  const [resumeAuthConfigMode, setResumeAuthConfigMode] = useState<'single' | 'batch'>('single')
  const [resumeAuthConfigAccount, setResumeAuthConfigAccount] = useState<any>(null)
  const [resumeAuthConfigScope, setResumeAuthConfigScope] = useState<'selected' | 'filtered'>('selected')
  const [invalidRecheckConfigOpen, setInvalidRecheckConfigOpen] = useState(false)
  const [invalidRecheckConfigMode, setInvalidRecheckConfigMode] = useState<'single' | 'batch'>('single')
  const [invalidRecheckConfigAccount, setInvalidRecheckConfigAccount] = useState<any>(null)
  const [invalidRecheckConfigScope, setInvalidRecheckConfigScope] = useState<'selected' | 'filtered'>('selected')
  const [webSessionLoginConfigOpen, setWebSessionLoginConfigOpen] = useState(false)
  const [webSessionLoginConfigMode, setWebSessionLoginConfigMode] = useState<'single' | 'batch'>('single')
  const [webSessionLoginConfigAccount, setWebSessionLoginConfigAccount] = useState<any>(null)
  const [webSessionLoginConfigScope, setWebSessionLoginConfigScope] = useState<'selected' | 'filtered'>('selected')
  const [paymentEligibilityConfigOpen, setPaymentEligibilityConfigOpen] = useState(false)
  const [paymentEligibilityConfigKind, setPaymentEligibilityConfigKind] = useState<PaymentEligibilityKind>('zero_amount_eligibility')
  const [paymentEligibilityConfigScope, setPaymentEligibilityConfigScope] = useState<AccountTaskScope>('selected')
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
  const [batchPaymentLinkTargetAccount, setBatchPaymentLinkTargetAccount] = useState<any>(null)
  const [batchPaymentLinkProfile, setBatchPaymentLinkProfile] = useState<PaymentLinkProfile | null>(null)
  const [batchPaymentLinkProfileLoading, setBatchPaymentLinkProfileLoading] = useState(false)
  const [batchPaymentLinkProfileError, setBatchPaymentLinkProfileError] = useState('')
  const [batchPaymentLinkPlan, setBatchPaymentLinkPlan] = useState<PaymentLinkGenerationMode>('plus')
  const [teamBillingCountryOptions, setTeamBillingCountryOptions] = useState<Array<{ value: string; label: string; currency: string }>>([])
  const [teamProxyCountrySearch, setTeamProxyCountrySearch] = useState('')
  const [registerForm] = Form.useForm()
  const [addForm] = Form.useForm()
  const [detailForm] = Form.useForm()
  const [resumeAuthConfigForm] = Form.useForm()
  const [invalidRecheckConfigForm] = Form.useForm()
  const [webSessionLoginConfigForm] = Form.useForm()
  const [paymentEligibilityConfigForm] = Form.useForm()
  const [phoneBindingTestForm] = Form.useForm()
  const [baxiCdkSubmitForm] = Form.useForm()
  const [paypalBindingForm] = Form.useForm()
  const [batchPaymentLinkForm] = Form.useForm()
  const [filterPresetForm] = Form.useForm()
  const filterPresetContentMode = Form.useWatch('mode', filterPresetForm) === 'fixed' ? 'fixed' : 'dynamic'
  const teamProxyCountryOptions = useMemo(() => {
    const typed = String(teamProxyCountrySearch || '').trim().toUpperCase()
    const codes = [...TEAM_PROXY_COUNTRY_CODES]
    if (/^[A-Z]{2}$/.test(typed) && !codes.includes(typed as (typeof TEAM_PROXY_COUNTRY_CODES)[number])) {
      codes.unshift(typed as (typeof TEAM_PROXY_COUNTRY_CODES)[number])
    }
    return codes.map((code) => ({ value: code, label: teamProxyCountryLabel(code) }))
  }, [teamProxyCountrySearch])
  const phoneBindingUsePoolValue = Form.useWatch('use_pool', phoneBindingTestForm)
  const phoneBindingPoolModeValue = Form.useWatch('phone_pool_mode', phoneBindingTestForm)
  const phoneBindingNumberFilterValue = Form.useWatch('phone_number_filter', phoneBindingTestForm)
  const phoneBindingSelectedPrefixesValue = Form.useWatch('selected_prefixes', phoneBindingTestForm)
  const phoneBindingPrefixSampleSizeValue = Form.useWatch('prefix_sample_size', phoneBindingTestForm)
  const phoneBindingPrefixSampleFilterValue = Form.useWatch('prefix_sample_filter', phoneBindingTestForm)
  const phoneBindingSmsProbeOnlyValue = Form.useWatch('prefix_sms_probe_only', phoneBindingTestForm)
  const phoneBindingReusePhoneValue = Form.useWatch('reuse_phone_until_unusable', phoneBindingTestForm)
  const phoneBindingPhoneLinesValue = Form.useWatch('phone_lines', phoneBindingTestForm)
  const phoneBindingProxyModeValue = Form.useWatch('proxy_mode', phoneBindingTestForm)
  const phoneBindingProxyFailoverValue = Form.useWatch('proxy_failover', phoneBindingTestForm)
  const invalidRecheckProxyModeValue = Form.useWatch('proxy_mode', invalidRecheckConfigForm)
  const invalidRecheckProxyFailoverValue = Form.useWatch('proxy_failover', invalidRecheckConfigForm)
  const webSessionLoginProxyModeValue = Form.useWatch('proxy_mode', webSessionLoginConfigForm)
  const webSessionLoginProxyFailoverValue = Form.useWatch('proxy_failover', webSessionLoginConfigForm)
  const baxiCdkUsePoolValue = Form.useWatch('use_pool', baxiCdkSubmitForm)
  const baxiCdkCodeLinesValue = Form.useWatch('code_lines', baxiCdkSubmitForm)
  const baxiCdkSelectedIdsValue = Form.useWatch('cdk_ids', baxiCdkSubmitForm)
  const baxiCdkTargetSuccessValue = Form.useWatch('target_success_count', baxiCdkSubmitForm)
  const baxiPaymentChannelValue = Form.useWatch('payment_channel', baxiCdkSubmitForm)
  const baxiPixSubmitModeValue = Form.useWatch('pix_submit_mode', baxiCdkSubmitForm)
  const [registerMailProvider, setRegisterMailProvider] = useState('luckmail')
  const [configCache, setConfigCache] = useState<Record<string, any> | null>(null)
  const configCacheRef = useRef<Record<string, any> | null>(null)
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
    setOaipayCategories([])
    setOaipaySelectedCategory(undefined)
    setOaipayFallbackCategory(undefined)
    setOaipayUploadModalOpen(true)
    setOaipayCategoryLoading(true)
    try {
      const res = await apiFetch('/integrations/oaipay-categories')
      if (res?.success === false) {
        setOaipayCategories([])
        message.error('无法获取 OAIPay 分组: ' + (res.error || '远端未返回可用分组'))
      } else if (Array.isArray(res)) {
        setOaipayCategories(res)
      } else if (res?.categories && Array.isArray(res.categories)) {
        setOaipayCategories(res.categories)
      } else if (res?.data && Array.isArray(res.data)) {
        setOaipayCategories(res.data)
      } else if (res?.error) {
        message.error('无法获取 OAIPay 分组: ' + res.error)
      } else {
        setOaipayCategories([])
        message.warning('OAIPay 分组返回为空，请检查远端分类接口')
      }
    } catch (e: any) {
      message.error('请求 OAIPay 分组失败: ' + (e?.message || String(e)))
    } finally {
      setOaipayCategoryLoading(false)
    }
  }
  const [activeTasksPanelOpen, setActiveTasksPanelOpen] = useState(false)
  const [registerLoading, setRegisterLoading] = useState(false)
  const [registerSettingsSaving, setRegisterSettingsSaving] = useState(false)
  const [registerControlConfig, setRegisterControlConfig] = useState<ChatGPTRegisterControlConfig>({})
  const [backfillLoading, setBackfillLoading] = useState<'' | 'cliproxyapi_pending' | 'cliproxyapi_selected' | 'sub2api_pending' | 'sub2api_selected'>('')
  const [batchResumeAuthLoading, setBatchResumeAuthLoading] = useState<'' | 'selected' | 'filtered' | 'selected_phone' | 'filtered_phone'>('')
  const [batchPaymentLinkLoading, setBatchPaymentLinkLoading] = useState(false)
  const [pixLinkScanOpen, setPixLinkScanOpen] = useState(false)
  const [pixLinkScanLoading, setPixLinkScanLoading] = useState(false)
  const [pixLinkScanReport, setPixLinkScanReport] = useState<PixLinkScanReport | null>(null)
  const [pixLinkScanError, setPixLinkScanError] = useState('')
  const [pixLinkCleanupLoading, setPixLinkCleanupLoading] = useState(false)
  const [pixLinkCleanupMode, setPixLinkCleanupMode] = useState<PixLinkCleanupMode | null>(null)
  const [pixLinkCleanupType, setPixLinkCleanupType] = useState<PaymentLinkCleanupType | null>(null)
  const [batchInvalidRecheckLoading, setBatchInvalidRecheckLoading] = useState(false)
  const [paymentEligibilityLoading, setPaymentEligibilityLoading] = useState(false)
  const [webSessionLoginLoading, setWebSessionLoginLoading] = useState(false)
  const [visibleColumnKeys, setVisibleColumnKeys] = useState<AccountColumnKey[]>(() => loadVisibleAccountColumnKeys())
  const [pinnedToolbarActionIds, setPinnedToolbarActionIds] = useState<AccountToolbarActionId[]>(() => loadPinnedToolbarActions())
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
  const [batchGopayTaskId, setBatchGopayTaskId] = useState('')
  const [batchGopayStopMode, setBatchGopayStopMode] = useState('')
  const [batchGopayRoundInterval, setBatchGopayRoundInterval] = useState(60)
  const [batchGopayOtpAutoResendDelay, setBatchGopayOtpAutoResendDelay] = useState(DEFAULT_GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS)
  const [batchGopayOtpDelaySaving, setBatchGopayOtpDelaySaving] = useState(false)
  const [batchGopayNextRoundAt, setBatchGopayNextRoundAt] = useState<number | null>(null)
  const [accessTokenCopiedAccountIds, setAccessTokenCopiedAccountIds] = useState<Set<number>>(() => new Set())
  const [copiedPaymentLinkUrlsByAccountId, setCopiedPaymentLinkUrlsByAccountId] = useState<Map<number, string>>(() => new Map())
  const [codexUsageRefreshingIds, setCodexUsageRefreshingIds] = useState<Set<number>>(() => new Set())
  const activeFilterPreset = useMemo(
    () => filterPresets.find((item) => item.id === activeFilterPresetId) || null,
    [activeFilterPresetId, filterPresets],
  )
  const activeFixedGroup = useMemo(
    () => fixedGroups.find((item) => item.id === activeFixedGroupId) || null,
    [activeFixedGroupId, fixedGroups],
  )
  const currentParentFixedGroups = useMemo(
    () => fixedGroups.filter((item) => item.parent_preset_id === activeFilterPresetId),
    [activeFilterPresetId, fixedGroups],
  )
  const accountsQuery = useAccountsQuery({
    primaryPresetId: activeFilterPresetId,
    secondaryScope: activeFilterPresetId ? secondaryFilterScope : '',
    fixedGroupId: secondaryFilterScope === 'fixed' ? activeFixedGroupId : '',
    fixedGroupRevision: secondaryFilterScope === 'fixed' ? activeFixedGroup?.revision : undefined,
    email: debouncedSearch,
    status: filterStatus,
    manuallyUsed: columnFilters.manuallyUsed.join(','),
    authType: columnFilters.authType.join(','),
    phoneBindingState: columnFilters.phoneBindingState.join(','),
    paymentLinkPlatform: columnFilters.paymentLinkPlatform.join(','),
    paymentLinkGenerated: columnFilters.paymentLinkGenerated.join(','),
    subscriptionType: columnFilters.subscriptionType.join(','),
    accountValidity: columnFilters.accountValidity.join(','),
    sub2apiState: columnFilters.sub2apiState.join(','),
    oaipayState: columnFilters.oaipayState.join(','),
    zeroAmountEligibilityState: columnFilters.zeroAmountEligibilityState.join(','),
    gcashPaymentMethodState: columnFilters.gcashPaymentMethodState.join(','),
    submitState: columnFilters.submitState.join(','),
    hasSubmitted: columnFilters.hasSubmitted.join(','),
    sortBy: subscriptionExpirySortOrder
      ? `${SUBSCRIPTION_EXPIRY_SORT_FIELD},${ACCOUNT_CREATED_AT_SORT_FIELD}`
      : ACCOUNT_CREATED_AT_SORT_FIELD,
    sortOrder: subscriptionExpirySortOrder
      ? `${subscriptionExpirySortOrder},${registrationSortOrder}`
      : registrationSortOrder,
    page: currentPage,
    pageSize: accountsPageSize,
  })
  const currentAccountFilterBody = useMemo<AccountFilterRequestBody>(() => ({
    email: debouncedSearch.trim(),
    status: filterStatus,
    manually_used: columnFilters.manuallyUsed.join(','),
    auth_type: columnFilters.authType.join(','),
    phone_binding_state: columnFilters.phoneBindingState.join(','),
    payment_link_platform: columnFilters.paymentLinkPlatform.join(','),
    payment_link_generated: columnFilters.paymentLinkGenerated.join(','),
    subscription_type: columnFilters.subscriptionType.join(','),
    account_validity: columnFilters.accountValidity.join(','),
    sub2api_state: columnFilters.sub2apiState.join(','),
    oaipay_state: columnFilters.oaipayState.join(','),
    zero_amount_eligibility_state: columnFilters.zeroAmountEligibilityState.join(','),
    gcash_payment_method_state: columnFilters.gcashPaymentMethodState.join(','),
    submit_state: columnFilters.submitState.join(','),
    has_submitted: columnFilters.hasSubmitted.join(','),
  }), [
    debouncedSearch,
    filterStatus,
    columnFilters.manuallyUsed,
    columnFilters.authType,
    columnFilters.phoneBindingState,
    columnFilters.paymentLinkPlatform,
    columnFilters.paymentLinkGenerated,
    columnFilters.subscriptionType,
    columnFilters.accountValidity,
    columnFilters.sub2apiState,
    columnFilters.oaipayState,
    columnFilters.zeroAmountEligibilityState,
    columnFilters.gcashPaymentMethodState,
    columnFilters.submitState,
    columnFilters.hasSubmitted,
  ])
  const currentFilteredTotalValue = Number(accountsQuery.data?.total ?? 0)
  const currentFilteredTotal = Number.isFinite(currentFilteredTotalValue)
    ? Math.max(0, currentFilteredTotalValue)
    : 0
  const currentFilterScopeReady = Boolean(accountsQuery.data)
    && !accountsQuery.isFetching
    && !accountsQuery.isPlaceholderData
    && !accountsQuery.isError
    && search.trim() === debouncedSearch
  const refetchAccounts = accountsQuery.refetch
  const accountDetailQuery = useAccountDetailQuery(detailAccount?.id ? Number(detailAccount.id) : null, detailModalOpen)
  const activeTasksQuery = useActiveTasksQuery(activeTasksPanelOpen)
  const refetchActiveTasks = activeTasksQuery.refetch
  const activeTasks = activeTasksQuery.data ?? EMPTY_LIST
  const activeTasksLoading = activeTasksQuery.isLoading || activeTasksQuery.isFetching
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
    () => buildAccountFilterPresetFilters(search, columnFilters, subscriptionExpirySortOrder, registrationSortOrder),
    [search, columnFilters, subscriptionExpirySortOrder, registrationSortOrder],
  )
  const activeFilterPresetDirty = useMemo(() => {
    if (!activeFilterPreset) return false
    if (secondaryFilterScope === 'fixed') return false
    return accountFilterPresetSignature(activeFilterPreset.filters) !== accountFilterPresetSignature(currentFilterPresetFilters)
  }, [activeFilterPreset, currentFilterPresetFilters, secondaryFilterScope])
  const pinnedFilterPresets = useMemo(() => {
    return filterPresets.filter((item) => item.pinned)
  }, [filterPresets])
  const pinnedFixedGroups = useMemo(() => {
    return currentParentFixedGroups.filter((item) => item.pinned)
  }, [currentParentFixedGroups])

  const loadFilterPresets = useCallback(async (silent = false) => {
    setFilterPresetLoading(true)
    try {
      const data = await apiFetch('/accounts/filter-presets')
      const dynamicItems = normalizeAccountFilterPresetItems(data?.dynamic_items ?? data?.items)
        .filter((item) => item.mode === 'dynamic')
      const nextFixedGroups = normalizeAccountFilterPresetItems(data?.fixed_groups)
        .filter((item) => item.mode === 'fixed' && item.parent_preset_id)
      setFilterPresets(dynamicItems)
      setFixedGroups(nextFixedGroups)
      setLegacyFixedPresets(normalizeAccountFilterPresetItems(data?.legacy_fixed_items))
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
    if (!activeFilterPresetId) return
    if (filterPresets.some((item) => item.id === activeFilterPresetId)) return
    setActiveFilterPresetId('')
    setActiveFixedGroupId('')
    setSecondaryFilterScope('unassigned')
    setSelectedRowKeys([])
    setSelectedAccountSnapshots({})
  }, [activeFilterPresetId, filterPresets])

  useEffect(() => {
    if (secondaryFilterScope !== 'fixed' || !activeFixedGroupId) return
    if (fixedGroups.some((item) => item.id === activeFixedGroupId && item.parent_preset_id === activeFilterPresetId)) return
    setActiveFixedGroupId('')
    setSecondaryFilterScope('unassigned')
    setSelectedRowKeys([])
    setSelectedAccountSnapshots({})
  }, [activeFilterPresetId, activeFixedGroupId, fixedGroups, secondaryFilterScope])

  useEffect(() => {
    setCurrentPage(1)
  }, [activeFilterPresetId, activeFixedGroupId, secondaryFilterScope, debouncedSearch, filterStatus, columnFilters.manuallyUsed, columnFilters.authType, columnFilters.phoneBindingState, columnFilters.paymentLinkPlatform, columnFilters.paymentLinkGenerated, columnFilters.subscriptionType, columnFilters.accountValidity, columnFilters.sub2apiState, columnFilters.oaipayState, columnFilters.zeroAmountEligibilityState, columnFilters.gcashPaymentMethodState, columnFilters.submitState, columnFilters.hasSubmitted, subscriptionExpirySortOrder, registrationSortOrder])

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
    if (!options.force && configCacheRef.current) return configCacheRef.current
    const cfg = await apiFetch('/config')
    configCacheRef.current = cfg
    setConfigCache(cfg)
    return cfg
  }, [])

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
      const data = await apiFetch('/baxigpt-cdk-pool?for_submit=true')
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

  const applyAccountTaskScopeToBody = useCallback((
    body: Record<string, unknown>,
    options: {
      scope: AccountTaskScope
      selectedIds?: Iterable<unknown>
      emptySelectedMessage?: string
      filteredMarker?: FilteredScopeMarker
    },
  ): number | null => {
    for (const key of ACCOUNT_FILTER_REQUEST_KEYS) delete body[key]
    delete body.account_ids
    delete body.all_filtered
    delete body.pending_only
    delete body.expected_total
    delete body.primary_preset_id
    delete body.secondary_scope
    delete body.fixed_group_id
    delete body.fixed_group_revision

    if (options.scope === 'selected') {
      const accountIds = normalizeAccountIds(options.selectedIds ?? selectedRowKeys)
      if (accountIds.length === 0) {
        appMessage.warning(options.emptySelectedMessage || '请先选择要处理的账号')
        return null
      }
      body.account_ids = accountIds
      return accountIds.length
    }

    if (!currentFilterScopeReady) {
      appMessage.warning('账号列表正在更新，请等待当前筛选数量刷新后再启动任务')
      return null
    }

    Object.assign(body, currentAccountFilterBody)
    if (activeFilterPreset) {
      body.primary_preset_id = activeFilterPreset.id
      body.secondary_scope = secondaryFilterScope
      if (secondaryFilterScope === 'fixed' && activeFixedGroup) {
        body.fixed_group_id = activeFixedGroup.id
        body.fixed_group_revision = activeFixedGroup.revision || 1
      }
    }
    body[options.filteredMarker || 'all_filtered'] = true
    body.expected_total = currentFilteredTotal
    return currentFilteredTotal
  }, [activeFilterPreset, activeFixedGroup, appMessage, currentAccountFilterBody, currentFilterScopeReady, currentFilteredTotal, secondaryFilterScope, selectedRowKeys])

  const postAccountScopeRequest = useCallback(async (
    path: string,
    body: Record<string, unknown>,
    toastKey: string,
  ) => {
    try {
      return await apiFetch(path, {
        method: 'POST',
        body: JSON.stringify(body),
      })
    } catch (error) {
      const scopeChangedMessage = getFilterScopeChangedMessage(error)
      if (!scopeChangedMessage) throw error
      appMessage.error({ content: scopeChangedMessage, key: toastKey, duration: 6 })
      void refetchAccounts()
      return null
    }
  }, [appMessage, refetchAccounts])

  const buildPaypalFilteredEligibleParams = useCallback(() => {
    const body: Record<string, unknown> = { ...currentAccountFilterBody }
    applyPaypalBindingEligibilityFilters(body)
    const params = new URLSearchParams({
      platform: 'chatgpt',
      page: '1',
      page_size: '1',
      detail: 'false',
    })
    if (activeFilterPreset) {
      params.set('primary_preset_id', activeFilterPreset.id)
      params.set('secondary_scope', secondaryFilterScope)
    }
    if (secondaryFilterScope === 'fixed' && activeFixedGroup) {
      params.set('fixed_group_id', activeFixedGroup.id)
      params.set('fixed_group_revision', String(activeFixedGroup.revision || 1))
    }
    if (body.email) params.set('email', String(body.email))
    if (body.status) params.set('status', String(body.status))
    if (body.manually_used) params.set('manually_used', String(body.manually_used))
    if (body.auth_type) params.set('auth_type', String(body.auth_type))
    if (body.phone_binding_state) params.set('phone_binding_state', String(body.phone_binding_state))
    if (body.payment_link_platform) params.set('payment_link_platform', String(body.payment_link_platform))
    if (body.payment_link_generated) params.set('payment_link_generated', String(body.payment_link_generated))
    if (body.subscription_type) params.set('subscription_type', String(body.subscription_type))
    if (body.account_validity) params.set('account_validity', String(body.account_validity))
    if (body.sub2api_state) params.set('sub2api_state', String(body.sub2api_state))
    if (body.oaipay_state) params.set('oaipay_state', String(body.oaipay_state))
    if (body.submit_state) params.set('submit_state', String(body.submit_state))
    if (body.has_submitted) params.set('has_submitted', String(body.has_submitted))
    return params
  }, [activeFilterPreset, activeFixedGroup, currentAccountFilterBody, secondaryFilterScope])

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
    const nextPageSize = normalizeAccountsPageSize(pageSize) ?? DEFAULT_ACCOUNTS_PAGE_SIZE
    setCustomAccountsPageSizeOptions((current) => {
      const next = normalizeCustomAccountsPageSizeOptions([...current, nextPageSize])
      if (next.length === current.length && next.every((item, index) => item === current[index])) {
        return current
      }
      saveCustomAccountsPageSizeOptions(next)
      return next
    })
    setAccountsPageSize(nextPageSize)
    setCurrentPage(1)
  }, [])

  const handleDefaultAccountsPageSizeChange = useCallback((pageSize: number) => {
    const nextPageSize = normalizeAccountsPageSize(pageSize) ?? DEFAULT_ACCOUNTS_PAGE_SIZE
    setDefaultAccountsPageSize(nextPageSize)
    saveDefaultAccountsPageSize(nextPageSize)
    message.success(`默认每页显示已设为 ${nextPageSize} 条`)
  }, [])

  const removeCustomAccountsPageSizeOption = useCallback((pageSize: number) => {
    const normalized = normalizeAccountsPageSize(pageSize)
    if (normalized === null || ACCOUNT_PAGE_SIZE_OPTIONS.includes(normalized)) return
    setCustomAccountsPageSizeOptions((current) => {
      const next = current.filter((item) => item !== normalized)
      saveCustomAccountsPageSizeOptions(next)
      return next
    })
    if (defaultAccountsPageSize === normalized) {
      setDefaultAccountsPageSize(DEFAULT_ACCOUNTS_PAGE_SIZE)
      saveDefaultAccountsPageSize(DEFAULT_ACCOUNTS_PAGE_SIZE)
      message.info(`已删除默认选项，默认每页显示恢复为 ${DEFAULT_ACCOUNTS_PAGE_SIZE} 条`)
    }
    if (accountsPageSize === normalized) {
      handleAccountsPageSizeChange(DEFAULT_ACCOUNTS_PAGE_SIZE)
    }
  }, [accountsPageSize, defaultAccountsPageSize, handleAccountsPageSizeChange])

  const applyFilterPreset = useCallback((preset: AccountFilterPreset, options?: { silent?: boolean }) => {
    if (preset.mode !== 'dynamic') return
    const filters = normalizeAccountFilterPresetFilters(preset.filters)
    setSearch(filters.search)
    setDebouncedSearch(filters.search)
    setColumnFilters(filters.columnFilters)
    setFilterStatus(filters.status.join(','))
    setSubscriptionExpirySortOrder(filters.sortOrder)
    setRegistrationSortOrder(filters.registrationSortOrder)
    setCurrentPage(1)
    setSelectedRowKeys([])
    setSelectedAccountSnapshots({})
    setSecondaryFilterScope('unassigned')
    setActiveFixedGroupId('')
    setActiveFilterPresetId(preset.id)
    if (!options?.silent) {
      message.success(`已应用筛选组合：${preset.name}`)
    }
  }, [])

  const applyUnassignedScope = useCallback((options?: { silent?: boolean }) => {
    if (!activeFilterPreset) return
    applyFilterPreset(activeFilterPreset, { silent: true })
    if (!options?.silent) message.success(`已切换固定账号组合：未固定`)
  }, [activeFilterPreset, applyFilterPreset])

  const applyFixedGroup = useCallback((group: AccountFilterPreset, options?: { silent?: boolean }) => {
    const parentPreset = group.parent_preset_id === activeFilterPreset?.id
      ? activeFilterPreset
      : filterPresets.find((item) => item.id === group.parent_preset_id) || null
    if (!parentPreset) {
      message.error('固定账号组合不属于当前条件筛选组合')
      return
    }
    setSearch('')
    setDebouncedSearch('')
    setColumnFilters(EMPTY_ACCOUNT_FILTERS)
    setFilterStatus('')
    setSubscriptionExpirySortOrder('')
    setRegistrationSortOrder(DEFAULT_REGISTRATION_SORT_ORDER)
    setCurrentPage(1)
    setSelectedRowKeys([])
    setSelectedAccountSnapshots({})
    setActiveFilterPresetId(parentPreset.id)
    setSecondaryFilterScope('fixed')
    setActiveFixedGroupId(group.id)
    if (!options?.silent) message.success(`已切换固定账号组合：${group.name}`)
  }, [activeFilterPreset, filterPresets])

  const clearFilterPreset = useCallback((options?: { silent?: boolean }) => {
    setSearch('')
    setDebouncedSearch('')
    setColumnFilters(EMPTY_ACCOUNT_FILTERS)
    setFilterStatus('')
    setSubscriptionExpirySortOrder('')
    setRegistrationSortOrder(DEFAULT_REGISTRATION_SORT_ORDER)
    setCurrentPage(1)
    setSelectedRowKeys([])
    setSelectedAccountSnapshots({})
    setSecondaryFilterScope('unassigned')
    setActiveFixedGroupId('')
    setActiveFilterPresetId('')
    if (!options?.silent) {
      message.success('已清除筛选组合与账号选择')
    }
  }, [])

  const fillFilterFormFields = useCallback((filters?: AccountFilterPresetFilters) => {
    const normalized = normalizeAccountFilterPresetFilters(filters)
    filterPresetForm.setFieldsValue({
      search: normalized.search,
      status: normalized.status,
      authType: normalized.columnFilters.authType,
      phoneBindingState: normalized.columnFilters.phoneBindingState,
      paymentLinkPlatform: normalized.columnFilters.paymentLinkPlatform,
      paymentLinkGenerated: normalized.columnFilters.paymentLinkGenerated,
      subscriptionType: normalized.columnFilters.subscriptionType,
      accountValidity: normalized.columnFilters.accountValidity,
      sub2apiState: normalized.columnFilters.sub2apiState,
      oaipayState: normalized.columnFilters.oaipayState,
      zeroAmountEligibilityState: normalized.columnFilters.zeroAmountEligibilityState,
      gcashPaymentMethodState: normalized.columnFilters.gcashPaymentMethodState,
      submitState: normalized.columnFilters.submitState,
      hasSubmitted: normalized.columnFilters.hasSubmitted,
      sortOrder: normalized.sortOrder || undefined,
      registrationSortOrder: normalized.registrationSortOrder,
    })
  }, [filterPresetForm])

  const openCreateCurrentFilterPreset = useCallback(() => {
    setFilterPresetEditing(null)
    setFilterPresetEditorMode('create-current')
    setFilterPresetEditorAccountIds([])
    filterPresetForm.setFieldsValue({
      name: '',
      description: buildAccountFilterPresetSummary(currentFilterPresetFilters),
      mode: 'dynamic',
      parent_preset_id: '',
      pinned: true,
    })
    fillFilterFormFields(currentFilterPresetFilters)
    setFilterPresetEditorOpen(true)
  }, [currentFilterPresetFilters, fillFilterFormFields, filterPresetForm])

  const openCreateFixedGroup = useCallback(() => {
    if (!activeFilterPreset) {
      message.warning('请先选择一级条件筛选组合')
      return
    }
    if (secondaryFilterScope !== 'unassigned') {
      message.warning('请先切换到“未固定”后选择账号')
      return
    }
    const selectedIds = normalizeAccountIds(selectedRowKeys)
    if (selectedIds.length === 0) {
      message.warning('请先选择要加入固定账号组合的账号')
      return
    }
    setFilterPresetEditing(null)
    setFilterPresetEditorMode('create-current')
    setFilterPresetEditorAccountIds(selectedIds)
    filterPresetForm.setFieldsValue({
      name: '',
      description: '',
      mode: 'fixed',
      parent_preset_id: activeFilterPreset.id,
      pinned: true,
    })
    fillFilterFormFields(undefined)
    setFilterPresetEditorOpen(true)
  }, [activeFilterPreset, fillFilterFormFields, filterPresetForm, secondaryFilterScope, selectedRowKeys])

  const openCopyFilterPreset = useCallback((preset: AccountFilterPreset) => {
    if (preset.mode !== 'dynamic') return
    setFilterPresetEditing(preset)
    setFilterPresetEditorMode('copy-preset')
    setFilterPresetEditorAccountIds([])
    filterPresetForm.setFieldsValue({
      name: `${preset.name} 副本`,
      description: preset.description || buildAccountFilterPresetSummary(preset.filters),
      mode: 'dynamic',
      parent_preset_id: '',
      pinned: true,
    })
    fillFilterFormFields(preset.filters)
    setFilterPresetEditorOpen(true)
  }, [fillFilterFormFields, filterPresetForm])

  const openEditFilterPresetMeta = useCallback((preset: AccountFilterPreset) => {
    setFilterPresetEditing(preset)
    setFilterPresetEditorMode('edit-meta')
    setFilterPresetEditorAccountIds(preset.account_ids)
    filterPresetForm.setFieldsValue({
      name: preset.name,
      description: preset.description || '',
      mode: preset.mode,
      parent_preset_id: preset.parent_preset_id || '',
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
    const mode = values.mode === 'fixed' ? 'fixed' : 'dynamic'
    const accountIds = mode === 'fixed' ? normalizeAccountIds(filterPresetEditorAccountIds) : []
    if (mode === 'fixed' && accountIds.length === 0) {
      message.warning('固定账号组合至少需要一个已选账号')
      return
    }
    if (mode === 'fixed' && accountIds.length > MAX_FIXED_FILTER_PRESET_ACCOUNT_IDS) {
      message.warning(`固定账号组合最多保存 ${MAX_FIXED_FILTER_PRESET_ACCOUNT_IDS} 个账号`)
      return
    }
    const filters = normalizeAccountFilterPresetFilters({
      search: values.search,
      status: values.status,
      columnFilters: {
        authType: values.authType,
        phoneBindingState: values.phoneBindingState,
        paymentLinkPlatform: values.paymentLinkPlatform,
        paymentLinkGenerated: values.paymentLinkGenerated,
        subscriptionType: values.subscriptionType,
        accountValidity: values.accountValidity,
        sub2apiState: values.sub2apiState,
        oaipayState: values.oaipayState,
        submitState: values.submitState,
        hasSubmitted: values.hasSubmitted,
      },
      sortOrder: values.sortOrder,
      registrationSortOrder: values.registrationSortOrder,
    })
    const body = {
      name,
      description: String(values.description || '').trim(),
      pinned: Boolean(values.pinned),
      mode,
      filters,
      account_ids: accountIds,
      parent_preset_id: mode === 'fixed'
        ? String(values.parent_preset_id || editingPreset?.parent_preset_id || activeFilterPreset?.id || '')
        : '',
    }
    setFilterPresetSaving(true)
    try {
      const endpoint = isEditMeta ? `/accounts/filter-presets/${editingPreset!.id}` : '/accounts/filter-presets'
      const data = await apiFetch(endpoint, {
        method: isEditMeta ? 'PUT' : 'POST',
        body: JSON.stringify(body),
      })
      setFilterPresets(normalizeAccountFilterPresetItems(data?.dynamic_items).filter((item) => item.mode === 'dynamic'))
      setFixedGroups(normalizeAccountFilterPresetItems(data?.fixed_groups).filter((item) => item.mode === 'fixed'))
      setLegacyFixedPresets(normalizeAccountFilterPresetItems(data?.legacy_fixed_items))
      const saved = normalizeAccountFilterPresetItem(data?.item)
      if (saved?.mode === 'dynamic' && (!isEditMeta || activeFilterPresetId === saved.id)) {
        applyFilterPreset(saved, { silent: true })
      } else if (saved?.mode === 'fixed' && saved.parent_preset_id === activeFilterPreset?.id) {
        applyFixedGroup(saved, { silent: true })
      }
      setFilterPresetEditorOpen(false)
      const discardedCount = normalizeAccountIds(data?.discarded_account_ids || []).length
      const discardedCopy = discardedCount > 0 ? `，已忽略 ${discardedCount} 个不存在的账号` : ''
      message.success(`${isEditMeta ? '组合已更新' : mode === 'fixed' ? '固定账号组合已创建' : '条件筛选组合已保存'}${discardedCopy}`)
    } catch (e: any) {
      message.error(e?.message || '保存筛选组合失败')
    } finally {
      setFilterPresetSaving(false)
    }
  }, [activeFilterPreset, activeFilterPresetId, applyFilterPreset, applyFixedGroup, filterPresetEditing, filterPresetEditorAccountIds, filterPresetEditorMode, filterPresetForm])

  const overwritePresetWithCurrent = useCallback(async (targetPreset?: AccountFilterPreset | null) => {
    const target = targetPreset || activeFilterPreset
    if (!target) return
    const fixedAccountIds = target.mode === 'fixed' ? normalizeAccountIds(selectedRowKeys) : []
    if (target.mode === 'fixed' && fixedAccountIds.length === 0) {
      message.warning('请先选择要覆盖保存到该组合的账号')
      return
    }
    if (target.mode === 'fixed' && fixedAccountIds.length > MAX_FIXED_FILTER_PRESET_ACCOUNT_IDS) {
      message.warning(`固定账号组合最多保存 ${MAX_FIXED_FILTER_PRESET_ACCOUNT_IDS} 个账号`)
      return
    }
    setFilterPresetSaving(true)
    try {
      const data = await apiFetch(`/accounts/filter-presets/${target.id}`, {
        method: 'PUT',
        body: JSON.stringify({
          name: target.name,
          description: target.description || '',
          pinned: Boolean(target.pinned),
          mode: target.mode,
          parent_preset_id: target.parent_preset_id || '',
          filters: normalizeAccountFilterPresetFilters(currentFilterPresetFilters),
          account_ids: fixedAccountIds,
        }),
      })
      setFilterPresets(normalizeAccountFilterPresetItems(data?.dynamic_items).filter((item) => item.mode === 'dynamic'))
      setFixedGroups(normalizeAccountFilterPresetItems(data?.fixed_groups).filter((item) => item.mode === 'fixed'))
      setLegacyFixedPresets(normalizeAccountFilterPresetItems(data?.legacy_fixed_items))
      const saved = normalizeAccountFilterPresetItem(data?.item)
      if (saved?.mode === 'dynamic' && activeFilterPresetId === saved.id) {
        applyFilterPreset(saved, { silent: true })
      } else if (saved?.mode === 'fixed' && activeFixedGroupId === saved.id) {
        applyFixedGroup(saved, { silent: true })
      }
      const discardedCount = normalizeAccountIds(data?.discarded_account_ids || []).length
      const discardedCopy = discardedCount > 0 ? `，忽略 ${discardedCount} 个不存在的账号` : ''
      message.success(`已用当前${target.mode === 'fixed' ? '所选账号' : '筛选条件'}覆盖组合「${target.name}」${discardedCopy}`)
    } catch (e: any) {
      message.error(e?.message || '覆盖筛选组合失败')
    } finally {
      setFilterPresetSaving(false)
    }
  }, [activeFilterPreset, activeFilterPresetId, activeFixedGroupId, applyFilterPreset, applyFixedGroup, currentFilterPresetFilters, selectedRowKeys])

  const overwriteActiveFilterPreset = useCallback(() => overwritePresetWithCurrent(activeFilterPreset), [activeFilterPreset, overwritePresetWithCurrent])

  const deleteFilterPreset = useCallback(async (preset: AccountFilterPreset) => {
    setFilterPresetSaving(true)
    try {
      const data = await apiFetch(`/accounts/filter-presets/${preset.id}`, { method: 'DELETE' })
      setFilterPresets(normalizeAccountFilterPresetItems(data?.dynamic_items).filter((item) => item.mode === 'dynamic'))
      setFixedGroups(normalizeAccountFilterPresetItems(data?.fixed_groups).filter((item) => item.mode === 'fixed'))
      setLegacyFixedPresets(normalizeAccountFilterPresetItems(data?.legacy_fixed_items))
      if (activeFilterPresetId === preset.id) clearFilterPreset({ silent: true })
      if (activeFixedGroupId === preset.id) applyUnassignedScope({ silent: true })
      message.success('筛选组合已删除')
    } catch (e: any) {
      message.error(e?.message || '删除筛选组合失败')
    } finally {
      setFilterPresetSaving(false)
    }
  }, [activeFilterPresetId, activeFixedGroupId, applyUnassignedScope, clearFilterPreset])

  const openFixedMigration = useCallback(() => {
    setFilterPresetManageOpen(false)
    setFixedMigrationParentById({})
    setFixedMigrationPriority(legacyFixedPresets.map((preset) => preset.id))
    setFixedMigrationPreview(null)
    setFixedMigrationOpen(true)
  }, [legacyFixedPresets])

  const moveFixedMigrationPriority = useCallback((presetId: string, offset: -1 | 1) => {
    setFixedMigrationPriority((current) => {
      const index = current.indexOf(presetId)
      const nextIndex = index + offset
      if (index < 0 || nextIndex < 0 || nextIndex >= current.length) return current
      const next = [...current]
      ;[next[index], next[nextIndex]] = [next[nextIndex], next[index]]
      return next
    })
    setFixedMigrationPreview(null)
  }, [])

  const runFixedMigration = useCallback(async (commit: boolean) => {
    const missingParent = legacyFixedPresets.find((preset) => !fixedMigrationParentById[preset.id])
    if (missingParent) {
      message.warning(`请为“${missingParent.name}”选择一级条件组合`)
      return
    }
    setFixedMigrationLoading(true)
    try {
      const data = await apiFetch('/accounts/filter-presets/migrate-fixed', {
        method: 'POST',
        body: JSON.stringify({
          parent_by_preset_id: fixedMigrationParentById,
          priority_order: fixedMigrationPriority,
          commit,
        }),
      })
      setFixedMigrationPreview(data?.preview || null)
      if (!commit) {
        message.success('迁移预览已生成')
        return
      }
      setFilterPresets(normalizeAccountFilterPresetItems(data?.dynamic_items).filter((item) => item.mode === 'dynamic'))
      setFixedGroups(normalizeAccountFilterPresetItems(data?.fixed_groups).filter((item) => item.mode === 'fixed'))
      setLegacyFixedPresets(normalizeAccountFilterPresetItems(data?.legacy_fixed_items))
      setFixedMigrationOpen(false)
      setSelectedRowKeys([])
      setSelectedAccountSnapshots({})
      message.success('旧固定组合迁移完成')
    } catch (e: any) {
      message.error(e?.message || '旧固定组合迁移失败')
    } finally {
      setFixedMigrationLoading(false)
    }
  }, [fixedMigrationParentById, fixedMigrationPriority, legacyFixedPresets])

  useEffect(() => {
    const ids = new Set<number>()
    accounts.forEach((record) => {
      const accountId = Number(record?.id || 0)
      if (!Number.isFinite(accountId) || accountId <= 0) return
      if (isBaxiGptPendingOrder(record) && !isBaxiGptWatchTerminal(record)) {
        ids.add(accountId)
      }
    })
    const taskStatus = taskSnapshot?.status || taskSnapshot?.status_snapshot
    if (isActiveTaskStatus(taskStatus)) {
      collectBaxiPollingAccountIdsFromTaskSnapshot(taskSnapshot).forEach((id) => ids.add(id))
    }
    activeTasks.forEach((task: any) => {
      if (isActiveTaskStatus(task?.status || task?.status_snapshot)) {
        collectBaxiPollingAccountIdsFromTaskSnapshot(task).forEach((id) => ids.add(id))
      }
    })
    setWatchingBaxiAccountIds((prev) => {
      if (prev.size === ids.size && Array.from(ids).every((id) => prev.has(id))) return prev
      return ids
    })
  }, [accounts, taskSnapshot, activeTasks])

  useEffect(() => {
    if (!pageVisible || !watchingBaxiAccountIdsKey) return
    const controller = new AbortController()
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
          signal: controller.signal,
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
        if (controller.signal.aborted) return
        // 后端 snapshot 接口可能还没上线，或者字段临时缺失；账号页不要因此中断。
      } finally {
        if (!cancelled && !controller.signal.aborted) {
          timer = window.setTimeout(pull, 4000)
        }
      }
    }
    void pull()
    return () => {
      cancelled = true
      controller.abort()
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

  const copyPaymentLink = useCallback(async (record: { id?: unknown }, url: string) => {
    const normalizedUrl = String(url || '').trim()
    if (!normalizedUrl) return
    const ok = await copyText(normalizedUrl)
    if (!ok) return
    const accountId = Number(record?.id || 0)
    if (!accountId) return
    setCopiedPaymentLinkUrlsByAccountId((prev) => {
      if (prev.get(accountId) === normalizedUrl) return prev
      const next = new Map(prev)
      next.set(accountId, normalizedUrl)
      return next
    })
  }, [])

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
    await refetchActiveTasks()
  }, [refetchActiveTasks])

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
    if (!registerModalOpen) return
    let cancelled = false
    loadConfigCache({ force: true })
      .then((cfg) => {
        if (cancelled) return
        setRegisterControlConfig(cfg)
        const provider = String(cfg?.mail_provider || 'luckmail').trim() || 'luckmail'
        const savedSettings = loadRegisterFormSettings(currentPlatform)
        const hasSavedMailProfile = parseBooleanConfigValue(savedSettings.register_mail_profile_saved)
        const savedProviderOverride = hasSavedMailProfile
          ? normalizeRegisterMailProviderOverride(savedSettings.mail_provider_override)
          : '__global__'
        const proxySettings = taskProxySettingsFromConfig(cfg)
        const savedEmail = window.localStorage.getItem('auto-chatgpt.manual_email_otp.email') || ''
        const configuredTempMailMode = String(cfg?.tempmail_mode || 'fixed_domain').trim().toLowerCase()
        const globalTempMailMode = normalizeRegisterTempMailMode(configuredTempMailMode)
        const globalTempMailFixedDomains = normalizeDomainList([
          ...parseStoredDomainList(cfg?.tempmail_fixed_domains),
          cfg?.tempmail_primary_domain,
        ])
        const hasSavedTempMailMode = hasSavedMailProfile && Object.prototype.hasOwnProperty.call(savedSettings, 'tempmail_mode')
        const hasSavedTempMailDomains = hasSavedMailProfile && Object.prototype.hasOwnProperty.call(savedSettings, 'tempmail_fixed_domains')
        const tempmailMode = hasSavedTempMailMode
          ? normalizeRegisterTempMailMode(savedSettings.tempmail_mode, globalTempMailMode)
          : globalTempMailMode
        const tempmailFixedDomains = hasSavedTempMailDomains
          ? normalizeDomainList(savedSettings.tempmail_fixed_domains)
          : globalTempMailFixedDomains
        const tempmailPrimaryDomain = hasSavedMailProfile && Object.prototype.hasOwnProperty.call(savedSettings, 'tempmail_primary_domain')
          ? String(savedSettings.tempmail_primary_domain || '').trim().replace(/^[@.]+/, '')
          : (tempmailFixedDomains[0] || '')
        const shouldHydrateExecutor = !registerForm.isFieldTouched('executor_type')
        const hydratedExecutor = normalizeExecutorForPlatform(
          currentPlatform,
          String(savedSettings.executor_type || cfg.default_executor || '').trim(),
        )
        const executorFieldHydration = shouldHydrateExecutor ? { executor_type: hydratedExecutor } : {}
        const effectiveExecutor = normalizeExecutorForPlatform(
          currentPlatform,
          shouldHydrateExecutor
            ? hydratedExecutor
            : registerForm.getFieldValue('executor_type'),
        )
        const delaySettings = normalizeRegisterDelaySettings(savedSettings, currentPlatform, cfg)
        const effectiveMailProvider = savedProviderOverride !== '__global__' ? savedProviderOverride : provider
        const configuredUniqueExitPolicy = normalizeRegisterUniqueExitPolicy(
          cfg.chatgpt_register_unique_exit_ip_policy,
          cfg.chatgpt_register_unique_exit_ip_enabled,
        )
        const uniqueExitPolicy = normalizeRegisterUniqueExitPolicy(
          savedSettings.chatgpt_register_unique_exit_ip_policy,
          savedSettings.chatgpt_register_unique_exit_ip_enabled,
          configuredUniqueExitPolicy,
        )
        setRegisterMailProvider(provider)
        registerForm.setFieldsValue({
          count: Number(savedSettings.count || 1) || 1,
          concurrency: normalizeRegisterConcurrency(
            savedSettings.concurrency,
            currentPlatform,
            effectiveExecutor,
            effectiveMailProvider === 'manual_email_otp',
            cfg,
          ),
          ...delaySettings,
          ...executorFieldHydration,
          ...proxySettings,
          mail_provider_override: savedProviderOverride,
          email_api_lines: String(cfg.email_api_lines || '').trim(),
          email_api_poll_interval_seconds: cfg.email_api_poll_interval_seconds || 3,
          email_api_request_timeout_seconds: cfg.email_api_request_timeout_seconds || 15,
          email_api_gmail_dot_variant_enabled: cfg.email_api_gmail_dot_variant_enabled === '' ? true : parseBooleanConfigValue(cfg.email_api_gmail_dot_variant_enabled),
          email_api_gmail_variant_count: Number(cfg.email_api_gmail_variant_count || 2) || 2,
          email_api_gmail_variant_rules: cfg.email_api_gmail_variant_rules || 'all',
          email_api_gmail_plus_tag_template: cfg.email_api_gmail_plus_tag_template || 'r{rand}',
          tempmail_mode: tempmailMode,
          tempmail_primary_domain: tempmailPrimaryDomain || tempmailFixedDomains[0] || '',
          tempmail_fixed_domains: tempmailFixedDomains,
          email: String(savedSettings.email || savedEmail || '').trim(),
          login_password: String(cfg.chatgpt_existing_account_login_password || '').trim(),
          chatgpt_existing_account_capture: savedSettings.chatgpt_existing_account_capture ?? false,
          chatgpt_save_registration_access_token_account:
            cfg.chatgpt_save_registration_access_token_account === ''
              ? true
              : cfg.chatgpt_save_registration_access_token_account === undefined
                ? true
                : parseBooleanConfigValue(cfg.chatgpt_save_registration_access_token_account),
          chatgpt_existing_account_login_route_enabled:
            cfg.chatgpt_existing_account_login_route_enabled === ''
              ? true
              : cfg.chatgpt_existing_account_login_route_enabled === undefined
                ? true
                : parseBooleanConfigValue(cfg.chatgpt_existing_account_login_route_enabled),
          chatgpt_register_unique_exit_ip_policy: uniqueExitPolicy,
          chatgpt_register_otp_wait_seconds:
            cfg.chatgpt_register_otp_wait_seconds ?? 120,
          chatgpt_register_otp_resend_wait_seconds:
            cfg.chatgpt_register_otp_resend_wait_seconds ?? 90,
          chatgpt_register_otp_account_budget_seconds:
            cfg.chatgpt_register_otp_account_budget_seconds ?? 210,
        })
      })
      .catch(() => {
        if (cancelled) return
        setRegisterControlConfig({})
        setRegisterMailProvider('luckmail')
        const savedSettings = loadRegisterFormSettings(currentPlatform)
        const hasSavedMailProfile = parseBooleanConfigValue(savedSettings.register_mail_profile_saved)
        const savedProviderOverride = hasSavedMailProfile
          ? normalizeRegisterMailProviderOverride(savedSettings.mail_provider_override)
          : '__global__'
        const savedTempMailMode = hasSavedMailProfile
          ? normalizeRegisterTempMailMode(savedSettings.tempmail_mode)
          : 'fixed_domain'
        const savedTempMailDomains = hasSavedMailProfile
          ? normalizeDomainList(savedSettings.tempmail_fixed_domains)
          : []
        const savedEmail = window.localStorage.getItem('auto-chatgpt.manual_email_otp.email') || ''
        const shouldHydrateExecutor = !registerForm.isFieldTouched('executor_type')
        const hydratedExecutor = normalizeExecutorForPlatform(
          currentPlatform,
          String(savedSettings.executor_type || 'protocol').trim(),
        )
        const executorFieldHydration = shouldHydrateExecutor ? { executor_type: hydratedExecutor } : {}
        const effectiveExecutor = normalizeExecutorForPlatform(
          currentPlatform,
          shouldHydrateExecutor
            ? hydratedExecutor
            : registerForm.getFieldValue('executor_type'),
        )
        const delaySettings = normalizeRegisterDelaySettings(savedSettings, currentPlatform)
        const effectiveMailProvider = savedProviderOverride !== '__global__' ? savedProviderOverride : 'luckmail'
        const fallbackProxySettings = taskProxySettingsFromConfig({})
        registerForm.setFieldsValue({
          count: Number(savedSettings.count || 1) || 1,
          concurrency: normalizeRegisterConcurrency(
            savedSettings.concurrency,
            currentPlatform,
            effectiveExecutor,
            effectiveMailProvider === 'manual_email_otp',
          ),
          ...delaySettings,
          ...executorFieldHydration,
          ...fallbackProxySettings,
          mail_provider_override: savedProviderOverride,
          email_api_lines: '',
          email_api_poll_interval_seconds: 3,
          email_api_request_timeout_seconds: 15,
          email_api_gmail_dot_variant_enabled: true,
          email_api_gmail_variant_count: 2,
          email_api_gmail_variant_rules: 'all',
          email_api_gmail_plus_tag_template: 'r{rand}',
          tempmail_mode: savedTempMailMode,
          tempmail_primary_domain: hasSavedMailProfile
            ? (String(savedSettings.tempmail_primary_domain || '').trim().replace(/^[@.]+/, '') || savedTempMailDomains[0] || '')
            : '',
          tempmail_fixed_domains: savedTempMailDomains,
          email: String(savedSettings.email || savedEmail || '').trim(),
          login_password: '',
          chatgpt_existing_account_capture: savedSettings.chatgpt_existing_account_capture ?? false,
          chatgpt_save_registration_access_token_account: true,
          chatgpt_existing_account_login_route_enabled: true,
          chatgpt_register_unique_exit_ip_policy: normalizeRegisterUniqueExitPolicy(
            savedSettings.chatgpt_register_unique_exit_ip_policy,
            savedSettings.chatgpt_register_unique_exit_ip_enabled,
          ),
          chatgpt_register_otp_wait_seconds: 120,
          chatgpt_register_otp_resend_wait_seconds: 90,
          chatgpt_register_otp_account_budget_seconds: 210,
        })
      })
    return () => {
      cancelled = true
    }
  }, [registerModalOpen, currentPlatform, registerForm, loadConfigCache])

  useEffect(() => {
    if (!taskId || !registerModalOpen) {
      setTaskSnapshot(null)
      return
    }
    if (!pageVisible) return

    const controller = new AbortController()
    let cancelled = false
    let timer: number | null = null

    const pull = async () => {
      try {
        const snapshot = await apiFetch(`/tasks/${taskId}`, { signal: controller.signal })
        if (cancelled) return
        setTaskSnapshot(snapshot)
        setActiveTasksPanelOpen(true)
        if (isActiveTaskStatus(snapshot?.status || snapshot?.status_snapshot)) {
          timer = window.setTimeout(pull, 1000)
        } else {
          clearTaskModalStorage()
          void refetchActiveTasks()
          void refetchAccounts()
          void loadFilterPresets(true)
        }
      } catch {
        if (cancelled || controller.signal.aborted) return
        clearTaskModalStorage()
        timer = window.setTimeout(pull, 1500)
      }
    }

    void pull()

    return () => {
      cancelled = true
      controller.abort()
      if (timer != null) {
        window.clearTimeout(timer)
      }
    }
  }, [taskId, registerModalOpen, pageVisible, loadFilterPresets, refetchActiveTasks, refetchAccounts])

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

  const exportCsv = async (
    exportMode: AccountExportMode = 'sub2api',
    exportScope: AccountExportScope = 'selected',
  ) => {
    if (currentPlatform === 'chatgpt') {
      try {
        const selectedIds = selectedRowKeys
          .map((key) => Number(key))
          .filter((id) => Number.isFinite(id) && id > 0)
        const body: Record<string, unknown> = {
          ids: selectedIds,
          mode: exportMode,
        }
        const useFilteredScope = exportMode === 'pix_payment_links'
          ? exportScope === 'filtered'
          : selectedIds.length === 0
        if (useFilteredScope) {
          body.ids = []
          const filteredCount = applyAccountTaskScopeToBody(body, {
            scope: 'filtered',
            emptySelectedMessage: '当前筛选范围没有可导出的账号',
          })
          if (filteredCount === null) return
          if (filteredCount === 0) {
            appMessage.warning('当前筛选范围没有可导出的账号')
            return
          }
        } else if (exportMode === 'pix_payment_links' && selectedIds.length === 0) {
          appMessage.warning('请先选择要导出 PIX 支付链接的账号')
          return
        }
        const res = await apiRequest('/chatgpt/export-sub2api-ticket', {
          method: 'POST',
          body: JSON.stringify(body),
        })
        if (!res.ok) {
          let detail = ''
          try {
            const data = await res.json()
            const errorDetail = data?.detail
            detail = typeof errorDetail === 'string'
              ? errorDetail
              : errorDetail && typeof errorDetail === 'object'
                ? String((errorDetail as { message?: unknown }).message || '')
                : String(data?.message || '')
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
    } finally {
      setResumeAuthAccountId(null)
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

  const getResumeAuthScope = (): 'selected' | 'filtered' => (selectedRowKeys.length > 0 ? 'selected' : 'filtered')

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
    const requestedCount = applyAccountTaskScopeToBody(body, {
      scope,
      emptySelectedMessage: '请先选择要补抓的账号',
    })
    if (requestedCount === null) return

    const loadingKey = `${scope}${allowPhoneVerification ? '_phone' : ''}` as 'selected' | 'filtered' | 'selected_phone' | 'filtered_phone'
    setBatchResumeAuthLoading(loadingKey)
    message.loading({ content: '批量补抓Auth任务创建中...', key: toastKey, duration: 0 })
    try {
      const res = await postAccountScopeRequest('/tasks/chatgpt/resume-subscription-auth/batch', body, toastKey)
      if (!res) return

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

  const buildBatchPaymentLinkParams = (forceRefresh = false): Record<string, unknown> => {
    const values = batchPaymentLinkForm.getFieldsValue()
    const rawMode = String(values?.plan || batchPaymentLinkPlan || 'plus').trim().toLowerCase()
    const mode: PaymentLinkGenerationMode = rawMode === 'team' ? 'team' : rawMode === 'short' ? 'short' : 'plus'
    if (mode === 'short') {
      return {
        plan: 'plus',
        country: normalizeCheckoutCountry(values?.short_country || DEFAULT_SHORT_PAYMENT_COUNTRY),
        currency: normalizeCheckoutCurrency(values?.short_currency || DEFAULT_SHORT_PAYMENT_CURRENCY),
        payment_source: SHORT_PAYMENT_SOURCE,
        payment_link_format: SHORT_PAYMENT_LINK_FORMAT,
        reuse_cached_link: !forceRefresh,
      }
    }
    const plan = mode === 'team' ? 'team' : 'plus'
    const params: Record<string, unknown> = {
      plan,
      reuse_cached_link: !forceRefresh,
    }
    if (plan === 'team') {
      const teamPlanData: Record<string, unknown> = {}
      const workspaceName = String(values?.workspace_name || '').trim()
      const priceInterval = String(values?.price_interval || '').trim().toLowerCase()
      const seatQuantity = values?.seat_quantity
      if (workspaceName) teamPlanData.workspace_name = workspaceName
      if (priceInterval) teamPlanData.price_interval = priceInterval
      if (seatQuantity !== undefined && seatQuantity !== null && seatQuantity !== '') {
        teamPlanData.seat_quantity = Number(seatQuantity)
      }
      if (Object.keys(teamPlanData).length > 0) params.team_plan_data = teamPlanData
      const promoCode = String(values?.promo_code || '').trim()
      const cancelUrl = String(values?.cancel_url || '').trim()
      const checkoutProxyRegion = String(values?.checkout_proxy_region || '').trim().toUpperCase()
      const checkoutUiMode = String(values?.checkout_ui_mode || DEFAULT_TEAM_CHECKOUT_UI_MODE).trim().toLowerCase()
      const billingCountry = String(values?.billing_country || DEFAULT_TEAM_BILLING_COUNTRY).trim().toUpperCase()
      if (promoCode) params.promo_code = promoCode
      if (cancelUrl) params.cancel_url = cancelUrl
      if (checkoutProxyRegion) params.checkout_proxy_region = checkoutProxyRegion
      params.checkout_ui_mode = checkoutUiMode === 'custom' ? 'custom' : DEFAULT_TEAM_CHECKOUT_UI_MODE
      params.billing_country = billingCountry
    }
    return params
  }

  const loadBatchPaymentLinkProfile = async (
    params?: Record<string, unknown>,
  ): Promise<PaymentLinkProfile | null> => {
    setBatchPaymentLinkProfileLoading(true)
    setBatchPaymentLinkProfileError('')
    try {
      const requestedPlan = String(params?.plan || 'plus').trim().toLowerCase()
      const profile = await apiFetch('/tasks/chatgpt/payment-links/profile', requestedPlan === 'team'
        ? {
            method: 'POST',
            body: JSON.stringify({ params }),
          }
        : undefined) as PaymentLinkProfile
      if (!profile || !String(profile.profile_hash || '').trim()) {
        throw new Error('long-link 未返回当前配置标识')
      }
      const billingCountryOptions = normalizeTeamBillingCountryOptions(profile.billing_country_options)
      if (billingCountryOptions.length === 0) {
        throw new Error('服务端未返回 long-link 账单国家选项')
      }
      setTeamBillingCountryOptions(billingCountryOptions)
      setBatchPaymentLinkProfile(profile)
      return profile
    } catch (e: any) {
      const detail = String(e?.message || '无法读取 long-link 管理端当前配置')
      setBatchPaymentLinkProfile(null)
      setBatchPaymentLinkProfileError(detail)
      return null
    } finally {
      setBatchPaymentLinkProfileLoading(false)
    }
  }

  const loadShortPaymentLinkProfile = async (): Promise<boolean> => {
    setBatchPaymentLinkProfileLoading(true)
    setBatchPaymentLinkProfileError('')
    setBatchPaymentLinkProfile(null)
    try {
      const profile = await apiFetch('/tasks/chatgpt/payment-links/short-profile') as PaymentLinkProfile
      const countryOptions = normalizeTeamBillingCountryOptions(profile?.billing_country_options)
      if (countryOptions.length === 0) {
        throw new Error('服务端未返回短链账单国家选项')
      }
      setTeamBillingCountryOptions(countryOptions)
      const selectedCountry = normalizeCheckoutCountry(
        batchPaymentLinkForm.getFieldValue('short_country') || profile.country || DEFAULT_SHORT_PAYMENT_COUNTRY,
      )
      const selected = countryOptions.find((item) => item.value === selectedCountry) || countryOptions[0]
      batchPaymentLinkForm.setFieldsValue({
        short_country: selected.value,
        short_currency: selected.currency,
      })
      return true
    } catch (error: unknown) {
      setBatchPaymentLinkProfileError(
        error instanceof Error && error.message ? error.message : '无法读取登录态短链配置',
      )
      return false
    } finally {
      setBatchPaymentLinkProfileLoading(false)
    }
  }

  const handleBatchPaymentLink = (options: { forceRefresh?: boolean; account?: any } = {}) => {
    const targetAccountId = Number(options.account?.id || 0)
    setBatchPaymentLinkForceRefresh(Boolean(options.forceRefresh))
    setBatchPaymentLinkTargetAccount(targetAccountId > 0 ? options.account : null)
    setBatchPaymentLinkPlan('plus')
    batchPaymentLinkForm.setFieldsValue({
      plan: 'plus',
      workspace_name: DEFAULT_TEAM_WORKSPACE_NAME,
      checkout_proxy_region: undefined,
      checkout_ui_mode: DEFAULT_TEAM_CHECKOUT_UI_MODE,
      billing_country: DEFAULT_TEAM_BILLING_COUNTRY,
      price_interval: '',
      seat_quantity: undefined,
      promo_code: '',
      cancel_url: '',
      short_country: DEFAULT_SHORT_PAYMENT_COUNTRY,
      short_currency: DEFAULT_SHORT_PAYMENT_CURRENCY,
    })
    setTeamProxyCountrySearch('')
    setBatchPaymentLinkProfile(null)
    setBatchPaymentLinkProfileError('')
    setBatchPaymentLinkConfigOpen(true)
    void loadBatchPaymentLinkProfile({ plan: 'plus' })
  }

  const loadPixLinkScan = async () => {
    setPixLinkScanOpen(true)
    setPixLinkScanLoading(true)
    setPixLinkScanError('')
    try {
      // Legacy compatibility paths remain available:
      // /tasks/chatgpt/payment-links/pix-cleanup/scan
      // /tasks/chatgpt/payment-links/pix-cleanup/preview
      // /tasks/chatgpt/payment-links/pix-cleanup/task
      // The legacy /tasks/chatgpt/payment-links/pix-cleanup/scan endpoint is
      // retained server-side; the mixed endpoint automatically groups PIX/UPI.
      const report = await apiFetch('/tasks/chatgpt/payment-links/scan') as PixLinkScanReport
      setPixLinkScanReport(report)
    } catch (error: unknown) {
      const detail = error instanceof Error && error.message
        ? error.message
        : '无法扫描当前支付链接'
      setPixLinkScanError(detail)
      appMessage.error(detail)
    } finally {
      setPixLinkScanLoading(false)
    }
  }

  const executePixLinkCleanup = async (
    cleanupMode: PixLinkCleanupMode,
    paymentType: PaymentLinkCleanupType = 'pix',
  ) => {
    const cleanupMeta = PIX_LINK_CLEANUP_META[cleanupMode]
    const paymentLabel = PAYMENT_LINK_SCAN_LABELS[paymentType]
    setPixLinkCleanupLoading(true)
    setPixLinkCleanupMode(cleanupMode)
    setPixLinkCleanupType(paymentType)
    try {
      const result = await apiFetch('/tasks/chatgpt/payment-links/cleanup/task', {
        method: 'POST',
        body: JSON.stringify({ cleanup_mode: cleanupMode, payment_type: paymentType }),
      }) as PixLinkCleanupTaskResponse
      const taskIdFromResponse = String(result?.task_id || '').trim()
      if (!taskIdFromResponse) {
        throw new Error('删除任务未返回 task_id')
      }
      const instanceId = String(result?.instance_id || '当前实例')
      const activeMode = result?.cleanup_mode || cleanupMode
      const activeMeta = PIX_LINK_CLEANUP_META[activeMode]
      setTaskModalMode('pix_cleanup')
      setTaskModalAccount(null)
      setTaskId(taskIdFromResponse)
      setTaskSnapshot({
        id: taskIdFromResponse,
        task_id: taskIdFromResponse,
        source: String(result?.source || `${paymentType}_payment_link_cleanup`),
        status: result?.already_running ? 'running' : 'pending',
        meta: {
          instance_id: instanceId,
          cleanup_mode: activeMode,
          cleanup_label: activeMeta.label,
          payment_type: paymentType,
        },
      })
      setRegisterModalOpen(true)
      setActiveTasksPanelOpen(true)
      setPixLinkScanOpen(false)
      void activeTasksQuery.refetch()
      if (result?.already_running) {
        appMessage.info(`${instanceId} 已有${activeMeta.label}链接删除任务在运行，已打开现有任务日志`)
      } else {
        appMessage.success(`${instanceId} ${paymentLabel} ${cleanupMeta.label}链接删除任务已启动`)
      }
    } catch (e: any) {
      appMessage.error(e?.message || `${paymentLabel} ${cleanupMeta.title}删除任务启动失败`)
      throw e
    } finally {
      setPixLinkCleanupLoading(false)
      setPixLinkCleanupMode(null)
      setPixLinkCleanupType(null)
    }
  }

  const handleCleanupPixLinks = async (
    cleanupMode: PixLinkCleanupMode,
    paymentType: PaymentLinkCleanupType = 'pix',
  ) => {
    const cleanupMeta = PIX_LINK_CLEANUP_META[cleanupMode]
    const paymentLabel = PAYMENT_LINK_SCAN_LABELS[paymentType]
    setPixLinkCleanupLoading(true)
    setPixLinkCleanupMode(cleanupMode)
    setPixLinkCleanupType(paymentType)
    let preview: PixLinkScanReport
    try {
      preview = await apiFetch(`/tasks/chatgpt/payment-links/cleanup/preview?cleanup_mode=${encodeURIComponent(cleanupMode)}&payment_type=${encodeURIComponent(paymentType)}`) as PixLinkScanReport
    } catch (e: any) {
      appMessage.error(e?.message || `读取${paymentLabel}${cleanupMeta.title}数量失败`)
      return
    } finally {
      setPixLinkCleanupLoading(false)
      setPixLinkCleanupMode(null)
      setPixLinkCleanupType(null)
    }

    const eligible = Number(preview?.eligible_links || 0)
    const missing = Number(preview?.missing_expiry_links || 0)
    const instanceId = String(preview?.instance_id || '当前实例')
    const cutoff = String(preview?.cutoff_display || '最近一个北京时间 11:00')
    if (eligible <= 0) {
      appMessage.info(`当前没有可删除的${paymentLabel}${cleanupMeta.title}`)
      return
    }

    appModal.confirm({
      title: `删除 ${eligible} 条${paymentLabel}${cleanupMeta.title}？`,
      content: (
        <Space direction="vertical" size={6} style={{ width: '100%' }}>
          <Text>实例：{instanceId}</Text>
          {cleanupMode === 'expired' && paymentType === 'pix' ? <Text>当前删除截止点：{cutoff}（北京时间）</Text> : null}
          {cleanupMode === 'expired' && paymentType === 'upi' ? <Text>以 Stripe QR 的 expires_at 为过期依据。</Text> : null}
          {cleanupMode === 'expired' && paymentType === 'ideal' ? <Text>以支付链接提取时间后 15 分钟为过期点。</Text> : null}
          {cleanupMode === 'expired' && paymentType === 'team' ? <Text>以支付链接提取时间后 24 小时为过期点。</Text> : null}
          {cleanupMode === 'valid' ? <Text type="warning">有效链接可能仍可使用，仅在确认不再需要时删除。</Text> : null}
          {cleanupMode === 'paid' ? <Text>仅匹配当前链接的明确支付成功证据。</Text> : null}
          {cleanupMode === 'cancelled' ? <Text>仅匹配当前链接的明确支付取消证据，普通失败和超时会保留。</Text> : null}
          {cleanupMode === 'unknown' ? <Text type="warning">该链接状态无法确认，系统默认保留；本次将按人工选择删除。</Text> : null}
          <Text type="secondary">
            只删除账号当前 {paymentLabel} 链接 URL 及其完全相同的 cashier_url；不会删除账号、支付生成历史、支付 CDK 或提交结果。
          </Text>
          {cleanupMode === 'expired' && missing > 0 ? <Text type="warning">另有 {missing} 条缺少有效时间信息，本次不会删除。</Text> : null}
        </Space>
      ),
      okText: '确认删除',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: () => executePixLinkCleanup(cleanupMode, paymentType),
    })
  }

  const submitBatchPaymentLinkConfig = async () => {
    try {
      await batchPaymentLinkForm.validateFields()
    } catch {
      return
    }
    const forceRefresh = Boolean(batchPaymentLinkForceRefresh)
    const requestParams = buildBatchPaymentLinkParams(forceRefresh)
    const requestedPlan = String(requestParams.plan || 'plus').trim().toLowerCase()
    const isShortPayment = batchPaymentLinkPlan === 'short'
    let profile: PaymentLinkProfile | null = null
    if (isShortPayment) {
      if (batchPaymentLinkProfileError) return
      if (teamBillingCountryOptions.length === 0 && !(await loadShortPaymentLinkProfile())) return
    } else {
      profile = await loadBatchPaymentLinkProfile(requestParams)
      if (!profile) return
    }
    const targetAccountId = Number(batchPaymentLinkTargetAccount?.id || 0)
    const hasTargetAccount = Number.isInteger(targetAccountId) && targetAccountId > 0
    const batchScope: AccountTaskScope = selectedRowKeys.length > 0 ? 'selected' : 'filtered'
    const scope: 'single' | 'selected' | 'filtered' = hasTargetAccount
      ? 'single'
      : batchScope
    const toastKey = `payment-link:${batchPaymentLinkPlan}:${scope}:${forceRefresh ? 'force' : 'normal'}`
    const body: Record<string, unknown> = {
      skip_existing: !forceRefresh,
      force_refresh: forceRefresh,
      params: requestParams,
    }
    const productLabel = isShortPayment
      ? 'Plus 登录态短链'
      : requestedPlan === 'team' ? 'Team checkout 长链接' : '支付链接'
    const actionLabel = forceRefresh ? `强制重新生成${productLabel}` : `${productLabel}生成`
    const requestedCount = hasTargetAccount
      ? applyAccountTaskScopeToBody(body, {
          scope: 'selected',
          selectedIds: [targetAccountId],
          emptySelectedMessage: '当前账号不存在或已失效',
        })
      : applyAccountTaskScopeToBody(body, {
          scope: batchScope,
          emptySelectedMessage: `请先选择要${forceRefresh ? '强制重新生成' : '生成'}支付链接的账号`,
        })
    if (requestedCount === null) return

    setBatchPaymentLinkLoading(true)
    message.loading({ content: `${actionLabel}任务创建中...`, key: toastKey, duration: 0 })
    try {
      const res = await postAccountScopeRequest('/tasks/chatgpt/payment-links/batch', body, toastKey)
      if (!res) return
      setBatchPaymentLinkConfigOpen(false)
      setBatchPaymentLinkTargetAccount(null)

      const eligible = Number(res?.eligible || 0)
      const skipped = Number(res?.skipped || 0)
      const missing = Number(res?.missing || 0)
      const taskIdFromResponse = String(res?.task_id || '').trim()

      if (!taskIdFromResponse) {
        message.info({
          content: `没有可${forceRefresh ? '重新生成' : '生成'}支付链接的账号。请求 ${requestedCount} 个，跳过 ${skipped} 个${missing > 0 ? `，缺失 ${missing} 个` : ''}`,
          key: toastKey,
        })
        if (res && typeof res === 'object') {
          showBatchActionResult(`${actionLabel}结果`, res)
        }
        return
      }

      const snapshot = await apiFetch(`/tasks/${taskIdFromResponse}`)
      setTaskModalMode('payment_link')
      setTaskModalAccount(
        scope === 'single'
          ? batchPaymentLinkTargetAccount
          : scope === 'selected' ? null : { email: `当前筛选 ${eligible} 个账号` },
      )
      setTaskId(taskIdFromResponse)
      setTaskSnapshot(snapshot)
      setRegisterModalOpen(true)
      setActiveTasksPanelOpen(true)
      void activeTasksQuery.refetch()
      const launchSummary = isShortPayment
        ? `CHATGPT SHORT · ${String(requestParams.country || '-').toUpperCase()} / ${String(requestParams.currency || '-').toUpperCase()} · 本地串行`
        : `${String(profile?.link_type || 'long-link').toUpperCase()} · ${(profile?.country || profile?.billing_country || '-').toUpperCase()} / ${(profile?.currency || '-').toUpperCase()} · 并发 ${profile?.effective_concurrency || '-'}`
      message.success({
        content: `${actionLabel}任务已启动：${launchSummary}，可执行 ${eligible} 个，跳过 ${skipped} 个${missing > 0 ? `，缺失 ${missing} 个` : ''}`,
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

  const openWebSessionLoginConfig = async (mode: 'single' | 'batch', record: any = null) => {
    const accountId = Number(record?.id || 0)
    if (mode === 'single' && !accountId) return
    const scope: 'selected' | 'filtered' = selectedRowKeys.length > 0 ? 'selected' : 'filtered'
    const cfg = await loadConfigCache({ force: true }).catch(() => configCache || {})
    const proxySettings = taskProxySettingsFromConfig(cfg || {})
    setWebSessionLoginConfigMode(mode)
    setWebSessionLoginConfigAccount(mode === 'single' ? record : null)
    setWebSessionLoginConfigScope(scope)
    webSessionLoginConfigForm.setFieldsValue({
      concurrency: loadWebSessionLoginConcurrency(),
      ...proxySettings,
    })
    setWebSessionLoginConfigOpen(true)
  }

  const handleWebSessionLogin = async (record: any) => {
    await openWebSessionLoginConfig('single', record)
  }

  const handleBatchWebSessionLogin = async () => {
    await openWebSessionLoginConfig('batch')
  }

  const submitWebSessionLoginConfig = async () => {
    const values = await webSessionLoginConfigForm.validateFields()
    validateTaskProxySettings(values)
    const proxyPayload = buildTaskProxyPayload(values)
    const isBatch = webSessionLoginConfigMode === 'batch'
    const requestedConcurrency = Math.max(1, Math.floor(Number(values.concurrency || 1) || 1))
    const toastKey = isBatch
      ? `web-session-login:${webSessionLoginConfigScope}`
      : `web-session-login:${Number(webSessionLoginConfigAccount?.id || 0)}`

    setWebSessionLoginLoading(true)
    message.loading({
      content: `${isBatch ? '批量' : ''}执行登录态任务创建中...`,
      key: toastKey,
      duration: 0,
    })
    try {
      if (!isBatch) {
        const accountId = Number(webSessionLoginConfigAccount?.id || 0)
        if (!accountId) throw new Error('执行登录态账号无效')
        const res = await apiFetch('/tasks/chatgpt/web-session-login', {
          method: 'POST',
          body: JSON.stringify({
            account_id: accountId,
            ...proxyPayload,
          }),
        })
        const taskIdFromResponse = String(res?.task_id || '').trim()
        if (!taskIdFromResponse) throw new Error('任务创建成功，但未返回 task_id')
        const snapshot = await apiFetch(`/tasks/${taskIdFromResponse}`)
        setWebSessionLoginConfigOpen(false)
        setTaskModalMode('web_session_login')
        setTaskModalAccount(webSessionLoginConfigAccount)
        setTaskId(taskIdFromResponse)
        setTaskSnapshot(snapshot)
        setRegisterModalOpen(true)
        setActiveTasksPanelOpen(true)
        void activeTasksQuery.refetch()
        message.success({ content: '执行登录态任务已启动', key: toastKey })
        return
      }

      saveWebSessionLoginConcurrency(requestedConcurrency)
      const body: Record<string, unknown> = {
        params: {
          concurrency: requestedConcurrency,
          ...proxyPayload,
        },
      }
      const requestedCount = applyAccountTaskScopeToBody(body, {
        scope: webSessionLoginConfigScope,
        emptySelectedMessage: '请先选择要执行登录态的账号',
      })
      if (requestedCount === null) return

      const res = await postAccountScopeRequest('/tasks/chatgpt/web-session-login/batch', body, toastKey)
      if (!res) return

      const eligible = Number(res?.eligible || 0)
      const skipped = Number(res?.skipped || 0)
      const missing = Number(res?.missing || 0)
      const effectiveConcurrency = Number(res?.effective_concurrency || Math.min(requestedConcurrency, eligible) || 1)
      const taskIdFromResponse = String(res?.task_id || '').trim()
      setWebSessionLoginConfigOpen(false)

      if (!taskIdFromResponse) {
        message.info({
          content: `没有可执行登录态的账号。请求 ${requestedCount} 个，跳过 ${skipped} 个${missing > 0 ? `，缺失 ${missing} 个` : ''}`,
          key: toastKey,
        })
        if (res && typeof res === 'object') {
          showBatchActionResult('批量执行登录态结果', res)
        }
        return
      }

      const snapshot = await apiFetch(`/tasks/${taskIdFromResponse}`)
      setTaskModalMode('web_session_login')
      setTaskModalAccount(webSessionLoginConfigScope === 'selected' ? null : { email: `当前筛选 ${eligible} 个账号` })
      setTaskId(taskIdFromResponse)
      setTaskSnapshot(snapshot)
      setRegisterModalOpen(true)
      setActiveTasksPanelOpen(true)
      void activeTasksQuery.refetch()
      message.success({
        content: `批量执行登录态任务已启动：可执行 ${eligible} 个，并发 ${effectiveConcurrency}，跳过 ${skipped} 个${missing > 0 ? `，缺失 ${missing} 个` : ''}`,
        key: toastKey,
      })
      showBatchActionResult('批量执行登录态结果', res)
    } catch (e: any) {
      message.error({ content: `${isBatch ? '批量' : ''}执行登录态失败: ${e.message}`, key: toastKey })
    } finally {
      setWebSessionLoginLoading(false)
    }
  }

  const openInvalidRecheckConfig = async (mode: 'single' | 'batch', record: any = null) => {
    const accountId = Number(record?.id || 0)
    if (mode === 'single' && !accountId) return
    const scope: 'selected' | 'filtered' = selectedRowKeys.length > 0 ? 'selected' : 'filtered'
    const cfg = await loadConfigCache({ force: true }).catch(() => configCache || {})
    const proxySettings = taskProxySettingsFromConfig(cfg || {})
    setInvalidRecheckConfigMode(mode)
    setInvalidRecheckConfigAccount(mode === 'single' ? record : null)
    setInvalidRecheckConfigScope(scope)
    invalidRecheckConfigForm.setFieldsValue({
      concurrency: loadInvalidRecheckConcurrency(),
      ...proxySettings,
    })
    setInvalidRecheckConfigOpen(true)
  }

  const handleInvalidRecheck = async (record: any) => {
    await openInvalidRecheckConfig('single', record)
  }

  const handleBatchInvalidRecheck = async () => {
    await openInvalidRecheckConfig('batch')
  }

  const startPaymentEligibilityTask = async (
    kind: PaymentEligibilityKind,
    mode: 'single' | 'batch',
    record: any = null,
    options: { concurrency?: unknown; scope?: AccountTaskScope } = {},
  ) => {
    const label = kind === 'gcash_payment_method' ? 'GCash 支付方式' : '0 元试用资格'
    const accountId = Number(record?.id || 0)
    if (mode === 'single' && !accountId) return
    const batchScope = options.scope || (selectedRowKeys.length > 0 ? 'selected' : 'filtered')
    const toastKey = `${kind}:${mode}:${accountId || 'scope'}`
    const cfg = await loadConfigCache({ force: true }).catch(() => configCache || {})
    const proxyPayload = buildTaskProxyPayload(taskProxySettingsFromConfig(cfg || {}))
    const concurrency = normalizePaymentEligibilityConcurrency(options.concurrency)
    message.loading({ content: `${mode === 'batch' ? '批量 ' : ''}${label}检测任务创建中...`, key: toastKey, duration: 0 })
    setPaymentEligibilityLoading(true)
    try {
      let response: any
      if (mode === 'single') {
        response = await apiFetch(`/tasks/chatgpt/${kind === 'gcash_payment_method' ? 'gcash-payment-method' : 'zero-amount-eligibility'}`, {
          method: 'POST',
          body: JSON.stringify({ account_id: accountId, ...proxyPayload, max_attempts: 2 }),
        })
      } else {
        const body: Record<string, unknown> = {
          params: { concurrency, max_attempts: 2, ...proxyPayload },
        }
        const requestedCount = applyAccountTaskScopeToBody(body, {
          scope: batchScope,
          emptySelectedMessage: `请先选择要检测${label}的账号，或切换为当前筛选范围`,
        })
        if (requestedCount === null) return
        response = await postAccountScopeRequest(
          `/tasks/chatgpt/${kind === 'gcash_payment_method' ? 'gcash-payment-method' : 'zero-amount-eligibility'}/batch`,
          body,
          toastKey,
        )
        if (!response) return
      }
      const taskIdFromResponse = String(response?.task_id || '').trim()
      if (mode === 'batch') setPaymentEligibilityConfigOpen(false)
      if (!taskIdFromResponse) {
        message.info({ content: `${label}没有可检测账号`, key: toastKey })
        if (response && typeof response === 'object') showBatchActionResult(`${label}检测结果`, response)
        return
      }
      const snapshot = await apiFetch(`/tasks/${taskIdFromResponse}`)
      setTaskModalMode('payment_eligibility')
      const batchScopeLabel = batchScope === 'selected' ? '所选账号' : '当前筛选'
      setTaskModalAccount(mode === 'single' ? record : { email: `${batchScopeLabel} ${Number(response?.eligible || 0)} 个账号` })
      setTaskId(taskIdFromResponse)
      setTaskSnapshot(snapshot)
      setRegisterModalOpen(true)
      setActiveTasksPanelOpen(true)
      void activeTasksQuery.refetch()
      const effectiveConcurrency = Number(response?.effective_concurrency || Math.min(concurrency, Number(response?.eligible || 0)) || 1)
      message.success({
        content: mode === 'batch'
          ? `批量 ${label}检测任务已启动：可执行 ${Number(response?.eligible || 0)} 个，并发 ${effectiveConcurrency}`
          : `${label}检测任务已启动`,
        key: toastKey,
      })
      if (mode === 'batch') showBatchActionResult(`${label}检测结果`, response)
    } catch (e: any) {
      message.error({ content: `${label}检测失败: ${e?.message || e}`, key: toastKey })
    } finally {
      setPaymentEligibilityLoading(false)
    }
  }

  const handlePaymentEligibility = async (
    record: any,
    kind: PaymentEligibilityKind,
  ) => {
    await startPaymentEligibilityTask(kind, 'single', record)
  }

  const handleBatchPaymentEligibility = (kind: PaymentEligibilityKind) => {
    const scope: AccountTaskScope = selectedRowKeys.length > 0 ? 'selected' : 'filtered'
    setPaymentEligibilityConfigKind(kind)
    setPaymentEligibilityConfigScope(scope)
    paymentEligibilityConfigForm.setFieldsValue({ concurrency: loadPaymentEligibilityConcurrency() })
    setPaymentEligibilityConfigOpen(true)
  }

  const submitPaymentEligibilityConfig = async () => {
    const values = await paymentEligibilityConfigForm.validateFields()
    const concurrency = normalizePaymentEligibilityConcurrency(values.concurrency)
    savePaymentEligibilityConcurrency(concurrency)
    await startPaymentEligibilityTask(paymentEligibilityConfigKind, 'batch', null, {
      concurrency,
      scope: paymentEligibilityConfigScope,
    })
  }

  const submitInvalidRecheckConfig = async () => {
    const values = await invalidRecheckConfigForm.validateFields()
    validateTaskProxySettings(values)
    const proxyPayload = buildTaskProxyPayload(values)
    const isBatch = invalidRecheckConfigMode === 'batch'
    const requestedConcurrency = Math.max(1, Math.floor(Number(values.concurrency || 1) || 1))
    const toastKey = isBatch
      ? `invalid-recheck:${invalidRecheckConfigScope}`
      : `invalid-recheck:${Number(invalidRecheckConfigAccount?.id || 0)}`

    setBatchInvalidRecheckLoading(true)
    message.loading({
      content: `${isBatch ? '批量' : ''}失效测活任务创建中...`,
      key: toastKey,
      duration: 0,
    })
    try {
      if (!isBatch) {
        const accountId = Number(invalidRecheckConfigAccount?.id || 0)
        if (!accountId) throw new Error('失效测活账号无效')
        const res = await apiFetch('/tasks/chatgpt/invalid-recheck', {
          method: 'POST',
          body: JSON.stringify({
            account_id: accountId,
            ...proxyPayload,
          }),
        })
        const taskIdFromResponse = String(res?.task_id || '').trim()
        if (!taskIdFromResponse) throw new Error('任务创建成功，但未返回 task_id')
        const snapshot = await apiFetch(`/tasks/${taskIdFromResponse}`)
        setInvalidRecheckConfigOpen(false)
        setTaskModalMode('invalid_recheck')
        setTaskModalAccount(invalidRecheckConfigAccount)
        setTaskId(taskIdFromResponse)
        setTaskSnapshot(snapshot)
        setRegisterModalOpen(true)
        setActiveTasksPanelOpen(true)
        void activeTasksQuery.refetch()
        message.success({ content: '失效测活任务已启动', key: toastKey })
        return
      }

      saveInvalidRecheckConcurrency(requestedConcurrency)
      const body: Record<string, unknown> = {
        params: {
          concurrency: requestedConcurrency,
          ...proxyPayload,
        },
      }
      const requestedCount = applyAccountTaskScopeToBody(body, {
        scope: invalidRecheckConfigScope,
        emptySelectedMessage: '请先选择要测活的失效账号',
      })
      if (requestedCount === null) return

      const res = await postAccountScopeRequest('/tasks/chatgpt/invalid-recheck/batch', body, toastKey)
      if (!res) return

      const eligible = Number(res?.eligible || 0)
      const skipped = Number(res?.skipped || 0)
      const missing = Number(res?.missing || 0)
      const effectiveConcurrency = Number(res?.effective_concurrency || Math.min(requestedConcurrency, eligible) || 1)
      const taskIdFromResponse = String(res?.task_id || '').trim()
      setInvalidRecheckConfigOpen(false)

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
      setTaskModalMode('invalid_recheck')
      setTaskModalAccount(invalidRecheckConfigScope === 'selected' ? null : { email: `当前筛选 ${eligible} 个失效账号` })
      setTaskId(taskIdFromResponse)
      setTaskSnapshot(snapshot)
      setRegisterModalOpen(true)
      setActiveTasksPanelOpen(true)
      void activeTasksQuery.refetch()
      message.success({
        content: `批量失效测活任务已启动：可执行 ${eligible} 个，并发 ${effectiveConcurrency}，跳过 ${skipped} 个${missing > 0 ? `，缺失 ${missing} 个` : ''}`,
        key: toastKey,
      })
      showBatchActionResult('批量失效测活结果', res)
    } catch (e: any) {
      message.error({ content: `${isBatch ? '批量' : ''}失效测活失败: ${e.message}`, key: toastKey })
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
    const phonePoolMode = normalizePhonePoolMode(values.phone_pool_mode, values as Record<string, unknown>)
    // 手动粘贴仅属于普通绑定；特殊模式固定从任务创建时的号池快照取号。
    const phoneLines = phonePoolMode === 'normal' ? String(values.phone_lines || '').trim() : ''
    const selectedPrefixes = normalizeSelectedPrefixes(values.selected_prefixes)
    const prefixBindEnabled = !phoneLines && phonePoolMode === 'prefix_limited'
    const prefixSampleEnabled = !phoneLines && phonePoolMode === 'prefix_sample'
    const unavailableNumberTestEnabled = !phoneLines && phonePoolMode === 'unavailable_numbers'
    const requestedPhoneNumberFilter = normalizePhoneNumberFilter(values.prefix_number_filter || values.phone_number_filter)
    const phoneNumberFilter: PhoneNumberFilter = prefixBindEnabled
      ? requestedPhoneNumberFilter
      : unavailableNumberTestEnabled
        ? 'unavailable'
        : 'available'
    const smsProbeOnly = Boolean(values.prefix_sms_probe_only || values.sms_probe_only)
    const rawPrefixSampleFilter = String(values.prefix_sample_filter || 'all')
    const prefixSampleFilter = rawPrefixSampleFilter === 'available' ? 'available' : rawPrefixSampleFilter === 'rejected' ? 'rejected' : 'all'
    const fixedSnapshotSelection = prefixSampleEnabled
      || unavailableNumberTestEnabled
      || (prefixBindEnabled && phoneNumberFilter !== 'available')
    const usePool = !phoneLines && (Boolean(values.use_pool) || prefixBindEnabled || prefixSampleEnabled || unavailableNumberTestEnabled)
    const reusePhoneUntilUnusable = fixedSnapshotSelection || smsProbeOnly ? false : Boolean(values.reuse_phone_until_unusable)
    const requestedConcurrency = Math.max(1, Math.floor(Number(values.concurrency || 1) || 1))
    const effectiveConcurrency = reusePhoneUntilUnusable ? 1 : requestedConcurrency
    const normalizedValues = {
      ...values,
      phone_pool_mode: phonePoolMode,
      phone_number_filter: phoneNumberFilter,
      prefix_number_filter: phoneNumberFilter,
      unavailable_number_test_enabled: unavailableNumberTestEnabled,
      prefix_bind_enabled: prefixBindEnabled,
      selected_prefixes: selectedPrefixes,
      prefix_sample_enabled: prefixSampleEnabled,
      prefix_sms_probe_only: smsProbeOnly,
      sms_probe_only: smsProbeOnly,
      reuse_phone_until_unusable: reusePhoneUntilUnusable,
      concurrency: effectiveConcurrency,
    }
    savePhoneBindingSettings(normalizedValues)
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
      phone_pool_mode: phonePoolMode,
      phone_number_filter: phoneNumberFilter,
      prefix_number_filter: phoneNumberFilter,
      unavailable_number_test_enabled: unavailableNumberTestEnabled,
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
      concurrency: effectiveConcurrency,
      reuse_phone_until_unusable: reusePhoneUntilUnusable,
      ...buildTaskProxyPayload(values),
    }
    const requestedAccounts = applyAccountTaskScopeToBody(body, {
      scope,
      emptySelectedMessage: '请先选择用于手机号绑定的账号，或切换为当前筛选范围',
    })
    if (requestedAccounts === null) return

    const toastKey = `phone-binding-test:${scope}`
    setPhoneBindingTestLoading(true)
    message.loading({ content: '手机号绑定任务创建中...', key: toastKey, duration: 0 })
    try {
      const res = await postAccountScopeRequest('/tasks/chatgpt/phone-binding-test', body, toastKey)
      if (!res) return
      const taskIdFromResponse = String(res?.task_id || '').trim()
      const eligible = Number(res?.eligible_accounts || 0)
      const phoneCount = Number(res?.phone_count || 0)
      const prefixSample = res?.prefix_sample && typeof res.prefix_sample === 'object' ? res.prefix_sample : null
      const prefixBind = res?.prefix_bind && typeof res.prefix_bind === 'object' ? res.prefix_bind : null
      const unavailableNumbers = res?.unavailable_numbers && typeof res.unavailable_numbers === 'object' ? res.unavailable_numbers : null
      const smsProbeOnly = Boolean(res?.sms_probe_only || prefixSample?.sms_probe_only || prefixBind?.sms_probe_only || unavailableNumbers?.sms_probe_only)
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
          ? `限定号段绑定已启动：${Number(prefixBind.prefix_count || 0)} 个号段，${phoneCount} 个候选号码，预计测试 ${Number(prefixBind.candidate_test_count || Math.min(phoneCount, eligible))} 个${smsProbeOnly ? '，仅测发码/收码' : ''}`
          : prefixSample?.enabled
            ? `号段抽样已启动：${Array.isArray(prefixSample.requested_prefixes) && prefixSample.requested_prefixes.length > 0 ? '指定号段，' : String(prefixSample.filter || 'all') === 'rejected' ? '仅失败样本，' : ''}${Number(prefixSample.prefix_count || 0)} 个号段，${phoneCount} 个号码，${eligible} 个账号${smsProbeOnly ? '，仅测发码/收码' : ''}`
            : unavailableNumbers?.enabled
              ? `不可用号码全量复测已启动：${phoneCount} 个固定候选，预计测试 ${Number(unavailableNumbers.candidate_test_count || Math.min(phoneCount, eligible))} 个${smsProbeOnly ? '，仅测发码/收码' : ''}`
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
      pix_cdk: '',
      pix_cdk_lines: '',
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
      saveBaxiGptCdkSettings({
        ...baxiCdkSubmitForm.getFieldsValue(true),
        use_pool: true,
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
      // Validation details can contain the transient PIX CDK field. Do not
      // forward the form object into browser devtools/local diagnostics.
      console.error('Baxi submit validation failed')
      message.error('表单参数有误，请检查（展开高级参数查看详细错误）')
      return
    }
    saveBaxiGptCdkSettings(values)
    const scope = (values.scope === 'filtered' ? 'filtered' : 'selected') as 'selected' | 'filtered'
    const paymentChannel = values.payment_channel === 'pix' ? 'pix' : 'ideal'
    const pixSubmitMode = values.pix_submit_mode === 'user_link' ? 'user_link' : 'auto_extract'
    const pixSubmissionLabel = pixSubmitMode === 'user_link' ? 'PIX 链接上传' : 'PIX 自动提链'
    const pixCdkLines = String(values.pix_cdk_lines || values.pix_cdk || '').trim()
    const codeLines = String(values.code_lines || '').trim()
    const usePool = paymentChannel === 'ideal' && !codeLines && Boolean(values.use_pool)
    const selectedCdkIds = normalizeBaxiCdkIdList(values.cdk_ids)
    if (paymentChannel === 'pix' && !pixCdkLines) {
      message.warning('请每行输入一个 PIX CDK')
      return
    }
    if (paymentChannel === 'ideal' && !usePool && !codeLines) {
      message.warning('请粘贴卡密，或启用卡密池')
      return
    }
    const body: Record<string, unknown> = {
      payment_channel: paymentChannel,
      failure_continue: Boolean(values.failure_continue),
      submit_interval_seconds: Number(values.submit_interval_seconds || 0),
      status_poll_interval_seconds: normalizeBaxiStatusPollInterval(values.status_poll_interval_seconds),
      status_poll_timeout_seconds: Number(values.status_poll_timeout_seconds || 1800),
      target_success_count: Math.max(Number(values.target_success_count || 0), 0),
    }
    if (paymentChannel === 'pix') {
      body.pix_submit_mode = pixSubmitMode
      body.pix_cdk_lines = pixCdkLines
      body.auto_poll_status = true
    } else {
      body.code_lines = codeLines
      body.use_pool = usePool
      body.precheck = Boolean(values.precheck)
      body.auto_poll_status = values.auto_poll_status !== false
      if (usePool && selectedCdkIds.length > 0) body.cdk_ids = selectedCdkIds
    }
    const requestedAccounts = applyAccountTaskScopeToBody(body, {
      scope,
      emptySelectedMessage: '请先选择用于 iDEAL / PIX 批量提交的账号，或切换为当前筛选范围',
    })
    if (requestedAccounts === null) return

    const toastKey = `baxigpt-cdk-submit:${scope}`
    setBaxiCdkSubmitLoading(true)
    message.loading({ content: `${paymentChannel === 'pix' ? pixSubmissionLabel : 'iDEAL'} 批量提交任务创建中...`, key: toastKey, duration: 0 })
    try {
      const res = await postAccountScopeRequest('/tasks/chatgpt/baxigpt-cdk-submit', body, toastKey)
      if (!res) return
      const taskIdFromResponse = String(res?.task_id || '').trim()
      const pairCount = Number(res?.pair_count || 0)
      const eligible = Number(res?.eligible_accounts || 0)
      const availableCodes = Number(res?.available_codes || 0)
      const spareCodes = Number(res?.spare_codes || 0)
      const targetSuccess = Number(res?.effective_target_success_count || 0)
      const pixCdkCount = Number(res?.pix_cdk_count || 0)
      const importInfo = res?.cdk_pool_import && typeof res.cdk_pool_import === 'object' ? res.cdk_pool_import : {}
      const importErrors = Array.isArray(importInfo?.errors) ? importInfo.errors : []
      const skippedAccounts = Array.isArray(res?.skipped_accounts) ? res.skipped_accounts : []
      const submittedLinkSkipped = paymentChannel === 'pix' && pixSubmitMode === 'user_link'
        ? skippedAccounts.filter((item: any) => String(item?.reason || '').includes('已提交至管理端')).length
        : 0

      if (!taskIdFromResponse) {
        message.info({
          content: paymentChannel === 'pix'
            ? `没有可提交的 ${pixSubmissionLabel}账号。请求 ${requestedAccounts} 个账号。${submittedLinkSkipped > 0 ? `其中 ${submittedLinkSkipped} 个链接已提交至管理端，请先重新同步或生成新链接。` : ''}`
            : `没有可提交的卡密/账号配对。请求 ${requestedAccounts} 个账号，可用卡密 ${availableCodes} 个`,
          key: toastKey,
        })
        if (res && typeof res === 'object') {
          showBatchActionResult(`${paymentChannel === 'pix' ? pixSubmissionLabel : 'iDEAL'} 批量提交结果`, res)
        }
        if (paymentChannel === 'pix') baxiCdkSubmitForm.setFieldValue('pix_cdk_lines', '')
        if (paymentChannel === 'ideal') await loadBaxiCdkPoolSummary()
        return
      }

      if (paymentChannel === 'pix') baxiCdkSubmitForm.setFieldValue('pix_cdk_lines', '')
      setBaxiCdkSubmitOpen(false)
      setTaskModalMode('baxigpt_cdk')
      setTaskModalAccount({
        email: targetSuccess > 0
          ? `${paymentChannel === 'pix' ? pixSubmissionLabel : 'iDEAL'} 批量提交：目标成功 ${targetSuccess} / 候选 ${pairCount}`
          : paymentChannel === 'pix'
            ? `${pixSubmissionLabel}：${pairCount} 个候选账号 / ${pixCdkCount} 个 CDK`
            : `iDEAL 批量提交：${pairCount} 对 / 库存余 ${spareCodes}`,
      })
      setTaskId(taskIdFromResponse)
      setTaskSnapshot({
        task_id: taskIdFromResponse,
        status: 'pending',
        source: 'baxigpt_cdk_submit',
        meta: {
          payment_channel: paymentChannel,
          pix_submit_mode: pixSubmitMode,
          pair_count: pairCount,
          eligible_accounts: eligible,
          pix_cdk_count: pixCdkCount,
        },
      })
      setRegisterModalOpen(true)
      setActiveTasksPanelOpen(true)
      void activeTasksQuery.refetch()
      if (paymentChannel === 'ideal') void loadBaxiCdkPoolSummary()
      void apiFetch(`/tasks/${taskIdFromResponse}`)
        .then((snapshot) => setTaskSnapshot(snapshot))
        .catch(() => {
          message.warning({
            content: '任务已创建，任务快照暂不可读；日志面板会自动重试。',
            key: toastKey,
          })
        })
      message.success({
        content: paymentChannel === 'pix'
          ? `${pixSubmissionLabel}已启动：${pairCount} 个候选账号，${pixCdkCount} 个 CDK${targetSuccess > 0 ? `，目标成功 ${targetSuccess} 个` : ''}。本站多额度 CDK 仅在确认 paid 后串行复用；外部 PIX CDK 成功后不复用；未知结果继续锁定人工复核。`
          : `iDEAL 批量提交已启动：${pairCount} 个候选配对${targetSuccess > 0 ? `，目标成功 ${targetSuccess} 个` : ''}，候选账号 ${eligible} 个，可用卡密 ${availableCodes} 个${spareCodes > 0 ? `，剩余入库 ${spareCodes} 个` : ''}${importErrors.length > 0 ? `，解析跳过 ${importErrors.length} 行` : ''}`,
        key: toastKey,
      })
      if (paymentChannel === 'ideal' && importErrors.length > 0) {
        showBatchActionResult('卡密解析结果', { items: importErrors, total: importErrors.length })
      }
    } catch (e: any) {
      const rawError = String(e?.message || '请求失败')
      const safeError = paymentChannel === 'pix'
        ? pixCdkLines.split(/\r?\n/).map((item) => item.trim()).filter(Boolean).reduce(
          (text, cdk) => text.split(cdk).join('[REDACTED]'),
          rawError,
        )
        : rawError
      message.error({ content: `${paymentChannel === 'pix' ? 'PIX' : 'iDEAL'} 批量提交失败: ${safeError}`, key: toastKey })
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
    if (applyAccountTaskScopeToBody(body, {
      scope,
      selectedIds: scope === 'selected' ? getPaypalBindingSelectedIds() : undefined,
      emptySelectedMessage: '当前选中账号里没有未订阅且有效的账号，请重新选择或切换为当前筛选范围',
    }) === null) return

    const toastKey = `paypal-binding:${scope}`
    setPaypalBindingLoading(true)
    message.loading({ content: 'PayPal绑定任务创建中...', key: toastKey, duration: 0 })
    try {
      const res = await postAccountScopeRequest('/tasks/chatgpt/paypal-bind', body, toastKey)
      if (!res) return
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
    const accountIds = normalizeAccountIds(selectedRowKeys)
    if (accountIds.length === 0) {
      appMessage.warning('请先选择要删除的账号')
      return
    }
    try {
      const result = await apiFetch('/accounts/batch-delete', {
        method: 'POST',
        body: JSON.stringify({ ids: accountIds }),
      })
      const deleted = Number(result?.deleted || 0)
      const notFound = Array.isArray(result?.not_found) ? result.not_found.length : 0
      if (deleted > 0) {
        appMessage.success(`批量删除完成：已删除 ${deleted} 个账号${notFound > 0 ? `，${notFound} 个已不存在` : ''}`)
      } else if (notFound > 0) {
        appMessage.warning(`选中的账号已不存在（${notFound} 个），列表已刷新`)
      } else {
        appMessage.info('没有账号被删除，列表已刷新')
      }
      setSelectedRowKeys([])
      setSelectedAccountSnapshots({})
      await load()
    } catch (error: unknown) {
      appMessage.error(error instanceof Error && error.message ? error.message : '批量删除失败')
    }
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
    const active = await apiFetch('/chatgpt/gopay/batch/active').catch(() => ({ task: null }))
    if (selectedAccounts.length === 0 && !active?.task) {
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
    setBatchGopayTaskId('')
    setBatchGopayStopMode('')
    setBatchGopayNextRoundAt(null)
    try {
      const { phones } = await loadGopayBatchConfig()
      if (active?.task) {
        applyGopayBatchTask(active.task)
        return
      }
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

  const applyGopayBatchTask = (task: any) => {
    if (!task || typeof task !== 'object') return
    const status = String(task.status || '')
    const items = Array.isArray(task.items) ? task.items : []
    setBatchGopayTaskId(String(task.task_id || '').trim())
    setBatchGopayStopMode(String(task.stop_mode || '').trim())
    setBatchGopayStarted(['queued', 'running'].includes(status))
    const nextRoundAt = Number(task.next_round_at || 0)
    setBatchGopayNextRoundAt(Number.isFinite(nextRoundAt) && nextRoundAt > 0 ? nextRoundAt * 1000 : null)
    setBatchGopayItems((previous) => {
      const localState = new Map(previous.map((item) => [Number(item.account?.id || 0), item]))
      return items.map((raw: any, index: number) => {
        const accountId = Number(raw?.account_id || raw?.account?.id || 0)
        const local = localState.get(accountId)
        const remoteStatus = String(raw?.status || 'queued')
        const statusValue: BatchGopayItem['status'] = (
          ['queued', 'starting', 'running', 'done', 'failed', 'cancelled', 'stopped'].includes(remoteStatus)
            ? remoteStatus
            : 'failed'
        ) as BatchGopayItem['status']
        return {
          account: raw?.account || local?.account || { id: accountId, email: raw?.email || '' },
          phone: raw?.phone || local?.phone,
          batchIndex: Number(raw?.batchIndex || raw?.batch_index || index + 1),
          round: Math.max(1, Number(raw?.round || 1)),
          status: statusValue,
          snapshot: raw?.snapshot || {},
          error: String(raw?.error || ''),
          logsOpen: local?.logsOpen || false,
          configOpen: local?.configOpen || false,
          submitting: local?.submitting || false,
        }
      })
    })
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

  const startBatchGopay = async () => {
    const items = batchGopayItems.filter((item) => item.status === 'queued')
    if (items.length === 0) return
    try {
      await saveBatchGopayOtpAutoResendDelay(batchGopayOtpAutoResendDelay, { notify: false, throwOnError: true })
      const defaults = buildBatchGopayPayload(items[0])
      const task = await apiFetch('/chatgpt/gopay/batch/start', {
        method: 'POST',
        body: JSON.stringify({
          items: items.map((item) => ({
            account_id: Number(item.account.id),
            phone: {
              id: String(item.phone.id || ''),
              label: String(item.phone.label || ''),
              phone_country_code: String(item.phone.phone_country_code || ''),
              phone_number: String(item.phone.phone_number || ''),
            },
            batchIndex: item.batchIndex,
            round: item.round,
          })),
          round_interval_seconds: batchGopayRoundInterval,
          otp_auto_resend_delay_seconds: batchGopayOtpAutoResendDelay,
          defaults: {
            pin: defaults.pin,
            access_token: String(batchGopayDefaults.access_token || '').trim(),
            proxy: defaults.proxy,
            country: defaults.country,
            currency: defaults.currency,
            billing_name: defaults.billing_name,
            billing_email: String(batchGopayDefaults.billing_email || '').trim(),
            billing_country: defaults.billing_country,
            billing_line1: defaults.billing_line1,
            billing_city: defaults.billing_city,
            billing_state: defaults.billing_state,
            billing_postal_code: defaults.billing_postal_code,
          },
        }),
      })
      applyGopayBatchTask(task)
      message.success(`已创建 GoPay 批量任务：${items.length} 个账号`)
    } catch (e: any) {
      message.error(e?.message || '启动批量 GoPay 失败')
    }
  }

  useEffect(() => {
    if (!pageVisible || !batchGopayOpen || !batchGopayStarted || !batchGopayTaskId) return
    let cancelled = false
    const refresh = async () => {
      try {
        const task = await apiFetch(`/chatgpt/gopay/batch/${encodeURIComponent(batchGopayTaskId)}`)
        if (!cancelled) applyGopayBatchTask(task)
      } catch (e: any) {
        if (!cancelled) message.warning(e?.message || '刷新 GoPay 批量任务失败')
      }
    }
    void refresh()
    const timer = window.setInterval(() => { void refresh() }, 3000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [pageVisible, batchGopayOpen, batchGopayStarted, batchGopayTaskId])

  const submitBatchGopayInput = async (item: BatchGopayItem, value: string) => {
    if (!batchGopayTaskId || !item.snapshot?.session_id) return
    const phase = String(item.snapshot.phase || '')
    const path = phase === 'waiting_otp' ? 'otp' : 'pin'
    const key = phase === 'waiting_otp' ? 'otp' : 'pin'
    updateBatchGopayItem(item.account.id, { submitting: true })
    try {
      const data = await apiFetch(`/chatgpt/gopay/batch/${encodeURIComponent(batchGopayTaskId)}/items/${item.account.id}/${path}`, {
        method: 'POST',
        body: JSON.stringify({ [key]: String(value || '').trim() }),
      })
      updateBatchGopayItem(item.account.id, {
        snapshot: data?.snapshot || item.snapshot,
        status: String(data?.status || item.status) as BatchGopayItem['status'],
        submitting: false,
        error: String(data?.error || ''),
      })
      message.success(`已提交 ${item.account.email}`)
    } catch (e: any) {
      updateBatchGopayItem(item.account.id, { submitting: false, error: e?.message || '提交失败' })
      message.error(e?.message || '提交失败')
    }
  }

  const resendBatchGopayOtp = async (item: BatchGopayItem) => {
    if (!batchGopayTaskId || !item.snapshot?.session_id) return
    updateBatchGopayItem(item.account.id, { submitting: true, error: '' })
    try {
      const data = await apiFetch(`/chatgpt/gopay/batch/${encodeURIComponent(batchGopayTaskId)}/items/${item.account.id}/resend-otp`, {
        method: 'POST',
      })
      updateBatchGopayItem(item.account.id, {
        snapshot: data?.snapshot || item.snapshot,
        status: String(data?.status || item.status) as BatchGopayItem['status'],
        submitting: false,
        error: String(data?.error || ''),
      })
      message.success(`GoPay OTP 重发请求已提交：${item.account.email}`)
    } catch (e: any) {
      updateBatchGopayItem(item.account.id, { submitting: false, error: e?.message || '重发 OTP 失败' })
      message.error(e?.message || '重发 OTP 失败')
    }
  }

  const cancelBatchGopayItem = async (item: BatchGopayItem) => {
    if (!batchGopayTaskId) {
      updateBatchGopayItem(item.account.id, { status: 'cancelled' })
      return
    }
    try {
      const data = await apiFetch(`/chatgpt/gopay/batch/${encodeURIComponent(batchGopayTaskId)}/items/${item.account.id}/cancel`, {
        method: 'POST',
      })
      updateBatchGopayItem(item.account.id, {
        status: String(data?.status || 'cancelled') as BatchGopayItem['status'],
        snapshot: data?.snapshot || item.snapshot,
        error: String(data?.error || ''),
      })
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
    if (!batchGopayTaskId) {
      await Promise.all(cancellableItems.map((item) => cancelBatchGopayItem(item)))
      message.success(`已取消 ${cancellableItems.length} 个待启动批量支付任务`)
      return
    }
    const task = await apiFetch(`/chatgpt/gopay/batch/${encodeURIComponent(batchGopayTaskId)}/cancel`, { method: 'POST' })
    applyGopayBatchTask(task)
    message.success(`已取消 ${cancellableItems.length} 个批量支付任务`)
  }

  const stopBatchGopayAfterCurrent = async () => {
    if (!batchGopayTaskId) {
      message.info('请先启动批量 GoPay 任务')
      return
    }
    try {
      const task = await apiFetch(`/chatgpt/gopay/batch/${encodeURIComponent(batchGopayTaskId)}/cancel`, {
        method: 'POST',
        body: JSON.stringify({ mode: 'after_current' }),
      })
      applyGopayBatchTask(task)
      message.success('已停止后续账号启动，当前 GoPay 会话会正常完成')
    } catch (e: any) {
      message.error(e?.message || '请求完成当前后停止失败')
    }
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
    try {
      await registerForm.validateFields()
    } catch {
      return
    }
    const values = registerForm.getFieldsValue(true)
    const mailProviderOverride = normalizeRegisterMailProviderOverride(values.mail_provider_override)
    const tempmailMode = normalizeRegisterTempMailMode(values.tempmail_mode)
    const tempmailFixedDomains = normalizeDomainList(values.tempmail_fixed_domains)
    const tempmailPrimaryDomain = String(
      values.tempmail_primary_domain || tempmailFixedDomains[0] || '',
    ).trim().replace(/^[@.]+/, '')
    const executorType = normalizeExecutorForPlatform(currentPlatform, values.executor_type)
    const effectiveMailProvider = mailProviderOverride !== '__global__'
      ? mailProviderOverride
      : registerMailProvider
    const delaySettings = normalizeRegisterDelaySettings(values, currentPlatform, registerControlConfig)
    const uniqueExitPolicy = normalizeRegisterUniqueExitPolicy(
      values.chatgpt_register_unique_exit_ip_policy,
      values.chatgpt_register_unique_exit_ip_enabled,
    )
    const settingsPayload = {
      count: Number(values.count || 1) || 1,
      concurrency: normalizeRegisterConcurrency(
        values.concurrency,
        currentPlatform,
        executorType,
        effectiveMailProvider === 'manual_email_otp',
        registerControlConfig,
      ),
      ...delaySettings,
      executor_type: executorType,
      proxy_mode: String(values.proxy_mode || 'dynamic'),
      proxy: String(values.proxy || '').trim(),
      proxy_country_code: String(values.proxy_country_code || '').trim().toUpperCase(),
      proxy_failover: Boolean(values.proxy_failover),
      proxy_max_candidates: Number(values.proxy_max_candidates || 5) || 5,
      proxy_min_score: Number(values.proxy_min_score || 50) || 50,
      mail_provider_override: mailProviderOverride,
      tempmail_mode: tempmailMode,
      tempmail_primary_domain: tempmailPrimaryDomain,
      tempmail_fixed_domains: tempmailFixedDomains,
      email: String(values.email || '').trim(),
      chatgpt_existing_account_capture: Boolean(values.chatgpt_existing_account_capture),
      chatgpt_save_registration_access_token_account:
        values.chatgpt_save_registration_access_token_account === undefined
          ? true
          : Boolean(values.chatgpt_save_registration_access_token_account),
      chatgpt_existing_account_login_route_enabled:
        values.chatgpt_existing_account_login_route_enabled === undefined
          ? true
          : Boolean(values.chatgpt_existing_account_login_route_enabled),
      chatgpt_register_unique_exit_ip_policy: uniqueExitPolicy,
      chatgpt_register_otp_wait_seconds: Number(values.chatgpt_register_otp_wait_seconds || 120) || 120,
      chatgpt_register_otp_resend_wait_seconds: Number(values.chatgpt_register_otp_resend_wait_seconds || 90) || 90,
      chatgpt_register_otp_account_budget_seconds: Number(values.chatgpt_register_otp_account_budget_seconds || 210) || 210,
    }

    setRegisterSettingsSaving(true)
    try {
      validateTaskProxySettings(settingsPayload)
      saveRegisterFormSettings(currentPlatform, {
        register_mail_profile_saved: true,
        count: settingsPayload.count,
        concurrency: settingsPayload.concurrency,
        register_delay_seconds: settingsPayload.register_delay_seconds,
        register_delay_max_seconds: settingsPayload.register_delay_max_seconds,
        executor_type: settingsPayload.executor_type,
        mail_provider_override: settingsPayload.mail_provider_override,
        tempmail_mode: settingsPayload.tempmail_mode,
        tempmail_primary_domain: settingsPayload.tempmail_primary_domain,
        tempmail_fixed_domains: settingsPayload.tempmail_fixed_domains,
        email: settingsPayload.email,
        chatgpt_register_unique_exit_ip_policy: settingsPayload.chatgpt_register_unique_exit_ip_policy,
        chatgpt_register_unique_exit_ip_enabled: undefined,
      })
      await saveTaskProxySettingsToConfig(settingsPayload)
      if (currentPlatform === 'chatgpt') {
        await apiFetch('/config', {
          method: 'PUT',
          body: JSON.stringify({
            data: {
              chatgpt_register_unique_exit_ip_policy: settingsPayload.chatgpt_register_unique_exit_ip_policy,
              chatgpt_save_registration_access_token_account: settingsPayload.chatgpt_save_registration_access_token_account ? 'true' : 'false',
              chatgpt_existing_account_login_route_enabled: settingsPayload.chatgpt_existing_account_login_route_enabled ? 'true' : 'false',
              chatgpt_register_otp_wait_seconds: String(settingsPayload.chatgpt_register_otp_wait_seconds),
              chatgpt_register_otp_resend_wait_seconds: String(settingsPayload.chatgpt_register_otp_resend_wait_seconds),
              chatgpt_register_otp_account_budget_seconds: String(settingsPayload.chatgpt_register_otp_account_budget_seconds),
            },
          }),
        })
      }
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
    try {
      const values = await registerForm.validateFields()
      setRegisterLoading(true)
      setTaskModalMode('register')
      setTaskModalAccount(null)
      const cfg = await loadConfigCache({ force: true })
      const selectedProviderOverride = String(values.mail_provider_override || '').trim()
      const resolvedMailProvider =
        selectedProviderOverride && selectedProviderOverride !== '__global__'
          ? selectedProviderOverride
          : (String(cfg.mail_provider || 'luckmail').trim() || 'luckmail')
      setRegisterMailProvider(resolvedMailProvider)
      const phoneSignupEnabled =
        currentPlatform === 'chatgpt'
        && String(values.chatgpt_registration_entry || '').trim().toLowerCase().replace(/-/g, '_') === 'phone_signup'
      const executorType = normalizeExecutorForPlatform(currentPlatform, values.executor_type)
      const existingAccountCapture =
        !phoneSignupEnabled
        && currentPlatform === 'chatgpt'
        && chatgptRegistrationMode === CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN
        && resolvedMailProvider === 'manual_email_otp'
        && Boolean(values.chatgpt_existing_account_capture)
      const normalizedLoginPassword = String(values.login_password || '').trim()
      if (phoneSignupEnabled && !normalizedLoginPassword) {
        throw new Error('手机号注册/已注册手机号登录必须填写同一个密码')
      }
      if (
        phoneSignupEnabled
        && !values.chatgpt_phone_signup_use_pool
        && !String(values.chatgpt_phone_signup_phone_lines || '').trim()
      ) {
        throw new Error('手机号注册请粘贴 手机号----收码API / 手机号|收码API，或勾选使用手机号池')
      }
      const configuredTempMailMode = String(values.tempmail_mode || cfg.tempmail_mode || 'fixed_domain').trim().toLowerCase()
      const tempmailMode = configuredTempMailMode === 'task_subdomain' ? 'task_subdomain' : 'fixed_domain'
      const tempmailFixedDomains = normalizeDomainList(values.tempmail_fixed_domains)
      const tempmailPrimaryDomain = tempmailFixedDomains[0]
        || (tempmailMode === 'task_subdomain'
          ? String(values.tempmail_primary_domain || cfg.tempmail_primary_domain || '').trim().replace(/^[@.]+/, '')
          : '')
      const registerExtra = {
        mail_provider: phoneSignupEnabled ? undefined : resolvedMailProvider,
        email_api_lines: !phoneSignupEnabled && resolvedMailProvider === 'email_api' ? String(values.email_api_lines || cfg.email_api_lines || '').trim() : undefined,
        email_api_poll_interval_seconds: cfg.email_api_poll_interval_seconds || values.email_api_poll_interval_seconds || 3,
        email_api_request_timeout_seconds: cfg.email_api_request_timeout_seconds || values.email_api_request_timeout_seconds || 15,
        email_api_gmail_dot_variant_enabled: parseBooleanConfigValue(
          values.email_api_gmail_dot_variant_enabled ?? cfg.email_api_gmail_dot_variant_enabled ?? true,
        ),
        email_api_gmail_variant_count: Number(values.email_api_gmail_variant_count ?? cfg.email_api_gmail_variant_count ?? 2) || 2,
        email_api_gmail_variant_rules: String(values.email_api_gmail_variant_rules ?? cfg.email_api_gmail_variant_rules ?? 'all').trim() || 'all',
        email_api_gmail_plus_tag_template: String(values.email_api_gmail_plus_tag_template ?? cfg.email_api_gmail_plus_tag_template ?? 'r{rand}').trim() || 'r{rand}',
        email_api_default_scheme: cfg.email_api_default_scheme || 'https',
        email_api_use_all_identities: !phoneSignupEnabled && resolvedMailProvider === 'email_api' ? true : undefined,
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
        tempmail_mode: tempmailMode,
        tempmail_primary_domain: tempmailPrimaryDomain,
        tempmail_fixed_domains: tempmailFixedDomains,
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
        chatgpt_registration_entry:
          currentPlatform === 'chatgpt' ? (phoneSignupEnabled ? 'phone_signup' : 'email_signup') : undefined,
        chatgpt_phone_signup_password: phoneSignupEnabled ? normalizedLoginPassword : undefined,
        chatgpt_phone_signup_use_pool: phoneSignupEnabled ? Boolean(values.chatgpt_phone_signup_use_pool) : undefined,
        chatgpt_phone_signup_phone_lines: phoneSignupEnabled ? String(values.chatgpt_phone_signup_phone_lines || '').trim() : undefined,
        chatgpt_phone_signup_timeout_seconds: phoneSignupEnabled ? values.chatgpt_phone_signup_timeout_seconds : undefined,
        chatgpt_phone_signup_poll_interval_seconds: phoneSignupEnabled ? values.chatgpt_phone_signup_poll_interval_seconds : undefined,
        chatgpt_phone_signup_max_resend_attempts: phoneSignupEnabled ? values.chatgpt_phone_signup_max_resend_attempts : undefined,
        chatgpt_phone_signup_resend_interval_seconds: phoneSignupEnabled ? values.chatgpt_phone_signup_resend_interval_seconds : undefined,
        chatgpt_existing_account_capture: currentPlatform === 'chatgpt' ? existingAccountCapture : undefined,
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
        chatgpt_register_unique_exit_ip_policy:
          currentPlatform === 'chatgpt'
            ? normalizeRegisterUniqueExitPolicy(
                values.chatgpt_register_unique_exit_ip_policy,
                values.chatgpt_register_unique_exit_ip_enabled,
              )
            : undefined,
        chatgpt_register_otp_wait_seconds:
          currentPlatform === 'chatgpt' ? values.chatgpt_register_otp_wait_seconds : undefined,
        chatgpt_register_otp_resend_wait_seconds:
          currentPlatform === 'chatgpt' ? values.chatgpt_register_otp_resend_wait_seconds : undefined,
        chatgpt_register_otp_account_budget_seconds:
          currentPlatform === 'chatgpt' ? values.chatgpt_register_otp_account_budget_seconds : undefined,
      }
      const chatgptRegistrationRequestAdapter =
        buildChatGPTRegistrationRequestAdapter(
          currentPlatform,
          chatgptRegistrationMode,
        )
      const adaptedRegisterExtra = phoneSignupEnabled
        ? registerExtra
        : chatgptRegistrationRequestAdapter
          ? chatgptRegistrationRequestAdapter.extendExtra(registerExtra)
          : registerExtra

      if (!phoneSignupEnabled && resolvedMailProvider === 'email_api' && currentPlatform === 'chatgpt') {
        const rawLines = String(values.email_api_lines || cfg.email_api_lines || '').trim()
        if (!rawLines) {
          throw new Error('邮箱验证码 API 模式必须填写 email----api 行')
        }
      }

      if (!phoneSignupEnabled && resolvedMailProvider === 'manual_email_otp' && currentPlatform === 'chatgpt') {
        const normalizedEmail = String(values.email || '').trim()
        if (!normalizedEmail) {
          throw new Error('手动邮箱模式必须填写邮箱地址')
        }
        window.localStorage.setItem('auto-chatgpt.manual_email_otp.email', normalizedEmail)
      }

      validateTaskProxySettings(values)
      const concurrency = normalizeRegisterConcurrency(
        values.concurrency,
        currentPlatform,
        executorType,
        phoneSignupEnabled || resolvedMailProvider === 'manual_email_otp',
        cfg,
      )
      const delaySettings = normalizeRegisterDelaySettings(values, currentPlatform, cfg)
      const uniqueExitPolicy = normalizeRegisterUniqueExitPolicy(
        values.chatgpt_register_unique_exit_ip_policy,
        values.chatgpt_register_unique_exit_ip_enabled,
      )
      mergeRegisterFormSettings(currentPlatform, {
        count: Number(values.count || 1) || 1,
        concurrency,
        ...delaySettings,
        executor_type: executorType,
        email: String(values.email || '').trim(),
        chatgpt_register_unique_exit_ip_policy: uniqueExitPolicy,
        chatgpt_register_unique_exit_ip_enabled: undefined,
      })
      const proxyPayload = buildTaskProxyPayload(values)

      const res = await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify({
          platform: currentPlatform,
          email:
            !phoneSignupEnabled && resolvedMailProvider === 'manual_email_otp' && currentPlatform === 'chatgpt'
              ? (String(values.email || '').trim() || null)
              : null,
          password: phoneSignupEnabled
            ? normalizedLoginPassword
            : existingAccountCapture
              ? (normalizedLoginPassword || null)
              : null,
          count: values.count,
          concurrency,
          ...delaySettings,
          executor_type: executorType,
          registration_diagnostics_mode: normalizeRegistrationDiagnosticsMode(
            values.registration_diagnostics_mode,
            executorType,
            currentPlatform,
          ),
          captcha_solver: cfg.default_captcha_solver || 'yescaptcha',
          ...proxyPayload,
          extra: adaptedRegisterExtra,
        }),
      })
      setTaskId(res.task_id)
    } catch (e: any) {
      if (Array.isArray(e?.errorFields)) return
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

        if (applyAccountTaskScopeToBody(body, {
          scope: mode === 'selected' ? 'selected' : 'filtered',
          emptySelectedMessage: '请先选择要上传的账号',
        }) === null) {
          message.destroy(toastKey)
          return
        }

        const endpoint = isSub2Api ? '/tasks/chatgpt/sub2api-upload/batch' : '/tasks/chatgpt/oaipay-upload/batch'
        const taskResult = await postAccountScopeRequest(endpoint, body, toastKey)
        if (!taskResult) return
        if (!isSub2Api) setOaipayUploadModalOpen(false)
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

        if (applyAccountTaskScopeToBody(body, {
          scope: mode === 'selected' ? 'selected' : 'filtered',
          emptySelectedMessage: '请先选择要上传的账号',
          filteredMarker: 'pending_only',
        }) === null) {
          message.destroy(toastKey)
          return
        }

        result = await postAccountScopeRequest('/integrations/backfill', body, toastKey)
        if (!result) return
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

    if (applyAccountTaskScopeToBody(body, {
      scope: scope === 'selected' ? 'selected' : 'filtered',
      emptySelectedMessage: '请先选择要同步的账号',
    }) === null) return

    setStatusSyncLoading(loadingKey)
    message.loading({ content: `${scopeLabel}${actionLabel}进行中...`, key: toastKey, duration: 0 })
    try {
      if (kind === 'probe') {
        const res = await postAccountScopeRequest('/tasks/chatgpt/probe-local-status/batch', body, toastKey)
        if (!res) return
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

      const result = await postAccountScopeRequest(`/actions/${currentPlatform}/${actionId}/batch`, body, toastKey)
      if (!result) return

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

  const buildBackfillLabel = (destination: 'cliproxyapi' | 'sub2api' | 'oaipay') => {
    const scope = getBackfillScope()
    const count = scope === 'selected' ? selectedRowKeys.length : total
    const destinationLabel = destination === 'oaipay' ? 'OAIPay' : destination === 'sub2api' ? 'Sub2API' : 'CLIProxyAPI'
    return scope === 'selected'
      ? `补传所选到 ${destinationLabel} (${count})`
      : `补传 ${destinationLabel} 待补传（筛选 ${count}）`
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
  const updatePinnedToolbarActions = (next: string[]) => {
    const normalized = normalizePinnedToolbarActions(next)
    setPinnedToolbarActionIds(normalized)
    savePinnedToolbarActions(normalized)
  }
  const resetPinnedToolbarActions = () => {
    const defaults = normalizePinnedToolbarActions(DEFAULT_PINNED_ACCOUNT_TOOLBAR_ACTIONS)
    setPinnedToolbarActionIds(defaults)
    savePinnedToolbarActions(defaults)
  }

  const renderToolbarActionVisibilityControl = () => {
    const toolbarActionOptions = toCheckboxOptions(ACCOUNT_TOOLBAR_ACTION_OPTIONS)
    const overlay = (
      <div
        onClick={(event) => event.stopPropagation()}
        style={{
          minWidth: isMobile ? 240 : 300,
          maxWidth: isMobile ? 'calc(100vw - 48px)' : 340,
          padding: 12,
          borderRadius: 8,
          background: token.colorBgElevated,
          boxShadow: token.boxShadowSecondary,
        }}
      >
        <Text type="secondary" style={{ display: 'block', fontSize: 12, marginBottom: 10 }}>
          勾选的操作会直接显示；未勾选的仍可在“更多操作”中使用。危险操作始终收在更多里。
        </Text>
        <Checkbox.Group
          value={pinnedToolbarActionIds}
          options={toolbarActionOptions}
          onChange={(checkedValues) => updatePinnedToolbarActions(checkedValues.map((item) => String(item)))}
          style={{
            display: 'grid',
            gridTemplateColumns: isMobile ? '1fr' : 'repeat(2, minmax(0, 1fr))',
            gap: 8,
          }}
        />
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 12 }}>
          <Button
            size="small"
            onClick={() => updatePinnedToolbarActions(ACCOUNT_TOOLBAR_ACTION_OPTIONS.map((option) => option.value))}
          >
            全部固定
          </Button>
          <Button size="small" onClick={resetPinnedToolbarActions}>
            默认
          </Button>
        </div>
      </div>
    )

    return (
      <Dropdown dropdownRender={() => overlay} trigger={['click']}>
        <Button size="small" icon={<SettingOutlined />}>
          操作显示
        </Button>
      </Dropdown>
    )
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
        <Text type="secondary" style={{ display: 'block', fontSize: 12, marginBottom: 10 }}>
          ID / 邮箱 / 操作固定显示；这里仅控制额外字段。
        </Text>
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
          {isMobile ? '字段' : '显示字段'}
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
          <Text style={subscriptionHistoryTextStyle}>{meta.subLabel}</Text>
        ) : null}
        {meta.refreshTimeLabel ? (
          <Text style={subscriptionHistoryTextStyle}>{meta.refreshTimeLabel}</Text>
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
    const summary = meta.summary
    const title = [
      meta.reason ? `原因：${meta.reason}` : '',
      summary?.marked_at ? `标记：${formatCompactDateTime(String(summary.marked_at))}` : '',
      summary?.submitted_at ? `提交：${formatCompactDateTime(String(summary.submitted_at))}` : '',
      summary?.paid_at ? `完成：${formatCompactDateTime(String(summary.paid_at))}` : '',
      summary?.last_checked_at ? `检查：${formatCompactDateTime(String(summary.last_checked_at))}` : '',
      summary?.order_id ? `order：${summary.order_id}` : '',
      summary?.code_masked ? `卡密：${summary.code_masked}` : '',
    ].filter(Boolean).join('\n')
    return (
      <Space size={[4, 4]} wrap title={title || meta.label}>
        {meta.tags.map((tag, index) => (
          <Tag key={`${tag.label}-${index}`} color={tag.color} style={compactTagStyle}>{tag.label}</Tag>
        ))}
      </Space>
    )
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

  const applyRegistrationSortOrder = useCallback((next: RegistrationSortOrder) => {
    setRegistrationSortOrder(next)
    setCurrentPage(1)
  }, [])

  const handleAccountsTableChange = useCallback((_pagination: any, _filters: Record<string, any>, sorter: any) => {
    const sorterItems = Array.isArray(sorter)
      ? sorter
      : sorter && typeof sorter === 'object' && (sorter.columnKey || sorter.field)
        ? [sorter]
        : []
    const hasRelevantSorter = sorterItems.some((item) => {
      const key = String(item?.columnKey || item?.field || '')
      return key === SUBSCRIPTION_EXPIRY_SORT_FIELD || key === ACCOUNT_CREATED_AT_SORT_FIELD
    })
    if (!hasRelevantSorter) return

    const expirySorter = sorterItems.find((item) => String(item?.columnKey || item?.field || '') === SUBSCRIPTION_EXPIRY_SORT_FIELD)
    const registrationSorter = sorterItems.find((item) => String(item?.columnKey || item?.field || '') === ACCOUNT_CREATED_AT_SORT_FIELD)
    if (expirySorter) {
      const order = String(expirySorter?.order || '')
      setSubscriptionExpirySortOrder(order === 'ascend' ? 'asc' : order === 'descend' ? 'desc' : '')
    }
    if (registrationSorter) {
      const order = String(registrationSorter?.order || '')
      setRegistrationSortOrder(order === 'descend' ? 'desc' : DEFAULT_REGISTRATION_SORT_ORDER)
    }
    setCurrentPage(1)
  }, [])

  const renderMobileFilterControls = () => {
    if (!isMobile) return null
    return (
      <div className="accounts-mobile-filter-grid">
          <Select
            allowClear
            mode="multiple"
            size="small"
            placeholder="使用状态"
            value={columnFilters.manuallyUsed}
            options={toSelectOptions(MANUAL_USE_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({ ...prev, manuallyUsed: value }))}
          />
          <Select
            allowClear
            mode="multiple"
            size="small"
            placeholder="业务状态"
            value={columnFilters.status}
            options={toSelectOptions(STATUS_FILTER_OPTIONS)}
            onChange={(value) => {
              setColumnFilters((prev) => ({ ...prev, status: value }))
              setFilterStatus(value.join(','))
            }}
          />
          <Select
            allowClear
            mode="multiple"
            size="small"
            placeholder="认证材料"
            value={columnFilters.authType}
            options={toSelectOptions(AUTH_TYPE_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({ ...prev, authType: value }))}
          />
          <Select
            allowClear
            mode="multiple"
            size="small"
            placeholder="手机号绑定"
            value={columnFilters.phoneBindingState}
            options={toSelectOptions(PHONE_BINDING_STATE_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({ ...prev, phoneBindingState: value }))}
          />
          <Select
            allowClear
            mode="multiple"
            size="small"
            placeholder="当前链接类型"
            value={columnFilters.paymentLinkPlatform}
            options={toSelectOptions(PAYMENT_LINK_PLATFORM_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({
              ...prev,
              paymentLinkPlatform: normalizePaymentLinkPlatformFilterValues(value),
            }))}
          />
          <Select
            allowClear
            mode="multiple"
            size="small"
            placeholder="提取记录"
            value={columnFilters.paymentLinkGenerated}
            options={toSelectOptions(PAYMENT_LINK_GENERATED_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({
              ...prev,
              paymentLinkGenerated: normalizePaymentLinkGeneratedFilterValues(value),
            }))}
          />
          <Select
            allowClear
            mode="multiple"
            size="small"
            placeholder="当前订阅"
            value={columnFilters.subscriptionType}
            options={toSelectOptions(SUBSCRIPTION_TYPE_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({ ...prev, subscriptionType: value }))}
          />
          <Select
            allowClear
            mode="multiple"
            size="small"
            placeholder="认证状态"
            value={columnFilters.accountValidity}
            options={toSelectOptions(ACCOUNT_VALIDITY_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({ ...prev, accountValidity: value }))}
          />
          <Select
            allowClear
            mode="multiple"
            size="small"
            placeholder="Sub2API"
            value={columnFilters.sub2apiState}
            options={toSelectOptions(SUB2API_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({ ...prev, sub2apiState: normalizeIntegrationUploadFilterValues(value) }))}
          />
          <Select
            allowClear
            mode="multiple"
            size="small"
            placeholder="OAIPay"
            value={columnFilters.oaipayState}
            options={toSelectOptions(OAIPAY_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({ ...prev, oaipayState: normalizeIntegrationUploadFilterValues(value) }))}
          />
          <Select
            allowClear
            mode="multiple"
            size="small"
            placeholder="0 元资格"
            value={columnFilters.zeroAmountEligibilityState}
            options={toSelectOptions(ZERO_AMOUNT_ELIGIBILITY_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({ ...prev, zeroAmountEligibilityState: value }))}
          />
          <Select
            allowClear
            mode="multiple"
            size="small"
            placeholder="GCash 方式"
            value={columnFilters.gcashPaymentMethodState}
            options={toSelectOptions(GCASH_PAYMENT_METHOD_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({ ...prev, gcashPaymentMethodState: value }))}
          />
          <Select
            allowClear
            mode="multiple"
            size="small"
            placeholder="提交状态"
            value={columnFilters.submitState}
            options={toSelectOptions(SUBMISSION_STATE_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({ ...prev, submitState: value }))}
          />
          <Select
            allowClear
            size="small"
            placeholder="提交记录"
            value={columnFilters.hasSubmitted[0] || undefined}
            options={toSelectOptions(HAS_SUBMITTED_FILTER_OPTIONS)}
            onChange={(value) => setColumnFilters((prev) => ({ ...prev, hasSubmitted: value ? [String(value)] : [] }))}
          />
          <Select
            allowClear
            size="small"
            placeholder="到期排序"
            value={subscriptionExpirySortOrder || undefined}
            options={toSelectOptions(SUBSCRIPTION_EXPIRY_SORT_OPTIONS)}
            onChange={(value) => applySubscriptionExpirySortOrder((value || '') as SubscriptionExpirySortOrder)}
          />
          <Select
            size="small"
            placeholder="注册时间排序"
            value={registrationSortOrder}
            options={toSelectOptions(REGISTRATION_SORT_OPTIONS)}
            onChange={(value) => applyRegistrationSortOrder((value || DEFAULT_REGISTRATION_SORT_ORDER) as RegistrationSortOrder)}
          />
      </div>
    )
  }

  const renderSub2ApiState = (record: any) => {
    const sync = record.sub2apiSync || {}
    const meta = integrationUploadStateMeta(
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
    const meta = integrationUploadStateMeta(sync)
    return <Tag color={meta.color} style={compactTagStyle}>{meta.label}</Tag>
  }

  const renderPaymentEligibilityState = (record: any) => {
    const zero = record?.zero_amount_eligibility || record?.zeroAmountEligibility || {}
    const gcash = record?.gcash_payment_method || record?.gcashPaymentMethod || {}
    const zeroState = String(zero.state || zero.confirmed_state || 'unknown').trim().toLowerCase()
    const gcashState = String(gcash.state || gcash.confirmed_state || 'unknown').trim().toLowerCase()
    const zeroMeta = zeroState === 'eligible'
      ? { color: 'success', label: '0 元可用' }
      : zeroState === 'ineligible'
        ? { color: 'warning', label: '非 0 元' }
        : { color: 'default', label: '0 元未检' }
    const gcashMeta = gcashState === 'available'
      ? { color: 'success', label: 'GCash 可用' }
      : gcashState === 'unavailable'
        ? { color: 'warning', label: 'GCash 不可用' }
        : { color: 'default', label: 'GCash 未检' }
    const zeroTitle = String(zero.reason_code || zero.last_attempt_state || '').trim()
    const gcashTitle = String(gcash.reason_code || gcash.last_attempt_state || '').trim()
    return (
      <Space size={4} wrap>
        <Tag color={zeroMeta.color} style={compactTagStyle} title={zeroTitle || undefined}>{zeroMeta.label}</Tag>
        <Tag color={gcashMeta.color} style={compactTagStyle} title={gcashTitle || undefined}>{gcashMeta.label}</Tag>
      </Space>
    )
  }

  const renderPaymentLinkState = (record: any) => {
    const link = record?.paymentLink && typeof record.paymentLink === 'object'
      ? record.paymentLink
      : record?.payment_link && typeof record.payment_link === 'object'
        ? record.payment_link
        : record?.chatgptLastPaymentLink && typeof record.chatgptLastPaymentLink === 'object'
          ? record.chatgptLastPaymentLink
          : record?.extra?.chatgpt_last_payment_link && typeof record.extra.chatgpt_last_payment_link === 'object'
            ? record.extra.chatgpt_last_payment_link
            : {}
    const url = String(link.url || '').trim()
    const status = String(link.link_status || '').trim().toLowerCase()
    const format = String(link.payment_link_format || '').trim().toLowerCase()
    const platform = String(record?.paymentLinkPlatform || record?.payment_link_platform || link.platform || '').trim().toLowerCase()
    const generated = hasPaymentLinkSuccessEvidence(record, link)
    const platformLabel = ({
      hosted: 'HOSTED',
      pix: 'PIX',
      upi: 'UPI',
      paypal: 'PAYPAL',
      ideal: 'iDEAL',
      twint: 'TWINT',
      kakao_pay: 'KAKAO PAY',
      team: 'TEAM',
      other: '其他',
    } as Record<string, string>)[platform]
    const storedLinkType = String(link.link_type || '').trim().toUpperCase()
    const productLabel = String(link.generation_kind || '').trim().toLowerCase() === 'team_checkout'
      || String(link.plan || '').trim().toLowerCase() === 'team'
      ? 'TEAM'
      : ''
    const checkoutProxyRegion = String(link.checkout_proxy_region || '').trim().toUpperCase()
    const linkType = platformLabel || storedLinkType
      || (format === 'paypal_url' ? 'PAYPAL' : format === 'long_link' ? 'LONG-LINK' : '')
    const generatedAt = formatCompactDateTime(String(link.generated_at || link.created_at || '').trim())
    const cleanedAt = formatCompactDateTime(String(link.cleaned_at || link.link_status_updated_at || '').trim())
    const cleanedStatusMeta = PAYMENT_LINK_CLEANED_STATUS_META[status]
    const statusMeta = (() => {
      if (cleanedStatusMeta) return cleanedStatusMeta
      if (status === 'invalid' || status === 'precheck_failed') return { color: 'error', label: status === 'invalid' ? '无效' : '校验失败' }
      if (status === 'already_paid' || status === 'paid') return { color: 'success', label: '已支付' }
      if (status === 'leased') return { color: 'processing', label: '已领取' }
      if (status) return { color: 'default', label: status }
      if (url) return { color: 'success', label: '已生成' }
      return { color: generated ? 'success' : 'default', label: generated ? '已成功提取' : '尚未提取' }
    })()
    const displayTime = cleanedStatusMeta ? (cleanedAt || generatedAt) : generatedAt
    const displayTimeLabel = cleanedStatusMeta ? '清理时间' : '生成时间'
    const statusTitle = String(link.link_status_reason || '').trim()
    const accountId = Number(record?.id || 0)
    const paymentLinkCopied = Boolean(url && accountId > 0 && copiedPaymentLinkUrlsByAccountId.get(accountId) === url)

    if (!url && !status && !generated) return <Text type="secondary" style={{ fontSize: 11 }}>尚未提取</Text>

    return (
      <Space direction="vertical" size={2} style={{ maxWidth: 172 }}>
        <Space size={5} wrap>
          {productLabel ? <Tag color="gold" style={compactTagStyle}>{productLabel}</Tag> : null}
          {productLabel && checkoutProxyRegion ? <Tag style={compactTagStyle}>{`IP ${checkoutProxyRegion}`}</Tag> : null}
          {linkType ? <Tag color="blue" style={compactTagStyle}>{linkType}</Tag> : null}
          <Tag color={statusMeta.color} style={compactTagStyle} title={statusTitle || undefined}>{statusMeta.label}</Tag>
        </Space>
        {displayTime ? (
          <Text type="secondary" style={{ fontSize: 11 }} title={`${displayTimeLabel}: ${displayTime.title}`}>
            {displayTime.compact}
          </Text>
        ) : null}
        {url ? (
          <Space size={0}>
            <Button
              title={paymentLinkCopied ? '已复制支付链接' : '复制支付链接'}
              aria-label={paymentLinkCopied ? '已复制支付链接' : '复制支付链接'}
              type="text"
              size="small"
              icon={paymentLinkCopied ? <CheckOutlined /> : <CopyOutlined />}
              style={paymentLinkCopied ? {
                color: token.colorWarningText,
                background: token.colorWarningBg,
                boxShadow: `inset 0 0 0 1px ${token.colorWarningBorder}`,
              } : undefined}
              onClick={(event) => {
                event.stopPropagation()
                void copyPaymentLink(record, url)
              }}
            />
            <Button
              title="打开支付链接"
              type="text"
              size="small"
              icon={<LinkOutlined />}
              onClick={(event) => {
                event.stopPropagation()
                window.open(url, '_blank', 'noopener,noreferrer')
              }}
            />
          </Space>
        ) : null}
      </Space>
    )
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
    secondary?: {
      primaryLabel?: string
      label: string
      values: string[]
      options: Array<{ value: string; text: string }>
      onChange: (next: string[]) => void
    },
  ) => {
    const selectedCount = values.length + (secondary?.values.length || 0)
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
        {secondary?.primaryLabel ? (
          <Text type="secondary" style={{ display: 'block', marginBottom: 6, fontSize: 12 }}>
            {secondary.primaryLabel}
          </Text>
        ) : null}
        <Checkbox.Group
          value={values}
          options={toCheckboxOptions(options)}
          onChange={(checkedValues) => onChange(checkedValues.map((item) => String(item)))}
          style={{ display: 'grid', gap: 8 }}
        />
        {secondary ? (
          <>
            <Text type="secondary" style={{ display: 'block', marginTop: 10, marginBottom: 6, fontSize: 12 }}>
              {secondary.label}
            </Text>
            <Checkbox.Group
              value={secondary.values}
              options={toCheckboxOptions(secondary.options)}
              onChange={(checkedValues) => secondary.onChange(checkedValues.map((item) => String(item)))}
              style={{ display: 'grid', gap: 8 }}
            />
          </>
        ) : null}
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 10 }}>
          <Button
            size="small"
            onClick={() => {
              onChange([])
              secondary?.onChange([])
            }}
          >
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
    const webSessionLoginAction = commonActions.find((action: any) => String(action?.id || '').trim().toLowerCase() === 'web_session_login')
    const invalidRecheckAction = commonActions.find((action: any) => String(action?.id || '').trim().toLowerCase() === 'invalid_recheck')
    const hiddenIds = new Set([
      paymentLinkAction ? String(paymentLinkAction.id) : '',
      webSessionLoginAction ? String(webSessionLoginAction.id) : '',
      invalidRecheckAction ? String(invalidRecheckAction.id) : '',
      'probe_local_status',
      'resume_subscription_auth',
    ].filter(Boolean))
    const moreActions = commonActions.filter((action: any) => !hiddenIds.has(String(action?.id || '')))

    return [
      { key: '__detail__', label: '账号详情' },
      ...(paymentLinkAction ? [{ key: '__payment_link_config__', label: '支付链接生成' }] : []),
      ...(paymentLinkAction ? [{ key: '__payment_link_regenerate__', label: '强制重新生成' }] : []),
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
      handleBatchPaymentLink({ account: record })
      return
    }
    if (String(key) === '__payment_link_regenerate__') {
      handleBatchPaymentLink({ account: record, forceRefresh: true })
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
            <Button size="small" style={mobileActionButtonStyle(accountActionTextStyles.payment)} onClick={() => handleBatchPaymentLink({ account: record })}>
              支付链接生成
            </Button>
          ) : null}
          <Button size="small" style={mobileActionButtonStyle(accountActionTextStyles.refresh)} onClick={() => openAccountProbeStatusAction(record)}>
            刷新状态
          </Button>
          <Button
            size="small"
            icon={<LoginOutlined />}
            style={mobileActionButtonStyle(accountActionTextStyles.resume)}
            onClick={() => handleWebSessionLogin(record)}
          >
            执行登录态
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
          {showInvalidRecheck ? (
            <Button size="small" style={mobileActionButtonStyle(accountActionTextStyles.resume)} onClick={() => handleInvalidRecheck(record)}>
              失效测活
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
              onClick={() => handleBatchPaymentLink({ account: record })}
            >
              支付链接生成
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
          <Button
            type="link"
            size="small"
            icon={<LoginOutlined />}
            style={accountActionTextStyles.resume}
            onClick={() => handleWebSessionLogin(record)}
          >
            执行登录态
          </Button>
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
      isColumnVisible('subscription_type') ? renderMobileStatusPill(
        'subscription_type',
        subscriptionMetaForMobile.label,
        subscriptionMetaForMobile.color,
        subscriptionMetaForMobile.subLabel || subscriptionMetaForMobile.refreshTimeLabel ? (
          <span
            title={subscriptionMetaForMobile.title}
            style={{ display: 'inline-flex', flexDirection: 'column', lineHeight: '15px', whiteSpace: 'nowrap' }}
          >
            {subscriptionMetaForMobile.subLabel ? <Text style={subscriptionHistoryTextStyle}>{subscriptionMetaForMobile.subLabel}</Text> : null}
            {subscriptionMetaForMobile.refreshTimeLabel ? <Text style={subscriptionHistoryTextStyle}>{subscriptionMetaForMobile.refreshTimeLabel}</Text> : null}
          </span>
        ) : undefined,
      ) : null,
      isColumnVisible('account_validity') ? renderMobileStatusPill('account_validity', validityMetaForMobile.label, validityMetaForMobile.color) : null,
      isColumnVisible('idea_submit_status') ? (
        <span key="idea_submit_status" style={{ display: 'inline-flex', alignItems: 'center', gap: 4, flexWrap: 'wrap' }}>
          {ideaMetaForMobile.tags.map((tag, index) => (
            <Tag key={`${tag.label}-${index}`} color={tag.color} style={mobileStatusPillStyle}>{tag.label}</Tag>
          ))}
        </span>
      ) : null,
      isChatgptPlatform && isColumnVisible('payment_eligibility') ? (
        <span key="payment_eligibility" style={{ display: 'inline-flex', alignItems: 'center', gap: 4, flexWrap: 'wrap' }}>
          {renderPaymentEligibilityState(record)}
        </span>
      ) : null,
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
      isChatgptPlatform && isColumnVisible('payment_link') ? renderPaymentLinkState(record) : null,
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
  const selectedAccountPreviewItems = selectedAccountItems.slice(0, 120)

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

  const selectedAccountTags = (
    <div className="accounts-selected-account-tags">
      {selectedAccountPreviewItems.map((account) => {
        const id = String(account?.id || '')
        const email = String(account?.email || '').trim()
        const status = String(account?.status || '').trim()
        const title = email || `账号 ${id}`
        return (
          <Tag
            key={id}
            closable
            onClose={(event) => {
              event.preventDefault()
              if (id) removeSelectedAccount(id)
            }}
            color={STATUS_COLORS[status] || 'default'}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 4,
              maxWidth: '100%',
              marginInlineEnd: 0,
              padding: '2px 6px',
            }}
          >
            <span className="accounts-selected-account-tag-email" title={title}>
              {title}
            </span>
            <Text type="secondary" style={{ fontSize: 11 }}>
              ID {id}{status ? ` · ${statusLabel(status)}` : ''}
            </Text>
          </Tag>
        )
      })}
      {selectedAccountItems.length > selectedAccountPreviewItems.length ? (
        <Text type="secondary">另有 {selectedAccountItems.length - selectedAccountPreviewItems.length} 个账号</Text>
      ) : null}
      {selectedAccountItems.length === 0 ? <Text type="secondary">暂无选中账号</Text> : null}
    </div>
  )

  const selectedAccountsControl = (
    <div className="accounts-selected-summary">
      <Text strong style={{ fontSize: 13 }}>总数：{total}</Text>
      <Text type="secondary">/</Text>
      {selectedAccountItems.length > 0 ? (
        <>
          <Popover content={selectedAccountTags} title="已选账号列表" trigger={['click']} placement={isMobile ? 'bottom' : 'bottomLeft'}>
            <Tag color="processing" className="accounts-selected-summary-count">已选：{selectedAccountItems.length}</Tag>
          </Popover>
          <Button size="small" type="link" onClick={clearSelectedAccounts} className="accounts-selected-summary-clear">
            清空选择
          </Button>
        </>
      ) : (
        <Text strong style={{ fontSize: 13 }}>已选：0</Text>
      )}
    </div>
  )

  const renderEmailColumnTitle = () => {
    if (isMobile) return '邮箱'
    return (
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
    )
  }



  const subscriptionExpiryTableSortOrder =
    subscriptionExpirySortOrder === 'asc'
      ? 'ascend'
      : subscriptionExpirySortOrder === 'desc'
        ? 'descend'
        : null

  const registrationTableSortOrder = registrationSortOrder === 'desc' ? 'descend' : 'ascend'



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
      title: renderEmailColumnTitle(),
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
      title: renderColumnFilterTitle(
        '手机号/API',
        columnFilters.phoneBindingState,
        PHONE_BINDING_STATE_FILTER_OPTIONS,
        (next) => setColumnFilters((prev) => ({ ...prev, phoneBindingState: next })),
      ),
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
        sorter: { multiple: 2 },
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
          '提交状态',
          columnFilters.submitState,
          SUBMISSION_STATE_FILTER_OPTIONS,
          (next) => setColumnFilters((prev) => ({ ...prev, submitState: next })),
          {
            label: '提交记录',
            values: columnFilters.hasSubmitted,
            options: HAS_SUBMITTED_FILTER_OPTIONS,
            onChange: (next) => setColumnFilters((prev) => ({ ...prev, hasSubmitted: next })),
          },
        ),
        key: 'idea_submit_status',
        width: 128,
        render: (_: any, record: any) => renderIdeaSubmitState(record),
      },
      {
        title: renderColumnFilterTitle(
          '支付资格',
          columnFilters.zeroAmountEligibilityState,
          ZERO_AMOUNT_ELIGIBILITY_FILTER_OPTIONS,
          (next) => setColumnFilters((prev) => ({ ...prev, zeroAmountEligibilityState: next })),
          {
            primaryLabel: '0 元试用资格',
            label: 'GCash 支付方式',
            values: columnFilters.gcashPaymentMethodState,
            options: GCASH_PAYMENT_METHOD_FILTER_OPTIONS,
            onChange: (next) => setColumnFilters((prev) => ({ ...prev, gcashPaymentMethodState: next })),
          },
        ),
        key: 'payment_eligibility',
        width: 190,
        render: (_: any, record: any) => renderPaymentEligibilityState(record),
      },
      {
        title: renderColumnFilterTitle(
          '支付链接',
          columnFilters.paymentLinkPlatform,
          PAYMENT_LINK_PLATFORM_FILTER_OPTIONS,
          (next) => setColumnFilters((prev) => ({
            ...prev,
            paymentLinkPlatform: normalizePaymentLinkPlatformFilterValues(next),
          })),
          {
            primaryLabel: '当前链接类型',
            label: '提取记录',
            values: columnFilters.paymentLinkGenerated,
            options: PAYMENT_LINK_GENERATED_FILTER_OPTIONS,
            onChange: (next) => setColumnFilters((prev) => ({
              ...prev,
              paymentLinkGenerated: normalizePaymentLinkGeneratedFilterValues(next),
            })),
          },
        ),
        key: 'payment_link',
        width: 184,
        render: (_: any, record: any) => renderPaymentLinkState(record),
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
          (next) => setColumnFilters((prev) => ({ ...prev, sub2apiState: normalizeIntegrationUploadFilterValues(next) })),
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
          (next) => setColumnFilters((prev) => ({ ...prev, oaipayState: normalizeIntegrationUploadFilterValues(next) })),
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
      dataIndex: ACCOUNT_CREATED_AT_SORT_FIELD,
      key: ACCOUNT_CREATED_AT_SORT_FIELD,
      width: 76,
      sorter: { multiple: 1 },
      sortOrder: registrationTableSortOrder,
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
    : total === 0
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
  const eligibilityScope = selectedRowKeys.length > 0 ? 'selected' : 'filtered'
  const eligibilityScopeCount = eligibilityScope === 'selected' ? selectedRowKeys.length : total
  const eligibilityMenuItems: MenuProps['items'] = [
    {
      key: 'zero_amount_eligibility',
      label: `批量检测 0 元试用资格（${eligibilityScope === 'selected' ? '所选' : '当前筛选'} ${eligibilityScopeCount}）`,
      disabled: paymentEligibilityLoading || eligibilityScopeCount <= 0,
    },
    {
      key: 'gcash_payment_method',
      label: `批量检测 GCash 支付方式（${eligibilityScope === 'selected' ? '所选' : '当前筛选'} ${eligibilityScopeCount}）`,
      disabled: paymentEligibilityLoading || eligibilityScopeCount <= 0,
    },
  ]

  const renderBatchGopayItem = (item: BatchGopayItem) => {
    const phase = String(item.snapshot?.phase || (item.status === 'queued' ? 'created' : item.status))
    const meta = gopayPhaseMeta(phase)
    const needsInput = phase === 'waiting_otp' || phase === 'waiting_link_pin' || phase === 'waiting_payment_pin'
    const canResendOtp = phase === 'waiting_otp' && Boolean(item.snapshot?.session_id)
    const isTerminal = ['done', 'failed', 'cancelled', 'stopped'].includes(item.status) || ['succeeded', 'failed', 'cancelled', 'stopped'].includes(phase)
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
            <Tag color={isTerminal ? (phase === 'succeeded' || item.status === 'done' ? 'success' : ['cancelled', 'stopped'].includes(item.status) ? 'default' : 'error') : 'processing'}>
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
  const phoneBindingUnavailableNumberTestEnabled = phoneBindingPoolMode === 'unavailable_numbers'
  const phoneBindingPrefixModeActive = phoneBindingPrefixBindEnabled || phoneBindingPrefixSampleEnabled
  const phoneBindingPrefixNumberFilter = normalizePhoneNumberFilter(phoneBindingNumberFilterValue)
  const phoneBindingPrefixNumberFilterLabel = phoneBindingPrefixNumberFilter === 'unavailable'
    ? '不可用'
    : phoneBindingPrefixNumberFilter === 'all'
      ? '全部'
      : '可用'
  const phoneBindingFixedSnapshotSelection = phoneBindingPrefixSampleEnabled
    || phoneBindingUnavailableNumberTestEnabled
    || (phoneBindingPrefixBindEnabled && phoneBindingPrefixNumberFilter !== 'available')
  const phoneBindingSelectedPrefixes = normalizeSelectedPrefixes(phoneBindingSelectedPrefixesValue)
  const phoneBindingProxyMode = String(phoneBindingProxyModeValue || DEFAULT_PHONE_BINDING_SETTINGS.proxy_mode)
  const invalidRecheckProxyMode = String(invalidRecheckProxyModeValue || 'dynamic')
  const invalidRecheckProxyFailover = Boolean(invalidRecheckProxyFailoverValue)
  const webSessionLoginProxyMode = String(webSessionLoginProxyModeValue || 'dynamic')
  const webSessionLoginProxyFailover = Boolean(webSessionLoginProxyFailoverValue)
  const phoneBindingPrefixSampleSize = Number(phoneBindingPrefixSampleSizeValue) === 2 ? 2 : 1
  const phoneBindingPrefixSampleFilter = (() => {
    const value = String(phoneBindingPrefixSampleFilterValue || 'all')
    if (value === 'available') return 'available'
    if (value === 'rejected') return 'rejected'
    return 'all'
  })()
  const phoneBindingUsePool = phoneBindingPrefixModeActive || phoneBindingUnavailableNumberTestEnabled || phoneBindingUsePoolValue !== false
  const phoneBindingManualText = phoneBindingPoolMode === 'normal' ? String(phoneBindingPhoneLinesValue || '').trim() : ''
  const phoneBindingShowManualInput = phoneBindingPoolMode === 'normal' && (phoneBindingManualOpen || !phoneBindingUsePool || Boolean(phoneBindingManualText))
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
        items: uniquePhonePoolPrefixItems(health.available || summary.healthy_prefixes || summary.available_prefixes, 'available'),
      },
      {
        key: 'partial' as const,
        label: '部分可用',
        color: 'cyan',
        items: uniquePhonePoolPrefixItems(health.partial || summary.partial_prefixes, 'partial'),
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
  const phoneBindingLimitedUnavailablePhones = phoneBindingSelectedPrefixItems.reduce((sum, item) => sum + Number(item.cannot_send_count || 0), 0)
  const phoneBindingLimitedRemainingCapacity = phoneBindingSelectedPrefixItems.reduce((sum, item) => sum + Number(item.remaining_capacity || 0), 0)
  const phoneBindingLimitedCapacity = phoneBindingReusePhoneValue ? phoneBindingLimitedRemainingCapacity : phoneBindingLimitedAvailablePhones
  const phoneBindingLimitedCandidatePhones = phoneBindingPrefixNumberFilter === 'unavailable'
    ? phoneBindingLimitedUnavailablePhones
    : phoneBindingPrefixNumberFilter === 'all'
      ? phoneBindingLimitedAvailablePhones + phoneBindingLimitedUnavailablePhones
      : phoneBindingLimitedAvailablePhones
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
  const phoneBindingManualCandidateCount = phoneBindingManualText
    ? phoneBindingManualText.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).length
    : 0
  const phoneBindingPoolCandidatePhoneCount = phoneBindingPrefixSampleEnabled
    ? phoneBindingSummarySampleCount
    : phoneBindingPrefixBindEnabled
      ? phoneBindingLimitedCandidatePhones
      : phoneBindingUnavailableNumberTestEnabled
        ? phoneBindingSummaryUnavailable
        : phoneBindingSummaryAvailable
  const phoneBindingCandidatePhoneCount = phoneBindingManualText
    ? phoneBindingManualCandidateCount
    : phoneBindingUsePool
      ? phoneBindingPoolCandidatePhoneCount
      : 0
  const phoneBindingDynamicCapacity = phoneBindingPrefixBindEnabled
    ? phoneBindingLimitedCapacity
    : phoneBindingReusePhoneValue
      ? phoneBindingSummaryRemaining
      : phoneBindingSummaryAvailable
  const phoneBindingExpectedTestCount = phoneBindingManualText || phoneBindingFixedSnapshotSelection
    ? Math.min(phoneBindingTargetCount, phoneBindingCandidatePhoneCount)
    : phoneBindingUsePool
      ? Math.min(phoneBindingTargetCount, phoneBindingDynamicCapacity)
      : 0
  const phoneBindingUncoveredPhoneCount = Math.max(phoneBindingCandidatePhoneCount - phoneBindingExpectedTestCount, 0)
  const updatePhoneBindingSettings = (patch: Record<string, unknown>) => {
    const nextValues = {
      ...phoneBindingTestForm.getFieldsValue(true),
      ...patch,
    }
    phoneBindingTestForm.setFieldsValue(patch)
    savePhoneBindingSettings(nextValues)
  }
  const setPhoneBindingSelectedPrefixes = (nextPrefixes: unknown) => {
    const selected = normalizeSelectedPrefixes(nextPrefixes)
    updatePhoneBindingSettings({ selected_prefixes: selected })
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
    const cannotSend = Number(item.cannot_send_count || 0)
    const rejected = Number(item.rejected_count || 0)
    if (available > 0) parts.push(`可用 ${available}`)
    if (remaining > 0) parts.push(`容量 ${remaining}`)
    if (cannotSend > 0) parts.push(`不可用 ${cannotSend}`)
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
        ? phoneBindingSelectedPrefixes.length > 0 && phoneBindingLimitedCandidatePhones <= 0
        : phoneBindingUnavailableNumberTestEnabled
          ? phoneBindingSummaryUnavailable <= 0
          : phoneBindingDynamicCapacity <= 0)
  const phoneBindingPoolShortage =
    phoneBindingUsePool
    && phoneBindingManualText === ''
    && phoneBindingTargetCount > 0
    && phoneBindingCandidatePhoneCount > 0
    && phoneBindingExpectedTestCount < phoneBindingTargetCount
  const baxiIsPix = baxiPaymentChannelValue === 'pix'
  const baxiPixUsesSavedLink = baxiIsPix && baxiPixSubmitModeValue === 'user_link'
  const baxiCdkManualText = baxiIsPix ? '' : String(baxiCdkCodeLinesValue || '').trim()
  const baxiCdkUsePool = !baxiIsPix && !baxiCdkManualText && baxiCdkUsePoolValue !== false
  const baxiCdkShowManualInput = baxiCdkManualOpen || Boolean(baxiCdkManualText) || !baxiCdkUsePool
  const baxiCdkSummary = baxiCdkPoolSummary || {}
  const baxiCdkTargetCount = baxiCdkSubmitScope === 'selected' ? selectedRowKeys.length : total
  const baxiCdkTargetSuccessLimit = Math.max(Number(baxiCdkTargetSuccessValue || 0), 0)
  const baxiCdkPlannedSuccessTarget = baxiCdkTargetSuccessLimit > 0 && baxiCdkTargetCount > 0
    ? Math.min(baxiCdkTargetSuccessLimit, baxiCdkTargetCount)
    : baxiCdkTargetCount
  const baxiCdkAvailable = Number(baxiCdkSummary.submit_candidates ?? baxiCdkSummary.available ?? 0)
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

  void [
    batchGopayLoading,
    batchGopayPhoneSaving,
    batchGopayStopMode,
    setBatchGopayRoundInterval,
    batchGopayOtpDelaySaving,
    batchGopayNextRoundAt,
    deleteBatchGopayPhone,
    moveBatchGopayPhone,
    openBatchGopayWorkbench,
    addBatchGopayPhoneToPool,
    updateBatchGopayPhoneNumberInput,
    startBatchGopay,
    cancelBatchGopayAll,
    stopBatchGopayAfterCurrent,
    renderBatchGopayItem,
  ]

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
        pinnedActionIds={pinnedToolbarActionIds}
        selectedAccountsControl={selectedAccountsControl}
        columnVisibilityControl={renderColumnVisibilityControl()}
        toolbarActionVisibilityControl={renderToolbarActionVisibilityControl()}
        activeTasksLoading={activeTasksLoading}
        activeTasks={activeTasks}
        onOpenTaskSnapshot={openTaskFromSnapshot}
        onRefreshActiveTasks={refreshActiveTasks}
        onActiveTasksOpen={() => setActiveTasksPanelOpen(true)}
        isChatgptPlatform={currentPlatform === 'chatgpt'}
        batchPaymentLinkLoading={batchPaymentLinkLoading}
        pixLinkCleanupLoading={pixLinkCleanupLoading || pixLinkScanLoading}
        webSessionLoginLoading={webSessionLoginLoading}
        batchInvalidRecheckLoading={batchInvalidRecheckLoading}
        paymentEligibilityLoading={paymentEligibilityLoading}
        phoneBindingTestLoading={phoneBindingTestLoading}
        paypalBindingLoading={paypalBindingLoading}
        baxiCdkSubmitLoading={baxiCdkSubmitLoading}
        onBatchPaymentLink={handleBatchPaymentLink}
        onScanPixLinks={() => { void loadPixLinkScan() }}
        onBatchWebSessionLogin={handleBatchWebSessionLogin}
        onBatchInvalidRecheck={handleBatchInvalidRecheck}
        eligibilityMenuItems={eligibilityMenuItems}
        onPaymentEligibilityClick={({ key }) => {
          const kind = String(key || '').trim() as 'zero_amount_eligibility' | 'gcash_payment_method'
          if (kind === 'zero_amount_eligibility' || kind === 'gcash_payment_method') {
            void handleBatchPaymentEligibility(kind)
          }
        }}
        onOpenPhoneBindingTest={() => { void openPhoneBindingTest() }}
        onOpenPaypalBinding={openPaypalBinding}
        onOpenBaxiCdkSubmit={openBaxiCdkSubmit}
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
          const [kind, scopeKey] = rawKey.split(':')
          const scope = scopeKey as 'selected' | 'all'
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
        search={search}
        onSearchChange={(value) => {
          setSearch(value)
          setColumnFilters((prev) => ({ ...prev, email: value }))
        }}
        onSearchSubmit={(value) => {
          const next = String(value || '').trim()
          setSearch(next)
          setColumnFilters((prev) => ({ ...prev, email: next }))
          setDebouncedSearch(next)
        }}
        filterPresetLoading={filterPresetLoading}
        activeFilterPresetId={activeFilterPresetId}
        filterPresets={filterPresets}
        pinnedFilterPresets={pinnedFilterPresets}
        activeFilterPreset={activeFilterPreset}
        fixedGroups={currentParentFixedGroups}
        pinnedFixedGroups={pinnedFixedGroups}
        activeFixedGroupId={activeFixedGroupId}
        secondaryScope={secondaryFilterScope}
        currentFilterPresetFilters={currentFilterPresetFilters}
        activeFilterPresetDirty={activeFilterPresetDirty}
        filterPresetSaving={filterPresetSaving}
        applyFilterPreset={applyFilterPreset}
        applyUnassignedScope={applyUnassignedScope}
        applyFixedGroup={applyFixedGroup}
        clearFilterPreset={clearFilterPreset}
        openCreateCurrentFilterPreset={openCreateCurrentFilterPreset}
        openCreateFixedGroup={openCreateFixedGroup}
        setFilterPresetManageOpen={setFilterPresetManageOpen}
        loadFilterPresets={loadFilterPresets}
        overwriteActiveFilterPreset={overwriteActiveFilterPreset}
        openCopyFilterPreset={openCopyFilterPreset}
        selectedAccountCount={selectedRowKeys.length}
        selectedAccountsControl={selectedAccountsControl}
        mobileFilterControls={renderMobileFilterControls()}
      />

      <AccountsTable
        columns={visibleColumns}
        accounts={accounts}
        loading={loading}
        total={total}
        currentPage={currentPage}
        pageSize={accountsPageSize}
        defaultPageSize={defaultAccountsPageSize}
        onPageChange={setCurrentPage}
        onPageSizeChange={handleAccountsPageSizeChange}
        onDefaultPageSizeChange={handleDefaultAccountsPageSizeChange}
        pageSizeOptions={accountsPageSizeOptions}
        customPageSizeOptions={customAccountsPageSizeOptions}
        minPageSize={MIN_ACCOUNTS_PAGE_SIZE}
        maxPageSize={MAX_ACCOUNTS_PAGE_SIZE}
        onPageSizeOptionAdd={handleAccountsPageSizeChange}
        onPageSizeOptionRemove={removeCustomAccountsPageSizeOption}
        selectedRowKeys={selectedRowKeys}
        setSelectedRowKeys={setSelectedRowKeys}
        onTableChange={handleAccountsTableChange}
        isMobile={isMobile}
        renderMobileCard={renderAccountMobileCard}
        onOpenDetail={(record) => {
          setDetailAccount(record)
          setDetailModalOpen(true)
        }}
      />

      <Modal
        title={filterPresetEditorMode === 'edit-meta'
          ? filterPresetContentMode === 'fixed' ? '编辑固定账号组合' : '编辑条件筛选组合'
          : filterPresetEditorMode === 'copy-preset'
            ? '复制条件筛选组合'
            : filterPresetContentMode === 'fixed' ? '新建固定账号组合' : '保存条件筛选组合'}
        open={filterPresetEditorOpen}
        onOk={() => { void saveFilterPresetForm() }}
        onCancel={() => setFilterPresetEditorOpen(false)}
        okText={filterPresetEditorMode === 'edit-meta' ? '更新组合' : '创建组合'}
        confirmLoading={filterPresetSaving}
        destroyOnClose
        width={700}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Form form={filterPresetForm} layout="vertical" preserve={false}>
            <Form.Item name="mode" hidden><Input /></Form.Item>
            <Form.Item name="parent_preset_id" hidden><Input /></Form.Item>
            <Form.Item
              name="name"
              label="组合名称"
              rules={[{ required: true, message: '请输入筛选组合名称' }, { max: 80, message: '名称最多 80 个字符' }]}
            >
              <Input placeholder={filterPresetContentMode === 'fixed' ? '例如：固定50' : '例如：Plus 长效未上传 OAIPay'} />
            </Form.Item>
            <Form.Item name="description" label="描述" rules={[{ max: 240, message: '描述最多 240 个字符' }]}>
              <Input.TextArea rows={2} placeholder="说明这组条件的用途，便于后续区分" />
            </Form.Item>
            <Form.Item name="pinned" valuePropName="checked" style={{ marginBottom: 12 }}>
              <Checkbox>置顶到账号页快捷入口</Checkbox>
            </Form.Item>
            {filterPresetContentMode === 'fixed' ? (
              <div
                style={{
                  padding: 14,
                  borderRadius: 8,
                  background: token.colorFillAlter,
                  border: `1px solid ${token.colorBorderSecondary}`,
                }}
              >
                <div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'space-between', alignItems: 'center', gap: 8, marginBottom: 10 }}>
                  <Space size={6} wrap>
                    <Text strong style={{ fontSize: 13 }}>固定账号成员</Text>
                    <Tag color={filterPresetEditorAccountIds.length > 0 ? 'processing' : 'error'} style={{ marginInlineEnd: 0 }}>
                      {filterPresetEditorAccountIds.length} 个
                    </Tag>
                  </Space>
                  <Button
                    size="small"
                    icon={<SyncOutlined />}
                    onClick={() => {
                      const accountIds = normalizeAccountIds(selectedRowKeys)
                      if (accountIds.length === 0) {
                        message.warning('当前没有选中账号')
                        return
                      }
                      setFilterPresetEditorAccountIds(accountIds)
                    }}
                  >
                    使用当前已选 ({selectedRowKeys.length})
                  </Button>
                </div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, maxHeight: 220, overflowY: 'auto' }}>
                  {filterPresetEditorAccountIds.slice(0, 80).map((accountId) => {
                    const account = selectedAccountSnapshots[String(accountId)]
                      || accounts.find((item) => Number(item?.id) === accountId)
                    const email = String(account?.email || '').trim()
                    return (
                      <Tag
                        key={accountId}
                        closable
                        onClose={(event) => {
                          event.preventDefault()
                          setFilterPresetEditorAccountIds((ids) => ids.filter((id) => id !== accountId))
                        }}
                        style={{ marginInlineEnd: 0, maxWidth: '100%' }}
                      >
                        <span
                          title={email || `ID ${accountId}`}
                          style={{ display: 'inline-block', maxWidth: isMobile ? 220 : 500, overflow: 'hidden', textOverflow: 'ellipsis', verticalAlign: 'bottom' }}
                        >
                          {email ? `${email} · ` : ''}ID {accountId}
                        </span>
                      </Tag>
                    )
                  })}
                  {filterPresetEditorAccountIds.length > 80 ? (
                    <Tag style={{ marginInlineEnd: 0 }}>另有 {filterPresetEditorAccountIds.length - 80} 个</Tag>
                  ) : null}
                  {filterPresetEditorAccountIds.length === 0 ? <Text type="secondary">暂无固定成员</Text> : null}
                </div>
              </div>
            ) : (
            <div
              style={{
                padding: 14,
                borderRadius: 8,
                background: token.colorFillAlter,
                border: `1px solid ${token.colorBorderSecondary}`,
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                <Text strong style={{ fontSize: 13 }}>筛选条件</Text>
                <Button
                  size="small"
                  type="link"
                  icon={<SyncOutlined />}
                  onClick={() => fillFilterFormFields(currentFilterPresetFilters)}
                  style={{ padding: 0 }}
                >
                  使用当前筛选
                </Button>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: isMobile ? '1fr' : '1fr 1fr', gap: 12 }}>
                <Form.Item name="search" label="关键词搜索" style={{ marginBottom: 0 }}>
                  <Input placeholder="邮箱或关键词" allowClear />
                </Form.Item>
                <Form.Item name="status" label="业务状态" style={{ marginBottom: 0 }}>
                  <Select mode="multiple" placeholder="全部业务状态" options={toSelectOptions(STATUS_FILTER_OPTIONS)} allowClear />
                </Form.Item>
                <Form.Item name="authType" label="认证材料" style={{ marginBottom: 0 }}>
                  <Select mode="multiple" placeholder="全部认证材料" options={toSelectOptions(AUTH_TYPE_FILTER_OPTIONS)} allowClear />
                </Form.Item>
                <Form.Item name="phoneBindingState" label="手机号绑定" style={{ marginBottom: 0 }}>
                  <Select mode="multiple" placeholder="全部绑定情况" options={toSelectOptions(PHONE_BINDING_STATE_FILTER_OPTIONS)} allowClear />
                </Form.Item>
                <Form.Item
                  name="paymentLinkPlatform"
                  label="当前链接类型"
                  normalize={normalizePaymentLinkPlatformFilterValues}
                  style={{ marginBottom: 0 }}
                >
                  <Select mode="multiple" placeholder="全部当前链接类型" options={toSelectOptions(PAYMENT_LINK_PLATFORM_FILTER_OPTIONS)} allowClear />
                </Form.Item>
                <Form.Item
                  name="paymentLinkGenerated"
                  label="提取记录"
                  normalize={normalizePaymentLinkGeneratedFilterValues}
                  style={{ marginBottom: 0 }}
                >
                  <Select mode="multiple" placeholder="全部提取记录" options={toSelectOptions(PAYMENT_LINK_GENERATED_FILTER_OPTIONS)} allowClear />
                </Form.Item>
                <Form.Item name="subscriptionType" label="当前订阅" normalize={normalizeSubscriptionTypeFilterValues} style={{ marginBottom: 0 }}>
                  <Select mode="multiple" placeholder="全部当前订阅" options={toSelectOptions(SUBSCRIPTION_TYPE_FILTER_OPTIONS)} allowClear />
                </Form.Item>
                <Form.Item name="accountValidity" label="认证状态" style={{ marginBottom: 0 }}>
                  <Select mode="multiple" placeholder="全部认证状态" options={toSelectOptions(ACCOUNT_VALIDITY_FILTER_OPTIONS)} allowClear />
                </Form.Item>
                <Form.Item name="sub2apiState" label="Sub2API 状态" style={{ marginBottom: 0 }}>
                  <Select mode="multiple" placeholder="全部 Sub2API 状态" options={toSelectOptions(SUB2API_FILTER_OPTIONS)} allowClear />
                </Form.Item>
                <Form.Item name="oaipayState" label="OAIPay 状态" style={{ marginBottom: 0 }}>
                  <Select mode="multiple" placeholder="全部 OAIPay 状态" options={toSelectOptions(OAIPAY_FILTER_OPTIONS)} allowClear />
                </Form.Item>
                <Form.Item name="zeroAmountEligibilityState" label="0 元资格" style={{ marginBottom: 0 }}>
                  <Select mode="multiple" placeholder="全部 0 元资格" options={toSelectOptions(ZERO_AMOUNT_ELIGIBILITY_FILTER_OPTIONS)} allowClear />
                </Form.Item>
                <Form.Item name="gcashPaymentMethodState" label="GCash 方式" style={{ marginBottom: 0 }}>
                  <Select mode="multiple" placeholder="全部 GCash 方式" options={toSelectOptions(GCASH_PAYMENT_METHOD_FILTER_OPTIONS)} allowClear />
                </Form.Item>
                <Form.Item name="submitState" label="提交状态" style={{ marginBottom: 0 }}>
                  <Select mode="multiple" placeholder="全部提交状态" options={toSelectOptions(SUBMISSION_STATE_FILTER_OPTIONS)} allowClear />
                </Form.Item>
                <Form.Item name="hasSubmitted" label="提交记录" style={{ marginBottom: 0 }}>
                  <Select placeholder="不限" options={toSelectOptions(HAS_SUBMITTED_FILTER_OPTIONS)} allowClear />
                </Form.Item>
                <Form.Item name="sortOrder" label="到期时间排序" style={{ marginBottom: 0 }}>
                  <Select placeholder="默认排序" options={SUBSCRIPTION_EXPIRY_SORT_OPTIONS} allowClear />
                </Form.Item>
                <Form.Item name="registrationSortOrder" label="注册时间排序" style={{ marginBottom: 0 }}>
                  <Select options={REGISTRATION_SORT_OPTIONS} />
                </Form.Item>
              </div>
            </div>
            )}
          </Form>
        </Space>
      </Modal>

      <Modal
        title="组合管理"
        open={filterPresetManageOpen}
        onCancel={() => setFilterPresetManageOpen(false)}
        footer={[
          <Button key="refresh" icon={<SyncOutlined spin={filterPresetLoading} />} onClick={() => void loadFilterPresets(false)}>
            刷新
          </Button>,
          <Button key="new-condition" icon={<SaveOutlined />} onClick={openCreateCurrentFilterPreset}>
            保存当前条件组合
          </Button>,
          activeFilterPreset && secondaryFilterScope === 'unassigned' && selectedRowKeys.length > 0 ? (
            <Button key="new-fixed" type="primary" onClick={openCreateFixedGroup}>
              新建固定账号组合
            </Button>
          ) : null,
          <Button key="close" onClick={() => setFilterPresetManageOpen(false)}>
            关闭
          </Button>,
        ].filter(Boolean)}
        width={860}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {legacyFixedPresets.length > 0 ? (
            <Alert
              type="warning"
              showIcon
              message="检测到旧固定组合"
              description="旧数据没有一级父级且可能存在重复成员，需预览并确认后迁移到两级结构。"
              action={<Button size="small" onClick={openFixedMigration}>迁移旧组合</Button>}
            />
          ) : null}

          <Text strong>条件筛选组合</Text>
          {filterPresets.map((preset) => (
            <div
              key={preset.id}
              style={{
                display: 'flex',
                flexWrap: 'wrap',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 12,
                padding: '10px 12px',
                border: `1px solid ${preset.id === activeFilterPresetId ? token.colorPrimaryBorder : token.colorBorderSecondary}`,
                borderRadius: 8,
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
            <Alert type="info" showIcon message="暂无条件筛选组合" />
          ) : null}

          <Text strong style={{ marginTop: 4 }}>固定账号组合</Text>
          {fixedGroups.map((group) => {
            const parent = filterPresets.find((item) => item.id === group.parent_preset_id)
            return (
              <div
                key={group.id}
                style={{
                  display: 'flex',
                  flexWrap: 'wrap',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  gap: 12,
                  padding: '10px 12px',
                  border: `1px solid ${group.id === activeFixedGroupId ? token.colorPrimaryBorder : token.colorBorderSecondary}`,
                  borderRadius: 8,
                  background: group.id === activeFixedGroupId ? token.colorPrimaryBg : token.colorFillAlter,
                }}
              >
                <div style={{ minWidth: 0, flex: 1 }}>
                  <Space size={6} wrap style={{ marginBottom: 4 }}>
                    <Text strong>{group.name}</Text>
                    {parent ? <Tag style={{ marginInlineEnd: 0 }}>{parent.name}</Tag> : null}
                    {group.pinned ? <Tag color="processing" style={{ marginInlineEnd: 0 }}>置顶</Tag> : null}
                    {group.id === activeFixedGroupId ? <Tag color="success" style={{ marginInlineEnd: 0 }}>当前</Tag> : null}
                  </Space>
                  {group.description ? (
                    <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>{group.description}</Text>
                  ) : null}
                  <SubscriptionStatusCounts
                    counts={group.subscription_counts}
                    labels="full"
                    splitUnknown
                    surface
                    className="accounts-fixed-group-manage-counts"
                  />
                </div>
                <Space size={6} wrap>
                  <Button size="small" onClick={() => {
                    applyFixedGroup(group)
                    setFilterPresetManageOpen(false)
                  }}>
                    应用
                  </Button>
                  <Button size="small" icon={<EditOutlined />} onClick={() => openEditFilterPresetMeta(group)}>
                    编辑
                  </Button>
                  <Popconfirm
                    title={`确认删除固定账号组合「${group.name}」？其中账号将解除固定归属。`}
                    onConfirm={() => { void deleteFilterPreset(group) }}
                  >
                    <Button size="small" danger icon={<DeleteOutlined />} loading={filterPresetSaving}>删除</Button>
                  </Popconfirm>
                </Space>
              </div>
            )
          })}
          {!filterPresetLoading && fixedGroups.length === 0 ? (
            <Alert type="info" showIcon message="暂无固定账号组合" />
          ) : null}
        </Space>
      </Modal>

      <Modal
        title="迁移旧固定组合"
        open={fixedMigrationOpen}
        onCancel={() => setFixedMigrationOpen(false)}
        width={860}
        footer={[
          <Button key="cancel" onClick={() => setFixedMigrationOpen(false)}>取消</Button>,
          <Button key="preview" loading={fixedMigrationLoading} onClick={() => { void runFixedMigration(false) }}>
            生成预览
          </Button>,
          <Popconfirm
            key="commit"
            title="确认按当前父级和优先级迁移？重复账号只归入优先级最高的组合。"
            disabled={!fixedMigrationPreview || Number(fixedMigrationPreview?.outside_parent_account_count || 0) > 0}
            onConfirm={() => { void runFixedMigration(true) }}
          >
            <Button
              type="primary"
              loading={fixedMigrationLoading}
              disabled={!fixedMigrationPreview || Number(fixedMigrationPreview?.outside_parent_account_count || 0) > 0}
            >
              确认迁移
            </Button>
          </Popconfirm>,
        ]}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Alert type="info" showIcon message="排序越靠前，重复账号的归属优先级越高。迁移不会自动更改一级组合条件。" />
          {fixedMigrationPriority.map((presetId, index) => {
            const preset = legacyFixedPresets.find((item) => item.id === presetId)
            if (!preset) return null
            return (
              <div key={preset.id} className="accounts-fixed-migration-row">
                <Text className="accounts-fixed-migration-name" ellipsis={{ tooltip: preset.name }}>{preset.name}</Text>
                <Select
                  value={fixedMigrationParentById[preset.id] || undefined}
                  placeholder="选择一级条件组合"
                  options={filterPresets.map((item) => ({ value: item.id, label: item.name }))}
                  onChange={(value) => {
                    setFixedMigrationParentById((current) => ({ ...current, [preset.id]: value }))
                    setFixedMigrationPreview(null)
                  }}
                />
                <Space.Compact>
                  <Button icon={<ArrowUpOutlined />} aria-label="提高优先级" disabled={index === 0} onClick={() => moveFixedMigrationPriority(preset.id, -1)} />
                  <Button icon={<ArrowDownOutlined />} aria-label="降低优先级" disabled={index === fixedMigrationPriority.length - 1} onClick={() => moveFixedMigrationPriority(preset.id, 1)} />
                </Space.Compact>
              </div>
            )
          })}
          {fixedMigrationPreview ? (
            <div style={{ paddingTop: 4 }}>
              <Text strong>迁移预览</Text>
              <Space direction="vertical" size={6} style={{ display: 'flex', marginTop: 8 }}>
                {(fixedMigrationPreview.groups || []).map((item: any) => (
                  <div key={item.id} style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 6 }}>
                    <Text style={{ minWidth: 140 }}>{item.name}</Text>
                    <Tag>迁入 {item.assigned_account_count}</Tag>
                    {item.conflict_account_count > 0 ? <Tag color="warning">冲突 {item.conflict_account_count}</Tag> : null}
                    {item.missing_account_count > 0 ? <Tag color="error">缺失 {item.missing_account_count}</Tag> : null}
                    {item.outside_parent_account_count > 0 ? <Tag color="error">不符合父级 {item.outside_parent_account_count}</Tag> : null}
                  </div>
                ))}
              </Space>
              {Number(fixedMigrationPreview.outside_parent_account_count || 0) > 0 ? (
                <Alert
                  type="error"
                  showIcon
                  style={{ marginTop: 10 }}
                  message="存在不符合父级条件的账号，当前预览不能提交"
                />
              ) : null}
            </div>
          ) : null}
        </Space>
      </Modal>

      <RegisterTaskModal
        open={registerModalOpen}
        currentPlatform={currentPlatform}
        taskModalMode={taskModalMode}
        taskModalAccount={taskModalAccount}
        taskId={taskId}
        taskSnapshot={taskSnapshot}
        registerForm={registerForm}
        registerMailProvider={registerMailProvider}
        registerControlConfig={registerControlConfig}
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
          void Promise.all([load(), loadFilterPresets(true)])
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

      <PixLinkScanModal
        open={pixLinkScanOpen}
        report={pixLinkScanReport}
        loading={pixLinkScanLoading}
        error={pixLinkScanError}
        cleanupMode={pixLinkCleanupMode}
        cleanupPaymentType={pixLinkCleanupType}
        onClose={() => {
          if (!pixLinkScanLoading && !pixLinkCleanupLoading) {
            setPixLinkScanOpen(false)
          }
        }}
        onScan={() => { void loadPixLinkScan() }}
        onCleanup={(cleanupMode, paymentType) => { void handleCleanupPixLinks(cleanupMode, paymentType) }}
      />

      <Modal
        title={`${batchPaymentLinkForceRefresh ? '强制重新生成' : '生成'}${
          batchPaymentLinkPlan === 'team'
            ? ' Team checkout 长链接'
            : batchPaymentLinkPlan === 'short' ? ' Plus 登录态短链' : '支付链接'
        }`}
        open={batchPaymentLinkConfigOpen}
        onCancel={() => {
          setBatchPaymentLinkConfigOpen(false)
          setBatchPaymentLinkTargetAccount(null)
        }}
        onOk={submitBatchPaymentLinkConfig}
        confirmLoading={batchPaymentLinkLoading}
        okButtonProps={{ disabled: batchPaymentLinkProfileLoading || Boolean(batchPaymentLinkProfileError) }}
        okText={batchPaymentLinkForceRefresh ? '开始重新生成' : '开始生成'}
        cancelText="取消"
        maskClosable={false}
        width={720}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Form
            form={batchPaymentLinkForm}
            layout="vertical"
            initialValues={{
              plan: 'plus',
              workspace_name: DEFAULT_TEAM_WORKSPACE_NAME,
              checkout_proxy_region: undefined,
              checkout_ui_mode: DEFAULT_TEAM_CHECKOUT_UI_MODE,
              billing_country: DEFAULT_TEAM_BILLING_COUNTRY,
              price_interval: '',
              seat_quantity: undefined,
              short_country: DEFAULT_SHORT_PAYMENT_COUNTRY,
              short_currency: DEFAULT_SHORT_PAYMENT_CURRENCY,
            }}
          >
            <Form.Item name="plan" label="生成类型" style={{ marginBottom: 12 }}>
              <Segmented
                block
                options={[
                  { label: 'Plus 管理端', value: 'plus' },
                  { label: 'Plus 登录态短链', value: 'short' },
                  { label: 'Team 优惠长链', value: 'team' },
                ]}
                onChange={(value) => {
                  const rawMode = String(value)
                  const nextPlan: PaymentLinkGenerationMode = rawMode === 'team' ? 'team' : rawMode === 'short' ? 'short' : 'plus'
                  setBatchPaymentLinkPlan(nextPlan)
                  setBatchPaymentLinkProfile(null)
                  setBatchPaymentLinkProfileError('')
                  if (nextPlan === 'short') {
                    void loadShortPaymentLinkProfile()
                    return
                  }
                  if (nextPlan === 'plus') {
                    void loadBatchPaymentLinkProfile({ plan: 'plus' })
                    return
                  }
                  const nextParams: Record<string, unknown> = {
                    ...buildBatchPaymentLinkParams(batchPaymentLinkForceRefresh),
                    plan: 'team',
                  }
                  if (/^[A-Z]{2}$/.test(String(nextParams.checkout_proxy_region || ''))) {
                    void loadBatchPaymentLinkProfile(nextParams)
                  }
                }}
              />
            </Form.Item>

            {batchPaymentLinkPlan === 'team' ? (
              <>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '0 12px' }}>
                  <Form.Item
                    name="workspace_name"
                    label="Workspace 名称"
                    rules={[
                      { required: true, whitespace: true, message: '请输入 Workspace 名称' },
                      { max: 256, message: 'Workspace 名称不能超过 256 个字符' },
                    ]}
                  >
                    <Input
                      maxLength={256}
                      placeholder={DEFAULT_TEAM_WORKSPACE_NAME}
                    />
                  </Form.Item>
                  <Form.Item
                    name="checkout_ui_mode"
                    label="支付页模式"
                    rules={[{ required: true, message: '请选择支付页模式' }]}
                  >
                    <Segmented
                      block
                      options={[
                        { label: 'Hosted（默认）', value: 'hosted' },
                        { label: 'Custom', value: 'custom' },
                      ]}
                      onChange={(value) => {
                        const checkoutUiMode = String(value) === 'custom' ? 'custom' : DEFAULT_TEAM_CHECKOUT_UI_MODE
                        batchPaymentLinkForm.setFieldValue('checkout_ui_mode', checkoutUiMode)
                        setBatchPaymentLinkProfile(null)
                        setBatchPaymentLinkProfileError('')
                        const checkoutProxyRegion = String(
                          batchPaymentLinkForm.getFieldValue('checkout_proxy_region') || '',
                        ).trim().toUpperCase()
                        if (!/^[A-Z]{2}$/.test(checkoutProxyRegion)) return
                        void loadBatchPaymentLinkProfile({
                          ...buildBatchPaymentLinkParams(batchPaymentLinkForceRefresh),
                          plan: 'team',
                          checkout_proxy_region: checkoutProxyRegion,
                          checkout_ui_mode: checkoutUiMode,
                        })
                      }}
                    />
                  </Form.Item>
                  <Form.Item
                    name="checkout_proxy_region"
                    label="动态 IP 国家"
                    rules={[
                      { required: true, message: '请选择动态 IP 国家' },
                      { pattern: /^[A-Za-z]{2}$/, message: '请输入两位国家代码' },
                    ]}
                  >
                    <Select
                      showSearch
                      allowClear
                      optionFilterProp="label"
                      placeholder="选择国家"
                      options={teamProxyCountryOptions}
                      onSearch={setTeamProxyCountrySearch}
                      onChange={(value) => {
                        const checkoutProxyRegion = String(value || '').trim().toUpperCase()
                        setTeamProxyCountrySearch(checkoutProxyRegion)
                        setBatchPaymentLinkProfile(null)
                        setBatchPaymentLinkProfileError('')
                        if (!checkoutProxyRegion) return
                        batchPaymentLinkForm.setFieldValue('checkout_proxy_region', checkoutProxyRegion)
                        void loadBatchPaymentLinkProfile({
                          ...buildBatchPaymentLinkParams(batchPaymentLinkForceRefresh),
                          plan: 'team',
                          checkout_proxy_region: checkoutProxyRegion,
                        })
                      }}
                    />
                  </Form.Item>
                  <Form.Item
                    name="billing_country"
                    label="账单国家"
                    rules={[{ required: true, message: '请选择账单国家' }]}
                  >
                    <Select
                      showSearch
                      optionFilterProp="label"
                      placeholder="选择账单国家"
                      loading={batchPaymentLinkProfileLoading && teamBillingCountryOptions.length === 0}
                      options={teamBillingCountryOptions}
                      onChange={(value) => {
                        const billingCountry = String(value || DEFAULT_TEAM_BILLING_COUNTRY).trim().toUpperCase()
                        batchPaymentLinkForm.setFieldValue('billing_country', billingCountry)
                        setBatchPaymentLinkProfile(null)
                        setBatchPaymentLinkProfileError('')
                        const checkoutProxyRegion = String(
                          batchPaymentLinkForm.getFieldValue('checkout_proxy_region') || '',
                        ).trim().toUpperCase()
                        if (!/^[A-Z]{2}$/.test(checkoutProxyRegion)) return
                        void loadBatchPaymentLinkProfile({
                          ...buildBatchPaymentLinkParams(batchPaymentLinkForceRefresh),
                          plan: 'team',
                          billing_country: billingCountry,
                          checkout_proxy_region: checkoutProxyRegion,
                        })
                      }}
                    />
                  </Form.Item>
                  <Form.Item name="price_interval" label="计费周期">
                    <Select
                      options={[
                        { value: '', label: '沿用 long-link 管理页' },
                        { value: 'month', label: '月付' },
                        { value: 'year', label: '年付' },
                      ]}
                    />
                  </Form.Item>
                  <Form.Item
                    name="seat_quantity"
                    label="席位数量"
                    rules={[
                      {
                        validator: (_, value) => value === undefined || value === null || value === ''
                          || (Number.isInteger(Number(value)) && Number(value) >= 2 && Number(value) <= 1000)
                          ? Promise.resolve()
                          : Promise.reject(new Error('席位数量必须是 2 到 1000 的整数')),
                      },
                    ]}
                  >
                    <InputNumber
                      min={2}
                      max={1000}
                      precision={0}
                      placeholder={batchPaymentLinkProfile?.team?.seat_quantity ? String(batchPaymentLinkProfile.team.seat_quantity) : '沿用管理页'}
                      style={{ width: '100%' }}
                    />
                  </Form.Item>
                  <Form.Item
                    name="promo_code"
                    label="优惠码"
                    rules={[{ max: 256, message: '优惠码不能超过 256 个字符' }]}
                  >
                    <Input maxLength={256} placeholder="沿用 long-link 管理页" />
                  </Form.Item>
                </div>
                <Form.Item
                  name="cancel_url"
                  label="取消支付跳转 URL"
                  rules={[
                    {
                      validator: (_, value) => {
                        const text = String(value || '').trim()
                        if (!text) return Promise.resolve()
                        try {
                          const parsed = new URL(text)
                          return ['http:', 'https:'].includes(parsed.protocol)
                            ? Promise.resolve()
                            : Promise.reject(new Error('请输入 HTTP(S) URL'))
                        } catch {
                          return Promise.reject(new Error('请输入完整的 HTTP(S) URL'))
                        }
                      },
                    },
                  ]}
                >
                  <Input maxLength={2048} placeholder={batchPaymentLinkProfile?.team?.cancel_url || '沿用 long-link 管理页'} />
                </Form.Item>
                <Button
                  size="small"
                  icon={<SyncOutlined />}
                  loading={batchPaymentLinkProfileLoading}
                  onClick={() => void loadBatchPaymentLinkProfile(buildBatchPaymentLinkParams(batchPaymentLinkForceRefresh))}
                >
                  刷新生效配置
                </Button>
              </>
            ) : null}
            {batchPaymentLinkPlan === 'short' ? (
              <Space direction="vertical" size={12} style={{ width: '100%' }}>
                <Alert
                  type="warning"
                  showIcon
                  message="登录态短链要求账号保存 Web Session"
                  description="任务只处理具备 ChatGPT Session Cookie 的 Plus 账号；只有 Access Token 的账号会直接跳过。链接生成后仍需在对应账号登录态中访问。"
                />
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '0 12px' }}>
                  <Form.Item
                    name="short_country"
                    label="账单国家"
                    rules={[{ required: true, message: '请选择账单国家' }]}
                  >
                    <Select
                      showSearch
                      optionFilterProp="label"
                      loading={batchPaymentLinkProfileLoading}
                      options={teamBillingCountryOptions}
                      onChange={(value) => {
                        const selected = teamBillingCountryOptions.find((item) => item.value === value)
                        batchPaymentLinkForm.setFieldsValue({
                          short_country: String(value || DEFAULT_SHORT_PAYMENT_COUNTRY).toUpperCase(),
                          short_currency: selected?.currency || DEFAULT_SHORT_PAYMENT_CURRENCY,
                        })
                      }}
                    />
                  </Form.Item>
                  <Form.Item
                    name="short_currency"
                    label="结算币种"
                    normalize={(value) => String(value || '').toUpperCase()}
                    rules={[
                      { required: true, message: '请输入结算币种' },
                      { pattern: /^[A-Za-z]{3}$/, message: '请输入三位货币代码' },
                    ]}
                  >
                    <Input maxLength={3} />
                  </Form.Item>
                </div>
              </Space>
            ) : null}
          </Form>
          <Alert
            type="info"
            showIcon
            message={
              batchPaymentLinkTargetAccount
                ? `范围：当前账号 ${String(batchPaymentLinkTargetAccount.email || batchPaymentLinkTargetAccount.id || '-')}`
                : selectedRowKeys.length > 0
                ? `范围：当前选中 ${selectedRowKeys.length} 个账号`
                : `范围：当前筛选结果 ${total} 个账号`
            }
            description={
              batchPaymentLinkPlan === 'short'
                ? batchPaymentLinkForceRefresh
                  ? '强制模式会重新创建登录态短链；已支付、已订阅、已失效、缺少 Web Session 或正在生成的账号仍会跳过。'
                  : '默认复用相同国家和币种的当前短链；缺少 Web Session 的账号不会进入生成队列。'
                : batchPaymentLinkForceRefresh
                ? `强制模式会重新生成当前${batchPaymentLinkPlan === 'team' ? ' Team 参数变体' : ' Plus 配置'}；已支付、已订阅、已失效或正在生成的账号仍会跳过。`
                : `默认只跳过相同${batchPaymentLinkPlan === 'team' ? ' Team 参数变体' : ' Plus 配置'}的当前链接和成功记录。`
            }
          />
          {batchPaymentLinkProfileLoading ? (
            <Alert
              type="info"
              showIcon
              message={batchPaymentLinkPlan === 'short' ? '正在读取本地短链国家配置...' : '正在读取 long-link 管理端当前配置...'}
            />
          ) : null}
          {batchPaymentLinkProfileError ? (
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Alert
                type="error"
                showIcon
                message={batchPaymentLinkPlan === 'short' ? '无法读取本地短链配置' : '无法读取 long-link 管理端配置'}
                description={batchPaymentLinkProfileError}
              />
              <Button
                size="small"
                style={{ alignSelf: 'flex-start' }}
                onClick={() => void (
                  batchPaymentLinkPlan === 'short'
                    ? loadShortPaymentLinkProfile()
                    : loadBatchPaymentLinkProfile(buildBatchPaymentLinkParams(batchPaymentLinkForceRefresh))
                )}
              >
                重新读取
              </Button>
            </Space>
          ) : null}
          {batchPaymentLinkPlan !== 'short' && batchPaymentLinkProfile ? (
            <>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 10 }}>
                <div>
                  <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>支付类型</Text>
                  <Tag color="blue" style={{ marginTop: 4 }}>{String(batchPaymentLinkProfile.link_type || 'hosted').toUpperCase()}</Tag>
                </div>
                {String(batchPaymentLinkProfile.plan || '').toLowerCase() === 'team' ? (
                  <>
                    <div>
                      <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>Workspace</Text>
                      <Text>{batchPaymentLinkProfile.team?.workspace_name || '-'}</Text>
                    </div>
                    <div>
                      <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>周期 / 席位</Text>
                      <Text>{`${batchPaymentLinkProfile.team?.price_interval === 'year' ? '年付' : '月付'} / ${batchPaymentLinkProfile.team?.seat_quantity || '-'}`}</Text>
                    </div>
                    <div>
                      <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>优惠码</Text>
                      <Text>{batchPaymentLinkProfile.team?.promo_code_configured ? '已配置' : '未配置'}</Text>
                    </div>
                    <div>
                      <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>动态 IP 国家</Text>
                      <Text>{batchPaymentLinkProfile.regions?.checkout || '-'}</Text>
                    </div>
                  </>
                ) : null}
                <div>
                  <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>账单国家 / 币种</Text>
                  <Text>{`${batchPaymentLinkProfile.country || batchPaymentLinkProfile.billing_country || '-'} / ${batchPaymentLinkProfile.currency || '-'}`}</Text>
                </div>
                <div>
                  <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>有效并发</Text>
                  <Text>{batchPaymentLinkProfile.effective_concurrency || '-'}</Text>
                </div>
                <div>
                  <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>Checkout 模式</Text>
                  <Text>{String(batchPaymentLinkProfile.checkout_ui_mode || '-').toUpperCase()}</Text>
                </div>
                <div>
                  <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>代理链</Text>
                  <Text>{batchPaymentLinkProfile.proxy_configured ? '已配置' : '未配置'}</Text>
                </div>
                <div>
                  <Text type="secondary" style={{ display: 'block', fontSize: 12 }}>配置哈希</Text>
                  <Text code title={batchPaymentLinkProfile.profile_hash}>{String(batchPaymentLinkProfile.profile_hash || '-').slice(0, 16)}</Text>
                </div>
              </div>
              <Text type="secondary" style={{ fontSize: 12 }}>
                long-link 在任务启动时再次冻结当前配置；代理、指纹和密钥不会暴露到本页。
              </Text>
            </>
          ) : null}
        </Space>
      </Modal>

      <Modal
        title={`批量 ${paymentEligibilityConfigKind === 'gcash_payment_method' ? 'GCash 支付方式' : '0 元试用资格'}检测`}
        open={paymentEligibilityConfigOpen}
        onCancel={() => setPaymentEligibilityConfigOpen(false)}
        onOk={submitPaymentEligibilityConfig}
        confirmLoading={paymentEligibilityLoading}
        okText="开始检测"
        cancelText="取消"
        width={460}
        maskClosable={false}
      >
        <Form form={paymentEligibilityConfigForm} layout="vertical">
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message={paymentEligibilityConfigScope === 'selected'
              ? `范围：当前选中 ${selectedRowKeys.length} 个账号`
              : `范围：当前筛选结果 ${total} 个账号`}
          />
          <Form.Item
            name="concurrency"
            label={`并发数（1-${PAYMENT_ELIGIBILITY_MAX_CONCURRENCY}）`}
            rules={[{ required: true, message: '请输入并发数' }]}
          >
            <InputNumber
              min={1}
              max={PAYMENT_ELIGIBILITY_MAX_CONCURRENCY}
              step={1}
              precision={0}
              style={{ width: '100%' }}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={webSessionLoginConfigMode === 'single' ? '执行登录态配置' : '批量执行登录态配置'}
        open={webSessionLoginConfigOpen}
        onCancel={() => setWebSessionLoginConfigOpen(false)}
        onOk={submitWebSessionLoginConfig}
        confirmLoading={webSessionLoginLoading}
        okText="开始执行"
        cancelText="取消"
        width={720}
        maskClosable={false}
      >
        <Form form={webSessionLoginConfigForm} layout="vertical">
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message={
              webSessionLoginConfigMode === 'single'
                ? `账号：${webSessionLoginConfigAccount?.email || webSessionLoginConfigAccount?.id || '-'}`
                : webSessionLoginConfigScope === 'selected'
                  ? `范围：当前选中 ${selectedRowKeys.length} 个账号`
                  : `范围：当前筛选结果 ${total} 个账号`
            }
            description="登录成功后立即更新 AccessToken、Session、Cookie、账号身份和浏览器 Profile，并持续保持本地浏览器；在任务面板人工停止并释放前不会关闭，也不会请求 ChatGPT logout。账号使用状态、订阅、手机号及邮箱绑定状态保持不变。"
          />

          {webSessionLoginConfigMode === 'batch' ? (
            <Form.Item
              name="concurrency"
              label="并发数"
              rules={[{ required: true, message: '请输入并发数' }]}
              extra="实际并发受实例持久浏览器容量限制；释放一个浏览器后，批任务才会继续补位下一个账号。"
            >
              <InputNumber min={1} step={1} precision={0} style={{ width: '100%' }} />
            </Form.Item>
          ) : null}

          <Form.Item name="proxy_mode" label="代理方式">
            <Segmented
              block
              options={[
                { label: '动态代理', value: 'dynamic' },
                { label: '代理池', value: 'pool' },
                { label: '指定代理', value: 'specified' },
                { label: '直连', value: 'direct' },
              ]}
            />
          </Form.Item>

          {webSessionLoginProxyMode === 'specified' || webSessionLoginProxyMode === 'dynamic' ? (
            <Space style={{ width: '100%' }} align="start" wrap>
              <Form.Item
                name="proxy"
                label={webSessionLoginProxyMode === 'dynamic' ? '动态节点（本次任务可覆盖）' : '指定代理'}
                style={{ flex: '1 1 360px' }}
                rules={webSessionLoginProxyMode === 'specified' ? [{ required: true, message: '请输入指定代理地址' }] : undefined}
                extra={webSessionLoginProxyMode === 'dynamic' ? '留空沿用全局动态节点。' : undefined}
              >
                <Input
                  placeholder={webSessionLoginProxyMode === 'dynamic'
                    ? '可留空；或填写本次任务使用的动态代理模板'
                    : 'http://user:pass@host:port'}
                />
              </Form.Item>
              <Form.Item name="proxy_failover" label="失败处理" valuePropName="checked" style={{ width: 200 }}>
                <Switch
                  checkedChildren={webSessionLoginProxyMode === 'dynamic' ? '刷新 SID' : '切换代理池'}
                  unCheckedChildren="不切换"
                />
              </Form.Item>
            </Space>
          ) : null}

          {webSessionLoginProxyMode === 'pool'
          || webSessionLoginProxyMode === 'dynamic'
          || (webSessionLoginProxyMode === 'specified' && webSessionLoginProxyFailover) ? (
            <Space style={{ width: '100%' }} align="start" wrap>
              <Form.Item
                name="proxy_country_code"
                label="出口国家"
                style={{ flex: '1 1 180px' }}
                rules={webSessionLoginProxyMode === 'dynamic' ? [{ required: true, message: '请输入动态代理出口国家' }] : undefined}
              >
                <Input placeholder={webSessionLoginProxyMode === 'dynamic' ? '例如 US / JP / SG' : '不限'} maxLength={2} />
              </Form.Item>
              {webSessionLoginProxyMode !== 'dynamic' ? (
                <>
                  <Form.Item name="proxy_min_score" label="最低健康分" style={{ width: 150 }}>
                    <InputNumber min={0} max={100} precision={0} style={{ width: '100%' }} />
                  </Form.Item>
                  <Form.Item name="proxy_max_candidates" label="最多候选" style={{ width: 150 }}>
                    <InputNumber min={1} max={100} precision={0} style={{ width: '100%' }} />
                  </Form.Item>
                </>
              ) : null}
            </Space>
          ) : null}
        </Form>
      </Modal>

      <Modal
        title={invalidRecheckConfigMode === 'single' ? '失效测活配置' : '批量失效测活配置'}
        open={invalidRecheckConfigOpen}
        onCancel={() => setInvalidRecheckConfigOpen(false)}
        onOk={submitInvalidRecheckConfig}
        confirmLoading={batchInvalidRecheckLoading}
        okText="开始测活"
        cancelText="取消"
        width={720}
        maskClosable={false}
      >
        <Form form={invalidRecheckConfigForm} layout="vertical">
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message={
              invalidRecheckConfigMode === 'single'
                ? `账号：${invalidRecheckConfigAccount?.email || invalidRecheckConfigAccount?.id || '-'}`
                : invalidRecheckConfigScope === 'selected'
                  ? `范围：当前选中 ${selectedRowKeys.length} 个账号`
                  : `范围：当前筛选结果 ${total} 个账号`
            }
          />

          {invalidRecheckConfigMode === 'batch' ? (
            <Form.Item
              name="concurrency"
              label="并发数"
              rules={[{ required: true, message: '请输入并发数' }]}
              extra="不设固定任务上限；实际 worker 数不会超过本次可执行账号数，浏览器事务仍按实例运行容量排队。"
            >
              <InputNumber min={1} step={1} precision={0} style={{ width: '100%' }} />
            </Form.Item>
          ) : null}

          <Form.Item name="proxy_mode" label="代理方式">
            <Segmented
              block
              options={[
                { label: '动态代理', value: 'dynamic' },
                { label: '代理池', value: 'pool' },
                { label: '指定代理', value: 'specified' },
                { label: '直连', value: 'direct' },
              ]}
            />
          </Form.Item>

          {invalidRecheckProxyMode === 'specified' || invalidRecheckProxyMode === 'dynamic' ? (
            <Space style={{ width: '100%' }} align="start" wrap>
              <Form.Item
                name="proxy"
                label={invalidRecheckProxyMode === 'dynamic' ? '动态节点（本次任务可覆盖）' : '指定代理'}
                style={{ flex: '1 1 360px' }}
                rules={invalidRecheckProxyMode === 'specified' ? [{ required: true, message: '请输入指定代理地址' }] : undefined}
                extra={invalidRecheckProxyMode === 'dynamic' ? '留空沿用全局动态节点。' : undefined}
              >
                <Input
                  placeholder={invalidRecheckProxyMode === 'dynamic'
                    ? '可留空；或填写本次任务使用的动态代理模板'
                    : 'http://user:pass@host:port'}
                />
              </Form.Item>
              <Form.Item name="proxy_failover" label="失败处理" valuePropName="checked" style={{ width: 200 }}>
                <Switch
                  checkedChildren={invalidRecheckProxyMode === 'dynamic' ? '刷新 SID' : '切换代理池'}
                  unCheckedChildren="不切换"
                />
              </Form.Item>
            </Space>
          ) : null}

          {invalidRecheckProxyMode === 'pool'
          || invalidRecheckProxyMode === 'dynamic'
          || (invalidRecheckProxyMode === 'specified' && invalidRecheckProxyFailover) ? (
            <Space style={{ width: '100%' }} align="start" wrap>
              <Form.Item
                name="proxy_country_code"
                label="出口国家"
                style={{ flex: '1 1 180px' }}
                rules={invalidRecheckProxyMode === 'dynamic' ? [{ required: true, message: '请输入动态代理出口国家' }] : undefined}
              >
                <Input placeholder={invalidRecheckProxyMode === 'dynamic' ? '例如 US / JP / SG' : '不限'} maxLength={2} />
              </Form.Item>
              {invalidRecheckProxyMode !== 'dynamic' ? (
                <>
                  <Form.Item name="proxy_min_score" label="最低健康分" style={{ width: 150 }}>
                    <InputNumber min={0} max={100} precision={0} style={{ width: '100%' }} />
                  </Form.Item>
                  <Form.Item name="proxy_max_candidates" label="最多候选" style={{ width: 150 }}>
                    <InputNumber min={1} max={100} precision={0} style={{ width: '100%' }} />
                  </Form.Item>
                </>
              ) : null}
            </Space>
          ) : null}
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
          <Form.Item name="phone_number_filter" hidden>
            <Input />
          </Form.Item>
          <Form.Item name="prefix_number_filter" hidden>
            <Input />
          </Form.Item>
          <Form.Item name="unavailable_number_test_enabled" valuePropName="checked" hidden>
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
                    disabled={phoneBindingPoolMode !== 'normal'}
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
              普通绑定使用可用号码；限定号段可按号码状态筛选；号段抽样和不可用复测固定从手机号池快照取号。
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
                        { label: '不可用号码全量复测', value: 'unavailable_numbers' },
                      ]}
                      onChange={(value) => {
                        const mode = normalizePhonePoolMode(value)
                        const nextPhoneNumberFilter: PhoneNumberFilter = mode === 'unavailable_numbers'
                          ? 'unavailable'
                          : mode === 'prefix_limited'
                            ? phoneBindingPrefixNumberFilter
                            : 'available'
                        const fixedSnapshotSelection = mode === 'prefix_sample'
                          || mode === 'unavailable_numbers'
                          || (mode === 'prefix_limited' && nextPhoneNumberFilter !== 'available')
                        updatePhoneBindingSettings({
                          phone_pool_mode: mode,
                          phone_number_filter: nextPhoneNumberFilter,
                          prefix_number_filter: nextPhoneNumberFilter,
                          unavailable_number_test_enabled: mode === 'unavailable_numbers',
                          prefix_bind_enabled: mode === 'prefix_limited',
                          prefix_sample_enabled: mode === 'prefix_sample',
                          use_pool: mode === 'normal' ? phoneBindingUsePoolValue !== false : true,
                          phone_lines: mode === 'normal' ? phoneBindingPhoneLinesValue : '',
                          reuse_phone_until_unusable: fixedSnapshotSelection ? false : phoneBindingReusePhoneValue,
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
                    ? `已选 ${phoneBindingSelectedPrefixes.length} 个号段，${phoneBindingPrefixNumberFilterLabel}候选 ${phoneBindingLimitedCandidatePhones} 个${phoneBindingPrefixNumberFilter === 'available' ? `，可分配 ${phoneBindingLimitedCapacity} 个账号` : '，固定快照逐个测试'}`
                    : phoneBindingPrefixSampleEnabled
                      ? phoneBindingSelectedPrefixes.length > 0
                        ? `按所选号段抽样，预计测试 ${phoneBindingSummarySampleCount} 个号码`
                        : `按范围抽样 ${phoneBindingSummaryPrefixCount} 个号段，预计 ${phoneBindingSummarySampleCount} 个号码`
                      : phoneBindingUnavailableNumberTestEnabled
                        ? `全池不可用号码 ${phoneBindingSummaryUnavailable} 个，创建任务时固定全部候选快照。`
                        : '普通绑定按手机号池可用容量自动取号，也可展开临时粘贴号码。'}
                </Text>
              </div>

              <Space wrap size={[6, 6]} style={{ marginTop: 10 }}>
                <Text type="secondary" style={{ fontSize: 12 }}>候选预览</Text>
                <Tag color="blue">候选号码 {phoneBindingCandidatePhoneCount}</Tag>
                <Tag>账号 {phoneBindingTargetCount}</Tag>
                <Tag color="processing">预计测试 {phoneBindingExpectedTestCount}</Tag>
                <Tag>未覆盖号码 {phoneBindingUncoveredPhoneCount}</Tag>
                {phoneBindingPrefixBindEnabled && phoneBindingPrefixNumberFilter === 'available' ? <Tag color="cyan">可分配 {phoneBindingLimitedCapacity}</Tag> : null}
                {!phoneBindingPrefixBindEnabled && !phoneBindingFixedSnapshotSelection && phoneBindingUsePool ? <Tag color="cyan">可用容量 {phoneBindingDynamicCapacity}</Tag> : null}
              </Space>

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

              {phoneBindingPrefixBindEnabled ? (
                <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', marginTop: 10 }}>
                  <Text strong style={{ fontSize: 12 }}>号段内号码</Text>
                  <Segmented
                    size="small"
                    value={phoneBindingPrefixNumberFilter}
                    options={[
                      { label: '可用号码', value: 'available' },
                      { label: '不可用号码', value: 'unavailable' },
                      { label: '全部号码', value: 'all' },
                    ]}
                    onChange={(value) => {
                      const numberFilter = normalizePhoneNumberFilter(value)
                      updatePhoneBindingSettings({
                        phone_number_filter: numberFilter,
                        prefix_number_filter: numberFilter,
                        reuse_phone_until_unusable: numberFilter === 'available' ? phoneBindingReusePhoneValue : false,
                      })
                    }}
                  />
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {phoneBindingPrefixNumberFilter === 'available'
                      ? '按号码自身可用状态动态分配。'
                      : phoneBindingPrefixNumberFilter === 'unavailable'
                        ? '只取所选号段内 status=cannot_send 的号码，固定快照且每个号码只测试一次。'
                        : '取所选号段内可用与 status=cannot_send 的号码，固定快照且每个号码只测试一次。'}
                  </Text>
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
                        { label: '抽样可用号码', value: 'available' },
                        { label: '仅失败样本', value: 'rejected' },
                      ]}
                    />
                  </Form.Item>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {phoneBindingSelectedPrefixes.length > 0
                      ? '已手动选择号段，抽样范围筛选本次不参与。'
                      : phoneBindingPrefixSampleFilter === 'available'
                        ? '只从号码自身可用的候选中每段抽 1/2 个号码。'
                        : phoneBindingPrefixSampleFilter === 'rejected'
                          ? '只复测有 OpenAI 拒绝记录的号码。'
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
                  ? '手机号池没有可测试的可用号码，请先启用或导入号码。'
                  : phoneBindingPrefixSampleFilter === 'rejected'
                  ? '手机号池没有可复测的 OpenAI 拒绝号码。'
                  : '手机号池没有可用于号段抽样的号码，请先启用/导入带 API 且未绑满的号码。'
                : phoneBindingPrefixBindEnabled
                ? `所选号段没有可测试的${phoneBindingPrefixNumberFilterLabel}号码。`
                : phoneBindingUnavailableNumberTestEnabled
                  ? '手机号池没有可复测的不可用号码（需要 status=cannot_send 且带收码 API）。'
                : '手机号池没有可用绑定容量，请先导入/重置号码，或展开临时粘贴号码。'}
            />
          ) : phoneBindingPoolShortage ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message={phoneBindingPrefixSampleEnabled
                ? `号段抽样候选不足：当前范围 ${phoneBindingTargetCount} 个账号，预计测试 ${phoneBindingExpectedTestCount} 个号码。`
                : phoneBindingPrefixBindEnabled && phoneBindingPrefixNumberFilter === 'available'
                  ? `所选号段容量不足：当前范围 ${phoneBindingTargetCount} 个账号，单号实际可分配 ${phoneBindingLimitedCapacity}`
                  : phoneBindingPrefixBindEnabled
                    ? `所选号段${phoneBindingPrefixNumberFilterLabel}候选不足：当前范围 ${phoneBindingTargetCount} 个账号，候选 ${phoneBindingLimitedCandidatePhones} 个，预计测试 ${phoneBindingExpectedTestCount} 个。`
                    : phoneBindingUnavailableNumberTestEnabled
                      ? `不可用号码候选不足：当前范围 ${phoneBindingTargetCount} 个账号，固定候选 ${phoneBindingCandidatePhoneCount} 个，预计测试 ${phoneBindingExpectedTestCount} 个。`
                      : `手机号池容量不足：当前范围 ${phoneBindingTargetCount} 个账号，剩余绑定容量 ${phoneBindingDynamicCapacity}`}
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
                  {phoneBindingUnavailableNumberTestEnabled
                    ? '不可用号码全量复测只使用手机号池固定快照；如需粘贴固定号码，请切回普通绑定。'
                    : phoneBindingPrefixBindEnabled
                    ? '限定号段绑定只使用手机号池；如需粘贴固定号码，请切回普通绑定。'
                    : phoneBindingPrefixSampleEnabled
                    ? '号段抽样测试只使用手机号池；如需粘贴固定号码，请切回普通绑定。'
                    : '每行：手机号 + 收码API；推荐 +手机号----https://...，也兼容 手机号---https://... / 手机号|https://...，会自动导入手机号池并回写结果。'}
                </Text>
              </div>
              <Button
                size="small"
                type={phoneBindingShowManualInput ? 'default' : 'dashed'}
                icon={<UploadOutlined />}
                disabled={phoneBindingPoolMode !== 'normal'}
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
                  placeholder={'+13434832954----https://api.sms8.net/api/record?token=...\n17632154294---https://phonenum.example.com/7632154294\n+12082260171|https://sms24.uk/api/sms/recordText?token=...&tpl=1'}
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
                    updatePhoneBindingSettings({
                      prefix_sms_probe_only: checked,
                      reuse_phone_until_unusable: checked ? false : phoneBindingReusePhoneValue,
                    })
                  }}
                />
              </Form.Item>
              <Text strong style={{ fontSize: 13 }}>只测发码/收码，不提交验证码</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                粘贴号码、普通手机号池、限定号段、号段抽样、不可用号码复测都生效；不保存账号绑定状态，不补抓 Auth/RT。
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
                name="concurrency"
                label="并发数"
                extra={phoneBindingReusePhoneValue
                  ? '同号连续绑定模式必须串行，避免多个账号抢同一个手机号。'
                  : '同时处理的账号数；不设固定上限，实际并发不会超过本次账号数和可用号码数。账号/号码间隔会作为初始启动错峰。'}
              >
                <InputNumber min={1} step={1} precision={0} disabled={Boolean(phoneBindingReusePhoneValue)} style={{ width: '100%' }} />
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
                  label={phoneBindingProxyMode === 'dynamic' ? '动态节点（本次任务可覆盖）' : '指定代理'}
                  rules={phoneBindingProxyMode === 'specified' ? [{ required: true, message: '请填写代理地址' }] : undefined}
                  extra={phoneBindingProxyMode === 'dynamic' ? '留空沿用全局动态节点；填写后仅覆盖本次手机号绑定任务。节点需包含 region-XX。全局配置请到代理管理页保存。' : '容器内建议使用 http://host.docker.internal:110xx。'}
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
                extra={phoneBindingUnavailableNumberTestEnabled
                  ? '不可用号码全量复测固定关闭，每个固定候选只测试一次。'
                  : phoneBindingPrefixBindEnabled && phoneBindingPrefixNumberFilter !== 'available'
                    ? '限定号段的不可用/全部号码筛选固定关闭，每个固定候选只测试一次。'
                  : phoneBindingPrefixSampleEnabled
                    ? '号段抽样模式下固定关闭，每个抽中的号码只测试一次。'
                  : phoneBindingSmsProbeOnlyValue
                  ? '只测发码/收码模式下固定关闭，避免同一号码重复探测。'
                  : '开启后，同一个号码会连续绑定多个账号，直到达到上限、限流、无法发码或接口异常。'}
              >
                <Switch
                  checkedChildren="开启"
                  unCheckedChildren="关闭"
                  disabled={phoneBindingFixedSnapshotSelection || Boolean(phoneBindingSmsProbeOnlyValue)}
                  onChange={(checked) => {
                    updatePhoneBindingSettings({
                      reuse_phone_until_unusable: checked,
                      ...(checked ? { concurrency: 1 } : {}),
                    })
                  }}
                />
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
                : `范围：当前筛选 ${total} 个账号，可提交 ${paypalFilteredEligibleCountLabel} 个`
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
                { value: 'filtered', label: `当前筛选账号（${total}，可提交 ${paypalFilteredEligibleCountLabel}）` },
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
        title="iDEAL / PIX 批量提交"
        open={baxiCdkSubmitOpen}
        onCancel={() => {
          baxiCdkSubmitForm.setFieldsValue({ pix_cdk: '', pix_cdk_lines: '' })
          setBaxiCdkSubmitOpen(false)
        }}
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
            description={baxiIsPix
              ? baxiPixUsesSavedLink
                ? '按账号范围读取已保存的 Stripe PIX 指令链接直接上传；不读取 Access Token。本站多额度 CDK 按剩余额度串行复用，同卡绝不并发，只有确认 paid 后才进入下一账号；明确失败会释放，余额不足仅停止本轮使用，网络、5xx、缺少轮询凭据或未知结果继续锁定人工复核。外部 PIX CDK 保持一次性规则。'
                : 'PIX 自动提链：每行一个外部 PIX CDK，提交时读取账号 Access Token。明确失败会释放该 CDK 继续尝试；支付成功、处理中或未知结果不会再次使用同一外部 CDK。'
              : 'iDEAL 提交成功指上游 /api/submit 返回 ok 和 order_id；不会阻塞等待 paid，默认会把订单加入后台轮询，查到状态后同步卡密池和绑定账号。'}
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
                placeholder="0 = 按账号范围全部提交"
                style={{ width: '100%' }}
              />
            </Form.Item>
          </div>

          <Form.Item name="payment_channel" label="支付通道" style={{ marginBottom: 12 }}>
            <Segmented
              block
              options={[
                { label: 'iDEAL', value: 'ideal' },
                { label: 'PIX', value: 'pix' },
              ]}
              onChange={() => {
                baxiCdkSubmitForm.setFieldValue('pix_cdk', '')
                baxiCdkSubmitForm.setFieldValue('pix_cdk_lines', '')
                setBaxiCdkManualOpen(false)
              }}
            />
          </Form.Item>

          {baxiIsPix ? (
            <Form.Item name="pix_submit_mode" label="PIX 提交方式" style={{ marginBottom: 12 }}>
              <Segmented
                block
                options={[
                  { label: '自动提链', value: 'auto_extract' },
                  { label: '上传已保存 PIX 链接', value: 'user_link' },
                ]}
              />
            </Form.Item>
          ) : null}

          {baxiIsPix ? (
            <div
              style={{
                border: `1px solid ${token.colorBorderSecondary}`,
                borderRadius: token.borderRadiusLG,
                padding: 12,
                marginBottom: 12,
                background: token.colorFillAlter,
              }}
            >
              <Form.Item
                name="pix_cdk_lines"
                label="PIX CDK / 本站额度 CDK（每行一个）"
                rules={[{ required: true, whitespace: true, message: '请每行输入一个 PIX CDK' }]}
                style={{ marginBottom: 0 }}
                extra={baxiPixUsesSavedLink
                  ? '可填本站多额度 CDK 或外部 PIX CDK。本站卡按余额串行复用，确认 paid 后才释放下一额度；外部卡成功后不复用。最多 100 个；同一输入只保留一次，避免并发使用同卡。'
                  : '最多 100 个外部 PIX CDK。明确失败会释放 CDK 继续尝试；支付成功、处理中或待复核的 CDK 不会再次提交。'}
              >
                <Input.TextArea
                  autoComplete="off"
                  placeholder={'PIX-CDK-1\nPIX-CDK-2'}
                  autoSize={{ minRows: 4, maxRows: 10 }}
                  spellCheck={false}
                />
              </Form.Item>
            </div>
          ) : null}

          {!baxiIsPix ? (
            <>
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
            </>
          ) : null}

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
                extra={baxiIsPix
                  ? '同一 CDK 始终串行；上游未确认的提交不会自动重投。不同 CDK 可各自处理一笔。'
                  : '只在上一个账号 /api/submit 成功后等待；预查失败、无配额或提交失败不会等待。'}
              >
                <InputNumber min={0} max={3600} step={1} addonAfter="秒" style={{ width: '100%' }} />
              </Form.Item>
              {!baxiIsPix ? (
                <Form.Item
                  name="auto_poll_status"
                  label="自动轮询状态"
                  valuePropName="checked"
                  extra="不阻塞下一个账号提交；后台按上游任务 ID 查询到 paid/failed 后同步卡密池和账号。"
                >
                  <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                </Form.Item>
              ) : null}
              <Form.Item
                name="status_poll_interval_seconds"
                label="状态轮询间隔"
                extra={baxiIsPix
                  ? '新任务按此值统一轮询；运行中的旧任务保持创建时参数。批量提交先串行创建订单，再统一轮询；后端默认 5 秒。'
                  : '新任务按此值轮询；运行中的旧任务保持创建时参数；后端默认 5 秒。'}
              >
                <InputNumber
                  min={BAXIGPT_STATUS_POLL_INTERVAL_MIN_SECONDS}
                  max={BAXIGPT_STATUS_POLL_INTERVAL_MAX_SECONDS}
                  step={1}
                  addonAfter="秒"
                  style={{ width: '100%' }}
                />
              </Form.Item>
              <Form.Item name="status_poll_timeout_seconds" label="未返回提醒" extra="到点只写提醒日志，任务会继续等待上游终态，不再提前结束。">
                <InputNumber min={1800} max={86400} step={60} addonAfter="秒" style={{ width: '100%' }} />
              </Form.Item>
              {!baxiIsPix ? (
                <Form.Item name="precheck" label="提交前预查" valuePropName="checked">
                  <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                </Form.Item>
              ) : null}
              <Form.Item
                name="failure_continue"
                label="失败后继续"
                valuePropName="checked"
                extra={baxiIsPix ? '开启后，单个账号明确失败不会阻断后续 PIX 提交。' : '开启后，单个卡密或账号失败不会阻断后续配对。'}
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
        onWebSessionLoginTask={handleWebSessionLogin}
        onInvalidRecheckTask={handleInvalidRecheck}
        onPaymentEligibilityTask={handlePaymentEligibility}
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
                    Plus + RT + 已确认手机号绑定 → PLUS--已接美国长效；Plus 缺少 RT 或绑定未确认 → PLUS--未接码；Free + RT → FREE--已接码带RT。
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
