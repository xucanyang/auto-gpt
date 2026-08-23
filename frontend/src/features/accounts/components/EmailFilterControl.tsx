import { useMemo, useState } from 'react'
import { Button, Input, Popover, Space, Tooltip, Typography } from 'antd'
import { CloseOutlined, SearchOutlined, UnorderedListOutlined } from '@ant-design/icons'
import {
  MAX_EXACT_EMAIL_FILTER_COUNT,
  canonicalizeAccountEmailFilter,
  hasMultipleEmailFilterLines,
  parseAccountEmailFilter,
} from '../emailFilter'

const { Text } = Typography

export type EmailFilterControlProps = {
  value: string
  onChange: (value: string) => void
  onSubmit: (value: string) => void
  isMobile?: boolean
}

export function EmailFilterControl({
  value,
  onChange,
  onSubmit,
  isMobile = false,
}: EmailFilterControlProps) {
  const [bulkEditorOpen, setBulkEditorOpen] = useState(false)
  const [bulkDraft, setBulkDraft] = useState('')
  const parsedValue = useMemo(() => parseAccountEmailFilter(value), [value])
  const parsedDraft = useMemo(() => parseAccountEmailFilter(bulkDraft), [bulkDraft])
  const bulkLimitExceeded = parsedDraft.emails.length > MAX_EXACT_EMAIL_FILTER_COUNT

  const openBulkEditor = (draft = value) => {
    setBulkDraft(draft)
    setBulkEditorOpen(true)
  }

  const applyBulkFilter = () => {
    if (bulkLimitExceeded || parsedDraft.emails.length === 0) return
    const next = parsedDraft.emails.length === 1
      ? `${parsedDraft.emails[0]}\n${parsedDraft.emails[0]}`
      : parsedDraft.emails.join('\n')
    onChange(next)
    onSubmit(next)
    setBulkEditorOpen(false)
  }

  const clearFilter = () => {
    setBulkDraft('')
    onChange('')
    onSubmit('')
    setBulkEditorOpen(false)
  }

  const bulkEditor = (
    <div className="accounts-email-filter-popover" onClick={(event) => event.stopPropagation()}>
      <div className="accounts-email-filter-popover-title">
        <Text strong>批量邮箱</Text>
        <Button
          type="text"
          size="small"
          icon={<CloseOutlined />}
          aria-label="关闭批量邮箱筛选"
          title="关闭"
          onClick={() => setBulkEditorOpen(false)}
        />
      </div>
      <Input.TextArea
        autoFocus
        value={bulkDraft}
        autoSize={{ minRows: 6, maxRows: 10 }}
        placeholder={'first@example.com\nsecond@example.com'}
        aria-label="批量邮箱，一行一个"
        status={bulkLimitExceeded ? 'error' : undefined}
        onChange={(event) => setBulkDraft(event.target.value)}
        onKeyDown={(event) => {
          if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
            event.preventDefault()
            applyBulkFilter()
          }
        }}
      />
      <div className="accounts-email-filter-popover-footer">
        <Text type={bulkLimitExceeded ? 'danger' : 'secondary'} className="accounts-email-filter-count">
          {bulkLimitExceeded
            ? `最多 ${MAX_EXACT_EMAIL_FILTER_COUNT} 个邮箱`
            : `${parsedDraft.emails.length} 个邮箱${parsedDraft.duplicateCount > 0 ? `，已去重 ${parsedDraft.duplicateCount} 个` : ''}`}
        </Text>
        <Space size={6}>
          <Button size="small" onClick={clearFilter}>清空</Button>
          <Button
            size="small"
            type="primary"
            icon={<SearchOutlined />}
            disabled={bulkLimitExceeded || parsedDraft.emails.length === 0}
            onClick={applyBulkFilter}
          >
            筛选
          </Button>
        </Space>
      </div>
    </div>
  )

  return (
    <Space.Compact
      block
      className="accounts-email-filter-control"
      onClick={(event) => event.stopPropagation()}
    >
      <Input.Search
        allowClear
        size="small"
        aria-label="搜索邮箱"
        placeholder="搜索邮箱"
        value={parsedValue.mode === 'bulk' ? `已筛选 ${parsedValue.emails.length} 个邮箱` : value}
        readOnly={parsedValue.mode === 'bulk'}
        onFocus={() => {
          if (parsedValue.mode === 'bulk') openBulkEditor(parsedValue.emails.join('\n'))
        }}
        onClick={() => {
          if (parsedValue.mode === 'bulk') openBulkEditor(parsedValue.emails.join('\n'))
        }}
        onPaste={(event) => {
          const pasted = event.clipboardData.getData('text')
          if (!hasMultipleEmailFilterLines(pasted)) return
          event.preventDefault()
          openBulkEditor(pasted)
        }}
        onChange={(event) => {
          const next = event.target.value
          if (parsedValue.mode === 'bulk') {
            if (!next) clearFilter()
            return
          }
          onChange(next)
        }}
        onSearch={(next) => {
          if (parsedValue.mode === 'bulk') {
            openBulkEditor(parsedValue.emails.join('\n'))
            return
          }
          const canonical = canonicalizeAccountEmailFilter(next)
          onChange(canonical)
          onSubmit(canonical)
        }}
      />
      <Popover
        content={bulkEditor}
        trigger="click"
        placement={isMobile ? 'bottom' : 'bottomLeft'}
        open={bulkEditorOpen}
        onOpenChange={(open) => {
          if (open) openBulkEditor(parsedValue.mode === 'bulk' ? parsedValue.emails.join('\n') : value)
          else setBulkEditorOpen(false)
        }}
      >
        <Tooltip title="批量筛选邮箱">
          <Button
            className="accounts-email-filter-bulk-trigger"
            size="small"
            type={parsedValue.mode === 'bulk' ? 'primary' : 'default'}
            icon={<UnorderedListOutlined />}
            aria-label="批量筛选邮箱"
            aria-pressed={parsedValue.mode === 'bulk'}
          />
        </Tooltip>
      </Popover>
    </Space.Compact>
  )
}
