import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

test('stale subscription shows the real local-status refresh time on desktop and mobile', () => {
  const helperStart = accountsSource.indexOf('function subscriptionRefreshTime(record: any)')
  const helperEnd = accountsSource.indexOf('\ntype SubscriptionTypeMeta', helperStart)
  assert.ok(helperStart >= 0 && helperEnd > helperStart)
  const helperSource = accountsSource.slice(helperStart, helperEnd)

  assert.match(helperSource, /localSubscription\.checked_at/)
  assert.match(helperSource, /compactSubscription\.checked_at/)
  assert.match(helperSource, /formatCompactDateTime\(String\(value\)\)/)
  assert.doesNotMatch(helperSource, /record\?\.updated_at|record\.updated_at/)

  assert.match(accountsSource, /subLabel: `上次 \$\{last\.label\}`,[\s\S]+refreshTimeLabel,[\s\S]+刷新时间：\$\{refreshTimeTitle\}/)
  assert.match(accountsSource, /meta\.refreshTimeLabel[\s\S]+\{meta\.refreshTimeLabel\}/)
  assert.match(accountsSource, /subscriptionMetaForMobile\.subLabel \|\| subscriptionMetaForMobile\.refreshTimeLabel[\s\S]+subscriptionMetaForMobile\.refreshTimeLabel/)
})
