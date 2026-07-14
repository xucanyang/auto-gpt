# Auto ChatGPT Performance Optimization Taskbook

> Historical implementation plan. K12, multi-Workspace, Business and Team product
> capabilities referenced below were retired in v2.0.0 and are no longer part of
> the supported product surface.

## Scope

This taskbook defines the full optimization plan for the current `auto-chatgpt`
codebase. The target is not incremental patching, but a full structural
optimization of:

- first-page bundle size
- Accounts page initialization cost
- Accounts page render complexity
- account/task API response weight
- background polling and automatic sync behavior

This taskbook is implementation-oriented. Each batch includes:

- goal
- files/modules involved
- concrete change items
- risks
- acceptance criteria

The plan assumes we keep the current functional scope:

- ChatGPT registration
- GoPay single/batch payment
- Pipeline workflows
- Sub2API / CLIProxyAPI / CPA / contribution sync
- mailbox integrations
- local solver

## Success Criteria

After all batches are completed, the system should meet these goals:

1. The app no longer ships a single oversized initial JS bundle.
2. Entering the Accounts page triggers only a small first-wave request set.
3. The Accounts page is split into feature modules, no longer a 5000+ line page.
4. Row-level action handling is shared, not instantiated once per row.
5. `/api/accounts` becomes a lightweight list endpoint by default.
6. `/api/tasks` is split into lightweight summary endpoints and detailed endpoints.
7. Team/source/status enrichment becomes batched and on-demand.
8. Polling is conditional, centralized, and easy to reason about.

## Batch 1 - App Shell And Initial Bundle Split

### Goal

Reduce the cost of initial page load by splitting route bundles and isolating
the application shell from heavy feature pages.

### Files / Modules

- `frontend/src/App.tsx`
- `frontend/src/main.tsx`
- `frontend/vite.config.ts`
- new: `frontend/src/app/`
- new: `frontend/src/shared/`

### Concrete Changes

1. Split the frontend app shell into:
   - `frontend/src/app/AppShell.tsx`
   - `frontend/src/app/router.tsx`
   - `frontend/src/app/providers.tsx`
2. Convert all major pages to `React.lazy(...)` + `Suspense`.
3. Add route-level loading fallbacks.
4. Configure Vite `manualChunks` for at least:
   - `vendor-react`
   - `vendor-antd`
   - `page-accounts`
   - `page-register`
   - `page-teams`
   - `page-pipeline`
   - `page-settings`
5. Move shared helpers/types into `frontend/src/shared/` so page chunks are
   less entangled.

### Risks

- Low risk to business logic.
- Moderate risk to route imports and fallback behavior.

### Acceptance Criteria

1. `static/assets/` contains multiple meaningful JS chunks instead of a single
   large entry bundle.
2. First navigation to `/` does not eagerly load all feature-page logic.
3. Accounts page code is not part of the smallest initial shell chunk.

## Batch 2 - Unified Frontend Data Layer

### Goal

Replace page-local `useEffect + apiFetch` orchestration with a unified query
layer that supports caching, invalidation, conditional loading, and controlled
polling.

### Files / Modules

- `frontend/package.json`
- new: `frontend/src/shared/api/queryClient.ts`
- new: `frontend/src/app/providers.tsx`
- new feature hooks under `frontend/src/features/**/hooks/`

### Concrete Changes

1. Add `@tanstack/react-query`.
2. Create one `QueryClient`.
3. Wrap the app with `QueryClientProvider`.
4. Introduce feature hooks, including:
   - `useAccountsQuery`
   - `useAccountsOverviewQuery`
   - `useAccountDetailQuery`
   - `useAccountActionsQuery`
   - `useActiveTasksQuery`
   - `usePendingInvitesQuery`
   - `useSub2ApiOverviewQuery`
   - `useGopaySessionQuery`
5. Standardize invalidation behavior after mutations.
6. Standardize conditional polling and visibility-aware refetch behavior.

### Risks

- Moderate risk during migration from page-local state to query hooks.
- Requires careful query-key design.

### Acceptance Criteria

1. The main feature pages no longer directly orchestrate most request lifecycle
   with ad hoc `useEffect`.
2. Repeated navigation does not refetch heavy resources unnecessarily.
3. Polling can be turned on/off via query configuration instead of scattered
   timers.

## Batch 3 - Accounts Page Feature Decomposition

### Goal

Break the current Accounts super-page into manageable feature modules so that
state, rendering, and request responsibility are isolated.

### Files / Modules

- `frontend/src/pages/Accounts.tsx`
- new: `frontend/src/features/accounts/**`
- new: `frontend/src/features/gopay/**`
- new: `frontend/src/features/auth/**`

### Concrete Changes

Split the page into at least:

- `features/accounts/pages/AccountsPage.tsx`
- `features/accounts/components/AccountsToolbar.tsx`
- `features/accounts/components/AccountsTable.tsx`
- `features/accounts/components/AccountRowActions.tsx`
- `features/accounts/components/AccountDetailDrawer.tsx`
- `features/accounts/components/ActiveTasksPanel.tsx`
- `features/accounts/components/Sub2ApiOverviewPanel.tsx`
- `features/gopay/components/GopayDialog.tsx`
- `features/gopay/components/BatchGopayWorkbench.tsx`
- `features/auth/components/ResumeAuthDialog.tsx`

### Risks

- Moderate refactor surface.
- Logic can drift if event boundaries are not clearly defined.

### Acceptance Criteria

1. The old page file becomes a small composition layer or is removed.
2. Table, detail, GoPay, task, and overview logic live in separate modules.
3. Accounts feature state becomes traceable by responsibility.

## Batch 4 - Shared Action Surface Instead Of Per-Row Heavy Menus

### Goal

Eliminate per-row instantiation of the heavy action state machine by replacing
row-local action menus with a shared action drawer/modal.

### Files / Modules

- current row action rendering in `frontend/src/pages/Accounts.tsx`
- new shared action surface component under `features/accounts/components/`
- GoPay/browser-auth/payment-link related feature components

### Concrete Changes

1. Remove row-local heavy `ActionMenu` instantiation.
2. Keep row rendering lightweight:
   - detail
   - open actions
   - delete
   - resume auth
3. Move action handling into one shared page-level component:
   - `AccountActionDrawer`
   - or `AccountActionModal`
4. Route all heavy flows through the shared surface:
   - payment link
   - GoPay
   - browser auth
   - status sync
   - upload/backfill actions

### Risks

- Moderate UI integration risk.
- Needs strong state ownership for the selected account/action.

### Acceptance Criteria

1. Table rows do not initialize a full heavy action state machine per row.
2. GoPay/browser-auth/payment-link state exists only once at the page level.
3. Large-row-count rendering becomes substantially lighter.

## Batch 5 - Accounts Page Request Model Rewrite

### Goal

Reduce the number and weight of automatic requests triggered when entering the
Accounts page.

### Files / Modules

- Accounts page feature hooks/components
- task/overview/invite/Gopay-related hooks

### Concrete Changes

1. Keep only the minimum first-wave requests on page entry:
   - accounts list
   - accounts overview summary
2. Load these lazily/on demand:
   - account actions metadata
   - active tasks
   - pending invites
   - GoPay defaults/config
   - payment countries/config
   - Sub2API detail sync
3. Remove page-load automatic Sub2API sync.
4. Stop default polling of `/api/tasks` on page load.
5. Reduce accounts list `page_size` from 100 to a smaller default.
6. Add debouncing for text search and some filters.

### Risks

- Moderate UX change risk if users rely on automatic background freshness.
- Requires explicit refresh affordances in UI.

### Acceptance Criteria

1. Opening the Accounts page triggers only a small, controlled first request set.
2. Background polling is not active unless a panel/flow actually needs it.
3. Search/filter interactions do not trigger immediate request storms.

## Batch 6 - Lightweight And Detailed API Separation

### Goal

Separate lightweight list/summary endpoints from heavyweight detail endpoints,
so the frontend can choose the cheapest path for each view.

### Files / Modules

- `api/accounts.py`
- `api/tasks.py`
- `core/task_runtime.py`

### Concrete Changes

Introduce/reshape:

- `GET /api/accounts`
  - lightweight list data
- `GET /api/accounts/overview`
  - summary counters / top-level overview
- `GET /api/accounts/{id}`
  - full detail
- `GET /api/accounts/{id}/team-source`
  - team source on demand
- `GET /api/tasks/active-summary`
  - lightweight active task data
- `GET /api/tasks/{id}`
  - detailed snapshot
- `GET /api/tasks/{id}/logs/stream`
  - detailed log streaming

Also ensure list-style task endpoints do not default to returning full logs and
control structures.

### Risks

- Moderate API contract migration risk.
- Frontend and backend must be updated together.

### Acceptance Criteria

1. Accounts list endpoints return small payloads appropriate for list rendering.
2. Task summary endpoints do not ship full logs by default.
3. Detail views explicitly use detail endpoints.

## Batch 7 - Team / Invite Enrichment Batch Refactor

### Goal

Make team/source enrichment efficient by switching from per-team detail lookups
to batched queries and explicit opt-in enrichment.

### Files / Modules

- `api/accounts.py`
- `services/team_lite.py`

### Concrete Changes

1. Refactor `get_team_db_briefs(...)` to batch team queries:
   - batch query `teams`
   - batch query `team_accounts`
   - join in memory
2. Remove implicit per-item detail lookup loops for list endpoints.
3. Make team enrichment opt-in:
   - default accounts list does not eagerly include full team source detail
   - detail endpoint or `include=...` parameter enables enrichment

### Risks

- Moderate data-shape change risk.
- Team-related UI must be aligned with new loading mode.

### Acceptance Criteria

1. Accounts list cost does not scale poorly with number of linked teams.
2. Team source detail becomes explicitly requested, not always eagerly built.

## Batch 8 - High-Frequency Account State Normalization

### Goal

Reduce repeated row-level JSON parsing and stabilize high-frequency list data by
normalizing a limited set of frequently used state into explicit columns or
explicit serialized summary structures.

### Files / Modules

- `core/db.py`
- `api/accounts.py`
- `services/chatgpt_account_state.py`
- `services/chatgpt_sync.py`
- `services/sub2api_sync.py`

### Concrete Changes

Do **not** fully denormalize everything at once. Instead, pick the smallest set
of high-value list fields, such as:

- `workspace_scope`
- `workspace_label`
- `auth_level`
- `subscription_plan`
- `codex_state`
- `cliproxy_remote_state`
- `sub2api_remote_state`
- `manually_used`
- `team_invite_status`

Normalize only the fields proven to be hot in list rendering and summary views.

### Risks

- Medium-to-high migration risk if too many fields are normalized at once.
- Must coordinate write-path changes across several services.

### Acceptance Criteria

1. List rendering no longer depends on heavy per-row `extra_json` parsing for
   the most frequently displayed fields.
2. Hot-path list fields are directly available from the account list response.

## Batch 9 - Unified Polling And Refresh Policy

### Goal

Centralize and standardize all polling so the app never keeps background loops
alive unnecessarily.

### Files / Modules

- feature query hooks
- task panels
- GoPay panels
- account overview panels

### Concrete Changes

Define one policy for polling:

1. Poll only while a relevant panel is visible.
2. Poll only while a related session/task is active.
3. Pause polling when the page is hidden.
4. Prefer user-triggered refresh over automatic silent sync unless business
   value is clear.
5. Make polling intervals and enablement live in shared hook/query config.

### Risks

- Low-to-moderate UX change risk.
- Requires discipline so new polling does not reappear in ad hoc components.

### Acceptance Criteria

1. No scattered raw `setInterval(...)` loops remain in feature pages.
2. Polling is discoverable, centralized, and conditional.
3. Background network noise is substantially reduced.

## Batch 10 - Cleanup, Validation, And Performance Acceptance

### Goal

Remove obsolete pathways, validate performance gains, and ensure the new
architecture is stable.

### Files / Modules

- old feature files left behind after migration
- build config
- performance logging / instrumentation

### Concrete Changes

1. Remove obsolete old-page logic and unused helpers.
2. Validate build outputs and chunking results.
3. Capture before/after metrics:
   - first bundle size
   - first interactive time
   - Accounts page initial request count
   - `/api/accounts` response size/time
   - `/api/tasks/active-summary` response size/time
4. Perform regression review for:
   - accounts list
   - GoPay flows
   - pipeline
   - team actions
   - task log/verification flows

### Risks

- Low if done after all prior batches.

### Acceptance Criteria

1. No dead legacy implementation paths remain in critical flows.
2. The new architecture is the only architecture serving the main UI.
3. Performance goals are measured, not guessed.

## Execution Notes

1. Execute batches in order.
2. Do not start with schema-heavy normalization before frontend structure and
   API layering are stable.
3. Preserve behavior first; lighten default paths second; normalize hot-path
   state only after the request model is under control.
4. Any batch that changes interface shape must include coordinated frontend and
   backend updates in the same rollout.
