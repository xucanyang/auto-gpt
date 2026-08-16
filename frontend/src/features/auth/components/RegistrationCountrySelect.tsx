import { useCallback, useEffect, useState } from 'react'
import { Button, Select, Space, Tooltip, Typography } from 'antd'
import type { SelectProps } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'
import {
  normalizeRegistrationEligibilityCountryOptions,
  type RegistrationEligibilityCountryOption,
} from '@/lib/registrationEligibilityCountry'

let cachedCountryOptions: RegistrationEligibilityCountryOption[] = []
let pendingCountryOptions: Promise<RegistrationEligibilityCountryOption[]> | null = null

async function fetchCountryOptions(force = false): Promise<RegistrationEligibilityCountryOption[]> {
  if (!force && cachedCountryOptions.length > 0) return cachedCountryOptions
  if (!force && pendingCountryOptions) return pendingCountryOptions

  pendingCountryOptions = apiFetch('/tasks/chatgpt/zero-amount-eligibility/profile')
    .then((profile) => {
      const options = normalizeRegistrationEligibilityCountryOptions(profile?.billing_country_options)
      if (options.length === 0) throw new Error('服务端未返回可用的国家/币种目录')
      cachedCountryOptions = options
      return options
    })
    .finally(() => {
      pendingCountryOptions = null
    })
  return pendingCountryOptions
}

type RegistrationCountrySelectProps = Omit<
  SelectProps<string>,
  'loading' | 'options' | 'optionFilterProp' | 'showSearch'
>

export function RegistrationCountrySelect({
  placeholder = '选择国家',
  style,
  ...props
}: RegistrationCountrySelectProps) {
  const [options, setOptions] = useState<RegistrationEligibilityCountryOption[]>(cachedCountryOptions)
  const [loading, setLoading] = useState(cachedCountryOptions.length === 0)
  const [error, setError] = useState('')

  const loadOptions = useCallback(async (force = false) => {
    setLoading(true)
    setError('')
    try {
      setOptions(await fetchCountryOptions(force))
    } catch (cause: unknown) {
      const detail = cause instanceof Error ? cause.message : String(cause || '')
      setError(detail || '无法读取国家目录')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (options.length === 0) void loadOptions()
  }, [loadOptions, options.length])

  return (
    <div style={{ width: '100%' }}>
      <Space.Compact block>
        <Select
          {...props}
          showSearch
          optionFilterProp="label"
          loading={loading}
          options={options}
          placeholder={placeholder}
          style={{ flex: 1, minWidth: 0, ...style }}
          notFoundContent={loading ? '正在加载国家目录...' : error || '没有匹配的国家'}
        />
        <Tooltip title={error ? '国家目录读取失败，点击重试' : '刷新国家目录'}>
          <Button
            aria-label="刷新国家目录"
            icon={<ReloadOutlined />}
            loading={loading}
            onClick={() => { void loadOptions(true) }}
          />
        </Tooltip>
      </Space.Compact>
      {error ? (
        <Typography.Text type="danger" style={{ display: 'block', marginTop: 4, fontSize: 12 }}>
          {error}
        </Typography.Text>
      ) : null}
    </div>
  )
}
