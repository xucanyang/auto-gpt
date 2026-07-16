from api import accounts
from core.db import AccountModel
from services.account_filters import account_submission_info


def _account(extra: dict) -> AccountModel:
    account = AccountModel(
        id=901,
        platform="chatgpt",
        email="submission@example.com",
        password="password",
        status="pending_payment",
    )
    account.set_extra(extra)
    return account


def test_legacy_idea_summary_preserves_unavailable_and_supports_timeout():
    unavailable = accounts._build_idea_submit_summary(
        {"idea_submit": {"unavailable": True, "reason": "legacy eligibility"}},
        {"status": "failed"},
    )
    assert unavailable["status"] == "unavailable"
    assert unavailable["unavailable"] is True

    timeout = accounts._build_idea_submit_summary({}, {"status": "timeout"})
    assert timeout["status"] == "timeout"
    assert timeout["unavailable"] is False


def test_compact_account_payload_exposes_channel_neutral_submission_evidence():
    extra = {
        "idea_submit": {
            "status": "failed",
            "unavailable": True,
            "reason": "Idea route ineligible",
            "source": "baxigpt_cdk_submit",
            "payment_channel": "pix",
            "pix_submit_mode": "user_link",
            "task_id": "task-pix",
            "order_id": "order-pix",
            "display_id": "display-pix",
        },
        "idea_submit_unavailable": True,
        "baxigpt_cdk": {
            "status": "failed",
            "payment_channel": "pix",
            "pix_submit_mode": "user_link",
            "task_id": "task-pix",
            "order_id": "order-pix",
            "display_id": "display-pix",
            "submitted_at": "2026-07-16T01:00:00Z",
            "last_checked_at": "2026-07-16T01:01:00Z",
            "last_error_message": "lower-priority CDK error",
        },
        "chatgpt_last_payment_link": {
            "url": "https://payments.stripe.com/qr/instructions/secret",
            "link_type": "pix",
            "link_status": "pix_submitted",
            "pix_submitted_at": "2026-07-16T01:02:00Z",
            "link_status_updated_at": "2026-07-16T01:02:00Z",
        },
    }
    account = _account(extra)
    compact = accounts._serialize_account_compact_item(account, extra=extra)

    assert compact["idea_submit"]["status"] == "unavailable"
    assert compact["submission"] == {
        "status": "failed",
        "state": "failed",
        "has_submitted": True,
        "link_submitted": True,
        "link_status": "pix_submitted",
        "unavailable": True,
        "eligibility_state": "unavailable",
        "reason": "Idea route ineligible",
        "source": "baxigpt_cdk_submit",
        "payment_channel": "pix",
        "pix_submit_mode": "user_link",
        "cdk_id": 0,
        "code_masked": "",
        "task_id": "task-pix",
        "order_id": "order-pix",
        "display_id": "display-pix",
        "submitted_at": "2026-07-16T01:00:00Z",
        "paid_at": "",
        "last_checked_at": "2026-07-16T01:01:00Z",
        "pix_submitted_at": "2026-07-16T01:02:00Z",
        "link_status_updated_at": "2026-07-16T01:02:00Z",
    }
    assert compact["submit_state"] == "failed"
    assert compact["has_submitted"] is True
    assert compact["submitState"] == "failed"
    assert compact["hasSubmitted"] is True
    assert "url" not in compact["submission"]
    assert account_submission_info(account, extra)["state"] == compact["submit_state"]


def test_generic_submission_uses_cdk_error_and_pix_link_when_legacy_marker_is_absent():
    extra = {
        "baxigpt_cdk": {
            "status": "failed",
            "task_id": "task-user-link",
            "order_id": "order-user-link",
            "last_error_message": "管理端已存在该 PIX 支付链接",
        },
        "chatgpt_last_payment_link": {
            "link_status": "pix_submitted",
            "pix_submitted_at": "2026-07-16T02:00:00Z",
        },
    }
    account = _account(extra)
    compact = accounts._serialize_account_compact_item(account, extra=extra)

    # The legacy summary keeps its old unavailable-only reason contract.
    assert compact["idea_submit"]["status"] == "failed"
    assert compact["idea_submit"]["reason"] == ""
    assert compact["submission"]["state"] == "failed"
    assert compact["submission"]["has_submitted"] is True
    assert compact["submission"]["link_submitted"] is True
    assert compact["submission"]["reason"] == "管理端已存在该 PIX 支付链接"
    assert compact["submission"]["source"] == "baxigpt_cdk_submit"
    assert compact["submission"]["payment_channel"] == "pix"


def test_generic_submission_infers_pix_channel_from_link_type_without_submit_marker():
    extra = {
        "baxigpt_cdk": {"status": "failed"},
        "chatgpt_last_payment_link": {"link_type": "pix"},
    }
    compact = accounts._serialize_account_compact_item(_account(extra), extra=extra)

    assert compact["submission"]["link_submitted"] is False
    assert compact["submission"]["payment_channel"] == "pix"
