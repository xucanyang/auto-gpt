from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import threading
import time
from typing import Callable


@dataclass(frozen=True)
class ExitIPClaim:
    claimed: bool
    key: str
    holder: str = ""
    state: str = ""
    expires_in_seconds: float = 0.0


@dataclass
class _ExitIPLease:
    owner: str
    state: str
    expires_at: float


def normalize_register_exit_ip(value: str) -> str:
    """Normalize IPv4 hosts and IPv6 network identities for de-duplication."""

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        # A probe should return a literal IP. Keeping malformed values stable is
        # still safer than allowing two tasks to bypass the registry entirely.
        return text.lower()
    if address.version == 4:
        return str(address)
    return str(ipaddress.ip_network(f"{address}/64", strict=False))


class RegisterExitIPRegistry:
    """Process-wide active/cooldown leases for ChatGPT registration exits."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._leases: dict[str, _ExitIPLease] = {}

    def _prune_locked(self, now: float) -> None:
        expired = [key for key, lease in self._leases.items() if lease.expires_at <= now]
        for key in expired:
            self._leases.pop(key, None)

    def claim(
        self,
        exit_ip: str,
        *,
        owner: str,
        active_ttl_seconds: float,
    ) -> ExitIPClaim:
        key = normalize_register_exit_ip(exit_ip)
        owner_key = str(owner or "").strip()
        if not key or not owner_key:
            return ExitIPClaim(False, key, state="invalid")

        now = self._clock()
        ttl = max(float(active_ttl_seconds or 0), 1.0)
        with self._lock:
            self._prune_locked(now)
            current = self._leases.get(key)
            if current is not None and not (
                current.state == "active" and current.owner == owner_key
            ):
                return ExitIPClaim(
                    False,
                    key,
                    holder=current.owner,
                    state=current.state,
                    expires_in_seconds=max(current.expires_at - now, 0.0),
                )
            self._leases[key] = _ExitIPLease(
                owner=owner_key,
                state="active",
                expires_at=now + ttl,
            )
            return ExitIPClaim(True, key, holder=owner_key, state="active")

    def release_owner(self, owner: str, *, cooldown_seconds: float) -> int:
        owner_key = str(owner or "").strip()
        if not owner_key:
            return 0
        now = self._clock()
        cooldown = max(float(cooldown_seconds or 0), 0.0)
        released = 0
        with self._lock:
            self._prune_locked(now)
            for key, lease in list(self._leases.items()):
                if lease.state != "active" or lease.owner != owner_key:
                    continue
                released += 1
                if cooldown <= 0:
                    self._leases.pop(key, None)
                    continue
                self._leases[key] = _ExitIPLease(
                    owner=f"cooldown:{owner_key}",
                    state="cooldown",
                    expires_at=now + cooldown,
                )
        return released

    def refresh_owner(self, owner: str, *, active_ttl_seconds: float) -> int:
        """Extend every active lease held by an in-flight registration attempt."""

        owner_key = str(owner or "").strip()
        if not owner_key:
            return 0
        now = self._clock()
        ttl = max(float(active_ttl_seconds or 0), 1.0)
        refreshed = 0
        with self._lock:
            self._prune_locked(now)
            for lease in self._leases.values():
                if lease.state != "active" or lease.owner != owner_key:
                    continue
                lease.expires_at = now + ttl
                refreshed += 1
        return refreshed

    def snapshot(self) -> dict[str, dict[str, object]]:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            return {
                key: {
                    "owner": lease.owner,
                    "state": lease.state,
                    "expires_in_seconds": max(lease.expires_at - now, 0.0),
                }
                for key, lease in self._leases.items()
            }


register_exit_ip_registry = RegisterExitIPRegistry()
