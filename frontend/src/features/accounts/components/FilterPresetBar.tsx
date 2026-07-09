import React from 'react';
import { Space, Select, Button, Dropdown, Typography, Input, Popover, Tag } from 'antd';
import { FilterOutlined, PushpinOutlined, SaveOutlined, SettingOutlined, SyncOutlined } from '@ant-design/icons';
import type { GlobalToken } from 'antd/es/theme/interface';
import type { AccountFilterPreset, AccountFilterPresetFilters } from '../../../pages/Accounts';
import { STATUS_COLORS, buildAccountFilterPresetSummary, statusLabel } from '../../../pages/Accounts';

const { Text } = Typography;

export interface FilterPresetSelectedAccountItem {
  id?: string | number;
  email?: string;
  status?: string;
}

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
  total: number;
  activeFilterPresetDirty: boolean;
  filterPresetSaving: boolean;
  applyFilterPreset: (preset: AccountFilterPreset) => void;
  clearFilterPreset: () => void;
  openCreateCurrentFilterPreset: () => void;
  setFilterPresetManageOpen: (open: boolean) => void;
  loadFilterPresets: (silent?: boolean) => void;
  overwriteActiveFilterPreset: () => void;
  openCopyFilterPreset: (preset: AccountFilterPreset) => void;
  selectedAccountItems?: FilterPresetSelectedAccountItem[];
  removeSelectedAccount?: (id: string) => void;
  clearSelectedAccounts?: () => void;
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
  total,
  activeFilterPresetDirty,
  filterPresetSaving,
  applyFilterPreset,
  clearFilterPreset,
  openCreateCurrentFilterPreset,
  setFilterPresetManageOpen,
  loadFilterPresets,
  overwriteActiveFilterPreset,
  openCopyFilterPreset,
  selectedAccountItems = [],
  removeSelectedAccount,
  clearSelectedAccounts,
  mobileFilterControls,
}) => {
  const activeSummary = activeFilterPreset
    ? (activeFilterPreset.summary || buildAccountFilterPresetSummary(activeFilterPreset.filters))
    : ''
  const currentSummary = buildAccountFilterPresetSummary(currentFilterPresetFilters)
  const selectedCount = selectedAccountItems.length
  const accountTags = (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, maxHeight: isMobile ? 180 : 240, overflow: 'auto', maxWidth: isMobile ? 300 : 430 }}>
      {selectedAccountItems.map((account) => {
        const id = String(account?.id || '')
        const email = String(account?.email || '').trim()
        const status = String(account?.status || '').trim()
        const title = email || `账号 ${id}`
        return (
          <Tag
            key={id}
            closable={Boolean(removeSelectedAccount)}
            onClose={(event) => {
              event.preventDefault()
              if (id) removeSelectedAccount?.(id)
            }}
            color={STATUS_COLORS[status] || 'default'}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 4,
              maxWidth: '100%',
              marginInlineEnd: 0,
              padding: '2px 6px',
            }}
          >
            <span
              title={title}
              style={{
                display: 'inline-block',
                maxWidth: isMobile ? 170 : 230,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                verticalAlign: 'bottom',
              }}
            >
              {title}
            </span>
            <Text type="secondary" style={{ fontSize: 11 }}>
              ID {id}{status ? ` · ${statusLabel(status)}` : ''}
            </Text>
          </Tag>
        )
      })}
      {selectedAccountItems.length === 0 ? <Text type="secondary">暂无选中账号</Text> : null}
    </div>
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
        boxShadow: token.boxShadowTertiary,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
      }}
    >
      <div className="accounts-filter-preset-main-row">
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
        <div className="accounts-filter-preset-main-controls">
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
              label: `${preset.name}${preset.built_in ? ' · 内置' : ''}`,
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
                { key: 'save', icon: <SaveOutlined />, label: '保存当前筛选', onClick: openCreateCurrentFilterPreset },
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
        </div>
      </div>

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

      {mobileFilterControls}

      <div className="accounts-filter-preset-summary-row">
        <div className="accounts-filter-preset-summary-left">
          <Text
            className="accounts-filter-preset-summary-copy"
            type="secondary"
            style={{ fontSize: 12 }}
            ellipsis={{ tooltip: activeFilterPreset ? activeSummary : currentSummary }}
          >
            {activeFilterPreset
              ? `当前组合：${activeFilterPreset.name}${activeFilterPreset.built_in ? ' (内置)' : ''}${activeFilterPresetDirty ? ' · 已修改' : ''}`
              : `筛选：${currentSummary}`}
          </Text>
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
        <div className="accounts-filter-preset-summary-right">
          <Text strong style={{ fontSize: 13 }}>总数：{total}</Text>
          <Text type="secondary">/</Text>
          {selectedCount > 0 ? (
            <>
              <Popover content={accountTags} title="已选账号列表" trigger={['click']} placement="bottomRight">
                <Tag color="processing" style={{ cursor: 'pointer', marginInlineEnd: 0 }}>已选：{selectedCount}</Tag>
              </Popover>
              <Button size="small" type="link" onClick={clearSelectedAccounts} style={{ padding: 0 }}>
                清空选择
              </Button>
            </>
          ) : (
            <Text strong style={{ fontSize: 13 }}>已选：0</Text>
          )}
        </div>
      </div>
    </div>
  )
}
