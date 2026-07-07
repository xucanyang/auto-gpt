import React from 'react';
import { Space, Button, Tag, Typography, Popover } from 'antd';
import type { GlobalToken } from 'antd/es/theme/interface';
import { STATUS_COLORS, statusLabel } from '../../../pages/Accounts';

const { Text } = Typography;

export interface SelectedAccountItem {
  id?: string | number;
  email?: string;
  status?: string;
}

export interface SelectedAccountsSummaryProps {
  isMobile: boolean;
  token: GlobalToken;
  selectedAccountItems: SelectedAccountItem[];
  removeSelectedAccount: (id: string) => void;
  clearSelectedAccounts: () => void;
}

export const SelectedAccountsSummary: React.FC<SelectedAccountsSummaryProps> = ({
  isMobile,
  token,
  selectedAccountItems,
  removeSelectedAccount,
  clearSelectedAccounts,
}) => {
  if (selectedAccountItems.length === 0) return null;

  const accountTags = (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, maxHeight: isMobile ? 180 : 240, overflow: 'auto', maxWidth: 400 }}>
      {selectedAccountItems.map((account) => {
        const id = String(account?.id || '')
        const email = String(account?.email || '').trim()
        const status = String(account?.status || '').trim()
        const title = email || `账号 ${id}`
        return (
          <Tag
            key={id}
            closable
            onClose={(event) => {
              event.preventDefault()
              removeSelectedAccount(id)
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
                maxWidth: 210,
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
    </div>
  )

  return (
    <div
      style={{
        flex: '0 0 auto',
        marginBottom: isMobile ? 10 : 12,
        padding: '4px 12px',
        border: `1px solid ${token.colorBorderSecondary}`,
        borderRadius: 8,
        background: token.colorFillAlter,
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        gap: 10,
      }}
    >
      <Space size={6}>
        <Text strong style={{ fontSize: 13 }}>已选账号</Text>
        <Popover content={accountTags} title="已选账号列表" trigger="hover" placement="bottomLeft">
          <Tag color="processing" style={{ cursor: 'pointer' }}>{selectedAccountItems.length} 个</Tag>
        </Popover>
      </Space>
      <Button size="small" type="link" onClick={clearSelectedAccounts} style={{ padding: 0 }}>
        清空
      </Button>
    </div>
  )
}
