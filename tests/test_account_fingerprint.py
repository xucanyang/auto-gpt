from services.chatgpt_core.account_fingerprint import (
    fingerprint_signature,
    inject_account_browser_fingerprint,
    merge_preserving_account_browser_fingerprint,
    persist_account_browser_fingerprint,
    resolve_account_browser_fingerprint,
)


def _fingerprint(device_id="dev-1", chrome="136.0.7103.92"):
    return {
        "device_id": device_id,
        "accept_language": "en-US,en;q=0.9",
        "impersonate": "chrome136",
        "chrome_major": 136,
        "chrome_full_version": chrome,
        "user_agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome} Safari/537.36",
        "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        "platform_version": "15.0.0",
        "viewport_width": 1440,
        "viewport_height": 900,
    }


def test_persist_account_browser_fingerprint_promotes_registration_context():
    fp = _fingerprint()
    extra = persist_account_browser_fingerprint(
        {"chatgpt_registration_context": {"browser_fingerprint": fp}},
        source="registration",
    )

    assert extra["chatgpt_browser_fingerprint"] == fp
    assert extra["chatgpt_browser_fingerprint_signature"] == fingerprint_signature(fp)
    assert extra["chatgpt_browser_fingerprint_source"] == "registration"
    assert extra["chatgpt_browser_fingerprint_isolated"] is True
    assert resolve_account_browser_fingerprint(extra) == fp


def test_merge_preserving_account_browser_fingerprint_keeps_existing_identity():
    existing_fp = _fingerprint(device_id="existing")
    incoming_fp = _fingerprint(device_id="incoming", chrome="137.0.7151.68")
    existing = persist_account_browser_fingerprint({}, existing_fp, source="registration")
    incoming = persist_account_browser_fingerprint({}, incoming_fp, source="registration")

    merged = merge_preserving_account_browser_fingerprint(incoming, existing, source="save_account")

    assert merged["chatgpt_browser_fingerprint"]["device_id"] == "existing"
    assert merged["chatgpt_browser_fingerprint_signature"] == fingerprint_signature(existing_fp)


def test_merge_preserving_account_browser_fingerprint_backfills_when_incoming_lacks_fingerprint():
    existing_fp = _fingerprint(device_id="existing")
    existing = persist_account_browser_fingerprint({}, existing_fp, source="registration")
    incoming = {"access_token": "at-new"}

    merged = merge_preserving_account_browser_fingerprint(incoming, existing, source="save_account")

    assert merged["access_token"] == "at-new"
    assert merged["chatgpt_browser_fingerprint"]["device_id"] == "existing"
    assert merged["chatgpt_browser_fingerprint_signature"] == fingerprint_signature(existing_fp)


def test_inject_account_browser_fingerprint_uses_legacy_registration_context():
    fp = _fingerprint(device_id="legacy")
    config = {"default_executor": "protocol"}
    injected = inject_account_browser_fingerprint(
        config,
        {"chatgpt_registration_context": {"browser_fingerprint": fp}},
    )

    assert injected["default_executor"] == "protocol"
    assert injected["chatgpt_browser_fingerprint"]["device_id"] == "legacy"
    assert injected["chatgpt_browser_fingerprint_signature"] == fingerprint_signature(fp)
