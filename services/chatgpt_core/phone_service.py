from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests

from smstome_tool import (
    PhoneEntry,
    get_unused_phone,
    mark_phone_blacklisted,
    parse_country_slugs,
    update_global_phone_list,
    wait_for_otp,
)


def _to_positive_int(value, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return default
    return parsed if parsed >= minimum else default


def _prefix_hint(phone: str, width: int = 7) -> str:
    value = str(phone or "").strip()
    return value[: min(len(value), width)] if value else ""


class SMSToMePhoneService:
    def __init__(self, config: Optional[dict] = None, log_fn: Optional[Callable[[str], None]] = None):
        self.config = dict(config or {})
        self.log_fn = log_fn or (lambda _msg: None)
        self.cookie_header = str(self.config.get("smstome_cookie", "") or "").strip() or None
        self.country_slugs = parse_country_slugs(self.config.get("smstome_country_slugs"))
        self.global_file = Path(str(self.config.get("smstome_global_file") or "smstome_all_numbers.txt"))
        self.used_numbers_dir = Path(str(self.config.get("smstome_used_numbers_dir") or "smstome_used"))
        self.task_name = str(self.config.get("smstome_task_name") or "chatgpt_add_phone").strip() or "chatgpt_add_phone"
        self.max_attempts = _to_positive_int(self.config.get("smstome_phone_attempts"), 3)
        self.otp_timeout_seconds = _to_positive_int(self.config.get("smstome_otp_timeout_seconds"), 45, minimum=10)
        self.poll_interval_seconds = _to_positive_int(self.config.get("smstome_poll_interval_seconds"), 5, minimum=1)
        self.sync_max_pages_per_country = _to_positive_int(
            self.config.get("smstome_sync_max_pages_per_country"),
            5,
        )

    @property
    def enabled(self) -> bool:
        return self._has_pool_file() or bool(self.cookie_header)

    def prefix_hint(self, phone: str) -> str:
        return _prefix_hint(phone)

    def _has_pool_file(self) -> bool:
        try:
            return self.global_file.exists() and self.global_file.stat().st_size > 0
        except OSError:
            return False

    def ensure_pool_ready(self) -> None:
        if self._has_pool_file():
            return
        if not self.cookie_header:
            raise RuntimeError("未找到 SMSToMe 号码池文件，且未配置 smstome_cookie")

        self.log_fn("SMSToMe 号码池不存在，开始自动同步...")
        count = update_global_phone_list(
            cookie_header=self.cookie_header,
            countries=self.country_slugs or None,
            output_path=self.global_file,
            max_pages_per_country=self.sync_max_pages_per_country,
        )
        if count <= 0:
            raise RuntimeError("SMSToMe 号码池同步后为空")
        self.log_fn(f"SMSToMe 号码池同步完成，共 {count} 个号码")

    def acquire_phone(self, *, exclude_prefixes: Optional[Iterable[str]] = None, **_kwargs) -> Optional[PhoneEntry]:
        self.ensure_pool_ready()
        return get_unused_phone(
            self.task_name,
            country_slug=self.country_slugs or None,
            global_file=self.global_file,
            used_numbers_dir=self.used_numbers_dir,
            exclude_prefixes=exclude_prefixes,
        )

    def mark_blacklisted(self, phone: str) -> None:
        mark_phone_blacklisted(self.task_name, phone, used_numbers_dir=self.used_numbers_dir)

    def mark_sms_sent(self, _entry: PhoneEntry) -> None:
        return None

    def request_next_code(self, _entry: PhoneEntry) -> bool:
        return False

    def complete(self, _entry: PhoneEntry) -> None:
        return None

    def cancel(self, _entry: PhoneEntry, *, reason: str = "") -> None:
        return None

    def wait_for_code(self, entry: PhoneEntry, *, timeout: Optional[int] = None) -> Optional[str]:
        wait_seconds = _to_positive_int(timeout, self.otp_timeout_seconds, minimum=10)
        return wait_for_otp(
            entry,
            cookie_header=self.cookie_header,
            timeout=wait_seconds,
            poll_interval=self.poll_interval_seconds,
            trace=lambda message: self.log_fn(f"[SMSToMe] {message}"),
            raise_on_timeout=False,
        )


@dataclass(frozen=True)
class LocalGatewayPhoneEntry:
    country_slug: str
    phone: str
    detail_url: str
    activation_id: str
    provider: str = ""
    provider_activation_id: str = ""


class LocalPhoneGatewayService:
    def __init__(self, config: Optional[dict] = None, log_fn: Optional[Callable[[str], None]] = None):
        self.config = dict(config or {})
        self.log_fn = log_fn or (lambda _msg: None)
        self.base_url = str(self.config.get("local_phone_gateway_url") or "").strip().rstrip("/")
        self.token = str(self.config.get("local_phone_gateway_token") or "").strip()
        self.service_alias = str(self.config.get("local_phone_gateway_service_alias") or "chatgpt").strip() or "chatgpt"
        self.consumer = str(self.config.get("local_phone_gateway_consumer") or "any-auto-register-local").strip()
        self.max_attempts = _to_positive_int(
            self.config.get("local_phone_gateway_max_attempts") or self.config.get("smstome_phone_attempts"),
            3,
        )
        self.otp_timeout_seconds = _to_positive_int(
            self.config.get("local_phone_gateway_timeout_seconds") or self.config.get("smstome_otp_timeout_seconds"),
            180,
            minimum=10,
        )
        self.poll_interval_seconds = _to_positive_int(
            self.config.get("local_phone_gateway_poll_interval_seconds") or self.config.get("smstome_poll_interval_seconds"),
            5,
            minimum=1,
        )
        self._entries_by_phone: dict[str, LocalGatewayPhoneEntry] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.token)

    def prefix_hint(self, phone: str) -> str:
        return _prefix_hint(phone)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(self, method: str, path: str, *, json_body: Optional[dict] = None, timeout: Optional[int] = None) -> dict:
        if not self.enabled:
            raise RuntimeError("本地接码网关未配置 local_phone_gateway_url/local_phone_gateway_token")
        try:
            resp = requests.request(
                method,
                self._url(path),
                headers=self._headers(),
                json=json_body,
                timeout=timeout or 30,
            )
        except Exception as exc:
            raise RuntimeError(f"本地接码网关请求失败: {exc}") from exc
        try:
            data = resp.json()
        except Exception:
            data = {"message": resp.text[:300]}
        if resp.status_code >= 400 or not bool(data.get("ok", resp.status_code < 400)):
            message = str(data.get("message") or data.get("detail") or data.get("error") or resp.text[:300] or "本地接码网关返回失败")
            raise RuntimeError(message)
        return data

    def acquire_phone(
        self,
        *,
        exclude_prefixes: Optional[Iterable[str]] = None,
        email: str = "",
        account_id: int = 0,
        task_id: str = "",
        purpose: str = "chatgpt_add_phone",
    ) -> Optional[LocalGatewayPhoneEntry]:
        body = {
            "consumer": self.consumer,
            "task_id": str(task_id or ""),
            "account_id": int(account_id or 0),
            "email": str(email or ""),
            "purpose": purpose,
            "service_alias": self.service_alias,
            "timeout_seconds": self.otp_timeout_seconds,
            "exclude_prefixes": list(exclude_prefixes or []),
        }
        data = self._request("POST", "/api/v1/activations", json_body=body)
        activation_id = str(data.get("activation_id") or "").strip()
        phone = str(data.get("phone") or "").strip()
        if not activation_id or not phone:
            raise RuntimeError("本地接码网关未返回有效 activation_id/phone")
        entry = LocalGatewayPhoneEntry(
            country_slug=str(data.get("country") or data.get("country_id") or "local_gateway"),
            phone=phone,
            detail_url=f"{self.base_url}/api/v1/activations/{activation_id}",
            activation_id=activation_id,
            provider=str(data.get("provider") or ""),
            provider_activation_id=str(data.get("provider_activation_id") or ""),
        )
        self._entries_by_phone[phone] = entry
        self.log_fn(f"[接码网关] 已取号: activation_id={activation_id}, provider={entry.provider or '-'}")
        return entry

    def mark_sms_sent(self, entry: LocalGatewayPhoneEntry) -> None:
        self._request("POST", f"/api/v1/activations/{entry.activation_id}/sent", json_body={"detail": "OpenAI add-phone/send ok"})

    def request_next_code(self, entry: LocalGatewayPhoneEntry) -> bool:
        try:
            self._request("POST", f"/api/v1/activations/{entry.activation_id}/retry", json_body={"reason": "OpenAI resend"})
            return True
        except Exception as exc:
            self.log_fn(f"[接码网关] 请求下一条短信失败: {exc}")
            return False

    def complete(self, entry: LocalGatewayPhoneEntry) -> None:
        try:
            self._request("POST", f"/api/v1/activations/{entry.activation_id}/complete", json_body={"reason": "OpenAI phone OTP validated"})
        except Exception as exc:
            self.log_fn(f"[接码网关] 完成订单失败: {exc}")

    def cancel(self, entry: LocalGatewayPhoneEntry, *, reason: str = "") -> None:
        try:
            self._request("POST", f"/api/v1/activations/{entry.activation_id}/cancel", json_body={"reason": reason})
        except Exception as exc:
            self.log_fn(f"[接码网关] 取消订单失败: {exc}")

    def mark_blacklisted(self, phone: str) -> None:
        entry = self._entries_by_phone.get(str(phone or "").strip())
        if entry:
            self.cancel(entry, reason="phone blacklisted by upstream")

    def wait_for_code(self, entry: LocalGatewayPhoneEntry, *, timeout: Optional[int] = None) -> Optional[str]:
        wait_seconds = _to_positive_int(timeout, self.otp_timeout_seconds, minimum=10)
        try:
            data = self._request(
                "GET",
                f"/api/v1/activations/{entry.activation_id}/code?wait_seconds={wait_seconds}&poll_interval_seconds={self.poll_interval_seconds}",
                timeout=wait_seconds + 30,
            )
        except Exception as exc:
            self.log_fn(f"[接码网关] 等待验证码失败: {exc}")
            return None
        return str(data.get("code") or "").strip() or None


def create_phone_service(config: Optional[dict] = None, log_fn: Optional[Callable[[str], None]] = None):
    cfg = dict(config or {})
    provider = str(cfg.get("chatgpt_phone_verification_provider") or cfg.get("phone_verification_provider") or "smstome").strip().lower()
    if provider in {"local_gateway", "gateway", "local"}:
        return LocalPhoneGatewayService(cfg, log_fn=log_fn)
    return SMSToMePhoneService(cfg, log_fn=log_fn)
