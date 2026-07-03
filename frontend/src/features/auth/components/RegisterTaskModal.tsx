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
  hme_ready_api: 'HME Ready API',
  icloud_hme: 'iCloud HME',
  tempmail_local: 'TempMail Ready API',
  tempmail_api: 'TempMail Ready API',
}

function mailProviderLabel(provider: string) {
  const normalized = String(provider || '').trim()
  return MAIL_PROVIDER_LABELS[normalized] || normalized || '未配置'
}

type RegisterTaskModalProps = {
  open: boolean
  currentPlatform: string
  taskModalMode: 'register' | 'resume_auth' | 'payment_link' | 'sub2api_upload' | 'oaipay_upload' | 'baxigpt_cdk' | 'paypal_bind' | 'probe_local_status'
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
  const proxyMode = Form.useWatch('proxy_mode', registerForm)
  const proxyFailover = Form.useWatch('proxy_failover', registerForm)
  const [tempmailDomains, setTempmailDomains] = useState<TempMailDomainOption[]>([])
  const [tempmailDomainsLoading, setTempmailDomainsLoading] = useState(false)
  const isPhoneSignup = currentPlatform === 'chatgpt' && chatgptRegistrationEntry === 'phone_signup'
  const effectiveRegisterMailProvider =
    currentPlatform === 'chatgpt' && !isPhoneSignup && registerProviderOverride && registerProviderOverride !== '__global__'
      ? registerProviderOverride
      : registerMailProvider
  const effectiveTempMailProvider = effectiveRegisterMailProvider === 'tempmail_local' || effectiveRegisterMailProvider === 'tempmail_api'
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
    if (!open || !effectiveTempMailProvider) return
    void loadTempMailDomains(true)
  }, [open, effectiveTempMailProvider])

  useEffect(() => {
    if (!open || !isPhoneSignup) return
    registerForm.setFieldsValue({ concurrency: 1 })
  }, [open, isPhoneSignup, registerForm])

  const isPhoneBindingTest = String(taskSnapshot?.source || '').trim() === 'phone_binding_test'
  const boundPhoneLines = Array.isArray(taskSnapshot?.meta?.bound_phone_lines) ? taskSnapshot.meta.bound_phone_lines : []
  const boundPhoneResults = Array.isArray(taskSnapshot?.meta?.bound_phone_results) ? taskSnapshot.meta.bound_phone_results : []
  const registeredPhoneLines = Array.isArray(taskSnapshot?.meta?.registered_phone_lines) ? taskSnapshot.meta.registered_phone_lines : []
  const phoneResults = Array.isArray(taskSnapshot?.meta?.runtime_results) ? taskSnapshot.meta.runtime_results : []
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
      return count > 0 ? `idea批量提交 (${count} 个)` : 'idea批量提交'
    }
    if (taskModalMode === 'paypal_bind') {
      const count = Number(taskSnapshot?.meta?.eligible_accounts || taskSnapshot?.meta?.eligible || 0)
      if (isOaiPayApprovalTask) return count > 0 ? `OaiPay授权链接 (${count} 个)` : 'OaiPay授权链接'
      return count > 0 ? `PayPal绑定 (${count} 个)` : 'PayPal绑定'
    }
    if (taskModalMode === 'payment_link') {
      const eligible = Number(taskSnapshot?.meta?.eligible || 0)
      return eligible > 0 ? `批量订阅链接 (${eligible} 个)` : '批量订阅链接'
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

  return (
    <Modal
      title={modalTitle()}
      open={open}
      onCancel={onClose}
      footer={null}
      width={taskId && (isPhoneBindingTest || isPhoneSignupTask) ? 980 : taskId ? 760 : isPhoneSignup ? 620 : 500}
      maskClosable={false}
    >
      {!taskId ? (
        <Form form={registerForm} layout="vertical" onFinish={onRegister}>
          {currentPlatform === 'chatgpt' ? (
            <Form.Item name="chatgpt_registration_entry" label="注册入口" initialValue="email_signup">
              <Select
                options={[
                  { value: 'email_signup', label: '邮箱注册' },
                ]}
              />
            </Form.Item>
          ) : null}
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
                  label="手机号----收码API"
                  rules={[
                    {
                      validator: (_, value) => {
                        if (!isPhoneSignup || phoneSignupUsePool) return Promise.resolve()
                        return String(value || '').trim()
                          ? Promise.resolve()
                          : Promise.reject(new Error('请输入手机号----收码API，或勾选使用手机号池'))
                      },
                    },
                  ]}
                  extra="一行一个，格式与手机号绑定一致：手机号----收码API。注册数量大于 1 时会逐行使用。"
                >
                  <Input.TextArea
                    rows={5}
                    placeholder={'+573234567890----https://example.com/sms?id=xxx'}
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
            <Form.Item name="mail_provider_override" label="邮箱服务" initialValue="__global__">
              <Select
                options={[
                  {
                    value: '__global__',
                    label: `跟随全局默认（当前：${mailProviderLabel(registerMailProvider)}）`,
                  },
                  { value: 'hme_ready_api', label: 'HME Ready API' },
                  { value: 'icloud_hme', label: 'iCloud HME' },
                  { value: 'tempmail_local', label: 'TempMail Ready API' },
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
              message="当前注册将使用 HME Ready API"
              description="auto-gpt 会通过 icloud-hide-email-helper 出池、收码和 finalize；不会直接使用 iCloud Cookie。"
            />
          ) : null}
          {currentPlatform === 'chatgpt' && !isPhoneSignup && effectiveRegisterMailProvider === 'icloud_hme' ? (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message="当前注册将使用 iCloud HME"
              description="auto-gpt 会按全局 iCloud HME 配置直接管理 Cookie、导入池/实时创建和共享收件箱。"
            />
          ) : null}
          {currentPlatform === 'chatgpt' && !isPhoneSignup && effectiveTempMailProvider ? (
            <>
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 16 }}
                message="当前注册将使用 TempMail Ready API"
                description="固定域名建箱。可单选或多选域名；多选时每个新邮箱会从候选域名中随机选择一个。"
              />
              <Space align="start" style={{ width: '100%' }}>
                <Form.Item
                  name="tempmail_fixed_domains"
                  label="TempMail 可用域名"
                  style={{ flex: 1 }}
                  rules={[
                    {
                      validator: (_, value) => {
                        if (!effectiveTempMailProvider) return Promise.resolve()
                        return normalizeDomainList(value).length > 0
                          ? Promise.resolve()
                          : Promise.reject(new Error('请选择至少一个 TempMail 可用域名'))
                      },
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
            </>
          ) : null}
          {currentPlatform === 'chatgpt' && !isPhoneSignup && effectiveRegisterMailProvider === 'manual_email_otp' ? (
            <>
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 16 }}
                message="当前注册将使用手动邮箱模式"
                description="请先填写你的邮箱地址。默认仍走原始“注册新号”逻辑；若开启“已有账号抓 auth”，则会跳过注册状态机，直接登录并抓取 workspace auth。真正需要验证码时，弹窗会切到任务日志面板，再出现验证码输入卡片。"
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
                    extra="开启后：跳过注册状态机，直接登录已有账号抓取 auth / workspace。关闭则保持原始手动注册新号逻辑。"
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
          <Form.Item name="proxy_mode" label="代理模式" initialValue="pool">
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
                label={proxyMode === 'dynamic' ? '动态代理模板' : '指定代理'}
                style={{ flex: 1 }}
                rules={proxyMode === 'dynamic' ? [{ required: true, message: '请输入动态代理模板' }] : undefined}
              >
                <Input placeholder={proxyMode === 'dynamic' ? 'socks5://user-region-JP-sid-xxxx-t-1:pass@host:port' : 'http://user:pass@host:port'} />
              </Form.Item>
              <Form.Item name="proxy_failover" label="失败处理" valuePropName="checked" style={{ width: 180 }} initialValue={false}>
                <Checkbox>{proxyMode === 'dynamic' ? '失败后刷新 sid 重试' : '失败后切换代理池'}</Checkbox>
              </Form.Item>
            </Space>
          ) : null}
          {proxyMode === 'pool' || proxyMode === 'dynamic' || (proxyMode === 'specified' && proxyFailover) ? (
            <Space style={{ width: '100%' }} align="start">
              <Form.Item name="proxy_country_code" label="出口国家" style={{ flex: 1 }}>
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
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="代理模式说明"
            description="直连不使用代理；指定代理默认只用填写节点，勾选失败切换后才使用代理池筛选项；代理池按健康分、冷却和实测出口国家挑选；动态代理只使用模板和出口国家，失败后刷新 sid 重试，不使用代理池的健康分/候选数。"
          />
          {currentPlatform === 'chatgpt' && isPhoneSignup ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message="手机号注册固定保存 AccessToken-only"
              description="这条链路只负责手机号注册阶段，不进入邮箱注册、team invite、full-auth 或 refresh_token 补抓。"
            />
          ) : currentPlatform === 'chatgpt' ? (
            <>
              <Form.Item label="ChatGPT Token 方案">
                <ChatGPTRegistrationModeSwitch
                  mode={chatgptRegistrationMode}
                  onChange={setChatgptRegistrationMode}
                />
              </Form.Item>
              {chatgptRegistrationMode !== CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN ? (
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message="当前为无 RT 方案"
                  description="team invite / 延迟激活配置会保留，但无 RT 方案下可能无法完整生效。"
                />
              ) : null}
              {chatgptRegistrationMode === CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN ? (
                <Form.Item
                  name="chatgpt_save_registration_access_token_account"
                  valuePropName="checked"
                  initialValue={true}
                  extra="默认开启：注册阶段已拿到 AccessToken，但后续 refresh_token / 工作空间抓取失败时，也会保存一个 AccessToken-only 账号，避免真实注册成功却没有入库。"
                >
                  <Checkbox>保存注册阶段 AccessToken 账号</Checkbox>
                </Form.Item>
              ) : null}
              <Form.Item
                noStyle
                shouldUpdate={(prev, next) => (
                  prev.chatgpt_existing_account_capture !== next.chatgpt_existing_account_capture
                  || prev.chatgpt_enable_team_invite !== next.chatgpt_enable_team_invite
                )}
              >
                {({ getFieldValue }) => {
                  const existingAccountCapture = Boolean(getFieldValue('chatgpt_existing_account_capture'))
                  const teamInviteEnabled = Boolean(getFieldValue('chatgpt_enable_team_invite'))
                  return existingAccountCapture ? (
                    <>
                      <Form.Item
                        label="工作空间抓取"
                        extra="默认只抓 free；只有确认是 Team/Business 或你明确勾选时才抓 business，避免普通账号保存重复工作空间。"
                      >
                        <Space direction="vertical" size={6}>
                          <Form.Item name="chatgpt_capture_business_workspace" valuePropName="checked" noStyle>
                            <Checkbox>抓取 business 工作空间</Checkbox>
                          </Form.Item>
                          <Form.Item name="chatgpt_capture_free_workspace" valuePropName="checked" noStyle>
                            <Checkbox>抓取 free 工作空间</Checkbox>
                          </Form.Item>
                        </Space>
                      </Form.Item>
                      <Alert
                        type="info"
                        showIcon
                        message="当前使用已有账号抓 auth"
                        description="这条链路不会进入注册 / team invite；会直接登录已有账号，并按上方勾选范围抓取工作空间。"
                      />
                    </>
                  ) : (
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
                      {teamInviteEnabled ? (
                        <>
                          <Form.Item
                            name="chatgpt_team_invite_deferred_activation"
                            valuePropName="checked"
                            extra="开启后：先完成全部账号注册并发出邀请，再统一进入激活阶段；不会在单账号刚注册完时立刻进入 business/free。窗口里的“Business 延迟邀请”只作为补救/重试入口。"
                          >
                            <Checkbox>延迟邀请（先统一发邀请，再统一激活）</Checkbox>
                          </Form.Item>
                          <Form.Item>
                            <Space direction="vertical" size={6}>
                              <Form.Item name="chatgpt_capture_business_workspace" valuePropName="checked" noStyle>
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
                      )}
                    </>
                  )
                }}
              </Form.Item>
            </>
          ) : null}
          <Form.Item>
            <Space direction="vertical" style={{ width: '100%' }} size={8}>
              {currentPlatform === 'chatgpt' ? (
                <Button block onClick={onSaveRegisterSettings} loading={registerSettingsSaving}>
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
          {taskSnapshot?.pending_verification ? (
            <TaskVerificationPanel
              taskId={String(taskId)}
              verification={taskSnapshot.pending_verification}
            />
          ) : null}
          <TaskLogPanel taskId={String(taskId)} onDone={onTaskDone} />
        </Space>
      )}
    </Modal>
  )
}
