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
