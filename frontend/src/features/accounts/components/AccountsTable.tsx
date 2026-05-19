import { Table } from 'antd'

type AccountsTableProps = {
  columns: any[]
  accounts: any[]
  loading: boolean
  total: number
  currentPage: number
  pageSize: number
  onPageChange: (page: number) => void
  selectedRowKeys: React.Key[]
  setSelectedRowKeys: (keys: React.Key[]) => void
  isChatgptPlatform: boolean
  onOpenDetail: (record: any) => void
}

export function AccountsTable({
  columns,
  accounts,
  loading,
  total,
  currentPage,
  pageSize,
  onPageChange,
  selectedRowKeys,
  setSelectedRowKeys,
  isChatgptPlatform,
  onOpenDetail,
}: AccountsTableProps) {
  return (
    <Table
      rowKey="id"
      columns={columns}
      dataSource={accounts}
      loading={loading}
      size="middle"
      rowSelection={{
        selectedRowKeys,
        onChange: setSelectedRowKeys,
        preserveSelectedRowKeys: true,
      }}
      pagination={{
        current: currentPage,
        pageSize,
        total,
        showSizeChanger: false,
        onChange: onPageChange,
      }}
      scroll={{ x: isChatgptPlatform ? 1084 : 980 }}
      onRow={(record) => ({
        onDoubleClick: () => {
          onOpenDetail(record)
        },
      })}
    />
  )
}
