import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  formatRegistrationPaymentEvent,
  isRegistrationTaskSnapshot,
  partitionRegistrationTaskLogs,
  registrationLogRegionForLine,
  registrationPaymentEventRegion,
} from '../src/lib/registrationTaskLogs.ts'

const taskLogPanelSource = await readFile(new URL('../src/components/TaskLogPanel.tsx', import.meta.url), 'utf8')
const taskLogTabsSource = await readFile(new URL('../src/features/auth/components/RegistrationTaskLogTabs.tsx', import.meta.url), 'utf8')
const taskLogLibSource = await readFile(new URL('../src/lib/registrationTaskLogs.ts', import.meta.url), 'utf8')
const appStylesSource = await readFile(new URL('../src/index.css', import.meta.url), 'utf8')

test('registration logs are split by stable business boundaries', () => {
  assert.equal(
    registrationLogRegionForLine('[10:00:00] [1/2][步骤04/09 提交注册] 已提交'),
    'registration',
  )
  assert.equal(
    registrationLogRegionForLine('[10:00:01] [0 元试用资格] 开始｜账号=a***@example.com'),
    'zero_amount',
  )
  assert.equal(
    registrationLogRegionForLine('[10:00:01] [链接格式 + 支付方式] 开始｜账号=a***@example.com'),
    'payment_details',
  )
  assert.equal(
    registrationLogRegionForLine('[10:00:02] [PayPal 跟进][账号=a***@example.com] 开始提取 PayPal approval URL'),
    'payment_link',
  )
  assert.equal(
    registrationLogRegionForLine('[10:00:03] [PayPal 跟进][账号=a***@example.com] 开始提交 PayPal 支付队列'),
    'payment',
  )
  assert.equal(
    registrationLogRegionForLine('[10:00:04] [支付后登录][代理] 候选=1/2'),
    'payment',
  )
  assert.equal(
    registrationLogRegionForLine('[10:00:05] 未知历史日志仍然可见'),
    'registration',
  )
})

test('structured PayPal events keep link extraction separate from final payment', () => {
  assert.equal(registrationPaymentEventRegion({ stage: 'extracting_link' }), 'payment_link')
  assert.equal(registrationPaymentEventRegion({ stage: 'extract_failed' }), 'payment_link')
  assert.equal(registrationPaymentEventRegion({ stage: 'payment_submitted' }), 'payment')
  assert.equal(registrationPaymentEventRegion({ stage: 'payment_authorized' }), 'payment')
  assert.match(
    formatRegistrationPaymentEvent({
      stage: 'payment_authorized',
      account: 'a***@example.com',
      message: '支付结果已回读',
      created_at: '2026-08-21T10:11:12+08:00',
    }),
    /^\[2026-08-21 10:11:12\] \[支付\]\[支付成功\]\[账号=a\*\*\*@example\.com\]/,
  )
})

test('partitioning deduplicates persisted events already present in raw logs', () => {
  const raw = [
    '[10:00:00] [1/1][步骤09/09 完成] 注册成功',
    '[10:00:01] [0 元试用资格] 完成｜结果=0 元可用',
    '[10:00:01] [链接格式 + 支付方式] 完成｜结果=检测完成',
    '[10:00:02] [PayPal 跟进][账号=a***@example.com] 开始提取 PayPal approval URL',
    '[10:00:03] [PayPal 跟进][账号=a***@example.com] 等待支付结果：running',
    '[10:00:04] [支付后登录][代理] 候选=1/2',
  ]
  const result = partitionRegistrationTaskLogs(raw, [
    {
      stage: 'extracting_link',
      account: 'a***@example.com',
      message: '开始提取 PayPal approval URL',
      created_at: '2026-08-21T10:00:02+08:00',
    },
    {
      stage: 'waiting_result',
      account: 'a***@example.com',
      message: '等待支付结果：running',
      created_at: '2026-08-21T10:00:03+08:00',
    },
    {
      stage: 'payment_authorized',
      account: 'a***@example.com',
      message: '支付结果已回读',
      created_at: '2026-08-21T10:05:00+08:00',
    },
  ])

  assert.equal(result.registration.length, 1)
  assert.equal(result.zero_amount.length, 1)
  assert.equal(result.payment_details.length, 1)
  assert.equal(result.payment_link.length, 1)
  assert.equal(result.payment.filter((line) => line.includes('等待支付结果：running')).length, 1)
  assert.equal(result.payment.filter((line) => line.includes('支付结果已回读')).length, 1)
  assert.ok(result.payment.some((line) => line.includes('[支付后登录][代理]')))
})

test('registration detection uses task metadata and timeline fallback without broad source matching', () => {
  assert.equal(isRegistrationTaskSnapshot({ source: 'manual', platform: 'chatgpt' }), true)
  assert.equal(isRegistrationTaskSnapshot({ source: 'cpa_replenish', platform: 'chatgpt' }), true)
  assert.equal(isRegistrationTaskSnapshot({
    source: 'future_source',
    meta: { registration_pipeline_request: {} },
  }), true)
  assert.equal(isRegistrationTaskSnapshot(
    { source: 'legacy' },
    ['[10:00:00] [1/2][步骤01/09 准备] 开始'],
  ), true)
  assert.equal(isRegistrationTaskSnapshot({ source: 'registration_paypal_payment' }), false)
  assert.equal(isRegistrationTaskSnapshot({ source: 'batch_invalid_recheck' }), false)
})

test('the shared task panel exposes five responsive registration regions and live payment polling', () => {
  assert.match(taskLogPanelSource, /partitionRegistrationTaskLogs\(lines, paymentEvents\)/)
  assert.match(taskLogPanelSource, /<RegistrationTaskLogTabs/)
  assert.match(taskLogPanelSource, /registrationPaypalTracking/)
  assert.match(taskLogPanelSource, /cache: 'no-store'/)
  assert.match(taskLogPanelSource, /复制当前区域/)
  assert.match(taskLogPanelSource, /!isRegistrationTask && paymentEvents\.length > 0/)
  assert.match(taskLogPanelSource, /PayPal 自动支付时间线/)

  for (const label of ['注册', '0元检测', '支付明细', '提链', '支付']) {
    assert.ok(taskLogLibSource.includes(label))
  }
  assert.match(taskLogTabsSource, /REGISTRATION_LOG_REGION_LABELS/)
  assert.match(appStylesSource, /\.registration-task-log-tabs \.ant-tabs-tab \{[\s\S]+?flex: 1 1 0/)
  assert.match(appStylesSource, /\.registration-task-log-tab-label \{[\s\S]+?white-space: nowrap/)
  assert.match(appStylesSource, /@media \(max-width: 768px\)[\s\S]+?\.registration-task-log-tab-icon \{[\s\S]+?display: none/)
})
