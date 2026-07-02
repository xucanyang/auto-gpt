import type { ReactNode } from 'react'
import { useEffect, useRef, useState } from 'react'
import { Card, Checkbox, Empty, Pagination, Spin, Table, Typography } from 'antd'

type MobileCardHelpers = {
  checked: boolean
  onCheckedChange: (checked: boolean) => void
}

type AccountsTableProps = {
  columns: any[]
  accounts: any[]
  loading: boolean
  total: number
  currentPage: number
  pageSize: number
  onPageChange: (page: number) => void
  onPageSizeChange?: (pageSize: number) => void
  pageSizeOptions?: number[]
  selectedRowKeys: React.Key[]
  setSelectedRowKeys: (keys: React.Key[]) => void
  onOpenDetail: (record: any) => void
  onTableChange?: (pagination: any, filters: Record<string, any>, sorter: any, extra: any) => void
  filterSummary?: ReactNode
  isMobile?: boolean
  renderMobileCard?: (record: any, helpers: MobileCardHelpers) => ReactNode
}

export function AccountsTable({
  columns,
  accounts,
  loading,
  total,
  currentPage,
  pageSize,
  onPageChange,
  onPageSizeChange,
  pageSizeOptions = [10, 20, 50],
  selectedRowKeys,
  setSelectedRowKeys,
  onOpenDetail,
  onTableChange,
  filterSummary,
  isMobile = false,
  renderMobileCard,
}: AccountsTableProps) {
  const tableAreaRef = useRef<HTMLDivElement | null>(null)
  const [tableBodyHeight, setTableBodyHeight] = useState(360)
  const selectedKeySet = new Set(selectedRowKeys)
  const pageKeys = accounts.map((record) => record.id as React.Key)
  const selectedOnPage = pageKeys.filter((key) => selectedKeySet.has(key))
  const totalPages = Math.max(1, Math.ceil(total / Math.max(pageSize, 1)))

  const updateRecordSelection = (key: React.Key, checked: boolean) => {
    if (checked) {
      setSelectedRowKeys(Array.from(new Set([...selectedRowKeys, key])))
      return
    }
    setSelectedRowKeys(selectedRowKeys.filter((item) => item !== key))
  }

  useEffect(() => {
    if (isMobile || !tableAreaRef.current) return
    const updateHeight = () => {
      const height = tableAreaRef.current?.clientHeight || 0
      if (!height) return
      setTableBodyHeight(Math.max(220, Math.floor(height - 58)))
    }
    updateHeight()
    const observer = new ResizeObserver(updateHeight)
    observer.observe(tableAreaRef.current)
    return () => observer.disconnect()
  }, [isMobile])

  const renderPager = (align: 'center' | 'flex-end') => {
    const handlePagerChange = (page: number, nextPageSize?: number) => {
      if (nextPageSize && nextPageSize !== pageSize) {
        onPageSizeChange?.(nextPageSize)
        return
      }
      if (page !== currentPage) onPageChange(page)
    }

    return (
      <div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: align, alignItems: 'center', gap: 8, width: '100%' }}>
        <Pagination
          current={currentPage}
          pageSize={pageSize}
          total={total}
          showSizeChanger={!isMobile && Boolean(onPageSizeChange)}
          pageSizeOptions={pageSizeOptions}
          showLessItems={isMobile || totalPages > 12}
          showQuickJumper={!isMobile && totalPages > 10}
          responsive
          showTotal={(value, range) => `第 ${range[0]}-${range[1]} 条 / 共 ${value} 条`}
          onChange={handlePagerChange}
          onShowSizeChange={(_, size) => onPageSizeChange?.(size)}
        />
      </div>
    )
  }

  const renderMobilePager = () => (
    <Pagination
      current={currentPage}
      pageSize={pageSize}
      total={total}
      showSizeChanger={false}
      showLessItems
      responsive
      onChange={onPageChange}
    />
  )

  if (isMobile) {
    return (
      <Spin spinning={loading}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, minWidth: 0 }}>
          {filterSummary}
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              gap: 8,
              padding: '0 2px',
            }}
          >
            <Checkbox
              checked={accounts.length > 0 && selectedOnPage.length === accounts.length}
              indeterminate={selectedOnPage.length > 0 && selectedOnPage.length < accounts.length}
              disabled={accounts.length === 0}
              onChange={(event) => {
                const checked = event.target.checked
                if (checked) {
                  setSelectedRowKeys(Array.from(new Set([...selectedRowKeys, ...pageKeys])))
                  return
                }
                setSelectedRowKeys(selectedRowKeys.filter((key) => !pageKeys.includes(key)))
              }}
            >
              选择本页
            </Checkbox>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              已选 {selectedRowKeys.length} 个
            </Typography.Text>
          </div>

          {accounts.length > 0 ? (
            accounts.map((record) => {
              const key = record.id as React.Key
              return (
                <Card
                  key={String(key)}
                  size="small"
                  styles={{ body: { padding: 12 } }}
                  style={{ borderRadius: 14 }}
                >
                  {renderMobileCard?.(record, {
                    checked: selectedKeySet.has(key),
                    onCheckedChange: (checked) => updateRecordSelection(key, checked),
                  })}
                </Card>
              )
            })
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={loading ? '加载中' : '暂无账号'} />
          )}

          <div style={{ display: 'flex', justifyContent: 'center', padding: '4px 0 10px' }}>
            {renderMobilePager()}
          </div>
        </div>
      </Spin>
    )
  }

  return (
    <div style={{ flex: '1 1 auto', minHeight: 0, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
      {filterSummary}
      <div ref={tableAreaRef} className="auto-chatgpt-accounts-table-scroll" style={{ flex: '1 1 auto', minHeight: 0, minWidth: 0, overflow: 'hidden' }}>
        <Table
          className="auto-chatgpt-accounts-table"
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
          pagination={false}
          onChange={onTableChange}
          scroll={{ x: 'max-content', y: tableBodyHeight }}
          sticky
          style={{ width: '100%', minWidth: 0, height: '100%' }}
          onRow={(record) => ({
            onDoubleClick: () => {
              onOpenDetail(record)
            },
          })}
        />
      </div>
      <div style={{ flex: '0 0 auto', display: 'flex', justifyContent: 'center', paddingTop: 10 }}>
        {renderPager('center')}
      </div>
    </div>
  )
}
