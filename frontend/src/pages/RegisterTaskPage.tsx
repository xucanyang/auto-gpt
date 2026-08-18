import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Card,
  Form,
  Input,
  InputNumber,
  Segmented,
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
import { RegistrationCountrySelect } from '@/features/auth/components/RegistrationCountrySelect'
import { RegistrationEligibilityCountryField } from '@/features/auth/components/RegistrationEligibilityCountryField'
import { RegistrationPaypalPaymentField } from '@/features/auth/components/RegistrationPaypalPaymentField'
import { RegistrationPipelineSummary } from '@/features/auth/components/RegistrationPipelineSummary'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import { TaskVerificationPanel } from '@/components/TaskVerificationPanel'
import { usePersistentChatGPTRegistrationMode } from '@/hooks/usePersistentChatGPTRegistrationMode'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import { buildChatGPTRegistrationRequestAdapter } from '@/lib/chatgptRegistrationRequestAdapter'
import {
  CHATGPT_REGISTER_DEFAULT_CONCURRENCY,
  CHATGPT_REGISTER_DEFAULT_DELAY_MAX_SECONDS,
  CHATGPT_REGISTER_DEFAULT_DELAY_SECONDS,
  getRegisterDefaultConcurrency,
  getRegisterConcurrencyLimit,
  isRegisterUniqueExitEnabled,
  normalizeRegisterConcurrency,
  normalizeRegisterDelaySettings,
  normalizeRegisterUniqueExitPolicy,
  type ChatGPTRegisterControlConfig,
} from '@/lib/chatgptRegisterTaskControls'
import {
  EXECUTOR_SELECTION_HELP,
  getExecutorOptions,
  normalizeExecutorForPlatform,
} from '@/lib/platformExecutorOptions'
import {
  getBrowserFamilyOptions,
  getBrowserFamilySelectionHelp,
  normalizeBrowserFamilyForExecutor,
} from '@/lib/browserFamilyOptions'
import { apiFetch } from '@/lib/utils'
import { buildTaskProxyPayload, taskProxySettingsFromConfig, validateTaskProxySettings } from '@/lib/taskProxySettings'
import {
  DEFAULT_REGISTRATION_ZERO_AMOUNT_COUNTRY,
  REGISTRATION_ZERO_AMOUNT_ENABLED_FIELD,
  REGISTRATION_ZERO_AMOUNT_COUNTRY_FIELD,
  readRegistrationEligibilityEnabled,
  readRegistrationEligibilityCountry,
} from '@/lib/registrationEligibilityCountry'
import {
  REGISTRATION_PAYPAL_LINK_ENABLED_FIELD,
  REGISTRATION_PAYPAL_PAYMENT_ENABLED_FIELD,
  readRegistrationPaypalLinkEnabled,
  readRegistrationPaypalPaymentEnabled,
} from '@/lib/registrationPaypalPayment'
import { normalizeDomainList, parseStoredDomainList } from '@/lib/domainList'
import {
  REGISTRATION_DIAGNOSTICS_OPTIONS,
  normalizeRegistrationDiagnosticsMode,
} from '@/lib/registrationDiagnostics'

const { Text } = Typography

const REGISTER_TASK_STORAGE_KEY = 'auto-chatgpt.register-task-page.current-task'
const DEFAULT_TEMPMAIL_API_URL = 'http://tempmail-api-1:8080'

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
  const [registerControlConfig, setRegisterControlConfig] = useState<ChatGPTRegisterControlConfig>({})
  const pollTimerRef = useRef<number | null>(null)
  const taskRef = useRef<any>(null)
  const { mode: chatgptRegistrationMode, setMode: setChatgptRegistrationMode } =
    usePersistentChatGPTRegistrationMode()

  useEffect(() => {
    apiFetch('/config').then((cfg) => {
      setRegisterControlConfig(cfg)
      const currentPlatform = form.getFieldValue('platform') || 'chatgpt'
      const proxySettings = taskProxySettingsFromConfig(cfg)
      const executorType = normalizeExecutorForPlatform(currentPlatform, cfg.default_executor)
      const delaySettings = normalizeRegisterDelaySettings({}, currentPlatform, cfg)
      form.setFieldsValue({
        executor_type: executorType,
        browser_family: normalizeBrowserFamilyForExecutor(
          currentPlatform,
          executorType,
          cfg.default_browser_family,
        ),
        concurrency: getRegisterDefaultConcurrency(currentPlatform, executorType, cfg),
        ...delaySettings,
        captcha_solver: cfg.default_captcha_solver || 'yescaptcha',
        mail_provider: cfg.mail_provider || 'luckmail',
        email_api_lines: cfg.email_api_lines || '',
        email_api_poll_interval_seconds: cfg.email_api_poll_interval_seconds || 3,
        email_api_request_timeout_seconds: cfg.email_api_request_timeout_seconds || 15,
        email_api_gmail_dot_variant_enabled: cfg.email_api_gmail_dot_variant_enabled === '' ? true : parseBooleanConfigValue(cfg.email_api_gmail_dot_variant_enabled),
        email_api_gmail_variant_count: Number(cfg.email_api_gmail_variant_count || 2) || 2,
        email_api_gmail_variant_rules: cfg.email_api_gmail_variant_rules || 'all',
        email_api_gmail_plus_tag_template: cfg.email_api_gmail_plus_tag_template || 'r{rand}',
        email_api_default_scheme: cfg.email_api_default_scheme || 'https',
        applemail_base_url: cfg.applemail_base_url || 'https://www.appleemail.top',
        applemail_pool_dir: cfg.applemail_pool_dir || 'mail',
        applemail_pool_file: cfg.applemail_pool_file || '',
        applemail_mailboxes: cfg.applemail_mailboxes || 'INBOX,Junk',
        yescaptcha_key: cfg.yescaptcha_key || '',
        moemail_api_url: cfg.moemail_api_url || '',
        moemail_api_key: cfg.moemail_api_key || '',
        tempmail_api_url: cfg.tempmail_api_url || DEFAULT_TEMPMAIL_API_URL,
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
        ...proxySettings,
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
        [REGISTRATION_ZERO_AMOUNT_ENABLED_FIELD]: readRegistrationEligibilityEnabled(),
        [REGISTRATION_ZERO_AMOUNT_COUNTRY_FIELD]:
          readRegistrationEligibilityCountry() || DEFAULT_REGISTRATION_ZERO_AMOUNT_COUNTRY,
        [REGISTRATION_PAYPAL_LINK_ENABLED_FIELD]:
          readRegistrationPaypalLinkEnabled(),
        [REGISTRATION_PAYPAL_PAYMENT_ENABLED_FIELD]:
          readRegistrationPaypalPaymentEnabled(),
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
        chatgpt_access_token_only_checkout_amount_check_enabled:
          cfg.chatgpt_access_token_only_checkout_amount_check_enabled === ''
            ? true
            : parseBooleanConfigValue(cfg.chatgpt_access_token_only_checkout_amount_check_enabled),
        chatgpt_access_token_only_checkout_country: String(cfg.chatgpt_access_token_only_checkout_country || 'US').trim().toUpperCase() || 'US',
        chatgpt_access_token_only_checkout_currency: String(cfg.chatgpt_access_token_only_checkout_currency || 'USD').trim().toUpperCase() || 'USD',
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
        chatgpt_register_unique_exit_ip_policy: normalizeRegisterUniqueExitPolicy(
          cfg.chatgpt_register_unique_exit_ip_policy,
          cfg.chatgpt_register_unique_exit_ip_enabled,
        ),
        chatgpt_register_otp_wait_seconds: cfg.chatgpt_register_otp_wait_seconds || 120,
        chatgpt_register_otp_resend_wait_seconds: cfg.chatgpt_register_otp_resend_wait_seconds || 90,
        chatgpt_register_otp_account_budget_seconds: cfg.chatgpt_register_otp_account_budget_seconds || 210,
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
    const executorType = normalizeExecutorForPlatform(values.platform, values.executor_type)
    const browserFamily = normalizeBrowserFamilyForExecutor(
      values.platform,
      executorType,
      values.browser_family,
    )
    const forceSerial = phoneSignupEnabled || (values.platform === 'chatgpt' && values.mail_provider === 'manual_email_otp')
    const concurrency = normalizeRegisterConcurrency(
      values.concurrency,
      values.platform,
      executorType,
      forceSerial,
      registerControlConfig,
    )
    const delaySettings = normalizeRegisterDelaySettings(values, values.platform, registerControlConfig)
    if (values.mail_provider === 'tempmail_local' && (values.tempmail_mode || 'fixed_domain') === 'fixed_domain'
      && normalizeDomainList(values.tempmail_fixed_domains).length === 0) {
      message.error('固定域名模式下请至少选择一个 TempMail 可用域名')
      return
    }
    if (phoneSignupEnabled && !values.chatgpt_phone_signup_use_pool && !String(values.chatgpt_phone_signup_phone_lines || '').trim()) {
      message.error('手机号注册请粘贴 手机号----收码API / 手机号|收码API，或勾选使用手机号池')
      return
    }
    if (!phoneSignupEnabled && values.platform === 'chatgpt' && values.mail_provider === 'email_api' && !String(values.email_api_lines || '').trim()) {
      message.error('邮箱验证码 API 模式请填写 email----api 行')
      return
    }
    if (phoneSignupEnabled && !String(values.login_password || values.password || '').trim()) {
      message.error('手机号注册/已注册手机号登录必须填写同一个密码')
      return
    }


    const registerExtra = {
      mail_provider: values.mail_provider,
      email_api_lines: values.mail_provider === 'email_api' ? String(values.email_api_lines || '').trim() : undefined,
      email_api_poll_interval_seconds: values.email_api_poll_interval_seconds,
      email_api_request_timeout_seconds: values.email_api_request_timeout_seconds,
      email_api_gmail_dot_variant_enabled: values.email_api_gmail_dot_variant_enabled,
      email_api_gmail_variant_count: values.email_api_gmail_variant_count,
      email_api_gmail_variant_rules: values.email_api_gmail_variant_rules,
      email_api_gmail_plus_tag_template: values.email_api_gmail_plus_tag_template,
      email_api_default_scheme: values.email_api_default_scheme,
      email_api_use_all_identities: values.mail_provider === 'email_api' ? true : undefined,
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
      chatgpt_save_registration_access_token_account:
        platform === 'chatgpt'
          ? (values.chatgpt_save_registration_access_token_account === undefined
            ? true
            : Boolean(values.chatgpt_save_registration_access_token_account))
          : undefined,
      chatgpt_existing_account_login_route_enabled:
        platform === 'chatgpt'
          ? (values.chatgpt_existing_account_login_route_enabled === undefined
            ? true
            : Boolean(values.chatgpt_existing_account_login_route_enabled))
          : undefined,
      chatgpt_register_unique_exit_ip_policy:
        platform === 'chatgpt'
          ? normalizeRegisterUniqueExitPolicy(values.chatgpt_register_unique_exit_ip_policy)
          : undefined,
      chatgpt_register_otp_wait_seconds:
        platform === 'chatgpt' ? values.chatgpt_register_otp_wait_seconds : undefined,
      chatgpt_register_otp_resend_wait_seconds:
        platform === 'chatgpt' ? values.chatgpt_register_otp_resend_wait_seconds : undefined,
      chatgpt_register_otp_account_budget_seconds:
        platform === 'chatgpt' ? values.chatgpt_register_otp_account_budget_seconds : undefined,
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
      validateTaskProxySettings(values)
      const proxyPayload = buildTaskProxyPayload(values)
      const res = await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify({
          platform: values.platform,
          email: values.email || null,
          password: phoneSignupEnabled ? String(values.login_password || values.password || '').trim() : values.password || null,
          count: values.count,
          concurrency,
          ...delaySettings,
          ...proxyPayload,
          executor_type: executorType,
          browser_family: browserFamily,
          registration_zero_amount_eligibility_enabled:
            values.platform === 'chatgpt'
            && Boolean(values[REGISTRATION_ZERO_AMOUNT_ENABLED_FIELD]),
          registration_zero_amount_checkout_country: String(
            values[REGISTRATION_ZERO_AMOUNT_COUNTRY_FIELD]
              || DEFAULT_REGISTRATION_ZERO_AMOUNT_COUNTRY,
          ).trim().toUpperCase() || DEFAULT_REGISTRATION_ZERO_AMOUNT_COUNTRY,
          registration_paypal_link_enabled:
            values.platform === 'chatgpt'
            && Boolean(values[REGISTRATION_PAYPAL_LINK_ENABLED_FIELD]),
          registration_paypal_payment_enabled:
            values.platform === 'chatgpt'
            && Boolean(values[REGISTRATION_PAYPAL_PAYMENT_ENABLED_FIELD]),
          registration_diagnostics_mode: normalizeRegistrationDiagnosticsMode(
            values.registration_diagnostics_mode,
            executorType,
            values.platform,
          ),
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
  const dynamicProxyProvider = Form.useWatch('dynamic_proxy_provider', form) || 'cliproxy'
  const proxyFailover = Form.useWatch('proxy_failover', form)
  const registerCount = Number(Form.useWatch('count', form) || 1)
  const manualEmail = Form.useWatch('email', form)
  const chatgptRegistrationEntry = Form.useWatch('chatgpt_registration_entry', form)
  const phoneSignupUsePool = Form.useWatch('chatgpt_phone_signup_use_pool', form)
  const uniqueExitIpPolicy = Form.useWatch('chatgpt_register_unique_exit_ip_policy', form)
  const selectedExecutor = Form.useWatch('executor_type', form)
  const executorType = normalizeExecutorForPlatform(platform, selectedExecutor)
  const executorOptions = getExecutorOptions(platform)
  const browserFamilyOptions = getBrowserFamilyOptions(platform, executorType)
  const isManualEmailOtp = platform === 'chatgpt' && mailProvider === 'manual_email_otp'
  const isPhoneSignup = platform === 'chatgpt' && chatgptRegistrationEntry === 'phone_signup'
  const forceSerialRegistration = isManualEmailOtp || isPhoneSignup
  const concurrencyLimit = getRegisterConcurrencyLimit(platform, executorType, registerControlConfig)
  const uniqueExitIpEnabled = isRegisterUniqueExitEnabled(uniqueExitIpPolicy, proxyMode)
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
    const normalizedBrowserFamily = normalizeBrowserFamilyForExecutor(
      platform,
      normalizedExecutor,
      form.getFieldValue('browser_family'),
    )
    if (form.getFieldValue('browser_family') !== normalizedBrowserFamily) {
      form.setFieldValue('browser_family', normalizedBrowserFamily)
    }
  }, [executorType, form, platform])

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
    const currentConcurrency = form.getFieldValue('concurrency')
    const nextConcurrency = normalizeRegisterConcurrency(
      currentConcurrency,
      platform,
      executorType,
      forceSerialRegistration,
      registerControlConfig,
    )
    if (Number(currentConcurrency) !== nextConcurrency) {
      form.setFieldValue('concurrency', nextConcurrency)
    }
  }, [executorType, forceSerialRegistration, form, platform, registerControlConfig])

  useEffect(() => {
    if (!isManualEmailOtp) return
    const normalizedEmail = String(manualEmail || '').trim()
    if (!normalizedEmail) return
    window.localStorage.setItem('auto-chatgpt.manual_email_otp.email', normalizedEmail)
  }, [isManualEmailOtp, manualEmail])

  useEffect(() => () => stopPolling(), [])

  const existingAccountLoginRoutes = Array.isArray(task?.meta?.existing_account_login_routes)
    ? task.meta.existing_account_login_routes.filter((item: any) => item && typeof item === 'object')
    : []
  const existingAccountRoutedCount = existingAccountLoginRoutes.filter((item: any) => Boolean(item?.routed) && !item?.blocked).length
  const existingAccountBlockedCount = existingAccountLoginRoutes.filter((item: any) => Boolean(item?.blocked)).length
  const uniqueExitIpMeta = task?.meta?.register_unique_exit_ip && typeof task.meta.register_unique_exit_ip === 'object'
    ? task.meta.register_unique_exit_ip
    : null
  const uniqueExitIpAssignedCount = Number(uniqueExitIpMeta?.assigned_count || 0)
  const uniqueExitIpCollisionCount = Number(uniqueExitIpMeta?.collision_count || 0)

  return (
    <div style={{ maxWidth: 800 }}>
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ fontSize: 24, fontWeight: 'bold', margin: 0 }}>注册任务</h1>
        <p style={{ color: '#7a8ba3', marginTop: 4 }}>创建账号自动注册任务</p>
      </div>

      <Form form={form} layout="vertical" onFinish={submit} initialValues={{
        platform: 'chatgpt',
        executor_type: 'protocol',
        browser_family: 'random',
        registration_diagnostics_mode: 'off',
        captcha_solver: 'yescaptcha',
        mail_provider: 'luckmail',
        email_api_poll_interval_seconds: 3,
        email_api_request_timeout_seconds: 15,
        email_api_gmail_dot_variant_enabled: true,
        email_api_gmail_variant_count: 2,
        email_api_gmail_variant_rules: 'all',
        email_api_gmail_plus_tag_template: 'r{rand}',
        email_api_default_scheme: 'https',
        applemail_base_url: 'https://www.appleemail.top',
        applemail_pool_dir: 'mail',
        applemail_mailboxes: 'INBOX,Junk',
        gptmail_base_url: 'https://mail.chatgpt.org.uk',
        cloudmail_timeout: 30,
        tempmail_api_url: DEFAULT_TEMPMAIL_API_URL,
        tempmail_api_key_header: 'Authorization',
        tempmail_mode: 'fixed_domain',
        tempmail_wait_timeout_seconds: 180,
        tempmail_ttl_minutes: 30,
        tempmail_reuse_window_minutes: 20,
        tempmail_platform: 'chatgpt',
        tempmail_permanent: false,
        chatgpt_save_registration_access_token_account: true,
        chatgpt_registration_entry: 'email_signup',
        chatgpt_phone_signup_use_pool: false,
        chatgpt_phone_signup_timeout_seconds: 180,
        chatgpt_phone_signup_poll_interval_seconds: 5,
        chatgpt_phone_signup_max_resend_attempts: 1,
        chatgpt_phone_signup_resend_interval_seconds: 60,
        ...taskProxySettingsFromConfig({}),
        count: 1,
        concurrency: CHATGPT_REGISTER_DEFAULT_CONCURRENCY,
        register_delay_seconds: CHATGPT_REGISTER_DEFAULT_DELAY_SECONDS,
        register_delay_max_seconds: CHATGPT_REGISTER_DEFAULT_DELAY_MAX_SECONDS,
        chatgpt_register_unique_exit_ip_policy: 'auto',
        maliapi_base_url: 'https://maliapi.215.im/v1',
        maliapi_auto_domain_strategy: 'balanced',
        solver_url: 'http://localhost:8889',
      }}>
        <Form.Item name="platform" hidden>
          <Input />
        </Form.Item>
        <Card title="基本配置" style={{ marginBottom: 16 }}>
          <Form.Item label="平台">
            <Input value="ChatGPT" readOnly />
          </Form.Item>
          <Form.Item
            name="executor_type"
            label="注册执行器"
            rules={[{ required: true }]}
            extra={EXECUTOR_SELECTION_HELP}
          >
            <Select options={executorOptions} />
          </Form.Item>
          {platform === 'chatgpt' ? (
            <Form.Item
              name="browser_family"
              label="浏览器指纹族"
              extra={getBrowserFamilySelectionHelp(platform, executorType)}
              rules={[{ required: true, message: '请选择浏览器指纹族' }]}
            >
              <Select options={browserFamilyOptions} />
            </Form.Item>
          ) : null}
          {platform === 'chatgpt' && ['protocol', 'headless', 'headed'].includes(executorType) ? (
            <Form.Item name="registration_diagnostics_mode" label="注册诊断">
              <Segmented block options={[...REGISTRATION_DIAGNOSTICS_OPTIONS]} />
            </Form.Item>
          ) : null}
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
            <Form.Item
              name="concurrency"
              label="并发数"
              style={{ flex: 1 }}
              extra={forceSerialRegistration ? '当前注册方式固定串行。' : `当前执行器最多并发 ${concurrencyLimit}。`}
            >
              <Input type="number" min={1} max={concurrencyLimit} disabled={forceSerialRegistration} />
            </Form.Item>
          </Space>
          <Space style={{ width: '100%' }}>
            <Form.Item name="register_delay_seconds" label="最小注册延迟(秒)" style={{ flex: 1 }}>
              <InputNumber min={0} max={3600} precision={1} step={0.5} style={{ width: '100%' }} placeholder="0 = 不延迟" />
            </Form.Item>
            <Form.Item
              name="register_delay_max_seconds"
              label="最大延迟(秒)"
              dependencies={['register_delay_seconds']}
              rules={[
                ({ getFieldValue }) => ({
                  validator: (_, value) => {
                    const maximum = Number(value || 0)
                    const minimum = Number(getFieldValue('register_delay_seconds') || 0)
                    return maximum === 0 || maximum >= minimum
                      ? Promise.resolve()
                      : Promise.reject(new Error('最大延迟不能小于最小延迟；填 0 或与最小值相同表示固定延迟'))
                  },
                }),
              ]}
              style={{ flex: 1 }}
            >
              <InputNumber min={0} max={3600} precision={1} step={0.5} style={{ width: '100%' }} placeholder="0 或与最小值相同 = 固定延迟" />
            </Form.Item>
            <Form.Item name="proxy_mode" label="代理模式" style={{ flex: 1 }}>
              <Select
                options={[
                  { value: 'direct', label: '直连' },
                  { value: 'pool', label: '使用代理池' },
                  { value: 'specified', label: '指定代理' },
                  { value: 'dynamic', label: '动态代理' },
                ]}
              />
            </Form.Item>
          </Space>
          {proxyMode === 'dynamic' ? (
            <Form.Item name="dynamic_proxy_provider" label="动态代理渠道">
              <Segmented
                block
                options={[
                  { label: 'Cliproxy', value: 'cliproxy' },
                  { label: 'MiyaIP', value: 'miyaip' },
                ]}
              />
            </Form.Item>
          ) : null}
          {proxyMode === 'specified' || proxyMode === 'dynamic' ? (
            <Space style={{ width: '100%' }} align="start">
              {proxyMode === 'specified' || dynamicProxyProvider === 'cliproxy' ? (
                <Form.Item
                  name="proxy"
                  label={proxyMode === 'dynamic' ? 'Cliproxy 动态节点（本次任务可覆盖）' : '指定代理'}
                  style={{ flex: 1 }}
                  extra={proxyMode === 'dynamic' ? '留空沿用全局 Cliproxy 节点；填写后仅覆盖本次注册任务。' : undefined}
                >
                  <Input placeholder={proxyMode === 'dynamic' ? '可留空；或填 socks5://user-region-JP-sid-xxxx-t-15:pass@host:port' : 'http://user:pass@host:port'} />
                </Form.Item>
              ) : null}
              <Form.Item name="proxy_failover" valuePropName="checked" label="失败处理" style={{ width: 180 }}>
                <Checkbox>{proxyMode === 'dynamic' ? '失败后更换线路' : '失败后切换代理池'}</Checkbox>
              </Form.Item>
            </Space>
          ) : null}
          {proxyMode === 'pool' || proxyMode === 'dynamic' || (proxyMode === 'specified' && proxyFailover) ? (
            <Space style={{ width: '100%' }} align="start">
              <Form.Item
                name="proxy_country_code"
                label="注册出口国家"
                style={{ flex: 1 }}
                rules={proxyMode === 'dynamic' ? [{ required: true, message: '请选择动态代理出口国家' }] : undefined}
              >
                <RegistrationCountrySelect
                  allowClear={proxyMode !== 'dynamic'}
                  placeholder={proxyMode === 'dynamic' ? '选择注册出口国家' : '不限国家'}
                />
              </Form.Item>
              {proxyMode !== 'dynamic' ? (
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
          {platform === 'chatgpt' ? (
            <Form.Item
              name="chatgpt_register_unique_exit_ip_policy"
              label="独立出口策略"
              extra="自动仅在动态代理下启用；强制会要求当前代理模式能够轮换出口；关闭则不探测和锁定出口 IP。"
            >
              <Segmented
                block
                options={[
                  { label: '自动', value: 'auto' },
                  { label: '强制', value: 'required' },
                  { label: '关闭', value: 'off' },
                ]}
              />
            </Form.Item>
          ) : null}
          {platform === 'chatgpt' ? (
            <RegistrationEligibilityCountryField form={form} />
          ) : null}
          {platform === 'chatgpt' ? (
            <RegistrationPaypalPaymentField form={form} />
          ) : null}
          {uniqueExitIpEnabled && proxyMode === 'direct' ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message="直连无法提供独立出口 IP"
              description="该开关需要动态代理、代理池或多个可切换代理；直连模式下多个账号会共用服务器出口。"
            />
          ) : null}
          {uniqueExitIpEnabled && proxyMode === 'specified' && !proxyFailover && registerCount > 1 ? (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 12 }}
              message="单个指定代理不能支撑批量独立出口"
              description="注册数量大于 1 时，请开启失败切换、改用代理池/动态代理，或把注册数量降为 1；后端会按同样规则拒绝创建任务。"
            />
          ) : null}
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="代理模式说明"
            description="直连不使用代理；指定代理默认只用填写节点，勾选失败切换后才使用代理池筛选项；代理池按健康分、冷却和实测出口国家挑选；动态代理按所选渠道生成线路，必须填写出口国家，失败后只在当前渠道内更换线路。"
          />
          {platform === 'chatgpt' && (
            <>
              <Form.Item name="chatgpt_registration_entry" label="注册入口">
                <Select
                  options={[
                    { value: 'email_signup', label: '邮箱注册' },
                    { value: 'phone_signup', label: '手机号注册' },
                  ]}
                />
              </Form.Item>
              {isPhoneSignup ? (
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message="当前为手机号注册"
                  description="手机号会作为 ChatGPT 登录标识；接码输入格式沿用手机号绑定的“手机号----收码API”，并兼容“手机号|收码API”。当前只执行注册阶段，并保存注册阶段 AccessToken 账号。"
                />
              ) : null}
              <Form.Item label="ChatGPT 注册凭据">
                <ChatGPTRegistrationModeSwitch
                  mode={chatgptRegistrationMode}
                  onChange={setChatgptRegistrationMode}
                />
              </Form.Item>
              <Form.Item
                name="chatgpt_existing_account_login_route_enabled"
                valuePropName="checked"
                initialValue={true}
                extra="开启：注册状态机发现邮箱已存在或被 OpenAI 路由到登录时，继续登录恢复并保存；关闭：直接跳过该邮箱，不保存到库存，并写入任务日志。"
              >
                <Checkbox>遇到已注册邮箱时路由到登录</Checkbox>
              </Form.Item>
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
                      { value: 'email_api', label: '邮箱验证码 API（email----api）' },
                      { value: 'hme_ready_api', label: 'HME Ready API + TempMail' },
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
                    <div>流程是：先手填邮箱 → 开始任务 → 注册阶段需要邮箱验证码时，任务状态区会弹出输入框。</div>
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
          {mailProvider === 'email_api' && (
            <>
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 16 }}
                message="邮箱验证码 API（email----api）"
                description={
                  <div>
                    <div>每行格式：邮箱----验证码 API。API 可以省略 http/https，后端默认补 https://。</div>
                    <div>API 返回 JSON，status 字段为验证码；status=0、空或非 4-8 位数字会继续等待。</div>
                    <div>Gmail 每行按“总身份数”展开：原邮箱 + N-1 个随机变体；默认规则包含 dot、plus、dot+plus 和 googlemail。</div>
                    <div>同一个 Gmail 根邮箱仍共用同一个 API，并按 Gmail/API 串行发码，避免验证码串号。</div>
                  </div>
                }
              />
              <Form.Item
                name="email_api_lines"
                label="邮箱 API 行"
                rules={[{ required: true, message: '请至少填写一行 email----api' }]}
                extra="示例：sumi523red@gmail.com----smsbower.page/api/mail/getCodeBySignature?s=xxx"
              >
                <Input.TextArea rows={6} placeholder={'name@gmail.com----api.example.com/get?id=xxx\nuser@example.com----https://api.example.com/code?u=2'} style={{ fontFamily: 'monospace' }} />
              </Form.Item>
              <Space align="start" style={{ width: '100%' }}>
                <Form.Item name="email_api_poll_interval_seconds" label="轮询间隔秒" style={{ flex: 1 }}>
                  <InputNumber min={1} max={60} precision={0} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item name="email_api_request_timeout_seconds" label="请求超时秒" style={{ flex: 1 }}>
                  <InputNumber min={1} max={120} precision={0} style={{ width: '100%' }} />
                </Form.Item>
              </Space>
              <Form.Item name="email_api_gmail_dot_variant_enabled" valuePropName="checked">
                <Checkbox>启用 Gmail 随机变体</Checkbox>
              </Form.Item>
              <Space align="start" style={{ width: '100%' }}>
                <Form.Item
                  name="email_api_gmail_variant_count"
                  label="每个 Gmail 总身份数"
                  tooltip="包含原邮箱；例如 5 = 原邮箱 + 4 个随机变体。关闭 Gmail 随机变体时强制只用原邮箱。"
                  style={{ flex: 1 }}
                >
                  <InputNumber min={1} max={500} precision={0} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item
                  name="email_api_gmail_variant_rules"
                  label="Gmail 变体规则"
                  extra="默认 all；可填 dot,plus,dot_plus,googlemail 的逗号组合。"
                  style={{ flex: 1 }}
                >
                  <Input placeholder="all" />
                </Form.Item>
              </Space>
              <Form.Item
                name="email_api_gmail_plus_tag_template"
                label="Plus 标签模板"
                extra="用于 plus / dot_plus / googlemail_plus，支持 {rand}、{index}、{base}；默认 r{rand}。"
              >
                <Input placeholder="r{rand}" />
              </Form.Item>
            </>
          )}
          {platform === 'chatgpt' && !isPhoneSignup && (
            <>
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 16 }}
                message="单账号注册邮箱验证码等待"
                description="这里只限制当前账号在邮箱验证码阶段的累计等待，不限制整批任务总耗时；首轮未收到后才会触发一次 email-otp/resend 重发。"
              />
              <Space align="start" style={{ width: '100%' }}>
                <Form.Item name="chatgpt_register_otp_wait_seconds" label="首轮等待秒" initialValue={120} style={{ flex: 1 }}>
                  <InputNumber min={30} max={3600} precision={0} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item name="chatgpt_register_otp_resend_wait_seconds" label="补发后等待秒" initialValue={90} style={{ flex: 1 }}>
                  <InputNumber min={30} max={3600} precision={0} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item
                  name="chatgpt_register_otp_account_budget_seconds"
                  label="单账号总预算秒"
                  initialValue={210}
                  tooltip="累计预算从当前账号第一次进入邮箱验证码等待开始计时；耗尽后直接放弃当前账号，不继续消耗整批任务时间。"
                  style={{ flex: 1 }}
                >
                  <InputNumber min={30} max={7200} precision={0} style={{ width: '100%' }} />
                </Form.Item>
              </Space>
            </>
          )}
          {mailProvider === 'tempmail_local' && (
            <>
              <Form.Item name="tempmail_api_url" label="API URL" rules={[{ required: true, message: '请输入 TempMail API 地址' }]}>
                <Input placeholder={DEFAULT_TEMPMAIL_API_URL} />
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
                  手机号注册使用与手机号绑定一致的输入格式：每行 `手机号----收码API`，也兼容 `手机号|收码API`。若号码已注册，会使用同一个密码走手机号登录短信验证。
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
                    label="手机号 / 收码API"
                    rules={[
                      {
                        validator: (_, value) => {
                          if (!isPhoneSignup || phoneSignupUsePool) return Promise.resolve()
                          return String(value || '').trim()
                            ? Promise.resolve()
                            : Promise.reject(new Error('请粘贴 手机号----收码API / 手机号|收码API，或勾选使用手机号池'))
                        },
                      },
                    ]}
                    extra="示例：+573234567890----https://example.com/api/sms?id=xxx；或 +12082260171|https://sms24.uk/api/sms/recordText?token=xxx&tpl=1"
                  >
                    <Input.TextArea autoSize={{ minRows: 4, maxRows: 10 }} placeholder={'+573234567890----https://example.com/api/sms?id=xxx\n+12082260171|https://sms24.uk/api/sms/recordText?token=xxx&tpl=1'} />
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
          <RegistrationPipelineSummary
            success={Number(task?.success || 0)}
            zeroAmount={task?.meta?.registration_zero_amount_eligibility}
            paypal={task?.meta?.registration_paypal_payment}
            style={{ marginTop: 16 }}
          />
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
          {uniqueExitIpMeta?.enabled ? (
            <Alert
              style={{ marginTop: 16 }}
              type={uniqueExitIpCollisionCount > 0 ? 'warning' : 'info'}
              showIcon
              message={`独立出口 IP：已分配 ${uniqueExitIpAssignedCount} 个，撞 IP ${uniqueExitIpCollisionCount} 次`}
              description="开启后同一注册任务内已分配过的出口 IP 不再复用；撞 IP 时会在当前渠道内自动更换线路，候选不足会记录失败。"
            />
          ) : null}
          {existingAccountLoginRoutes.length > 0 ? (
            <Alert
              style={{ marginTop: 16 }}
              type={existingAccountBlockedCount > 0 ? 'warning' : 'info'}
              showIcon
              message={`已注册邮箱处理：登录恢复 ${existingAccountRoutedCount} 个，跳过 ${existingAccountBlockedCount} 个`}
              description={
                <Space direction="vertical" size={4} style={{ width: '100%' }}>
                  {existingAccountLoginRoutes.slice(-8).map((item: any, index: number) => (
                    <div key={`${item?.email || index}-${item?.detected_at || index}`}>
                      <Tag color={item?.blocked ? 'orange' : 'blue'}>
                        {item?.blocked ? '已跳过' : '已路由'}
                      </Tag>
                      <span>{String(item?.email || '-')}</span>
                      {item?.reason ? (
                        <Text type="secondary" style={{ marginLeft: 8 }}>
                          {String(item.reason).slice(0, 120)}
                        </Text>
                      ) : null}
                    </div>
                  ))}
                </Space>
              }
            />
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
