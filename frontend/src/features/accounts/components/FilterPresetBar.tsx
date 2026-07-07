import React from 'react';
import { Space, Select, Button, Dropdown, Typography } from 'antd';
import { FilterOutlined, PushpinOutlined, SaveOutlined, SettingOutlined, SyncOutlined } from '@ant-design/icons';
import type { GlobalToken } from 'antd/es/theme/interface';
import type { AccountFilterPreset, AccountFilterPresetFilters } from '../../../pages/Accounts';
import { buildAccountFilterPresetSummary } from '../../../pages/Accounts';

const { Text } = Typography;

export interface FilterPresetBarProps {
  isMobile: boolean;
  token: GlobalToken;
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
}

export const FilterPresetBar: React.FC<FilterPresetBarProps> = ({
  isMobile,
  token,
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
}) => {
  const activeSummary = activeFilterPreset
    ? (activeFilterPreset.summary || buildAccountFilterPresetSummary(activeFilterPreset.filters))
    : ''
  const currentSummary = buildAccountFilterPresetSummary(currentFilterPresetFilters)

  return (
    <div
      style={{
        flex: '0 0 auto',
        marginBottom: isMobile ? 8 : 12,
        padding: isMobile ? 8 : '4px 8px',
        border: `1px solid ${token.colorBorderSecondary}`,
        borderRadius: 8,
        background: token.colorBgContainer,
        boxShadow: token.boxShadowTertiary,
        display: 'flex',
        alignItems: 'center',
        flexWrap: 'wrap',
        gap: 12,
      }}
    >
      <Space size={8} wrap style={{ minWidth: 0 }}>
        <Space size={6}>
          <FilterOutlined style={{ color: token.colorPrimary }} />
          <Text strong style={{ fontSize: 13 }}>筛选组合</Text>
        </Space>
        <Select
          allowClear
          showSearch
          size="small"
          placeholder={filterPresetLoading ? '读取中...' : '选择后应用'}
          loading={filterPresetLoading}
          value={activeFilterPresetId || undefined}
          style={{ width: isMobile ? '100%' : 180 }}
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
        {pinnedFilterPresets.length > 0 && !isMobile ? (
          <Space size={6} wrap>
            <div style={{ width: 1, height: 16, background: token.colorBorderSecondary, margin: '0 4px' }} />
            {pinnedFilterPresets.map((preset) => {
              const active = preset.id === activeFilterPresetId
              return (
                <Button
                  key={preset.id}
                  size="small"
                  type={active ? 'primary' : 'default'}
                  ghost={active}
                  icon={preset.pinned && !preset.built_in ? <PushpinOutlined /> : undefined}
                  onClick={() => {
                    if (active) clearFilterPreset()
                    else applyFilterPreset(preset)
                  }}
                >
                  {preset.name}
                </Button>
              )
            })}
          </Space>
        ) : null}
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
          <Button size="small" type="text" icon={<SettingOutlined />} />
        </Dropdown>
      </Space>

      <div style={{ flex: '1 1 auto', minWidth: 0, display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 8 }}>
        <Text type="secondary" style={{ fontSize: 12 }} ellipsis={{ tooltip: activeFilterPreset ? activeSummary : currentSummary }}>
          {activeFilterPreset ? `当前组合：${activeFilterPreset.name}${activeFilterPreset.built_in ? ' (内置)' : ''}` : `匹配 ${total} 个`}
        </Text>
        {activeFilterPreset && activeFilterPresetDirty ? (
          <Space size={4}>
            {!activeFilterPreset.built_in ? (
              <Button size="small" type="link" style={{ padding: 0 }} loading={filterPresetSaving} onClick={() => void overwriteActiveFilterPreset()}>
                覆盖保存
              </Button>
            ) : null}
            <Button size="small" type="link" style={{ padding: 0 }} onClick={() => openCopyFilterPreset(activeFilterPreset)}>
              另存为
            </Button>
            <Button size="small" type="link" danger style={{ padding: 0 }} onClick={() => applyFilterPreset(activeFilterPreset)}>
              还原
            </Button>
          </Space>
        ) : null}
      </div>
    </div>
  )
}
