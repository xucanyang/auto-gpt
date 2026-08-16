import { useState } from 'react'
import { Form, Switch } from 'antd'
import {
  DEFAULT_REGISTRATION_PAYPAL_PAYMENT_ENABLED,
  REGISTRATION_PAYPAL_PAYMENT_ENABLED_FIELD,
  readRegistrationPaypalPaymentEnabled,
  writeRegistrationPaypalPaymentEnabled,
} from '@/lib/registrationPaypalPayment'

type RegistrationPaypalPaymentFieldProps = {
  enabled?: boolean
}

export function RegistrationPaypalPaymentField({
  enabled = true,
}: RegistrationPaypalPaymentFieldProps) {
  const [initialEnabled] = useState(
    () => readRegistrationPaypalPaymentEnabled()
      || DEFAULT_REGISTRATION_PAYPAL_PAYMENT_ENABLED,
  )

  if (!enabled) return null

  return (
    <Form.Item
      name={REGISTRATION_PAYPAL_PAYMENT_ENABLED_FIELD}
      label="注册后提链并支付"
      valuePropName="checked"
      initialValue={initialEnabled}
      getValueFromEvent={(checked: boolean) => {
        const next = Boolean(checked)
        writeRegistrationPaypalPaymentEnabled(next)
        return next
      }}
      extra="开启后会产生真实 PayPal 支付；提链或支付入队异常只记录后处理结果，不改变注册成功状态。"
    >
      <Switch checkedChildren="开启" unCheckedChildren="关闭" />
    </Form.Item>
  )
}
