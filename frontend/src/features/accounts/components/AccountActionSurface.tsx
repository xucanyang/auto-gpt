import { useEffect, useRef, useState } from 'react'
import {
  Alert,
  AutoComplete,
  Button,
  Checkbox,
  Collapse,
  Dropdown,
  Form,
  Input,
  InputNumber,
  message,
  Modal,
  Popconfirm,
  Select,
  Space,
  Steps,
  Tag,
  Typography,
  theme,
} from 'antd'
import type { MenuProps } from 'antd'
import { MoreOutlined, SyncOutlined } from '@ant-design/icons'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import { CliproxySyncSummary, LocalProbeSummary } from '@/features/accounts/components/AccountDetailModal'
import {
  DEFAULT_GOPAY_PHONE_COUNTRY_CODE,
  normalizeGopayPhonePart,
  normalizeGopayRecognizedCountryCodes,
  splitGopayPhoneInput,
} from '@/lib/gopayPhone'
import { apiFetch } from '@/lib/utils'
import { buildTaskProxyPayload, taskProxySettingsFromConfig, validateTaskProxySettings } from '@/lib/taskProxySettings'

const { Text } = Typography

const GOPAY_ACTIVE_PHASES = new Set(['created', 'starting', 'waiting_otp', 'waiting_link_pin', 'waiting_payment_pin', 'verifying'])
const DEFAULT_CHECKOUT_COUNTRY = 'ID'
const DEFAULT_CHECKOUT_CURRENCY = 'IDR'
const DEFAULT_PAYMENT_LINK_FORMAT = 'long_hosted'
const PAYMENT_LINK_FORMAT_OPTIONS = [
  { label: '长支付链接', value: 'long_hosted' },
  { label: '短连接路径', value: 'short_chatgpt' },
]
const DEFAULT_GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS = 120
const PAYMENT_LINK_REFRESH_DELAY_MS = 20_000
const CHECKOUT_COUNTRY_LABEL_OVERRIDES: Record<string, string> = {
  EU: '欧盟',
  US2: '美国 2',
  XK: '科索沃',
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

type AccountActionSurfaceProps = {
  account: any
  open: boolean
  onClose: () => void
  showShell?: boolean
  onRefresh: () => Promise<void> | void
  onOpenDetail?: (record: any) => void
  actionsLoading?: boolean
  actions: any[]
  onEnsureActionsLoaded?: () => Promise<void> | void
  initialActionId?: string | null
  initialActionMode?: 'direct' | 'dialog'
  onInitialActionHandled?: () => void
  onResumeAuthTask?: (record: any) => Promise<void> | void
  onInvalidRecheckTask?: (record: any) => Promise<void> | void
  onK12RecaptureTask?: (record: any, params?: Record<string, unknown>) => Promise<void> | void
  authStateMeta: (state?: string) => { color: string; label: string }
  planMeta: (plan?: string) => { color: string; label: string }
  codexStateMeta: (state?: string) => { color: string; label: string }
  formatSyncTime: (value?: string) => string
}

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
  const normalized = normalizePaymentLinkFormat(value)
  return normalized === 'short_chatgpt' ? '短连接路径' : '长支付链接'
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

function checkoutCountryName(code: string) {
  const normalized = normalizeCheckoutCountry(code)
  if (CHECKOUT_COUNTRY_LABEL_OVERRIDES[normalized]) return CHECKOUT_COUNTRY_LABEL_OVERRIDES[normalized]
  try {
    const displayNames = new Intl.DisplayNames(['zh-CN'], { type: 'region' })
    return displayNames.of(normalized) || normalized
  } catch {
    return normalized
  }
}

function checkoutCountryLabel(code: string, currency?: string) {
  const normalized = normalizeCheckoutCountry(code)
  const suffix = currency ? ` · ${normalizeCheckoutCurrency(currency)}` : ''
  return `${checkoutCountryName(normalized)} (${normalized})${suffix}`
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

function formatActionResultText(data: any, fallback: string) {
  const payload = data && typeof data === 'object' ? data : {}
  const logs = Array.isArray(payload.logs) ? payload.logs.filter(Boolean) : []
  const messageText = String(payload.message || fallback || '').trim()
  if (logs.length > 0) {
    return [messageText, ...logs].filter(Boolean).join('\n')
  }
  return messageText || fallback
}

function formatCachedPaymentLinkSummary(link: any) {
  if (!link || typeof link !== 'object') return ''
  const url = String(link.url || '').trim()
  if (!url) return ''
  const plan = String(link.plan || '').trim().toUpperCase() || 'PLUS'
  const country = String(link.country || '').trim().toUpperCase() || '-'
  const currency = String(link.currency || '').trim().toUpperCase() || '-'
  const linkFormat = paymentLinkFormatLabel(link.payment_link_format)
  const source = String(link.source || '').trim() || 'unknown'
  return `${plan} · ${country} / ${currency} · ${linkFormat} · ${source}`
}

export function AccountActionSurface({
  account,
  open,
  onClose,
  showShell = true,
  onRefresh,
  onOpenDetail,
  actionsLoading = false,
  actions,
  onEnsureActionsLoaded,
  initialActionId = null,
  initialActionMode = 'dialog',
  onInitialActionHandled,
  onResumeAuthTask,
  onInvalidRecheckTask,
  onK12RecaptureTask,
  authStateMeta,
  planMeta,
  codexStateMeta,
  formatSyncTime,
}: AccountActionSurfaceProps) {
  const { token } = theme.useToken()
  const acc = (account && typeof account === 'object' ? account : {}) as any
  const accountIdentity = acc?.id == null ? '' : String(acc.id)
  const [actionForm] = Form.useForm()
  const [resultOpen, setResultOpen] = useState(false)
  const [actionOpen, setActionOpen] = useState(false)
  const [activeAction, setActiveAction] = useState<any>(null)
  const [resultTitle, setResultTitle] = useState('')
  const [resultStatus, setResultStatus] = useState<'success' | 'error'>('success')
  const [resultText, setResultText] = useState('')
  const [resultUrl, setResultUrl] = useState('')
  const [resultProbe, setResultProbe] = useState<any>(null)
  const [resultCliproxySync, setResultCliproxySync] = useState<any>(null)
  const [runningActionId, setRunningActionId] = useState('')
  const [checkoutCountries, setCheckoutCountries] = useState<string[]>([])
  const [checkoutCurrencyByCountry, setCheckoutCurrencyByCountry] = useState<Record<string, string>>({
    [DEFAULT_CHECKOUT_COUNTRY]: DEFAULT_CHECKOUT_CURRENCY,
  })
  const [checkoutCountriesLoading, setCheckoutCountriesLoading] = useState(false)
  const [checkoutConfigLoading, setCheckoutConfigLoading] = useState(false)
  const [paymentLinkGlobalDefaults, setPaymentLinkGlobalDefaults] = useState<Record<string, any>>({})
  const [paymentLinkProxyOptions, setPaymentLinkProxyOptions] = useState<Array<{ label: string; value: string }>>([
    { label: '不使用代理（直连）', value: '' },
  ])
  const [paymentLinkProxyLoading, setPaymentLinkProxyLoading] = useState(false)
  const [gopayForm] = Form.useForm()
  const [gopayInputForm] = Form.useForm()
  const gopayPhoneCountryCode = Form.useWatch('phone_country_code', gopayForm)
  const gopayPhoneNumber = Form.useWatch('phone_number', gopayForm)
  const [gopayOpen, setGopayOpen] = useState(false)
  const [gopaySnapshot, setGopaySnapshot] = useState<any>(acc.chatgptGopay || null)
  const [gopayLoading, setGopayLoading] = useState(false)
  const [gopayPhonePoolSaving, setGopayPhonePoolSaving] = useState(false)
  const [gopayBillingGenerating, setGopayBillingGenerating] = useState(false)
  const [gopaySubmitting, setGopaySubmitting] = useState(false)
  const [gopayResendingOtp, setGopayResendingOtp] = useState(false)
  const [gopayOtpDelaySaving, setGopayOtpDelaySaving] = useState(false)
  const [gopayStatusWarningShown, setGopayStatusWarningShown] = useState(false)
  const [gopayCheckoutUrl, setGopayCheckoutUrl] = useState('')
  const [gopayOtpAutoResendDelay, setGopayOtpAutoResendDelay] = useState(DEFAULT_GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS)
  const [gopayGlobalDefaults, setGopayGlobalDefaults] = useState<Record<string, any>>({})
  const [gopayPhoneCandidates, setGopayPhoneCandidates] = useState<GopayPhoneCandidate[]>([])
  const [gopayRecognizedCountryCodes, setGopayRecognizedCountryCodes] = useState<string[]>([DEFAULT_GOPAY_PHONE_COUNTRY_CODE])
  const [gopayConfigCollapsed, setGopayConfigCollapsed] = useState(false)
  const [browserAuthOpen, setBrowserAuthOpen] = useState(false)
  const [browserAuthSnapshot, setBrowserAuthSnapshot] = useState<any>(null)
  const [browserAuthLoading, setBrowserAuthLoading] = useState(false)
  const [browserAuthUrl, setBrowserAuthUrl] = useState('https://chatgpt.com/')
  const [browserAuthText, setBrowserAuthText] = useState('')
  const [browserAuthProxy, setBrowserAuthProxy] = useState('')
  const [browserAuthFreshProfile, setBrowserAuthFreshProfile] = useState(true)
  const actionProxyModeValue = Form.useWatch('proxy_mode', actionForm)
  const actionProxyFailoverValue = Form.useWatch('proxy_failover', actionForm)
  const autoHandledActionRef = useRef('')
  const paymentLinkRefreshTimerRef = useRef<number | null>(null)
  const initialActionKey = String(initialActionId || '').trim()
  const resolvedInitialActionId = initialActionKey === 'payment_link_regenerate' ? 'payment_link' : initialActionKey
  const shouldForceRegeneratePaymentLink = initialActionKey === 'payment_link_regenerate'

  useEffect(() => {
    if (!open || gopayOpen) return
    setGopaySnapshot(acc.chatgptGopay || null)
    setGopayCheckoutUrl('')
    setGopayConfigCollapsed(false)
    setActionOpen(false)
    setResultOpen(false)
  }, [accountIdentity, open, gopayOpen])

  useEffect(() => {
    if (!open || gopayOpen) return
    setGopaySnapshot(acc.chatgptGopay || null)
  }, [acc.chatgptGopay, open, gopayOpen])

  useEffect(() => {
    return () => {
      if (paymentLinkRefreshTimerRef.current !== null) {
        window.clearTimeout(paymentLinkRefreshTimerRef.current)
      }
    }
  }, [])

  useEffect(() => {
    if (!open || actions.length > 0 || !onEnsureActionsLoaded) return
    void onEnsureActionsLoaded()
  }, [open, actions.length, onEnsureActionsLoaded])

  useEffect(() => {
    if (!gopayOpen || !gopaySnapshot?.session_id || !GOPAY_ACTIVE_PHASES.has(String(gopaySnapshot.phase || ''))) return
    const timer = window.setInterval(async () => {
      try {
        const data = await apiFetch(`/chatgpt/${acc.id}/gopay/${encodeURIComponent(gopaySnapshot.session_id)}`)
        setGopaySnapshot(data)
        setGopayStatusWarningShown(false)
      } catch (e: any) {
        if (!gopayStatusWarningShown) {
          message.warning(e?.message || '读取 GoPay 状态失败')
          setGopayStatusWarningShown(true)
        }
      }
    }, 3000)
    return () => window.clearInterval(timer)
  }, [acc.id, gopayOpen, gopaySnapshot?.session_id, gopaySnapshot?.phase, gopayStatusWarningShown])

  useEffect(() => {
    if (gopaySnapshot?.session_id && GOPAY_ACTIVE_PHASES.has(String(gopaySnapshot.phase || ''))) {
      setGopayConfigCollapsed(true)
    }
  }, [gopaySnapshot?.session_id, gopaySnapshot?.phase])

  const showResult = (title: string, status: 'success' | 'error', text: string, url = '', probe: any = null, cliproxySync: any = null) => {
    setResultTitle(title)
    setResultStatus(status)
    setResultText(text)
    setResultUrl(url)
    setResultProbe(probe)
    setResultCliproxySync(cliproxySync)
    setResultOpen(true)
  }

  const cachedPaymentLink = () => (
    acc.chatgptLastPaymentLink && typeof acc.chatgptLastPaymentLink === 'object'
      ? acc.chatgptLastPaymentLink
      : {}
  )

  const showCachedPaymentLinkResult = (action: any) => {
    const cachedLink = cachedPaymentLink()
    const cachedUrl = String(cachedLink.url || '').trim()
    if (!cachedUrl) return false
    const label = action?.label || '订阅链接'
    message.success('已复用缓存订阅链接')
    showResult(label, 'success', '已复用账号数据中的缓存订阅链接。', cachedUrl)
    return true
  }

  const copyResultUrl = async () => {
    if (!resultUrl) return
    try {
      const ok = await copyTextToClipboard(resultUrl)
      if (ok) {
        message.success('链接已复制')
        return
      }
      throw new Error('copy failed')
    } catch {
      message.error('复制失败')
    }
  }

  const refreshAfterPaymentLinkResult = () => {
    if (paymentLinkRefreshTimerRef.current !== null) {
      window.clearTimeout(paymentLinkRefreshTimerRef.current)
    }
    paymentLinkRefreshTimerRef.current = window.setTimeout(() => {
      paymentLinkRefreshTimerRef.current = null
      void Promise.resolve(onRefresh()).catch((e: any) => {
        message.warning(e?.message || '订阅链接已生成，但刷新账号列表失败')
      })
    }, PAYMENT_LINK_REFRESH_DELAY_MS)
  }

  const browserAuthPath = (suffix = '') => {
    const captureId = browserAuthSnapshot?.capture_id
    if (!captureId) return ''
    return `/chatgpt/${acc.id}/browser-auth/${encodeURIComponent(captureId)}${suffix}`
  }

  const startBrowserAuth = async () => {
    setBrowserAuthOpen(true)
    setBrowserAuthSnapshot(null)
    setBrowserAuthLoading(true)
    try {
      const data = await apiFetch(`/chatgpt/${acc.id}/browser-auth/start`, {
        method: 'POST',
        body: JSON.stringify({
          url: browserAuthUrl,
          proxy: browserAuthProxy,
          fresh_profile: browserAuthFreshProfile,
        }),
      })
      setBrowserAuthSnapshot(data)
      message.success('浏览器登录会话已启动')
    } catch (e: any) {
      message.error(e?.message || '启动浏览器登录失败')
      setBrowserAuthSnapshot({
        error: e?.message || '启动浏览器登录失败',
        title: '启动失败',
        url: browserAuthUrl,
      })
    } finally {
      setBrowserAuthLoading(false)
    }
  }

  const refreshBrowserAuth = async () => {
    const path = browserAuthPath()
    if (!path) return
    setBrowserAuthLoading(true)
    try {
      setBrowserAuthSnapshot(await apiFetch(path))
    } catch (e: any) {
      message.error(e?.message || '刷新浏览器画面失败')
    } finally {
      setBrowserAuthLoading(false)
    }
  }

  const navigateBrowserAuth = async () => {
    const path = browserAuthPath('/navigate')
    if (!path) return startBrowserAuth()
    setBrowserAuthLoading(true)
    try {
      const data = await apiFetch(path, {
        method: 'POST',
        body: JSON.stringify({ url: browserAuthUrl }),
      })
      setBrowserAuthSnapshot(data)
    } catch (e: any) {
      message.error(e?.message || '跳转失败')
    } finally {
      setBrowserAuthLoading(false)
    }
  }

  const clickBrowserAuth = async (event: any) => {
    const path = browserAuthPath('/click')
    if (!path || !browserAuthSnapshot) return
    const rect = event.currentTarget.getBoundingClientRect()
    const x = ((event.clientX - rect.left) / rect.width) * Number(browserAuthSnapshot.width || rect.width)
    const y = ((event.clientY - rect.top) / rect.height) * Number(browserAuthSnapshot.height || rect.height)
    setBrowserAuthLoading(true)
    try {
      setBrowserAuthSnapshot(await apiFetch(path, {
        method: 'POST',
        body: JSON.stringify({ x, y }),
      }))
    } catch (e: any) {
      message.error(e?.message || '点击失败')
    } finally {
      setBrowserAuthLoading(false)
    }
  }

  const sendBrowserAuthText = async () => {
    const path = browserAuthPath('/type')
    const text = browserAuthText
    if (!path || !text) return
    setBrowserAuthLoading(true)
    try {
      setBrowserAuthSnapshot(await apiFetch(path, {
        method: 'POST',
        body: JSON.stringify({ text }),
      }))
      setBrowserAuthText('')
    } catch (e: any) {
      message.error(e?.message || '输入失败')
    } finally {
      setBrowserAuthLoading(false)
    }
  }

  const sendBrowserAuthKey = async (key: string) => {
    const path = browserAuthPath('/key')
    if (!path) return
    setBrowserAuthLoading(true)
    try {
      setBrowserAuthSnapshot(await apiFetch(path, {
        method: 'POST',
        body: JSON.stringify({ key }),
      }))
    } catch (e: any) {
      message.error(e?.message || '按键失败')
    } finally {
      setBrowserAuthLoading(false)
    }
  }

  const injectBrowserBilling = async () => {
    const path = browserAuthPath('/inject-billing')
    if (!path) return
    setBrowserAuthLoading(true)
    try {
      const data = await apiFetch(path, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      setBrowserAuthSnapshot(data)
      message.success('已尝试注入地址信息')
    } catch (e: any) {
      message.error(e?.message || '注入地址失败')
    } finally {
      setBrowserAuthLoading(false)
    }
  }

  const captureBrowserAuth = async () => {
    const path = browserAuthPath('/capture')
    if (!path) return
    setBrowserAuthLoading(true)
    try {
      const data = await apiFetch(path, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      setBrowserAuthSnapshot(null)
      setBrowserAuthOpen(false)
      message.success(data.message || '浏览器登录态已保存')
      showResult('浏览器登录', 'success', JSON.stringify(data, null, 2))
      await onRefresh()
    } catch (e: any) {
      message.error(e?.message || '保存登录态失败')
    } finally {
      setBrowserAuthLoading(false)
    }
  }

  const closeBrowserAuth = async () => {
    const path = browserAuthPath('/close')
    if (path) {
      try {
        await apiFetch(path, {
          method: 'POST',
          body: JSON.stringify({}),
        })
      } catch {
        // Closing is best effort; the backend also replaces old sessions on start.
      }
    }
    setBrowserAuthSnapshot(null)
    setBrowserAuthOpen(false)
  }

  const runAction = async (action: any, params: Record<string, any> = {}) => {
    const actionId = String(action?.id || '')
    const actionLabel = action?.label || actionId

    if (actionId === 'resume_subscription_auth' && onResumeAuthTask) {
      await onResumeAuthTask(acc)
      return
    }
    if (actionId === 'invalid_recheck' && onInvalidRecheckTask) {
      await onInvalidRecheckTask(acc)
      return
    }
    if (actionId === 'k12_workspace_recapture' && onK12RecaptureTask) {
      await onK12RecaptureTask(acc, params)
      return
    }

    try {
      setRunningActionId(actionId)
      const r = await apiFetch(`/actions/${acc.platform}/${acc.id}/${actionId}`, {
        method: 'POST',
        body: JSON.stringify({ params }),
      })
      if (!r.ok) {
        const data = r.data || {}
        const probe = typeof data === 'object' && data ? data.probe || null : null
        const cliproxySync = typeof data === 'object' && data ? data.sync || null : null
        showResult(actionLabel, 'error', formatActionResultText(data, r.error || data.message || '操作失败'), '', probe, cliproxySync)
        await onRefresh()
        return
      }
      const data = r.data || {}
      if (data.url || data.checkout_url || data.cashier_url) {
        const targetUrl = data.url || data.checkout_url || data.cashier_url
        message.success('链接已生成')
        showResult(actionLabel, 'success', '操作成功，请在弹窗中打开或复制链接。', targetUrl)
        if (actionId === 'payment_link') {
          refreshAfterPaymentLinkResult()
        } else {
          await onRefresh()
        }
        return
      }

      message.success(data.message || '操作成功')
      const probe = typeof data === 'object' && data ? data.probe || null : null
      const cliproxySync = typeof data === 'object' && data ? data.sync || null : null
      const text =
        probe || cliproxySync
          ? formatActionResultText(data, '操作成功')
          : typeof data === 'string'
          ? data
          : Array.isArray(data.logs)
          ? formatActionResultText(data, '操作成功')
          : Object.keys(data).length > 0
          ? JSON.stringify(data, null, 2)
          : '操作成功'
      showResult(actionLabel, 'success', text, '', probe, cliproxySync)
      await onRefresh()
    } catch (e: any) {
      const detail = e?.message ? String(e.message) : '请求失败'
      message.error(detail)
      showResult(actionLabel, 'error', detail)
    } finally {
      setRunningActionId('')
    }
  }

  const loadCheckoutCountries = async (force = false, proxy?: string) => {
    if (!force && checkoutCountries.length > 0) return checkoutCountries
    setCheckoutCountriesLoading(true)
    try {
      const normalizedProxy = proxy === undefined ? undefined : String(proxy || '').trim()
      const query = new URLSearchParams()
      if (normalizedProxy !== undefined) query.set('proxy', normalizedProxy)
      const data = await apiFetch(`/chatgpt/payment-countries${query.toString() ? `?${query.toString()}` : ''}`)
      const countries = Array.from(new Set((data.countries || []).map((item: any) => normalizeCheckoutCountry(item)))) as string[]
      setCheckoutCountries(countries)
      return countries
    } catch (e: any) {
      message.warning(e?.message || '读取国家列表失败，使用默认地区')
      return checkoutCountries.length ? checkoutCountries : [DEFAULT_CHECKOUT_COUNTRY]
    } finally {
      setCheckoutCountriesLoading(false)
    }
  }

  const loadCheckoutConfig = async (country: string, targetForm = actionForm, proxy?: string) => {
    const normalizedCountry = normalizeCheckoutCountry(country)
    setCheckoutConfigLoading(true)
    try {
      const normalizedProxy = proxy === undefined ? undefined : String(proxy || '').trim()
      const query = new URLSearchParams()
      if (normalizedProxy !== undefined) query.set('proxy', normalizedProxy)
      const data = await apiFetch(`/chatgpt/payment-config/${encodeURIComponent(normalizedCountry)}${query.toString() ? `?${query.toString()}` : ''}`)
      const currency = normalizeCheckoutCurrency(data.symbol_code)
      setCheckoutCurrencyByCountry((prev) => ({ ...prev, [normalizedCountry]: currency }))
      targetForm.setFieldsValue({ country: normalizedCountry, currency })
      return currency
    } catch (e: any) {
      const fallback = checkoutCurrencyByCountry[normalizedCountry] || DEFAULT_CHECKOUT_CURRENCY
      targetForm.setFieldsValue({ country: normalizedCountry, currency: fallback })
      message.warning(e?.message || '读取货币配置失败，已使用本地默认值')
      return fallback
    } finally {
      setCheckoutConfigLoading(false)
    }
  }

  const buildPaymentLinkFormValues = (defaults: Record<string, any> = {}) => {
    const initialCountry = normalizeCheckoutCountry(defaults.country || DEFAULT_CHECKOUT_COUNTRY)
    const initialCurrency = normalizeCheckoutCurrency(
      defaults.currency
      || checkoutCurrencyByCountry[initialCountry]
      || DEFAULT_CHECKOUT_CURRENCY,
    )
    const normalizedPlan = String(defaults.plan || 'plus').trim().toLowerCase() === 'team' ? 'team' : 'plus'
    const seatQuantity = Number(defaults.seat_quantity)
    return {
      plan: normalizedPlan,
      country: initialCountry,
      currency: initialCurrency,
      proxy: String(defaults.proxy || '').trim(),
      payment_link_format: normalizePaymentLinkFormat(defaults.payment_link_format),
      promo_code: String(defaults.promo_code || 'STRIPEATLASGPT4BIZ050126').trim(),
      workspace_name: String(defaults.workspace_name || 'MyTeam').trim() || 'MyTeam',
      seat_quantity: Number.isFinite(seatQuantity) ? Math.max(2, Math.trunc(seatQuantity)) : 5,
      price_interval: String(defaults.price_interval || 'month').trim().toLowerCase() === 'year' ? 'year' : 'month',
      save_defaults: defaults.save_defaults !== false,
    }
  }

  const loadPaymentLinkGlobalDefaults = async (force = false) => {
    if (!force && Object.keys(paymentLinkGlobalDefaults).length > 0) return paymentLinkGlobalDefaults
    try {
      const data = await apiFetch('/config')
      const defaults = parseMaybeJsonObject(data.chatgpt_payment_link_defaults)
      setPaymentLinkGlobalDefaults(defaults)
      return defaults
    } catch {
      setPaymentLinkGlobalDefaults({})
      return {}
    }
  }

  const loadPaymentLinkProxyOptions = async (force = false, preservedProxy = '') => {
    if (!force && paymentLinkProxyOptions.length > 1) return paymentLinkProxyOptions
    setPaymentLinkProxyLoading(true)
    try {
      const data = await apiFetch('/proxies')
      const items = Array.isArray(data) ? data : []
      const options = [
        { label: '不使用代理（直连）', value: '' },
        ...items
          .filter((item: any) => item && item.is_active !== false && String(item.url || '').trim())
          .map((item: any) => {
            const url = String(item.url || '').trim()
            const region = String(item.region || '').trim()
            return {
              label: region ? `${region} · ${url}` : url,
              value: url,
            }
          }),
      ]
      const normalizedPreserved = String(preservedProxy || '').trim()
      if (normalizedPreserved && !options.some((item) => item.value === normalizedPreserved)) {
        options.push({
          label: `历史代理 · ${normalizedPreserved}`,
          value: normalizedPreserved,
        })
      }
      setPaymentLinkProxyOptions(options)
      return options
    } catch {
      const normalizedPreserved = String(preservedProxy || '').trim()
      const fallbackOptions = normalizedPreserved
        ? [
            { label: '不使用代理（直连）', value: '' },
            { label: `历史代理 · ${normalizedPreserved}`, value: normalizedPreserved },
          ]
        : [{ label: '不使用代理（直连）', value: '' }]
      setPaymentLinkProxyOptions(fallbackOptions)
      return fallbackOptions
    } finally {
      setPaymentLinkProxyLoading(false)
    }
  }

  const savePaymentLinkGlobalDefaults = async (values: Record<string, any>) => {
    const payload = {
      plan: String(values.plan || 'plus').trim().toLowerCase() === 'team' ? 'team' : 'plus',
      country: normalizeCheckoutCountry(values.country),
      currency: normalizeCheckoutCurrency(values.currency),
      proxy: String(values.proxy || '').trim(),
      payment_link_format: normalizePaymentLinkFormat(values.payment_link_format),
      promo_code: String(values.promo_code || '').trim(),
      workspace_name: String(values.workspace_name || 'MyTeam').trim() || 'MyTeam',
      seat_quantity: Math.max(2, Math.trunc(Number(values.seat_quantity || 5) || 5)),
      price_interval: String(values.price_interval || 'month').trim().toLowerCase() === 'year' ? 'year' : 'month',
    }
    await apiFetch('/config', {
      method: 'PUT',
      body: JSON.stringify({
        data: {
          chatgpt_payment_link_defaults: JSON.stringify(payload),
        },
      }),
    })
    setPaymentLinkGlobalDefaults(payload)
    return payload
  }

  const buildPaymentLinkDefaults = async () => {
    const globalDefaults = await loadPaymentLinkGlobalDefaults(true)
    const accountDefaults = acc.chatgptPaymentLinkDefaults && typeof acc.chatgptPaymentLinkDefaults === 'object'
      ? acc.chatgptPaymentLinkDefaults
      : {}
    return buildPaymentLinkFormValues({ ...globalDefaults, ...accountDefaults })
  }

  const canReusePaymentLinkCache = (values: Record<string, any>) => {
    const cachedLink = acc.chatgptLastPaymentLink && typeof acc.chatgptLastPaymentLink === 'object'
      ? acc.chatgptLastPaymentLink
      : {}
    const cachedUrl = String(cachedLink.url || '').trim()
    if (!cachedUrl) return false
    return (
      String(cachedLink.plan || '').trim().toLowerCase() === String(values.plan || 'plus').trim().toLowerCase()
      && normalizeCheckoutCountry(cachedLink.country || DEFAULT_CHECKOUT_COUNTRY) === normalizeCheckoutCountry(values.country)
      && normalizeCheckoutCurrency(cachedLink.currency || DEFAULT_CHECKOUT_CURRENCY) === normalizeCheckoutCurrency(values.currency)
      && String(cachedLink.proxy || '').trim() === String(values.proxy || '').trim()
      && normalizePaymentLinkFormat(cachedLink.payment_link_format) === normalizePaymentLinkFormat(values.payment_link_format)
    )
  }

  const buildGopayFormValues = (defaults: Record<string, any>, snapshot: Record<string, any> | null = null) => {
    const mergedDefaults = defaults || {}
    const initialCountry = normalizeCheckoutCountry(snapshot?.country || mergedDefaults.country || DEFAULT_CHECKOUT_COUNTRY)
    const initialCurrency = normalizeCheckoutCurrency(
      snapshot?.currency
      || mergedDefaults.currency
      || checkoutCurrencyByCountry[initialCountry]
      || DEFAULT_CHECKOUT_CURRENCY,
    )
    return {
      phone_country_code: DEFAULT_GOPAY_PHONE_COUNTRY_CODE,
      phone_number: '',
      pin: String(mergedDefaults.pin || '').trim(),
      proxy: String(mergedDefaults.proxy || '').trim(),
      browser_profile_mode: String(mergedDefaults.browser_profile_mode || 'fresh_payment').trim() || 'fresh_payment',
      save_defaults: true,
      access_token: '',
      plan: 'plus',
      country: initialCountry,
      currency: initialCurrency,
      checkout_url: '',
      billing_name: String(mergedDefaults.billing_name || 'John Doe').trim(),
      billing_email: String(mergedDefaults.billing_email || acc.email || 'buyer@example.com').trim(),
      billing_country: String(mergedDefaults.billing_country || 'US').trim() || 'US',
      billing_line1: String(mergedDefaults.billing_line1 || '3110 Sunset Boulevard').trim(),
      billing_city: String(mergedDefaults.billing_city || 'Los Angeles').trim(),
      billing_state: String(mergedDefaults.billing_state || 'CA').trim(),
      billing_postal_code: String(mergedDefaults.billing_postal_code || '90026').trim(),
    }
  }

  const loadGopayGlobalDefaults = async (force = false) => {
    if (!force && Object.keys(gopayGlobalDefaults).length > 0) return gopayGlobalDefaults
    try {
      const data = await apiFetch('/config')
      const defaults = parseMaybeJsonObject(data.chatgpt_gopay_defaults)
      setGopayPhoneCandidates(normalizeGopayPhoneCandidates(data.chatgpt_gopay_phone_candidates))
      setGopayGlobalDefaults(defaults)
      return defaults
    } catch {
      setGopayGlobalDefaults({})
      return {}
    }
  }

  const loadGopayOtpSettings = async () => {
    try {
      const data = await apiFetch('/integrations/gopay-otp')
      const delay = normalizeGopayOtpAutoResendDelay(data.otp_auto_resend_delay_seconds)
      const recognizedCodes = normalizeGopayRecognizedCountryCodes(data.recognized_country_codes)
      setGopayOtpAutoResendDelay(delay)
      setGopayRecognizedCountryCodes(recognizedCodes)
      return delay
    } catch {
      setGopayOtpAutoResendDelay(DEFAULT_GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS)
      setGopayRecognizedCountryCodes([DEFAULT_GOPAY_PHONE_COUNTRY_CODE])
      return DEFAULT_GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS
    }
  }

  const saveGopayOtpAutoResendDelay = async (
    value: unknown,
    options: { notify?: boolean; throwOnError?: boolean } = {},
  ) => {
    const delay = normalizeGopayOtpAutoResendDelay(value)
    setGopayOtpDelaySaving(true)
    try {
      const data = await apiFetch('/integrations/gopay-otp/settings', {
        method: 'PUT',
        body: JSON.stringify({ otp_auto_resend_delay_seconds: delay }),
      })
      const savedDelay = normalizeGopayOtpAutoResendDelay(data.otp_auto_resend_delay_seconds)
      setGopayOtpAutoResendDelay(savedDelay)
      if (options.notify !== false) message.success('GoPay OTP 自动重发延迟已保存')
      return savedDelay
    } catch (e: any) {
      if (options.notify !== false) message.error(e?.message || '保存 GoPay OTP 自动重发延迟失败')
      if (options.throwOnError) throw e
      return delay
    } finally {
      setGopayOtpDelaySaving(false)
    }
  }

  const saveGopayPhoneCandidates = async (nextCandidates: GopayPhoneCandidate[]) => {
    const normalized = normalizeGopayPhoneCandidates(nextCandidates)
    setGopayPhoneCandidates(normalized)
    await apiFetch('/config', {
      method: 'PUT',
      body: JSON.stringify({
        data: {
          chatgpt_gopay_phone_candidates: JSON.stringify(normalized),
        },
      }),
    })
    return normalized
  }

  const rememberCurrentGopayPhone = async (values: Record<string, any>) => {
    const { phone_country_code, phone_number } = splitGopayPhoneInput(
      values.phone_country_code,
      values.phone_number,
      gopayRecognizedCountryCodes,
    )
    if (!phone_country_code || !phone_number) return gopayPhoneCandidates
    return saveGopayPhoneCandidates(upsertGopayPhoneCandidate(gopayPhoneCandidates, {
      label: `GoPay ${gopayPhoneCandidates.length + 1}`,
      phone_country_code,
      phone_number,
      enabled: true,
    }))
  }

  const deleteCurrentGopayPhone = async (phoneId: string) => {
    await saveGopayPhoneCandidates(removeGopayPhoneCandidate(gopayPhoneCandidates, phoneId))
  }

  const moveCurrentGopayPhone = async (phoneId: string, direction: 'up' | 'down' | 'top' | 'bottom') => {
    await saveGopayPhoneCandidates(moveGopayPhoneCandidate(gopayPhoneCandidates, phoneId, direction))
  }

  const applyGopayPhoneInput = (value: unknown) => {
    const normalizedPhone = splitGopayPhoneInput(
      gopayForm.getFieldValue('phone_country_code'),
      value,
      gopayRecognizedCountryCodes,
    )
    gopayForm.setFieldsValue(normalizedPhone)
    return normalizedPhone.phone_number
  }

  const buildGopayDefaultsPayload = async () => {
    const values = await gopayForm.validateFields()
    return {
      pin: String(values.pin || '').trim(),
      proxy: String(values.proxy || '').trim(),
      browser_profile_mode: String(values.browser_profile_mode || 'fresh_payment').trim() || 'fresh_payment',
      country: normalizeCheckoutCountry(values.country),
      currency: normalizeCheckoutCurrency(values.currency),
      access_token: '',
      billing_name: String(values.billing_name || '').trim(),
      billing_email: String(values.billing_email || '').trim(),
      billing_country: String(values.billing_country || '').trim() || 'US',
      billing_line1: String(values.billing_line1 || '').trim(),
      billing_city: String(values.billing_city || '').trim(),
      billing_state: String(values.billing_state || '').trim(),
      billing_postal_code: String(values.billing_postal_code || '').trim(),
      checkout_url: '',
    }
  }

  const saveGopayDefaults = async () => {
    try {
      const payload = await buildGopayDefaultsPayload()
      setGopayLoading(true)
      await apiFetch('/config', {
        method: 'PUT',
        body: JSON.stringify({
          data: {
            chatgpt_gopay_defaults: JSON.stringify(payload),
          },
        }),
      })
      setGopayGlobalDefaults(payload)
      message.success('GoPay 默认参数已保存')
    } catch (e: any) {
      message.error(e?.message || '保存 GoPay 默认参数失败')
    } finally {
      setGopayLoading(false)
    }
  }

  const generateGopayBillingAddress = async () => {
    const values = gopayForm.getFieldsValue(true)
    const currentEmail = String(values.billing_email || '').trim()
    setGopayBillingGenerating(true)
    try {
      const data = await apiFetch(`/chatgpt/${acc.id}/gopay/generate-billing-address`, {
        method: 'POST',
        body: JSON.stringify({
          country: normalizeCheckoutCountry(values.country),
          billing_name: String(values.billing_name || '').trim(),
          billing_email: currentEmail,
          billing_country: String(values.billing_country || '').trim(),
          billing_line1: String(values.billing_line1 || '').trim(),
          billing_city: String(values.billing_city || '').trim(),
          billing_state: String(values.billing_state || '').trim(),
          billing_postal_code: String(values.billing_postal_code || '').trim(),
        }),
      })
      const billing = data?.billing && typeof data.billing === 'object' ? data.billing : {}
      gopayForm.setFieldsValue({
        billing_name: String(billing.billing_name || values.billing_name || '').trim(),
        billing_email: currentEmail,
        billing_country: String(billing.billing_country || values.billing_country || '').trim(),
        billing_line1: String(billing.billing_line1 || '').trim(),
        billing_city: String(billing.billing_city || '').trim(),
        billing_state: String(billing.billing_state || '').trim(),
        billing_postal_code: String(billing.billing_postal_code || '').trim(),
      })
      const target = String(data?.target_country || '').trim()
      const strategy = String(data?.strategy || '').trim()
      message.success(`GoPay 账单地址已生成${target ? `：${target}` : ''}${strategy ? ` / ${strategy}` : ''}`)
    } catch (e: any) {
      message.error(e?.message || '生成 GoPay 账单地址失败')
    } finally {
      setGopayBillingGenerating(false)
    }
  }

  const addCurrentGopayPhoneToPool = async () => {
    try {
      const normalizedPhone = splitGopayPhoneInput(
        gopayForm.getFieldValue('phone_country_code'),
        gopayForm.getFieldValue('phone_number'),
        gopayRecognizedCountryCodes,
      )
      gopayForm.setFieldsValue(normalizedPhone)
      const values = await gopayForm.validateFields(['phone_country_code', 'phone_number'])
      setGopayPhonePoolSaving(true)
      await rememberCurrentGopayPhone(values)
      message.success('手机号已加入候选池')
    } catch (e: any) {
      if (e?.errorFields) return
      message.error(e?.message || '加入手机号池失败')
    } finally {
      setGopayPhonePoolSaving(false)
    }
  }

  const openGopayDialog = async () => {
    const [globalDefaults] = await Promise.all([
      loadGopayGlobalDefaults(true),
      loadGopayOtpSettings(),
    ])
    const defaults = { ...(acc.chatgptGopayDefaults || {}), ...globalDefaults }
    const formValues = buildGopayFormValues(defaults, gopaySnapshot)
    formValues.checkout_url = ''
    formValues.access_token = ''
    setGopayCheckoutUrl('')
    gopayForm.setFieldsValue(formValues)
    gopayInputForm.resetFields()
    setGopayStatusWarningShown(false)
    setGopayConfigCollapsed(Boolean(gopaySnapshot?.session_id))
    setGopayOpen(true)
  }

  const startGopay = async () => {
    const values = await gopayForm.validateFields()
    setGopayLoading(true)
    try {
      await saveGopayOtpAutoResendDelay(gopayOtpAutoResendDelay, { notify: false, throwOnError: true })
      const country = normalizeCheckoutCountry(values.country)
      const checkoutUrl = String(values.checkout_url || '').trim()
      const normalizedPhone = splitGopayPhoneInput(
        values.phone_country_code,
        values.phone_number,
        gopayRecognizedCountryCodes,
      )
      gopayForm.setFieldsValue(normalizedPhone)
      const data = await apiFetch(`/chatgpt/${acc.id}/gopay/start`, {
        method: 'POST',
        body: JSON.stringify({
          phone_country_code: normalizedPhone.phone_country_code,
          phone_number: normalizedPhone.phone_number,
          pin: String(values.pin || '').trim(),
          access_token: String(values.access_token || '').trim(),
          proxy: String(values.proxy || '').trim(),
          browser_profile_mode: String(values.browser_profile_mode || 'fresh_payment').trim() || 'fresh_payment',
          save_defaults: values.save_defaults !== false,
          plan: 'plus',
          country,
          currency: normalizeCheckoutCurrency(values.currency),
          checkout_url: checkoutUrl,
          billing_name: String(values.billing_name || '').trim(),
          billing_email: String(values.billing_email || '').trim(),
          billing_country: String(values.billing_country || '').trim(),
          billing_line1: String(values.billing_line1 || '').trim(),
          billing_city: String(values.billing_city || '').trim(),
          billing_state: String(values.billing_state || '').trim(),
          billing_postal_code: String(values.billing_postal_code || '').trim(),
        }),
      })
      if (values.save_defaults !== false) {
        setGopayGlobalDefaults({
          pin: String(values.pin || '').trim(),
          proxy: String(values.proxy || '').trim(),
          browser_profile_mode: String(values.browser_profile_mode || 'fresh_payment').trim() || 'fresh_payment',
          country,
          currency: normalizeCheckoutCurrency(values.currency),
          access_token: '',
          billing_name: String(values.billing_name || '').trim(),
          billing_email: String(values.billing_email || '').trim(),
          billing_country: String(values.billing_country || '').trim() || 'US',
          billing_line1: String(values.billing_line1 || '').trim(),
          billing_city: String(values.billing_city || '').trim(),
          billing_state: String(values.billing_state || '').trim(),
          billing_postal_code: String(values.billing_postal_code || '').trim(),
          checkout_url: '',
        })
      }
      await rememberCurrentGopayPhone(values)
      setGopaySnapshot(data)
      setGopayStatusWarningShown(false)
      setGopayCheckoutUrl('')
      setGopayConfigCollapsed(true)
      message.success('GoPay 支付流程已启动')
      await onRefresh()
    } catch (e: any) {
      message.error(e?.message || '启动 GoPay 支付失败')
    } finally {
      setGopayLoading(false)
    }
  }

  const resetGopayFlow = () => {
    setGopaySnapshot(null)
    gopayInputForm.resetFields()
    const defaults = { ...(acc.chatgptGopayDefaults || {}), ...gopayGlobalDefaults }
    const formValues = buildGopayFormValues(defaults, null)
    formValues.checkout_url = ''
    formValues.access_token = ''
    gopayForm.resetFields()
    setGopayCheckoutUrl('')
    gopayForm.setFieldsValue(formValues)
    setGopayConfigCollapsed(false)
    setGopayStatusWarningShown(false)
  }

  const submitGopayInput = async () => {
    if (!gopaySnapshot?.session_id) return
    const phase = String(gopaySnapshot.phase || '')
    const values = await gopayInputForm.validateFields()
    const path = phase === 'waiting_otp' ? 'otp' : 'pin'
    const key = phase === 'waiting_otp' ? 'otp' : 'pin'
    setGopaySubmitting(true)
    try {
      const data = await apiFetch(`/chatgpt/${acc.id}/gopay/${encodeURIComponent(gopaySnapshot.session_id)}/${path}`, {
        method: 'POST',
        body: JSON.stringify({ [key]: String(values[key] || '').trim() }),
      })
      gopayInputForm.resetFields()
      setGopaySnapshot(data)
      setGopayStatusWarningShown(false)
      message.success('已提交')
      await onRefresh()
    } catch (e: any) {
      message.error(e?.message || '提交失败')
    } finally {
      setGopaySubmitting(false)
    }
  }

  const resendGopayOtp = async () => {
    if (!gopaySnapshot?.session_id) return
    setGopayResendingOtp(true)
    try {
      const data = await apiFetch(`/chatgpt/${acc.id}/gopay/${encodeURIComponent(gopaySnapshot.session_id)}/resend-otp`, {
        method: 'POST',
      })
      setGopaySnapshot(data)
      setGopayStatusWarningShown(false)
      message.success('GoPay OTP 重发请求已提交')
      await onRefresh()
    } catch (e: any) {
      message.error(e?.message || 'GoPay OTP 重发失败')
    } finally {
      setGopayResendingOtp(false)
    }
  }

  const cancelGopay = async () => {
    if (!gopaySnapshot?.session_id) {
      setGopayOpen(false)
      return
    }
    setGopayLoading(true)
    try {
      const data = await apiFetch(`/chatgpt/${acc.id}/gopay/${encodeURIComponent(gopaySnapshot.session_id)}/cancel`, {
        method: 'POST',
      })
      setGopaySnapshot(data)
      await onRefresh()
    } catch (e: any) {
      message.error(e?.message || '取消 GoPay 会话失败')
    } finally {
      setGopayLoading(false)
      setGopayOpen(false)
    }
  }

  const openActionDialog = async (action: any) => {
    setActiveAction(action)
    actionForm.resetFields()
    if (action.id === 'payment_link') {
      const initialValues = await buildPaymentLinkDefaults()
      await loadPaymentLinkProxyOptions(true, String(initialValues.proxy || '').trim())
      actionForm.setFieldsValue({
        ...initialValues,
        reuse_cached_link: canReusePaymentLinkCache(initialValues),
      })
      setActionOpen(true)
      return
    }
    if (action.id === 'k12_workspace_recapture') {
      let cfg: Record<string, any> = {}
      try {
        cfg = await apiFetch('/config')
      } catch {
        cfg = {}
      }
      actionForm.setFieldsValue({
        workspace_ids: '',
        save_all_spaces: true,
        strict_join: false,
        join_timeout_seconds: 60,
        join_retry_count: 2,
        post_join_poll_seconds: '3,8,15',
        ...taskProxySettingsFromConfig(cfg || {}, { proxy_mode: 'dynamic', proxy_failover: true }),
      })
      setActionOpen(true)
      return
    }
    const initialValues: Record<string, any> = {}
    for (const param of action.params || []) {
      if (param.default !== undefined) initialValues[param.key] = param.default
    }
    actionForm.setFieldsValue(initialValues)
    setActionOpen(true)
  }

  const handleAction = async (actionId: string) => {
    const action = actions.find((item) => item.id === actionId) || { id: actionId, label: actionId, params: [] }
    if ((action.params || []).length > 0) {
      await openActionDialog(action)
      return
    }
    await runAction(action, {})
  }

  const submitActionDialog = async () => {
    if (!activeAction) return
    const values = await actionForm.validateFields()
    const params = { ...values }
    if (activeAction.id === 'payment_link') {
      params.plan = String(params.plan || 'plus').trim().toLowerCase() || 'plus'
      params.country = normalizeCheckoutCountry(params.country)
      params.currency = normalizeCheckoutCurrency(params.currency)
      params.proxy = String(params.proxy || '').trim()
      params.payment_link_format = normalizePaymentLinkFormat(params.payment_link_format)
      params.promo_code = String(params.promo_code || '').trim()
      params.reuse_cached_link = params.reuse_cached_link !== false
      params.save_defaults = params.save_defaults !== false
      if (params.save_defaults) {
        await savePaymentLinkGlobalDefaults(params)
      }
    }
    if (activeAction.id === 'k12_workspace_recapture') {
      validateTaskProxySettings(values)
      Object.assign(params, buildTaskProxyPayload(values), {
        workspace_ids: String(values.workspace_ids || '').trim(),
        save_all_spaces: values.save_all_spaces !== false,
        strict_join: Boolean(values.strict_join),
        join_timeout_seconds: Number(values.join_timeout_seconds || 60),
        join_retry_count: Number(values.join_retry_count || 2),
        post_join_poll_seconds: String(values.post_join_poll_seconds || '3,8,15').trim(),
      })
    }
    setActionOpen(false)
    await runAction(activeAction, params)
  }

  const runPaymentLinkWithDefaults = async (action: any, options: { forceRegenerate?: boolean } = {}) => {
    if (!options.forceRegenerate && showCachedPaymentLinkResult(action)) return
    const values = await buildPaymentLinkDefaults()
    const params = {
      ...values,
      plan: String(values.plan || 'plus').trim().toLowerCase() || 'plus',
      country: normalizeCheckoutCountry(values.country),
      currency: normalizeCheckoutCurrency(values.currency),
      proxy: String(values.proxy || '').trim(),
      payment_link_format: normalizePaymentLinkFormat(values.payment_link_format),
      promo_code: String(values.promo_code || '').trim(),
      reuse_cached_link: options.forceRegenerate ? false : canReusePaymentLinkCache(values),
      save_defaults: false,
    }
    await runAction(action, params)
  }

  useEffect(() => {
    if (!open || !resolvedInitialActionId) {
      autoHandledActionRef.current = ''
      return
    }
    if (actionsLoading || actionOpen || resultOpen || gopayOpen || browserAuthOpen || runningActionId) return
    if (resolvedInitialActionId === 'gopay' && acc?.platform === 'chatgpt') {
      const signature = `${acc?.id || ''}:${initialActionKey || resolvedInitialActionId}`
      if (autoHandledActionRef.current === signature) return
      autoHandledActionRef.current = signature
      void openGopayDialog().finally(() => {
        onInitialActionHandled?.()
      })
      return
    }
    const matchedAction = actions.find((item) => String(item?.id || '') === resolvedInitialActionId)
    if (!matchedAction) return
    const signature = `${acc?.id || ''}:${initialActionKey || resolvedInitialActionId}`
    if (autoHandledActionRef.current === signature) return
    autoHandledActionRef.current = signature
    const runInitialAction = async () => {
      if (resolvedInitialActionId === 'payment_link' && initialActionMode === 'direct') {
        await runPaymentLinkWithDefaults(matchedAction, { forceRegenerate: shouldForceRegeneratePaymentLink })
        return
      }
      await handleAction(resolvedInitialActionId)
    }
    void runInitialAction().finally(() => {
      onInitialActionHandled?.()
    })
  }, [
    open,
    initialActionKey,
    resolvedInitialActionId,
    shouldForceRegeneratePaymentLink,
    initialActionMode,
    actionsLoading,
    actionOpen,
    resultOpen,
    gopayOpen,
    browserAuthOpen,
    runningActionId,
    actions,
    acc?.id,
    onInitialActionHandled,
  ])

  const renderActionFields = () => {
    if (!activeAction) return null
    if (activeAction.id === 'payment_link') {
      const selectedCountry = normalizeCheckoutCountry(actionForm.getFieldValue('country'))
      const selectedCurrency = normalizeCheckoutCurrency(actionForm.getFieldValue('currency'))
      const selectedProxy = String(actionForm.getFieldValue('proxy') || '').trim()
      const selectedPlan = String(actionForm.getFieldValue('plan') || 'plus').trim().toLowerCase()
      const selectedPaymentLinkFormat = normalizePaymentLinkFormat(actionForm.getFieldValue('payment_link_format'))
      const cachedLink = acc.chatgptLastPaymentLink && typeof acc.chatgptLastPaymentLink === 'object'
        ? acc.chatgptLastPaymentLink
        : {}
      const cachedUrl = String(cachedLink.url || '').trim()
      const cachedPlan = String(cachedLink.plan || '').trim().toLowerCase()
      const cachedCountry = normalizeCheckoutCountry(cachedLink.country || DEFAULT_CHECKOUT_COUNTRY)
      const cachedCurrency = normalizeCheckoutCurrency(cachedLink.currency || DEFAULT_CHECKOUT_CURRENCY)
      const cachedProxy = String(cachedLink.proxy || '').trim()
      const cachedPaymentLinkFormat = normalizePaymentLinkFormat(cachedLink.payment_link_format)
      const cacheMatchesCurrentSelection = Boolean(
        cachedUrl
        && cachedPlan === selectedPlan
        && cachedCountry === selectedCountry
        && cachedCurrency === selectedCurrency
        && cachedProxy === selectedProxy
        && cachedPaymentLinkFormat === selectedPaymentLinkFormat
      )
      const countryOptions = (checkoutCountries.length ? checkoutCountries : [DEFAULT_CHECKOUT_COUNTRY]).map((code) => {
        const currency = checkoutCurrencyByCountry[code]
        return { label: checkoutCountryLabel(code, currency), value: code }
      })
      return (
        <>
          <Form.Item name="plan" label="套餐" initialValue="plus" rules={[{ required: true }]}>
            <Select
              options={[
                { label: 'Plus', value: 'plus' },
                { label: 'Team', value: 'team' },
              ]}
            />
          </Form.Item>
          <Form.Item name="payment_link_format" label="生成路径" initialValue={DEFAULT_PAYMENT_LINK_FORMAT} rules={[{ required: true }]}>
            <Select options={PAYMENT_LINK_FORMAT_OPTIONS} />
          </Form.Item>
          <Form.Item label="国家" required>
            <Space.Compact style={{ width: '100%' }}>
              <Form.Item name="country" noStyle rules={[{ required: true, message: '请选择国家' }]}>
                <Select
                  showSearch
                  loading={checkoutCountriesLoading}
                  optionFilterProp="label"
                  options={countryOptions}
                  onDropdownVisibleChange={(open) => {
                    if (open) loadCheckoutCountries(false, String(actionForm.getFieldValue('proxy') || '').trim())
                  }}
                  onChange={(value) => loadCheckoutConfig(value, actionForm, String(actionForm.getFieldValue('proxy') || '').trim())}
                />
              </Form.Item>
              <Button loading={checkoutCountriesLoading} onClick={() => loadCheckoutCountries(true, String(actionForm.getFieldValue('proxy') || '').trim())}>
                刷新
              </Button>
            </Space.Compact>
          </Form.Item>
          <Form.Item name="currency" label="货币" initialValue={DEFAULT_CHECKOUT_CURRENCY} rules={[{ required: true }]}>
            <Input readOnly suffix={checkoutConfigLoading ? <SyncOutlined spin /> : null} />
          </Form.Item>
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={`当前选择：${checkoutCountryLabel(selectedCountry, selectedCurrency)}`}
            description={`生成路径：${paymentLinkFormatLabel(selectedPaymentLinkFormat)}。国家列表和货币只在打开选择器、刷新或切换国家时更新；生成链接时只使用这里显示的国家和货币。当前代理：${selectedProxy || '直连（不使用代理）'}`}
          />
          {cachedUrl ? (
            <Alert
              type={cacheMatchesCurrentSelection ? 'success' : 'warning'}
              showIcon
              style={{ marginBottom: 12 }}
              message={cacheMatchesCurrentSelection ? '检测到可直接复用的缓存订阅链接' : '检测到历史订阅链接，但当前参数与缓存不完全一致'}
              description={
                <>
                  <div>{formatCachedPaymentLinkSummary(cachedLink)}</div>
                  <div style={{ marginTop: 4, wordBreak: 'break-all' }}>{cachedUrl}</div>
                </>
              }
            />
          ) : null}
          <Form.Item
            name="proxy"
            label="支付代理节点"
            extra="可选。默认可直连生成订阅链接；如果 checkout 页面异常，再切换到某个代理节点重新生成。"
          >
            <Select
              showSearch
              allowClear
              optionFilterProp="label"
              placeholder="选择代理节点，留空为直连"
              loading={paymentLinkProxyLoading}
              options={paymentLinkProxyOptions}
              onDropdownVisibleChange={(open) => {
                if (open) loadPaymentLinkProxyOptions(false, String(actionForm.getFieldValue('proxy') || '').trim())
              }}
              onChange={async (value) => {
                await loadCheckoutConfig(
                  String(actionForm.getFieldValue('country') || DEFAULT_CHECKOUT_COUNTRY),
                  actionForm,
                  String(value || '').trim(),
                )
              }}
            />
          </Form.Item>
          <Form.Item
            name="reuse_cached_link"
            valuePropName="checked"
            extra="勾选后，若当前套餐/国家/货币/代理与缓存一致，则直接复用注册或上次生成时缓存的订阅链接；取消勾选则强制重新生成。"
          >
            <Checkbox disabled={!cacheMatchesCurrentSelection}>优先复用缓存订阅链接</Checkbox>
          </Form.Item>
          <Form.Item
            name="promo_code"
            label="Promo Code"
            extra="可选。Team 订阅链接会使用这里的 promo code；保存后下次打开会自动带出。"
          >
            <Input placeholder="STRIPEATLASGPT4BIZ050126" />
          </Form.Item>
          <Form.Item noStyle shouldUpdate={(prev, next) => prev.plan !== next.plan}>
            {({ getFieldValue }) =>
              getFieldValue('plan') === 'team' ? (
                <>
                  <Form.Item name="workspace_name" label="Team 名称" initialValue="MyTeam">
                    <Input />
                  </Form.Item>
                  <Form.Item name="seat_quantity" label="席位数量" initialValue={5}>
                    <InputNumber min={2} style={{ width: '100%' }} />
                  </Form.Item>
                  <Form.Item name="price_interval" label="计费周期" initialValue="month">
                    <Select
                      options={[
                        { label: '月付', value: 'month' },
                        { label: '年付', value: 'year' },
                      ]}
                    />
                  </Form.Item>
                </>
              ) : null
            }
          </Form.Item>
          <Form.Item name="save_defaults" valuePropName="checked" style={{ marginBottom: 0 }}>
            <Checkbox>保存本次订阅链接配置</Checkbox>
          </Form.Item>
        </>
      )
    }

    if (activeAction.id === 'k12_workspace_recapture') {
      const proxyMode = String(actionProxyModeValue || actionForm.getFieldValue('proxy_mode') || 'dynamic')
      const proxyFailover = Boolean(actionProxyFailoverValue)
      return (
        <>
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="复用当前账号已保存的 AccessToken + cookies/session_token"
            description="会重新执行 K12 join、拉取 accounts/check 空间列表并交换 workspace AccessToken；结果写回当前账号与 workspace variant 账号，弹窗结果只展示脱敏摘要。"
          />
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
          <Form.Item label="代理模式" name="proxy_mode">
            <Select style={{ width: 260 }}>
              <Select.Option value="pool">代理池自动选取</Select.Option>
              <Select.Option value="specified">手动指定代理</Select.Option>
              <Select.Option value="dynamic">动态代理</Select.Option>
              <Select.Option value="direct">直连 (不使用代理)</Select.Option>
            </Select>
          </Form.Item>
          {(proxyMode === 'specified' || proxyMode === 'dynamic') && (
            <Form.Item
              label={proxyMode === 'dynamic' ? '动态代理模板（可选覆盖）' : '代理地址'}
              name="proxy"
              rules={proxyMode === 'specified' ? [{ required: true, message: '请输入代理地址' }] : undefined}
              extra={proxyMode === 'dynamic' ? '留空使用全局动态代理模板；填写后仅本次重跑覆盖全局模板。' : undefined}
            >
              <Input placeholder={proxyMode === 'dynamic' ? '可留空；或填 socks5://user-region-JP-sid-xxxx-t-15:pass@host:port' : 'http://user:pass@host:port 或 socks5://...'} />
            </Form.Item>
          )}
          {(proxyMode === 'pool' || proxyMode === 'dynamic' || (proxyMode === 'specified' && proxyFailover)) && (
            <Space style={{ display: 'flex', flexWrap: 'wrap', gap: 16 }} align="baseline">
              <Form.Item
                label="目标国家 (ISO 缩写)"
                name="proxy_country_code"
                rules={proxyMode === 'dynamic' ? [{ required: true, message: '请输入动态代理出口国家' }] : undefined}
              >
                <Input style={{ width: 140 }} placeholder={proxyMode === 'dynamic' ? '必填，如 US' : '如 US, JP, 不填则不限'} />
              </Form.Item>
              {proxyMode !== 'dynamic' ? (
                <>
                  <Form.Item label="最低健康度分数" name="proxy_min_score">
                    <InputNumber min={0} max={100} step={5} style={{ width: 140 }} />
                  </Form.Item>
                  <Form.Item label="候选代理数量" name="proxy_max_candidates">
                    <InputNumber min={1} max={20} step={1} style={{ width: 140 }} />
                  </Form.Item>
                </>
              ) : null}
            </Space>
          )}
          {proxyMode !== 'direct' && (
            <Form.Item name="proxy_failover" valuePropName="checked">
              <Checkbox>{proxyMode === 'dynamic' ? '失败后刷新 sid 重试' : '使用多个候选代理，遇到网络失败时自动切换'}</Checkbox>
            </Form.Item>
          )}
        </>
      )
    }

    if (activeAction.id === 'logout_web_session') {
      return (
        <>
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="退出当前账号保存的 ChatGPT 网页会话"
            description="按浏览器退出流程使用 cookies + CSRF 发起 signout；成功后会清除本系统保存的 cookies/session_token。AccessToken 和 RefreshToken 不会撤销，也不会被删除。"
          />
          <Form.Item
            name="confirm_logout"
            valuePropName="checked"
            rules={[{ validator: (_, value) => value ? Promise.resolve() : Promise.reject(new Error('请勾选确认后再退出')) }]}
          >
            <Checkbox>我确认退出该账号的网页 Cookie 会话</Checkbox>
          </Form.Item>
        </>
      )
    }

    return (activeAction.params || []).map((param: any) => (
      <Form.Item
        key={param.key}
        name={param.key}
        label={param.type === 'boolean' ? undefined : param.label}
        initialValue={param.default}
        valuePropName={param.type === 'boolean' ? 'checked' : 'value'}
      >
        {param.type === 'select' ? (
          <Select options={(param.options || []).map((option: string) => ({ label: option, value: option }))} />
        ) : param.type === 'boolean' ? (
          <Checkbox>{param.label}</Checkbox>
        ) : (
          <Input />
        )}
      </Form.Item>
    ))
  }

  const renderGopayCurrentInput = () => {
    const phase = String(gopaySnapshot?.phase || '')
    if (phase === 'waiting_otp') {
      return (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Alert
            type="info"
            showIcon
            message={`自动重发延迟：${gopaySnapshot?.otp_auto_resend_delay_seconds ?? gopayOtpAutoResendDelay} 秒`}
            description={gopaySnapshot?.otp_auto_resend_done ? '本次会话已自动重发过 OTP。' : '自动重发只会在等待 OTP 阶段触发一次；设置为 0 可关闭。'}
          />
          <Space wrap>
            <Text type="secondary">全局自动重发延迟</Text>
            <InputNumber
              min={0}
              max={3600}
              precision={0}
              value={gopayOtpAutoResendDelay}
              onChange={(value) => setGopayOtpAutoResendDelay(normalizeGopayOtpAutoResendDelay(value))}
              addonAfter="秒"
            />
            <Button loading={gopayOtpDelaySaving} onClick={() => saveGopayOtpAutoResendDelay(gopayOtpAutoResendDelay)}>
              保存延迟
            </Button>
            <Button loading={gopayResendingOtp} onClick={resendGopayOtp}>
              重发 OTP
            </Button>
          </Space>
          <Form form={gopayInputForm} layout="vertical">
            <Form.Item name="otp" label="GoPay OTP" rules={[{ required: true, message: '请输入 GoPay OTP' }]}>
              <Input inputMode="numeric" autoComplete="one-time-code" maxLength={8} />
            </Form.Item>
            <Button type="primary" loading={gopaySubmitting} onClick={submitGopayInput}>
              提交 OTP
            </Button>
          </Form>
        </Space>
      )
    }
    if (phase === 'waiting_link_pin' || phase === 'waiting_payment_pin') {
      return (
        <Form form={gopayInputForm} layout="vertical">
          <Alert
            type="warning"
            showIcon
            message={phase === 'waiting_link_pin' ? '默认 PIN 未通过绑定验证' : '默认 PIN 未通过支付验证'}
            description="请输入新的 GoPay PIN；提交后会保存为默认 PIN，并尽量自动完成后续 PIN 步骤。"
            style={{ marginBottom: 12 }}
          />
          <Form.Item name="pin" label={phase === 'waiting_link_pin' ? 'GoPay 绑定 PIN' : 'GoPay 支付 PIN'} rules={[{ required: true, message: '请输入 GoPay PIN' }]}>
            <Input.Password inputMode="numeric" autoComplete="one-time-code" maxLength={8} />
          </Form.Item>
          <Button type="primary" loading={gopaySubmitting} onClick={submitGopayInput}>
            {phase === 'waiting_link_pin' ? '提交绑定 PIN' : '提交支付 PIN'}
          </Button>
        </Form>
      )
    }
    return null
  }

  const renderGopayDialog = () => {
    const phase = String(gopaySnapshot?.phase || '')
    const meta = gopayPhaseMeta(phase)
    const hasActiveSession = Boolean(gopaySnapshot?.session_id && GOPAY_ACTIVE_PHASES.has(phase))
    const gopayPhoneLabel = formatGopayPhoneLabel({
      phone_country_code: gopaySnapshot?.phone_country_code || gopayPhoneCountryCode,
      phone_number: gopaySnapshot?.phone_number || gopayPhoneNumber,
    })
    const countryOptions = (checkoutCountries.length ? checkoutCountries : [DEFAULT_CHECKOUT_COUNTRY]).map((code) => {
      const currency = checkoutCurrencyByCountry[code]
      return { label: checkoutCountryLabel(code, currency), value: code }
    })
    const phoneOptions = gopayPhoneCandidates
      .filter((item) => item.enabled !== false)
      .map((item) => ({
        label: formatGopayPhoneLabel(item),
        value: item.phone_number,
        candidate: item,
      }))
    return (
      <Modal
        title={`GoPay 支付 · ${acc.email || acc.id}${gopayPhoneLabel ? ` · ${gopayPhoneLabel}` : ''}`}
        open={gopayOpen}
        onCancel={() => setGopayOpen(false)}
        width={720}
        footer={[
          hasActiveSession ? (
            <Button key="cancel-flow" danger loading={gopayLoading} onClick={cancelGopay}>
              取消流程
            </Button>
          ) : null,
          gopaySnapshot?.session_id && !hasActiveSession ? (
            <Button key="restart-flow" onClick={resetGopayFlow}>
              重新开始
            </Button>
          ) : null,
          <Button key="close" onClick={() => setGopayOpen(false)}>
            关闭
          </Button>,
        ].filter(Boolean)}
        maskClosable={false}
      >
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
          style={{ marginBottom: 16 }}
        />
        {gopaySnapshot?.session_id ? (
          <Alert
            type={meta.status === 'error' ? 'error' : meta.status === 'finish' ? 'success' : 'info'}
            showIcon
            message={meta.title}
            description={gopaySnapshot.last_error || meta.description}
            style={{ marginBottom: 16 }}
          />
        ) : null}
        {gopaySnapshot?.session_id ? (
          <Button
            type="link"
            onClick={() => setGopayConfigCollapsed((value) => !value)}
            style={{ marginBottom: 8, paddingLeft: 0 }}
          >
            {gopayConfigCollapsed ? '展开参数配置' : '收起参数配置'}
          </Button>
        ) : null}
        <div style={{ marginBottom: 12 }}>
          <Space wrap>
            <Text type="secondary">OTP 自动重发延迟</Text>
            <InputNumber
              min={0}
              max={3600}
              precision={0}
              value={gopayOtpAutoResendDelay}
              onChange={(value) => setGopayOtpAutoResendDelay(normalizeGopayOtpAutoResendDelay(value))}
              addonAfter="秒"
            />
            <Button loading={gopayOtpDelaySaving} onClick={() => saveGopayOtpAutoResendDelay(gopayOtpAutoResendDelay)}>
              保存延迟
            </Button>
          </Space>
        </div>
        {!gopaySnapshot?.session_id || !gopayConfigCollapsed ? (
          <Form form={gopayForm} layout="vertical">
            <div
              style={{
                position: 'sticky',
                top: 0,
                zIndex: 2,
                marginBottom: 12,
                padding: '10px 12px',
                border: `1px solid ${token.colorBorder}`,
                borderRadius: token.borderRadiusLG,
                background: token.colorBgContainer,
                boxShadow: token.boxShadowTertiary,
              }}
            >
              <Space wrap>
                <Form.Item name="save_defaults" valuePropName="checked" style={{ marginBottom: 0 }}>
                  <Checkbox>保存为全局 GoPay 默认值</Checkbox>
                </Form.Item>
                <Button loading={gopayPhonePoolSaving} onClick={addCurrentGopayPhoneToPool}>加入手机号池</Button>
                <Button loading={gopayLoading} onClick={saveGopayDefaults}>保存默认参数</Button>
                <Button type="primary" loading={gopayLoading} onClick={startGopay} disabled={hasActiveSession}>
                  {hasActiveSession ? '当前会话进行中' : '开始 GoPay 支付'}
                </Button>
              </Space>
            </div>
            <Form.Item
              label="GoPay 手机"
              required
              extra={phoneOptions.length === 0 ? '暂无手机号候选，输入后点“加入手机号池”或开始支付都会自动加入。' : undefined}
            >
              <Space.Compact style={{ width: '100%' }}>
                <Form.Item name="phone_country_code" noStyle rules={[{ required: true, message: '请输入区号' }]}>
                  <Input style={{ width: 110 }} addonBefore="+" />
                </Form.Item>
                <Form.Item
                  name="phone_number"
                  noStyle
                  rules={[{ required: true, message: '请输入手机号' }]}
                  getValueFromEvent={(value) => applyGopayPhoneInput(value)}
                >
                  <AutoComplete
                    allowClear
                    placeholder="选择或输入手机号"
                    style={{ width: '100%' }}
                    options={phoneOptions}
                    filterOption={(inputValue, option) =>
                      String(option?.label ?? option?.value ?? '').toLowerCase().includes(inputValue.toLowerCase())
                    }
                    onChange={(value) => {
                      applyGopayPhoneInput(value)
                    }}
                    onSelect={(value, option: any) => {
                      const candidate = option?.candidate
                      gopayForm.setFieldsValue(candidate ? {
                        phone_country_code: candidate.phone_country_code,
                        phone_number: candidate.phone_number,
                      } : {
                        ...splitGopayPhoneInput(
                          gopayForm.getFieldValue('phone_country_code'),
                          value,
                          gopayRecognizedCountryCodes,
                        ),
                      })
                    }}
                  />
                </Form.Item>
              </Space.Compact>
            </Form.Item>
            {gopayPhoneCandidates.length > 0 ? (
              <Collapse
                size="small"
                defaultActiveKey={[]}
                style={{ marginBottom: 16 }}
                items={[
                  {
                    key: 'gopay-phone-pool',
                    label: `手机号池（${gopayPhoneCandidates.length}）`,
                    children: (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                        {gopayPhoneCandidates.map((phone, index) => (
                          <div
                            key={phone.id}
                            style={{
                              display: 'flex',
                              justifyContent: 'space-between',
                              gap: 8,
                              alignItems: 'center',
                              padding: '8px 10px',
                              border: `1px solid ${token.colorBorder}`,
                              borderRadius: token.borderRadius,
                              background: token.colorBgContainer,
                            }}
                          >
                            <Space wrap>
                              <Tag>{index + 1}</Tag>
                              <Text>{formatGopayPhoneLabel(phone)}</Text>
                              {formatGopayPhoneExpiryLabel(phone) ? <Tag color="processing">有效期 {formatGopayPhoneExpiryLabel(phone)}</Tag> : <Tag>有效期 -</Tag>}
                            </Space>
                            <Space size={4} wrap>
                              <Button size="small" disabled={index === 0} onClick={() => moveCurrentGopayPhone(phone.id, 'up')}>上移</Button>
                              <Button size="small" disabled={index === gopayPhoneCandidates.length - 1} onClick={() => moveCurrentGopayPhone(phone.id, 'down')}>下移</Button>
                              <Button size="small" disabled={index === 0} onClick={() => moveCurrentGopayPhone(phone.id, 'top')}>置顶</Button>
                              <Button size="small" disabled={index === gopayPhoneCandidates.length - 1} onClick={() => moveCurrentGopayPhone(phone.id, 'bottom')}>置底</Button>
                              <Popconfirm title="确认删除该手机号？" onConfirm={() => deleteCurrentGopayPhone(phone.id)}>
                                <Button size="small" danger>删除</Button>
                              </Popconfirm>
                            </Space>
                          </div>
                        ))}
                      </div>
                    ),
                  },
                ]}
              />
            ) : null}
            <Form.Item label="国家" required>
              <Space.Compact style={{ width: '100%' }}>
                <Form.Item name="country" noStyle rules={[{ required: true, message: '请选择国家' }]}>
                  <Select
                    showSearch
                    loading={checkoutCountriesLoading}
                    optionFilterProp="label"
                    options={countryOptions}
                    onDropdownVisibleChange={(open) => {
                      if (open) loadCheckoutCountries(false)
                    }}
                    onChange={async (value) => {
                      await loadCheckoutConfig(value, gopayForm)
                    }}
                  />
                </Form.Item>
                <Button loading={checkoutCountriesLoading} onClick={() => loadCheckoutCountries(true)}>
                  刷新
                </Button>
              </Space.Compact>
            </Form.Item>
            <Form.Item name="currency" label="货币" rules={[{ required: true }]}>
              <Input readOnly suffix={checkoutConfigLoading ? <SyncOutlined spin /> : null} />
            </Form.Item>
            <Form.Item
              name="proxy"
              label="支付代理节点"
              extra="遇到 checkout 页面 Something went wrong 时，换一个代理节点并重新开始 GoPay。留空则使用全局/上次订阅链接代理。"
            >
              <Input placeholder="http://user:pass@host:port 或 socks5://host:port" />
            </Form.Item>
            <Form.Item
              name="browser_profile_mode"
              label="浏览器环境"
              extra="推荐使用全新支付环境；遇到拒绝时换代理并重新生成一个环境。"
            >
              <Select
                options={[
                  { label: '全新支付浏览器环境', value: 'fresh_payment' },
                  { label: '复用账号注册环境', value: 'account' },
                ]}
              />
            </Form.Item>
            <Form.Item
              name="pin"
              label="默认 GoPay PIN"
              extra={acc.chatgptGopayDefaults?.pin ? '已保存默认 PIN；留空则继续使用已保存值。填写新 PIN 会覆盖默认值。' : '保存后 OTP 之后会自动完成绑定 PIN 和支付 PIN。'}
            >
              <Input.Password inputMode="numeric" autoComplete="one-time-code" maxLength={8} placeholder={acc.chatgptGopayDefaults?.pin ? '留空使用已保存 PIN' : '请输入并保存默认 PIN'} />
            </Form.Item>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, marginBottom: 12 }}>
              <Text strong>账单信息</Text>
              <Button loading={gopayBillingGenerating} onClick={generateGopayBillingAddress}>
                生成地址
              </Button>
            </div>
            <Form.Item name="billing_name" label="账单姓名">
              <Input />
            </Form.Item>
            <Form.Item name="billing_email" label="账单邮箱">
              <Input />
            </Form.Item>
            <Form.Item name="billing_country" label="账单国家">
              <Input />
            </Form.Item>
            <Form.Item name="billing_line1" label="账单地址">
              <Input />
            </Form.Item>
            <Form.Item name="billing_city" label="账单城市">
              <Input />
            </Form.Item>
            <Form.Item name="billing_state" label="账单州/省">
              <Input />
            </Form.Item>
            <Form.Item name="billing_postal_code" label="账单邮编">
              <Input />
            </Form.Item>
            <Form.Item
              name="access_token"
              label="本次自定义 Access Token"
              extra="填写后，本次 GoPay 会话会使用这个 token 创建并确认 Plus checkout；不会覆盖账号中保存的 access token。"
            >
              <Input.Password placeholder="留空则使用当前账号 access token" />
            </Form.Item>
            <Form.Item name="checkout_url" label="订阅链接 / checkout session">
              <Input
                placeholder="推荐留空：每次重新创建 checkout；被拒绝后不要复用旧 cs_live"
                onChange={(event) => setGopayCheckoutUrl(event.target.value)}
              />
            </Form.Item>
            <Alert
              type={gopayCheckoutUrl ? 'success' : 'info'}
              showIcon
              message={gopayCheckoutUrl ? '将复用上方 checkout；如果该页面已被拒绝，请清空后重试' : '开始时会在同一 GoPay 会话内创建新的 Plus checkout'}
              style={{ marginBottom: 12 }}
            />
          </Form>
        ) : null}
        {renderGopayCurrentInput()}
        {gopaySnapshot?.task_id ? (
          <div style={{ marginTop: 16 }}>
            <TaskLogPanel taskId={String(gopaySnapshot.task_id)} onDone={onRefresh} />
          </div>
        ) : gopaySnapshot?.logs?.length ? (
          <pre
            style={{
              margin: '16px 0 0',
              maxHeight: 220,
              overflow: 'auto',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              fontFamily: 'monospace',
              fontSize: 12,
            }}
          >
            {gopaySnapshot.logs.join('\n')}
          </pre>
        ) : null}
      </Modal>
    )
  }

  const renderBrowserAuthDialog = () => (
    <Modal
      title={`ChatGPT 浏览器登录 · ${acc.email || acc.id}`}
      open={browserAuthOpen}
      onCancel={closeBrowserAuth}
      width={980}
      style={{ top: 8, paddingBottom: 8 }}
      styles={{
        body: {
          maxHeight: 'calc(100vh - 132px)',
          overflowY: 'auto',
        },
      }}
      footer={[
        <Button key="close-browser" onClick={closeBrowserAuth}>
          关闭
        </Button>,
        <Button key="capture-browser" type="primary" loading={browserAuthLoading} disabled={!browserAuthSnapshot?.capture_id} onClick={captureBrowserAuth}>
          保存登录态
        </Button>,
      ]}
      maskClosable={false}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="先在下方浏览器画面里完成 ChatGPT 登录，进入 chatgpt.com 首页后点击“保存登录态”。"
        description="页面只保存 cookies/session/access token 的长度和状态，不会在日志里显示明文。"
      />
      <Space.Compact style={{ width: '100%', marginBottom: 12 }}>
        <Input value={browserAuthUrl} onChange={(event) => setBrowserAuthUrl(event.target.value)} onPressEnter={navigateBrowserAuth} />
        <Button loading={browserAuthLoading} onClick={navigateBrowserAuth}>
          打开
        </Button>
        <Button loading={browserAuthLoading} onClick={refreshBrowserAuth}>
          刷新画面
        </Button>
        <Button loading={browserAuthLoading} onClick={injectBrowserBilling}>
          注入地址
        </Button>
      </Space.Compact>
      <Space style={{ width: '100%', marginBottom: 12 }} align="start">
        <Input
          value={browserAuthProxy}
          onChange={(event) => setBrowserAuthProxy(event.target.value)}
          placeholder="登录代理节点，留空直连"
          style={{ width: 420 }}
        />
        <Checkbox checked={browserAuthFreshProfile} onChange={(event) => setBrowserAuthFreshProfile(event.target.checked)}>
          全新浏览器环境
        </Checkbox>
        <Button loading={browserAuthLoading} onClick={startBrowserAuth}>
          重新打开
        </Button>
      </Space>
      <Space.Compact style={{ width: '100%', marginBottom: 12 }}>
        <Input.Password
          value={browserAuthText}
          onChange={(event) => setBrowserAuthText(event.target.value)}
          onPressEnter={sendBrowserAuthText}
          placeholder="点击截图中的输入框后，在这里输入要发送的文本"
        />
        <Button loading={browserAuthLoading} onClick={sendBrowserAuthText}>
          输入
        </Button>
        <Button loading={browserAuthLoading} onClick={() => sendBrowserAuthKey('Enter')}>
          Enter
        </Button>
        <Button loading={browserAuthLoading} onClick={() => sendBrowserAuthKey('Tab')}>
          Tab
        </Button>
        <Button loading={browserAuthLoading} onClick={() => sendBrowserAuthKey('Backspace')}>
          退格
        </Button>
      </Space.Compact>
      {browserAuthSnapshot ? (
        <div>
          {browserAuthSnapshot.error ? (
            <Alert type="error" showIcon style={{ marginBottom: 12 }} message={browserAuthSnapshot.error} />
          ) : null}
          <Text type="secondary" style={{ display: 'block', marginBottom: 8, wordBreak: 'break-all' }}>
            {browserAuthSnapshot.title || '浏览器'} · {browserAuthSnapshot.url || ''}
          </Text>
          {browserAuthSnapshot.screenshot ? (
            <div
              style={{
                border: '1px solid rgba(128,128,128,0.35)',
                borderRadius: 6,
                overflow: 'hidden',
                background: '#111',
              }}
            >
              <img
                src={`data:image/jpeg;base64,${browserAuthSnapshot.screenshot}`}
                alt="ChatGPT browser"
                onClick={clickBrowserAuth}
                style={{
                  display: 'block',
                  width: '100%',
                  height: 'auto',
                  cursor: browserAuthLoading ? 'progress' : 'crosshair',
                  userSelect: 'none',
                }}
                draggable={false}
              />
            </div>
          ) : (
            <Alert type="warning" showIcon message="还没有浏览器截图，请刷新画面。" />
          )}
        </div>
      ) : (
        <Alert type="warning" showIcon message="浏览器登录会话尚未启动。" />
      )}
    </Modal>
  )

  const paymentLinkAction = actions.find((a) => String(a?.id || '') === 'payment_link')
  const menuActions = actions.filter((a) => String(a?.id || '') !== 'payment_link')
  const menuItems: MenuProps['items'] = [
    ...(onOpenDetail
      ? [{
          key: '__detail__',
          label: '详情',
        }]
      : []),
    ...menuActions.map((a) => ({
      key: a.id,
      label: a.label,
    })),
  ]

  const hasActions = actions.length > 0
  const modalTitle = activeAction?.label || `账号动作 · ${acc?.email || acc?.id || ''}`

  return (
    <>
      {showShell ? (
        <Modal
          title={modalTitle}
          open={open}
          onCancel={onClose}
          footer={[
            paymentLinkAction ? (
              <Button
                key="payment-link"
                loading={runningActionId === 'payment_link'}
                onClick={() => handleAction('payment_link')}
              >
                订阅链接
              </Button>
            ) : null,
            acc?.platform === 'chatgpt' ? (
              <Button key="gopay" loading={gopayLoading} onClick={openGopayDialog}>
                GoPay
              </Button>
            ) : null,
            acc?.platform === 'chatgpt' ? (
              <Button key="browser-auth" loading={browserAuthLoading} onClick={startBrowserAuth}>
                浏览器登录
              </Button>
            ) : null,
            menuActions.length > 0 ? (
              <Dropdown
                key="more-actions"
                menu={{
                  items: menuItems,
                  onClick: ({ key }) => {
                    if (String(key) === '__detail__') {
                      onOpenDetail?.(acc)
                      onClose()
                      return
                    }
                    void handleAction(String(key))
                  },
                }}
              >
                <Button icon={runningActionId ? <SyncOutlined spin /> : <MoreOutlined />} loading={Boolean(runningActionId)}>
                  更多动作
                </Button>
              </Dropdown>
            ) : null,
            <Button key="close" type="primary" onClick={onClose}>
              关闭
            </Button>,
          ].filter(Boolean)}
          width={560}
          maskClosable={false}
        >
          {!hasActions ? (
            actionsLoading ? (
              <Alert type="info" showIcon message="正在加载账号动作..." />
            ) : (
              <Alert type="info" showIcon message="当前账号没有可用动作。" />
            )
          ) : (
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <Text type="secondary">
                当前账号: {acc?.email || acc?.id || '-'}
              </Text>
              {paymentLinkAction ? (
                <Button block loading={runningActionId === 'payment_link'} onClick={() => handleAction('payment_link')}>
                  订阅链接
                </Button>
              ) : null}
              {acc?.platform === 'chatgpt' ? (
                <Button block loading={gopayLoading} onClick={openGopayDialog}>
                  GoPay
                </Button>
              ) : null}
              {acc?.platform === 'chatgpt' ? (
                <Button block loading={browserAuthLoading} onClick={startBrowserAuth}>
                  浏览器登录
                </Button>
              ) : null}
              {menuActions.length > 0 ? (
                <Space wrap>
                  {menuActions.map((action) => (
                    <Button
                      key={String(action.id)}
                      loading={runningActionId === String(action.id)}
                      onClick={() => handleAction(String(action.id))}
                    >
                      {action.label || action.id}
                    </Button>
                  ))}
                </Space>
              ) : null}
            </Space>
          )}
        </Modal>
      ) : null}
      <Modal
        title={activeAction?.label || '执行操作'}
        open={actionOpen}
        onCancel={() => setActionOpen(false)}
        onOk={submitActionDialog}
        confirmLoading={Boolean(runningActionId)}
        maskClosable={false}
      >
        <Form form={actionForm} layout="vertical">
          {renderActionFields()}
        </Form>
      </Modal>
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
            <LocalProbeSummary
              probe={resultProbe}
              authStateMeta={authStateMeta}
              planMeta={planMeta}
              codexStateMeta={codexStateMeta}
              formatSyncTime={formatSyncTime}
            />
          </div>
        ) : null}
        {resultCliproxySync ? (
          <div style={{ marginBottom: 12 }}>
            <CliproxySyncSummary
              sync={resultCliproxySync}
              formatSyncTime={formatSyncTime}
            />
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
      {renderGopayDialog()}
      {renderBrowserAuthDialog()}
    </>
  )
}
