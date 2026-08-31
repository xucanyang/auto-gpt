import assert from 'node:assert/strict'
import test from 'node:test'

import {
  loadTempMailPreferredDomains,
  resolveTempMailPreferredDomains,
  sameTempMailDomainOrder,
  saveTempMailPreferredDomains,
  tempMailPreferredDomainsStorageKey,
} from '../src/lib/tempMailDomainPreferences.ts'

function memoryStorage() {
  const values = new Map()
  return {
    getItem(key) {
      return values.has(key) ? values.get(key) : null
    },
    setItem(key, value) {
      values.set(key, String(value))
    },
  }
}

test('TempMail preferred domains persist normalized order per scope', () => {
  const storage = memoryStorage()

  assert.equal(loadTempMailPreferredDomains('chatgpt', storage), null)
  assert.equal(saveTempMailPreferredDomains(' ChatGPT ', [
    '@One.Example',
    '.two.example',
    'one.example',
  ], storage), true)
  assert.deepEqual(loadTempMailPreferredDomains('chatgpt', storage), [
    'one.example',
    'two.example',
  ])
  assert.equal(loadTempMailPreferredDomains('google', storage), null)
  assert.equal(
    tempMailPreferredDomainsStorageKey(' ChatGPT '),
    'auto-chatgpt.tempmail-preferred-domains.v1.chatgpt',
  )
})

test('stored empty preference overrides fallback while missing storage migrates old domains', () => {
  const storage = memoryStorage()

  assert.deepEqual(
    resolveTempMailPreferredDomains('chatgpt', ['Legacy.Example'], storage),
    ['legacy.example'],
  )
  assert.equal(saveTempMailPreferredDomains('chatgpt', [], storage), true)
  assert.deepEqual(
    resolveTempMailPreferredDomains('chatgpt', ['legacy.example'], storage),
    [],
  )
})

test('preferred domain order remains significant for the primary domain', () => {
  assert.equal(
    sameTempMailDomainOrder(['first.example', 'second.example'], ['first.example', 'second.example']),
    true,
  )
  assert.equal(
    sameTempMailDomainOrder(['first.example', 'second.example'], ['second.example', 'first.example']),
    false,
  )
})

test('auto-plus3 removes the failed 2026-09-01 domains exactly once', () => {
  const storage = memoryStorage()
  const initial = [
    'nbsov.asia',
    'vlmns.asia',
    'sefg.asia',
    '5ugu.com',
    'gdyfcw.com',
    'xmdjxds.com',
    'yhegsi.com',
    'ieazg.com',
    'f867.com',
    'tadouhy.com',
    'uoipra.com',
    'niudingwang.com',
  ]
  const expected = [
    'nbsov.asia',
    'vlmns.asia',
    '5ugu.com',
    'uoipra.com',
    'niudingwang.com',
  ]

  assert.equal(saveTempMailPreferredDomains('chatgpt', initial, storage), true)
  assert.deepEqual(
    loadTempMailPreferredDomains('chatgpt', storage, 'AUTO-PLUS3.CCCY.ME'),
    expected,
  )
  assert.equal(
    storage.getItem(tempMailPreferredDomainsStorageKey('chatgpt')),
    JSON.stringify(expected),
  )

  assert.equal(saveTempMailPreferredDomains('chatgpt', ['sefg.asia', '5ugu.com'], storage), true)
  assert.deepEqual(
    loadTempMailPreferredDomains('chatgpt', storage, 'auto-plus3.cccy.me'),
    ['sefg.asia', '5ugu.com'],
  )
})

test('failed-domain cleanup does not change preferences on other instances', () => {
  const storage = memoryStorage()
  const domains = ['sefg.asia', '5ugu.com']

  assert.equal(saveTempMailPreferredDomains('chatgpt', domains, storage), true)
  assert.deepEqual(
    loadTempMailPreferredDomains('chatgpt', storage, 'auto-plus.cccy.me'),
    domains,
  )
})
