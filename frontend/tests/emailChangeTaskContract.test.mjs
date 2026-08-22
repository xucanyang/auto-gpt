import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const actionSurfaceSource = await readFile(
  new URL('../src/features/accounts/components/AccountActionSurface.tsx', import.meta.url),
  'utf8',
)
const modalSource = await readFile(
  new URL('../src/features/accounts/components/ChangeEmailTaskModal.tsx', import.meta.url),
  'utf8',
)
const taskLogPanelSource = await readFile(
  new URL('../src/components/TaskLogPanel.tsx', import.meta.url),
  'utf8',
)
const taskTypesSource = await readFile(new URL('../src/lib/taskTypes.ts', import.meta.url), 'utf8')

test('account action opens the dedicated email-change task surface', () => {
  assert.match(actionSurfaceSource, /actionId === 'change_email'/)
  assert.match(actionSurfaceSource, /setChangeEmailOpen\(true\)/)
  assert.match(actionSurfaceSource, /<ChangeEmailTaskModal/)
  assert.match(taskTypesSource, /chatgpt_email_change: '邮箱换绑'/)
})

test('target mailbox selection uses real provider boundaries', () => {
  assert.match(modalSource, /HME Ready 自动分配/)
  assert.match(modalSource, /automatic_ready_checkout|Ready 服务自动分配|Ready 自动分配/)
  assert.doesNotMatch(modalSource, /hme_available|hmeOptions|hmeRef/)
  assert.doesNotMatch(modalSource, /provider === 'hme_ready_api'.*body\.target_email/)
  assert.match(modalSource, /provider === 'tempmail_local'.*body\.domain = domain/)
  assert.match(modalSource, /provider === 'manual_email_otp'.*body\.target_email = manualEmail\.trim\(\)/)
})

test('email-change recovery and release are explicit durable actions', () => {
  assert.match(modalSource, /email-change\/tasks\/\$\{encodeURIComponent\(taskId\)\}/)
  assert.match(modalSource, /email-change\/reservations\/\$\{encodeURIComponent\(reservationRef\)\}\/release/)
  assert.match(modalSource, /\/tasks\/\$\{encodeURIComponent\(taskId\)\}\/resume/)
  assert.match(modalSource, /remoteBoundaryCrossed/)
  assert.match(modalSource, /继续恢复/)
  assert.match(modalSource, /释放并重选/)
})

test('optional social-login removal stays default-off and OTP input remains task-scoped', () => {
  assert.match(modalSource, /useState\(false\)/)
  assert.match(modalSource, /remove_social_subs: removeSocialSubs/)
  assert.match(modalSource, /同时移除社交登录绑定/)
  assert.match(taskLogPanelSource, /taskSource === 'chatgpt_email_change'/)
  assert.match(taskLogPanelSource, /taskSnapshot\?\.pending_verification/)
})
