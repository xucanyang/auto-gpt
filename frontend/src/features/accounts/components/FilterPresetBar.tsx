import React from 'react';
import { Space, Select, Button, Dropdown, Typography, Input } from 'antd';
import { FilterOutlined, PushpinOutlined, SaveOutlined, SettingOutlined, SyncOutlined } from '@ant-design/icons';
import type { GlobalToken } from 'antd/es/theme/interface';
import type { AccountFilterPreset, AccountFilterPresetFilters } from '../../../pages/Accounts';
import { buildAccountFilterPresetSummary } from '../../../pages/Accounts';

const { Text } = Typography;

export interface FilterPresetBarProps {
  isMobile: boolean;
  token: GlobalToken;
  search: string;
  onSearchChange: (value: string) => void;
  onSearchSubmit: (value: string) => void;
  filterPresetLoading: boolean;
  activeFilterPresetId: string | null;
  filterPresets: AccountFilterPreset[];
  pinnedFilterPresets: AccountFilterPreset[];
  activeFilterPreset: AccountFilterPreset | null;
  currentFilterPresetFilters: AccountFilterPresetFilters;
  activeFilterPresetDirty: boolean;
  filterPresetSaving: boolean;
  applyFilterPreset: (preset: AccountFilterPreset) => void;
  clearFilterPreset: () => void;
  openCreateCurrentFilterPreset: () => void;
  setFilterPresetManageOpen: (open: boolean) => void;
  loadFilterPresets: (silent?: boolean) => void;
  overwriteActiveFilterPreset: () => void;
  openCopyFilterPreset: (preset: AccountFilterPreset) => void;
  selectedAccountCount: number;
  selectedAccountsControl?: React.ReactNode;
  mobileFilterControls?: React.ReactNode;
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
  currentFilterPresetFilters,
  activeFilterPresetDirty,
  filterPresetSaving,
  applyFilterPreset,
  clearFilterPreset,
  openCreateCurrentFilterPreset,
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
  const shouldRenderDirtyActions = Boolean(activeFilterPreset && activeFilterPresetDirty)
  const shouldRenderSummary = isMobile || shouldRenderFilterSummary || shouldRenderDirtyActions

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
        boxShadow: token.boxShadowTertiary,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
      }}
    >
      <div className={`accounts-filter-preset-main-row ${isMobile ? 'accounts-filter-preset-main-row-mobile' : 'accounts-filter-preset-main-row-desktop'}`}>
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
        <div className={`accounts-filter-preset-main-controls ${isMobile ? 'accounts-filter-preset-main-controls-mobile' : 'accounts-filter-preset-main-controls-desktop'}`}>
          <div className="accounts-filter-preset-label">
            <FilterOutlined style={{ color: token.colorPrimary }} />
            <Text strong style={{ fontSize: 13 }}>筛选组合</Text>
          </div>
          <Select
            className="accounts-filter-preset-select"
            allowClear
            showSearch
            size="small"
            placeholder={filterPresetLoading ? '读取中...' : '选择后应用'}
            loading={filterPresetLoading}
            value={activeFilterPresetId || undefined}
            style={{ width: 180 }}
            optionFilterProp="label"
            options={filterPresets.map((preset) => ({
              value: preset.id,
              label: `${preset.name}${preset.mode === 'fixed' ? ` · 固定 ${preset.account_count}` : ''}${preset.built_in ? ' · 内置' : ''}`,
            }))}
            onChange={(presetId) => {
              const preset = filterPresets.find((item) => item.id === presetId)
              if (preset) applyFilterPreset(preset)
              else clearFilterPreset()
            }}
          />
          <Dropdown
            menu={{
              items: [
                {
                  key: 'save',
                  icon: <SaveOutlined />,
                  label: selectedAccountCount > 0 ? `保存已选账号 (${selectedAccountCount})` : '保存当前筛选',
                  onClick: openCreateCurrentFilterPreset,
                },
                { key: 'manage', icon: <SettingOutlined />, label: '管理筛选组合', onClick: () => setFilterPresetManageOpen(true) },
                { type: 'divider' },
                { key: 'refresh', icon: <SyncOutlined spin={filterPresetLoading} />, label: '刷新组合', onClick: () => void loadFilterPresets(false) },
              ]
            }}
          >
            <Button
              className="accounts-filter-preset-manage"
              size="small"
              type="text"
              icon={<SettingOutlined />}
              title="管理筛选组合"
              aria-label="管理筛选组合"
            />
          </Dropdown>
          {pinnedFilterPresets.length > 0 && !isMobile ? (
            <div className="accounts-filter-preset-pinned-row">
              <div className="accounts-filter-preset-pinned-label">
                <PushpinOutlined />
                <Text type="secondary">置顶</Text>
              </div>
              <div className="accounts-filter-preset-pinned-scroll">
                {pinnedFilterPresets.map((preset) => {
                  const active = preset.id === activeFilterPresetId
                  return (
                    <Button
                      key={preset.id}
                      className="accounts-filter-preset-pinned-button"
                      size="small"
                      type={active ? 'primary' : 'default'}
                      ghost={active}
                      icon={preset.pinned ? <PushpinOutlined /> : undefined}
                      onClick={() => {
                        if (active) clearFilterPreset()
                        else applyFilterPreset(preset)
                      }}
                    >
                      {preset.name}
                    </Button>
                  )
                })}
              </div>
            </div>
          ) : null}
        </div>
      </div>

      {mobileFilterControls}

      {shouldRenderSummary ? (
        <div className="accounts-filter-preset-summary-row">
          {shouldRenderFilterSummary || shouldRenderDirtyActions ? (
            <div className="accounts-filter-preset-summary-left">
              {activeFilterPreset?.mode === 'fixed' && activeFilterPresetDirty ? (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  固定成员：已保存 {activeFilterPreset.account_count}，当前已选 {selectedAccountCount}
                </Text>
              ) : null}
              {shouldRenderFilterSummary ? (
                <Text
                  className="accounts-filter-preset-summary-copy"
                  type="secondary"
                  style={{ fontSize: 12 }}
                  ellipsis={{ tooltip: currentSummary }}
                >
                  {`筛选：${currentSummary}`}
                </Text>
              ) : null}
              {activeFilterPreset && activeFilterPresetDirty ? (
                <Space className="accounts-filter-preset-summary-actions" size={4}>
                  <Button size="small" type="link" style={{ padding: 0 }} loading={filterPresetSaving} onClick={() => void overwriteActiveFilterPreset()}>
                    覆盖保存
                  </Button>
                  <Button size="small" type="link" style={{ padding: 0 }} onClick={() => openCopyFilterPreset(activeFilterPreset)}>
                    另存为
                  </Button>
                  <Button size="small" type="link" danger style={{ padding: 0 }} onClick={() => applyFilterPreset(activeFilterPreset)}>
                    还原
                  </Button>
                </Space>
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
