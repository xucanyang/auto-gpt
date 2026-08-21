import assert from 'node:assert/strict'
import test from 'node:test'

import {
  clearTempMailCurrentSelection,
  clearTempMailPreferredSelection,
  normalizeTempMailDomainOptions,
  orderTempMailSelectedDomains,
  updateTempMailCurrentSelection,
  updateTempMailPreferredMembership,
} from '../src/lib/tempMailDomainSelection.ts'

test('bulk clear helpers preserve the preferred-versus-current selection boundary', () => {
  assert.deepEqual(clearTempMailPreferredSelection(), {
    preferredDomains: [],
    selectedDomains: [],
    primaryDomain: '',
  })
  assert.deepEqual(clearTempMailCurrentSelection(), {
    selectedDomains: [],
    primaryDomain: '',
  })
})

test('TempMail API domain normalization preserves first-occurrence response order', () => {
  assert.deepEqual(normalizeTempMailDomainOptions([
    { domain: 'Second.Example', status: 'ACTIVE' },
    { domain: '@first.example', dns_status: 'PRESENT' },
    { domain: 'second.example', available: false, status: 'disabled' },
    { domain: '.third.example' },
  ]), [
    {
      domain: 'second.example',
      available: true,
      status: 'active',
      dns_status: '',
    },
    {
      domain: 'first.example',
      available: true,
      status: '',
      dns_status: 'present',
    },
    {
      domain: 'third.example',
      available: true,
      status: '',
      dns_status: '',
    },
  ])
})

test('all-domain membership changes do not auto-select a domain for the current task', () => {
  const added = updateTempMailPreferredMembership(
    ['one.example'],
    ['one.example'],
    'two.example',
    true,
  )
  assert.deepEqual(added, {
    preferredDomains: ['one.example', 'two.example'],
    selectedDomains: ['one.example'],
  })

  const removed = updateTempMailPreferredMembership(
    added.preferredDomains,
    ['one.example', 'two.example'],
    'one.example',
    false,
  )
  assert.deepEqual(removed, {
    preferredDomains: ['two.example'],
    selectedDomains: ['two.example'],
  })
})

test('preferred-domain checkboxes independently control the multi-domain task selection', () => {
  const preferred = ['one.example', 'two.example', 'three.example']
  const selected = updateTempMailCurrentSelection(
    ['one.example'],
    preferred,
    'three.example',
    true,
  )
  assert.deepEqual(selected, ['one.example', 'three.example'])
  assert.deepEqual(
    updateTempMailCurrentSelection(selected, preferred, 'one.example', false),
    ['three.example'],
  )
  assert.deepEqual(preferred, ['one.example', 'two.example', 'three.example'])
})

test('task domains remain in preferred order and exclude unavailable or non-preferred values', () => {
  assert.deepEqual(orderTempMailSelectedDomains(
    ['third.example', 'outside.example', 'first.example'],
    ['first.example', 'second.example', 'third.example'],
    ['third.example', 'first.example'],
  ), ['first.example', 'third.example'])
  assert.deepEqual(orderTempMailSelectedDomains(
    [],
    ['first.example', 'second.example'],
  ), [])
})
