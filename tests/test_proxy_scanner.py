import unittest
from unittest import mock

from core.db import ProxyModel
from services import proxy_scanner


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class ProxyScannerTests(unittest.TestCase):
    def test_mask_proxy_url_redacts_credentials(self):
        masked = proxy_scanner.mask_proxy_url("http://user:pass@127.0.0.1:8080")
        self.assertEqual(masked, "http://***:***@127.0.0.1:8080")

    def test_probe_basic_extracts_exit_ip(self):
        with mock.patch(
            "services.proxy_scanner.requests.get",
            return_value=_Response(payload={"ip": "203.0.113.10"}, text='{"ip":"203.0.113.10"}'),
        ) as get_mock:
            result = proxy_scanner.probe_basic("http://127.0.0.1:8080", timeout_seconds=3)

        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_ip"], "203.0.113.10")
        self.assertEqual(get_mock.call_count, 1)
        self.assertIn("proxies", get_mock.call_args.kwargs)

    def test_lookup_geo_parses_ipapi_payload(self):
        with mock.patch(
            "services.proxy_scanner.requests.get",
            return_value=_Response(
                payload={
                    "country_code": "JP",
                    "country_name": "Japan",
                    "region": "Tokyo",
                    "city": "Tokyo",
                    "asn": "AS64500",
                    "org": "Example ISP",
                }
            ),
        ):
            result = proxy_scanner.lookup_geo("203.0.113.10")

        self.assertTrue(result["ok"])
        self.assertEqual(result["country_code"], "JP")
        self.assertEqual(result["asn"], "AS64500")

    def test_calculate_health_score_penalizes_blocked_chatgpt(self):
        proxy = ProxyModel(url="http://127.0.0.1:8080", is_active=True)
        proxy.scan_status = "ok"
        proxy.last_latency_ms = 900
        proxy.chatgpt_status = "blocked_403"

        score = proxy_scanner.calculate_health_score(proxy)

        self.assertLess(score, 70)
        self.assertGreater(score, 0)

    def test_probe_chatgpt_prefers_cffi_success_over_legacy_403(self):
        cffi_result = {
            "ok": True,
            "status": "ok",
            "target": "chatgpt_cffi",
            "status_code": 200,
            "latency_ms": 42,
            "error_code": "",
            "error": "",
            "targets": {
                "chatgpt": {"ok": True, "status_code": 200},
                "auth": {"ok": True, "status_code": 200},
            },
        }
        with mock.patch("services.proxy_scanner.probe_chatgpt_cffi", return_value=cffi_result), mock.patch(
            "services.proxy_scanner._request_via_proxy",
            return_value={"ok": False, "status_code": 403, "latency_ms": 12, "error_code": "http_403", "error": "HTTP 403"},
        ):
            result = proxy_scanner.probe_chatgpt("http://127.0.0.1:8080", timeout_seconds=3)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["legacy_requests"]["status"], "blocked_403")

    def test_calculate_health_score_allows_cffi_ok_chatgpt(self):
        proxy = ProxyModel(url="http://127.0.0.1:8080", is_active=True)
        proxy.scan_status = "ok"
        proxy.last_latency_ms = 900
        proxy.chatgpt_status = "ok"

        score = proxy_scanner.calculate_health_score(proxy)

        self.assertGreaterEqual(score, 90)


if __name__ == "__main__":
    unittest.main()
