import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  buildActiveTaskStopTargets,
  buildBatchStopRequest,
  failedBatchStopTargetKeys,
} from '../src/lib/activeTaskControls.ts'

const activeTasksPanelSource = await readFile(
  new URL('../src/features/accounts/components/ActiveTasksPanel.tsx', import.meta.url),
  'utf8',
)
const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const taskLogPanelSource = await readFile(
  new URL('../src/components/TaskLogPanel.tsx', import.meta.url),
  'utf8',
)
const activeTasksQuerySource = await readFile(
  new URL('../src/features/accounts/hooks/useActiveTasksQuery.ts', import.meta.url),
  'utf8',
)

const task = (id, overrides = {}) => ({
  id,
  source: 'manual',
  progress: '2/100',
  capabilities: {
    stop_after_current: true,
    stop_modes: ['immediate', 'after_current'],
  },
  control: {},
  meta: {},
  ...overrides,
})

test('ordinary active tasks remain independently selectable stop targets', () => {
  const targets = buildActiveTaskStopTargets([
    task('task-a'),
    task('task-b', { capabilities: { stop_after_current: false, stop_modes: ['immediate'] } }),
  ])

  assert.equal(targets.length, 2)
  assert.equal(targets[0].key, 'task:task-a')
  assert.equal(targets[0].supportsAfterCurrent, true)
  assert.equal(targets[1].supportsAfterCurrent, false)
  assert.deepEqual(buildBatchStopRequest(targets, 'immediate'), {
    mode: 'immediate',
    task_ids: ['task-a', 'task-b'],
    registration_domain_group_ids: [],
  })
})

test('rotating children collapse into one group target while retaining every log snapshot', () => {
  const groupMeta = {
    registration_domain_task_group: {
      id: 'register-group-1',
      mode: 'rotating',
    },
  }
  const targets = buildActiveTaskStopTargets([
    task('rotation-a', { meta: groupMeta }),
    task('ordinary'),
    task('rotation-b', { meta: groupMeta }),
  ])

  assert.equal(targets.length, 2)
  assert.equal(targets[0].targetType, 'registration_domain_group')
  assert.equal(targets[0].targetId, 'register-group-1')
  assert.equal(targets[0].items.length, 2)
  assert.match(targets[0].label, /2 个运行任务/)
  assert.deepEqual(buildBatchStopRequest([targets[0]], 'after_current'), {
    mode: 'after_current',
    task_ids: [],
    registration_domain_group_ids: ['register-group-1'],
  })
})

test('immediate-stop targets are marked while graceful targets can still be upgraded', () => {
  const targets = buildActiveTaskStopTargets([
    task('immediate', { control: { stop_mode: 'immediate' } }),
    task('graceful', { control: { stop_after_current_requested: true } }),
  ])

  assert.equal(targets[0].stopMode, 'immediate')
  assert.equal(targets[1].stopMode, 'after_current')
})

test('only failed batch-stop targets remain selected for retry', () => {
  const keys = failedBatchStopTargetKeys({
    results: [
      { target_type: 'task', target_id: 'task-a', status: 'accepted' },
      { target_type: 'task', target_id: 'task-b', status: 'failed' },
      {
        target_type: 'registration_domain_group',
        target_id: 'group-a',
        status: 'failed',
      },
      { target_type: 'task', target_id: 'missing', status: 'not_found' },
    ],
  })

  assert.deepEqual([...keys], [
    'task:task-b',
    'registration_domain_group:group-a',
  ])
})

test('active task surface exposes selection, both stop modes, log access, and live refresh', () => {
  assert.match(activeTasksPanelSource, /全选可停止任务/)
  assert.match(activeTasksPanelSource, /完成当前后停止/)
  assert.match(activeTasksPanelSource, /立即停止/)
  assert.match(activeTasksPanelSource, /查看子任务日志/)
  assert.match(activeTasksPanelSource, /buildBatchStopRequest/)
  assert.match(accountsSource, /apiFetch\('\/tasks\/batch-stop'/)
  assert.match(activeTasksQuerySource, /refetchInterval: enabled \? 3000 : false/)
  assert.match(taskLogPanelSource, /registrationDomainTaskGroup\.mode[\s\S]+?=== 'rotating'/)
  assert.match(taskLogPanelSource, /domain-groups\/\$\{encodeURIComponent\(rotatingRegistrationGroupId\)\}\/stop/)
  assert.match(taskLogPanelSource, /立即停止整组/)
})
