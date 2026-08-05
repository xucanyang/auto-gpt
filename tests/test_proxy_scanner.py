import unittest
from unittest import mock

from core.db import ProxyModel
from core.task_runtime import StopTaskRequested
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

    def test_lookup_geo_via_proxy_trace_parses_country(self):
        with mock.patch(
            "services.proxy_scanner.requests.get",
            return_value=_Response(text="fl=1\nip=203.0.113.10\nloc=US\ncolo=LAX\n"),
        ):
            result = proxy_scanner.lookup_geo_via_proxy_trace("socks5://user:pass@127.0.0.1:1080")

        self.assertTrue(result["ok"])
        self.assertEqual(result["country_code"], "US")
        self.assertEqual(result["exit_ip"], "203.0.113.10")
        self.assertEqual(result["source"], "cloudflare_trace")

    def test_scan_proxy_url_prefers_proxy_trace_geo_over_server_geo_lookup(self):
        with mock.patch(
            "services.proxy_scanner.probe_basic",
            return_value={"ok": True, "exit_ip": "203.0.113.10", "latency_ms": 10},
        ), mock.patch(
            "services.proxy_scanner.lookup_geo_via_proxy_trace",
            return_value={"ok": True, "country_code": "US", "source": "cloudflare_trace", "exit_ip": "203.0.113.10"},
        ), mock.patch("services.proxy_scanner.lookup_geo") as lookup_geo:
            result = proxy_scanner.scan_proxy_url("socks5://user:pass@127.0.0.1:1080", targets=["basic", "geo"])

        self.assertEqual(result["geo"]["country_code"], "US")
        self.assertEqual(result["geo"]["source"], "cloudflare_trace")
        lookup_geo.assert_not_called()

    def test_scan_proxy_url_threads_stop_checker_through_network_phases(self):
        stop_checker = mock.Mock()
        with mock.patch(
            "services.proxy_scanner.probe_basic",
            return_value={"ok": True, "exit_ip": "203.0.113.10", "latency_ms": 10},
        ) as probe_basic, mock.patch(
            "services.proxy_scanner.lookup_geo_via_proxy_trace",
            return_value={"ok": True, "country_code": "US", "source": "cloudflare_trace"},
        ) as proxy_trace:
            proxy_scanner.scan_proxy_url(
                "socks5://user:pass@127.0.0.1:1080",
                targets=["basic", "geo"],
                stop_checker=stop_checker,
            )

        self.assertIs(probe_basic.call_args.kwargs["stop_checker"], stop_checker)
        self.assertIs(proxy_trace.call_args.kwargs["stop_checker"], stop_checker)
        self.assertGreaterEqual(stop_checker.call_count, 2)

    def test_scan_proxy_url_stops_before_starting_network_probe(self):
        stop_checker = mock.Mock(side_effect=RuntimeError("stop requested"))
        with mock.patch("services.proxy_scanner.probe_basic") as probe_basic:
            with self.assertRaisesRegex(RuntimeError, "stop requested"):
                proxy_scanner.scan_proxy_url(
                    "socks5://user:pass@127.0.0.1:1080",
                    targets=["basic", "geo"],
                    stop_checker=stop_checker,
                )

        probe_basic.assert_not_called()

    def test_lookup_geo_does_not_swallow_task_interruption_after_request(self):
        stop_checker = mock.Mock(
            side_effect=[None, None, StopTaskRequested()],
        )
        with mock.patch(
            "services.proxy_scanner.requests.get",
            return_value=_Response(payload={"country_code": "JP"}),
        ):
            with self.assertRaises(StopTaskRequested):
                proxy_scanner.lookup_geo(
                    "203.0.113.10",
                    stop_checker=stop_checker,
                )

    def test_calculate_health_score_penalizes_blocked_chatgpt(self):
        proxy = ProxyModel(url="http://127.0.0.1:8080", is_active=True)
        proxy.scan_status = "ok"
        proxy.last_latency_ms = 900
        proxy.chatgpt_status = "blocked_403"

        score = proxy_scanner.calculate_health_score(proxy)

        self.assertLess(score, 70)
        self.assertGreater(score, 0)

    def test_probe_chatgpt_prefers_registration_success_over_legacy_403(self):
        registration_result = {
            "ok": True,
            "status": "ok",
            "target": "registration_homepage_csrf",
            "status_code": 200,
            "latency_ms": 24,
            "error_code": "",
            "error": "",
            "attempts": [],
        }
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
        with mock.patch("services.proxy_scanner.probe_chatgpt_registration_flow", return_value=registration_result), mock.patch(
            "services.proxy_scanner.probe_chatgpt_cffi", return_value=cffi_result
        ), mock.patch(
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
