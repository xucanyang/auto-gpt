import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const presetBarSource = await readFile(
  new URL('../src/features/accounts/components/FilterPresetBar.tsx', import.meta.url),
  'utf8',
)
const stylesSource = await readFile(new URL('../src/index.css', import.meta.url), 'utf8')

test('standard account export uses selected ids first and otherwise the complete filtered scope', () => {
  const handlerStart = accountsSource.indexOf('const exportCsv = async (')
  const handlerEnd = accountsSource.indexOf('\n  const getResumeAuthGlobalDefaults', handlerStart)
  assert.ok(handlerStart >= 0 && handlerEnd > handlerStart)
  const handler = accountsSource.slice(handlerStart, handlerEnd)

  assert.match(handler, /const useFilteredScope = exportMode === 'pix_payment_links'[\s\S]+: selectedIds\.length === 0/)
  assert.match(handler, /applyAccountTaskScopeToBody\(body, \{[\s\S]+scope: 'filtered'/)
  assert.match(handler, /body\.ids = \[\]/)
  assert.match(handler, /if \(filteredCount === 0\)[\s\S]+当前筛选范围没有可导出的账号/)
})

test('preset settings and fixed-group creation actions live beside their left-side labels', () => {
  const labels = Array.from(
    presetBarSource.matchAll(/<div className="accounts-filter-preset-label">([\s\S]*?)\n\s*<\/div>/g),
    (match) => match[1],
  )
  assert.ok(labels.length >= 2)
  assert.match(labels[0], /条件筛选组合/)
  assert.match(labels[0], /<Dropdown[\s\S]+accounts-filter-preset-label-action/)
  assert.match(labels[1], /固定账号组合/)
  assert.match(labels[1], /<Tooltip[\s\S]+<PlusOutlined \/>/)

  assert.match(stylesSource, /grid-template-columns: 164px minmax\(140px, 180px\) minmax\(0, 1fr\);/)
  assert.match(stylesSource, /\.accounts-filter-preset-label-action\.ant-btn[\s\S]+font-size: 17px;/)
  assert.doesNotMatch(stylesSource, /grid-template-columns: 128px minmax\(140px, 180px\) minmax\(0, 1fr\) 28px;/)
})
