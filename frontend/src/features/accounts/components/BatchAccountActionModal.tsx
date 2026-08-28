import { useEffect } from 'react'
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
  danger?: 'warning' | 'danger' | string
  confirmation_param?: string
  confirmation_label?: string
  description?: string
}

export type PlatformActionDefinition = {
  id: string
  label?: string
  params?: PlatformActionParam[]
  batch?: PlatformBatchActionConfig
}

type BatchAccountActionModalProps = {
  action: PlatformActionDefinition | null
  open: boolean
  loading: boolean
  targetScope: 'selected' | 'filtered'
  targetCount: number
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
  maxAccounts = 1000,
  onCancel,
  onSubmit,
}: BatchAccountActionModalProps) {
  const [form] = Form.useForm<Record<string, string | boolean | undefined>>()
  const batchConfig = action?.batch || {}
  const actionLabel = String(action?.label || action?.id || '账号操作')
  const targetLabel = targetScope === 'selected' ? '当前选中' : '当前筛选'
  const exceedsLimit = targetCount > maxAccounts
  const isDanger = batchConfig.danger === 'danger'

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
    const values = await form.validateFields()
    await onSubmit(values)
  }

  return (
    <Modal
      title={`批量${actionLabel}`}
      open={open && Boolean(action)}
      width={600}
      okText={isDanger ? '确认并执行' : '开始执行'}
      cancelText="取消"
      confirmLoading={loading}
      okButtonProps={{ danger: isDanger, disabled: targetCount <= 0 || exceedsLimit }}
      maskClosable={false}
      destroyOnHidden
      onCancel={onCancel}
      onOk={() => { void submit() }}
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
                        : Promise.reject(new Error('请勾选确认后再执行批量操作')),
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
          提交时会校验当前筛选结果数量；范围已变化时不会执行。
        </Typography.Text>
      ) : null}
    </Modal>
  )
}
