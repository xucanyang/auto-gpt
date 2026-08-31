import assert from 'node:assert/strict'
import test from 'node:test'

import {
  resolveTempMailPreferredDomains,
  sameTempMailDomainOrder,
  saveTempMailPreferredDomains,
  tempMailPreferredDomainsConfigPatch,
} from '../src/lib/tempMailDomainPreferences.ts'

test('configured TempMail preferred domains retain normalized database order', () => {
  assert.deepEqual(
    resolveTempMailPreferredDomains('chatgpt', [
      '@One.Example',
      '.two.example',
      'one.example',
    ]),
    ['one.example', 'two.example'],
  )
})

test('preferred-domain config patch persists an explicit empty selection', () => {
  assert.deepEqual(tempMailPreferredDomainsConfigPatch([]), {
    tempmail_fixed_domains: '[]',
    tempmail_primary_domain: '',
  })
})

test('saving preferred domains writes the instance config database contract', async () => {
  const calls = []
  const saved = await saveTempMailPreferredDomains(
    ' ChatGPT ',
    ['@Nbsov.Asia', '.5ugu.com', 'nbsov.asia'],
    async (path, options) => {
      calls.push({ path, options })
      return { ok: true }
    },
  )

  assert.deepEqual(saved, ['nbsov.asia', '5ugu.com'])
  assert.equal(calls.length, 1)
  assert.equal(calls[0].path, '/config')
  assert.equal(calls[0].options.method, 'PUT')
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    data: {
      tempmail_fixed_domains: '["nbsov.asia","5ugu.com"]',
      tempmail_primary_domain: 'nbsov.asia',
    },
  })
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
