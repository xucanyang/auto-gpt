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
} from 'antd'
import { ChatGPTRegistrationModeSwitch } from '@/components/ChatGPTRegistrationModeSwitch'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import { TaskVerificationPanel } from '@/components/TaskVerificationPanel'
import {
  CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
  type ChatGPTRegistrationMode,
} from '@/lib/chatgptRegistrationMode'

type RegisterTaskModalProps = {
  open: boolean
  currentPlatform: string
  taskModalMode: 'register' | 'resume_auth' | 'payment_link'
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
  const effectiveRegisterMailProvider =
    currentPlatform === 'chatgpt' && registerProviderOverride && registerProviderOverride !== '__global__'
      ? registerProviderOverride
      : registerMailProvider

  const modalTitle = () => {
    if (taskModalMode === 'payment_link') {
      const eligible = Number(taskSnapshot?.meta?.eligible || 0)
      return eligible > 0 ? `批量订阅链接 (${eligible} 个)` : '批量订阅链接'
    }
    if (taskModalMode === 'resume_auth') {
      return taskModalAccount?.email
        ? `补抓Auth ${taskModalAccount.email}`
        : Number(taskSnapshot?.meta?.eligible || 0) > 0
          ? `批量补抓Auth (${taskSnapshot?.meta?.eligible} 个)`
          : '补抓Auth'
    }
    return `注册 ${currentPlatform}`
  }

  return (
    <Modal
      title={modalTitle()}
      open={open}
      onCancel={onClose}
      footer={null}
      width={taskId ? 760 : 500}
      maskClosable={false}
    >
      {!taskId ? (
        <Form form={registerForm} layout="vertical" onFinish={onRegister}>
          {currentPlatform === 'chatgpt' ? (
            <Form.Item name="mail_provider_override" label="邮箱服务" initialValue="__global__">
              <Select
                options={[
                  {
                    value: '__global__',
                    label: `跟随全局默认（当前：${registerMailProvider === 'manual_email_otp' ? '手动邮箱 + 手输验证码' : registerMailProvider || 'luckmail'}）`,
                  },
                  { value: 'manual_email_otp', label: '手动邮箱 + 手输验证码' },
                ]}
              />
            </Form.Item>
          ) : null}
          {currentPlatform === 'chatgpt' && effectiveRegisterMailProvider === 'manual_email_otp' ? (
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
            <Input type="number" min={1} disabled={currentPlatform === 'chatgpt' && effectiveRegisterMailProvider === 'manual_email_otp'} />
          </Form.Item>
          <Form.Item name="concurrency" label="并发数" initialValue={1} rules={[{ required: true }]}>
            <Input type="number" min={1} max={5} disabled={currentPlatform === 'chatgpt' && effectiveRegisterMailProvider === 'manual_email_otp'} />
          </Form.Item>
          <Form.Item name="register_delay_seconds" label="每个注册延迟(秒)" initialValue={0}>
            <InputNumber min={0} precision={1} step={0.5} style={{ width: '100%' }} placeholder="0 = 不延迟" />
          </Form.Item>
          {currentPlatform === 'chatgpt' && (
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
                  extra="开启后：注册阶段已拿到 AccessToken，但后续 refresh_token / 工作空间抓取失败时，也会保存一个 AccessToken-only 账号。"
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
          )}
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
