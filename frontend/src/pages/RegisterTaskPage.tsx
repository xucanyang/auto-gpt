import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Card,
  Form,
  Input,
  InputNumber,
  Select,
  Button,
  Checkbox,
  Tag,
  Space,
  Typography,
  Descriptions,
  message,
} from 'antd'
import {
  PlayCircleOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  LoadingOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import { ChatGPTRegistrationModeSwitch } from '@/components/ChatGPTRegistrationModeSwitch'
import { PhoneBindingResultsTable } from '@/components/phone-binding/PhoneBindingResultsTable'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import { TaskVerificationPanel } from '@/components/TaskVerificationPanel'
import { usePersistentChatGPTRegistrationMode } from '@/hooks/usePersistentChatGPTRegistrationMode'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import { buildChatGPTRegistrationRequestAdapter } from '@/lib/chatgptRegistrationRequestAdapter'
import { CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN } from '@/lib/chatgptRegistrationMode'
import { getExecutorOptions, normalizeExecutorForPlatform } from '@/lib/platformExecutorOptions'
import { apiFetch } from '@/lib/utils'
import { normalizeDomainList, parseStoredDomainList } from '@/lib/domainList'

const { Text } = Typography

const REGISTER_TASK_STORAGE_KEY = 'auto-chatgpt.register-task-page.current-task'

type RegisterTaskStatus = 'pending' | 'running' | 'done' | 'failed' | 'stopped'

function normalizeRegisterTaskStatus(value: unknown): RegisterTaskStatus {
  const normalized = String(value || '').trim().toLowerCase()
  if (normalized === 'success') return 'done'
  if (normalized === 'skipped') return 'stopped'
  if (normalized === 'pending' || normalized === 'running' || normalized === 'done' || normalized === 'failed' || normalized === 'stopped') {
    return normalized
  }
  return 'pending'
}

function mapHistoryTaskStatus(status: unknown, snapshotStatus?: unknown): RegisterTaskStatus {
  const normalized = String(status || '').trim().toLowerCase()
  if (normalized === 'success') return 'done'
  if (normalized === 'failed') return 'failed'
  if (normalized === 'skipped' || normalized === 'stopped') return 'stopped'
  if (normalized === 'running') return 'running'
  return normalizeRegisterTaskStatus(snapshotStatus)
}

function normalizeTaskSnapshot(task: any, fallbackTaskId?: string) {
  if (!task) return null
  const normalizedId = task.id || task.task_id || fallbackTaskId || ''
  return {
    ...task,
    id: normalizedId,
    task_id: normalizedId,
    status: normalizeRegisterTaskStatus(task.status || task.status_snapshot || 'pending'),
    progress: task.progress || '0/0',
    skipped: task.skipped ?? 0,
    success: task.success ?? 0,
    errors: Array.isArray(task.errors) ? task.errors : [],
    cashier_urls: Array.isArray(task.cashier_urls) ? task.cashier_urls : [],
    pending_verification: task.pending_verification || null,
    error: task.error || '',
  }
}

export default function RegisterTaskPage() {
  const [form] = Form.useForm()
  const [task, setTask] = useState<any>(null)
  const [polling, setPolling] = useState(false)
  const [tempmailDomains, setTempmailDomains] = useState<any[]>([])
  const [tempmailDomainsLoading, setTempmailDomainsLoading] = useState(false)
  const pollTimerRef = useRef<number | null>(null)
  const taskRef = useRef<any>(null)
  const { mode: chatgptRegistrationMode, setMode: setChatgptRegistrationMode } =
    usePersistentChatGPTRegistrationMode()

  useEffect(() => {
    apiFetch('/config').then((cfg) => {
      const currentPlatform = form.getFieldValue('platform') || 'chatgpt'
      form.setFieldsValue({
        executor_type: normalizeExecutorForPlatform(currentPlatform, cfg.default_executor),
        captcha_solver: cfg.default_captcha_solver || 'yescaptcha',
        mail_provider: cfg.mail_provider || 'luckmail',
        applemail_base_url: cfg.applemail_base_url || 'https://www.appleemail.top',
        applemail_pool_dir: cfg.applemail_pool_dir || 'mail',
        applemail_pool_file: cfg.applemail_pool_file || '',
        applemail_mailboxes: cfg.applemail_mailboxes || 'INBOX,Junk',
        yescaptcha_key: cfg.yescaptcha_key || '',
        moemail_api_url: cfg.moemail_api_url || '',
        moemail_api_key: cfg.moemail_api_key || '',
        tempmail_api_url: cfg.tempmail_api_url || 'http://127.0.0.1:18081',
        tempmail_api_key: cfg.tempmail_api_key || '',
        tempmail_api_key_header: cfg.tempmail_api_key_header || 'Authorization',
        tempmail_mode: cfg.tempmail_mode || 'fixed_domain',
        tempmail_primary_domain: cfg.tempmail_primary_domain || '',
        tempmail_fixed_domains: parseStoredDomainList(cfg.tempmail_fixed_domains || cfg.tempmail_primary_domain),
        tempmail_wait_timeout_seconds: cfg.tempmail_wait_timeout_seconds || 180,
        tempmail_ttl_minutes: cfg.tempmail_ttl_minutes || 30,
        tempmail_reuse_window_minutes: cfg.tempmail_reuse_window_minutes || 20,
        tempmail_permanent: parseBooleanConfigValue(cfg.tempmail_permanent),
        tempmail_platform: cfg.tempmail_platform || 'chatgpt',
        skymail_api_base: cfg.skymail_api_base || 'https://api.skymail.ink',
        skymail_token: cfg.skymail_token || '',
        skymail_domain: cfg.skymail_domain || '',
        cloudmail_api_base: cfg.cloudmail_api_base || '',
        cloudmail_admin_email: cfg.cloudmail_admin_email || '',
        cloudmail_admin_password: cfg.cloudmail_admin_password || '',
        cloudmail_domain: cfg.cloudmail_domain || '',
        cloudmail_subdomain: cfg.cloudmail_subdomain || '',
        cloudmail_timeout: cfg.cloudmail_timeout || 30,
        laoudo_auth: cfg.laoudo_auth || '',
        laoudo_email: cfg.laoudo_email || '',
        laoudo_account_id: cfg.laoudo_account_id || '',
        proxy_max_candidates: Number(cfg.proxy_pool_max_candidates || 5),
        proxy_min_score: Number(cfg.proxy_scan_min_score || 50),
        gptmail_base_url: cfg.gptmail_base_url || 'https://mail.chatgpt.org.uk',
        gptmail_api_key: cfg.gptmail_api_key || '',
        gptmail_domain: cfg.gptmail_domain || '',
        opentrashmail_api_url: cfg.opentrashmail_api_url || '',
        opentrashmail_domain: cfg.opentrashmail_domain || '',
        opentrashmail_password: cfg.opentrashmail_password || '',
        maliapi_base_url: cfg.maliapi_base_url || 'https://maliapi.215.im/v1',
        maliapi_api_key: cfg.maliapi_api_key || '',
        maliapi_domain: cfg.maliapi_domain || '',
        maliapi_auto_domain_strategy: cfg.maliapi_auto_domain_strategy || 'balanced',
        duckmail_api_url: cfg.duckmail_api_url || '',
        duckmail_provider_url: cfg.duckmail_provider_url || '',
        duckmail_bearer: cfg.duckmail_bearer || '',
        freemail_api_url: cfg.freemail_api_url || '',
        freemail_admin_token: cfg.freemail_admin_token || '',
        freemail_username: cfg.freemail_username || '',
        freemail_password: cfg.freemail_password || '',
        freemail_domain: cfg.freemail_domain || '',
        cfworker_api_url: cfg.cfworker_api_url || '',
        cfworker_admin_token: cfg.cfworker_admin_token || '',
        cfworker_custom_auth: cfg.cfworker_custom_auth || '',
        cfworker_domain_override: '',
        cfworker_subdomain: cfg.cfworker_subdomain || '',
        cfworker_random_subdomain: parseBooleanConfigValue(cfg.cfworker_random_subdomain),
        cfworker_fingerprint: cfg.cfworker_fingerprint || '',
        smstome_cookie: cfg.smstome_cookie || '',
        smstome_country_slugs: cfg.smstome_country_slugs || '',
        smstome_phone_attempts: cfg.smstome_phone_attempts || '',
        smstome_otp_timeout_seconds: cfg.smstome_otp_timeout_seconds || '',
        smstome_poll_interval_seconds: cfg.smstome_poll_interval_seconds || '',
        smstome_sync_max_pages_per_country: cfg.smstome_sync_max_pages_per_country || '',
        chatgpt_registration_entry: 'email_signup',
        chatgpt_phone_signup_use_pool: parseBooleanConfigValue(cfg.chatgpt_phone_signup_use_pool),
        chatgpt_phone_signup_phone_lines: '',
        chatgpt_phone_signup_timeout_seconds: cfg.chatgpt_phone_signup_timeout_seconds || 180,
        chatgpt_phone_signup_poll_interval_seconds: cfg.chatgpt_phone_signup_poll_interval_seconds || 5,
        chatgpt_phone_signup_max_resend_attempts: cfg.chatgpt_phone_signup_max_resend_attempts || 1,
        chatgpt_phone_signup_resend_interval_seconds: cfg.chatgpt_phone_signup_resend_interval_seconds || 60,
        login_password: cfg.chatgpt_phone_signup_password || '',
        luckmail_base_url: cfg.luckmail_base_url || 'https://mails.luckyous.com/',
        luckmail_api_key: cfg.luckmail_api_key || '',
        luckmail_email_type: cfg.luckmail_email_type || '',
        luckmail_domain: cfg.luckmail_domain || '',
        chatgpt_enable_team_invite: parseBooleanConfigValue(cfg.chatgpt_enable_team_invite),
        chatgpt_team_invite_deferred_activation: parseBooleanConfigValue(cfg.chatgpt_team_invite_deferred_activation),
        chatgpt_capture_business_workspace: cfg.chatgpt_capture_business_workspace === '' ? true : parseBooleanConfigValue(cfg.chatgpt_capture_business_workspace),
        chatgpt_capture_free_workspace: cfg.chatgpt_capture_free_workspace === '' ? true : parseBooleanConfigValue(cfg.chatgpt_capture_free_workspace),
        chatgpt_access_token_only_checkout_amount_check_enabled:
          cfg.chatgpt_access_token_only_checkout_amount_check_enabled === ''
            ? true
            : parseBooleanConfigValue(cfg.chatgpt_access_token_only_checkout_amount_check_enabled),
        chatgpt_access_token_only_checkout_country: String(cfg.chatgpt_access_token_only_checkout_country || 'US').trim().toUpperCase() || 'US',
        chatgpt_access_token_only_checkout_currency: String(cfg.chatgpt_access_token_only_checkout_currency || 'USD').trim().toUpperCase() || 'USD',
        chatgpt_access_token_only_gopay_provider_link_enabled: parseBooleanConfigValue(
          cfg.chatgpt_access_token_only_gopay_provider_link_enabled,
        ),
        chatgpt_save_registration_access_token_account:
          cfg.chatgpt_save_registration_access_token_account === ''
            ? true
            : cfg.chatgpt_save_registration_access_token_account === undefined
              ? true
              : parseBooleanConfigValue(cfg.chatgpt_save_registration_access_token_account),
      })
    })
  }, [form])

  useEffect(() => {
    taskRef.current = task
  }, [task])

  const stopPolling = () => {
    if (pollTimerRef.current != null) {
      window.clearInterval(pollTimerRef.current)
      pollTimerRef.current = null
    }
    setPolling(false)
  }

  const submit = async () => {
    const values = await form.validateFields()
    const phoneSignupEnabled = values.platform === 'chatgpt' && values.chatgpt_registration_entry === 'phone_signup'
    if (values.mail_provider === 'tempmail_local' && (values.tempmail_mode || 'fixed_domain') === 'fixed_domain'
      && normalizeDomainList(values.tempmail_fixed_domains).length === 0) {
      message.error('固定域名模式下请至少选择一个 TempMail 可用域名')
      return
    }
    if (phoneSignupEnabled && !values.chatgpt_phone_signup_use_pool && !String(values.chatgpt_phone_signup_phone_lines || '').trim()) {
      message.error('手机号注册请粘贴 手机号----收码API，或勾选使用手机号池')
      return
    }
    if (phoneSignupEnabled && !String(values.login_password || values.password || '').trim()) {
      message.error('手机号注册/已注册手机号登录必须填写同一个密码')
      return
    }


    const registerExtra = {
      mail_provider: values.mail_provider,
      applemail_base_url: values.applemail_base_url,
      applemail_pool_dir: values.applemail_pool_dir,
      applemail_pool_file: values.applemail_pool_file,
      applemail_mailboxes: values.applemail_mailboxes,
      laoudo_auth: values.laoudo_auth,
      laoudo_email: values.laoudo_email,
      laoudo_account_id: values.laoudo_account_id,
      gptmail_base_url: values.gptmail_base_url,
      gptmail_api_key: values.gptmail_api_key,
      gptmail_domain: values.gptmail_domain,
      opentrashmail_api_url: values.opentrashmail_api_url,
      opentrashmail_domain: values.opentrashmail_domain,
      opentrashmail_password: values.opentrashmail_password,
      maliapi_base_url: values.maliapi_base_url,
      maliapi_api_key: values.maliapi_api_key,
      maliapi_domain: values.maliapi_domain,
      maliapi_auto_domain_strategy: values.maliapi_auto_domain_strategy,
      moemail_api_url: values.moemail_api_url,
      moemail_api_key: values.moemail_api_key,
      tempmail_api_url: values.tempmail_api_url,
      tempmail_api_key: values.tempmail_api_key,
      tempmail_api_key_header: values.tempmail_api_key_header,
      tempmail_mode: values.tempmail_mode,
      tempmail_primary_domain: normalizeDomainList(values.tempmail_fixed_domains)[0] || values.tempmail_primary_domain,
      tempmail_fixed_domains: normalizeDomainList(values.tempmail_fixed_domains),
      tempmail_wait_timeout_seconds: values.tempmail_wait_timeout_seconds,
      tempmail_ttl_minutes: values.tempmail_ttl_minutes,
      tempmail_reuse_window_minutes: values.tempmail_reuse_window_minutes,
      tempmail_permanent: values.tempmail_permanent,
      tempmail_platform: values.tempmail_platform,
      skymail_api_base: values.skymail_api_base,
      skymail_token: values.skymail_token,
      skymail_domain: values.skymail_domain,
      cloudmail_api_base: values.cloudmail_api_base,
      cloudmail_admin_email: values.cloudmail_admin_email,
      cloudmail_admin_password: values.cloudmail_admin_password,
      cloudmail_domain: values.cloudmail_domain,
      cloudmail_subdomain: values.cloudmail_subdomain,
      cloudmail_timeout: values.cloudmail_timeout,
      duckmail_api_url: values.duckmail_api_url,
      duckmail_provider_url: values.duckmail_provider_url,
      duckmail_bearer: values.duckmail_bearer,
      freemail_api_url: values.freemail_api_url,
      freemail_admin_token: values.freemail_admin_token,
      freemail_username: values.freemail_username,
      freemail_password: values.freemail_password,
      freemail_domain: values.freemail_domain,
      cfworker_api_url: values.cfworker_api_url,
      cfworker_admin_token: values.cfworker_admin_token,
      cfworker_custom_auth: values.cfworker_custom_auth,
      cfworker_domain_override: values.cfworker_domain_override,
      cfworker_subdomain: values.cfworker_subdomain,
      cfworker_random_subdomain: values.cfworker_random_subdomain,
      cfworker_fingerprint: values.cfworker_fingerprint,
      smstome_cookie: values.smstome_cookie,
      smstome_country_slugs: values.smstome_country_slugs,
      smstome_phone_attempts: values.smstome_phone_attempts,
      smstome_otp_timeout_seconds: values.smstome_otp_timeout_seconds,
      smstome_poll_interval_seconds: values.smstome_poll_interval_seconds,
      smstome_sync_max_pages_per_country: values.smstome_sync_max_pages_per_country,
      chatgpt_registration_entry: phoneSignupEnabled ? 'phone_signup' : 'email_signup',
      chatgpt_phone_signup_password: phoneSignupEnabled ? String(values.login_password || values.password || '').trim() : undefined,
      chatgpt_phone_signup_use_pool: phoneSignupEnabled ? Boolean(values.chatgpt_phone_signup_use_pool) : undefined,
      chatgpt_phone_signup_phone_lines: phoneSignupEnabled ? values.chatgpt_phone_signup_phone_lines : undefined,
      chatgpt_phone_signup_timeout_seconds: phoneSignupEnabled ? values.chatgpt_phone_signup_timeout_seconds : undefined,
      chatgpt_phone_signup_poll_interval_seconds: phoneSignupEnabled ? values.chatgpt_phone_signup_poll_interval_seconds : undefined,
      chatgpt_phone_signup_max_resend_attempts: phoneSignupEnabled ? values.chatgpt_phone_signup_max_resend_attempts : undefined,
      chatgpt_phone_signup_resend_interval_seconds: phoneSignupEnabled ? values.chatgpt_phone_signup_resend_interval_seconds : undefined,
      luckmail_base_url: values.luckmail_base_url,
      luckmail_api_key: values.luckmail_api_key,
      luckmail_email_type: values.luckmail_email_type,
      luckmail_domain: values.luckmail_domain,
      yescaptcha_key: values.yescaptcha_key,
      solver_url: values.solver_url,
      chatgpt_enable_team_invite: platform === 'chatgpt' ? Boolean(values.chatgpt_enable_team_invite) : undefined,
      chatgpt_capture_free_workspace:
        platform === 'chatgpt'
          ? Boolean(values.chatgpt_capture_free_workspace)
          : undefined,
      chatgpt_capture_business_workspace:
        platform === 'chatgpt' && values.chatgpt_enable_team_invite
          ? values.chatgpt_capture_business_workspace
          : undefined,
      chatgpt_team_invite_deferred_activation:
        platform === 'chatgpt' && values.chatgpt_enable_team_invite
          ? Boolean(values.chatgpt_team_invite_deferred_activation)
          : undefined,
      chatgpt_save_registration_access_token_account:
        platform === 'chatgpt'
          ? (values.chatgpt_save_registration_access_token_account === undefined
            ? true
            : Boolean(values.chatgpt_save_registration_access_token_account))
          : undefined,
      chatgpt_access_token_only_checkout_amount_check_enabled:
        platform === 'chatgpt'
          ? Boolean(values.chatgpt_access_token_only_checkout_amount_check_enabled)
          : undefined,
      chatgpt_access_token_only_checkout_country:
        platform === 'chatgpt'
          ? String(values.chatgpt_access_token_only_checkout_country || 'US').trim().toUpperCase() || 'US'
          : undefined,
      chatgpt_access_token_only_checkout_currency:
        platform === 'chatgpt'
          ? String(values.chatgpt_access_token_only_checkout_currency || 'USD').trim().toUpperCase() || 'USD'
          : undefined,
      chatgpt_access_token_only_gopay_provider_link_enabled:
        platform === 'chatgpt'
          ? Boolean(values.chatgpt_access_token_only_gopay_provider_link_enabled)
          : undefined,
    }
    const chatgptRegistrationRequestAdapter =
      buildChatGPTRegistrationRequestAdapter(
        values.platform,
        chatgptRegistrationMode,
      )
    const adaptedRegisterExtra = chatgptRegistrationRequestAdapter
      ? (phoneSignupEnabled ? registerExtra : chatgptRegistrationRequestAdapter.extendExtra(registerExtra))
      : registerExtra

    try {
      if (phoneSignupEnabled) {
        await apiFetch('/config', {
          method: 'PUT',
          body: JSON.stringify({
            data: {
              chatgpt_phone_signup_password: String(values.login_password || values.password || '').trim(),
            },
          }),
        })
      }
      const res = await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify({
          platform: values.platform,
          email: values.email || null,
          password: phoneSignupEnabled ? String(values.login_password || values.password || '').trim() : values.password || null,
          count: values.count,
          concurrency: phoneSignupEnabled ? 1 : values.concurrency,
          register_delay_seconds: values.register_delay_seconds || 0,
          proxy: values.proxy_mode === 'specified' ? values.proxy || null : null,
          proxy_mode: values.proxy_mode || (values.proxy ? 'specified' : 'pool'),
          proxy_country_code: String(values.proxy_country_code || '').trim().toUpperCase(),
          proxy_failover: Boolean(values.proxy_failover),
          proxy_max_candidates: Number(values.proxy_max_candidates || 5),
          proxy_min_score: Number(values.proxy_min_score || 0),
          executor_type: values.executor_type,
          captcha_solver: values.captcha_solver,
          extra: adaptedRegisterExtra,
        }),
      }) as { task_id?: string }

      const createdTaskId = String(res?.task_id || '').trim()
      if (!createdTaskId) {
        throw new Error('创建任务成功，但未返回 task_id')
      }

      setTask(normalizeTaskSnapshot({ id: createdTaskId, status: 'running', progress: `0/${values.count || 1}` }, createdTaskId))
      setPolling(true)

      try {
        const snapshot = await apiFetch(`/tasks/${createdTaskId}`)
        setTask(normalizeTaskSnapshot(snapshot, createdTaskId))
      } catch {
        // 任务已创建成功；如果首轮快照暂时失败，后续轮询会继续拉取
      }

      pollTask(createdTaskId)
    } catch (error_: unknown) {
      stopPolling()
      const detail = error_ instanceof Error ? error_.message : '创建注册任务失败'
      message.error(detail)
    }
  }

  const pollTask = async (id: string) => {
    stopPolling()
    setPolling(true)

    const loadHistoryFallback = async (reason: string) => {
      try {
        const history = await apiFetch(`/tasks/logs/by-task/${encodeURIComponent(id)}`) as {
          detail?: {
            status_snapshot?: string
            progress?: string
            success?: number
            skipped?: number
            errors?: string[]
            cashier_urls?: string[]
          }
          status?: string
          error?: string
          email?: string
        }
        const detail = history?.detail && typeof history.detail === 'object' ? history.detail : {}
        const restoredTask = normalizeTaskSnapshot({
          ...(taskRef.current || {}),
          id,
          status: mapHistoryTaskStatus(history.status, detail.status_snapshot),
          progress: detail.progress || taskRef.current?.progress || '0/0',
          skipped: detail.skipped ?? taskRef.current?.skipped ?? 0,
          success: detail.success ?? taskRef.current?.success ?? 0,
          errors: Array.isArray(detail.errors) ? detail.errors : (taskRef.current?.errors || []),
          cashier_urls: Array.isArray(detail.cashier_urls) ? detail.cashier_urls : (taskRef.current?.cashier_urls || []),
          error: history.error || taskRef.current?.error || (reason ? `${reason}，已切换到历史日志` : '已切换到历史日志'),
        }, id)
        setTask(restoredTask)
        setPolling(false)
        return true
      } catch {
        return false
      }
    }

    pollTimerRef.current = window.setInterval(async () => {
      try {
        const t = await apiFetch(`/tasks/${id}`)
        const normalizedTask = normalizeTaskSnapshot(t, id)
        setTask(normalizedTask)
        if (normalizedTask.status === 'done' || normalizedTask.status === 'failed' || normalizedTask.status === 'stopped') {
          stopPolling()
          if (normalizedTask.cashier_urls && normalizedTask.cashier_urls.length > 0) {
            normalizedTask.cashier_urls.forEach((url: string) => window.open(url, '_blank'))
          }
        }
      } catch (error_: unknown) {
        const detail = error_ instanceof Error ? error_.message : '获取任务状态失败'
        const recovered = await loadHistoryFallback(detail)
        if (recovered) {
          stopPolling()
          if (!taskRef.current?.status || !['done', 'failed', 'stopped'].includes(String(taskRef.current.status))) {
            message.warning(detail)
          }
          return
        }
        stopPolling()
        setTask((previous: any) => normalizeTaskSnapshot({
          ...(previous || {}),
          id,
          status: previous?.status || 'failed',
          error: detail,
        }, id))
        message.error(detail)
      }
    }, 2000)
  }

  useEffect(() => {
    if (typeof window === 'undefined') return
    const saved = window.localStorage.getItem(REGISTER_TASK_STORAGE_KEY)
    if (!saved) return

    try {
      const parsed = JSON.parse(saved)
      const restoredTask = normalizeTaskSnapshot(parsed, parsed?.id)
      if (!restoredTask?.id) return
      setTask(restoredTask)
      if (!['done', 'failed', 'stopped'].includes(String(restoredTask.status))) {
        void pollTask(restoredTask.id)
      }
    } catch {
      window.localStorage.removeItem(REGISTER_TASK_STORAGE_KEY)
    }
    // Restore only once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (typeof window === 'undefined') return
    if (!task?.id) return
    const persistedTask = normalizeTaskSnapshot(task, task.id)
    window.localStorage.setItem(REGISTER_TASK_STORAGE_KEY, JSON.stringify({
      id: persistedTask?.id,
      task_id: persistedTask?.task_id,
      status: persistedTask?.status,
      progress: persistedTask?.progress,
      skipped: persistedTask?.skipped,
      success: persistedTask?.success,
      errors: persistedTask?.errors,
      cashier_urls: persistedTask?.cashier_urls,
      pending_verification: persistedTask?.pending_verification,
      error: persistedTask?.error,
    }))
  }, [task])

  const mailProvider = Form.useWatch('mail_provider', form)
  const tempmailSelectedDomains = Form.useWatch('tempmail_fixed_domains', form) || []
  const captchaSolver = Form.useWatch('captcha_solver', form)
  const platform = Form.useWatch('platform', form)
  const proxyMode = Form.useWatch('proxy_mode', form)
  const proxyFailover = Form.useWatch('proxy_failover', form)
  const manualEmail = Form.useWatch('email', form)
  const chatgptRegistrationEntry = Form.useWatch('chatgpt_registration_entry', form)
  const phoneSignupUsePool = Form.useWatch('chatgpt_phone_signup_use_pool', form)
  const executorOptions = getExecutorOptions(platform)
  const isManualEmailOtp = platform === 'chatgpt' && mailProvider === 'manual_email_otp'
  const isPhoneSignup = platform === 'chatgpt' && chatgptRegistrationEntry === 'phone_signup'
  const isRefreshTokenMode =
    chatgptRegistrationMode === CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN
  const normalizedTempMailSelectedDomains = normalizeDomainList(tempmailSelectedDomains)
  const tempmailDomainOptions = useMemo(() => {
    const byDomain = new Map<string, any>()
    tempmailDomains.forEach((item) => {
      const domain = String(item?.domain || '').trim().toLowerCase()
      if (domain) byDomain.set(domain, item)
    })
    normalizedTempMailSelectedDomains.forEach((domain) => {
      if (!byDomain.has(domain)) byDomain.set(domain, { domain, available: true })
    })
    return Array.from(byDomain.values()).map((item) => ({
      label: item.dns_status ? `${item.domain} · ${item.dns_status}` : item.domain,
      value: item.domain,
      disabled: item.available === false,
    }))
  }, [normalizedTempMailSelectedDomains, tempmailDomains])

  const loadTempMailDomains = async (silent = false) => {
    setTempmailDomainsLoading(true)
    try {
      const data = await apiFetch('/config/tempmail/domains', {
        method: 'POST',
        body: JSON.stringify({ include_inactive: false }),
      })
      const domains = Array.isArray(data?.domains) ? data.domains : []
      setTempmailDomains(domains)
      if (!silent) message.success(`已加载 ${domains.length} 个可用域名`)
    } catch (error: any) {
      if (!silent) message.error(error?.message || '读取 TempMail 域名失败')
    } finally {
      setTempmailDomainsLoading(false)
    }
  }

  useEffect(() => {
    if (mailProvider !== 'tempmail_local') return
    void loadTempMailDomains(true)
  }, [mailProvider])

  useEffect(() => {
    const currentExecutor = form.getFieldValue('executor_type')
    const normalizedExecutor = normalizeExecutorForPlatform(platform, currentExecutor)
    if (currentExecutor !== normalizedExecutor) {
      form.setFieldValue('executor_type', normalizedExecutor)
    }
  }, [form, platform])

  useEffect(() => {
    if (platform !== 'chatgpt' && ['manual_email_otp'].includes(String(mailProvider || ''))) {
      form.setFieldValue('mail_provider', 'luckmail')
      return
    }
    if (platform === 'chatgpt' && mailProvider === 'manual_email_otp') {
      form.setFieldsValue({ count: 1, concurrency: 1 })
      const savedEmail = window.localStorage.getItem('auto-chatgpt.manual_email_otp.email') || ''
      const currentEmail = String(form.getFieldValue('email') || '').trim()
      if (!currentEmail && savedEmail) {
        form.setFieldValue('email', savedEmail)
      }
    }
  }, [form, platform, mailProvider])

  useEffect(() => {
    if (!isPhoneSignup) return
    form.setFieldValue('concurrency', 1)
  }, [form, isPhoneSignup])

  useEffect(() => {
    if (!isManualEmailOtp) return
    const normalizedEmail = String(manualEmail || '').trim()
    if (!normalizedEmail) return
    window.localStorage.setItem('auto-chatgpt.manual_email_otp.email', normalizedEmail)
  }, [isManualEmailOtp, manualEmail])

  useEffect(() => () => stopPolling(), [])

  return (
    <div style={{ maxWidth: 800 }}>
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ fontSize: 24, fontWeight: 'bold', margin: 0 }}>注册任务</h1>
        <p style={{ color: '#7a8ba3', marginTop: 4 }}>创建账号自动注册任务</p>
      </div>

      <Form form={form} layout="vertical" onFinish={submit} initialValues={{
        platform: 'chatgpt',
        executor_type: 'protocol',
        captcha_solver: 'yescaptcha',
        mail_provider: 'luckmail',
        applemail_base_url: 'https://www.appleemail.top',
        applemail_pool_dir: 'mail',
        applemail_mailboxes: 'INBOX,Junk',
        gptmail_base_url: 'https://mail.chatgpt.org.uk',
        cloudmail_timeout: 30,
        tempmail_api_url: 'http://127.0.0.1:18081',
        tempmail_api_key_header: 'Authorization',
        tempmail_mode: 'fixed_domain',
        tempmail_wait_timeout_seconds: 180,
        tempmail_ttl_minutes: 30,
        tempmail_reuse_window_minutes: 20,
        tempmail_platform: 'chatgpt',
        tempmail_permanent: false,
        chatgpt_enable_team_invite: false,
        chatgpt_team_invite_deferred_activation: false,
        chatgpt_capture_business_workspace: true,
        chatgpt_capture_free_workspace: true,
        chatgpt_save_registration_access_token_account: true,
        chatgpt_registration_entry: 'email_signup',
        chatgpt_phone_signup_use_pool: false,
        chatgpt_phone_signup_timeout_seconds: 180,
        chatgpt_phone_signup_poll_interval_seconds: 5,
        chatgpt_phone_signup_max_resend_attempts: 1,
        chatgpt_phone_signup_resend_interval_seconds: 60,
        proxy_mode: 'pool',
        proxy_country_code: '',
        proxy_failover: false,
        proxy_max_candidates: 5,
        proxy_min_score: 50,
        count: 1,
        concurrency: 1,
        register_delay_seconds: 0,
        maliapi_base_url: 'https://maliapi.215.im/v1',
        maliapi_auto_domain_strategy: 'balanced',
        solver_url: 'http://localhost:8889',
      }}>
        <Card title="基本配置" style={{ marginBottom: 16 }}>
          <Form.Item label="平台">
            <Input value="ChatGPT" readOnly />
          </Form.Item>
          <Form.Item name="executor_type" label="执行器" rules={[{ required: true }]}>
            <Select options={executorOptions} />
          </Form.Item>
          <Form.Item name="captcha_solver" label="验证码" rules={[{ required: true }]}>
            <Select
              options={[
                { value: 'yescaptcha', label: 'YesCaptcha' },
                { value: 'local_solver', label: '本地 Solver (Camoufox)' },
                { value: 'manual', label: '手动' },
              ]}
            />
          </Form.Item>
          <Space style={{ width: '100%' }}>
            <Form.Item name="count" label="目标成功数" style={{ flex: 1 }}>
              <Input type="number" min={1} disabled={isManualEmailOtp} />
            </Form.Item>
            <Form.Item name="concurrency" label="并发数" style={{ flex: 1 }}>
              <Input type="number" min={1} max={5} disabled={isManualEmailOtp || isPhoneSignup} />
            </Form.Item>
          </Space>
          <Space style={{ width: '100%' }}>
            <Form.Item name="register_delay_seconds" label="最小注册延迟(秒)" initialValue={0} style={{ flex: 1 }}>
              <InputNumber min={0} precision={1} step={0.5} style={{ width: '100%' }} placeholder="0" />
            </Form.Item>
            <Form.Item name="register_delay_max_seconds" label="最大延迟(秒)" initialValue={0} style={{ flex: 1 }}>
              <InputNumber min={0} precision={1} step={0.5} style={{ width: '100%' }} placeholder="0" />
            </Form.Item>
            <Form.Item name="proxy_mode" label="代理模式" style={{ flex: 1 }}>
              <Select
                options={[
                  { value: 'direct', label: '直连' },
                  { value: 'pool', label: '使用代理池' },
                  { value: 'specified', label: '指定代理' },
                ]}
              />
            </Form.Item>
          </Space>
          {proxyMode === 'specified' ? (
            <Space style={{ width: '100%' }} align="start">
              <Form.Item name="proxy" label="指定代理" style={{ flex: 1 }}>
                <Input placeholder="http://user:pass@host:port" />
              </Form.Item>
              <Form.Item name="proxy_failover" valuePropName="checked" label="失败处理" style={{ width: 180 }}>
                <Checkbox>失败后切换代理池</Checkbox>
              </Form.Item>
            </Space>
          ) : null}
          {proxyMode === 'pool' || (proxyMode === 'specified' && proxyFailover) ? (
            <Space style={{ width: '100%' }} align="start">
              <Form.Item name="proxy_country_code" label="出口国家" style={{ flex: 1 }}>
                <Input placeholder="不限，或填 US / JP / SG" />
              </Form.Item>
              <Form.Item name="proxy_min_score" label="最低健康分" style={{ width: 150 }}>
                <InputNumber min={0} max={100} precision={0} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="proxy_max_candidates" label="最多候选" style={{ width: 150 }}>
                <InputNumber min={1} max={100} precision={0} style={{ width: '100%' }} />
              </Form.Item>
            </Space>
          ) : null}
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="代理模式说明"
            description="直连不会使用代理池；指定代理只用填写的节点，勾选失败切换后才会回退代理池；使用代理池会按健康分、冷却状态和实测出口国家挑选候选。"
          />
          {platform === 'chatgpt' && (
            <>
              <Form.Item name="chatgpt_registration_entry" label="注册入口">
                <Select
                  options={[
                    { value: 'email_signup', label: '邮箱注册' },
                  ]}
                />
              </Form.Item>
              {isPhoneSignup ? (
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message="当前为手机号注册"
                  description="手机号会作为 ChatGPT 登录标识；接码输入格式沿用手机号绑定的“手机号----收码API”。当前只执行注册阶段，并保存注册阶段 AccessToken 账号。"
                />
              ) : null}
              <Form.Item label="ChatGPT Token 方案">
                <ChatGPTRegistrationModeSwitch
                  mode={chatgptRegistrationMode}
                  onChange={setChatgptRegistrationMode}
                />
              </Form.Item>
              {!isRefreshTokenMode ? (
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message="当前为无 RT 方案"
                  description="team invite / 延迟激活相关配置会保留，但无 RT 方案下可能无法完整生效。"
                />
              ) : null}
              {!isRefreshTokenMode ? (
                <Form.Item
                  name="chatgpt_access_token_only_checkout_amount_check_enabled"
                  valuePropName="checked"
                  extra="关闭后：仍生成订阅链接，但不做 checkout amount 额度校验，也不会因为额度结果跳过保存账号。"
                >
                  <Checkbox>启用额度验证</Checkbox>
                </Form.Item>
              ) : null}
              {!isRefreshTokenMode ? (
                <Space style={{ width: '100%' }}>
                  <Form.Item
                    name="chatgpt_access_token_only_checkout_country"
                    label="额度验证国家"
                    style={{ flex: 1 }}
                    extra="无 RT 注册生成订阅链接并验证 amount 时使用。"
                  >
                    <Input placeholder="US" />
                  </Form.Item>
                  <Form.Item
                    name="chatgpt_access_token_only_checkout_currency"
                    label="额度验证货币"
                    style={{ flex: 1 }}
                    extra="默认 USD；留空会按国家推导。"
                  >
                    <Input placeholder="USD" />
                  </Form.Item>
                </Space>
              ) : null}
              <Form.Item
                name="chatgpt_access_token_only_gopay_provider_link_enabled"
                valuePropName="checked"
                extra="开启后：注册/登录成功后继续进入 GoPay/Midtrans 平台链接阶段，只保存平台链接，不进入手机号 OTP/PIN；失败不会丢弃已注册账号。"
              >
                <Checkbox>注册后获取 GoPay 平台链接</Checkbox>
              </Form.Item>
              {isRefreshTokenMode ? (
                <Form.Item
                  name="chatgpt_save_registration_access_token_account"
                  valuePropName="checked"
                  initialValue={true}
                  extra="默认开启：注册阶段已拿到 AccessToken，但后续 refresh_token / 工作空间抓取失败时，也会保存一个 AccessToken-only 账号，避免真实注册成功却没有入库。"
                >
                  <Checkbox>保存注册阶段 AccessToken 账号</Checkbox>
                </Form.Item>
              ) : null}
              <>
                <Form.Item
                  label="工作空间抓取"
                  extra="free 勾选独立生效；business 依赖 team invite。若两项都勾，会分别获取并按名称区分保存。"
                >
                  <Space direction="vertical" size={6}>
                    <Form.Item name="chatgpt_capture_free_workspace" valuePropName="checked" noStyle>
                      <Checkbox>抓取 free 工作空间</Checkbox>
                    </Form.Item>
                  </Space>
                </Form.Item>
                <Form.Item
                  name="chatgpt_enable_team_invite"
                  valuePropName="checked"
                  label="Business Team Invite"
                  extra="关闭时走原始注册/登录链路；开启后才会进入 business recovery / team invite。"
                >
                  <Checkbox>启用 team invite / business 恢复</Checkbox>
                </Form.Item>
                <Form.Item
                  noStyle
                  shouldUpdate={(prev, next) => prev.chatgpt_enable_team_invite !== next.chatgpt_enable_team_invite}
                >
                  {({ getFieldValue }) => {
                    const teamInviteEnabled = getFieldValue('chatgpt_enable_team_invite')
                    return (
                      <>
                        <Form.Item
                          name="chatgpt_team_invite_deferred_activation"
                          valuePropName="checked"
                          extra="开启后：先完成全部账号注册并发出邀请，再统一进入激活阶段；不会在单账号刚注册完时立刻进入 business/free。"
                        >
                          <Checkbox disabled={!teamInviteEnabled}>延迟邀请（先统一发邀请，再统一激活）</Checkbox>
                        </Form.Item>
                        <Form.Item>
                          <Space direction="vertical" size={6}>
                            <Form.Item name="chatgpt_capture_business_workspace" valuePropName="checked" noStyle>
                              <Checkbox disabled={!teamInviteEnabled}>抓取 business 工作空间</Checkbox>
                            </Form.Item>
                          </Space>
                        </Form.Item>
                        {!teamInviteEnabled ? (
                          <Alert
                            type="info"
                            showIcon
                            message="当前关闭 team invite"
                            description="普通模式下会直接走 free 主链；business 与延迟邀请配置在开启 team invite 后才生效。"
                          />
                        ) : null}
                      </>
                    )
                  }}
                </Form.Item>
              </>
            </>
          )}
        </Card>

        {!isPhoneSignup ? (
        <Card title="邮箱配置" style={{ marginBottom: 16 }}>
          <Form.Item name="mail_provider" label="邮箱服务" rules={[{ required: true }]}>
            <Select
              options={[
                ...(platform === 'chatgpt'
                  ? [
                      { value: 'manual_email_otp', label: '手动邮箱 + 手输验证码' },
                      { value: 'hme_ready_api', label: 'HME Ready API' },
                      { value: 'icloud_hme', label: 'iCloud HME' },
                    ]
                  : []),
                { value: 'luckmail', label: 'LuckMail' },
                { value: 'applemail', label: 'AppleMail / 小苹果' },
                { value: 'moemail', label: 'MoeMail (sall.cc)' },
                { value: 'tempmail_lol', label: 'TempMail.lol' },
                { value: 'tempmail_local', label: 'TempMail Ready API' },
                { value: 'skymail', label: 'SkyMail (CloudMail)' },
                { value: 'cloudmail', label: 'CloudMail (genToken)' },
                { value: 'maliapi', label: 'YYDS Mail / MaliAPI' },
                { value: 'gptmail', label: 'GPTMail' },
                { value: 'opentrashmail', label: 'OpenTrashMail' },
                { value: 'duckmail', label: 'DuckMail' },
                { value: 'freemail', label: 'Freemail' },
                { value: 'laoudo', label: 'Laoudo' },
                { value: 'cfworker', label: 'CF Worker' },
              ]}
            />
          </Form.Item>
          {mailProvider === 'manual_email_otp' && (
            <>
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 16 }}
                message="手动邮箱 + 手输验证码"
                description={
                  <div>
                    <div>适合你自己掌控邮箱时使用。</div>
                    <div>流程是：先手填邮箱 → 开始任务 → 若注册阶段或 OAuth 阶段需要邮箱验证码，任务状态区会弹出输入框。</div>
                    <div>当前模式会自动锁定为单任务、单并发，密码仍由系统随机生成。</div>
                  </div>
                }
              />
              <Form.Item
                name="email"
                label="手填邮箱地址"
                rules={[{ required: true, message: '请输入邮箱地址' }]}
                extra="会记住你上次填写的邮箱；下次切回这个模式时自动回填。"
              >
                <Input placeholder="name@gmail.com" autoComplete="email" />
              </Form.Item>
              <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
                当前模式下密码不会手填，系统会自动随机生成；批量数量和并发数会强制锁为 1。
              </Text>
            </>
          )}
          {mailProvider === 'tempmail_local' && (
            <>
              <Form.Item name="tempmail_api_url" label="API URL" rules={[{ required: true, message: '请输入 TempMail API 地址' }]}>
                <Input placeholder="http://127.0.0.1:18081" />
              </Form.Item>
              <Form.Item name="tempmail_api_key" label="API Key" rules={[{ required: true, message: '请输入 TempMail API Key' }]}>
                <Input.Password placeholder="tm_xxx" />
              </Form.Item>
              <Form.Item name="tempmail_api_key_header" label="鉴权 Header">
                <Input placeholder="Authorization" />
              </Form.Item>
              <Form.Item name="tempmail_mode" label="建箱模式">
                <Select
                  options={[
                    { value: 'fixed_domain', label: '固定域名' },
                    { value: 'task_subdomain', label: '随机子域 / Ready' },
                  ]}
                />
              </Form.Item>
              <Space align="start" style={{ width: '100%' }}>
                <Form.Item
                  name="tempmail_fixed_domains"
                  label="可用域名（固定域名模式时必填）"
                  style={{ flex: 1 }}
                  rules={[
                    {
                      validator: (_, value) => {
                        if (form.getFieldValue('tempmail_mode') !== 'fixed_domain') return Promise.resolve()
                        return normalizeDomainList(value).length > 0
                          ? Promise.resolve()
                          : Promise.reject(new Error('请选择至少一个 TempMail 可用域名'))
                      },
                    },
                  ]}
                  extra="可单选或多选；多选时每个新邮箱会从候选域名中随机选择一个。"
                >
                  <Select
                    mode="multiple"
                    allowClear
                    showSearch
                    loading={tempmailDomainsLoading}
                    placeholder={tempmailDomainsLoading ? '正在加载域名...' : '请选择一个或多个可用域名'}
                    options={tempmailDomainOptions}
                    optionFilterProp="label"
                  />
                </Form.Item>
                <Button
                  icon={<ReloadOutlined />}
                  loading={tempmailDomainsLoading}
                  onClick={() => { void loadTempMailDomains(false) }}
                  style={{ marginTop: 30 }}
                >
                  刷新
                </Button>
              </Space>
              <Form.Item name="tempmail_primary_domain" hidden>
                <Input />
              </Form.Item>
              <Form.Item name="tempmail_wait_timeout_seconds" label="建箱等待秒数">
                <InputNumber min={30} max={600} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="tempmail_ttl_minutes" label="邮箱 TTL 分钟">
                <InputNumber min={1} max={1440} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="tempmail_reuse_window_minutes" label="子域复用窗口分钟">
                <InputNumber min={1} max={1440} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="tempmail_platform" label="平台标识">
                <Input placeholder="chatgpt" />
              </Form.Item>
              <Form.Item name="tempmail_permanent" valuePropName="checked">
                <Checkbox>永久邮箱</Checkbox>
              </Form.Item>
            </>
          )}
          {mailProvider === 'skymail' && (
            <>
              <Form.Item name="skymail_api_base" label="API Base">
                <Input placeholder="https://api.skymail.ink" />
              </Form.Item>
              <Form.Item name="skymail_token" label="Authorization Token">
                <Input.Password placeholder="Bearer xxxxx" />
              </Form.Item>
              <Form.Item name="skymail_domain" label="邮箱域名">
                <Input placeholder="mail.example.com" />
              </Form.Item>
            </>
          )}
          {mailProvider === 'cloudmail' && (
            <>
              <Form.Item name="cloudmail_api_base" label="API Base" rules={[{ required: true, message: '请输入 CloudMail API 地址' }]}>
                <Input placeholder="https://cloudmail.example.com" />
              </Form.Item>
              <Form.Item name="cloudmail_admin_email" label="管理员邮箱（可选）" extra="留空自动使用 admin@域名">
                <Input placeholder="admin@example.com" />
              </Form.Item>
              <Form.Item name="cloudmail_admin_password" label="管理员密码" rules={[{ required: true, message: '请输入 CloudMail 管理员密码' }]}>
                <Input.Password placeholder="admin password" />
              </Form.Item>
              <Form.Item name="cloudmail_domain" label="邮箱域名（可选）" extra="支持单个域名，或逗号分隔多个域名">
                <Input placeholder="mail.example.com,mail2.example.com" />
              </Form.Item>
              <Form.Item name="cloudmail_subdomain" label="子域名（可选）">
                <Input placeholder="pool-a" />
              </Form.Item>
              <Form.Item name="cloudmail_timeout" label="请求超时秒数">
                <InputNumber min={5} max={120} style={{ width: '100%' }} />
              </Form.Item>
            </>
          )}
          {mailProvider === 'laoudo' && (
            <>
              <Form.Item name="laoudo_email" label="邮箱地址">
                <Input placeholder="xxx@laoudo.com" />
              </Form.Item>
              <Form.Item name="laoudo_account_id" label="Account ID">
                <Input placeholder="563" />
              </Form.Item>
              <Form.Item name="laoudo_auth" label="JWT Token">
                <Input placeholder="eyJ..." />
              </Form.Item>
            </>
          )}
          {mailProvider === 'maliapi' && (
            <>
              <Form.Item name="maliapi_base_url" label="API URL">
                <Input placeholder="https://maliapi.215.im/v1" />
              </Form.Item>
              <Form.Item name="maliapi_api_key" label="API Key">
                <Input.Password placeholder="AC-..." />
              </Form.Item>
              <Form.Item name="maliapi_domain" label="邮箱域名（可选）">
                <Input placeholder="example.com" />
              </Form.Item>
              <Form.Item name="maliapi_auto_domain_strategy" label="自动域名策略">
                <Select
                  options={[
                    { value: 'balanced', label: 'balanced' },
                    { value: 'prefer_owned', label: 'prefer_owned' },
                    { value: 'prefer_public', label: 'prefer_public' },
                  ]}
                />
              </Form.Item>
            </>
          )}
          {mailProvider === 'applemail' && (
            <>
              <Form.Item name="applemail_base_url" label="API URL">
                <Input placeholder="https://www.appleemail.top" />
              </Form.Item>
              <Form.Item
                name="applemail_pool_dir"
                label="邮箱池目录"
                extra="默认读取项目根目录下的 mail 目录。"
              >
                <Input placeholder="mail" />
              </Form.Item>
              <Form.Item
                name="applemail_pool_file"
                label="邮箱池文件（可选）"
                extra="留空会自动使用目录中最新的 .json/.txt 文件；JSON 内容导入请到全局配置页操作。"
              >
                <Input placeholder="applemail_20260403.json" />
              </Form.Item>
              <Form.Item name="applemail_mailboxes" label="轮询文件夹">
                <Input placeholder="INBOX,Junk" />
              </Form.Item>
            </>
          )}
          {mailProvider === 'gptmail' && (
            <>
              <Form.Item name="gptmail_base_url" label="API URL">
                <Input placeholder="https://mail.chatgpt.org.uk" />
              </Form.Item>
              <Form.Item name="gptmail_api_key" label="API Key">
                <Input.Password placeholder="gpt-test" />
              </Form.Item>
              <Form.Item
                name="gptmail_domain"
                label="邮箱域名（可选）"
                extra="已知当前可用域名时可直接本地拼装随机地址，省掉一次 generate-email 请求"
              >
                <Input placeholder="example.com" />
              </Form.Item>
            </>
          )}
          {mailProvider === 'opentrashmail' && (
            <>
              <Form.Item name="opentrashmail_api_url" label="API URL" rules={[{ required: true, message: '请输入 OpenTrashMail 地址' }]}>
                <Input placeholder="http://mail.example.com:8085" />
              </Form.Item>
              <Form.Item
                name="opentrashmail_domain"
                label="邮箱域名（可选）"
                extra="已知 OpenTrashMail 当前启用域名时可直接本地拼装随机地址；留空则调用 /api/random 自动获取"
              >
                <Input placeholder="xiyoufm.com" />
              </Form.Item>
              <Form.Item
                name="opentrashmail_password"
                label="站点密码（可选）"
                extra="当 OpenTrashMail 开启 PASSWORD 保护时填写，会自动追加到 JSON API 查询参数"
              >
                <Input.Password placeholder="留空表示未启用" />
              </Form.Item>
            </>
          )}
          {mailProvider === 'cfworker' && (
            <>
              <Form.Item name="cfworker_api_url" label="API URL">
                <Input placeholder="https://apimail.example.com" />
              </Form.Item>
              <Form.Item name="cfworker_admin_token" label="Admin Token">
                <Input placeholder="abc123,,,abc" />
              </Form.Item>
              <Form.Item name="cfworker_custom_auth" label="Site Password">
                <Input.Password placeholder="private site password" />
              </Form.Item>
              <Form.Item
                name="cfworker_domain_override"
                label="单次任务指定域名（可选）"
                extra="留空时将从设置页已启用的域名列表中随机选择。"
              >
                <Input placeholder="example.com" />
              </Form.Item>
              <Form.Item
                name="cfworker_subdomain"
                label="子域名（可选）"
                extra="填写后将生成 xxx@子域名.根域名；若启用随机子域名，则会生成 xxx@随机值.子域名.根域名。"
              >
                <Input placeholder="mail / pool-a" />
              </Form.Item>
              <Form.Item name="cfworker_random_subdomain" label="随机子域名" valuePropName="checked">
                <Checkbox>每次注册前随机生成一层子域名</Checkbox>
              </Form.Item>
              <Form.Item name="cfworker_fingerprint" label="Fingerprint (可选)">
                <Input placeholder="cfb82279f..." />
              </Form.Item>
            </>
          )}
          {mailProvider === 'freemail' && (
            <>
              <Form.Item name="freemail_api_url" label="API URL" rules={[{ required: true, message: '请输入 Freemail API 地址' }]}>
                <Input placeholder="https://mail.example.com" />
              </Form.Item>
              <Form.Item name="freemail_admin_token" label="管理员令牌（可选）">
                <Input.Password placeholder="JWT_TOKEN" />
              </Form.Item>
              <Form.Item name="freemail_username" label="用户名（可选）">
                <Input placeholder="admin" />
              </Form.Item>
              <Form.Item name="freemail_password" label="密码（可选）">
                <Input.Password placeholder="password" />
              </Form.Item>
              <Form.Item name="freemail_domain" label="邮箱域名（可选）" extra="填写后会优先使用该域名生成邮箱">
                <Input placeholder="example.com" />
              </Form.Item>
            </>
          )}
          {mailProvider === 'luckmail' && (
            <>
              <Form.Item name="luckmail_base_url" label="平台地址">
                <Input placeholder="https://mails.luckyous.com" />
              </Form.Item>
              <Form.Item name="luckmail_api_key" label="API Key">
                <Input.Password placeholder="ak_..." />
              </Form.Item>
              <Form.Item name="luckmail_email_type" label="邮箱类型（可选）">
                <Input placeholder="ms_graph / ms_imap" />
              </Form.Item>
              <Form.Item name="luckmail_domain" label="邮箱域名（可选）">
                <Input placeholder="outlook.com" />
              </Form.Item>
            </>
          )}
        </Card>
        ) : null}

        {platform === 'chatgpt' && (
          <Card title="ChatGPT 手机验证" style={{ marginBottom: 16 }}>
            {isPhoneSignup ? (
              <>
                <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
                  手机号注册使用与手机号绑定一致的输入格式：每行 `手机号----收码API`。若号码已注册，会使用同一个密码走手机号登录短信验证。
                </Text>
                <Form.Item
                  name="login_password"
                  label="手机号注册/登录密码"
                  rules={[{ required: isPhoneSignup, message: '请输入手机号注册/登录密码' }]}
                  extra="新手机号注册时用这个密码创建账号；遇到已注册手机号时，也用这个密码登录续跑。"
                >
                  <Input.Password placeholder="新注册和已注册登录共用同一个密码" autoComplete="new-password" />
                </Form.Item>
                <Form.Item name="chatgpt_phone_signup_use_pool" valuePropName="checked">
                  <Checkbox>使用手机号池</Checkbox>
                </Form.Item>
                {!phoneSignupUsePool ? (
                  <Form.Item
                    name="chatgpt_phone_signup_phone_lines"
                    label="手机号----收码API"
                    rules={[
                      {
                        validator: (_, value) => {
                          if (!isPhoneSignup || phoneSignupUsePool) return Promise.resolve()
                          return String(value || '').trim()
                            ? Promise.resolve()
                            : Promise.reject(new Error('请粘贴 手机号----收码API，或勾选使用手机号池'))
                        },
                      },
                    ]}
                    extra="示例：+573234567890----https://example.com/api/sms?id=xxx"
                  >
                    <Input.TextArea autoSize={{ minRows: 4, maxRows: 10 }} placeholder="+573234567890----https://example.com/api/sms?id=xxx" />
                  </Form.Item>
                ) : (
                  <Alert
                    type="info"
                    showIcon
                    style={{ marginBottom: 12 }}
                    message="将从手机号池取号"
                    description="手机号注册会串行执行，成功注册后的号码会标记为已用，避免再次作为注册手机号复用。"
                  />
                )}
                <Space style={{ width: '100%' }} align="start">
                  <Form.Item name="chatgpt_phone_signup_timeout_seconds" label="短信等待秒数" style={{ flex: 1 }}>
                    <InputNumber min={10} max={1800} style={{ width: '100%' }} />
                  </Form.Item>
                  <Form.Item name="chatgpt_phone_signup_poll_interval_seconds" label="轮询间隔秒数" style={{ flex: 1 }}>
                    <InputNumber min={1} max={60} style={{ width: '100%' }} />
                  </Form.Item>
                </Space>
                <Space style={{ width: '100%' }} align="start">
                  <Form.Item name="chatgpt_phone_signup_max_resend_attempts" label="重发次数" style={{ flex: 1 }}>
                    <InputNumber min={1} max={20} style={{ width: '100%' }} />
                  </Form.Item>
                  <Form.Item name="chatgpt_phone_signup_resend_interval_seconds" label="重发后等待秒数" style={{ flex: 1 }}>
                    <InputNumber min={10} max={600} style={{ width: '100%' }} />
                  </Form.Item>
                </Space>
              </>
            ) : (
              <>
                <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
                  仅在 OAuth 流程进入 `add_phone` 时使用，用于自动取号并轮询短信验证码。
                </Text>
                <Form.Item name="smstome_cookie" label="SMSToMe Cookie">
                  <Input.Password placeholder="cf_clearance=...; PHPSESSID=..." />
                </Form.Item>
                <Form.Item name="smstome_country_slugs" label="国家列表">
                  <Input placeholder="united-kingdom,poland,finland" />
                </Form.Item>
                <Form.Item name="smstome_phone_attempts" label="手机号尝试次数">
                  <Input placeholder="3" />
                </Form.Item>
                <Form.Item name="smstome_otp_timeout_seconds" label="短信等待秒数">
                  <Input placeholder="45" />
                </Form.Item>
                <Form.Item name="smstome_poll_interval_seconds" label="轮询间隔秒数">
                  <Input placeholder="5" />
                </Form.Item>
                <Form.Item name="smstome_sync_max_pages_per_country" label="每国同步页数">
                  <Input placeholder="5" />
                </Form.Item>
              </>
            )}
          </Card>
        )}

        {captchaSolver === 'yescaptcha' && (
          <Card title="验证码配置" style={{ marginBottom: 16 }}>
            <Form.Item name="yescaptcha_key" label="YesCaptcha Key">
              <Input />
            </Form.Item>
          </Card>
        )}

        {captchaSolver === 'local_solver' && (
          <Card title="本地 Solver 配置" style={{ marginBottom: 16 }}>
            <Form.Item name="solver_url" label="Solver URL">
              <Input />
            </Form.Item>
            <Text type="secondary" style={{ fontSize: 12 }}>
              启动命令: python services/turnstile_solver/start.py --browser_type camoufox --port 8889
            </Text>
          </Card>
        )}

        <Button type="primary" htmlType="submit" block disabled={polling} icon={polling ? <LoadingOutlined /> : <PlayCircleOutlined />}>
          {polling ? '任务运行中...' : '开始注册'}
        </Button>
      </Form>

      {task && (
        <Card title={
          <Space>
            <span>任务状态</span>
            <Tag color={
              task.status === 'done' ? 'success' :
              task.status === 'stopped' ? 'warning' :
              task.status === 'failed' ? 'error' : 'processing'
            }>
              {task.status}
            </Tag>
          </Space>
        } style={{ marginTop: 16 }}>
          <Descriptions column={1} size="small">
            <Descriptions.Item label="任务 ID">
              <Text copyable style={{ fontFamily: 'monospace' }}>{task.id}</Text>
            </Descriptions.Item>
            <Descriptions.Item label="成功进度">{task.progress}</Descriptions.Item>
            <Descriptions.Item label="跳过">{task.skipped ?? 0}</Descriptions.Item>
          </Descriptions>
          {task.success != null && (
            <div style={{ marginTop: 8, color: '#10b981' }}>
              <CheckCircleOutlined /> 成功 {task.success} 个
            </div>
          )}
          {task.errors?.length > 0 && (
            <div style={{ marginTop: 8 }}>
              {task.errors.map((e: string, i: number) => (
                <div key={i} style={{ color: '#ef4444', marginBottom: 4 }}>
                  <CloseCircleOutlined /> {e}
                </div>
              ))}
            </div>
          )}
          {task.error && (
            <div style={{ marginTop: 8, color: '#ef4444' }}>
              <CloseCircleOutlined /> {task.error}
            </div>
          )}
          {task.pending_verification ? (
            <TaskVerificationPanel
              taskId={task.id}
              verification={task.pending_verification}
            />
          ) : null}
          {Array.isArray(task?.meta?.runtime_results) && task.meta.runtime_results.length > 0 ? (
            <div style={{ marginTop: 16 }}>
              <PhoneBindingResultsTable
                results={task.meta.runtime_results}
                prefixSummary={task.meta.prefix_summary}
                showPrefixSummary={Array.isArray(task?.meta?.prefix_summary?.items)}
                showSuccessfulLines
                boundPhoneLines={Array.isArray(task?.meta?.registered_phone_lines) ? task.meta.registered_phone_lines : []}
                emptyText="任务结束后，这里会输出已完成手机号注册的手机号。"
              />
            </div>
          ) : null}
          {task.id ? (
            <div style={{ marginTop: 16 }}>
              <TaskLogPanel taskId={task.id} />
            </div>
          ) : null}
        </Card>
      )}
    </div>
  )
}
