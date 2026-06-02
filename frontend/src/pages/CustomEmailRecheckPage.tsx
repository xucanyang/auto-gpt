import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  ConfigProvider,
  Descriptions,
  Form,
  Input,
  Row,
  Select,
  Space,
  Tag,
  Typography,
  message,
  theme,
} from 'antd'
import {
  CheckCircleOutlined,
  KeyOutlined,
  LoadingOutlined,
  MailOutlined,
  PlayCircleOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons'
import { ChatGPTRegistrationModeSwitch } from '@/components/ChatGPTRegistrationModeSwitch'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import { TaskVerificationPanel } from '@/components/TaskVerificationPanel'
import { usePersistentChatGPTRegistrationMode } from '@/hooks/usePersistentChatGPTRegistrationMode'
import { buildChatGPTRegistrationRequestAdapter } from '@/lib/chatgptRegistrationRequestAdapter'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import { CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN } from '@/lib/chatgptRegistrationMode'
import { getExecutorOptions, normalizeExecutorForPlatform } from '@/lib/platformExecutorOptions'
import { apiFetch } from '@/lib/utils'

const { Paragraph, Text, Title } = Typography

const CUSTOM_EMAIL_RECHECK_STORAGE_KEY = 'auto-chatgpt.custom-email-recheck.current-task'

type TaskStatus = 'pending' | 'running' | 'done' | 'failed' | 'stopped'

function normalizeTaskStatus(value: unknown): TaskStatus {
  const normalized = String(value || '').trim().toLowerCase()
  if (normalized === 'success') return 'done'
  if (normalized === 'skipped') return 'stopped'
  if (normalized === 'pending' || normalized === 'running' || normalized === 'done' || normalized === 'failed' || normalized === 'stopped') {
    return normalized
  }
  return 'pending'
}

function mapHistoryTaskStatus(status: unknown, snapshotStatus?: unknown): TaskStatus {
  const normalized = String(status || '').trim().toLowerCase()
  if (normalized === 'success') return 'done'
  if (normalized === 'failed') return 'failed'
  if (normalized === 'skipped' || normalized === 'stopped') return 'stopped'
  if (normalized === 'running') return 'running'
  return normalizeTaskStatus(snapshotStatus)
}

function normalizeTaskSnapshot(task: any, fallbackTaskId?: string) {
  if (!task) return null
  const normalizedId = task.id || task.task_id || fallbackTaskId || ''
  return {
    ...task,
    id: normalizedId,
    task_id: normalizedId,
    status: normalizeTaskStatus(task.status || task.status_snapshot || 'pending'),
    progress: task.progress || '0/0',
    skipped: task.skipped ?? 0,
    success: task.success ?? 0,
    errors: Array.isArray(task.errors) ? task.errors : [],
    cashier_urls: Array.isArray(task.cashier_urls) ? task.cashier_urls : [],
    pending_verification: task.pending_verification || null,
    error: task.error || '',
    meta: task.meta && typeof task.meta === 'object' ? task.meta : {},
  }
}

function statusTag(task: any) {
  const status = normalizeTaskStatus(task?.status)
  if (status === 'done') return <Tag color="success" icon={<CheckCircleOutlined />}>已完成</Tag>
  if (status === 'failed') return <Tag color="error">失败</Tag>
  if (status === 'stopped') return <Tag color="warning">已停止</Tag>
  if (status === 'running') return <Tag color="processing" icon={<LoadingOutlined />}>运行中</Tag>
  return <Tag>等待中</Tag>
}

export default function CustomEmailRecheckPage() {
  const [form] = Form.useForm()
  const [task, setTask] = useState<any>(null)
  const [submitting, setSubmitting] = useState(false)
  const [polling, setPolling] = useState(false)
  const pollTimerRef = useRef<number | null>(null)
  const taskRef = useRef<any>(null)
  const { mode: chatgptRegistrationMode, setMode: setChatgptRegistrationMode } =
    usePersistentChatGPTRegistrationMode()

  const isRefreshTokenMode = chatgptRegistrationMode === CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN
  const executorOptions = useMemo(() => getExecutorOptions('chatgpt'), [])
  const watchedEmail = Form.useWatch('email', form)
  const watchedFreeWorkspace = Form.useWatch('chatgpt_capture_free_workspace', form)
  const watchedBusinessWorkspace = Form.useWatch('chatgpt_capture_business_workspace', form)
  const pageTone = useMemo(() => ({
    pageBg: 'linear-gradient(180deg, #edf0eb 0%, #e6ebe5 24%, #dde4df 40%, #f5f7f4 40%, #f1f4f0 100%)',
    pageHalo: 'radial-gradient(circle at top right, rgba(82, 104, 92, 0.18) 0%, rgba(82, 104, 92, 0) 38%)',
    heroPanel: 'linear-gradient(145deg, #202a26 0%, #293631 52%, #35453f 100%)',
    heroBorder: 'rgba(213, 226, 217, 0.14)',
    heroAccent: '#c6d8cb',
    heroMuted: 'rgba(223, 232, 226, 0.82)',
    heroText: '#f5f8f4',
    heroTagBg: 'rgba(198, 216, 203, 0.12)',
    heroPillBg: 'rgba(255,255,255,0.08)',
    paperBg: 'rgba(248, 250, 247, 0.96)',
    paperShadow: '0 24px 64px rgba(35, 48, 41, 0.1)',
    panelBorder: 'rgba(83, 104, 93, 0.14)',
    sectionInset: 'linear-gradient(180deg, #eff4ef 0%, #e7eeea 100%)',
    sectionInsetBorder: 'rgba(89, 109, 99, 0.12)',
    titleText: '#25312c',
    bodyText: '#3d4b45',
    mutedText: '#5d6d65',
    subtleText: '#7b8a83',
    summaryLabel: '#6d7d74',
    summaryValue: '#27342e',
    accentSoft: '#d9e5dd',
    accentLine: '#728c7d',
    actionBg: '#4d6758',
    actionBgHover: '#42594c',
    actionText: '#f4f8f5',
    buttonShadow: '0 14px 30px rgba(50, 68, 58, 0.16)',
    infoAlertBg: '#eef5f0',
    infoAlertBorder: '#cadacd',
    infoAlertText: '#375144',
    warningAlertBg: '#f3f5ec',
    warningAlertBorder: '#d6dac3',
    warningAlertText: '#5e6642',
    neutralTagBg: '#edf3ef',
    neutralTagText: '#476053',
    localInputBg: '#f6f8f4',
    localInputBorder: '#b8c5bb',
    localInputText: '#24312b',
    localPlaceholder: '#87958c',
  }), [])

  const localLightTheme = useMemo(() => ({
    algorithm: theme.defaultAlgorithm,
    token: {
      colorPrimary: pageTone.actionBg,
      colorInfo: pageTone.actionBg,
      colorSuccess: '#4d7460',
      colorWarning: '#8a7b45',
      colorError: '#9c5c58',
      colorText: pageTone.bodyText,
      colorTextBase: pageTone.bodyText,
      colorTextSecondary: pageTone.mutedText,
      colorTextTertiary: pageTone.subtleText,
      colorTextQuaternary: pageTone.subtleText,
      colorBgBase: pageTone.paperBg,
      colorBgContainer: pageTone.paperBg,
      colorBgElevated: '#fbfcfa',
      colorBorder: pageTone.panelBorder,
      colorBorderSecondary: pageTone.sectionInsetBorder,
      colorFillAlter: pageTone.sectionInset,
    },
    components: {
      Input: {
        colorBgContainer: pageTone.localInputBg,
        activeBg: '#ffffff',
        colorBorder: pageTone.localInputBorder,
        colorText: pageTone.localInputText,
        colorTextPlaceholder: pageTone.localPlaceholder,
      },
      InputNumber: {
        colorBgContainer: pageTone.localInputBg,
        activeBg: '#ffffff',
        colorBorder: pageTone.localInputBorder,
        colorText: pageTone.localInputText,
      },
      Select: {
        selectorBg: pageTone.localInputBg,
        colorBorder: pageTone.localInputBorder,
        optionSelectedBg: pageTone.accentSoft,
        optionActiveBg: '#edf2ee',
        colorText: pageTone.localInputText,
        colorTextPlaceholder: pageTone.localPlaceholder,
      },
      Checkbox: {
        colorText: pageTone.bodyText,
      },
      Descriptions: {
        colorText: pageTone.bodyText,
        colorTextSecondary: pageTone.mutedText,
      },
      Alert: {
        colorText: pageTone.bodyText,
      },
      Tag: {
        colorText: pageTone.bodyText,
      },
    },
  }), [pageTone])

  useEffect(() => {
    apiFetch('/config').then((cfg) => {
      const savedEmail = window.localStorage.getItem('auto-chatgpt.manual_email_otp.email') || ''
      form.setFieldsValue({
        email: savedEmail,
        login_password: String(cfg.chatgpt_existing_account_login_password || '').trim(),
        executor_type: normalizeExecutorForPlatform('chatgpt', cfg.default_executor),
        captcha_solver: String(cfg.default_captcha_solver || 'yescaptcha').trim() || 'yescaptcha',
        chatgpt_capture_free_workspace:
          cfg.chatgpt_capture_free_workspace === '' ? true : parseBooleanConfigValue(cfg.chatgpt_capture_free_workspace),
        chatgpt_capture_business_workspace:
          cfg.chatgpt_capture_business_workspace === '' ? false : parseBooleanConfigValue(cfg.chatgpt_capture_business_workspace),
        chatgpt_save_registration_access_token_account: false,
      })
    }).catch((error: any) => {
      message.warning(error?.message || '读取默认配置失败，已使用页面默认值')
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

  const pollTask = async (taskId: string) => {
    stopPolling()
    setPolling(true)

    const loadHistoryFallback = async (reason: string) => {
      try {
        const history = await apiFetch(`/tasks/logs/by-task/${encodeURIComponent(taskId)}`) as {
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
        }
        const detail = history?.detail && typeof history.detail === 'object' ? history.detail : {}
        const restoredTask = normalizeTaskSnapshot({
          ...(taskRef.current || {}),
          id: taskId,
          status: mapHistoryTaskStatus(history.status, detail.status_snapshot),
          progress: detail.progress || taskRef.current?.progress || '0/0',
          skipped: detail.skipped ?? taskRef.current?.skipped ?? 0,
          success: detail.success ?? taskRef.current?.success ?? 0,
          errors: Array.isArray(detail.errors) ? detail.errors : (taskRef.current?.errors || []),
          cashier_urls: Array.isArray(detail.cashier_urls) ? detail.cashier_urls : (taskRef.current?.cashier_urls || []),
          error: history.error || taskRef.current?.error || reason,
        }, taskId)
        setTask(restoredTask)
        return true
      } catch {
        return false
      }
    }

    pollTimerRef.current = window.setInterval(async () => {
      try {
        const snapshot = await apiFetch(`/tasks/${taskId}`)
        const normalizedTask = normalizeTaskSnapshot(snapshot, taskId)
        setTask(normalizedTask)
        if (['done', 'failed', 'stopped'].includes(String(normalizedTask.status))) {
          stopPolling()
        }
      } catch (error_: unknown) {
        const detail = error_ instanceof Error ? error_.message : '获取任务状态失败'
        const recovered = await loadHistoryFallback(detail)
        stopPolling()
        if (!recovered) {
          setTask((previous: any) => normalizeTaskSnapshot({
            ...(previous || {}),
            id: taskId,
            status: previous?.status || 'failed',
            error: detail,
          }, taskId))
          message.error(detail)
        }
      }
    }, 2000)
  }

  useEffect(() => {
    if (typeof window === 'undefined') return
    const saved = window.localStorage.getItem(CUSTOM_EMAIL_RECHECK_STORAGE_KEY)
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
      window.localStorage.removeItem(CUSTOM_EMAIL_RECHECK_STORAGE_KEY)
    }
  }, [])

  useEffect(() => {
    if (typeof window === 'undefined') return
    if (!task?.id) return
    const persistedTask = normalizeTaskSnapshot(task, task.id)
    window.localStorage.setItem(CUSTOM_EMAIL_RECHECK_STORAGE_KEY, JSON.stringify({
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
      meta: persistedTask?.meta,
    }))
  }, [task])

  useEffect(() => () => stopPolling(), [])

  const handleSubmit = async () => {
    const values = await form.validateFields()
    if (!isRefreshTokenMode) {
      throw new Error('自定义邮箱测活当前仅支持 RT 方案')
    }
    if (!values.chatgpt_capture_free_workspace && !values.chatgpt_capture_business_workspace) {
      throw new Error('至少选择一个工作空间抓取范围')
    }

    const normalizedEmail = String(values.email || '').trim()
    const normalizedPassword = String(values.login_password || '').trim()
    const registerExtra = {
      mail_provider: 'manual_email_otp',
      manual_email_address: normalizedEmail,
      chatgpt_existing_account_capture: true,
      chatgpt_capture_free_workspace: Boolean(values.chatgpt_capture_free_workspace),
      chatgpt_capture_business_workspace: Boolean(values.chatgpt_capture_business_workspace),
      chatgpt_enable_team_invite: false,
      chatgpt_team_invite_deferred_activation: false,
      chatgpt_save_registration_access_token_account: Boolean(values.chatgpt_save_registration_access_token_account),
      chatgpt_registration_mode: 'refresh_token',
    }

    const chatgptRegistrationRequestAdapter = buildChatGPTRegistrationRequestAdapter('chatgpt', chatgptRegistrationMode)
    const adaptedRegisterExtra = chatgptRegistrationRequestAdapter
      ? chatgptRegistrationRequestAdapter.extendExtra(registerExtra)
      : registerExtra

    setSubmitting(true)
    try {
      window.localStorage.setItem('auto-chatgpt.manual_email_otp.email', normalizedEmail)
      const response = await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          email: normalizedEmail,
          password: normalizedPassword || null,
          count: 1,
          concurrency: 1,
          register_delay_seconds: 0,
          proxy: null,
          executor_type: values.executor_type,
          captcha_solver: values.captcha_solver,
          extra: adaptedRegisterExtra,
        }),
      }) as { task_id?: string }

      const taskId = String(response?.task_id || '').trim()
      if (!taskId) {
        throw new Error('创建测活任务成功，但未返回 task_id')
      }

      setTask(normalizeTaskSnapshot({
        id: taskId,
        status: 'running',
        progress: '0/1',
        meta: {
          email: normalizedEmail,
          source: 'custom_email_recheck',
        },
      }, taskId))

      try {
        const snapshot = await apiFetch(`/tasks/${taskId}`)
        setTask(normalizeTaskSnapshot(snapshot, taskId))
      } catch {
        // 首轮快照失败时由后续轮询兜底
      }
      void pollTask(taskId)
      message.success('自定义邮箱测活任务已启动')
    } catch (error_: unknown) {
      const detail = error_ instanceof Error ? error_.message : '创建自定义邮箱测活任务失败'
      message.error(detail)
    } finally {
      setSubmitting(false)
    }
  }

  const summaryEmail = String(task?.meta?.email || watchedEmail || '').trim()
  const summaryScope = [
    watchedFreeWorkspace ? 'free' : null,
    watchedBusinessWorkspace ? 'business' : null,
  ].filter(Boolean).join(' + ') || '-'

  return (
    <ConfigProvider theme={localLightTheme}>
      <div
        className="page-enter"
        style={{
          minHeight: 'calc(100vh - 48px)',
          background: `${pageTone.pageHalo}, ${pageTone.pageBg}`,
          borderRadius: 28,
          padding: 20,
          color: pageTone.bodyText,
        }}
      >
        <div
          style={{
            maxWidth: 1120,
            margin: '0 auto',
          }}
        >
          <div
            style={{
              background: pageTone.heroPanel,
              border: `1px solid ${pageTone.heroBorder}`,
              borderRadius: 28,
              padding: '28px 28px 24px',
              color: pageTone.heroText,
              boxShadow: '0 24px 56px rgba(36, 23, 16, 0.22)',
            }}
          >
            <Space direction="vertical" size={14} style={{ width: '100%' }}>
              <Tag
                bordered={false}
                style={{
                  alignSelf: 'flex-start',
                  background: pageTone.heroTagBg,
                  color: pageTone.heroAccent,
                  paddingInline: 12,
                  lineHeight: '26px',
                  borderRadius: 999,
                }}
              >
                独立工具页
              </Tag>
              <div>
                <Title level={2} style={{ color: pageTone.heroText, margin: 0 }}>
                  自定义邮箱测活
                </Title>
                <Paragraph style={{ color: pageTone.heroMuted, margin: '10px 0 0', maxWidth: 760 }}>
                  这个页面专门处理“不在 ChatGPT 账号列表中”的邮箱。它复用现有手动邮箱与已有账号抓 Auth 链路，
                  成功后会按当前系统规则自动保存到账号池，失败则保留任务日志和验证码交互，不再依赖原先的失效账号列表入口。
                </Paragraph>
              </div>
              <Space size={[8, 8]} wrap>
                <Tag bordered={false} style={{ background: pageTone.heroPillBg, color: pageTone.heroText }}>手动邮箱 + 手输验证码</Tag>
                <Tag bordered={false} style={{ background: pageTone.heroPillBg, color: pageTone.heroText }}>已有账号抓 Auth</Tag>
                <Tag bordered={false} style={{ background: pageTone.heroPillBg, color: pageTone.heroText }}>独立任务日志</Tag>
              </Space>
            </Space>
          </div>

          <Row gutter={[20, 20]} style={{ marginTop: 20 }}>
            <Col xs={24} xl={15}>
              <Card
                bordered={false}
                style={{
                  borderRadius: 24,
                  background: pageTone.paperBg,
                  boxShadow: pageTone.paperShadow,
                  border: `1px solid ${pageTone.panelBorder}`,
                }}
                bodyStyle={{ padding: 24 }}
                title={<span style={{ color: pageTone.titleText }}>测活参数</span>}
              >
                <Form
                  form={form}
                  layout="vertical"
                  initialValues={{
                    email: '',
                    login_password: '',
                    executor_type: 'protocol',
                    captcha_solver: 'yescaptcha',
                    chatgpt_capture_free_workspace: true,
                    chatgpt_capture_business_workspace: false,
                    chatgpt_save_registration_access_token_account: false,
                  }}
                  onFinish={handleSubmit}
                >
                <Row gutter={16}>
                  <Col xs={24} md={15}>
                    <Form.Item
                      name="email"
                      label="邮箱地址"
                      rules={[
                        { required: true, message: '请输入邮箱地址' },
                        { type: 'email', message: '请输入合法邮箱地址' },
                      ]}
                      extra="这里填写列表外账号的真实邮箱地址。页面会记住你最近一次填写的值。"
                    >
                      <Input
                        size="large"
                        prefix={<MailOutlined />}
                        placeholder="name@example.com"
                        autoComplete="email"
                      />
                    </Form.Item>
                  </Col>
                  <Col xs={24} md={9}>
                    <Form.Item
                      name="executor_type"
                      label="执行器"
                      extra="默认跟随当前 ChatGPT 页面惯用执行器。"
                    >
                      <Select size="large" options={executorOptions} />
                    </Form.Item>
                  </Col>
                </Row>

                <Form.Item
                  name="captcha_solver"
                  label="验证码方案"
                  rules={[{ required: true, message: '请选择验证码方案' }]}
                  extra="默认跟随全局配置。页面本身只负责邮箱与登录测活，不限制你继续用自动验证码服务。"
                >
                  <Select
                    size="large"
                    options={[
                      { value: 'yescaptcha', label: 'YesCaptcha' },
                      { value: 'local_solver', label: '本地 Solver (Camoufox)' },
                      { value: 'manual', label: '手动' },
                    ]}
                  />
                </Form.Item>

                <Form.Item
                  name="login_password"
                  label="登录密码"
                  extra="留空时优先走邮箱 OTP。填写后会优先尝试密码登录，再按实际页面需要进入邮箱验证码流程。"
                >
                  <Input.Password
                    size="large"
                    prefix={<KeyOutlined />}
                    placeholder="留空表示优先邮箱 OTP"
                    autoComplete="current-password"
                  />
                </Form.Item>

                <Card
                  bordered={false}
                  style={{
                    background: pageTone.sectionInset,
                    border: `1px solid ${pageTone.sectionInsetBorder}`,
                    borderRadius: 20,
                    marginBottom: 20,
                  }}
                  bodyStyle={{ padding: 18 }}
                >
                  <Space direction="vertical" size={12} style={{ width: '100%' }}>
                    <div>
                      <Text strong style={{ fontSize: 15, color: pageTone.titleText }}>抓取策略</Text>
                      <Paragraph style={{ margin: '6px 0 0', color: pageTone.mutedText }}>
                        这页固定走 ChatGPT RT 链路，并开启“已有账号抓 Auth”。如果只想判断邮箱对应账号是否还能登录，
                        保留 `free` 即可；只有明确要抓团队工作空间时，再打开 `business`。
                      </Paragraph>
                    </div>
                    <Form.Item label="Token 方案" style={{ marginBottom: 0 }}>
                      <ChatGPTRegistrationModeSwitch
                        mode={chatgptRegistrationMode}
                        onChange={setChatgptRegistrationMode}
                      />
                    </Form.Item>
                    {!isRefreshTokenMode ? (
                      <Alert
                        type="warning"
                        showIcon
                        style={{
                          background: pageTone.warningAlertBg,
                          borderColor: pageTone.warningAlertBorder,
                          color: pageTone.warningAlertText,
                        }}
                        message="当前不是 RT 方案"
                        description="自定义邮箱测活依赖已有账号抓 Auth，目前只支持 RT 方案。切回 RT 后再启动任务。"
                      />
                    ) : null}
                    <Row gutter={[12, 12]}>
                      <Col xs={24} md={12}>
                        <Form.Item name="chatgpt_capture_free_workspace" valuePropName="checked" style={{ marginBottom: 0 }}>
                          <Checkbox>抓取 free 工作空间</Checkbox>
                        </Form.Item>
                      </Col>
                      <Col xs={24} md={12}>
                        <Form.Item name="chatgpt_capture_business_workspace" valuePropName="checked" style={{ marginBottom: 0 }}>
                          <Checkbox>抓取 business 工作空间</Checkbox>
                        </Form.Item>
                      </Col>
                    </Row>
                    <Form.Item
                      name="chatgpt_save_registration_access_token_account"
                      valuePropName="checked"
                      style={{ marginBottom: 0 }}
                    >
                      <Checkbox>后续工作空间补抓失败时，仍保留 AccessToken-only 结果</Checkbox>
                    </Form.Item>
                  </Space>
                </Card>

                <Alert
                  type="info"
                  showIcon
                  style={{
                    marginBottom: 20,
                    background: pageTone.infoAlertBg,
                    borderColor: pageTone.infoAlertBorder,
                    color: pageTone.infoAlertText,
                  }}
                  message="这不是原来的失效账号测活"
                  description="原失效测活依赖现有账号表里的 invalid 记录和 mailbox_state；这个页面绕开那个前提，直接对你指定的邮箱发起登录探测。"
                />

                <Space size={12} wrap>
                  <Button
                    type="primary"
                    htmlType="submit"
                    size="large"
                    icon={<PlayCircleOutlined />}
                    loading={submitting}
                    disabled={!isRefreshTokenMode}
                    style={{
                      background: pageTone.actionBg,
                      borderColor: pageTone.actionBg,
                      color: pageTone.actionText,
                      boxShadow: pageTone.buttonShadow,
                    }}
                  >
                    启动测活
                  </Button>
                  <Button
                    size="large"
                    onClick={() => {
                      form.resetFields()
                      const savedEmail = window.localStorage.getItem('auto-chatgpt.manual_email_otp.email') || ''
                      form.setFieldsValue({
                        email: savedEmail,
                        chatgpt_capture_free_workspace: true,
                        chatgpt_capture_business_workspace: false,
                        chatgpt_save_registration_access_token_account: false,
                      })
                    }}
                  >
                    重置页面
                  </Button>
                </Space>
                </Form>
              </Card>
            </Col>

            <Col xs={24} xl={9}>
              <Space direction="vertical" size={20} style={{ width: '100%' }}>
                <Card
                bordered={false}
                style={{
                  borderRadius: 24,
                  background: pageTone.paperBg,
                  boxShadow: pageTone.paperShadow,
                  border: `1px solid ${pageTone.panelBorder}`,
                }}
                bodyStyle={{ padding: 22 }}
              >
                <Space direction="vertical" size={14} style={{ width: '100%' }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
                    <Text strong style={{ fontSize: 16 }}>当前任务</Text>
                    {statusTag(task)}
                  </div>
                  <Descriptions
                    column={1}
                    size="small"
                    labelStyle={{ width: 88, color: pageTone.summaryLabel, fontWeight: 500 }}
                    contentStyle={{ color: pageTone.summaryValue, fontWeight: 600 }}
                  >
                    <Descriptions.Item label="邮箱">{summaryEmail || '-'}</Descriptions.Item>
                    <Descriptions.Item label="范围">{summaryScope}</Descriptions.Item>
                    <Descriptions.Item label="进度">{String(task?.progress || '未启动')}</Descriptions.Item>
                    <Descriptions.Item label="任务 ID">{String(task?.id || '-')}</Descriptions.Item>
                  </Descriptions>
                  {polling ? (
                    <Text style={{ color: pageTone.mutedText }}>任务状态轮询中，验证码面板会在需要时自动出现。</Text>
                  ) : (
                    <Text style={{ color: pageTone.mutedText }}>任务结束后，这里会保留最后一次快照，方便回看失败原因。</Text>
                  )}
                </Space>
                </Card>

                <Card
                bordered={false}
                style={{
                  borderRadius: 24,
                  background: pageTone.paperBg,
                  boxShadow: pageTone.paperShadow,
                  border: `1px solid ${pageTone.panelBorder}`,
                }}
                bodyStyle={{ padding: 22 }}
                title={<span style={{ color: pageTone.titleText }}>使用说明</span>}
              >
                <Space direction="vertical" size={12} style={{ width: '100%' }}>
                  <div style={{ display: 'flex', gap: 12 }}>
                    <SafetyCertificateOutlined style={{ color: pageTone.accentLine, fontSize: 18, marginTop: 2 }} />
                    <Text style={{ color: pageTone.bodyText }}>如果账号需要邮箱验证码，这里不会中断任务，而是把输入面板挂到下方任务区。</Text>
                  </div>
                  <div style={{ display: 'flex', gap: 12 }}>
                    <SafetyCertificateOutlined style={{ color: pageTone.accentLine, fontSize: 18, marginTop: 2 }} />
                    <Text style={{ color: pageTone.bodyText }}>成功结果会按现有注册保存规则进入账号池；同邮箱且同工作空间变体会覆盖，不会无限重复插入。</Text>
                  </div>
                  <div style={{ display: 'flex', gap: 12 }}>
                    <SafetyCertificateOutlined style={{ color: pageTone.accentLine, fontSize: 18, marginTop: 2 }} />
                    <Text style={{ color: pageTone.bodyText }}>若只是测试普通个人号，建议只抓 `free`，这样更接近原“测活”语义，也能减少无关失败。</Text>
                  </div>
                </Space>
                </Card>
              </Space>
            </Col>
          </Row>

          {task?.id ? (
            <Card
              bordered={false}
              style={{
                borderRadius: 24,
                background: pageTone.paperBg,
                boxShadow: pageTone.paperShadow,
                border: `1px solid ${pageTone.panelBorder}`,
                marginTop: 20,
              }}
              bodyStyle={{ padding: 22 }}
              title={<span style={{ color: pageTone.titleText }}>任务面板</span>}
            >
              <Space direction="vertical" size={16} style={{ width: '100%' }}>
                {task?.pending_verification ? (
                  <TaskVerificationPanel
                    taskId={String(task.id)}
                    verification={task.pending_verification}
                  />
                ) : null}
                <TaskLogPanel taskId={String(task.id)} onDone={() => {
                  void pollTask(String(task.id))
                }} />
              </Space>
            </Card>
          ) : null}
        </div>
      </div>
    </ConfigProvider>
  )
}
