import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'

const actionSurface = fs.readFileSync(
  new URL('../src/features/accounts/components/AccountActionSurface.tsx', import.meta.url),
  'utf8',
)
const appShell = fs.readFileSync(new URL('../src/app/AppShell.tsx', import.meta.url), 'utf8')

test('web-only logout and destructive token revocation remain separate account actions', () => {
  assert.match(actionSurface, /activeAction\.id === 'logout_web_session'/)
  assert.match(actionSurface, /AccessToken 和 RefreshToken 不会撤销，也不会被删除/)
  assert.match(actionSurface, /activeAction\.id === 'logout_and_revoke_tokens'/)
  assert.match(actionSurface, /confirm_revoke_all/)
  assert.match(actionSurface, /永久撤销该账号当前保存的 AT\/RT/)
})

test('destructive logout has an explicit error alert and danger confirmation button', () => {
  assert.match(actionSurface, /message="彻底退出并撤销当前账号认证材料"/)
  assert.match(actionSurface, /网络异常或无法确认失效的材料会保留以便重试/)
  assert.match(actionSurface, /okButtonProps=\{\{ danger: activeAction\?\.id === 'logout_and_revoke_tokens' \}\}/)
})

test('sidebar exposes the current release version', () => {
  assert.match(appShell, /v2\.38\.9/)
})
