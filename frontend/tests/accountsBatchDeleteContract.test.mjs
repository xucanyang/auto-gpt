import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const toolbarSource = await readFile(new URL('../src/features/accounts/components/AccountsToolbar.tsx', import.meta.url), 'utf8')
const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

test('account danger actions use the App modal context', () => {
  assert.match(toolbarSource, /const \{ modal: appModal \} = App\.useApp\(\)/)
  assert.match(toolbarSource, /appModal\.confirm\(/)
  assert.doesNotMatch(toolbarSource, /(?<![A-Za-z])Modal\.confirm\(/)
})

test('batch account deletion normalizes ids and reports request failures', () => {
  const handlerStart = accountsSource.indexOf('const handleBatchDelete = async () => {')
  const handlerEnd = accountsSource.indexOf('\n  const handleDeleteAccount', handlerStart)
  assert.ok(handlerStart >= 0 && handlerEnd > handlerStart)
  const handler = accountsSource.slice(handlerStart, handlerEnd)

  assert.match(handler, /normalizeAccountIds\(selectedRowKeys\)/)
  assert.match(handler, /body: JSON\.stringify\(\{ ids: accountIds \}\)/)
  assert.match(handler, /await load\(\)/)
  assert.match(handler, /catch \(error: unknown\)/)
  assert.match(handler, /批量删除失败/)
})
