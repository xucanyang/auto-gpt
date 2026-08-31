import {
  CreditCardOutlined,
  ExperimentOutlined,
  LinkOutlined,
  ProfileOutlined,
  UserAddOutlined,
} from '@ant-design/icons'
import { Space, Tabs, Tag, theme } from 'antd'
import type { ReactNode } from 'react'

import {
  REGISTRATION_LOG_REGION_LABELS,
  REGISTRATION_LOG_REGIONS,
  type RegistrationLogRegion,
} from '@/lib/registrationTaskLogs'

export type RegistrationTaskLogRegionStatus = {
  color: string
  label: string
}

type RegistrationTaskLogTabsProps = {
  activeRegion: RegistrationLogRegion
  counts: Record<RegistrationLogRegion, number>
  status: RegistrationTaskLogRegionStatus
  onChange: (region: RegistrationLogRegion) => void
}

const REGION_META: Record<RegistrationLogRegion, { label: string; icon: ReactNode }> = {
  registration: { label: REGISTRATION_LOG_REGION_LABELS.registration, icon: <UserAddOutlined /> },
  zero_amount: { label: REGISTRATION_LOG_REGION_LABELS.zero_amount, icon: <ExperimentOutlined /> },
  payment_details: {
    label: REGISTRATION_LOG_REGION_LABELS.payment_details,
    icon: <ProfileOutlined />,
  },
  payment_link: { label: REGISTRATION_LOG_REGION_LABELS.payment_link, icon: <LinkOutlined /> },
  payment: { label: REGISTRATION_LOG_REGION_LABELS.payment, icon: <CreditCardOutlined /> },
}

function compactCount(value: number): string {
  const count = Math.max(0, Math.floor(Number(value) || 0))
  if (count > 9999) return '9999+'
  return String(count)
}

export function RegistrationTaskLogTabs({
  activeRegion,
  counts,
  status,
  onChange,
}: RegistrationTaskLogTabsProps) {
  const { token } = theme.useToken()
  const activeCount = Math.max(0, Math.floor(Number(counts[activeRegion]) || 0))

  return (
    <>
      <Tabs
        className="registration-task-log-tabs"
        activeKey={activeRegion}
        animated={false}
        onChange={(value) => onChange(value as RegistrationLogRegion)}
        items={REGISTRATION_LOG_REGIONS.map((region) => {
          const meta = REGION_META[region]
          return {
            key: region,
            label: (
              <span className="registration-task-log-tab-label">
                <span className="registration-task-log-tab-icon" aria-hidden="true">{meta.icon}</span>
                <span>{meta.label}</span>
                <span
                  className="registration-task-log-tab-count"
                  style={{
                    color: region === activeRegion ? token.colorPrimaryText : token.colorTextSecondary,
                    background: region === activeRegion ? token.colorPrimaryBg : token.colorFillSecondary,
                  }}
                >
                  {compactCount(counts[region])}
                </span>
              </span>
            ),
            children: null,
          }
        })}
      />
      <div
        className="registration-task-log-region-status"
        style={{
          borderColor: token.colorBorderSecondary,
          background: token.colorFillAlter,
          color: token.colorTextSecondary,
        }}
      >
        <Space size={6} wrap>
          <Tag color={status.color} style={{ marginInlineEnd: 0 }}>{status.label}</Tag>
          <span>{activeCount.toLocaleString('zh-CN')} 条日志</span>
        </Space>
      </div>
    </>
  )
}
