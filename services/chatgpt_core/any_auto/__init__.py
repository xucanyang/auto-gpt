"""Vendored any-auto-register ChatGPT registration transport.

Source: /opt/any-auto-register/platforms/chatgpt (read-only reference).
protocol / headless / headed all run through this package; auto-gpt only maps
results into its inventory contract.
"""

from .transport import (
    AnyAutoRegistrationResult,
    run_any_auto_browser_registration,
    run_any_auto_protocol_registration,
)

__all__ = [
    "AnyAutoRegistrationResult",
    "run_any_auto_browser_registration",
    "run_any_auto_protocol_registration",
]
