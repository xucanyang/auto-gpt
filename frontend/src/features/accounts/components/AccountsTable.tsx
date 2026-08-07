import type { ReactNode } from 'react'
import { useEffect, useRef, useState } from 'react'
import { CheckOutlined, PlusOutlined, SaveOutlined, SettingOutlined } from '@ant-design/icons'
import { Button, Card, Checkbox, Empty, InputNumber, Pagination, Popover, Select, Spin, Table, Tag, Tooltip } from 'antd'
import type { TableColumnsType, TableProps } from 'antd'

type MobileCardHelpers = {
  checked: boolean
  onCheckedChange: (checked: boolean) => void
}

type AccountTableRecord = {
  id: React.Key
  [key: string]: unknown
}

type AccountsTableProps = {
  columns: TableColumnsType<AccountTableRecord>
  accounts: AccountTableRecord[]
  loading: boolean
  total: number
  currentPage: number
  pageSize: number
  defaultPageSize?: number
  onPageChange: (page: number) => void
  onPageSizeChange?: (pageSize: number) => void
  onDefaultPageSizeChange?: (pageSize: number) => void
  pageSizeOptions?: number[]
  customPageSizeOptions?: number[]
  minPageSize?: number
  maxPageSize?: number
  onPageSizeOptionAdd?: (pageSize: number) => void
  onPageSizeOptionRemove?: (pageSize: number) => void
  selectedRowKeys: React.Key[]
  setSelectedRowKeys: (keys: React.Key[]) => void
  onOpenDetail: (record: AccountTableRecord) => void
  onTableChange?: TableProps<AccountTableRecord>['onChange']
  isMobile?: boolean
  renderMobileCard?: (record: AccountTableRecord, helpers: MobileCardHelpers) => ReactNode
}

export function AccountsTable({
  columns,
  accounts,
  loading,
  total,
  currentPage,
  pageSize,
  defaultPageSize = 20,
  onPageChange,
  onPageSizeChange,
  onDefaultPageSizeChange,
  pageSizeOptions = [10, 20, 50],
  customPageSizeOptions = [],
  minPageSize = 1,
  maxPageSize = 200,
  onPageSizeOptionAdd,
  onPageSizeOptionRemove,
  selectedRowKeys,
  setSelectedRowKeys,
  onOpenDetail,
  onTableChange,
  isMobile = false,
  renderMobileCard,
}: AccountsTableProps) {
  const tableAreaRef = useRef<HTMLDivElement | null>(null)
  const [tableBodyHeight, setTableBodyHeight] = useState(360)
  const [pendingPageSize, setPendingPageSize] = useState<number | null>(null)
  const selectedKeySet = new Set(selectedRowKeys)
  const pageKeys = accounts.map((record) => record.id as React.Key)
  const selectedOnPage = pageKeys.filter((key) => selectedKeySet.has(key))
  const totalPages = Math.max(1, Math.ceil(total / Math.max(pageSize, 1)))
  const mobileRangeStart = accounts.length > 0 ? (currentPage - 1) * pageSize + 1 : 0
  const mobileRangeEnd = accounts.length > 0 ? mobileRangeStart + accounts.length - 1 : 0

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

  const renderPageSizeSettings = () => {
    if (!onPageSizeChange || !onDefaultPageSizeChange || !onPageSizeOptionAdd || !onPageSizeOptionRemove) return null
    const canAddPageSize = pendingPageSize !== null
      && Number.isInteger(pendingPageSize)
      && pendingPageSize >= minPageSize
      && pendingPageSize <= maxPageSize
    const isDefaultPageSize = pageSize === defaultPageSize

    const addPageSizeOption = () => {
      if (!canAddPageSize || pendingPageSize === null) return
      onPageSizeOptionAdd(pendingPageSize)
      setPendingPageSize(null)
    }

    return (
      <Popover
        trigger="click"
        placement="top"
        title="每页显示设置"
        content={(
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, width: 'min(284px, calc(100vw - 48px))' }}>
            <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto', alignItems: 'center', gap: 6 }}>
              <Select
                aria-label="每页显示条数"
                value={pageSize}
                options={pageSizeOptions.map((size) => ({
                  value: size,
                  label: `${size} 条/页${size === defaultPageSize ? '（默认）' : ''}`,
                }))}
                onChange={onPageSizeChange}
                style={{ width: '100%', minWidth: 0 }}
              />
              <Button
                type={isDefaultPageSize ? 'default' : 'primary'}
                aria-label={isDefaultPageSize ? `${pageSize} 条每页已是默认` : `将 ${pageSize} 条每页设为默认`}
                title={isDefaultPageSize ? '当前已是默认显示数量' : '设为以后打开账号列表时的默认显示数量'}
                icon={isDefaultPageSize ? <CheckOutlined /> : <SaveOutlined />}
                disabled={isDefaultPageSize}
                onClick={() => onDefaultPageSizeChange(pageSize)}
              >
                {isDefaultPageSize ? '默认' : '设为默认'}
              </Button>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <InputNumber
                aria-label="新增自定义每页条数"
                min={minPageSize}
                max={maxPageSize}
                precision={0}
                value={pendingPageSize}
                placeholder={`${minPageSize}-${maxPageSize}`}
                onChange={(value) => setPendingPageSize(typeof value === 'number' ? value : null)}
                onPressEnter={addPageSizeOption}
                style={{ flex: '1 1 auto', minWidth: 0 }}
              />
              <Tooltip title="添加并使用">
                <Button
                  aria-label="添加并使用自定义每页条数"
                  icon={<PlusOutlined />}
                  disabled={!canAddPageSize}
                  onClick={addPageSizeOption}
                />
              </Tooltip>
            </div>
            {customPageSizeOptions.length > 0 ? (
              <div aria-label="自定义每页条数列表" style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {customPageSizeOptions.map((size) => (
                  <Tag
                    key={size}
                    closable
                    onClose={(event) => {
                      event.preventDefault()
                      onPageSizeOptionRemove(size)
                    }}
                    style={{ marginInlineEnd: 0 }}
                  >
                    {size} 条
                  </Tag>
                ))}
              </div>
            ) : null}
          </div>
        )}
      >
        <Button
          aria-label="自定义每页条数"
          title="自定义每页条数"
          icon={<SettingOutlined />}
        />
      </Popover>
    )
  }

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
        {renderPageSizeSettings()}
      </div>
    )
  }

  const renderMobilePager = () => (
    <div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'center', alignItems: 'center', gap: 8 }}>
      <Pagination
        current={currentPage}
        pageSize={pageSize}
        total={total}
        showSizeChanger={false}
        showLessItems
        responsive
        onChange={onPageChange}
      />
      {renderPageSizeSettings()}
    </div>
  )

  if (isMobile) {
    return (
      <Spin spinning={loading}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, minWidth: 0 }}>
          <div className="accounts-mobile-selection-bar">
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
            <span className="accounts-mobile-selection-range">
              {accounts.length > 0 ? `本页 ${mobileRangeStart}-${mobileRangeEnd} / 共 ${total}` : `共 ${total} 条`}
            </span>
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
      <div ref={tableAreaRef} className="auto-chatgpt-accounts-table-scroll" style={{ flex: '1 1 auto', minHeight: 0, minWidth: 0, overflow: 'hidden' }}>
        <Table<AccountTableRecord>
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
