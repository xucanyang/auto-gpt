# Auto ChatGPT Performance Optimization Closeout

> Historical closeout. K12, multi-Workspace, Business and Team references below
> describe the pre-v2.0.0 system and are not current product capabilities.

## Scope

This note captures the current end state of the performance optimization work
that was executed against the `auto-chatgpt` codebase, with emphasis on the
Accounts page and the request model around it.

It is not a fresh plan. It is a closeout-oriented status summary:

- what is already completed
- what concrete architecture changed
- what measurable outputs look like now
- what remains as explicit follow-up work

## Completed Batches

### Batch 1 - App Shell And Initial Bundle Split

Completed.

Key outcomes:

- app shell and routing were split out of the old monolithic app entry
- route-level lazy loading is in place
- Vite manual chunking is configured

Relevant areas:

- `frontend/src/app/`
- `frontend/src/main.tsx`
- `frontend/vite.config.ts`

### Batch 2 - Unified Frontend Data Layer

Completed first-stage migration and then extended further during later batches.

Key outcomes:

- React Query is installed and wired
- shared `QueryClient` is used
- Accounts page now uses feature hooks instead of relying entirely on page-local
  request orchestration

Relevant areas:

- `frontend/src/shared/api/queryClient.ts`
- `frontend/src/features/accounts/hooks/`

### Batch 3 - Accounts Page Feature Decomposition

Completed.

Key outcomes:

- Accounts page no longer keeps major modal and panel UI blocks inline
- feature components now own toolbar/table/modal/panel rendering
- row action logic was extracted before Batch 4 replaced the row-local model

Representative extracted components:

- `frontend/src/features/accounts/components/AccountsToolbar.tsx`
- `frontend/src/features/accounts/components/AccountsTable.tsx`
- `frontend/src/features/accounts/components/AccountDetailModal.tsx`
- `frontend/src/features/accounts/components/BatchGopayWorkbench.tsx`
- `frontend/src/features/accounts/components/PendingInvitesModal.tsx`
- `frontend/src/features/accounts/components/AddAccountModal.tsx`
- `frontend/src/features/accounts/components/ImportAccountsModal.tsx`
- `frontend/src/features/auth/components/RegisterTaskModal.tsx`

### Batch 4 - Shared Action Surface Instead Of Per-Row Heavy Menus

Completed in the main architectural sense.

Key outcomes:

- per-row heavy action state machine was replaced by a single shared page-level
  action surface
- the action surface is also lazy-loaded
- obsolete row-local action component path was removed

Relevant areas:

- `frontend/src/features/accounts/components/AccountActionSurface.tsx`
- `frontend/src/pages/Accounts.tsx`

Removed obsolete runtime path:

- `frontend/src/features/accounts/components/AccountRowActions.tsx`

### Batch 5 - Accounts Page Request Model Rewrite

Substantially completed.

Key outcomes:

- Accounts list default page size reduced from 100 to 50
- search now uses debounce
- account actions metadata is loaded on demand
- active tasks are no longer fetched by default on page entry
- pending invites are no longer fetched by default on page entry
- register modal config uses page-level cache reuse
- task polling is limited to the task modal lifecycle
- automatic Sub2API page-load sync was removed

Relevant areas:

- `frontend/src/pages/Accounts.tsx`
- `frontend/src/features/accounts/hooks/useAccountsQuery.ts`
- `frontend/src/features/accounts/hooks/useActiveTasksQuery.ts`
- `frontend/src/features/accounts/hooks/usePendingInvitesQuery.ts`

### Batch 6 - Lightweight And Detailed API Separation

Started and partially completed.

Key outcomes:

- `GET /api/accounts` now supports a lightweight list path
- frontend accounts list explicitly requests lightweight list data
- `GET /api/accounts/{id}` remains the detail path
- `GET /api/tasks/active-summary` was introduced and is used by the Accounts
  page instead of the full task snapshot list for the active-task selector

Relevant areas:

- `api/accounts.py`
- `api/tasks.py`
- `frontend/src/features/accounts/hooks/useAccountsQuery.ts`
- `frontend/src/features/accounts/hooks/useActiveTasksQuery.ts`

### Batch 7 - Team / Invite Enrichment Batch Refactor

Started.

Key outcomes:

- accounts list no longer defaults to full team brief enrichment
- list path now returns a minimal team/invite summary by default
- detail path still keeps full enrich behavior

Relevant areas:

- `api/accounts.py`
- `services/team_lite.py`

### Batch 8 - High-Frequency Account State Normalization

Started.

Key outcomes:

- lightweight list payload now directly exposes several hot-path fields instead
  of requiring the UI to depend only on nested JSON parsing
- Accounts page now prefers those normalized hot fields where available

Current hot fields exposed in lightweight list responses:

- `workspace_scope`
- `workspace_label`
- `workspace_display_name`
- `manually_used`
- `session_token`
- `auth_level`
- `subscription_plan`
- `codex_state`
- `cliproxy_remote_state`
- `sub2api_remote_state`
- `team_invite_status`

Relevant areas:

- `api/accounts.py`
- `frontend/src/pages/Accounts.tsx`

### Batch 9 - Unified Polling And Refresh Policy

Started.

Key outcomes:

- Accounts page GoPay batch polling is gated by page visibility
- `TaskLogPanel` stream connection is gated by page visibility
- active task list now uses a lightweight summary endpoint instead of the full
  task snapshot list

Relevant areas:

- `frontend/src/pages/Accounts.tsx`
- `frontend/src/components/TaskLogPanel.tsx`
- `frontend/src/features/accounts/hooks/useActiveTasksQuery.ts`
- `api/tasks.py`

### Batch 10 - Cleanup, Validation, And Performance Acceptance

Partially completed.

Key outcomes:

- obsolete row-local action runtime path removed
- repeated frontend builds were executed after each structural step
- current performance artifacts are recorded below

## Current Build Snapshot

Latest observed build output:

- `page-accounts`: about `1,045,120` bytes uncompressed
- `AccountActionSurface`: about `39,673` bytes uncompressed
- `vendor-react`: about `213,626` bytes uncompressed
- `vendor-antd`: about `25,459` bytes uncompressed

These values come from the latest generated files under `static/assets/`.

## Current Request Model

For the Accounts page, the current request model is approximately:

### Page entry

Expected first-wave request set:

- accounts list

No longer default-on at page entry:

- account actions metadata
- active tasks list
- pending invite list
- automatic Sub2API sync

### On-demand views / panels

- account detail: `GET /api/accounts/{id}`
- actions metadata: loaded when shared account action surface opens
- active tasks: loaded when the active-tasks selector is opened or task state
  restoration requires it
- pending invites: loaded when the pending invite modal is opened

### Background behavior

- task modal polling runs only while the task modal is active
- GoPay batch state polling runs only while the workbench is open and the page
  is visible
- task log streaming runs only while the panel is mounted and the page is visible

## Current API Split

The current practical API layering looks like this:

- `GET /api/accounts`
  - lightweight list-oriented response
- `GET /api/accounts/{id}`
  - full detail response
- `GET /api/tasks/active-summary`
  - lightweight active task list for selector-style UI
- `GET /api/tasks/{id}`
  - full task snapshot
- `GET /api/tasks/{id}/logs/stream`
  - detailed log stream

## Remaining Explicit Follow-Ups

The work is in a materially improved state, but the following items still
remain if the goal is a stricter interpretation of “fully finished” Batches
6-10:

1. Further reduce frontend dependence on `extra_json` fallback parsing in hot
   list rendering paths.
2. Consider adding `GET /api/accounts/overview` if a dedicated summary endpoint
   is still desired instead of deriving overview state from the current list.
3. Consider adding `GET /api/accounts/{id}/team-source` if team source detail
   should become independently requestable from the main detail endpoint.
4. Review whether `GET /api/tasks` should remain exposed as a full snapshot list
   for legacy/debug use only, while active UIs standardize on
   `GET /api/tasks/active-summary`.
5. Audit other pages with raw polling loops:
   - `frontend/src/pages/Pipeline.tsx`
   - `frontend/src/pages/RegisterTaskPage.tsx`
   - parts of `frontend/src/pages/Settings.tsx`
6. Decide whether `TaskVerificationPanel` countdown polling should also adopt a
   shared visibility-aware clock helper.
7. Produce a stricter before/after metrics document if formal performance
   acceptance is required.

## Bottom Line

The optimization work has moved the codebase from a large monolithic Accounts
page with eager side effects toward:

- a decomposed feature layout
- shared page-level heavy action handling
- a much more conditional request model
- the beginnings of real light-list / heavy-detail API layering
- visibility-aware background activity

The largest remaining gap is not architecture confusion anymore; it is
finishing the last mile of normalization and formal acceptance.
