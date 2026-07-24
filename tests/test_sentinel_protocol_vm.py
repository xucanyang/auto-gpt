"""协议 Sentinel：PoW + turnstile VM（对齐 any-auto-register）。"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from services.chatgpt_core import sentinel_token as st


class SentinelProtocolVmTests(unittest.TestCase):
    def test_build_sentinel_token_solves_turnstile_when_dx_present(self):
        class FakeResp:
            status_code = 200

            def json(self):
                return {
                    "token": "c-challenge-token",
                    "proofofwork": {"required": False},
                    "turnstile": {"dx": "ZHgtZGVtby1iNjQ="},
                }

        session = mock.Mock()
        session.post.return_value = FakeResp()

        with mock.patch(
            "services.chatgpt_core.sentinel_token.solve_turnstile_dx",
            create=True,
        ), mock.patch(
            "services.chatgpt_core.sentinel_vm.solve_turnstile_dx",
            return_value="turnstile-t-value",
        ) as solve_dx:
            token = st.build_sentinel_token(
                session,
                "device-xyz",
                flow="oauth_create_account",
                user_agent="Mozilla/5.0 Test",
            )

        self.assertIsNotNone(token)
        payload = json.loads(token)
        self.assertEqual(payload["c"], "c-challenge-token")
        self.assertEqual(payload["flow"], "oauth_create_account")
        self.assertEqual(payload["id"], "device-xyz")
        self.assertEqual(payload["t"], "turnstile-t-value")
        self.assertTrue(str(payload.get("p") or "").startswith("gAAAAA"))
        solve_dx.assert_called_once()
        # any-auto: turnstile 使用请求时的 requirements p 作为 xor key
        call_args = solve_dx.call_args
        request_p = call_args.args[1] if call_args.args else call_args.kwargs.get("p_token")
        self.assertTrue(str(request_p or "").startswith("gAAAAAC"))

    def test_build_sentinel_token_keeps_empty_t_when_no_dx(self):
        class FakeResp:
            status_code = 200

            def json(self):
                return {
                    "token": "c-only",
                    "proofofwork": {"required": False},
                    "turnstile": {},
                }

        session = mock.Mock()
        session.post.return_value = FakeResp()
        token = st.build_sentinel_token(session, "did", flow="authorize_continue")
        payload = json.loads(token)
        self.assertEqual(payload["t"], "")
        self.assertEqual(payload["c"], "c-only")


if __name__ == "__main__":
    unittest.main()
