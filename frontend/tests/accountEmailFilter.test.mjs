import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { after, test } from 'node:test'
import { pathToFileURL } from 'node:url'
import ts from 'typescript'

const moduleDir = await mkdtemp(join(tmpdir(), 'auto-gpt-email-filter-tests-'))
after(() => rm(moduleDir, { force: true, recursive: true }))

const sourceUrl = new URL('../src/features/accounts/emailFilter.ts', import.meta.url)
const source = await readFile(sourceUrl, 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
  fileName: sourceUrl.pathname,
})
await writeFile(join(moduleDir, 'emailFilter.mjs'), compiled.outputText)

const {
  MAX_EXACT_EMAIL_FILTER_COUNT,
  canonicalizeAccountEmailFilter,
  hasMultipleEmailFilterLines,
  parseAccountEmailFilter,
} = await import(pathToFileURL(join(moduleDir, 'emailFilter.mjs')).href)

const componentSource = await readFile(
  new URL('../src/features/accounts/components/EmailFilterControl.tsx', import.meta.url),
  'utf8',
)
const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const presetBarSource = await readFile(
  new URL('../src/features/accounts/components/FilterPresetBar.tsx', import.meta.url),
  'utf8',
)
const querySource = await readFile(
  new URL('../src/features/accounts/hooks/useAccountsQuery.ts', import.meta.url),
  'utf8',
)

test('single-line email search keeps the existing fuzzy-search contract', () => {
  assert.deepEqual(parseAccountEmailFilter('  @Example.COM  '), {
    mode: 'single',
    search: '@Example.COM',
    emails: ['@example.com'],
    inputCount: 1,
    duplicateCount: 0,
  })
  assert.equal(canonicalizeAccountEmailFilter('  @Example.COM  '), '@Example.COM')
  assert.equal(hasMultipleEmailFilterLines('one@example.com\n\n'), false)
})

test('multiline email filter trims, lowercases and deduplicates exact addresses', () => {
  const value = ' First@Example.com\r\n\r\nsecond@example.com\nFIRST@example.com '
  assert.deepEqual(parseAccountEmailFilter(value), {
    mode: 'bulk',
    search: '',
    emails: ['first@example.com', 'second@example.com'],
    inputCount: 3,
    duplicateCount: 1,
  })
  assert.equal(canonicalizeAccountEmailFilter(value), 'first@example.com\nsecond@example.com')
  assert.equal(hasMultipleEmailFilterLines(value), true)
  assert.equal(MAX_EXACT_EMAIL_FILTER_COUNT, 1000)

  const duplicateOnly = 'same@example.com\nSAME@example.com'
  assert.equal(parseAccountEmailFilter(duplicateOnly).mode, 'bulk')
  assert.equal(canonicalizeAccountEmailFilter(duplicateOnly), 'same@example.com\nsame@example.com')
})

test('desktop and mobile surfaces share multiline paste UI and POST exact-email queries', () => {
  assert.match(componentSource, /<Input\.TextArea/)
  assert.match(componentSource, /event\.clipboardData\.getData\('text'\)/)
  assert.match(componentSource, /hasMultipleEmailFilterLines\(pasted\)/)
  assert.match(componentSource, /parsedDraft\.duplicateCount/)
  assert.match(componentSource, /MAX_EXACT_EMAIL_FILTER_COUNT/)
  assert.match(componentSource, /parsedDraft\.emails\.length === 1[\s\S]+parsedDraft\.emails\[0\][\s\S]+parsedDraft\.emails\[0\]/)
  assert.match(accountsSource, /<EmailFilterControl[\s\S]+renderEmailColumnTitle/)
  assert.match(accountsSource, /emails: appliedEmailFilter\.mode === 'bulk'/)
  assert.match(accountsSource, /const fetchPaypalFilteredEligibleAccounts = useCallback[\s\S]+apiFetch\('\/accounts\/query',[\s\S]+JSON\.stringify\(body\)/)
  assert.match(presetBarSource, /<EmailFilterControl[\s\S]+isMobile/)
  assert.match(querySource, /apiFetch\('\/accounts\/query',[\s\S]+method: 'POST'/)
  assert.match(querySource, /email: '',[\s\S]+emails,/)
})
