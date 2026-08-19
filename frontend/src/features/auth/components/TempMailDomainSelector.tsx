import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  Alert,
  Button,
  Checkbox,
  Collapse,
  Form,
  Skeleton,
  Tag,
  Tooltip,
  message,
} from 'antd'
import type { FormInstance } from 'antd'
import { ReloadOutlined, SaveOutlined, WarningOutlined } from '@ant-design/icons'
import { normalizeDomainList } from '@/lib/domainList'
import {
  TEMPMAIL_PREFERRED_DOMAINS_CHANGED_EVENT,
  loadTempMailPreferredDomains,
  resolveTempMailPreferredDomains,
  sameTempMailDomainOrder,
  saveTempMailPreferredDomains,
  tempMailPreferredDomainsStorageKey,
} from '@/lib/tempMailDomainPreferences'
import { apiFetch } from '@/lib/utils'

type TempMailDomainOption = {
  domain: string
  available?: boolean
  status?: string
  dns_status?: string
}

type TempMailDomainSelectorProps = {
  form: FormInstance
  active?: boolean
  preferenceScope: string
  fixedFieldName?: string
  preferredFieldName?: string
  primaryFieldName?: string
}

type BoundSelectionSurfaceProps = {
  value?: string[]
  onChange?: (value: string[]) => void
  children: ReactNode
}

function BoundSelectionSurface({ children }: BoundSelectionSurfaceProps) {
  return <div className="tempmail-domain-selector">{children}</div>
}

function HiddenDomainValue() {
  return null
}

function normalizeDomainOptions(input: unknown): TempMailDomainOption[] {
  const byDomain = new Map<string, TempMailDomainOption>()
  if (!Array.isArray(input)) return []
  input.forEach((raw) => {
    const domain = normalizeDomainList([(raw as TempMailDomainOption)?.domain])[0]
    if (!domain) return
    const item = raw as TempMailDomainOption
    byDomain.set(domain, {
      domain,
      available: item.available !== false,
      status: String(item.status || '').trim().toLowerCase(),
      dns_status: String(item.dns_status || '').trim().toLowerCase(),
    })
  })
  return Array.from(byDomain.values()).sort((left, right) => left.domain.localeCompare(right.domain))
}

function unavailableReason(option: TempMailDomainOption | undefined) {
  if (!option) return 'TempMail API 当前未返回此域名'
  if (option.dns_status && !['present', 'ready', 'active'].includes(option.dns_status)) {
    return `DNS 状态：${option.dns_status}`
  }
  if (option.status && !['active', 'ready', 'enabled'].includes(option.status)) {
    return `域名状态：${option.status}`
  }
  return '域名当前不可用'
}

export function TempMailDomainSelector({
  form,
  active = true,
  preferenceScope,
  fixedFieldName = 'tempmail_fixed_domains',
  preferredFieldName = 'tempmail_preferred_domains',
  primaryFieldName = 'tempmail_primary_domain',
}: TempMailDomainSelectorProps) {
  const watchedPreferredDomains = Form.useWatch(preferredFieldName, form)
  const preferredDomains = useMemo(
    () => normalizeDomainList(watchedPreferredDomains),
    [watchedPreferredDomains],
  )
  const [savedPreferredDomains, setSavedPreferredDomains] = useState<string[]>([])
  const [domains, setDomains] = useState<TempMailDomainOption[]>([])
  const [domainsResolved, setDomainsResolved] = useState(false)
  const [domainsLoading, setDomainsLoading] = useState(false)
  const [domainsError, setDomainsError] = useState('')
  const [allDomainsExpanded, setAllDomainsExpanded] = useState(false)
  const [initialized, setInitialized] = useState(false)
  const requestSequence = useRef(0)

  const domainMap = useMemo(
    () => new Map(domains.map((item) => [item.domain, item])),
    [domains],
  )
  const availableDomainSet = useMemo(
    () => new Set(domains.filter((item) => item.available !== false).map((item) => item.domain)),
    [domains],
  )
  const effectiveDomains = useMemo(
    () => domainsResolved
      ? preferredDomains.filter((domain) => availableDomainSet.has(domain))
      : preferredDomains,
    [availableDomainSet, domainsResolved, preferredDomains],
  )
  const unavailablePreferredCount = preferredDomains.length - effectiveDomains.length
  const preferenceDirty = initialized
    && !sameTempMailDomainOrder(preferredDomains, savedPreferredDomains)

  const allDomainOptions = useMemo(() => {
    const byDomain = new Map(domains.map((item) => [item.domain, item]))
    preferredDomains.forEach((domain) => {
      if (!byDomain.has(domain)) {
        byDomain.set(domain, {
          domain,
          available: domainsResolved ? false : true,
          status: domainsResolved ? 'missing' : '',
        })
      }
    })
    return Array.from(byDomain.values()).sort((left, right) => left.domain.localeCompare(right.domain))
  }, [domains, domainsResolved, preferredDomains])

  const loadDomains = useCallback(async (silent = false) => {
    const requestId = ++requestSequence.current
    setDomainsLoading(true)
    setDomainsError('')
    try {
      const data = await apiFetch('/config/tempmail/domains', {
        method: 'POST',
        body: JSON.stringify({ include_inactive: true }),
      })
      if (requestId !== requestSequence.current) return
      const nextDomains = normalizeDomainOptions(data?.domains)
      setDomains(nextDomains)
      setDomainsResolved(true)
      if (!silent) {
        const availableCount = nextDomains.filter((item) => item.available !== false).length
        message.success(`已加载 ${availableCount} 个可用域名`)
      }
    } catch (error: unknown) {
      if (requestId !== requestSequence.current) return
      const detail = error instanceof Error ? error.message : '读取 TempMail 域名失败'
      setDomainsError(detail)
      if (!silent) message.error(detail)
    } finally {
      if (requestId === requestSequence.current) setDomainsLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!active) {
      requestSequence.current += 1
      setInitialized(false)
      setAllDomainsExpanded(false)
      return
    }

    let cancelled = false
    queueMicrotask(() => {
      if (cancelled) return
      const fallback = normalizeDomainList(
        form.getFieldValue(preferredFieldName) || form.getFieldValue(fixedFieldName),
      )
      const initial = resolveTempMailPreferredDomains(preferenceScope, fallback)
      form.setFieldsValue({
        [preferredFieldName]: initial,
        [fixedFieldName]: initial,
        [primaryFieldName]: initial[0] || '',
      })
      setSavedPreferredDomains(loadTempMailPreferredDomains(preferenceScope) ?? initial)
      setAllDomainsExpanded(initial.length === 0)
      setInitialized(true)
    })
    void loadDomains(true)
    return () => {
      cancelled = true
    }
  }, [active, fixedFieldName, form, loadDomains, preferenceScope, preferredFieldName, primaryFieldName])

  useEffect(() => {
    if (!active || !initialized) return
    const currentFixedDomains = normalizeDomainList(form.getFieldValue(fixedFieldName))
    const currentPrimaryDomain = normalizeDomainList([form.getFieldValue(primaryFieldName)])[0] || ''
    if (!sameTempMailDomainOrder(currentFixedDomains, effectiveDomains)) {
      form.setFieldValue(fixedFieldName, effectiveDomains)
    }
    if (currentPrimaryDomain !== (effectiveDomains[0] || '')) {
      form.setFieldValue(primaryFieldName, effectiveDomains[0] || '')
    }
    if (effectiveDomains.length === 0) setAllDomainsExpanded(true)
  }, [active, effectiveDomains, fixedFieldName, form, initialized, primaryFieldName])

  useEffect(() => {
    if (typeof window === 'undefined') return
    const normalizedScope = String(preferenceScope || 'chatgpt').trim().toLowerCase() || 'chatgpt'
    const syncSavedPreference = (event: Event) => {
      const detail = (event as CustomEvent<{ scope?: string; domains?: unknown }>).detail
      if (String(detail?.scope || '').trim().toLowerCase() !== normalizedScope) return
      setSavedPreferredDomains(normalizeDomainList(detail?.domains))
    }
    const syncStoragePreference = (event: StorageEvent) => {
      if (event.key !== tempMailPreferredDomainsStorageKey(normalizedScope)) return
      setSavedPreferredDomains(loadTempMailPreferredDomains(normalizedScope) ?? [])
    }
    window.addEventListener(TEMPMAIL_PREFERRED_DOMAINS_CHANGED_EVENT, syncSavedPreference)
    window.addEventListener('storage', syncStoragePreference)
    return () => {
      window.removeEventListener(TEMPMAIL_PREFERRED_DOMAINS_CHANGED_EVENT, syncSavedPreference)
      window.removeEventListener('storage', syncStoragePreference)
    }
  }, [preferenceScope])

  const updatePreferredDomains = (nextDomains: unknown) => {
    const normalized = normalizeDomainList(nextDomains)
    const nextEffectiveDomains = domainsResolved
      ? normalized.filter((domain) => availableDomainSet.has(domain))
      : normalized
    form.setFieldsValue({
      [preferredFieldName]: normalized,
      [fixedFieldName]: nextEffectiveDomains,
      [primaryFieldName]: nextEffectiveDomains[0] || '',
    })
  }

  const togglePreferredDomain = (domain: string, checked: boolean) => {
    updatePreferredDomains(
      checked
        ? [...preferredDomains, domain]
        : preferredDomains.filter((item) => item !== domain),
    )
  }

  const savePreferredDomains = () => {
    if (!saveTempMailPreferredDomains(preferenceScope, preferredDomains)) {
      message.error('优选域名保存失败，请检查浏览器本地存储权限')
      return
    }
    setSavedPreferredDomains(preferredDomains)
    message.success(`已保存 ${preferredDomains.length} 个优选域名`)
  }

  const renderDomainName = (domain: string) => (
    <Tooltip title={domain} mouseEnterDelay={0.4}>
      <span className="tempmail-domain-name">{domain}</span>
    </Tooltip>
  )

  return (
    <>
      <Form.Item name={preferredFieldName} hidden>
        <HiddenDomainValue />
      </Form.Item>
      <Form.Item
        name={fixedFieldName}
        rules={[
          {
            validator: (_, value) => {
              if (normalizeDomainList(value).length > 0) return Promise.resolve()
              setAllDomainsExpanded(true)
              return Promise.reject(new Error('请选择至少一个可用的 TempMail 优选域名'))
            },
          },
        ]}
        style={{ marginBottom: 16 }}
      >
        <BoundSelectionSurface>
          <section className="tempmail-domain-preferred" aria-labelledby="tempmail-preferred-domains-title">
            <div className="tempmail-domain-section-head">
              <div className="tempmail-domain-section-title-row">
                <span id="tempmail-preferred-domains-title" className="tempmail-domain-section-title">优选域名</span>
                <Tag color={effectiveDomains.length > 0 ? 'blue' : 'default'}>
                  本次使用 {effectiveDomains.length}
                </Tag>
                {unavailablePreferredCount > 0 ? (
                  <Tag color="warning">不可用 {unavailablePreferredCount}</Tag>
                ) : null}
                {preferenceDirty ? <Tag color="gold">未保存</Tag> : null}
              </div>
              <Button
                size="small"
                icon={<SaveOutlined />}
                disabled={!preferenceDirty}
                onClick={savePreferredDomains}
              >
                保存优选
              </Button>
            </div>

            {preferredDomains.length > 0 ? (
              <div className="tempmail-domain-grid tempmail-domain-preferred-grid">
                {preferredDomains.map((domain) => {
                  const option = domainMap.get(domain)
                  const unavailable = domainsResolved && (!option || option.available === false)
                  return (
                    <div className="tempmail-domain-option" key={domain}>
                      <Checkbox
                        checked
                        onChange={(event) => togglePreferredDomain(domain, event.target.checked)}
                      >
                        {renderDomainName(domain)}
                      </Checkbox>
                      {unavailable ? (
                        <Tooltip title={unavailableReason(option)}>
                          <WarningOutlined
                            className="tempmail-domain-unavailable-icon"
                            aria-label="当前不可用"
                          />
                        </Tooltip>
                      ) : null}
                    </div>
                  )
                })}
              </div>
            ) : (
              <Alert
                type="info"
                showIcon
                message="暂无优选域名"
                description="请从下方全部域名中勾选；至少需要一个可用域名才能开始注册。"
              />
            )}
          </section>

          <Collapse
            ghost
            className="tempmail-domain-all-collapse"
            activeKey={allDomainsExpanded ? ['all-tempmail-domains'] : []}
            onChange={(keys) => {
              const nextKeys = Array.isArray(keys) ? keys : [keys]
              setAllDomainsExpanded(nextKeys.includes('all-tempmail-domains'))
            }}
            items={[
              {
                key: 'all-tempmail-domains',
                label: (
                  <div className="tempmail-domain-all-title">
                    <span>全部域名</span>
                    <Tag>{domainsResolved ? `${domains.length} 个` : '读取中'}</Tag>
                  </div>
                ),
                extra: (
                  <Tooltip title="刷新 TempMail 域名">
                    <Button
                      type="text"
                      size="small"
                      icon={<ReloadOutlined />}
                      loading={domainsLoading}
                      aria-label="刷新 TempMail 域名"
                      onClick={(event) => {
                        event.stopPropagation()
                        void loadDomains(false)
                      }}
                    />
                  </Tooltip>
                ),
                children: domainsLoading && !domainsResolved ? (
                  <div className="tempmail-domain-skeleton-grid" aria-label="正在加载可用域名">
                    {Array.from({ length: 6 }, (_, index) => (
                      <Skeleton.Button active block key={index} />
                    ))}
                  </div>
                ) : (
                  <>
                    {domainsError ? (
                      <Alert
                        type="warning"
                        showIcon
                        message="全部域名加载失败"
                        description={domainsError}
                        style={{ marginBottom: 10 }}
                      />
                    ) : null}
                    {allDomainOptions.length > 0 ? (
                      <div className="tempmail-domain-grid tempmail-domain-all-grid">
                        {allDomainOptions.map((option) => {
                          const checked = preferredDomains.includes(option.domain)
                          const unavailable = domainsResolved && option.available === false
                          return (
                            <div className="tempmail-domain-option" key={option.domain}>
                              <Checkbox
                                checked={checked}
                                disabled={unavailable && !checked}
                                onChange={(event) => togglePreferredDomain(option.domain, event.target.checked)}
                              >
                                {renderDomainName(option.domain)}
                              </Checkbox>
                              {unavailable ? (
                                <Tooltip title={unavailableReason(option)}>
                                  <WarningOutlined
                                    className="tempmail-domain-unavailable-icon"
                                    aria-label="当前不可用"
                                  />
                                </Tooltip>
                              ) : null}
                            </div>
                          )
                        })}
                      </div>
                    ) : domainsError ? null : (
                      <Alert type="warning" showIcon message="暂无可用域名" />
                    )}
                  </>
                ),
              },
            ]}
          />
        </BoundSelectionSurface>
      </Form.Item>
    </>
  )
}
