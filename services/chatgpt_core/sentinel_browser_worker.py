"""Internal subprocess entrypoint for killable Playwright transactions."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from typing import Any


def _write_message(protocol_fd: int, message: dict[str, Any]) -> None:
    payload = (
        json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    view = memoryview(payload)
    while view:
        written = os.write(protocol_fd, view)
        view = view[written:]


def _read_message() -> dict[str, Any]:
    raw = sys.stdin.readline()
    if not raw:
        raise EOFError("browser worker control channel closed")
    message = json.loads(raw)
    if not isinstance(message, dict):
        raise ValueError("browser worker control message must be an object")
    return message


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    protocol_fd = int(sys.argv[1])
    # The protocol belongs only to this worker. Chromium and the Playwright Node
    # driver must not keep it open after this process exits.
    os.set_inheritable(protocol_fd, False)

    try:
        request = _read_message()
        operation = str(request.get("operation") or "")
        payload = request.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("browser worker payload must be an object")

        from services.chatgpt_core.sentinel_browser import (
            _create_account_via_browser_sync,
            _get_sentinel_token_via_browser_sync,
        )

        logger = lambda message: _write_message(
            protocol_fd,
            {"type": "log", "message": str(message)},
        )

        callback_sequence = 0

        def request_callback(name: str, callback_payload: dict[str, Any]) -> Any:
            nonlocal callback_sequence
            callback_sequence += 1
            callback_id = f"callback-{callback_sequence}"
            _write_message(
                protocol_fd,
                {
                    "type": "callback_request",
                    "id": callback_id,
                    "name": str(name or ""),
                    "payload": dict(callback_payload or {}),
                },
            )
            response = _read_message()
            if response.get("type") != "callback_response":
                raise RuntimeError("invalid browser callback response type")
            if str(response.get("id") or "") != callback_id:
                raise RuntimeError("browser callback response id mismatch")
            error = str(response.get("error") or "").strip()
            if error:
                raise RuntimeError(error)
            return response.get("value")

        if operation == "sentinel_token":
            value = _get_sentinel_token_via_browser_sync(
                **payload,
                log_fn=logger,
            )
        elif operation == "create_account":
            value = _create_account_via_browser_sync(
                **payload,
                stop_check=None,
                log_fn=logger,
            )
            if value is not None:
                value = asdict(value)
        elif operation == "browser_registration":
            from services.chatgpt_core.browser_registration import (
                run_browser_registration_stage_sync,
            )

            try:
                value = run_browser_registration_stage_sync(
                    **payload,
                    otp_callback=lambda callback_payload=None: request_callback(
                        "otp", dict(callback_payload or {})
                    ),
                    log_fn=logger,
                )
            except Exception as exc:
                value = {
                    "error": (
                        f"browser_registration_failed: {type(exc).__name__}: {exc}"
                    )[:1000]
                }
        elif operation == "browser_oauth_token_recovery":
            from services.chatgpt_core.browser_registration import (
                run_browser_oauth_token_recovery_sync,
            )

            try:
                value = run_browser_oauth_token_recovery_sync(
                    **payload,
                    otp_callback=lambda callback_payload=None: request_callback(
                        "otp", dict(callback_payload or {})
                    ),
                    log_fn=logger,
                )
            except Exception as exc:
                value = {
                    "error": (
                        "browser_oauth_token_recovery_failed: "
                        f"{type(exc).__name__}: {exc}"
                    )[:1000]
                }
        else:
            raise ValueError(f"unsupported browser worker operation: {operation}")

        _write_message(protocol_fd, {"type": "result", "value": value})
        return 0
    except BaseException as exc:  # keep protocol alive for all worker failures
        try:
            _write_message(
                protocol_fd,
                {
                    "type": "error",
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                },
            )
        except Exception:
            pass
        return 1
    finally:
        try:
            os.close(protocol_fd)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
