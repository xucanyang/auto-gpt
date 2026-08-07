import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

test('terminal task polling refreshes task, account, and fixed-group state', () => {
  const effectStart = accountsSource.indexOf("if (!taskId || !registerModalOpen) {")
  const effectEnd = accountsSource.indexOf('\n  useEffect(() => {', effectStart + 1)
  assert.ok(effectStart >= 0 && effectEnd > effectStart)
  const pollingEffect = accountsSource.slice(effectStart, effectEnd)

  assert.match(pollingEffect, /if \(isActiveTaskStatus\([\s\S]*?\}\s*else\s*\{[\s\S]*?void refetchActiveTasks\(\)[\s\S]*?void refetchAccounts\(\)[\s\S]*?void loadFilterPresets\(true\)/)
  assert.match(pollingEffect, /\[taskId, registerModalOpen, pageVisible, loadFilterPresets, refetchActiveTasks, refetchAccounts\]/)
})
