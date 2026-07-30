from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_PAGE = ROOT / "frontend" / "src" / "pages" / "Accounts.tsx"
ACCOUNTS_QUERY = ROOT / "frontend" / "src" / "features" / "accounts" / "hooks" / "useAccountsQuery.ts"


def test_accounts_uses_canonical_submission_filters_and_keeps_legacy_reads_compatible():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    query = ACCOUNTS_QUERY.read_text(encoding="utf-8")

    assert "submit_state: string" in page
    assert "has_submitted: string" in page
    assert "'submit_state'," in page
    assert "'has_submitted'," in page
    assert "{ value: 'idea_submit_status', text: '提交状态'" in page
    assert "const SUBMISSION_STATE_FILTER_OPTIONS" in page
    assert "const HAS_SUBMITTED_FILTER_OPTIONS" in page
    assert 'placeholder="提交记录"' in page
    assert "{ value: 'true', text: '有提交记录' }" in page
    assert "{ value: 'false', text: '无提交记录' }" in page
    assert "submit_state: columnFilters.submitState.join(',')" in page
    assert "has_submitted: columnFilters.hasSubmitted.join(',')" in page

    # Old presets/callers may still be read, but the canonical query is sent
    # whenever the new filter is present.
    assert "source as Record<string, unknown>).ideaSubmitState" in page
    assert "source as Record<string, unknown>).idea_submit_state" in page
    assert "if (!canonicalSubmitState && ideaSubmitState) params.set('idea_submit_state', ideaSubmitState)" in query
    assert "inferHasSubmittedFromLegacyValues" not in page
    assert "return ['submitting', 'paid'].includes(state)" in page
    assert "if (unavailable) tags.push({ color: 'error', label: '不可用' })" in page
    assert "else if (!hasSubmitted && !unavailable)" in page
    assert "tags.push({ color: 'warning', label: '提交失败' })" in page
    assert "tags.push({ color: 'warning', label: '待人工复核' })" in page
    assert "{ value: 'stopped', text: '已停止' }" in page
    assert "tags.push({ color: 'default', label: '已停止' })" in page


def test_baxi_pix_poll_interval_is_persisted_and_bounded_without_a_five_second_override():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")

    assert "const DEFAULT_BAXIGPT_STATUS_POLL_INTERVAL_SECONDS = 5" in page
    assert "const BAXIGPT_STATUS_POLL_INTERVAL_MIN_SECONDS = 1" in page
    assert "const BAXIGPT_STATUS_POLL_INTERVAL_MAX_SECONDS = 3600" in page
    assert "status_poll_interval_seconds: normalizeBaxiStatusPollInterval(raw.status_poll_interval_seconds)" in page
    assert "status_poll_interval_seconds: normalizeBaxiStatusPollInterval(values.status_poll_interval_seconds)" in page
    assert "min={BAXIGPT_STATUS_POLL_INTERVAL_MIN_SECONDS}" in page
    assert "max={BAXIGPT_STATUS_POLL_INTERVAL_MAX_SECONDS}" in page
    assert "新任务按此值统一轮询；运行中的旧任务保持创建时参数。批量提交先串行创建订单，再统一轮询；后端默认 5 秒。" in page
    assert "Number(values.status_poll_interval_seconds || 5)" not in page
