import { useState } from 'react'
import { Form, Switch } from 'antd'
import type { FormInstance } from 'antd/es/form'
import { RegistrationCountrySelect } from '@/features/auth/components/RegistrationCountrySelect'
import {
  DEFAULT_REGISTRATION_PAYMENT_DETAILS_ENABLED,
  DEFAULT_REGISTRATION_ZERO_AMOUNT_ENABLED,
  DEFAULT_REGISTRATION_ZERO_AMOUNT_COUNTRY,
  REGISTRATION_PAYMENT_DETAILS_ENABLED_FIELD,
  REGISTRATION_ZERO_AMOUNT_ENABLED_FIELD,
  REGISTRATION_ZERO_AMOUNT_COUNTRY_FIELD,
  normalizeRegistrationEligibilityCountry,
  readRegistrationEligibilityEnabled,
  readRegistrationEligibilityCountry,
  readRegistrationPaymentDetailsEnabled,
  writeRegistrationEligibilityEnabled,
  writeRegistrationEligibilityCountry,
  writeRegistrationPaymentDetailsEnabled,
} from '@/lib/registrationEligibilityCountry'

type RegistrationEligibilityCountryFieldProps = {
  form: FormInstance
  enabled?: boolean
}

export function RegistrationEligibilityCountryField({
  form,
  enabled = true,
}: RegistrationEligibilityCountryFieldProps) {
  const [initialEnabled] = useState(
    () => readRegistrationEligibilityEnabled() || DEFAULT_REGISTRATION_ZERO_AMOUNT_ENABLED,
  )
  const [initialCountry] = useState(
    () => readRegistrationEligibilityCountry() || DEFAULT_REGISTRATION_ZERO_AMOUNT_COUNTRY,
  )
  const [initialPaymentDetailsEnabled] = useState(
    () => readRegistrationPaymentDetailsEnabled()
      || DEFAULT_REGISTRATION_PAYMENT_DETAILS_ENABLED,
  )
  const watchedEnabled = Form.useWatch(REGISTRATION_ZERO_AMOUNT_ENABLED_FIELD, form)
  const watchedPaymentDetailsEnabled = Form.useWatch(
    REGISTRATION_PAYMENT_DETAILS_ENABLED_FIELD,
    form,
  )
  const eligibilityEnabled = watchedEnabled === undefined ? initialEnabled : Boolean(watchedEnabled)
  const paymentDetailsEnabled = watchedPaymentDetailsEnabled === undefined
    ? initialPaymentDetailsEnabled
    : Boolean(watchedPaymentDetailsEnabled)

  if (!enabled) return null

  return (
    <>
      <Form.Item
        name={REGISTRATION_ZERO_AMOUNT_ENABLED_FIELD}
        label="注册后 0 元检测"
        valuePropName="checked"
        initialValue={initialEnabled}
        getValueFromEvent={(checked: boolean) => {
          const next = Boolean(checked)
          writeRegistrationEligibilityEnabled(next)
          return next
        }}
        extra="开启后在账号注册成功后检测 0 元试用资格；检测结果不影响注册成功状态。"
      >
        <Switch checkedChildren="开启" unCheckedChildren="关闭" />
      </Form.Item>
      <Form.Item
        name={REGISTRATION_PAYMENT_DETAILS_ENABLED_FIELD}
        label="注册后链接格式 + 支付方式检测"
        valuePropName="checked"
        initialValue={initialPaymentDetailsEnabled}
        getValueFromEvent={(checked: boolean) => {
          const next = Boolean(checked)
          writeRegistrationPaymentDetailsEnabled(next)
          return next
        }}
        extra="开启后检测 Checkout 链接格式和可用支付方式；与 0 元检测同时开启时共用一次 Checkout。"
      >
        <Switch checkedChildren="开启" unCheckedChildren="关闭" />
      </Form.Item>
      {eligibilityEnabled || paymentDetailsEnabled ? (
      <Form.Item
        name={REGISTRATION_ZERO_AMOUNT_COUNTRY_FIELD}
        label="注册后支付资格检测国家"
        initialValue={initialCountry}
        getValueFromEvent={(value: unknown) => {
          const country = normalizeRegistrationEligibilityCountry(value)
            || DEFAULT_REGISTRATION_ZERO_AMOUNT_COUNTRY
          writeRegistrationEligibilityCountry(country)
          return country
        }}
        rules={[
          { required: true, message: '请选择注册后支付资格检测国家' },
          { pattern: /^[A-Za-z]{2}$/, message: '请选择有效的两位国家代码' },
        ]}
        extra="只影响后置支付资格检测的结账国家，不改变注册出口国家。"
      >
        <RegistrationCountrySelect placeholder="选择支付资格检测国家" />
      </Form.Item>
      ) : null}
    </>
  )
}
