import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  REGISTRATION_DOMAIN_TASK_MODE_COMBINED,
  REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN,
  REGISTRATION_DOMAIN_TASK_MODE_ROTATING,
  createRegistrationTasks,
  isRegistrationDomainTaskGroupActive,
  normalizeRegistrationDomainTaskGroup,
  normalizeRegistrationDomainTaskMode,
  registrationDomainTaskTotalTarget,
  registrationTaskCreateEndpoint,
} from '../src/lib/registrationDomainTasks.ts'

const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const registerPageSource = await readFile(new URL('../src/pages/RegisterTaskPage.tsx', import.meta.url), 'utf8')
const registerModalSource = await readFile(new URL('../src/features/auth/components/RegisterTaskModal.tsx', import.meta.url), 'utf8')
const tempMailDomainSelectorSource = await readFile(new URL('../src/features/auth/components/TempMailDomainSelector.tsx', import.meta.url), 'utf8')
const modeFieldSource = await readFile(new URL('../src/features/auth/components/RegistrationDomainTaskModeField.tsx', import.meta.url), 'utf8')
const groupTabsSource = await readFile(new URL('../src/features/auth/components/RegistrationDomainTaskGroupTabs.tsx', import.meta.url), 'utf8')
const appStylesSource = await readFile(new URL('../src/index.css', import.meta.url), 'utf8')

test('registration task mode defaults to the compatible combined endpoint', () => {
  assert.equal(normalizeRegistrationDomainTaskMode(undefined), REGISTRATION_DOMAIN_TASK_MODE_COMBINED)
  assert.equal(normalizeRegistrationDomainTaskMode('unknown'), REGISTRATION_DOMAIN_TASK_MODE_COMBINED)
  assert.equal(normalizeRegistrationDomainTaskMode('per_domain'), REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN)
  assert.equal(normalizeRegistrationDomainTaskMode('rotating'), REGISTRATION_DOMAIN_TASK_MODE_ROTATING)
  assert.equal(registrationTaskCreateEndpoint('combined'), '/tasks/register')
  assert.equal(registrationTaskCreateEndpoint('per_domain'), '/tasks/register/by-domain')
  assert.equal(registrationTaskCreateEndpoint('rotating'), '/tasks/register/by-domain')
})

test('domain task creation canonicalizes a legacy primary-only request', async () => {
  const calls = []
  await createRegistrationTasks(async (path, options) => {
    calls.push({ path, body: JSON.parse(String(options?.body || '{}')) })
    return {
      task_group_id: 'legacy-primary-group',
      mode: 'per_domain',
      tasks: [{ task_id: 'task-legacy', domain: 'legacy.example', position: 1 }],
      errors: [],
    }
  }, {
    count: 1,
    extra: {
      tempmail_primary_domain: '@Legacy.Example',
      tempmail_fixed_domains: [],
    },
  }, REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN)

  assert.equal(calls.length, 1)
  assert.equal(calls[0].path, '/tasks/register/by-domain')
  assert.deepEqual(calls[0].body.extra.tempmail_fixed_domains, ['legacy.example'])
  assert.equal(calls[0].body.extra.tempmail_primary_domain, 'legacy.example')
})

test('domain task creation rejects an empty current selection before any API call', async () => {
  let callCount = 0
  await assert.rejects(
    () => createRegistrationTasks(async () => {
      callCount += 1
      return {}
    }, {
      count: 1,
      extra: {
        tempmail_primary_domain: '',
        tempmail_fixed_domains: [],
      },
    }, REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN),
    /请在优选域名中勾选至少一个本次使用的可用域名/,
  )
  assert.equal(callCount, 0)
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
    state: 'running',
    requestedDomainCount: 3,
    taskAttemptCount: 2,
    requestedCountPerTask: 100,
    requestedConcurrencyPerTask: 10,
    activeDomainSlots: 2,
    policy: {},
    counts: {},
    domains: [],
    failure: {},
    technicalFailures: [],
    stopReason: '',
    tasks: [
      { taskId: 'task-a', domain: 'a.example', position: 1, state: 'active', attempt: 1, isCurrent: true, error: '', quality: {}, trigger: {} },
      { taskId: 'task-b', domain: 'b.example', position: 2, state: 'active', attempt: 1, isCurrent: true, error: '', quality: {}, trigger: {} },
    ],
    errors: [
      { domain: 'c.example', position: 3, state: 'failed', message: 'capacity unavailable', retryCount: 0, retryLimit: 0 },
    ],
  })
  assert.equal(registrationDomainTaskTotalTarget(6, 100), 600)
})

test('rotating creation uses the server scheduler and never expands through the legacy API', async () => {
  const calls = []
  const response = await createRegistrationTasks(async (path, options) => {
    const body = JSON.parse(String(options?.body || '{}'))
    calls.push({ path, body })
    return {
      task_group_id: 'rotation-1',
      mode: 'rotating',
      state: 'running',
      requested_domain_count: 2,
      requested_count_per_task: 100,
      requested_concurrency_per_task: 5,
      active_domain_slots: 1,
      policy: {
        rejection_rate_threshold_percent: 50,
        rejection_rate_min_samples: 10,
        no_link_streak_threshold: 10,
      },
      counts: { active: 1, pending: 1 },
      tasks: [{ task_id: 'task-first', domain: 'first.example', position: 1, state: 'active' }],
      domains: [
        { task_id: 'task-first', domain: 'first.example', position: 1, state: 'active' },
        { domain: 'second.example', position: 2, state: 'pending' },
      ],
      errors: [],
    }
  }, {
    count: 100,
    concurrency: 5,
    registration_zero_amount_eligibility_enabled: true,
    registration_paypal_link_enabled: true,
    registration_domain_active_slots: 1,
    extra: { tempmail_fixed_domains: ['first.example', 'second.example'] },
  }, 'rotating')
  const group = normalizeRegistrationDomainTaskGroup(response)

  assert.equal(calls.length, 1)
  assert.equal(calls[0].path, '/tasks/register/by-domain')
  assert.equal(calls[0].body.registration_domain_task_mode, 'rotating')
  assert.equal(calls[0].body.registration_domain_active_slots, 1)
  assert.equal(group?.mode, 'rotating')
  assert.equal(group?.activeDomainSlots, 1)
  assert.deepEqual(group?.counts, { active: 1, pending: 1 })
  assert.equal(group?.domains[1].state, 'pending')
  assert.equal(isRegistrationDomainTaskGroupActive(group), true)
})

test('rotation normalization preserves retry task history without inflating domain count', () => {
  const group = normalizeRegistrationDomainTaskGroup({
    task_group_id: 'rotation-retry',
    mode: 'rotating',
    state: 'running',
    requested_domain_count: 2,
    task_attempt_count: 2,
    active_domain_slots: 1,
    counts: { retry_wait: 1, pending: 1 },
    tasks: [
      {
        task_id: 'task-attempt-1',
        domain: 'first.example',
        position: 1,
        state: 'technical_failed',
        attempt: 1,
        is_current: false,
        error: 'proxy unavailable',
      },
      {
        task_id: 'task-attempt-2',
        domain: 'first.example',
        position: 1,
        state: 'active',
        attempt: 2,
        is_current: true,
      },
    ],
    domains: [
      {
        task_id: 'task-attempt-2',
        domain: 'first.example',
        position: 1,
        state: 'active',
        attempt_count: 2,
        retry_count: 1,
        retry_limit: 2,
        technical_failure: {
          code: 'dynamic_proxy_unavailable',
          label: '动态代理不可用',
        },
      },
      { domain: 'second.example', position: 2, state: 'pending' },
    ],
    technical_failures: [
      { domain: 'first.example', code: 'dynamic_proxy_unavailable' },
    ],
  })

  assert.equal(group?.requestedDomainCount, 2)
  assert.equal(group?.taskAttemptCount, 2)
  assert.deepEqual(group?.tasks.map((item) => [item.attempt, item.isCurrent]), [
    [1, false],
    [2, true],
  ])
  assert.equal(group?.domains[0].retryCount, 1)
  assert.equal(group?.domains[0].technicalFailure.code, 'dynamic_proxy_unavailable')
  assert.equal(group?.technicalFailures.length, 1)
})

test('rotating creation rejects missing quality stages and never falls back on a 404', async () => {
  await assert.rejects(
    createRegistrationTasks(async () => ({}), {
      registration_zero_amount_eligibility_enabled: true,
      registration_paypal_link_enabled: false,
    }, 'rotating'),
    /同时开启注册后 0 元检测和提链/,
  )

  let calls = 0
  const missingEndpoint = Object.assign(new Error('not found'), { status: 404 })
  await assert.rejects(
    createRegistrationTasks(async () => {
      calls += 1
      throw missingEndpoint
    }, {
      registration_zero_amount_eligibility_enabled: true,
      registration_paypal_link_enabled: true,
      extra: { tempmail_fixed_domains: ['first.example', 'second.example'] },
    }, 'rotating'),
    /not found/,
  )
  assert.equal(calls, 1)
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
    state: 'failed',
    message: 'domain unavailable',
    retryCount: 0,
    retryLimit: 0,
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
  assert.match(tempMailDomainSelectorSource, /本次未选择可用域名/)
  assert.match(modeFieldSource, /自动轮换/)
  assert.match(modeFieldSource, /连续未提链阈值/)
  assert.match(modeFieldSource, /本次没有等待域名可补位/)
  assert.match(modeFieldSource, /同类故障跨域重复出现时整组熔断/)
  assert.match(modeFieldSource, /总目标/)
  assert.match(groupTabsSource, /group\.tasks\.map/)
  assert.match(groupTabsSource, /onSelectTask/)
  assert.match(groupTabsSource, /fetchRegistrationDomainTaskGroup/)
  assert.match(groupTabsSource, /停止轮换/)
  assert.match(groupTabsSource, /等待同域技术重试/)
  assert.match(groupTabsSource, /基础设施熔断/)
  assert.match(appStylesSource, /\.registration-domain-task-group \{[\s\S]+?width: 100%[\s\S]+?min-width: 0/)
  assert.match(appStylesSource, /\.registration-domain-task-group \.ant-alert-description \{[\s\S]+?overflow-wrap: anywhere/)
  assert.match(accountsSource, /createRegistrationTasks\([\s\S]+?effectiveDomainTaskMode/)
  assert.match(accountsSource, /fetchRegistrationDomainTaskGroup\([\s\S]+?restoredGroup\.groupId/)
  assert.match(registerPageSource, /createRegistrationTasks\([\s\S]+?effectiveDomainTaskMode/)
  assert.match(accountsSource, /setRegistrationDomainTaskGroup\(group\)/)
  assert.match(registerPageSource, /setRegistrationDomainTaskGroup\(createdGroup\)/)
})
