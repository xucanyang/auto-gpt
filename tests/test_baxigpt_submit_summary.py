import unittest

from api.tasks import (
    _build_idea_submit_runtime_summary,
    _idea_submit_log_item_text,
    _is_idea_account_unavailable_reason,
)


class BaxiGptSubmitSummaryTests(unittest.TestCase):
    def test_groups_success_failed_timeout_and_unsubmitted_accounts(self):
        summary = _build_idea_submit_runtime_summary(
            pairs=[
                {"account_id": 1, "email": "ok@example.com", "cdk_id": 11, "code_masked": "CDK***1111"},
                {"account_id": 2, "email": "bad@example.com", "cdk_id": 12, "code_masked": "CDK***2222"},
                {"account_id": 3, "email": "never@example.com", "cdk_id": 13, "code_masked": "CDK***3333"},
                {"account_id": 4, "email": "slow@example.com", "cdk_id": 14, "code_masked": "CDK***4444"},
            ],
            missing_ids=[99],
            skipped_accounts=[{"account_id": 5, "email": "skip@example.com", "reason": "账号缺少 Access Token"}],
            runtime_results=[
                {"account_id": 1, "email": "ok@example.com", "status": "paid", "order_id": "cdk::task-ok", "display_id": "task-ok", "code_masked": "CDK***1111"},
                {"account_id": 2, "email": "bad@example.com", "status": "failed", "reason": "No trial eligibility", "idea_marked_unavailable": True},
                {"account_id": 3, "email": "never@example.com", "status": "unsubmitted", "reason": "卡密失败次数过多"},
                {"account_id": 4, "email": "slow@example.com", "status": "timeout", "reason": "轮询处理超时"},
            ],
            submitted_count=3,
            errors=["bad@example.com: No trial eligibility"],
        )

        self.assertEqual(summary["total_accounts"], 6)
        self.assertEqual(summary["submitted"], 3)
        self.assertEqual(summary["paid"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["timeout"], 1)
        self.assertEqual(summary["unsubmitted"], 3)
        self.assertEqual(summary["marked_unavailable"], 1)
        self.assertEqual(summary["success_accounts"][0]["email"], "ok@example.com")
        self.assertEqual(summary["success_accounts"][0]["display_id"], "task-ok")
        self.assertNotIn("code_masked", summary["success_accounts"][0])
        self.assertNotIn("order_id", summary["success_accounts"][0])
        self.assertEqual(summary["failed_accounts"][0]["reason"], "No trial eligibility")
        self.assertEqual(summary["unsubmitted_accounts"][0]["email"], "never@example.com")
        self.assertEqual(summary["unsubmitted_accounts"][1]["email"], "skip@example.com")
        self.assertEqual(summary["unsubmitted_accounts"][2]["account_id"], 99)

    def test_final_log_item_does_not_render_card_key_or_order_prefix(self):
        text = _idea_submit_log_item_text(
            {
                "email": "user@example.com",
                "code_masked": "CDK***1111",
                "order_id": "CDK-SECRET-1111::task-abc",
                "reason": "No trial eligibility",
            }
        )
        self.assertEqual(text, "user@example.com | task=task-abc | No trial eligibility")
        self.assertNotIn("CDK", text)

    def test_account_unavailable_reason_classifier_ignores_transient_network(self):
        self.assertTrue(_is_idea_account_unavailable_reason("400: 结账创建失败 - 当前账号没有资格，请换号重试。"))
        self.assertTrue(_is_idea_account_unavailable_reason("No trial eligibility"))
        self.assertFalse(_is_idea_account_unavailable_reason("502: 节点网络异常 (TLS 连接错误) - 底层网络波动，请稍后再试。"))


if __name__ == "__main__":
    unittest.main()
