import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const accountsSource = await readFile(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')
const toolbarSource = await readFile(
  new URL('../src/features/accounts/components/AccountsToolbar.tsx', import.meta.url),
  'utf8',
)
const modalSource = await readFile(
  new URL('../src/features/accounts/components/BatchAccountActionModal.tsx', import.meta.url),
  'utf8',
)
const actionSurfaceSource = await readFile(
  new URL('../src/features/accounts/components/AccountActionSurface.tsx', import.meta.url),
  'utf8',
)
const registerTaskModalSource = await readFile(
  new URL('../src/features/auth/components/RegisterTaskModal.tsx', import.meta.url),
  'utf8',
)
const taskTypesSource = await readFile(new URL('../src/lib/taskTypes.ts', import.meta.url), 'utf8')

test('generic account actions share pinned and more-operation surfaces without a standalone batch entry', () => {
  assert.doesNotMatch(toolbarSource, /批量账号操作/)
  assert.doesNotMatch(toolbarSource, /batchAccountActionMenuItems|batchAccountActionMenuOpen/)
  assert.doesNotMatch(toolbarSource, /flattenBatchActionMenuForMobile/)
  assert.match(toolbarSource, /genericAccountActions\.map\(\(action\) => \[String\(action\.id \|\| ''\), action\]\)/)
  assert.match(toolbarSource, /Array\.from\(genericAccountActionById\.keys\(\)\)/)
  assert.match(toolbarSource, /genericAccountActionById\.get\(actionId\)/)
  assert.match(toolbarSource, /onGenericAccountAction\(actionId\)/)
})

test('operation visibility is metadata-driven and migrates v2 preferences to v3', () => {
  assert.match(accountsSource, /toolbar-actions\.v3/)
  assert.match(accountsSource, /toolbar-actions\.v2/)
  assert.match(accountsSource, /action\?\.batch\?\.mode === 'generic'/)
  assert.match(accountsSource, /genericAccountActions\.forEach\(\(action\) =>/)
  assert.match(accountsSource, /toolbarActionOptionMap\.set\(actionId,/)
  assert.match(accountsSource, /updatePinnedToolbarActions\(Array\.from\(toolbarActionOptionMap\.keys\(\)\)\)/)
  assert.match(accountsSource, /危险操作默认不固定，执行时仍需二次确认/)
  assert.match(accountsSource, /\^\[A-Za-z0-9_\.:-\]\+\$/)
})

test('operation visibility popup remains reachable in compact mobile viewports', () => {
  assert.match(accountsSource, /data-testid="toolbar-action-visibility-popup"/)
  assert.match(accountsSource, /maxHeight: isMobile \? 'min\(420px, calc\(50dvh - 24px\)\)' : undefined/)
  assert.match(accountsSource, /overflowY: isMobile \? 'auto' : undefined/)
  assert.match(accountsSource, /overscrollBehaviorY: isMobile \? 'contain' : undefined/)
  assert.match(accountsSource, /boxSizing: 'border-box'/)
  assert.match(accountsSource, /<Dropdown popupRender=\{\(\) => overlay\} trigger=\{\['click'\]\}>/)
})

test('single selected and filtered scopes use one background-task launcher', () => {
  const handlerStart = accountsSource.indexOf('const buildAccountActionTarget = (')
  const handlerEnd = accountsSource.indexOf('\n  const handleBackfill = async', handlerStart)
  assert.ok(handlerStart >= 0 && handlerEnd > handlerStart)
  const handlers = accountsSource.slice(handlerStart, handlerEnd)

  assert.match(handlers, /accountId > 0\s*\? 'single'/)
  assert.match(handlers, /selectedIds\.length > 0 \? 'selected' : 'filtered'/)
  assert.match(handlers, /const scopeBody: Record<string, unknown> = \{\}/)
  assert.match(handlers, /applyAccountTaskScopeToBody\(scopeBody,/)
  assert.match(handlers, /scopeBody: JSON\.parse\(JSON\.stringify\(scopeBody\)\)/)
  assert.match(handlers, /buildAccountFilterPresetSummary\(currentFilterPresetFilters\)/)
  assert.match(handlers, /\.\.\.target\.scopeBody,[\s\S]+action_id: actionId,[\s\S]+scope: target\.scope,[\s\S]+params/)
  assert.match(handlers, /body\.confirmed_total = target\.targetCount/)
  assert.match(handlers, /'\/tasks\/chatgpt\/account-actions\/batch'/)
  assert.doesNotMatch(handlers, /`\/actions\/\$\{currentPlatform\}\/\$\{encodeURIComponent\(actionId\)\}\/batch`/)

  const launcherStart = handlers.indexOf('const startAccountActionTask = async (')
  const launcher = handlers.slice(launcherStart)
  assert.doesNotMatch(launcher, /applyAccountTaskScopeToBody/)
})

test('task creation opens a provisional log snapshot and active task panel without waiting for a snapshot GET', () => {
  const handlerStart = accountsSource.indexOf('const startAccountActionTask = async (')
  const handlerEnd = accountsSource.indexOf('\n  const openAccountAction =', handlerStart)
  assert.ok(handlerStart >= 0 && handlerEnd > handlerStart)
  const handler = accountsSource.slice(handlerStart, handlerEnd)

  assert.match(handler, /task_snapshot/)
  assert.match(handler, /status: responseSnapshot\?\.status \|\| 'pending'/)
  assert.match(handler, /setTaskModalMode\(taskModalModeFromSource\(source\)\)/)
  assert.match(handler, /setTaskId\(taskIdFromResponse\)/)
  assert.match(handler, /setTaskSnapshot\(provisionalSnapshot\)/)
  assert.match(handler, /setRegisterModalOpen\(true\)/)
  assert.match(handler, /setActiveTasksPanelOpen\(true\)/)
  assert.match(handler, /activeTasksQuery\.refetch\(\)/)
  assert.doesNotMatch(handler, /await apiFetch\(`\/tasks\/\$\{taskIdFromResponse\}`\)/)
})

test('account-action task submission is synchronously guarded against duplicate requests', () => {
  assert.match(accountsSource, /const accountActionRequestInFlightRef = useRef\(false\)/)
  assert.match(accountsSource, /if \(accountActionRequestInFlightRef\.current\) return false/)
  assert.match(accountsSource, /accountActionRequestInFlightRef\.current = true/)
  assert.match(accountsSource, /finally \{[\s\S]+accountActionRequestInFlightRef\.current = false/)
  assert.match(accountsSource, /onSubmit=\{async \(params\) =>/)
  assert.match(modalSource, /const submitInFlightRef = useRef\(false\)/)
  assert.match(modalSource, /if \(loading \|\| submitInFlightRef\.current\) return/)
  assert.match(modalSource, /onOk=\{\(\) => submit\(\)\}/)
})

test('all status synchronization variants use the common task launcher', () => {
  const handlerStart = accountsSource.indexOf('const handleBatchStatusSync = async (')
  const handlerEnd = accountsSource.indexOf('\n  const getStatusSyncScope', handlerStart)
  assert.ok(handlerStart >= 0 && handlerEnd > handlerStart)
  const handler = accountsSource.slice(handlerStart, handlerEnd)

  assert.match(handler, /taskAccountActions\.find/)
  assert.match(handler, /buildAccountActionTarget/)
  assert.match(handler, /startAccountActionTask\(target, customParams \|\| \{\}\)/)
  assert.doesNotMatch(handler, /\/actions\//)
  assert.doesNotMatch(handler, /probe-local-status\/batch/)
})

test('single row task actions leave the synchronous fallback and immediately use the task launcher', () => {
  assert.match(actionSurfaceSource, /onTaskAction\?:/)
  assert.match(actionSurfaceSource, /action\?\.execution\?\.mode === 'task' && onTaskAction/)
  assert.match(actionSurfaceSource, /await onTaskAction\(acc, action, params\)/)
  assert.match(accountsSource, /onTaskAction=\{handleSingleAccountTaskAction\}/)
})

test('single-row payment links use the existing background task config instead of the synchronous action API', () => {
  assert.match(actionSurfaceSource, /onPaymentLinkTask\?:/)
  assert.match(actionSurfaceSource, /actionId === 'payment_link' && onPaymentLinkTask/)
  assert.match(actionSurfaceSource, /forceRefresh: params\.reuse_cached_link === false/)
  assert.match(accountsSource, /onPaymentLinkTask=\{handleSingleAccountPaymentLinkTask\}/)
  assert.match(accountsSource, /handleBatchPaymentLink\(\{ account: record, forceRefresh: Boolean\(options\.forceRefresh\) \}\)/)

  const submitterStart = accountsSource.indexOf('const submitBatchPaymentLinkConfig = async () => {')
  const submitterEnd = accountsSource.indexOf('\n  const openWebSessionLoginConfig', submitterStart)
  assert.ok(submitterStart >= 0 && submitterEnd > submitterStart)
  const submitter = accountsSource.slice(submitterStart, submitterEnd)
  assert.match(submitter, /const provisionalSnapshot = \{/)
  assert.match(submitter, /setTaskSnapshot\(provisionalSnapshot\)/)
  assert.doesNotMatch(submitter, /await apiFetch\(`\/tasks\/\$\{taskIdFromResponse\}`\)/)
})

test('the shared confirmation modal supports single selected and filtered targets', () => {
  assert.match(modalSource, /targetScope: 'single' \| 'selected' \| 'filtered'/)
  assert.match(modalSource, /targetScope === 'single'[\s\S]+\? '当前账号'/)
  assert.match(modalSource, /title=\{`\$\{isBatch \? '批量' : ''\}\$\{actionLabel\}`\}/)
  assert.match(modalSource, /筛选摘要：\$\{targetSummary\}/)
  assert.match(modalSource, /maxAccounts = 1000/)
  assert.match(modalSource, /confirmation_param/)
  assert.match(modalSource, /Input\.Password/)
})

test('account-action task source has modal, active-task, and history labels with single-aware titles', () => {
  assert.match(accountsSource, /normalized === 'batch_account_action'[\s\S]+return 'account_action'/)
  assert.match(registerTaskModalSource, /taskModalMode === 'account_action'/)
  assert.match(registerTaskModalSource, /accountActionScope === 'single'/)
  assert.match(registerTaskModalSource, /taskModalAccount\?\.email/)
  assert.match(taskTypesSource, /batch_account_action: '账号操作'/)
  assert.match(taskTypesSource, /taskSourceDisplayLabel/)
  assert.match(taskTypesSource, /`账号操作 · \$\{actionLabel\}`/)
  assert.match(taskTypesSource, /scope === 'single'[\s\S]+'batch_probe_local_status'/)
  assert.match(taskTypesSource, /'batch_sub2api_upload'/)
  assert.match(taskTypesSource, /'batch_oaipay_upload'/)
  assert.match(accountsSource, /setTaskModalAccount\(scope === 'single'/)
  assert.match(accountsSource, /snapshotMeta\?\.email \|\| emails\[0\]/)
})

test('active email-change tasks reopen their dedicated task surface with the exact task id', () => {
  assert.match(accountsSource, /source === 'chatgpt_email_change' && accountId > 0/)
  assert.match(accountsSource, /setActionAccount\(\{ id: accountId, email \}\)/)
  assert.match(accountsSource, /setActionSurfaceInitialActionId\('change_email'\)/)
  assert.match(accountsSource, /setActionSurfaceInitialTaskId\(id\)/)
  assert.match(accountsSource, /initialTaskId=\{actionSurfaceInitialTaskId\}/)
  assert.match(actionSurfaceSource, /initialTaskId=\{initialTaskId\}/)
})

test('existing specialized task entry points still remain available', () => {
  assert.match(accountsSource, /'\/tasks\/chatgpt\/sub2api-upload\/batch'/)
  assert.match(accountsSource, /'\/tasks\/chatgpt\/oaipay-upload\/batch'/)
  assert.match(accountsSource, /'\/tasks\/chatgpt\/payment-links\/batch'/)
  assert.match(accountsSource, /'\/tasks\/chatgpt\/resume-subscription-auth'/)
  assert.match(accountsSource, /key: 'gcash_payment_method',[\s\S]{0,180}批量检测 GCash 支付方式/)
})
