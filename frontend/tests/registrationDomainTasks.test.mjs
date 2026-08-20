import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  REGISTRATION_DOMAIN_TASK_MODE_COMBINED,
  REGISTRATION_DOMAIN_TASK_MODE_PER_DOMAIN,
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
  assert.match(accountsSource, /registrationTaskCreateEndpoint\(effectiveDomainTaskMode\)/)
  assert.match(registerPageSource, /registrationTaskCreateEndpoint\(effectiveDomainTaskMode\)/)
  assert.match(accountsSource, /setRegistrationDomainTaskGroup\(group\)/)
  assert.match(registerPageSource, /setRegistrationDomainTaskGroup\(createdGroup\)/)
})
