import assert from 'node:assert/strict'
import test from 'node:test'

import {
  activeTaskLabel,
  registrationMailboxSummary,
} from '../src/lib/taskTypes.ts'

test('registration active task label shows one fixed domain', () => {
  assert.equal(
    activeTaskLabel({
      source: 'manual',
      progress: '0/500',
      platform: 'chatgpt',
      meta: {
        registration_mailbox: {
          provider: 'tempmail_local',
          mode: 'fixed_domain',
          primary_domain: 'f867.com',
          domains: ['f867.com'],
          domain_count: 1,
        },
      },
    }),
    '注册·0/500-f867.com',
  )
})

test('registration active task label collapses multiple domains', () => {
  assert.equal(
    activeTaskLabel({
      source: 'registration_task',
      progress: '12/500',
      meta: {
        registration_mailbox: {
          provider: 'tempmail_local',
          domains: ['first.example', 'second.example'],
          domain_count: 2,
        },
      },
    }),
    '注册·12/500-多域名',
  )
})

test('registration active task label names every compatible HME provider as iCloud', () => {
  for (const provider of [
    'hme_ready_api',
    'helper_ready_api',
    'icloud_hme',
    'icloud_hme_ready',
    'icloud_hme_helper_ready',
  ]) {
    assert.equal(
      activeTaskLabel({
        source: 'register',
        progress: '3/500',
        platform: 'chatgpt',
        meta: { registration_mailbox: { provider } },
      }),
      '注册·3/500-iCloud',
    )
  }
})

test('registration active task label never falls back to ChatGPT', () => {
  assert.equal(
    activeTaskLabel({
      source: 'manual',
      progress: '0/500',
      platform: 'ChatGPT',
      meta: {},
    }),
    '注册·0/500',
  )
})

test('legacy domain metadata remains readable and other task labels stay unchanged', () => {
  assert.equal(
    registrationMailboxSummary({
      mail_provider: 'tempmail_local',
      tempmail_fixed_domains: '["One.Example", "two.example"]',
    }),
    '多域名',
  )
  assert.equal(
    activeTaskLabel({
      source: 'batch_custom_email_recheck',
      progress: '2/3',
      platform: 'chatgpt',
      meta: { email_count: 3 },
    }),
    '批量邮箱测活 · 2/3 · 3 个邮箱',
  )
})
