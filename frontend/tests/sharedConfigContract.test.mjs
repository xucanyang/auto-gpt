import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const settingsSource = await readFile(new URL('../src/pages/Settings.tsx', import.meta.url), 'utf8')

test('shared config actions use the App modal context and expose failures', () => {
  assert.match(settingsSource, /const \{ message: appMessage, modal: appModal \} = App\.useApp\(\)/)

  const shareActions = settingsSource.match(/const toggleShareMode[\s\S]+?\n  useEffect\(\(\) => \{/u)?.[0] || ''
  assert.match(shareActions, /appModal\.confirm\(/)
  assert.match(shareActions, /appModal\.info\(/)
  assert.doesNotMatch(shareActions, /(?<![A-Za-z])Modal\.(confirm|info)\(/)
  assert.match(shareActions, /关闭共享配置失败/)
  assert.match(shareActions, /enable_shared:\s*true/)
  assert.match(shareActions, /页面存在未保存修改/)
  assert.match(settingsSource, /发布本地并启用共享/)
})
