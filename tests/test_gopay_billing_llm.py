import os
import sys
import unittest
import importlib.util
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

HAS_API_DEPS = importlib.util.find_spec("fastapi") is not None and importlib.util.find_spec("sqlmodel") is not None

if HAS_API_DEPS:
    from api import chatgpt as chatgpt_api
else:
    chatgpt_api = None


class DummyAccount:
    email = "buyer@example.com"


@unittest.skipUnless(HAS_API_DEPS, "fastapi/sqlmodel are not installed in this environment")
class GoPayBillingLlmTests(unittest.TestCase):
    def test_responses_llm_generates_address_with_required_prompt(self):
        response = mock.Mock(status_code=200, text="")
        response.json.return_value = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                '{"billing_name":"Alex Carter","country":"United States","line1":"1600 Amphitheatre Parkway",'
                                '"city":"Mountain View","state":"CA","postal_code":"94043"}'
                            ),
                        }
                    ],
                }
            ]
        }

        with mock.patch.object(
            chatgpt_api,
            "_load_gopay_billing_llm_config",
            return_value={
                "enabled": True,
                "api_base_url": "https://api.666800.xyz",
                "api_key": "sk-test",
                "model": "gpt-5.4",
                "wire_api": "responses",
                "country_strategy": "checkout_country",
                "fixed_country": "ID",
                "reasoning_effort": "xhigh",
                "timeout_seconds": 45.0,
                "prompt": chatgpt_api.DEFAULT_GOPAY_BILLING_LLM_PROMPT,
            },
        ), mock.patch("requests.post", return_value=response) as post:
            address, target_country, strategy = chatgpt_api._call_gopay_billing_address_llm(
                {"country": "ID"},
                checkout_country="US",
                generation_context="batch_index=2; account_email=buyer@example.com; phone=+86 13800138000",
            )

        self.assertEqual(
            address,
            {
                "name": "Alex Carter",
                "country": "US",
                "line1": "1600 Amphitheatre Parkway",
                "city": "Mountain View",
                "state": "CA",
                "postal_code": "94043",
            },
        )
        self.assertEqual(target_country, "US")
        self.assertEqual(strategy, "checkout_country")
        call = post.call_args
        self.assertEqual(call.args[0], "https://api.666800.xyz/v1/responses")
        body = call.kwargs["json"]
        self.assertEqual(body["model"], "gpt-5.4")
        self.assertEqual(body["instructions"], "你是 GoPay/Stripe 账单地址生成器。你只输出可解析的 JSON 对象。")
        self.assertFalse(body["store"])
        self.assertEqual(body["reasoning"], {"effort": "xhigh"})
        user_text = body["input"][1]["content"][0]["text"]
        self.assertIn("地址在谷歌地图中能找到对应的位置", user_text)
        self.assertIn("billing_name", user_text)
        self.assertIn("不要生成 billing_email", user_text)
        self.assertIn("本次请求唯一上下文", user_text)
        self.assertIn("batch_index=2", user_text)
        self.assertIn("每个账号都必须生成不同的真实姓名和不同的真实账单地址", user_text)
        self.assertIn("不要返回与当前兜底/旧地址相同或高度相似的地址", user_text)
        self.assertIn("优先在这个城市/区域附近选择真实可查地址", user_text)
        self.assertIn("billing_name 的姓名风格种子", user_text)

    def test_resolve_gopay_billing_falls_back_when_llm_is_not_configured(self):
        req = chatgpt_api.GoPayStartReq(
            phone_country_code="86",
            phone_number="13800138000",
            billing_country="US",
            billing_line1="3110 Sunset Boulevard",
            billing_city="Los Angeles",
            billing_state="CA",
            billing_postal_code="90026",
        )

        with mock.patch.object(chatgpt_api, "_call_gopay_billing_address_llm", return_value=None):
            billing, source, target_country, strategy, warning = chatgpt_api._resolve_gopay_billing(req, {}, DummyAccount())

        self.assertEqual(source, "manual")
        self.assertEqual(target_country, "")
        self.assertEqual(strategy, "")
        self.assertEqual(warning, "")
        self.assertEqual(billing["email"], "buyer@example.com")
        self.assertEqual(billing["line1"], "3110 Sunset Boulevard")

    def test_manual_generation_keeps_email_and_applies_generated_name(self):
        req = chatgpt_api.GoPayGenerateBillingReq(
            country="ID",
            billing_email="fixed@example.com",
            billing_country="US",
        )
        generated = (
            {
                "name": "Maya Wilson",
                "country": "SG",
                "line1": "1 Fullerton Road",
                "city": "Singapore",
                "state": "Singapore",
                "postal_code": "049213",
            },
            "SG",
            "fixed_country",
        )

        with mock.patch.object(chatgpt_api, "_call_gopay_billing_address_llm", return_value=generated):
            billing, target_country, strategy = chatgpt_api._resolve_gopay_billing_for_manual_generation(req, DummyAccount())

        self.assertEqual(target_country, "SG")
        self.assertEqual(strategy, "fixed_country")
        self.assertEqual(billing["name"], "Maya Wilson")
        self.assertEqual(billing["email"], "fixed@example.com")

    def test_resolve_gopay_billing_passes_batch_generation_context(self):
        req = chatgpt_api.GoPayStartReq(
            phone_country_code="86",
            phone_number="13800138000",
            billing_email="batch-account@example.com",
            billing_country="US",
            billing_generation_context="batch_index=3; account_email=batch-account@example.com; phone=+86 13800138000",
        )
        generated = (
            {
                "name": "Jordan Lee",
                "country": "US",
                "line1": "500 Terry A Francois Boulevard",
                "city": "San Francisco",
                "state": "CA",
                "postal_code": "94158",
            },
            "US",
            "billing_country",
        )

        with mock.patch.object(chatgpt_api, "_call_gopay_billing_address_llm", return_value=generated) as call:
            billing, source, target_country, strategy, warning = chatgpt_api._resolve_gopay_billing(
                req,
                {},
                DummyAccount(),
                checkout_country="US",
            )

        self.assertEqual(source, "llm")
        self.assertEqual(target_country, "US")
        self.assertEqual(strategy, "billing_country")
        self.assertEqual(warning, "")
        self.assertEqual(billing["name"], "Jordan Lee")
        self.assertEqual(billing["email"], "batch-account@example.com")
        self.assertEqual(
            call.call_args.kwargs["generation_context"],
            "batch_index=3; account_email=batch-account@example.com; phone=+86 13800138000",
        )

    def test_responses_sse_text_is_parsed(self):
        response = mock.Mock(status_code=200, text="")
        response.json.side_effect = ValueError("not json")
        response.text = "\n".join([
            'event: response.created',
            'data: {"type":"response.created","response":{"status":"in_progress"}}',
            'event: response.output_text.done',
            (
                'data: {"type":"response.output_text.done","text":'
                '"{\\"billing_name\\":\\"Avery Stone\\",\\"country\\":\\"ID\\",'
                '\\"line1\\":\\"Jl. Diponegoro No. 150\\",\\"city\\":\\"Denpasar\\",'
                '\\"state\\":\\"Bali\\",\\"postal_code\\":\\"80114\\"}"}'
            ),
            'event: response.completed',
            'data: {"type":"response.completed","response":{"status":"completed"}}',
        ])

        with mock.patch.object(
            chatgpt_api,
            "_load_gopay_billing_llm_config",
            return_value={
                "enabled": True,
                "api_base_url": "https://api.666800.xyz",
                "api_key": "sk-test",
                "model": "gpt-5.4",
                "wire_api": "responses",
                "country_strategy": "fixed_country",
                "fixed_country": "ID",
                "reasoning_effort": "xhigh",
                "timeout_seconds": 45.0,
                "prompt": chatgpt_api.DEFAULT_GOPAY_BILLING_LLM_PROMPT,
            },
        ), mock.patch("requests.post", return_value=response):
            address, target_country, strategy = chatgpt_api._call_gopay_billing_address_llm(
                {"country": "US"},
                checkout_country="US",
                generation_context="batch_index=4",
            )

        self.assertEqual(strategy, "fixed_country")
        self.assertEqual(target_country, "ID")
        self.assertEqual(address["name"], "Avery Stone")
        self.assertEqual(address["line1"], "Jl. Diponegoro No. 150")


if __name__ == "__main__":
    unittest.main()
