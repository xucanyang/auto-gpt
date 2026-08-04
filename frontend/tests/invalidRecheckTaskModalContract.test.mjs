import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const modalSource = await readFile(
  new URL('../src/features/auth/components/RegisterTaskModal.tsx', import.meta.url),
  'utf8',
)

test('invalid recheck keeps a dedicated task modal mode', () => {
  const sourceMapper = accountsSource.match(/function taskModalModeFromSource[\s\S]+?\n}/)?.[0] || ''
  assert.match(
    sourceMapper,
    /normalized === 'invalid_recheck' \|\| normalized === 'batch_invalid_recheck'\) return 'invalid_recheck'/,
  )
  assert.doesNotMatch(
    sourceMapper,
    /normalized === 'invalid_recheck' \|\| normalized === 'batch_invalid_recheck'\) return 'resume_auth'/,
  )

  const singleHandler = accountsSource.match(/const handleInvalidRecheck = async[\s\S]+?\n  }/)?.[0] || ''
  const batchHandler = accountsSource.match(/const handleBatchInvalidRecheck = async[\s\S]+?\n  }/)?.[0] || ''
  assert.match(singleHandler, /setTaskModalMode\('invalid_recheck'\)/)
  assert.match(batchHandler, /setTaskModalMode\('invalid_recheck'\)/)
})

test('invalid recheck modal title never presents the task as auth recovery', () => {
  const titleBranch = modalSource.match(
    /if \(taskModalMode === 'invalid_recheck'\) \{[\s\S]+?(?=    if \(taskModalMode === 'resume_auth'\))/,
  )?.[0] || ''
  assert.match(titleBranch, /`失效测活 \$\{taskModalAccount\.email\}`/)
  assert.match(titleBranch, /`批量失效测活 \(\$\{eligible} 个\)`/)
  assert.doesNotMatch(titleBranch, /补抓Auth/)
})
