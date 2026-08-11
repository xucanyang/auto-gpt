import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const read = (relativePath) => fs.readFileSync(path.join(root, relativePath), 'utf8')

test('project datetime utility fixes display to Asia/Shanghai and treats legacy naive rows as UTC', () => {
  const source = read('src/lib/dateTime.ts')
  assert.match(source, /PROJECT_TIME_ZONE\s*=\s*'Asia\/Shanghai'/)
  assert.match(source, /DATE_TIME_WITHOUT_OFFSET/)
  assert.match(source, /`\$\{text\.replace\(' ', 'T'\)\}Z`/)
  assert.match(source, /timeZone:\s*PROJECT_TIME_ZONE/)
  assert.match(source, /hourCycle:\s*'h23'/)
})

test('task, account, proxy, delivery and settings surfaces use the shared Beijing formatter', () => {
  const targets = [
    'src/pages/TaskHistory.tsx',
    'src/components/task-detail/TaskDetailHeader.tsx',
    'src/pages/Accounts.tsx',
    'src/pages/BaxiGptCdkPool.tsx',
    'src/pages/PhonePool.tsx',
    'src/pages/DeliveryCards.tsx',
    'src/pages/Proxies.tsx',
    'src/pages/CodexUsagePage.tsx',
    'src/pages/Settings.tsx',
    'src/components/RegistrationDiagnosticsPanel.tsx',
  ]
  for (const target of targets) {
    assert.match(read(target), /@\/lib\/dateTime/, `${target} must use the shared project timezone module`)
  }
})

test('production compose and image declare the same Beijing timezone', () => {
  const dockerfile = read('../Dockerfile')
  const compose = read('../docker-compose.multi.yml')
  assert.match(dockerfile, /TZ=Asia\/Shanghai/)
  assert.match(dockerfile, /tzdata/)
  assert.equal((compose.match(/TZ:\s*Asia\/Shanghai/g) || []).length, 4)
})

test('task history no longer delegates timestamps to the visitor browser timezone', () => {
  const taskHistory = read('src/pages/TaskHistory.tsx')
  const taskHeader = read('src/components/task-detail/TaskDetailHeader.tsx')
  assert.doesNotMatch(taskHistory, /new Date\([^)]*created_at[^)]*\)\.toLocaleString/)
  assert.doesNotMatch(taskHeader, /new Date\([^)]*created_at[^)]*\)\.toLocaleString/)
})
