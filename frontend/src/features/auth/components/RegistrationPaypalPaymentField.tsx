import { useEffect, useState } from 'react'
import { Form, Switch } from 'antd'
import type { FormInstance } from 'antd/es/form'
import {
  REGISTRATION_ZERO_AMOUNT_ENABLED_FIELD,
  readRegistrationEligibilityEnabled,
} from '@/lib/registrationEligibilityCountry'
import {
  DEFAULT_REGISTRATION_PAYPAL_LINK_ENABLED,
  DEFAULT_REGISTRATION_PAYPAL_PAYMENT_ENABLED,
  REGISTRATION_PAYPAL_LINK_ENABLED_FIELD,
  REGISTRATION_PAYPAL_PAYMENT_ENABLED_FIELD,
  readRegistrationPaypalLinkEnabled,
  readRegistrationPaypalPaymentEnabled,
  writeRegistrationPaypalLinkEnabled,
  writeRegistrationPaypalPaymentEnabled,
} from '@/lib/registrationPaypalPayment'

type RegistrationPaypalPaymentFieldProps = {
  form: FormInstance
  enabled?: boolean
}

export function RegistrationPaypalPaymentField({
  form,
  enabled = true,
}: RegistrationPaypalPaymentFieldProps) {
  const [initialLinkEnabled] = useState(
    () => readRegistrationPaypalLinkEnabled()
      || DEFAULT_REGISTRATION_PAYPAL_LINK_ENABLED,
  )
  const [initialEnabled] = useState(
    () => readRegistrationPaypalPaymentEnabled()
      || DEFAULT_REGISTRATION_PAYPAL_PAYMENT_ENABLED,
  )
  const watchedZeroEnabled = Form.useWatch(REGISTRATION_ZERO_AMOUNT_ENABLED_FIELD, form)
  const watchedLinkEnabled = Form.useWatch(REGISTRATION_PAYPAL_LINK_ENABLED_FIELD, form)
  const zeroEnabled = watchedZeroEnabled === undefined
    ? readRegistrationEligibilityEnabled()
    : Boolean(watchedZeroEnabled)
  const linkEnabled = watchedLinkEnabled === undefined
    ? initialLinkEnabled
    : Boolean(watchedLinkEnabled)

  useEffect(() => {
    if (watchedZeroEnabled !== false) return
    form.setFieldsValue({
      [REGISTRATION_PAYPAL_LINK_ENABLED_FIELD]: false,
      [REGISTRATION_PAYPAL_PAYMENT_ENABLED_FIELD]: false,
    })
    writeRegistrationPaypalLinkEnabled(false)
    writeRegistrationPaypalPaymentEnabled(false)
  }, [form, watchedZeroEnabled])

  useEffect(() => {
    if (watchedLinkEnabled !== false) return
    form.setFieldValue(REGISTRATION_PAYPAL_PAYMENT_ENABLED_FIELD, false)
    writeRegistrationPaypalPaymentEnabled(false)
  }, [form, watchedLinkEnabled])

  if (!enabled) return null

  return (
    <>
      <Form.Item
        name={REGISTRATION_PAYPAL_LINK_ENABLED_FIELD}
        label="有 0 元资格后提链"
        valuePropName="checked"
        initialValue={initialLinkEnabled}
        getValueFromEvent={(checked: boolean) => {
          const next = Boolean(checked)
          writeRegistrationPaypalLinkEnabled(next)
          if (!next) {
            form.setFieldValue(REGISTRATION_PAYPAL_PAYMENT_ENABLED_FIELD, false)
            writeRegistrationPaypalPaymentEnabled(false)
          }
          return next
        }}
        extra={zeroEnabled
          ? '只对本次检测明确为 0 元可用的账号提取 PayPal approval URL。'
          : '先开启注册后 0 元检测。'}
      >
        <Switch disabled={!zeroEnabled} checkedChildren="开启" unCheckedChildren="关闭" />
      </Form.Item>
      <Form.Item
        name={REGISTRATION_PAYPAL_PAYMENT_ENABLED_FIELD}
        label="提链成功后提交支付"
        valuePropName="checked"
        initialValue={initialEnabled}
        getValueFromEvent={(checked: boolean) => {
          const next = Boolean(checked)
          writeRegistrationPaypalPaymentEnabled(next)
          return next
        }}
        extra={linkEnabled
          ? '开启后会产生真实 PayPal 支付；入队和最终支付结果会分别记录。'
          : '先开启有 0 元资格后提链。'}
      >
        <Switch disabled={!zeroEnabled || !linkEnabled} checkedChildren="开启" unCheckedChildren="关闭" />
      </Form.Item>
    </>
  )
}
