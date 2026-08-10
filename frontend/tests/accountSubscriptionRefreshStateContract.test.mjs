import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

test('subscription rows distinguish active refresh from exhausted refresh failure', () => {
  const helperStart = accountsSource.indexOf('function subscriptionTypeMeta(record: any)')
  const helperEnd = accountsSource.indexOf('\nfunction accountValidityValue', helperStart)
  assert.ok(helperStart >= 0 && helperEnd > helperStart)
  const helperSource = accountsSource.slice(helperStart, helperEnd)

  assert.match(helperSource, /refreshState === 'refreshing'[\s\S]+\? '刷新中'/)
  assert.match(helperSource, /refreshState === 'refresh_failed' \|\| refreshState === 'unknown_plan'[\s\S]+\? '刷新失败'/)
  assert.match(helperSource, /refreshState === 'auth_invalid'[\s\S]+label: '不可确认'/)
  assert.match(helperSource, /const refreshHint = refreshState === 'refreshing'[\s\S]+refreshState === 'refresh_failed'/)
  assert.match(helperSource, /label: 'Free', subLabel: refreshHint/)
  assert.match(helperSource, /label: 'Plus', subLabel: refreshHint/)
})
