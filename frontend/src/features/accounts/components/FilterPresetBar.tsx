import React from 'react'
import { Button, Dropdown, Input, Select, Tooltip, Typography } from 'antd'
import {
  CheckOutlined,
  FilterOutlined,
  LockOutlined,
  PlusOutlined,
  PushpinOutlined,
  SaveOutlined,
  SettingOutlined,
  SyncOutlined,
} from '@ant-design/icons'
import type { GlobalToken } from 'antd/es/theme/interface'
import type { AccountFilterPreset, AccountFilterPresetFilters } from '../../../pages/Accounts'
import { buildAccountFilterPresetSummary } from '../../../pages/Accounts'
import {
  SubscriptionStatusCounts,
} from './SubscriptionStatusCounts'
import type { SubscriptionStatusCountsValue } from '../subscriptionStatusCounts'

const { Text } = Typography
const UNASSIGNED_SCOPE_VALUE = '__unassigned__'

export interface FilterPresetBarProps {
  isMobile: boolean
  token: GlobalToken
  search: string
  onSearchChange: (value: string) => void
  onSearchSubmit: (value: string) => void
  filterPresetLoading: boolean
  activeFilterPresetId: string | null
  filterPresets: AccountFilterPreset[]
  pinnedFilterPresets: AccountFilterPreset[]
  activeFilterPreset: AccountFilterPreset | null
  fixedGroups: AccountFilterPreset[]
  pinnedFixedGroups: AccountFilterPreset[]
  activeFixedGroupId: string
  secondaryScope: 'unassigned' | 'fixed'
  currentFilterPresetFilters: AccountFilterPresetFilters
  activeFilterPresetDirty: boolean
  filterPresetSaving: boolean
  applyFilterPreset: (preset: AccountFilterPreset) => void
  applyUnassignedScope: () => void
  applyFixedGroup: (group: AccountFilterPreset) => void
  clearFilterPreset: () => void
  openCreateCurrentFilterPreset: () => void
  openCreateFixedGroup: () => void
  setFilterPresetManageOpen: (open: boolean) => void
  loadFilterPresets: (silent?: boolean) => void
  overwriteActiveFilterPreset: () => void
  openCopyFilterPreset: (preset: AccountFilterPreset) => void
  selectedAccountCount: number
  selectedAccountsControl?: React.ReactNode
  mobileFilterControls?: React.ReactNode
}

export const FilterPresetBar: React.FC<FilterPresetBarProps> = ({
  isMobile,
  token,
  search,
  onSearchChange,
  onSearchSubmit,
  filterPresetLoading,
  activeFilterPresetId,
  filterPresets,
  pinnedFilterPresets,
  activeFilterPreset,
  fixedGroups,
  pinnedFixedGroups,
  activeFixedGroupId,
  secondaryScope,
  currentFilterPresetFilters,
  activeFilterPresetDirty,
  filterPresetSaving,
  applyFilterPreset,
  applyUnassignedScope,
  applyFixedGroup,
  clearFilterPreset,
  openCreateCurrentFilterPreset,
  openCreateFixedGroup,
  setFilterPresetManageOpen,
  loadFilterPresets,
  overwriteActiveFilterPreset,
  openCopyFilterPreset,
  selectedAccountCount,
  selectedAccountsControl,
  mobileFilterControls,
}) => {
  const currentSummary = buildAccountFilterPresetSummary(currentFilterPresetFilters)
  const hasCurrentFilter = currentSummary !== '无筛选条件'
  const shouldRenderFilterSummary = !activeFilterPreset && hasCurrentFilter
  const shouldRenderDirtyActions = Boolean(activeFilterPreset && activeFilterPresetDirty && secondaryScope === 'unassigned')
  const shouldRenderSummary = isMobile || shouldRenderFilterSummary || shouldRenderDirtyActions
  const secondaryValue = secondaryScope === 'fixed' && activeFixedGroupId
    ? activeFixedGroupId
    : UNASSIGNED_SCOPE_VALUE
  const canCreateFixedGroup = Boolean(
    activeFilterPreset
    && secondaryScope === 'unassigned'
    && selectedAccountCount > 0,
  )
  const createFixedGroupTooltip = !activeFilterPreset
    ? '请先选择条件组合'
    : secondaryScope !== 'unassigned'
      ? '请先切换到未固定'
      : selectedAccountCount > 0
        ? '新建固定账号组合'
        : '请先勾选账号'

  const renderShortcut = (
    preset: AccountFilterPreset,
    active: boolean,
    onClick: () => void,
    subscriptionCounts?: SubscriptionStatusCountsValue,
  ) => {
    const button = (
      <Button
        key={preset.id}
        className="accounts-filter-preset-pinned-button"
        size="small"
        type={active ? 'primary' : 'default'}
        ghost={active}
        icon={active ? <CheckOutlined /> : preset.pinned ? <PushpinOutlined /> : undefined}
        aria-pressed={active}
        title={subscriptionCounts ? undefined : preset.name}
        onClick={onClick}
      >
        {preset.name}
      </Button>
    )
    if (!subscriptionCounts) return button
    return (
      <Tooltip
        key={preset.id}
        mouseEnterDelay={0.15}
        title={<SubscriptionStatusCounts counts={subscriptionCounts} labels="short" />}
      >
        {button}
      </Tooltip>
    )
  }

  const fixedGroupLabel = (group: AccountFilterPreset) => (
    <Tooltip
      mouseEnterDelay={0.15}
      title={<SubscriptionStatusCounts counts={group.subscription_counts} labels="short" />}
    >
      <span className="accounts-fixed-group-option-label">{group.name}</span>
    </Tooltip>
  )

  return (
    <div
      className={`accounts-filter-preset-bar ${isMobile ? 'accounts-filter-preset-bar-mobile' : ''}`}
      style={{
        flex: '0 0 auto',
        marginBottom: isMobile ? 8 : 12,
        padding: isMobile ? 10 : '8px 10px',
        border: `1px solid ${token.colorBorderSecondary}`,
        borderRadius: 8,
        background: token.colorBgContainer,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
      }}
    >
      {isMobile ? (
        <div className="accounts-filter-preset-search">
          <Input.Search
            allowClear
            size="small"
            placeholder="搜索邮箱"
            value={search}
            style={{ width: '100%' }}
            onChange={(event) => onSearchChange(event.target.value)}
            onSearch={(value) => onSearchSubmit(String(value || '').trim())}
          />
        </div>
      ) : null}

      <div className="accounts-filter-scope-row">
        <div className="accounts-filter-preset-label">
          <FilterOutlined style={{ color: token.colorPrimary }} />
          <Text strong style={{ fontSize: 13 }}>条件筛选组合</Text>
          <Dropdown
            menu={{
              items: [
                { key: 'save', icon: <SaveOutlined />, label: '保存当前条件组合', onClick: openCreateCurrentFilterPreset },
                { key: 'manage', icon: <SettingOutlined />, label: '管理组合', onClick: () => setFilterPresetManageOpen(true) },
                { type: 'divider' },
                { key: 'refresh', icon: <SyncOutlined spin={filterPresetLoading} />, label: '刷新组合', onClick: () => void loadFilterPresets(false) },
              ],
            }}
          >
            <Button
              className="accounts-filter-preset-label-action"
              size="small"
              type="text"
              icon={<SettingOutlined />}
              title="管理组合"
              aria-label="管理组合"
            />
          </Dropdown>
        </div>
        <Select
          className="accounts-filter-preset-select"
          allowClear
          showSearch
          size="small"
          placeholder={filterPresetLoading ? '读取中...' : '选择条件组合'}
          loading={filterPresetLoading}
          value={activeFilterPresetId || undefined}
          optionFilterProp="label"
          options={filterPresets.map((preset) => ({ value: preset.id, label: preset.name }))}
          onChange={(presetId) => {
            const preset = filterPresets.find((item) => item.id === presetId)
            if (preset) applyFilterPreset(preset)
            else clearFilterPreset()
          }}
        />
        {!isMobile && pinnedFilterPresets.length > 0 ? (
          <div className="accounts-filter-preset-pinned-row">
            <div className="accounts-filter-preset-pinned-scroll">
              {pinnedFilterPresets.map((preset) => renderShortcut(
                preset,
                preset.id === activeFilterPresetId,
                () => applyFilterPreset(preset),
              ))}
            </div>
          </div>
        ) : null}
      </div>

      <div className="accounts-filter-scope-row">
        <div className="accounts-filter-preset-label">
          <LockOutlined style={{ color: activeFilterPreset ? token.colorPrimary : token.colorTextDisabled }} />
          <Text strong style={{ fontSize: 13 }}>固定账号组合</Text>
          <Tooltip title={createFixedGroupTooltip}>
            <span className="accounts-filter-preset-label-action-wrap">
              <Button
                className="accounts-filter-preset-label-action"
                size="small"
                type="text"
                icon={<PlusOutlined />}
                aria-label="新建固定账号组合"
                disabled={!canCreateFixedGroup}
                onClick={openCreateFixedGroup}
              />
            </span>
          </Tooltip>
        </div>
        <Select
          className="accounts-filter-preset-select"
          showSearch
          size="small"
          disabled={!activeFilterPreset}
          placeholder="先选择条件组合"
          value={activeFilterPreset ? secondaryValue : undefined}
          optionFilterProp="searchText"
          options={[
            { value: UNASSIGNED_SCOPE_VALUE, label: '未固定', searchText: '未固定' },
            ...fixedGroups.map((group) => ({
              value: group.id,
              label: fixedGroupLabel(group),
              searchText: group.name,
            })),
          ]}
          onChange={(value) => {
            if (value === UNASSIGNED_SCOPE_VALUE) {
              applyUnassignedScope()
              return
            }
            const group = fixedGroups.find((item) => item.id === value)
            if (group) applyFixedGroup(group)
          }}
        />
        {!isMobile && activeFilterPreset ? (
          <div className="accounts-filter-preset-pinned-row">
            <div className="accounts-filter-preset-pinned-scroll">
              <Button
                className="accounts-filter-preset-pinned-button"
                size="small"
                type={secondaryScope === 'unassigned' ? 'primary' : 'default'}
                ghost={secondaryScope === 'unassigned'}
                icon={secondaryScope === 'unassigned' ? <CheckOutlined /> : undefined}
                aria-pressed={secondaryScope === 'unassigned'}
                onClick={applyUnassignedScope}
              >
                未固定
              </Button>
              {pinnedFixedGroups.map((group) => renderShortcut(
                group,
                secondaryScope === 'fixed' && group.id === activeFixedGroupId,
                () => applyFixedGroup(group),
                group.subscription_counts,
              ))}
            </div>
          </div>
        ) : null}
      </div>

      {mobileFilterControls}

      {shouldRenderSummary ? (
        <div className="accounts-filter-preset-summary-row">
          {shouldRenderFilterSummary || shouldRenderDirtyActions ? (
            <div className="accounts-filter-preset-summary-left">
              {shouldRenderFilterSummary ? (
                <Text className="accounts-filter-preset-summary-copy" type="secondary" style={{ fontSize: 12 }} ellipsis={{ tooltip: currentSummary }}>
                  {`筛选：${currentSummary}`}
                </Text>
              ) : null}
              {shouldRenderDirtyActions ? (
                <div className="accounts-filter-preset-summary-actions">
                  <Button size="small" type="link" style={{ padding: 0 }} loading={filterPresetSaving} onClick={() => void overwriteActiveFilterPreset()}>
                    覆盖条件
                  </Button>
                  <Button size="small" type="link" style={{ padding: 0 }} onClick={() => openCopyFilterPreset(activeFilterPreset!)}>
                    另存为
                  </Button>
                  <Button size="small" type="link" danger style={{ padding: 0 }} onClick={() => applyFilterPreset(activeFilterPreset!)}>
                    还原
                  </Button>
                </div>
              ) : null}
            </div>
          ) : null}
          {isMobile ? (
            <div className="accounts-filter-preset-summary-right accounts-filter-preset-mobile-selected-accounts">
              {selectedAccountsControl}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
