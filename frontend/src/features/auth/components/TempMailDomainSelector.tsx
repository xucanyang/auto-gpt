import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  Alert,
  Button,
  Checkbox,
  Collapse,
  Form,
  Skeleton,
  Space,
  Tag,
  Tooltip,
  message,
} from 'antd'
import type { FormInstance } from 'antd'
import {
  ClearOutlined,
  CloseOutlined,
  ReloadOutlined,
  SaveOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import { normalizeDomainList } from '@/lib/domainList'
import {
  TEMPMAIL_PREFERRED_DOMAINS_CHANGED_EVENT,
  loadTempMailPreferredDomains,
  resolveTempMailPreferredDomains,
  sameTempMailDomainOrder,
  saveTempMailPreferredDomains,
  tempMailPreferredDomainsStorageKey,
} from '@/lib/tempMailDomainPreferences'
import {
  clearTempMailCurrentSelection,
  clearTempMailPreferredSelection,
  normalizeTempMailDomainOptions,
  orderTempMailSelectedDomains,
  updateTempMailCurrentSelection,
  updateTempMailPreferredMembership,
  type TempMailDomainOption,
} from '@/lib/tempMailDomainSelection'
import { apiFetch } from '@/lib/utils'

type TempMailDomainSelectorProps = {
  form: FormInstance
  active?: boolean
  preferenceScope: string
  fixedFieldName?: string
  preferredFieldName?: string
  primaryFieldName?: string
}

type BoundSelectionSurfaceProps = {
  children: ReactNode
}

function BoundSelectionSurface({ children }: BoundSelectionSurfaceProps) {
  return <div className="tempmail-domain-selector">{children}</div>
}

function HiddenDomainValue() {
  return null
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
  const watchedSelectedDomains = Form.useWatch(fixedFieldName, form)
  const preferredDomains = useMemo(
    () => normalizeDomainList(watchedPreferredDomains),
    [watchedPreferredDomains],
  )
  const selectedDomains = useMemo(
    () => normalizeDomainList(watchedSelectedDomains),
    [watchedSelectedDomains],
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
  const availableDomains = useMemo(
    () => domains.filter((item) => item.available !== false).map((item) => item.domain),
    [domains],
  )
  const availableDomainSet = useMemo(() => new Set(availableDomains), [availableDomains])
  const preferredDomainSet = useMemo(() => new Set(preferredDomains), [preferredDomains])
  const effectiveSelectedDomains = useMemo(
    () => orderTempMailSelectedDomains(
      selectedDomains,
      preferredDomains,
      domainsResolved ? availableDomains : undefined,
    ),
    [availableDomains, domainsResolved, preferredDomains, selectedDomains],
  )
  const selectedDomainSet = useMemo(
    () => new Set(effectiveSelectedDomains),
    [effectiveSelectedDomains],
  )
  const unavailablePreferredCount = domainsResolved
    ? preferredDomains.filter((domain) => !availableDomainSet.has(domain)).length
    : 0
  const preferenceDirty = initialized
    && !sameTempMailDomainOrder(preferredDomains, savedPreferredDomains)

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
      const nextDomains = normalizeTempMailDomainOptions(data?.domains)
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
      const rawPreferredDomains = form.getFieldValue(preferredFieldName)
      const rawSelectedDomains = form.getFieldValue(fixedFieldName)
      const preferredFallback = rawPreferredDomains === undefined
        ? rawSelectedDomains
        : rawPreferredDomains
      const initialPreferredDomains = resolveTempMailPreferredDomains(
        preferenceScope,
        preferredFallback,
      )
      const initialSelectedDomains = orderTempMailSelectedDomains(
        rawSelectedDomains === undefined ? initialPreferredDomains : rawSelectedDomains,
        initialPreferredDomains,
      )
      form.setFieldsValue({
        [preferredFieldName]: initialPreferredDomains,
        [fixedFieldName]: initialSelectedDomains,
        [primaryFieldName]: initialSelectedDomains[0] || '',
      })
      setSavedPreferredDomains(
        loadTempMailPreferredDomains(preferenceScope) ?? initialPreferredDomains,
      )
      setAllDomainsExpanded(initialPreferredDomains.length === 0)
      setInitialized(true)
    })
    void loadDomains(true)
    return () => {
      cancelled = true
    }
  }, [active, fixedFieldName, form, loadDomains, preferenceScope, preferredFieldName, primaryFieldName])

  useEffect(() => {
    if (!active || !initialized) return
    const currentPrimaryDomain = normalizeDomainList([form.getFieldValue(primaryFieldName)])[0] || ''
    if (!sameTempMailDomainOrder(selectedDomains, effectiveSelectedDomains)) {
      form.setFieldValue(fixedFieldName, effectiveSelectedDomains)
    }
    if (currentPrimaryDomain !== (effectiveSelectedDomains[0] || '')) {
      form.setFieldValue(primaryFieldName, effectiveSelectedDomains[0] || '')
    }
  }, [
    active,
    effectiveSelectedDomains,
    fixedFieldName,
    form,
    initialized,
    primaryFieldName,
    selectedDomains,
  ])

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

  const togglePreferredMembership = (domain: string, checked: boolean) => {
    const next = updateTempMailPreferredMembership(
      preferredDomains,
      selectedDomains,
      domain,
      checked,
    )
    form.setFieldsValue({
      [preferredFieldName]: next.preferredDomains,
      [fixedFieldName]: next.selectedDomains,
      [primaryFieldName]: next.selectedDomains[0] || '',
    })
  }

  const toggleCurrentSelection = (domain: string, checked: boolean) => {
    const nextSelectedDomains = updateTempMailCurrentSelection(
      selectedDomains,
      preferredDomains,
      domain,
      checked,
    )
    form.setFieldsValue({
      [fixedFieldName]: nextSelectedDomains,
      [primaryFieldName]: nextSelectedDomains[0] || '',
    })
  }

  const clearPreferredDomains = () => {
    if (preferredDomains.length === 0) return
    const cleared = clearTempMailPreferredSelection()
    form.setFieldsValue({
      [preferredFieldName]: cleared.preferredDomains,
      [fixedFieldName]: cleared.selectedDomains,
      [primaryFieldName]: cleared.primaryDomain,
    })
    message.info('已清空优选域名；点击“保存优选”后持久生效')
  }

  const clearCurrentSelection = () => {
    if (selectedDomains.length === 0) return
    const cleared = clearTempMailCurrentSelection()
    form.setFieldsValue({
      [fixedFieldName]: cleared.selectedDomains,
      [primaryFieldName]: cleared.primaryDomain,
    })
    message.info('已清空本次使用域名')
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
      <Form.Item name={fixedFieldName} hidden>
        <HiddenDomainValue />
      </Form.Item>

      <BoundSelectionSurface>
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
                <Space size={2}>
                  <Button
                    type="text"
                    danger
                    size="small"
                    icon={<ClearOutlined />}
                    disabled={preferredDomains.length === 0}
                    onClick={(event) => {
                      event.stopPropagation()
                      clearPreferredDomains()
                    }}
                  >
                    清空优选
                  </Button>
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
                </Space>
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
                  {domains.length > 0 ? (
                    <div className="tempmail-domain-grid tempmail-domain-all-grid">
                      {domains.map((option, upstreamIndex) => {
                        const checked = preferredDomainSet.has(option.domain)
                        const unavailable = domainsResolved && option.available === false
                        return (
                          <div className="tempmail-domain-option" key={option.domain}>
                            <Checkbox
                              checked={checked}
                              disabled={unavailable && !checked}
                              onChange={(event) => togglePreferredMembership(
                                option.domain,
                                event.target.checked,
                              )}
                            >
                              <span className="tempmail-domain-label">
                                <span
                                  className="tempmail-domain-sequence"
                                  aria-label={`上游顺序 ${upstreamIndex + 1}`}
                                >
                                  {upstreamIndex + 1}.
                                </span>
                                {renderDomainName(option.domain)}
                              </span>
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

        <section
          className="tempmail-domain-preferred"
          aria-labelledby="tempmail-preferred-domains-title"
        >
          <div className="tempmail-domain-section-head">
            <div className="tempmail-domain-section-title-row">
              <span id="tempmail-preferred-domains-title" className="tempmail-domain-section-title">
                优选域名
              </span>
              <Tag color={effectiveSelectedDomains.length > 0 ? 'blue' : 'default'}>
                本次使用 {effectiveSelectedDomains.length}
              </Tag>
              {unavailablePreferredCount > 0 ? (
                <Tag color="warning">不可用 {unavailablePreferredCount}</Tag>
              ) : null}
              {preferenceDirty ? <Tag color="gold">未保存</Tag> : null}
            </div>
            <Space size={4} className="tempmail-domain-section-actions">
              <Button
                size="small"
                icon={<ClearOutlined />}
                disabled={selectedDomains.length === 0}
                onClick={clearCurrentSelection}
              >
                清空本次选择
              </Button>
              <Button
                size="small"
                icon={<SaveOutlined />}
                disabled={!preferenceDirty}
                onClick={savePreferredDomains}
              >
                保存优选
              </Button>
            </Space>
          </div>

          {preferredDomains.length > 0 ? (
            <div className="tempmail-domain-grid tempmail-domain-preferred-grid">
              {preferredDomains.map((domain) => {
                const option = domainMap.get(domain)
                const unavailable = domainsResolved && (!option || option.available === false)
                return (
                  <div className="tempmail-domain-option" key={domain}>
                    <Checkbox
                      checked={selectedDomainSet.has(domain)}
                      disabled={unavailable}
                      onChange={(event) => toggleCurrentSelection(domain, event.target.checked)}
                    >
                      {renderDomainName(domain)}
                    </Checkbox>
                    {unavailable ? (
                      <div className="tempmail-domain-option-actions">
                        <Tooltip title={unavailableReason(option)}>
                          <WarningOutlined
                            className="tempmail-domain-unavailable-icon"
                            aria-label="当前不可用"
                          />
                        </Tooltip>
                        {!option ? (
                          <Tooltip title="从优选域名移除">
                            <Button
                              type="text"
                              danger
                              size="small"
                              icon={<CloseOutlined />}
                              aria-label={`从优选域名移除 ${domain}`}
                              onClick={() => togglePreferredMembership(domain, false)}
                            />
                          </Tooltip>
                        ) : null}
                      </div>
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
              description="请从上方全部域名中勾选；开始注册前需在优选域名中选择至少一个可用域名。"
            />
          )}
        </section>
      </BoundSelectionSurface>
    </>
  )
}
