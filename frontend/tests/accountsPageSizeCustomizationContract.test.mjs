import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const tableSource = await readFile(
  new URL('../src/features/accounts/components/AccountsTable.tsx', import.meta.url),
  'utf8',
)

test('account page sizes support bounded browser-local custom options', () => {
  assert.match(accountsSource, /const ACCOUNT_PAGE_SIZE_OPTIONS = \[10, 20, 50\]/)
  assert.match(accountsSource, /const MIN_ACCOUNTS_PAGE_SIZE = 1/)
  assert.match(accountsSource, /const MAX_ACCOUNTS_PAGE_SIZE = 200/)
  assert.match(accountsSource, /ACCOUNTS_CUSTOM_PAGE_SIZE_OPTIONS_STORAGE_KEY/)
  assert.match(accountsSource, /function normalizeCustomAccountsPageSizeOptions/)
  assert.match(accountsSource, /setCustomAccountsPageSizeOptions\(\(current\) =>/)
  assert.match(accountsSource, /saveCustomAccountsPageSizeOptions\(next\)/)
  assert.match(accountsSource, /if \(accountsPageSize === normalized\)[\s\S]+handleAccountsPageSizeChange\(DEFAULT_ACCOUNTS_PAGE_SIZE\)/)
})

test('desktop and mobile pagers expose add, select, and delete controls', () => {
  assert.match(tableSource, /title="每页显示设置"/)
  assert.match(tableSource, /aria-label="新增自定义每页条数"/)
  assert.match(tableSource, /onPageSizeOptionAdd\(pendingPageSize\)/)
  assert.match(tableSource, /customPageSizeOptions\.map\(\(size\) =>/)
  assert.match(tableSource, /closable/)
  assert.match(tableSource, /onPageSizeOptionRemove\(size\)/)
  assert.match(tableSource, /const renderMobilePager[\s\S]+renderPageSizeSettings\(\)/)
  assert.match(tableSource, /aria-label="自定义每页条数"\s+title="自定义每页条数"/)
  assert.doesNotMatch(tableSource, /<Popover[\s\S]+<Tooltip title="自定义每页条数">/)
})
