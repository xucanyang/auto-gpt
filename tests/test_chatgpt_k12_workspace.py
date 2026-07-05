import unittest

from services.chatgpt_core.k12_workspace import (
    capture_k12_and_all_spaces,
    normalize_account_spaces,
    parse_k12_workspace_ids,
    request_k12_workspace_join,
)


class DummyResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class DummySession:
    def __init__(self):
        self.posts = []
        self.gets = []
        self.post_responses = []
        self.get_responses = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.post_responses.pop(0) if self.post_responses else DummyResponse(200, text='{"ok":true}')

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return self.get_responses.pop(0) if self.get_responses else DummyResponse(200, payload={})


class FailingPostSession(DummySession):
    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        raise RuntimeError(
            "Authorization: Bearer secret-access-token cookie_header='__Secure-next-auth.session-token=secret-cookie'"
        )


class DummyClient:
    def __init__(self):
        self.session = DummySession()
        self.exchanged = []

    def fetch_chatgpt_session(self, workspace_id="", workspace_reason="setCurrentAccount"):
        self.exchanged.append((workspace_id, workspace_reason))
        return True, {
            "accessToken": f"at-{workspace_id}",
            "sessionToken": "session-cookie",
            "account": {"id": workspace_id},
            "user": {"id": "user-1"},
            "expires": "2026-12-31T00:00:00Z",
        }

    def get_chatgpt_cookie_header(self):
        return "oai-did=device; __Secure-next-auth.session-token=session-cookie"

    def get_next_auth_session_token(self):
        return "session-cookie"


class ExchangeFailClient(DummyClient):
    def fetch_chatgpt_session(self, workspace_id="", workspace_reason="setCurrentAccount"):
        self.exchanged.append((workspace_id, workspace_reason))
        if workspace_id == "ws-k12":
            return False, "workspace session failed"
        return super().fetch_chatgpt_session(workspace_id=workspace_id, workspace_reason=workspace_reason)


class K12WorkspaceTests(unittest.TestCase):
    def test_parse_k12_workspace_ids_accepts_mixed_separators_and_urls(self):
        self.assertEqual(
            parse_k12_workspace_ids("ws-a, ws-b\nhttps://chatgpt.com/g/ws-c?x=1 ws-a"),
            ["ws-a", "ws-b", "ws-c"],
        )

    def test_normalize_account_spaces_uses_ordering_and_dedupes(self):
        spaces = normalize_account_spaces(
            {
                "account_ordering": ["default", "ws-k12"],
                "accounts": {
                    "default": {
                        "account": {
                            "id": "acct-free",
                            "name": "Personal",
                            "structure": "personal",
                        }
                    },
                    "ws-k12": {
                        "account": {
                            "id": "ws-k12",
                            "name": "School Lab",
                            "structure": "workspace",
                            "plan_type": "edu",
                        }
                    },
                },
            }
        )
        self.assertEqual([space.workspace_id for space in spaces], ["acct-free", "ws-k12"])
        self.assertTrue(spaces[0].is_default)
        self.assertEqual(spaces[1].name, "School Lab")
        self.assertEqual(spaces[1].structure, "workspace")

    def test_request_k12_workspace_join_posts_invites_request(self):
        client = DummyClient()
        client.session.post_responses.append(DummyResponse(200, text='{"status":"ok"}'))

        result = request_k12_workspace_join(
            chatgpt_client=client,
            workspace_id="ws-k12",
            access_token="at",
            cookies="cookie=1",
            timeout=10,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.workspace_id, "ws-k12")
        self.assertEqual(len(client.session.posts), 1)
        url, kwargs = client.session.posts[0]
        self.assertIn("/backend-api/accounts/ws-k12/invites/request", url)
        self.assertIn("Authorization", kwargs["headers"])
        self.assertEqual(kwargs["data"], "")

    def test_request_k12_workspace_join_reports_failure_without_secret_values(self):
        client = DummyClient()
        client.session.post_responses.append(DummyResponse(403, text="forbidden"))

        result = request_k12_workspace_join(
            chatgpt_client=client,
            workspace_id="ws-k12",
            access_token="secret-access-token",
            cookies="secret-cookie=1",
        )

        self.assertFalse(result.ok)
        raw = repr(result.to_summary())
        self.assertNotIn("secret-access-token", raw)
        self.assertNotIn("secret-cookie", raw)

    def test_request_k12_workspace_join_redacts_exception_text(self):
        client = DummyClient()
        client.session = FailingPostSession()

        result = request_k12_workspace_join(
            chatgpt_client=client,
            workspace_id="ws-k12",
            access_token="secret-access-token",
            cookies="__Secure-next-auth.session-token=secret-cookie",
        )

        self.assertFalse(result.ok)
        raw = repr(result.to_summary())
        self.assertNotIn("secret-access-token", raw)
        self.assertNotIn("secret-cookie", raw)

    def test_capture_disabled_when_enabled_false_even_with_workspace_ids(self):
        client = DummyClient()

        result = capture_k12_and_all_spaces(
            chatgpt_client=client,
            base_session={"access_token": "at-base", "session_token": "session-cookie", "cookies": "cookie=1"},
            access_token="at-base",
            target_workspace_ids="ws-k12",
            config={
                "chatgpt_k12_enabled": False,
                "chatgpt_k12_save_all_spaces": True,
            },
        )

        self.assertFalse(result["summary"]["enabled"])
        self.assertEqual(result["summary"]["saved_spaces"], 0)
        self.assertEqual(client.session.posts, [])
        self.assertEqual(client.session.gets, [])
        self.assertEqual(client.exchanged, [])

    def test_capture_k12_and_all_spaces_builds_free_and_k12_artifacts(self):
        client = DummyClient()
        client.session.post_responses.append(DummyResponse(200, text='{"status":"ok"}'))
        client.session.get_responses.append(
            DummyResponse(
                200,
                payload={
                    "account_ordering": ["default", "ws-k12"],
                    "accounts": {
                        "default": {
                            "account": {
                                "id": "acct-free",
                                "name": "Personal",
                                "structure": "personal",
                            }
                        },
                        "ws-k12": {
                            "account": {
                                "id": "ws-k12",
                                "name": "School Lab",
                                "structure": "workspace",
                                "plan_type": "edu",
                            }
                        },
                    },
                },
            )
        )

        result = capture_k12_and_all_spaces(
            chatgpt_client=client,
            base_session={"access_token": "at-base", "session_token": "session-cookie", "cookies": "cookie=1"},
            access_token="at-base",
            session_token="session-cookie",
            cookies="cookie=1",
            target_workspace_ids="ws-k12",
            config={
                "chatgpt_k12_enabled": True,
                "chatgpt_k12_save_all_spaces": True,
                "chatgpt_k12_post_join_poll_seconds": "0",
            },
            log_fn=lambda *_args: None,
        )

        self.assertEqual(result["summary"]["joined"], 1)
        self.assertEqual(result["summary"]["saved_spaces"], 2)
        self.assertEqual(
            [(item["scope"], item["variant_key"]) for item in result["artifacts"]],
            [("free", "free:acct-free"), ("k12", "k12:ws-k12")],
        )
        self.assertEqual(client.exchanged[0][0], "acct-free")
        self.assertEqual(client.exchanged[1][0], "ws-k12")
        self.assertEqual(result["artifacts"][1]["space"]["name"], "School Lab")
        self.assertEqual(result["artifacts"][1]["k12_join"]["status_code"], 200)

    def test_strict_join_fails_when_target_exchange_fails(self):
        client = ExchangeFailClient()
        client.session.post_responses.append(DummyResponse(200, text='{"status":"ok"}'))
        client.session.get_responses.append(
            DummyResponse(
                200,
                payload={
                    "account_ordering": ["default", "ws-k12"],
                    "accounts": {
                        "default": {
                            "account": {
                                "id": "acct-free",
                                "name": "Personal",
                                "structure": "personal",
                            }
                        },
                        "ws-k12": {
                            "account": {
                                "id": "ws-k12",
                                "name": "School Lab",
                                "structure": "workspace",
                                "plan_type": "edu",
                            }
                        },
                    },
                },
            )
        )

        result = capture_k12_and_all_spaces(
            chatgpt_client=client,
            base_session={"access_token": "at-base", "session_token": "session-cookie", "cookies": "cookie=1"},
            access_token="at-base",
            target_workspace_ids="ws-k12",
            config={
                "chatgpt_k12_enabled": True,
                "chatgpt_k12_save_all_spaces": True,
                "chatgpt_k12_strict_join": True,
                "chatgpt_k12_post_join_poll_seconds": "0",
            },
            log_fn=lambda *_args: None,
        )

        self.assertTrue(result["summary"]["strict_join_failed"])
        self.assertEqual(result["summary"]["exchange_failed_target_ids"], ["ws-k12"])
        self.assertEqual(
            [(item["scope"], item["variant_key"]) for item in result["artifacts"]],
            [("free", "free:acct-free")],
        )


if __name__ == "__main__":
    unittest.main()
