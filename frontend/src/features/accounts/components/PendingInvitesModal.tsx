import { Alert, Button, Modal, Space, Table, Tag, Typography } from 'antd'

type PendingInvitesModalProps = {
  open: boolean
  onClose: () => void
  loading: boolean
  items: any[]
  selectedRowKeys: React.Key[]
  onSelectedRowKeysChange: (keys: React.Key[]) => void
  activatingAll: boolean
  activatingId: number | null
  abandoningId: number | null
  onRefresh: () => Promise<void> | void
  onActivateSelected: () => Promise<void> | void
  onActivateAll: () => Promise<void> | void
  onActivateOne: (inviteId: number) => Promise<void> | void
  onAbandonOne: (inviteId: number) => Promise<void> | void
  pendingActivationKindMeta: (kind?: string) => { color: string; label: string }
  pendingInviteStatusMeta: (status?: string) => { color: string; label: string }
  formatSyncTime: (value?: string) => string
}

const { Text } = Typography

export function PendingInvitesModal({
  open,
  onClose,
  loading,
  items,
  selectedRowKeys,
  onSelectedRowKeysChange,
  activatingAll,
  activatingId,
  abandoningId,
  onRefresh,
  onActivateSelected,
  onActivateAll,
  onActivateOne,
  onAbandonOne,
  pendingActivationKindMeta,
  pendingInviteStatusMeta,
  formatSyncTime,
}: PendingInvitesModalProps) {
  return (
    <Modal
      title="ChatGPT 待激活 / Auth 补抓中心"
      open={open}
      onCancel={onClose}
      footer={[
        <Button key="refresh" onClick={onRefresh} loading={loading}>
          刷新
        </Button>,
        <Button key="activate-selected" onClick={onActivateSelected} loading={activatingAll} disabled={selectedRowKeys.length === 0}>
          激活所选 {selectedRowKeys.length > 0 ? `(${selectedRowKeys.length})` : ''}
        </Button>,
        <Button key="activate-all" onClick={onActivateAll} loading={activatingAll}>
          激活可恢复项
        </Button>,
        <Button key="close" type="primary" onClick={onClose}>
          确定
        </Button>,
      ]}
      width={1180}
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Alert
          type="info"
          showIcon
          message="这里承载的是“延迟邀请 / 订阅后 Auth 补抓 / 激活中断恢复”流程。"
          description="订阅链接生成后会进入这里，付款完成后可执行重新激活来补抓 RT/workspace；延迟邀请仍会从保存的检查点继续，不会重新注册账号。"
        />

        <Table
          rowKey="id"
          size="small"
          loading={loading}
          pagination={{ pageSize: 8, showSizeChanger: false }}
          rowSelection={{
            selectedRowKeys,
            onChange: onSelectedRowKeysChange,
            getCheckboxProps: (record: any) => ({ disabled: record.can_activate === false }),
          }}
          dataSource={items}
          columns={[
            {
              title: '邮箱',
              dataIndex: 'email',
              key: 'email',
              width: 220,
              render: (value: string) => <Text copyable={{ text: value }}>{value}</Text>,
            },
            {
              title: '类型',
              dataIndex: 'activation_kind',
              key: 'activation_kind',
              width: 108,
              render: (value: string) => {
                const meta = pendingActivationKindMeta(value)
                return <Tag color={meta.color}>{meta.label}</Tag>
              },
            },
            {
              title: 'Team',
              key: 'team',
              width: 128,
              render: (_: any, record: any) => {
                if (record.activation_kind === 'subscription_auth') return record.team_name || 'subscription'
                return record.team_name || (record.team_id ? `team=${record.team_id}` : '-')
              },
            },
            {
              title: '状态',
              dataIndex: 'status',
              key: 'status',
              width: 132,
              render: (value: string) => {
                const meta = pendingInviteStatusMeta(value)
                return <Tag color={meta.color}>{meta.label}</Tag>
              },
            },
            {
              title: '检查点',
              dataIndex: 'last_checkpoint_label',
              key: 'last_checkpoint_label',
              width: 120,
              render: (value: string) => value || '-',
            },
            {
              title: '尝试',
              dataIndex: 'activation_attempt_count',
              key: 'activation_attempt_count',
              width: 80,
              render: (value: number) => value || 0,
            },
            {
              title: '最近尝试',
              dataIndex: 'last_attempt_at',
              key: 'last_attempt_at',
              width: 168,
              render: (value: string) => formatSyncTime(value) || '-',
            },
            {
              title: '邀请时间',
              dataIndex: 'invited_at',
              key: 'invited_at',
              width: 168,
              render: (value: string) => formatSyncTime(value) || value || '-',
            },
            {
              title: '错误',
              dataIndex: 'last_error',
              key: 'last_error',
              render: (value: string, record: any) => value || record.last_error_code || '-',
            },
            {
              title: '操作',
              key: 'action',
              width: 220,
              render: (_: any, record: any) => (
                <Space size={4} wrap>
                  <Button
                    type="primary"
                    size="small"
                    disabled={record.can_activate === false}
                    loading={activatingId === record.id}
                    onClick={() => onActivateOne(record.id)}
                  >
                    重新激活
                  </Button>
                  <Button
                    size="small"
                    danger
                    disabled={record.status === 'completed' || record.status === 'abandoned'}
                    loading={abandoningId === record.id}
                    onClick={() => onAbandonOne(record.id)}
                  >
                    标记放弃
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      </Space>
    </Modal>
  )
}
