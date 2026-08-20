import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  REGISTRATION_DOMAIN_TASK_MODE_COMBINED,
  REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN,
  createRegistrationTasks,
  normalizeRegistrationDomainTaskGroup,
  normalizeRegistrationDomainTaskMode,
  registrationDomainTaskTotalTarget,
  registrationTaskCreateEndpoint,
} from '../src/lib/registrationDomainTasks.ts'

const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const registerPageSource = await readFile(new URL('../src/pages/RegisterTaskPage.tsx', import.meta.url), 'utf8')
const registerModalSource = await readFile(new URL('../src/features/auth/components/RegisterTaskModal.tsx', import.meta.url), 'utf8')
const modeFieldSource = await readFile(new URL('../src/features/auth/components/RegistrationDomainTaskModeField.tsx', import.meta.url), 'utf8')
const groupTabsSource = await readFile(new URL('../src/features/auth/components/RegistrationDomainTaskGroupTabs.tsx', import.meta.url), 'utf8')
const appStylesSource = await readFile(new URL('../src/index.css', import.meta.url), 'utf8')

test('registration task mode defaults to the compatible combined endpoint', () => {
  assert.equal(normalizeRegistrationDomainTaskMode(undefined), REGISTRATION_DOMAIN_TASK_MODE_COMBINED)
  assert.equal(normalizeRegistrationDomainTaskMode('unknown'), REGISTRATION_DOMAIN_TASK_MODE_COMBINED)
  assert.equal(normalizeRegistrationDomainTaskMode('per_domain'), REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN)
  assert.equal(registrationTaskCreateEndpoint('combined'), '/tasks/register')
  assert.equal(registrationTaskCreateEndpoint('per_domain'), '/tasks/register/by-domain')
})

test('domain task group response keeps every independent task and partial error', () => {
  const group = normalizeRegistrationDomainTaskGroup({
    task_group_id: 'register-group-1',
    requested_domain_count: 3,
    requested_count_per_task: 100,
    requested_concurrency_per_task: 10,
    tasks: [
      { task_id: 'task-a', domain: '@A.example', position: 1 },
      { task_id: 'task-b', domain: '.B.example', position: 2 },
      { task_id: 'task-b', domain: 'duplicate.example', position: 3 },
    ],
    errors: [
      { domain: 'C.example', position: 3, message: 'capacity unavailable' },
    ],
  })

  assert.deepEqual(group, {
    groupId: 'register-group-1',
    mode: 'per_domain',
    requestedDomainCount: 3,
    requestedCountPerTask: 100,
    requestedConcurrencyPerTask: 10,
    tasks: [
      { taskId: 'task-a', domain: 'a.example', position: 1 },
      { taskId: 'task-b', domain: 'b.example', position: 2 },
    ],
    errors: [
      { domain: 'c.example', position: 3, message: 'capacity unavailable' },
    ],
  })
  assert.equal(registrationDomainTaskTotalTarget(6, 100), 600)
})

test('per-domain creation falls back to independent legacy tasks during a rolling deploy', async () => {
  const calls = []
  const apiFetch = async (path, options) => {
    const body = JSON.parse(String(options?.body || '{}'))
    calls.push({ path, body })
    if (path === '/tasks/register/by-domain') {
      throw Object.assign(new Error('not found'), { status: 404 })
    }
    const domain = body.extra.tempmail_fixed_domains[0]
    if (domain === 'broken.example') throw new Error('domain unavailable')
    return { task_id: `task-${domain}` }
  }

  const response = await createRegistrationTasks(apiFetch, {
    platform: 'chatgpt',
    count: 100,
    concurrency: 10,
    executor_type: 'headless',
    extra: {
      mail_provider: 'tempmail_local',
      tempmail_mode: 'fixed_domain',
      tempmail_fixed_domains: ['first.example', 'broken.example', 'third.example'],
      shared_setting: 'preserved',
    },
  }, 'per_domain')
  const group = normalizeRegistrationDomainTaskGroup(response)

  assert.equal(calls[0].path, '/tasks/register/by-domain')
  assert.deepEqual(calls.slice(1).map((item) => item.path), [
    '/tasks/register',
    '/tasks/register',
    '/tasks/register',
  ])
  assert.deepEqual(
    calls.slice(1).map((item) => item.body.extra.tempmail_fixed_domains),
    [['first.example'], ['broken.example'], ['third.example']],
  )
  assert.ok(calls.slice(1).every((item) => item.body.count === 100))
  assert.ok(calls.slice(1).every((item) => item.body.concurrency === 10))
  assert.ok(calls.slice(1).every((item) => item.body.extra.shared_setting === 'preserved'))
  assert.deepEqual(group?.tasks.map((item) => item.domain), ['first.example', 'third.example'])
  assert.deepEqual(group?.errors, [{
    domain: 'broken.example',
    position: 2,
    message: 'domain unavailable',
  }])
})

test('per-domain creation never hides a non-routing batch API failure', async () => {
  const failure = Object.assign(new Error('service unavailable'), { status: 503 })
  await assert.rejects(
    createRegistrationTasks(
      async () => { throw failure },
      { extra: { tempmail_fixed_domains: ['first.example'] } },
      'per_domain',
    ),
    /service unavailable/,
  )
})

test('both registration surfaces expose per-domain mode and task-log switching', () => {
  for (const source of [registerModalSource, registerPageSource]) {
    assert.match(source, /<RegistrationDomainTaskModeField/)
    assert.match(source, /<RegistrationDomainTaskGroupTabs/)
  }
  assert.match(modeFieldSource, /合并任务/)
  assert.match(modeFieldSource, /按域名拆分/)
  assert.match(modeFieldSource, /总目标/)
  assert.match(groupTabsSource, /group\.tasks\.map/)
  assert.match(groupTabsSource, /onSelectTask/)
  assert.match(appStylesSource, /\.registration-domain-task-group \{[\s\S]+?width: 100%[\s\S]+?min-width: 0/)
  assert.match(appStylesSource, /\.registration-domain-task-group \.ant-alert-description \{[\s\S]+?overflow-wrap: anywhere/)
  assert.match(accountsSource, /createRegistrationTasks\([\s\S]+?effectiveDomainTaskMode/)
  assert.match(registerPageSource, /createRegistrationTasks\([\s\S]+?effectiveDomainTaskMode/)
  assert.match(accountsSource, /setRegistrationDomainTaskGroup\(group\)/)
  assert.match(registerPageSource, /setRegistrationDomainTaskGroup\(createdGroup\)/)
})
