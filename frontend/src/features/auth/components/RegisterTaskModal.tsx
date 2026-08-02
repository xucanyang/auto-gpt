import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Checkbox,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Tag,
  message,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { ChatGPTRegistrationModeSwitch } from '@/components/ChatGPTRegistrationModeSwitch'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import { TaskVerificationPanel } from '@/components/TaskVerificationPanel'
import { PhoneBindingResultsTable } from '@/components/phone-binding/PhoneBindingResultsTable'
import { ApprovalUrlResultsTable } from '@/components/approval-url/ApprovalUrlResultsTable'
import {
  CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
  type ChatGPTRegistrationMode,
} from '@/lib/chatgptRegistrationMode'
import { EXECUTOR_SELECTION_HELP, getExecutorOptions } from '@/lib/platformExecutorOptions'
import { apiFetch } from '@/lib/utils'
import { normalizeDomainList } from '@/lib/domainList'


type TempMailDomainOption = {
  domain: string
  available?: boolean
  status?: string
  dns_status?: string
}

const MAIL_PROVIDER_LABELS: Record<string, string> = {
  luckmail: 'LuckMail',
  manual_email_otp: '手动邮箱 + 手输验证码',
  email_api: '邮箱验证码 API',
  hme_ready_api: 'HME Ready API + TempMail',
  tempmail_local: 'TempMail Ready API',
  tempmail_api: 'TempMail Ready API',
}

function mailProviderLabel(provider: string) {
  const raw = String(provider || '').trim().toLowerCase()
  const normalized = ['icloud_hme', 'icloud_hme_ready', 'icloud_hme_helper_ready', 'helper_ready_api'].includes(raw)
    ? 'hme_ready_api'
    : raw
  return MAIL_PROVIDER_LABELS[normalized] || normalized || '未配置'
}

type RegisterTaskModalProps = {
  open: boolean
  currentPlatform: string
  taskModalMode: 'register' | 'resume_auth' | 'payment_link' | 'pix_cleanup' | 'sub2api_upload' | 'oaipay_upload' | 'baxigpt_cdk' | 'paypal_bind' | 'probe_local_status'
  taskModalAccount: any
  taskId: string | null
  taskSnapshot: any
  registerForm: any
  registerMailProvider: string
  chatgptRegistrationMode: ChatGPTRegistrationMode
  setChatgptRegistrationMode: (mode: ChatGPTRegistrationMode) => void
  registerLoading: boolean
  registerSettingsSaving: boolean
  onClose: () => void
  onSaveRegisterSettings: () => Promise<void> | void
  onRegister: () => Promise<void> | void
  onTaskDone: () => void
}

export function RegisterTaskModal({
  open,
  currentPlatform,
  taskModalMode,
  taskModalAccount,
  taskId,
  taskSnapshot,
  registerForm,
  registerMailProvider,
  chatgptRegistrationMode,
  setChatgptRegistrationMode,
  registerLoading,
  registerSettingsSaving,
  onClose,
  onSaveRegisterSettings,
  onRegister,
  onTaskDone,
}: RegisterTaskModalProps) {
  const registerProviderOverride = Form.useWatch('mail_provider_override', registerForm)
  const chatgptRegistrationEntry = Form.useWatch('chatgpt_registration_entry', registerForm)
  const phoneSignupUsePool = Form.useWatch('chatgpt_phone_signup_use_pool', registerForm)
  const selectedTempMailDomains = Form.useWatch('tempmail_fixed_domains', registerForm) || []
  const selectedTempMailMode = String(Form.useWatch('tempmail_mode', registerForm) || '').trim().toLowerCase()
  const proxyMode = Form.useWatch('proxy_mode', registerForm)
  const proxyFailover = Form.useWatch('proxy_failover', registerForm)
  const uniqueExitIpEnabled = Form.useWatch('chatgpt_register_unique_exit_ip_enabled', registerForm)
  const registerCount = Number(Form.useWatch('count', registerForm) || 1)
  const [tempmailDomains, setTempmailDomains] = useState<TempMailDomainOption[]>([])
  const [tempmailDomainsLoading, setTempmailDomainsLoading] = useState(false)
  const isPhoneSignup = currentPlatform === 'chatgpt' && chatgptRegistrationEntry === 'phone_signup'
  const rawEffectiveRegisterMailProvider =
    currentPlatform === 'chatgpt' && !isPhoneSignup && registerProviderOverride && registerProviderOverride !== '__global__'
      ? registerProviderOverride
      : registerMailProvider
  const effectiveRegisterMailProvider = ['icloud_hme', 'icloud_hme_ready', 'icloud_hme_helper_ready', 'helper_ready_api'].includes(
    String(rawEffectiveRegisterMailProvider || '').trim().toLowerCase(),
  )
    ? 'hme_ready_api'
    : rawEffectiveRegisterMailProvider
  const effectiveTempMailProvider = effectiveRegisterMailProvider === 'tempmail_local' || effectiveRegisterMailProvider === 'tempmail_api'
  const tempmailRequiresFixedDomain = effectiveTempMailProvider && selectedTempMailMode === 'fixed_domain'
  const tempmailUsesTaskSubdomain = effectiveTempMailProvider && selectedTempMailMode === 'task_subdomain'
  const normalizedSelectedTempMailDomains = normalizeDomainList(selectedTempMailDomains)
  const tempmailDomainOptions = useMemo(() => {
    const byDomain = new Map<string, TempMailDomainOption>()
    tempmailDomains.forEach((item) => {
      const domain = String(item?.domain || '').trim().toLowerCase()
      if (domain) byDomain.set(domain, item)
    })
    normalizedSelectedTempMailDomains.forEach((domain) => {
      if (!byDomain.has(domain)) byDomain.set(domain, { domain, available: true })
    })
    return Array.from(byDomain.values()).map((item) => ({
      label: item.dns_status ? `${item.domain} · ${item.dns_status}` : item.domain,
      value: item.domain,
      disabled: item.available === false,
    }))
  }, [normalizedSelectedTempMailDomains, tempmailDomains])

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
    if (!open || !tempmailRequiresFixedDomain) return
    void loadTempMailDomains(true)
  }, [open, tempmailRequiresFixedDomain])

  useEffect(() => {
    if (!open || !isPhoneSignup) return
    registerForm.setFieldsValue({ concurrency: 1 })
  }, [open, isPhoneSignup, registerForm])

  const isPhoneBindingTest = String(taskSnapshot?.source || '').trim() === 'phone_binding_test'
  const boundPhoneLines = Array.isArray(taskSnapshot?.meta?.bound_phone_lines) ? taskSnapshot.meta.bound_phone_lines : []
  const boundPhoneResults = Array.isArray(taskSnapshot?.meta?.bound_phone_results) ? taskSnapshot.meta.bound_phone_results : []
  const registeredPhoneLines = Array.isArray(taskSnapshot?.meta?.registered_phone_lines) ? taskSnapshot.meta.registered_phone_lines : []
  const phoneResults = Array.isArray(taskSnapshot?.meta?.runtime_results) ? taskSnapshot.meta.runtime_results : []
  const existingAccountLoginRoutes = Array.isArray(taskSnapshot?.meta?.existing_account_login_routes)
    ? taskSnapshot.meta.existing_account_login_routes.filter((item: any) => item && typeof item === 'object')
    : []
  const existingAccountRoutedCount = existingAccountLoginRoutes.filter((item: any) => Boolean(item?.routed) && !item?.blocked).length
  const existingAccountBlockedCount = existingAccountLoginRoutes.filter((item: any) => Boolean(item?.blocked)).length
  const uniqueExitIpMeta = taskSnapshot?.meta?.register_unique_exit_ip && typeof taskSnapshot.meta.register_unique_exit_ip === 'object'
    ? taskSnapshot.meta.register_unique_exit_ip
    : null
  const uniqueExitIpAssignedCount = Number(uniqueExitIpMeta?.assigned_count || 0)
  const uniqueExitIpCollisionCount = Number(uniqueExitIpMeta?.collision_count || 0)
  const registeredPhoneSuccessCount = registeredPhoneLines.length
    || phoneResults.filter((item: any) => String(item?.status || '') === 'registered_phone_signup').length
  const taskSource = String(taskSnapshot?.source || taskSnapshot?.meta?.source || '').trim()
  const isPhoneSignupTask = Boolean(taskSnapshot?.meta?.phone_signup?.enabled) || registeredPhoneSuccessCount > 0
  const isOaiPayApprovalTask = taskSource === 'chatgpt_oaipay_approval'
  const prefixSample = taskSnapshot?.meta?.prefix_sample && typeof taskSnapshot.meta.prefix_sample === 'object'
    ? taskSnapshot.meta.prefix_sample
    : {}
  const prefixSummary = taskSnapshot?.meta?.prefix_summary && typeof taskSnapshot.meta.prefix_summary === 'object'
    ? taskSnapshot.meta.prefix_summary
    : {}
  const isPrefixSample = Boolean(prefixSample?.enabled)
  const modalTitle = () => {
    if (taskModalMode === 'pix_cleanup') {
      const cleanupLabel = String(taskSnapshot?.meta?.cleanup_label || '').trim()
      const paymentType = String(taskSnapshot?.meta?.payment_type || 'pix').trim().toLowerCase()
      const paymentLabel = ({
        hosted: 'Hosted Checkout',
        paypal: 'PayPal',
        ideal: 'iDEAL',
        upi: 'UPI',
        pix: 'PIX',
        twint: 'TWINT',
        kakao_pay: 'Kakao Pay',
        team: 'ChatGPT Team',
        other: '其他支付链接',
      } as Record<string, string>)[paymentType] || paymentType.toUpperCase()
      return cleanupLabel ? `${cleanupLabel} ${paymentLabel} 链接删除` : `${paymentLabel} 链接删除`
    }
    if (taskModalMode === 'probe_local_status') {
      const eligible = Number(taskSnapshot?.meta?.eligible || 0)
      return eligible > 0 ? `批量同步本地状态 (${eligible} 个)` : '批量同步本地状态'
    }
    if (taskModalMode === 'sub2api_upload') {
      const eligible = Number(taskSnapshot?.meta?.eligible || 0)
      return eligible > 0 ? `Sub2API 批量上传 (${eligible} 个)` : 'Sub2API 批量上传'
    }
    if (taskModalMode === 'oaipay_upload') {
      const eligible = Number(taskSnapshot?.meta?.eligible || 0)
      return eligible > 0 ? `OAIPay 批量上传 (${eligible} 个)` : 'OAIPay 批量上传'
    }
    if (taskModalMode === 'baxigpt_cdk') {
      const count = Number(taskSnapshot?.meta?.pair_count || 0)
      const channel = String(taskSnapshot?.meta?.payment_channel || '').trim().toLowerCase() === 'pix' ? 'PIX' : 'iDEAL'
      const isSavedPixLinkUpload = channel === 'PIX'
        && String(taskSnapshot?.meta?.pix_submit_mode || '').trim().toLowerCase() === 'user_link'
      const label = isSavedPixLinkUpload ? 'PIX 链接上传' : `${channel} 批量提交`
      return count > 0 ? `${label} (${count} 个)` : label
    }
    if (taskModalMode === 'paypal_bind') {
      const count = Number(taskSnapshot?.meta?.eligible_accounts || taskSnapshot?.meta?.eligible || 0)
      if (isOaiPayApprovalTask) return count > 0 ? `OaiPay授权链接 (${count} 个)` : 'OaiPay授权链接'
      return count > 0 ? `PayPal绑定 (${count} 个)` : 'PayPal绑定'
    }
    if (taskModalMode === 'payment_link') {
      const eligible = Number(taskSnapshot?.meta?.eligible || 0)
      return eligible > 0 ? `支付链接生成 (${eligible} 个)` : '支付链接生成'
    }
    if (taskModalMode === 'resume_auth') {
      if (isPhoneBindingTest) {
        const count = Number(taskSnapshot?.meta?.phone_count || 0)
        return count > 0 ? `手机号绑定 (${count} 个)` : '手机号绑定'
      }
      return taskModalAccount?.email
        ? `补抓Auth ${taskModalAccount.email}`
        : Number(taskSnapshot?.meta?.eligible || 0) > 0
          ? `批量补抓Auth (${taskSnapshot?.meta?.eligible} 个)`
          : '补抓Auth'
    }
    if (taskId && isPhoneSignupTask) {
      const count = registeredPhoneSuccessCount
      return count > 0 ? `手机号注册 (${count} 个)` : '手机号注册'
    }
    return `注册 ${currentPlatform}`
  }

  const handleSaveRegisterSettings = async () => {
    try {
      await onSaveRegisterSettings()
    } catch (error: any) {
      message.error(error?.message || '保存注册设置失败')
    }
  }

  const handleRegister = async () => {
    try {
      await onRegister()
    } catch (error: any) {
      message.error(error?.message || '创建注册任务失败')
    }
  }

  return (
    <Modal
      title={modalTitle()}
      open={open}
      onCancel={onClose}
      footer={null}
      width={taskId && (isPhoneBindingTest || isPhoneSignupTask) ? 980 : taskId ? 760 : currentPlatform === 'chatgpt' ? 620 : 500}
      maskClosable={false}
    >
      {!taskId ? (
        <Form form={registerForm} layout="vertical" onFinish={handleRegister}>
          <Form.Item name="tempmail_mode" hidden>
            <Input />
          </Form.Item>
          <Form.Item name="tempmail_primary_domain" hidden>
            <Input />
          </Form.Item>
          {currentPlatform === 'chatgpt' ? (
            <Form.Item name="chatgpt_registration_entry" label="注册入口" initialValue="email_signup">
              <Select
                options={[
                  { value: 'email_signup', label: '邮箱注册' },
                  { value: 'phone_signup', label: '手机号注册' },
                ]}
              />
            </Form.Item>
          ) : null}
          <Form.Item
            name="executor_type"
            label="注册执行器"
            initialValue="protocol"
            rules={[{ required: true, message: '请选择注册执行器' }]}
            extra={EXECUTOR_SELECTION_HELP}
          >
            <Select options={getExecutorOptions(currentPlatform)} />
          </Form.Item>
          {currentPlatform === 'chatgpt' && isPhoneSignup ? (
            <>
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 16 }}
                message="当前注册将使用手机号注册"
                description="手机号和验证码读取格式与手机号绑定一致。若号码已注册，会使用同一个密码走手机号登录短信验证；成功后保存 AccessToken-only。"
              />
              <Form.Item
                name="login_password"
                label="手机号注册/登录密码"
                rules={[{ required: true, message: '请输入手机号注册/登录密码' }]}
                extra="新手机号注册时用这个密码创建账号；遇到已注册手机号时，也用这个密码登录续跑。"
              >
                <Input.Password placeholder="新注册和已注册登录共用同一个密码" autoComplete="new-password" />
              </Form.Item>
              <Form.Item name="chatgpt_phone_signup_use_pool" valuePropName="checked" initialValue={false}>
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
                          : Promise.reject(new Error('请输入手机号----收码API / 手机号|收码API，或勾选使用手机号池'))
                      },
                    },
                  ]}
                  extra="一行一个，格式与手机号绑定一致：手机号----收码API；也兼容 手机号|收码API。注册数量大于 1 时会逐行使用。"
                >
                  <Input.TextArea
                    rows={5}
                    placeholder={'+573234567890----https://example.com/sms?id=xxx\n+12082260171|https://sms24.uk/api/sms/recordText?token=xxx&tpl=1'}
                  />
                </Form.Item>
              ) : null}
              <Space style={{ width: '100%' }} align="start">
                <Form.Item name="chatgpt_phone_signup_timeout_seconds" label="短信等待秒数" initialValue={180} style={{ flex: 1 }}>
                  <InputNumber min={30} max={1800} precision={0} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item name="chatgpt_phone_signup_poll_interval_seconds" label="轮询间隔秒数" initialValue={5} style={{ flex: 1 }}>
                  <InputNumber min={1} max={60} precision={0} style={{ width: '100%' }} />
                </Form.Item>
              </Space>
              <Space style={{ width: '100%' }} align="start">
                <Form.Item name="chatgpt_phone_signup_max_resend_attempts" label="重发次数" initialValue={1} style={{ flex: 1 }}>
                  <InputNumber min={1} max={5} precision={0} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item name="chatgpt_phone_signup_resend_interval_seconds" label="重发后等待秒数" initialValue={60} style={{ flex: 1 }}>
                  <InputNumber min={10} max={600} precision={0} style={{ width: '100%' }} />
                </Form.Item>
              </Space>
            </>
          ) : null}
          {currentPlatform === 'chatgpt' && !isPhoneSignup ? (
            <Form.Item
              name="mail_provider_override"
              label="邮箱服务（本任务默认）"
              initialValue="__global__"
              extra="选择“跟随全局默认”以使用设置页配置；点击“保存设置”后，本浏览器的注册面板会记住这里的选择，不会改写全局配置。"
            >
              <Select
                options={[
                  {
                    value: '__global__',
                    label: `跟随全局默认（当前：${mailProviderLabel(registerMailProvider)}）`,
                  },
                  { value: 'hme_ready_api', label: 'HME Ready API + TempMail' },
                  { value: 'tempmail_local', label: 'TempMail Ready API' },
                  { value: 'email_api', label: '邮箱验证码 API（email----api）' },
                  { value: 'manual_email_otp', label: '手动邮箱 + 手输验证码' },
                ]}
              />
            </Form.Item>
          ) : null}
          {currentPlatform === 'chatgpt' && !isPhoneSignup && effectiveRegisterMailProvider === 'hme_ready_api' ? (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message="当前注册将使用 HME Ready API + TempMail"
              description="Helper 负责 HME 出池、身份和 finalize；auto-gpt 直接从 TempMail 转发箱读取验证码。"
            />
          ) : null}
          {currentPlatform === 'chatgpt' && !isPhoneSignup && effectiveTempMailProvider ? (
            <>
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 16 }}
                message="当前注册将使用 TempMail Ready API"
                description={tempmailRequiresFixedDomain
                  ? '固定域名建箱。可单选或多选域名；多选时每个新邮箱会从候选域名中随机选择一个。'
                  : tempmailUsesTaskSubdomain
                    ? '随机子域 Ready 建箱。每个任务由 TempMail API 分配子域邮箱，不需要选择固定域名。'
                    : '正在读取 TempMail 建箱配置...'}
              />
              {tempmailRequiresFixedDomain ? (
                <Space align="start" style={{ width: '100%' }}>
                  <Form.Item
                    name="tempmail_fixed_domains"
                    label="TempMail 可用域名"
                    style={{ flex: 1 }}
                    rules={[
                      {
                        validator: (_, value) => (
                          normalizeDomainList(value).length > 0
                            ? Promise.resolve()
                            : Promise.reject(new Error('请选择至少一个 TempMail 可用域名'))
                        ),
                      },
                    ]}
                    extra="固定域名模式必选。选择多个域名时，注册任务会自动分散使用。"
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
              ) : null}
            </>
          ) : null}
          {currentPlatform === 'chatgpt' && !isPhoneSignup && effectiveRegisterMailProvider === 'manual_email_otp' ? (
            <>
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 16 }}
                message="当前注册将使用手动邮箱模式"
                description="请先填写你的邮箱地址。注册任务只执行 signup 并保存 AccessToken/Web Session/Cookie；已有账号的 Auth 补抓请从账号页单独发起。真正需要验证码时，弹窗会切到任务日志面板，再出现验证码输入卡片。"
              />
              <Form.Item
                name="email"
                label="手填邮箱地址"
                rules={[{ required: true, message: '请输入邮箱地址' }]}
                extra="会自动记住你上次填写的邮箱。"
              >
                <Input placeholder="name@gmail.com" autoComplete="email" />
              </Form.Item>
              {chatgptRegistrationMode === CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN ? (
                <>
                  <Form.Item
                    name="chatgpt_existing_account_capture"
                    valuePropName="checked"
                    initialValue={false}
                    extra="开启后：跳过注册状态机，直接登录已有账号抓取认证信息。关闭则保持原始手动注册新号逻辑。"
                  >
                    <Checkbox>已有账号抓 auth</Checkbox>
                  </Form.Item>
                  <Form.Item
                    noStyle
                    shouldUpdate={(prev, next) => prev.chatgpt_existing_account_capture !== next.chatgpt_existing_account_capture}
                  >
                    {({ getFieldValue }) =>
                      getFieldValue('chatgpt_existing_account_capture') ? (
                        <Form.Item
                          name="login_password"
                          label="登录密码"
                          extra="优先尝试密码登录；若流程仍要求邮箱 OTP，任务面板会继续等待你手动输入验证码。默认值来自设置页，可临时覆盖。"
                        >
                          <Input.Password placeholder="留空则优先走邮箱 OTP" autoComplete="current-password" />
                        </Form.Item>
                      ) : null
                    }
                  </Form.Item>
                </>
              ) : null}
            </>
          ) : null}
          {currentPlatform === 'chatgpt' && !isPhoneSignup && effectiveRegisterMailProvider === 'email_api' ? (
            <>
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 16 }}
                message="当前注册将使用邮箱验证码 API"
                description="每行 email----api；API 返回 JSON，status 字段为验证码。Gmail 每行按“原邮箱 + N-1 个随机变体”展开，默认规则包含 dot、plus、dot+plus 和 googlemail，并按 Gmail/API 串行发码。"
              />
              <Form.Item
                name="email_api_lines"
                label="邮箱 API 行"
                rules={[{ required: true, message: '请至少填写一行 email----api' }]}
                extra="API 可省略协议，后端会自动补 https://。"
              >
                <Input.TextArea rows={6} placeholder={'name@gmail.com----api.example.com/get?id=xxx\nuser@example.com----https://api.example.com/code?u=2'} style={{ fontFamily: 'monospace' }} />
              </Form.Item>
              <Form.Item name="email_api_gmail_dot_variant_enabled" valuePropName="checked">
                <Checkbox>启用 Gmail 随机变体</Checkbox>
              </Form.Item>
              <Space align="start" style={{ width: '100%' }}>
                <Form.Item name="email_api_gmail_variant_count" label="每个 Gmail 总身份数" style={{ flex: 1 }}>
                  <InputNumber min={1} max={500} precision={0} style={{ width: '100%' }} />
                </Form.Item>
                <Form.Item
                  name="email_api_gmail_variant_rules"
                  label="Gmail 变体规则"
                  extra="默认 all；可填 dot,plus,dot_plus,googlemail。"
                  style={{ flex: 1 }}
                >
                  <Input placeholder="all" />
                </Form.Item>
              </Space>
              <Form.Item
                name="email_api_gmail_plus_tag_template"
                label="Plus 标签模板"
                extra="支持 {rand}、{index}、{base}；默认 r{rand}。"
              >
                <Input placeholder="r{rand}" />
              </Form.Item>
            </>
          ) : null}
          {currentPlatform === 'chatgpt' && !isPhoneSignup ? (
            <>
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 16 }}
                message="单账号注册邮箱验证码等待"
                description="只限制当前账号在邮箱验证码阶段的累计等待，不限制整批任务总耗时；首轮未收到才触发一次 email-otp/send 补发。"
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
                  tooltip="累计预算从当前账号第一次进入邮箱验证码等待开始计时；耗尽后直接放弃当前账号。"
                  style={{ flex: 1 }}
                >
                  <InputNumber min={30} max={7200} precision={0} style={{ width: '100%' }} />
                </Form.Item>
              </Space>
            </>
          ) : null}
          <Form.Item name="count" label="注册数量" initialValue={1} rules={[{ required: true }]}>
            <Input type="number" min={1} disabled={!isPhoneSignup && currentPlatform === 'chatgpt' && effectiveRegisterMailProvider === 'manual_email_otp'} />
          </Form.Item>
          <Form.Item name="concurrency" label="并发数" initialValue={1} rules={[{ required: true }]}>
            <Input type="number" min={1} max={5} disabled={isPhoneSignup || (currentPlatform === 'chatgpt' && ['manual_email_otp'].includes(String(effectiveRegisterMailProvider || '')))} />
          </Form.Item>
          <Space align="start" style={{ width: '100%' }}>
            <Form.Item name="register_delay_seconds" label="最小注册延迟(秒)" initialValue={0} style={{ flex: 1 }}>
              <InputNumber min={0} precision={1} step={0.5} style={{ width: '100%' }} placeholder="0 = 不延迟" />
            </Form.Item>
            <Form.Item name="register_delay_max_seconds" label="最大注册延迟(秒)" initialValue={0} style={{ flex: 1 }}>
              <InputNumber min={0} precision={1} step={0.5} style={{ width: '100%' }} placeholder="0 = 固定延迟" />
            </Form.Item>
          </Space>
          <Form.Item name="proxy_mode" label="代理模式" initialValue="dynamic">
            <Select
              options={[
                { value: 'direct', label: '直连' },
                { value: 'pool', label: '使用代理池' },
                { value: 'specified', label: '指定代理' },
                { value: 'dynamic', label: '动态代理' },
              ]}
            />
          </Form.Item>
          {proxyMode === 'specified' || proxyMode === 'dynamic' ? (
            <Space style={{ width: '100%' }} align="start">
              <Form.Item
                name="proxy"
                label={proxyMode === 'dynamic' ? '动态节点（本次任务可覆盖）' : '指定代理'}
                style={{ flex: 1 }}
                extra={proxyMode === 'dynamic' ? '留空沿用全局动态节点；填写后仅覆盖本次注册任务。全局配置请到代理管理页保存。' : undefined}
              >
                <Input placeholder={proxyMode === 'dynamic' ? '可留空；或填 socks5://user-region-JP-sid-xxxx-t-15:pass@host:port' : 'http://user:pass@host:port'} />
              </Form.Item>
              <Form.Item name="proxy_failover" label="失败处理" valuePropName="checked" style={{ width: 180 }} initialValue={false}>
                <Checkbox>{proxyMode === 'dynamic' ? '失败后刷新 sid 重试' : '失败后切换代理池'}</Checkbox>
              </Form.Item>
            </Space>
          ) : null}
          {proxyMode === 'pool' || proxyMode === 'dynamic' || (proxyMode === 'specified' && proxyFailover) ? (
            <Space style={{ width: '100%' }} align="start">
              <Form.Item
                name="proxy_country_code"
                label="出口国家"
                style={{ flex: 1 }}
                rules={proxyMode === 'dynamic' ? [{ required: true, message: '请输入动态代理出口国家' }] : undefined}
              >
                <Input placeholder={proxyMode === 'dynamic' ? '必填，例如 US / JP / SG' : '不限，或填 US / JP / SG'} maxLength={2} />
              </Form.Item>
              {proxyMode !== 'dynamic' ? (
                <>
              <Form.Item name="proxy_min_score" label="最低健康分" initialValue={50} style={{ width: 150 }}>
                <InputNumber min={0} max={100} precision={0} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="proxy_max_candidates" label="最多候选" initialValue={5} style={{ width: 150 }}>
                <InputNumber min={1} max={100} precision={0} style={{ width: '100%' }} />
              </Form.Item>
                </>
              ) : null}
            </Space>
          ) : null}
          {currentPlatform === 'chatgpt' ? (
            <Form.Item
              name="chatgpt_register_unique_exit_ip_enabled"
              valuePropName="checked"
              initialValue={false}
              extra="开启后每个注册尝试会先探测真实出口 IP；本任务内已分配过的 IP 不再复用。动态代理会扩大 sid 刷新候选，代理池会换候选；不足时当前尝试会失败/补尝试，速度会明显变慢。"
            >
              <Checkbox>注册任务内强制独立出口 IP</Checkbox>
            </Form.Item>
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
            description="直连不使用代理；指定代理默认只用填写节点，勾选失败切换后才使用代理池筛选项；代理池按健康分、冷却和实测出口国家挑选；动态代理默认使用全局动态节点，填写节点只覆盖本次注册，必须填写出口国家，失败后刷新 sid 重试。"
          />
          {currentPlatform === 'chatgpt' && isPhoneSignup ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message="手机号注册固定保存 AccessToken-only"
              description="这条链路只负责手机号注册阶段，不进入邮箱注册、full-auth 或 refresh_token 补抓。"
            />
          ) : currentPlatform === 'chatgpt' ? (
            <>
              <Form.Item label="ChatGPT 注册凭据">
                <ChatGPTRegistrationModeSwitch
                  mode={chatgptRegistrationMode}
                  onChange={setChatgptRegistrationMode}
                />
              </Form.Item>
              {chatgptRegistrationMode === CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN ? (
                <Form.Item
                  name="chatgpt_save_registration_access_token_account"
                  valuePropName="checked"
                  initialValue={true}
                  extra="注册阶段只保存 signup 产生的 AccessToken、Web Session 和 Cookie；完整 Auth/refresh_token 请使用账号页的独立补抓 Auth 任务。"
                >
                  <Checkbox>保存注册阶段 AccessToken 账号</Checkbox>
                </Form.Item>
              ) : null}
              <Form.Item
                name="chatgpt_existing_account_login_route_enabled"
                valuePropName="checked"
                initialValue={true}
                extra="开启：注册状态机发现邮箱已存在或被 OpenAI 路由到登录时，继续登录恢复并保存；关闭：直接跳过该邮箱，不保存到库存，并写入任务日志。"
              >
                <Checkbox>遇到已注册邮箱时路由到登录</Checkbox>
              </Form.Item>
            </>
          ) : null}
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
          {isPhoneBindingTest ? (
            <Alert
              type={boundPhoneLines.length > 0 ? 'success' : 'info'}
              showIcon
              message={isPrefixSample
                ? `号段抽样：已测试 ${Number(prefixSummary?.tested_phone_count || 0)} / ${Number(prefixSummary?.selected_phone_count || 0)} 个号码`
                : `已成功绑定 ${boundPhoneLines.length} 次`}
              description={
                <Space direction="vertical" style={{ width: '100%' }} size={8}>
                  <PhoneBindingResultsTable
                    results={phoneResults}
                    prefixSummary={prefixSummary}
                    showPrefixSummary={isPrefixSample}
                    boundPhoneLines={boundPhoneLines}
                    boundPhoneResults={boundPhoneResults}
                    showSuccessfulLines
                  />
                </Space>
              }
            />
          ) : null}
          {isPhoneSignupTask ? (
            <Alert
              type={registeredPhoneSuccessCount > 0 ? 'success' : 'info'}
              showIcon
              message={`手机号注册结果：已成功 ${registeredPhoneSuccessCount} 个`}
              description={
                <Space direction="vertical" style={{ width: '100%' }} size={8}>
                  <PhoneBindingResultsTable
                    results={phoneResults}
                    prefixSummary={prefixSummary}
                    showPrefixSummary={isPrefixSample || Array.isArray(prefixSummary?.items)}
                    boundPhoneLines={registeredPhoneLines}
                    showSuccessfulLines
                    emptyText="任务结束后，这里会输出已完成手机号注册的手机号。"
                  />
                </Space>
              }
            />
          ) : null}
          {isOaiPayApprovalTask ? (
            <Alert
              type={phoneResults.some((item: any) => String(item?.status || '') === 'success') ? 'success' : 'info'}
              showIcon
              message="approvalUrl 提取结果"
              description={<ApprovalUrlResultsTable results={phoneResults} />}
            />
          ) : null}
          {uniqueExitIpMeta?.enabled ? (
            <Alert
              type={uniqueExitIpCollisionCount > 0 ? 'warning' : 'info'}
              showIcon
              message={`独立出口 IP：已分配 ${uniqueExitIpAssignedCount} 个，撞 IP ${uniqueExitIpCollisionCount} 次`}
              description="开启后同一注册任务内已分配过的出口 IP 不再复用；撞 IP 时会自动换候选/刷新 sid，候选不足会记录失败。"
            />
          ) : null}
          {existingAccountLoginRoutes.length > 0 ? (
            <Alert
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
                        <span style={{ color: '#6b7280', marginLeft: 8 }}>
                          {String(item.reason).slice(0, 120)}
                        </span>
                      ) : null}
                    </div>
                  ))}
                </Space>
              }
            />
          ) : null}
          {taskSnapshot?.pending_verification ? (
            <TaskVerificationPanel
              taskId={String(taskId)}
              verification={taskSnapshot.pending_verification}
            />
          ) : null}
          <TaskLogPanel
            taskId={String(taskId)}
            onDone={onTaskDone}
            showTaskControls={taskModalMode !== 'pix_cleanup'}
          />
        </Space>
      )}
    </Modal>
  )
}
