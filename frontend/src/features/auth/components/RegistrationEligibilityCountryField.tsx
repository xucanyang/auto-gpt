import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Form, Select, Space, Typography } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import type { FormInstance } from 'antd/es/form'
import { apiFetch } from '@/lib/utils'
import {
  DEFAULT_REGISTRATION_ZERO_AMOUNT_COUNTRY,
  REGISTRATION_ZERO_AMOUNT_COUNTRY_FIELD,
  normalizeRegistrationEligibilityCountry,
  normalizeRegistrationEligibilityCountryOptions,
  readRegistrationEligibilityCountry,
  writeRegistrationEligibilityCountry,
  type RegistrationEligibilityCountryOption,
} from '@/lib/registrationEligibilityCountry'

type RegistrationEligibilityCountryFieldProps = {
  form: FormInstance
  enabled?: boolean
}

export function RegistrationEligibilityCountryField({
  form,
  enabled = true,
}: RegistrationEligibilityCountryFieldProps) {
  const [options, setOptions] = useState<RegistrationEligibilityCountryOption[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const selectedCountry = Form.useWatch(REGISTRATION_ZERO_AMOUNT_COUNTRY_FIELD, form)

  const loadOptions = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const profile = await apiFetch('/tasks/chatgpt/zero-amount-eligibility/profile')
      const nextOptions = normalizeRegistrationEligibilityCountryOptions(profile?.billing_country_options)
      if (nextOptions.length === 0) throw new Error('服务端未返回可用的国家/币种目录')
      setOptions(nextOptions)

      const current = normalizeRegistrationEligibilityCountry(
        form.getFieldValue(REGISTRATION_ZERO_AMOUNT_COUNTRY_FIELD),
      )
      const stored = readRegistrationEligibilityCountry()
      const profileDefault = normalizeRegistrationEligibilityCountry(profile?.default_country)
        || DEFAULT_REGISTRATION_ZERO_AMOUNT_COUNTRY
      const preferred = [current, stored, profileDefault, DEFAULT_REGISTRATION_ZERO_AMOUNT_COUNTRY]
        .find((value) => nextOptions.some((item) => item.value === value))
        || nextOptions[0].value
      if (!form.isFieldTouched(REGISTRATION_ZERO_AMOUNT_COUNTRY_FIELD) || !current) {
        form.setFieldValue(REGISTRATION_ZERO_AMOUNT_COUNTRY_FIELD, preferred)
      }
      writeRegistrationEligibilityCountry(preferred)
    } catch (cause: unknown) {
      const detail = cause instanceof Error ? cause.message : String(cause || '')
      setError(detail || '无法读取 0 元检测国家目录')
      const current = normalizeRegistrationEligibilityCountry(
        form.getFieldValue(REGISTRATION_ZERO_AMOUNT_COUNTRY_FIELD),
      )
      if (!current) form.setFieldValue(REGISTRATION_ZERO_AMOUNT_COUNTRY_FIELD, DEFAULT_REGISTRATION_ZERO_AMOUNT_COUNTRY)
    } finally {
      setLoading(false)
    }
  }, [form])

  useEffect(() => {
    if (!enabled) return
    void loadOptions()
  }, [enabled, loadOptions])

  useEffect(() => {
    if (normalizeRegistrationEligibilityCountry(selectedCountry)) {
      writeRegistrationEligibilityCountry(selectedCountry)
    }
  }, [selectedCountry])

  if (!enabled) return null

  return (
    <>
      <Form.Item
        name={REGISTRATION_ZERO_AMOUNT_COUNTRY_FIELD}
        label="注册后 0 元检测国家"
        initialValue={DEFAULT_REGISTRATION_ZERO_AMOUNT_COUNTRY}
        rules={[
          { required: true, message: '请选择注册后 0 元检测国家' },
          { pattern: /^[A-Za-z]{2}$/, message: '请选择有效的两位国家代码' },
        ]}
        extra="注册成功后自动检测 0 元试用资格；只影响后置检测的结账国家，不改变注册代理出口。"
      >
        <Select
          showSearch
          optionFilterProp="label"
          loading={loading}
          placeholder="选择国家"
          options={options}
          disabled={options.length === 0 && loading}
        />
      </Form.Item>
      {error ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="0 元检测国家目录读取失败"
          description={(
            <Space size={8} wrap>
              <Typography.Text type="secondary">{error}</Typography.Text>
              <Button size="small" icon={<ReloadOutlined />} onClick={() => { void loadOptions() }}>
                重试
              </Button>
            </Space>
          )}
        />
      ) : null}
    </>
  )
}
