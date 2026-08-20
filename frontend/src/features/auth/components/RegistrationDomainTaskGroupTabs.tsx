import { Alert, Space, Tabs, Tag, Typography } from 'antd'

import type { RegistrationDomainTaskGroup } from '@/lib/registrationDomainTasks'

const { Text } = Typography

type RegistrationDomainTaskGroupTabsProps = {
  group: RegistrationDomainTaskGroup
  activeTaskId: string
  onSelectTask: (taskId: string) => void
}

export function RegistrationDomainTaskGroupTabs({
  group,
  activeTaskId,
  onSelectTask,
}: RegistrationDomainTaskGroupTabsProps) {
  return (
    <div className="registration-domain-task-group">
      <Space size={6} wrap className="registration-domain-task-group-summary">
        <Text strong>域名任务组</Text>
        <Tag color="processing">已创建 {group.tasks.length}/{group.requestedDomainCount}</Tag>
        <Tag>每任务 {group.requestedCountPerTask} 个</Tag>
      </Space>
      {group.errors.length > 0 ? (
        <Alert
          type="warning"
          showIcon
          message={`${group.errors.length} 个域名任务创建失败`}
          description={group.errors.map((item) => `${item.domain}：${item.message}`).join('；')}
        />
      ) : null}
      <Tabs
        activeKey={activeTaskId}
        onChange={onSelectTask}
        items={group.tasks.map((item) => ({
          key: item.taskId,
          label: item.domain,
        }))}
      />
    </div>
  )
}
