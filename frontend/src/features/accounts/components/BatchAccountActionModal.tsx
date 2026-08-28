import { useEffect, useRef } from 'react'
import { Alert, Checkbox, Form, Input, Modal, Select, Typography } from 'antd'

export type PlatformActionParam = {
  key: string
  label?: string
  type?: 'text' | 'boolean' | 'select' | string
  default?: string | boolean
  options?: Array<string | { label?: string; value?: string }>
  placeholder?: string
}

export type PlatformBatchActionConfig = {
  mode?: string
  group?: 'authentication' | 'integration' | string
  selected_only?: boolean
  scopes?: Array<'single' | 'selected' | 'filtered' | string>
  danger?: 'warning' | 'danger' | string
  confirmation_param?: string
  confirmation_label?: string
  description?: string
}

export type PlatformActionExecutionConfig = {
  mode?: 'task' | string
  handler?: string
  scopes?: Array<'single' | 'selected' | 'filtered' | string>
}

export type PlatformActionDefinition = {
  id: string
  label?: string
  params?: PlatformActionParam[]
  batch?: PlatformBatchActionConfig
  execution?: PlatformActionExecutionConfig
}

type BatchAccountActionModalProps = {
  action: PlatformActionDefinition | null
  open: boolean
  loading: boolean
  targetScope: 'single' | 'selected' | 'filtered'
  targetCount: number
  targetSummary?: string
  maxAccounts?: number
  onCancel: () => void
  onSubmit: (params: Record<string, string | boolean | undefined>) => Promise<void> | void
}

function selectOptions(options: PlatformActionParam['options']) {
  return (options || []).map((option) => (
    typeof option === 'string'
      ? { label: option, value: option }
      : { label: option.label || option.value || '', value: option.value || option.label || '' }
  ))
}

function isSecretParam(param: PlatformActionParam) {
  return /(?:api[_ -]?key|admin[_ -]?key|token|secret)/i.test(`${param.key} ${param.label || ''}`)
}

export function BatchAccountActionModal({
  action,
  open,
  loading,
  targetScope,
  targetCount,
  targetSummary = '',
  maxAccounts = 1000,
  onCancel,
  onSubmit,
}: BatchAccountActionModalProps) {
  const [form] = Form.useForm<Record<string, string | boolean | undefined>>()
  const submitInFlightRef = useRef(false)
  const batchConfig = action?.batch || {}
  const actionLabel = String(action?.label || action?.id || '账号操作')
  const targetLabel = targetScope === 'single'
    ? '当前账号'
    : targetScope === 'selected' ? '当前选中' : '当前筛选'
  const exceedsLimit = targetCount > maxAccounts
  const isDanger = batchConfig.danger === 'danger'
  const isBatch = targetScope !== 'single'

  useEffect(() => {
    if (!open || !action) return
    const initialValues: Record<string, string | boolean | undefined> = {}
    for (const param of action.params || []) {
      if (param.default !== undefined) initialValues[param.key] = param.default
    }
    form.resetFields()
    form.setFieldsValue(initialValues)
  }, [action, form, open])

  const submit = async () => {
    if (loading || submitInFlightRef.current) return
    submitInFlightRef.current = true
    try {
      const values = await form.validateFields()
      await onSubmit(values)
    } finally {
      submitInFlightRef.current = false
    }
  }

  return (
    <Modal
      title={`${isBatch ? '批量' : ''}${actionLabel}`}
      open={open && Boolean(action)}
      width={600}
      okText={isDanger ? '确认并执行' : '开始执行'}
      cancelText="取消"
      confirmLoading={loading}
      okButtonProps={{ danger: isDanger, disabled: targetCount <= 0 || exceedsLimit }}
      maskClosable={false}
      destroyOnHidden
      onCancel={onCancel}
      onOk={() => submit()}
    >
      <Alert
        type={exceedsLimit ? 'error' : isDanger ? 'error' : batchConfig.danger === 'warning' ? 'warning' : 'info'}
        showIcon
        style={{ marginBottom: 16 }}
        message={`${targetLabel} ${targetCount} 个账号`}
        description={exceedsLimit
          ? `通用批量操作单次最多处理 ${maxAccounts} 个账号，请缩小筛选范围或改为勾选目标账号。`
          : batchConfig.description || `将对${targetLabel}账号执行“${actionLabel}”。`}
      />

      <Form form={form} layout="vertical" preserve={false}>
        {(action?.params || []).map((param) => {
          const requiresConfirmation = param.key === batchConfig.confirmation_param
          if (param.type === 'boolean') {
            return (
              <Form.Item
                key={param.key}
                name={param.key}
                valuePropName="checked"
                rules={requiresConfirmation
                  ? [{
                      validator: (_, value) => value
                        ? Promise.resolve()
                        : Promise.reject(new Error('请勾选确认后再执行操作')),
                    }]
                  : undefined}
              >
                <Checkbox>
                  {requiresConfirmation
                    ? batchConfig.confirmation_label || param.label || param.key
                    : param.label || param.key}
                </Checkbox>
              </Form.Item>
            )
          }

          return (
            <Form.Item key={param.key} name={param.key} label={param.label || param.key}>
              {param.type === 'select' ? (
                <Select options={selectOptions(param.options)} />
              ) : isSecretParam(param) ? (
                <Input.Password
                  autoComplete="new-password"
                  placeholder={param.placeholder || '留空使用系统设置'}
                />
              ) : (
                <Input placeholder={param.placeholder || '留空使用系统设置'} />
              )}
            </Form.Item>
          )
        })}
      </Form>

      {targetScope === 'filtered' ? (
        <Typography.Text type="secondary" style={{ display: 'block', fontSize: 12 }}>
          {targetSummary ? `筛选摘要：${targetSummary}。` : ''}提交时会校验当前筛选结果数量；范围已变化时不会执行。
        </Typography.Text>
      ) : null}
    </Modal>
  )
}
