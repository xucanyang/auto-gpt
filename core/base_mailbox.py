"""邮箱池基类 - 抽象临时邮箱/收件服务"""

from datetime import datetime, timezone
import json
import os
import random
import re
import tempfile
import threading
import time
import hashlib

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Any, Callable
from .proxy_utils import build_requests_proxy_config


VERIFICATION_KEYWORDS = (
    "openai",
    "chatgpt",
    "codex",
    "verify",
    "verification",
    "verification code",
    "security code",
    "one-time code",
    "temporary code",
    "otp",
    "验证码",
    "校验码",
    "验证代码",
    "临时验证码",
    "临时代码",
)


@dataclass
class MailboxAccount:
    email: str
    account_id: str = ""
    extra: dict = None  # 平台额外信息


class TempMailReadyAuthError(RuntimeError):
    """TempMail Ready 鉴权/配置错误，不能按普通“未收到验证码”继续轮询。"""


class BaseMailbox(ABC):
    def _log(self, message: str) -> None:
        log_fn = getattr(self, "_log_fn", None)
        if callable(log_fn):
            log_fn(message)

    def _checkpoint(self, *, consume_skip: bool = True) -> None:
        task_control = getattr(self, "_task_control", None)
        if task_control is None:
            return
        task_control.checkpoint(
            consume_skip=consume_skip,
            attempt_id=getattr(self, "_task_attempt_token", None),
        )

    def _sleep_with_checkpoint(self, seconds: float) -> None:
        remaining = max(float(seconds or 0), 0.0)
        while remaining > 0:
            self._checkpoint()
            chunk = min(0.25, remaining)
            time.sleep(chunk)
            remaining -= chunk

    def _run_polling_wait(
        self,
        *,
        timeout: int,
        poll_interval: float,
        poll_once: Callable[[], Optional[str]],
        timeout_message: str | None = None,
    ) -> str:
        timeout_seconds = max(int(timeout or 0), 1)
        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:
            self._checkpoint()
            code = poll_once()
            if code:
                return code

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._sleep_with_checkpoint(min(float(poll_interval), remaining))

        self._checkpoint()
        raise TimeoutError(timeout_message or f"等待验证码超时 ({timeout_seconds}s)")

    def _record_verification_result(
        self,
        *,
        message_id: Any = "",
        code: str = "",
        phase: str = "",
        provider: str = "",
        metadata: Optional[dict] = None,
    ) -> None:
        payload = dict(metadata or {})
        payload["message_id"] = str(message_id or payload.get("message_id") or payload.get("id") or "").strip()
        payload["code"] = str(code or payload.get("code") or "").strip()
        payload["phase"] = str(phase or payload.get("phase") or "").strip()
        payload["provider"] = str(provider or payload.get("provider") or self.__class__.__name__).strip()
        payload["recorded_at"] = time.time()
        self._last_verification_result = payload

    @abstractmethod
    def get_email(self) -> MailboxAccount:
        """获取一个可用邮箱"""
        ...

    @abstractmethod
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        """等待并返回验证码，code_pattern 为自定义正则（默认匹配6位数字）"""
        ...

    def _safe_extract(self, text: str, pattern: str = None) -> Optional[str]:
        """通用验证码提取逻辑：若有捕获组则返回 group(1)，否则返回 group(0)"""
        import re

        text = str(text or "")
        if not text:
            return None

        patterns = []
        if pattern:
            patterns.append(pattern)

        # 先匹配带明显语义的验证码，避免误提取 MIME boundary、时间戳等 6 位数字。
        patterns.extend(
            [
                r"(?is)(?:verification\s+code|one[-\s]*time\s+(?:password|code)|security\s+code|login\s+code|验证码|校验码|动态码|認證碼|驗證碼)[^0-9]{0,30}(\d{6})",
                r"(?is)\bcode\b[^0-9]{0,12}(\d{6})",
                r"(?<!#)(?<!\d)(\d{6})(?!\d)",
            ]
        )

        for regex in patterns:
            m = re.search(regex, text)
            if m:
                # 兼容逻辑：若 pattern 中有捕获组则取 group(1)，否则取 group(0)
                return m.group(1) if m.groups() else m.group(0)
        return None

    def _strip_html_to_text(self, content: str) -> str:
        import html
        import re

        text = str(content or "")
        if not text:
            return ""
        text = html.unescape(text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _extract_verification_code_scored(
        self, subject: str, text: str, html_content: str
    ) -> tuple[str, str]:
        import re

        raw_content = "\n".join(
            part
            for part in (
                str(subject or ""),
                str(text or ""),
                self._strip_html_to_text(html_content),
            )
            if part
        ).strip()
        if not raw_content:
            return "", ""

        normalized = self._strip_html_to_text(raw_content)
        subject_text = str(subject or "").strip()
        subject_lower = subject_text.lower()

        candidates: list[tuple[str, int, str]] = []

        def _score_candidate(code: str, score: int, source: str) -> None:
            if not code:
                return
            normalized_code = (
                code.upper() if re.search(r"[A-Za-z]", code or "") else code
            )
            candidates.append((normalized_code, score, source))

        def is_valid_code(value: str) -> bool:
            if not value:
                return False
            if re.fullmatch(r"\d{4,8}", value):
                return True
            return bool(
                re.fullmatch(r"(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9]{4,8}", value)
            )

        subject_has_keyword = any(
            keyword in subject_lower for keyword in VERIFICATION_KEYWORDS
        )

        for match in re.finditer(r"\b([A-Za-z0-9]{4,8})\b", subject_text):
            code = match.group(1).strip()
            if not is_valid_code(code):
                continue
            score = 360 + (120 if subject_has_keyword else 0) - len(code)
            _score_candidate(code, score, "主题命中")

        lines = [line.strip() for line in normalized.split("\n") if line.strip()]
        for idx, line in enumerate(lines):
            prev_line = lines[idx - 1] if idx > 0 else ""
            next_line = lines[idx + 1] if idx + 1 < len(lines) else ""
            context = f"{prev_line} {line} {next_line}".lower()
            has_keyword = any(
                keyword in context for keyword in VERIFICATION_KEYWORDS
            )
            for match in re.finditer(r"\b([A-Za-z0-9]{4,8})\b", line):
                code = match.group(1).strip()
                if not is_valid_code(code):
                    continue
                if line == code:
                    source = (
                        "正文独立数字行命中"
                        if code.isdigit()
                        else "正文独立字母数字行命中"
                    )
                    base_score = 260
                else:
                    source = "正文数字命中" if code.isdigit() else "正文混合码命中"
                    base_score = 180
                score = base_score + (180 if has_keyword else 0) - len(code)
                _score_candidate(code, score, source)

        keyword_patterns = [
            re.compile(
                r"(验证码|校验码|验证代码|代码|临时验证码|临时代码|verification code|verification codes|security code|security codes|one-time code|temporary code|otp|code)(?:[^A-Za-z0-9]{0,20})([A-Za-z0-9]{4,8})",
                re.I,
            ),
            re.compile(
                r"([A-Za-z0-9]{4,8})(?:[^A-Za-z0-9]{0,20})(验证码|校验码|验证代码|代码|临时验证码|临时代码|verification code|verification codes|security code|security codes|one-time code|temporary code|otp|code)",
                re.I,
            ),
        ]
        for pattern in keyword_patterns:
            for match in pattern.finditer(normalized):
                if len(match.groups()) > 1:
                    code = match.group(2).strip()
                    if not is_valid_code(code):
                        code = match.group(1).strip()
                else:
                    code = match.group(1).strip()
                if not is_valid_code(code):
                    continue
                base_score = 320 if code.isdigit() else 340
                score = base_score - len(code)
                _score_candidate(code, score, "关键词近邻命中")

        for match in re.finditer(r"\b([A-Za-z0-9]{4,8})\b", normalized):
            code = match.group(1).strip()
            if not is_valid_code(code):
                continue
            start = match.start()
            context = normalized[
                max(0, start - 60) : min(len(normalized), start + len(code) + 60)
            ].lower()
            has_keyword = any(
                keyword in context for keyword in VERIFICATION_KEYWORDS
            )
            base_score = 120 if code.isdigit() else 100
            score = base_score + (170 if has_keyword else 0) - len(code)
            source = "上下文命中" if has_keyword else "兜底命中"
            _score_candidate(code, score, source)

        candidates.sort(key=lambda item: item[1], reverse=True)
        if not candidates:
            return "", ""
        return candidates[0][0], candidates[0][2]

    def _decode_raw_content(self, raw: str) -> str:
        """解析邮件原始文本 (借鉴自 Fugle)，处理 Quoted-Printable 和 HTML 实体"""
        import quopri, html, re

        text = str(raw or "")
        if not text:
            return ""
        # 简单切分 Header 和 Body
        if "\r\n\r\n" in text:
            text = text.split("\r\n\r\n", 1)[1]
        elif "\n\n" in text:
            text = text.split("\n\n", 1)[1]
        try:
            # 处理 Quoted-Printable
            decoded_bytes = quopri.decodestring(text)
            text = decoded_bytes.decode("utf-8", errors="ignore")
        except Exception:
            pass
        # 清除 HTML 标签并反转义
        text = html.unescape(text)
        text = re.sub(r"(?im)^content-(?:type|transfer-encoding):.*$", " ", text)
        text = re.sub(r"(?im)^--+[_=\w.-]+$", " ", text)
        text = re.sub(r"(?i)----=_part_[\w.]+", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @abstractmethod
    def get_current_ids(self, account: MailboxAccount) -> set:
        """返回当前邮件 ID 集合（用于过滤旧邮件）"""
        ...
    def _yyds_safe_extract(self, text: str, pattern: str = None) -> Optional[str]:
        """通用验证码提取逻辑：若有捕获组则返回 group(1)，否则返回 group(0)"""
        import re

        text = str(text or "")
        if not text:
            return None

        # [修复点 1]：优先过滤掉所有 URL 链接，直接从根源防止提取到追踪链接（如 SendGrid）里的随机数字
        text = re.sub(r"https?://\S+", "", text)

        patterns = []
        if pattern:
            # [修复点 2]：如果外部传入了纯 \d{6} 的粗糙正则，自动为其加上字母数字边界
            if pattern in (r"\d{6}", r"(\d{6})"):
                patterns.append(r"(?<![a-zA-Z0-9])(\d{6})(?![a-zA-Z0-9])")
            else:
                patterns.append(pattern)

        # 先匹配带明显语义的验证码，避免误提取 MIME boundary、时间戳等 6 位数字。
        patterns.extend(
            [
                r"(?is)(?:verification\s+code|one[-\s]*time\s+(?:password|code)|security\s+code|login\s+code|验证码|校验码|动态码|認證碼|驗證碼)[^0-9]{0,30}(\d{6})",
                r"(?is)\bcode\b[^0-9]{0,12}(\d{6})",
                # [修复点 3]：修改兜底正则，严格要求 6 位数字前后不能有字母或数字（防止匹配 u20216706）
                r"(?<![a-zA-Z0-9])(\d{6})(?![a-zA-Z0-9])",
            ]
        )

        for regex in patterns:
            m = re.search(regex, text)
            if m:
                # 兼容逻辑：若 pattern 中有捕获组则取 group(1)，否则取 group(0)
                return m.group(1) if m.groups() else m.group(0)
        return None

    def _yyds_decode_raw_content(self, raw: str) -> str:
        """解析邮件原始文本 (借鉴自 Fugle)，处理 Quoted-Printable 和 HTML 实体"""
        import quopri, html, re

        text = str(raw or "")
        if not text:
            return ""
            
        # [修复点 4]：只有在明确包含常见邮件 Header 时，才进行 \r\n\r\n 切分。
        # 否则会误删 MaliAPI 等直接返回的已解析 JSON 正文内容（遇到普通的正文换行就错误截断了）
        if re.search(r"(?im)^(?:Return-Path|Received|Date|From|To|Subject|Content-Type):", text):
            if "\r\n\r\n" in text:
                text = text.split("\r\n\r\n", 1)[1]
            elif "\n\n" in text:
                text = text.split("\n\n", 1)[1]
                
        try:
            # 处理 Quoted-Printable
            decoded_bytes = quopri.decodestring(text)
            text = decoded_bytes.decode("utf-8", errors="ignore")
        except Exception:
            pass
        # 清除 HTML 标签并反转义
        text = html.unescape(text)
        text = re.sub(r"(?im)^content-(?:type|transfer-encoding):.*$", " ", text)
        text = re.sub(r"(?im)^--+[_=\w.-]+$", " ", text)
        text = re.sub(r"(?i)----=_part_[\w.]+", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text


class ManualEmailOtpMailbox(BaseMailbox):
    """人工邮箱验证码模式：邮箱地址由用户提供，验证码由任务页手动提交。"""

    def __init__(self, email: str = "", extra: dict = None, proxy: str = None):
        self._email = str(email or "").strip()
        self._extra = dict(extra or {})
        self._proxy = proxy
        self._tempmail_mailbox = None
        self._tempmail_account_cache: dict[str, MailboxAccount | None] = {}
        self._tempmail_domain_allowlist: set[str] | None = None

    @staticmethod
    def _email_domain(email: str) -> str:
        normalized = str(email or "").strip().lower()
        if "@" not in normalized:
            return ""
        return normalized.rsplit("@", 1)[-1].strip().lstrip("@.")

    @staticmethod
    def _read_bool(value: Any, default: bool = False) -> bool:
        if value in (None, ""):
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _auto_tempmail_enabled(self) -> bool:
        return self._read_bool(
            self._extra.get("manual_email_otp_auto_tempmail"),
            default=True,
        )

    def _build_tempmail_mailbox(self):
        if self._tempmail_mailbox is not None:
            return self._tempmail_mailbox

        api_url = str(self._extra.get("tempmail_api_url") or "").strip()
        api_key = str(self._extra.get("tempmail_api_key") or "").strip()
        if not api_url or not api_key:
            return None

        mailbox = TempMailLocalMailbox(
            api_url=api_url,
            api_key=api_key,
            api_key_header=self._extra.get("tempmail_api_key_header", "Authorization"),
            primary_domain=self._extra.get("tempmail_primary_domain", ""),
            primary_domains=self._extra.get("tempmail_fixed_domains", ""),
            mode="fixed_domain",
            wait_timeout_seconds=self._extra.get("tempmail_wait_timeout_seconds", 180),
            ttl_minutes=self._extra.get("tempmail_ttl_minutes", 30),
            reuse_window_minutes=self._extra.get("tempmail_reuse_window_minutes", 20),
            permanent=self._extra.get("tempmail_permanent", False),
            platform=self._extra.get("tempmail_platform", "chatgpt"),
            proxy=self._proxy,
        )
        mailbox._log_fn = getattr(self, "_log_fn", None)
        mailbox._task_control = getattr(self, "_task_control", None)
        mailbox._task_attempt_token = getattr(self, "_task_attempt_token", None)
        self._tempmail_mailbox = mailbox
        return mailbox

    @staticmethod
    def _normalize_tempmail_domain_item(item: Any) -> tuple[str, bool]:
        if isinstance(item, str):
            domain = str(item or "").strip().lower().lstrip("@.")
            return domain, bool(domain)
        if not isinstance(item, dict):
            return "", False
        domain = str(
            item.get("domain")
            or item.get("name")
            or item.get("value")
            or ""
        ).strip().lower().lstrip("@.")
        if not domain:
            return "", False
        is_active = item.get("is_active")
        if is_active is None:
            is_active = item.get("active")
        status = str(
            item.get("status") or ("active" if is_active is not False else "disabled")
        ).strip().lower()
        dns_status = str(item.get("dns_status") or "").strip().lower()
        allowed = (
            is_active is not False
            and status in {"", "active", "ready", "enabled"}
            and dns_status not in {"missing", "error", "failed", "invalid"}
        )
        return domain, allowed

    def _list_tempmail_domains(self) -> set[str]:
        if self._tempmail_domain_allowlist is not None:
            return set(self._tempmail_domain_allowlist)
        mailbox = self._build_tempmail_mailbox()
        if mailbox is None:
            self._tempmail_domain_allowlist = set()
            return set()
        domains: set[str] = set()
        try:
            response = mailbox._request(
                "GET",
                "/api/domains",
                headers=mailbox._headers(),
                timeout=15,
            )
            if response.status_code == 200:
                payload = response.json()
                items: list[Any] = []
                if isinstance(payload, list):
                    items = payload
                elif isinstance(payload, dict):
                    for key in ("domains", "data", "items"):
                        value = payload.get(key)
                        if isinstance(value, list):
                            items = value
                            break
                    if not items and isinstance(payload.get("data"), dict):
                        nested = payload.get("data") or {}
                        for key in ("domains", "items"):
                            value = nested.get(key)
                            if isinstance(value, list):
                                items = value
                                break
                for item in items:
                    domain, allowed = self._normalize_tempmail_domain_item(item)
                    if domain and allowed:
                        domains.add(domain)
            else:
                if mailbox._is_auth_error_response(response.status_code, response.text[:200]):
                    mailbox._raise_api_error("域名列表读取失败", response)
                self._log(
                    f"[manual_email_otp] TempMail 域名列表读取失败: {response.status_code} {response.text[:200]}"
                )
        except TempMailReadyAuthError:
            raise
        except Exception as exc:
            self._log(f"[manual_email_otp] TempMail 域名列表读取异常: {exc}")

        if not domains:
            configured = []
            primary = str(self._extra.get("tempmail_primary_domain") or "").strip()
            if primary:
                configured.append(primary)
            fixed = self._extra.get("tempmail_fixed_domains")
            if isinstance(fixed, (list, tuple, set)):
                configured.extend(str(item or "").strip() for item in fixed)
            elif fixed not in (None, ""):
                configured.extend(re.split(r"[\s,;]+", str(fixed)))
            for item in configured:
                domain = str(item or "").strip().lower().lstrip("@.")
                if domain:
                    domains.add(domain)

        self._tempmail_domain_allowlist = set(domains)
        return set(self._tempmail_domain_allowlist)

    def _email_matches_tempmail_domain(self, email: str) -> bool:
        domain = self._email_domain(email)
        if not domain:
            return False
        allowlist = self._list_tempmail_domains()
        return domain in allowlist

    def _lookup_tempmail_account(self, email: str) -> MailboxAccount | None:
        normalized_email = str(email or "").strip().lower()
        if not normalized_email:
            return None
        if normalized_email in self._tempmail_account_cache:
            return self._tempmail_account_cache[normalized_email]

        mailbox = self._build_tempmail_mailbox()
        if mailbox is None:
            self._tempmail_account_cache[normalized_email] = None
            return None

        try:
            response = mailbox._request(
                "GET",
                "/api/mailboxes",
                headers=mailbox._headers(),
                params={"page": 1, "size": 100},
                timeout=15,
            )
            if response.status_code != 200:
                if mailbox._is_auth_error_response(response.status_code, response.text[:200]):
                    mailbox._raise_api_error("邮箱查询失败", response)
                self._log(
                    f"[manual_email_otp] TempMail 邮箱查询失败: {response.status_code} {response.text[:200]}"
                )
                self._tempmail_account_cache[normalized_email] = None
                return None

            payload = response.json()
            items = payload.get("data") if isinstance(payload, dict) else []
            if not isinstance(items, list):
                self._tempmail_account_cache[normalized_email] = None
                return None

            for item in items:
                full_address = str(item.get("full_address") or "").strip().lower()
                mailbox_id = str(item.get("id") or "").strip()
                if full_address != normalized_email or not mailbox_id:
                    continue
                account = MailboxAccount(
                    email=normalized_email,
                    account_id=mailbox_id,
                    extra={"mailbox": item},
                )
                self._tempmail_account_cache[normalized_email] = account
                return account
        except TempMailReadyAuthError:
            raise
        except Exception as exc:
            self._log(f"[manual_email_otp] TempMail 邮箱查询异常: {exc}")

        self._tempmail_account_cache[normalized_email] = None
        return None

    def _ensure_tempmail_account(self, email: str) -> MailboxAccount | None:
        normalized_email = str(email or "").strip().lower()
        if not normalized_email:
            return None
        mailbox = self._build_tempmail_mailbox()
        if mailbox is None:
            self._tempmail_account_cache[normalized_email] = None
            return None

        ensure_by_email = getattr(mailbox, "ensure_mailbox_by_email", None)
        if not callable(ensure_by_email):
            return self._lookup_tempmail_account(normalized_email)

        try:
            account = ensure_by_email(normalized_email)
            if account is not None and getattr(account, "account_id", ""):
                self._tempmail_account_cache[normalized_email] = account
                action = str((getattr(account, "extra", None) or {}).get("mailbox_action") or "")
                if action == "created_exact_address":
                    self._log(f"[manual_email_otp] TempMail 邮箱不存在，已按原地址新建: {normalized_email}")
                else:
                    self._log(f"[manual_email_otp] 检测到 TempMail 邮箱，已绑定自动收码: {normalized_email}")
                return account
        except TempMailReadyAuthError:
            raise
        except Exception as exc:
            self._log(f"[manual_email_otp] TempMail 邮箱确保失败: {exc}")

        self._tempmail_account_cache[normalized_email] = None
        return None

    def _resolve_tempmail_context(self, email: str, *, ensure: bool = False) -> tuple[Any, MailboxAccount | None]:
        if not self._auto_tempmail_enabled():
            return None, None
        mailbox = self._build_tempmail_mailbox()
        if mailbox is None:
            return None, None
        if not self._email_matches_tempmail_domain(email):
            return None, None
        account = self._ensure_tempmail_account(email) if ensure else self._lookup_tempmail_account(email)
        return mailbox, account

    def get_email(self) -> MailboxAccount:
        if not self._email:
            raise RuntimeError("manual_email_otp 模式缺少邮箱地址")
        mailbox, account = self._resolve_tempmail_context(self._email, ensure=True)
        if mailbox is not None and account is not None and getattr(account, "account_id", ""):
            return account
        return MailboxAccount(email=self._email, account_id=self._email, extra={})

    def get_current_ids(self, account: MailboxAccount) -> set:
        email = str(getattr(account, "email", "") or self._email or "").strip()
        mailbox, resolved_account = self._resolve_tempmail_context(email, ensure=True)
        target_account = resolved_account or account
        if mailbox is not None and target_account is not None:
            account_id = str(getattr(target_account, "account_id", "") or "").strip()
            if account_id and account_id != email:
                try:
                    return set(mailbox.get_current_ids(target_account) or set())
                except Exception as exc:
                    self._log(f"[manual_email_otp] TempMail 基线读取失败: {exc}")
        # 手动邮箱模式不拉收件箱，因此没有“已有邮件 ID”概念。
        return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        email = str(getattr(account, "email", "") or self._email or "").strip()
        if not email:
            raise RuntimeError("manual_email_otp 模式缺少邮箱地址")

        task_control = getattr(self, "_task_control", None)
        phase = str(kwargs.get("phase") or "email_otp").strip() or "email_otp"
        phase_label = (
            str(kwargs.get("phase_label") or "邮箱验证码").strip() or "邮箱验证码"
        )
        timeout_seconds = max(int(timeout or 0), 1)

        deadline = time.monotonic() + timeout_seconds
        mailbox, resolved_account = self._resolve_tempmail_context(email, ensure=True)
        if mailbox is not None and resolved_account is not None and getattr(resolved_account, "account_id", ""):
            auto_timeout = timeout_seconds if task_control is None else max(1, timeout_seconds - 15)
            self._log(
                f"检测到 TempMail 邮箱，自动轮询验证码：{phase_label}（邮箱 {email}，超时 {auto_timeout}s）"
            )
            try:
                code = mailbox.wait_for_code(
                    resolved_account,
                    keyword=keyword,
                    timeout=auto_timeout,
                    before_ids=before_ids,
                    code_pattern=code_pattern,
                    **kwargs,
                )
                normalized_code = str(code or "").strip()
                meta = dict(getattr(mailbox, "_last_verification_result", None) or {})
                if meta:
                    self._last_verification_result = meta
                else:
                    self._record_verification_result(
                        message_id=f"tempmail:{phase}:{time.time_ns()}",
                        code=normalized_code,
                        phase=phase,
                        provider="manual_email_otp_tempmail",
                        metadata={
                            "email": email,
                            "phase_label": phase_label,
                            "submission_source": "tempmail_auto_poll",
                        },
                    )
                self._log(f"[验证码] 验证码已获取：{phase_label}")
                return normalized_code
            except TimeoutError:
                if task_control is None:
                    raise
                self._log(f"[验证码] TempMail 自动收码超时，回退人工输入：{phase_label}")
            except Exception as exc:
                if isinstance(exc, TempMailReadyAuthError):
                    raise
                if task_control is None:
                    raise
                self._log(f"[验证码] TempMail 自动收码失败，回退人工输入：{phase_label} ({exc})")

        if task_control is None:
            raise RuntimeError("manual_email_otp 模式未绑定任务控制器，且未命中 TempMail 自动收码")

        remaining_seconds = max(1, int(deadline - time.monotonic()))
        self._log(f"[验证码] 等待人工输入：{phase_label}（邮箱 {email}，超时 {remaining_seconds}s）")
        code = task_control.wait_for_verification_code(
            attempt_id=getattr(self, "_task_attempt_token", None),
            phase=phase,
            phase_label=phase_label,
            email=email,
            timeout_seconds=remaining_seconds,
        )
        normalized_code = str(code or "").strip()
        # 手动模式没有真实邮件 ID；为每次人工提交生成稳定的提交标识，
        # 避免“连续两次收到同一个 OTP”时被上层误判成旧验证码而直接跳过。
        self._record_verification_result(
            message_id=f"manual:{phase}:{time.time_ns()}",
            code=normalized_code,
            phase=phase,
            provider="manual_email_otp",
            metadata={
                "email": email,
                "phase_label": phase_label,
                "submission_source": "manual_input",
            },
        )
        self._log(f"[验证码] 验证码已获取：{phase_label}")
        return normalized_code


EMAIL_API_PROVIDER_VALUES = {"email_api", "api_email", "email_otp_api", "mail_api_otp"}
_GMAIL_DOMAIN_TYPOS = {"gamil.com", "gmial.com", "gmai.com"}
_EMAIL_API_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_GMAIL_EQUIVALENT_DOMAINS = {"gmail.com", "googlemail.com"}
_EMAIL_API_GMAIL_VARIANT_COUNT_MAX = 500
DEFAULT_EMAIL_API_GMAIL_VARIANT_RULES = ("dot", "plus", "dot_plus", "googlemail")
DEFAULT_EMAIL_API_GMAIL_PLUS_TAG_TEMPLATE = "r{rand}"


def _email_api_truthy(value: Any, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y", "是", "开启", "启用"}:
        return True
    if text in {"0", "false", "no", "off", "n", "否", "关闭", "禁用"}:
        return False
    return default


def _email_api_positive_float(value: Any, default: float, minimum: float = 0.5, maximum: float = 300.0) -> float:
    try:
        parsed = float(str(value).strip())
    except Exception:
        parsed = default
    return max(float(minimum), min(float(maximum), parsed))


def _email_api_positive_int(value: Any, default: int, minimum: int = 1, maximum: int = 500) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        parsed = int(default)
    return max(int(minimum), min(int(maximum), parsed))


def normalize_email_api_url(raw: Any, *, default_scheme: str = "https") -> str:
    from urllib.parse import urlsplit, urlunsplit

    text = str(raw or "").strip()
    if not text:
        raise ValueError("API URL 为空")
    scheme = str(default_scheme or "https").strip().lower() or "https"
    if scheme not in {"http", "https"}:
        scheme = "https"
    if text.startswith("//"):
        text = f"{scheme}:{text}"
    elif "://" not in text:
        text = f"{scheme}://{text}"

    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("API URL 只支持 http/https")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "", parts.query or "", ""))


def _redact_email_api_url(value: Any) -> str:
    from urllib.parse import urlsplit, urlunsplit

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        if parts.scheme and parts.netloc:
            netloc = parts.hostname or ""
            if parts.port:
                netloc = f"{netloc}:{parts.port}"
            return urlunsplit((parts.scheme, netloc, parts.path or "", "", ""))[:240]
    except Exception:
        pass
    return text[:120]


def _normalize_email_api_email(raw_email: Any) -> tuple[str, list[str]]:
    raw = str(raw_email or "").strip().lower()
    warnings: list[str] = []
    if not raw:
        raise ValueError("邮箱为空")
    if any(ch.isspace() for ch in raw):
        raise ValueError("邮箱不能包含空白字符")

    # 容错用户常见写法：xx.xxxxx.gmail.com / xx.xxxxx.gamil.com。
    if "@" not in raw:
        for suffix in (".gmail.com", ".gamil.com", ".gmial.com", ".gmai.com"):
            if raw.endswith(suffix) and len(raw) > len(suffix):
                local = raw[: -len(suffix)].strip(".")
                if local:
                    raw = f"{local}@gmail.com"
                    warnings.append(f"已将 {raw_email} 按 Gmail 地址处理为 {raw}")
                    break

    if "@" not in raw:
        raise ValueError("邮箱格式不合法")
    local, domain = raw.rsplit("@", 1)
    local = local.strip().lower()
    domain = domain.strip().lower().lstrip(".")
    if domain in _GMAIL_DOMAIN_TYPOS:
        warnings.append(f"检测到 {domain}，已按 gmail.com 处理")
        domain = "gmail.com"
    if not local or not domain:
        raise ValueError("邮箱格式不合法")
    email = f"{local}@{domain}"
    if not _EMAIL_API_EMAIL_RE.fullmatch(email):
        raise ValueError("邮箱格式不合法")
    return email, warnings


def _gmail_canonical_email(email: str) -> str:
    normalized = str(email or "").strip().lower()
    if "@" not in normalized:
        return normalized
    local, domain = normalized.rsplit("@", 1)
    if domain not in _GMAIL_EQUIVALENT_DOMAINS:
        return normalized
    if "+" in local:
        local = local.split("+", 1)[0]
    return f"{local.replace('.', '')}@gmail.com"


def _gmail_base_local(email: str) -> str:
    normalized = str(email or "").strip().lower()
    if "@" not in normalized:
        return ""
    local, domain = normalized.rsplit("@", 1)
    if domain not in _GMAIL_EQUIVALENT_DOMAINS:
        return ""
    if "+" in local:
        local = local.split("+", 1)[0]
    return local.replace(".", "")


def _parse_gmail_variant_rules(value: Any = None) -> list[str]:
    if value in (None, ""):
        return list(DEFAULT_EMAIL_API_GMAIL_VARIANT_RULES)
    if isinstance(value, (list, tuple, set)):
        raw_items = [str(item or "") for item in value]
    else:
        raw_items = re.split(r"[\s,;，；|/]+", str(value or ""))

    rules: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        item = str(raw or "").strip().lower().replace("-", "_")
        if not item:
            continue
        if item in {"all", "*", "default", "全部", "所有"}:
            expanded = list(DEFAULT_EMAIL_API_GMAIL_VARIANT_RULES)
        elif item in {"dot", "dots", "gmail_dot", "point", "点号", "点"}:
            expanded = ["dot"]
        elif item in {"plus", "tag", "plus_tag", "gmail_plus", "+", "加号"}:
            expanded = ["plus"]
        elif item in {"mixed", "mix", "dot_plus", "dotplus", "dot+plus", "plus_dot", "gmail_dot_plus", "混合"}:
            expanded = ["dot_plus"]
        elif item in {"googlemail", "google_mail", "googlemail_domain", "domain", "域名"}:
            expanded = ["googlemail"]
        else:
            continue
        for rule in expanded:
            if rule not in seen:
                seen.add(rule)
                rules.append(rule)
    return rules or list(DEFAULT_EMAIL_API_GMAIL_VARIANT_RULES)


def _gmail_variant_rng(seed: Any = None):
    text = str(seed or "").strip()
    if text:
        return random.Random(text)
    return random.SystemRandom()


def _gmail_random_token(rng, length: int = 6) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(rng.choice(alphabet) for _ in range(max(1, int(length or 1))))


def _gmail_plus_tag(template: Any, *, index: int, base: str, rng) -> str:
    tpl = str(template or DEFAULT_EMAIL_API_GMAIL_PLUS_TAG_TEMPLATE).strip() or DEFAULT_EMAIL_API_GMAIL_PLUS_TAG_TEMPLATE
    rand = _gmail_random_token(rng, 6)
    dynamic_markers = ("{index}", "{n}", "{rand}", "{hex}")
    try:
        tag = tpl.format(index=index, n=index, base=base, rand=rand, hex=rand)
    except Exception:
        tag = f"{tpl}{index}"
    tag = re.sub(r"[^a-zA-Z0-9._-]+", "", str(tag or "").lower()).strip("._-")
    if not any(marker in tpl for marker in dynamic_markers):
        tag = f"{tag}{index}"
    return tag or f"r{index}"


def _gmail_local_from_dot_mask(compact: str, mask: int) -> str:
    if len(compact) < 2:
        return compact
    chars: list[str] = []
    for idx, ch in enumerate(compact):
        chars.append(ch)
        if idx < len(compact) - 1 and (mask & (1 << idx)):
            chars.append(".")
    return "".join(chars)


def _gmail_random_dotted_local(compact: str, rng) -> str:
    if len(compact) < 2:
        return compact
    # 每个字符间隙随机放点；mask=0 是无点原形，也是一种合法 dot 等价写法。
    mask = 0
    for idx in range(len(compact) - 1):
        if rng.choice((False, True)):
            mask |= 1 << idx
    return _gmail_local_from_dot_mask(compact, mask)


def _gmail_iter_dot_locals(compact: str, *, limit: int = 1024) -> list[str]:
    compact = str(compact or "").strip().lower()
    if not compact:
        return []
    if len(compact) < 2:
        return [compact]
    max_masks = 1 << min(len(compact) - 1, 20)
    locals_: list[str] = []
    seen: set[str] = set()

    def add(mask: int) -> None:
        if len(locals_) >= limit:
            return
        local = _gmail_local_from_dot_mask(compact, mask)
        if local not in seen:
            seen.add(local)
            locals_.append(local)

    # 先给可读性较好的单点位置，再补全多点组合。
    preferred = 2 if len(compact) > 2 else 1
    for pos in [preferred] + [pos for pos in range(1, len(compact)) if pos != preferred]:
        add(1 << (pos - 1))
    add(0)
    for mask in range(1, max_masks):
        add(mask)
        if len(locals_) >= limit:
            break
    return locals_


def build_gmail_dot_variant(email: Any) -> str:
    normalized = str(email or "").strip().lower()
    if "@" not in normalized:
        return ""
    local, domain = normalized.rsplit("@", 1)
    if domain != "gmail.com":
        return ""
    tag = ""
    base_local = local
    if "+" in base_local:
        base_local, suffix = base_local.split("+", 1)
        tag = "+" + suffix
    compact = base_local.replace(".", "")
    if len(compact) < 2:
        return ""

    candidates: list[str] = []
    if "." in base_local:
        candidates.append(compact + tag)
    preferred = 2 if len(compact) > 2 else 1
    positions = [preferred] + [pos for pos in range(1, len(compact)) if pos != preferred]
    for pos in positions:
        candidates.append(compact[:pos] + "." + compact[pos:] + tag)

    for local_candidate in candidates:
        candidate = f"{local_candidate}@gmail.com".lower()
        if candidate != normalized:
            return candidate
    return ""


def build_gmail_variants(
    email: Any,
    *,
    count: Any = 2,
    rules: Any = None,
    plus_tag_template: Any = DEFAULT_EMAIL_API_GMAIL_PLUS_TAG_TEMPLATE,
    include_original: bool = True,
    random_seed: Any = None,
) -> list[dict[str, str]]:
    """Build one original Gmail identity plus random Gmail-equivalent variants.

    Supported default rules are the public Gmail equivalences/operators we rely on:
    dot aliases, plus tags, dot+plus mixed aliases, and the googlemail.com domain
    equivalent.  The caller decides ``count`` as total identities per source Gmail
    row; when not enough finite dot/googlemail-only variants exist, fewer rows are
    returned instead of fabricating non-Gmail identities.
    """

    normalized = str(email or "").strip().lower()
    if "@" not in normalized:
        return []
    local, domain = normalized.rsplit("@", 1)
    if domain not in _GMAIL_EQUIVALENT_DOMAINS:
        return [{"email": normalized, "variant": "original"}] if include_original and normalized else []
    compact = _gmail_base_local(normalized)
    if not compact:
        return [{"email": normalized, "variant": "original"}] if include_original and normalized else []

    desired = _email_api_positive_int(
        count,
        2,
        minimum=1,
        maximum=_EMAIL_API_GMAIL_VARIANT_COUNT_MAX,
    )
    enabled_rules = _parse_gmail_variant_rules(rules)
    rng = _gmail_variant_rng(random_seed)
    results: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(candidate_local: str, candidate_domain: str, variant: str) -> bool:
        candidate_local = str(candidate_local or "").strip(".").lower()
        candidate_domain = str(candidate_domain or "gmail.com").strip().lower()
        if not candidate_local or candidate_domain not in _GMAIL_EQUIVALENT_DOMAINS:
            return False
        candidate = f"{candidate_local}@{candidate_domain}"
        if candidate in seen:
            return False
        seen.add(candidate)
        results.append({"email": candidate, "variant": variant})
        return True

    if include_original:
        add(local, domain, "original")
    if len(results) >= desired:
        return results[:desired]
    if not enabled_rules:
        return results[:desired]

    plus_index = 1
    attempts = max(200, desired * 120)
    for _ in range(attempts):
        if len(results) >= desired:
            break
        rule = rng.choice(enabled_rules)
        if rule == "dot":
            add(_gmail_random_dotted_local(compact, rng), "gmail.com", "gmail_dot")
            continue
        if rule == "plus":
            tag = _gmail_plus_tag(plus_tag_template, index=plus_index, base=compact, rng=rng)
            plus_index += 1
            add(f"{compact}+{tag}", "gmail.com", "gmail_plus")
            continue
        if rule == "dot_plus":
            tag = _gmail_plus_tag(plus_tag_template, index=plus_index, base=compact, rng=rng)
            plus_index += 1
            add(f"{_gmail_random_dotted_local(compact, rng)}+{tag}", "gmail.com", "gmail_dot_plus")
            continue
        if rule == "googlemail":
            subrule = rng.choice(("base", "dot", "plus", "dot_plus"))
            if subrule == "base":
                add(compact, "googlemail.com", "googlemail")
            elif subrule == "dot":
                add(_gmail_random_dotted_local(compact, rng), "googlemail.com", "googlemail_dot")
            elif subrule == "plus":
                tag = _gmail_plus_tag(plus_tag_template, index=plus_index, base=compact, rng=rng)
                plus_index += 1
                add(f"{compact}+{tag}", "googlemail.com", "googlemail_plus")
            else:
                tag = _gmail_plus_tag(plus_tag_template, index=plus_index, base=compact, rng=rng)
                plus_index += 1
                add(f"{_gmail_random_dotted_local(compact, rng)}+{tag}", "googlemail.com", "googlemail_dot_plus")

    if len(results) >= desired:
        return results[:desired]

    # Deterministic fallback fills gaps left by random duplicate hits and gives
    # dot-only configurations every finite dot combination before stopping.
    dot_locals = _gmail_iter_dot_locals(compact, limit=max(desired * 4, 64))
    if "dot" in enabled_rules:
        for dotted in dot_locals:
            if len(results) >= desired:
                break
            add(dotted, "gmail.com", "gmail_dot")
    if "plus" in enabled_rules:
        while len(results) < desired:
            tag = _gmail_plus_tag(plus_tag_template, index=plus_index, base=compact, rng=rng)
            plus_index += 1
            add(f"{compact}+{tag}", "gmail.com", "gmail_plus")
    if "dot_plus" in enabled_rules:
        tag_round = 0
        while len(results) < desired:
            tag_round += 1
            tag = _gmail_plus_tag(plus_tag_template, index=plus_index, base=compact, rng=rng)
            plus_index += 1
            for dotted in dot_locals:
                if len(results) >= desired:
                    break
                add(f"{dotted}+{tag}", "gmail.com", "gmail_dot_plus")
            if tag_round > desired + 5:
                break
    if "googlemail" in enabled_rules:
        for dotted in dot_locals:
            if len(results) >= desired:
                break
            add(dotted, "googlemail.com", "googlemail_dot" if "." in dotted else "googlemail")
        while len(results) < desired:
            tag = _gmail_plus_tag(plus_tag_template, index=plus_index, base=compact, rng=rng)
            plus_index += 1
            add(f"{compact}+{tag}", "googlemail.com", "googlemail_plus")
    return results[:desired]


def parse_email_api_lines(
    lines: Any,
    *,
    gmail_dot_variant_enabled: bool = True,
    gmail_variant_count: Any = 2,
    gmail_variant_rules: Any = None,
    gmail_plus_tag_template: Any = DEFAULT_EMAIL_API_GMAIL_PLUS_TAG_TEMPLATE,
    gmail_variant_random_seed: Any = None,
    default_scheme: str = "https",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse ``email----api`` rows into registration identities.

    Gmail rows expand to the exact submitted address plus random Gmail-equivalent
    variants.  The exact ChatGPT account email is never canonicalized; the Gmail
    canonical address is only used for duplicate and lock detection.
    """

    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen_emails: set[str] = set()
    gmail_api_by_root: dict[str, str] = {}
    emitted_gmail_roots: set[str] = set()

    if isinstance(lines, (list, tuple, set)):
        raw_lines = [str(item or "") for item in lines]
    else:
        raw_lines = str(lines or "").splitlines()

    for line_no, raw in enumerate(raw_lines, start=1):
        line = str(raw or "").strip()
        if not line:
            continue
        parts = re.split(r"-{4,}", line, maxsplit=1)
        if len(parts) != 2:
            errors.append({"line": line_no, "raw": line, "reason": "缺少 ---- 分隔符"})
            continue
        raw_email, raw_api = parts[0].strip(), parts[1].strip()
        try:
            email, warnings = _normalize_email_api_email(raw_email)
            api_url = normalize_email_api_url(raw_api, default_scheme=default_scheme)
        except ValueError as exc:
            errors.append({"line": line_no, "raw": line, "reason": str(exc)})
            continue

        is_gmail = bool(_gmail_base_local(email))
        gmail_root = _gmail_canonical_email(email) if is_gmail else ""
        if gmail_root:
            existing_api = gmail_api_by_root.get(gmail_root)
            if existing_api and existing_api != api_url:
                errors.append({"line": line_no, "raw": line, "reason": "同一个 Gmail 根邮箱绑定了不同 API"})
                continue
            gmail_api_by_root[gmail_root] = api_url
            if gmail_root in emitted_gmail_roots:
                # 同一个 Gmail 根邮箱最多展开一次：原地址 + N-1 个随机变体。
                continue
            emitted_gmail_roots.add(gmail_root)

        def add(candidate_email: str, variant: str) -> None:
            normalized_candidate = candidate_email.strip().lower()
            if not normalized_candidate or normalized_candidate in seen_emails:
                return
            seen_emails.add(normalized_candidate)
            lock_keys = {f"api:{api_url}"}
            if gmail_root:
                lock_keys.add(f"gmail:{gmail_root}")
            candidates.append(
                {
                    "email": normalized_candidate,
                    "source_email": email,
                    "gmail_root": gmail_root,
                    "api_url": api_url,
                    "api_url_masked": _redact_email_api_url(api_url),
                    "variant": variant,
                    "line": line_no,
                    "warnings": list(warnings),
                    "lock_keys": sorted(lock_keys),
                }
            )

        if is_gmail:
            variant_enabled = _email_api_truthy(gmail_dot_variant_enabled, default=True)
            identity_count = (
                _email_api_positive_int(
                    gmail_variant_count,
                    2,
                    minimum=1,
                    maximum=_EMAIL_API_GMAIL_VARIANT_COUNT_MAX,
                )
                if variant_enabled
                else 1
            )
            seed = f"{gmail_variant_random_seed}:{line_no}:{email}:{api_url}" if gmail_variant_random_seed not in (None, "") else None
            for variant_item in build_gmail_variants(
                email,
                count=identity_count,
                rules=gmail_variant_rules,
                plus_tag_template=gmail_plus_tag_template,
                include_original=True,
                random_seed=seed,
            ):
                add(str(variant_item.get("email") or ""), str(variant_item.get("variant") or "gmail_variant"))
        else:
            add(email, "original")
    return candidates, errors


@dataclass
class EmailApiPool:
    candidates: list[dict[str, Any]]
    statuses: list[str] = field(default_factory=list)
    active_locks: set[str] = field(default_factory=set)
    condition: threading.Condition = field(default_factory=lambda: threading.Condition(threading.Lock()))

    def __post_init__(self) -> None:
        if not self.statuses or len(self.statuses) != len(self.candidates):
            self.statuses = ["available" for _ in self.candidates]

    def acquire(self, *, wait_timeout: float = 1800.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(float(wait_timeout or 0), 1.0)
        with self.condition:
            while True:
                for idx, item in enumerate(self.candidates):
                    if self.statuses[idx] != "available":
                        continue
                    lock_keys = {str(key) for key in (item.get("lock_keys") or []) if str(key)}
                    if lock_keys & self.active_locks:
                        continue
                    self.statuses[idx] = "leased"
                    self.active_locks.update(lock_keys)
                    allocated = dict(item)
                    allocated["_pool_index"] = idx
                    allocated["_lock_keys"] = sorted(lock_keys)
                    return allocated

                if not any(status == "available" for status in self.statuses):
                    raise RuntimeError("Email API 邮箱行已用完")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("Email API 邮箱等待同 API/Gmail 串行锁超时")
                self.condition.wait(min(0.25, remaining))

    def finalize(self, item: dict[str, Any], status: str) -> None:
        idx = item.get("_pool_index")
        lock_keys = {str(key) for key in (item.get("_lock_keys") or item.get("lock_keys") or []) if str(key)}
        with self.condition:
            try:
                idx_int = int(idx)
            except Exception:
                idx_int = -1
            if 0 <= idx_int < len(self.statuses) and self.statuses[idx_int] == "leased":
                self.statuses[idx_int] = status or "failed"
            for key in lock_keys:
                self.active_locks.discard(key)
            self.condition.notify_all()


class EmailApiMailbox(BaseMailbox):
    """邮箱验证码 API：固定邮箱 + GET JSON status 字段轮询验证码。"""

    _pools: dict[str, EmailApiPool] = {}
    _pools_lock = threading.Lock()

    def __init__(
        self,
        lines: Any = "",
        candidates: list[dict[str, Any]] | None = None,
        api_url: str = "",
        email: str = "",
        poll_interval_seconds: Any = 3,
        request_timeout_seconds: Any = 15,
        gmail_dot_variant_enabled: Any = True,
        gmail_variant_count: Any = 2,
        gmail_variant_rules: Any = None,
        gmail_plus_tag_template: Any = DEFAULT_EMAIL_API_GMAIL_PLUS_TAG_TEMPLATE,
        gmail_variant_random_seed: Any = None,
        default_scheme: str = "https",
        pool_key: str = "",
        proxy: str | None = None,
    ):
        self.raw_lines = lines
        self.candidates = candidates if isinstance(candidates, list) else None
        self.api_url = str(api_url or "").strip()
        self.email = str(email or "").strip()
        self.poll_interval_seconds = _email_api_positive_float(poll_interval_seconds, 3, minimum=0.5, maximum=60)
        self.request_timeout_seconds = _email_api_positive_float(request_timeout_seconds, 15, minimum=1, maximum=120)
        self.gmail_dot_variant_enabled = _email_api_truthy(gmail_dot_variant_enabled, default=True)
        self.gmail_variant_count = _email_api_positive_int(
            gmail_variant_count,
            2,
            minimum=1,
            maximum=_EMAIL_API_GMAIL_VARIANT_COUNT_MAX,
        )
        self.gmail_variant_rules = str(gmail_variant_rules or "all").strip() or "all"
        self.gmail_plus_tag_template = str(gmail_plus_tag_template or DEFAULT_EMAIL_API_GMAIL_PLUS_TAG_TEMPLATE).strip() or DEFAULT_EMAIL_API_GMAIL_PLUS_TAG_TEMPLATE
        self.gmail_variant_random_seed = str(gmail_variant_random_seed or "").strip()
        self.default_scheme = str(default_scheme or "https").strip().lower() or "https"
        self.pool_key = str(pool_key or "").strip()
        self.proxy = build_requests_proxy_config(proxy)
        self._allocated_item: dict[str, Any] | None = None
        self._pool: EmailApiPool | None = None
        self._pool_lookup_key = ""
        self._last_poll_error = ""

    @classmethod
    def release_pool(cls, pool_key: str) -> None:
        key = str(pool_key or "").strip()
        if not key:
            return
        with cls._pools_lock:
            cls._pools.pop(key, None)

    @staticmethod
    def code_from_status(value: Any) -> str:
        raw = "" if value is None else str(value).strip()
        if not raw or raw.lower() in {"false", "none", "null"}:
            return ""
        if re.fullmatch(r"0+", raw):
            return ""
        return raw if re.fullmatch(r"\d{4,8}", raw) else ""

    @classmethod
    def codes_from_payload(cls, payload: Any) -> list[str]:
        """Extract candidate OTP codes from common email API response shapes.

        The originally documented contract used ``status`` as the code field,
        but smsbower's live response currently returns ``status=1`` with
        ``code``/``all_codes`` carrying the actual OTP once available.  Keep
        supporting both shapes and de-duplicate while preserving priority.
        """

        codes: list[str] = []

        def add(value: Any) -> None:
            code = cls.code_from_status(value)
            if code and code not in codes:
                codes.append(code)

        if isinstance(payload, dict):
            add(payload.get("status"))
            for key in (
                "code",
                "otp",
                "verification_code",
                "verificationCode",
                "email_code",
                "emailCode",
            ):
                add(payload.get(key))
            all_codes = payload.get("all_codes")
            if all_codes is None:
                all_codes = payload.get("allCodes")
            if isinstance(all_codes, (list, tuple, set)):
                for item in reversed(list(all_codes)):
                    add(item)
            else:
                add(all_codes)
            data = payload.get("data")
            if isinstance(data, dict):
                for code in cls.codes_from_payload(data):
                    if code not in codes:
                        codes.append(code)
        else:
            add(payload)
        return codes

    def _normalized_candidates(self) -> list[dict[str, Any]]:
        if isinstance(self.candidates, list) and self.candidates:
            return [dict(item) for item in self.candidates if isinstance(item, dict)]
        candidates, errors = parse_email_api_lines(
            self.raw_lines,
            gmail_dot_variant_enabled=self.gmail_dot_variant_enabled,
            gmail_variant_count=self.gmail_variant_count,
            gmail_variant_rules=self.gmail_variant_rules,
            gmail_plus_tag_template=self.gmail_plus_tag_template,
            gmail_variant_random_seed=self.gmail_variant_random_seed,
            default_scheme=self.default_scheme,
        )
        if errors:
            first = errors[0]
            raise RuntimeError(f"Email API 行解析失败: 第 {first.get('line')} 行 {first.get('reason')}")
        return candidates

    def _resolve_pool(self) -> EmailApiPool:
        if self._pool is not None:
            return self._pool
        candidates = self._normalized_candidates()
        if not candidates:
            raise RuntimeError("Email API 模式请至少提供一条 email----api")
        key = self.pool_key
        if not key:
            digest_source = json.dumps(
                [{"email": item.get("email"), "api_url": item.get("api_url")} for item in candidates],
                ensure_ascii=False,
                sort_keys=True,
            )
            key = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
        with self._pools_lock:
            pool = self._pools.get(key)
            if pool is None:
                pool = EmailApiPool(candidates=[dict(item) for item in candidates])
                self._pools[key] = pool
        self._pool_lookup_key = key
        self._pool = pool
        return pool

    def _account_api_url(self, account: MailboxAccount | None = None) -> str:
        extra = dict(getattr(account, "extra", None) or {}) if account is not None else {}
        raw = extra.get("api_url") or self.api_url
        if not raw:
            raise RuntimeError("Email API 邮箱状态缺少 api_url")
        return normalize_email_api_url(raw, default_scheme=self.default_scheme)

    def _request_status_payload(self, api_url: str) -> dict[str, Any]:
        import requests

        response = requests.request(
            "GET",
            api_url,
            headers={"accept": "application/json"},
            timeout=self.request_timeout_seconds,
            proxies=self.proxy,
        )
        raw_text = response.text or ""
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(
                f"Email API 返回不是 JSON: HTTP {response.status_code} {_redact_email_api_url(api_url)} {raw_text[:120]}"
            ) from exc
        if response.status_code >= 400:
            message = ""
            if isinstance(payload, dict):
                message = str(payload.get("error") or payload.get("message") or payload.get("detail") or "").strip()
            raise RuntimeError(f"Email API 请求失败: HTTP {response.status_code} {message or _redact_email_api_url(api_url)}")
        if not isinstance(payload, dict):
            raise RuntimeError("Email API 返回不是 JSON Object")
        return payload

    def _poll_status_code(self, account: MailboxAccount, *, swallow_errors: bool = False) -> str:
        api_url = self._account_api_url(account)
        try:
            payload = self._request_status_payload(api_url)
        except Exception as exc:
            if swallow_errors:
                return ""
            raise RuntimeError(str(exc)) from exc
        codes = self.codes_from_payload(payload)
        return codes[0] if codes else ""

    def get_email(self) -> MailboxAccount:
        if self.email and self.api_url:
            email, warnings = _normalize_email_api_email(self.email)
            api_url = normalize_email_api_url(self.api_url, default_scheme=self.default_scheme)
            extra = {
                "provider": "email_api",
                "api_url": api_url,
                "api_url_masked": _redact_email_api_url(api_url),
                "source_email": email,
                "gmail_root": _gmail_canonical_email(email) if email.endswith("@gmail.com") else "",
                "variant": "restored",
                "warnings": warnings,
                "mailbox_action": "restored_existing",
            }
            return MailboxAccount(email=email, account_id=email, extra=extra)

        pool = self._resolve_pool()
        item = pool.acquire()
        self._allocated_item = item
        email = str(item.get("email") or "").strip()
        api_url = str(item.get("api_url") or "").strip()
        self._log(
            f"[EmailAPI] 分配邮箱: {email} variant={item.get('variant') or 'original'} api={_redact_email_api_url(api_url)}"
        )
        extra = {
            "provider": "email_api",
            "api_url": api_url,
            "api_url_masked": _redact_email_api_url(api_url),
            "source_email": item.get("source_email") or email,
            "gmail_root": item.get("gmail_root") or "",
            "variant": item.get("variant") or "original",
            "line": item.get("line") or 0,
            "warnings": item.get("warnings") or [],
            "lock_keys": item.get("_lock_keys") or item.get("lock_keys") or [],
            "pool_key": self._pool_lookup_key or self.pool_key,
            "pool_index": item.get("_pool_index"),
            "mailbox_action": "leased",
        }
        return MailboxAccount(email=email, account_id=email, extra=extra)

    def get_current_ids(self, account: MailboxAccount) -> set:
        api_url = self._account_api_url(account)
        try:
            payload = self._request_status_payload(api_url)
        except Exception:
            return set()
        return {f"status:{code}" for code in self.codes_from_payload(payload)}

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        seen = {str(mid) for mid in (before_ids or set()) if str(mid)}
        exclude_codes = {str(code).strip() for code in (kwargs.get("exclude_codes") or set()) if str(code or "").strip()}
        api_url = self._account_api_url(account)

        def poll_once() -> Optional[str]:
            try:
                payload = self._request_status_payload(api_url)
                self._last_poll_error = ""
            except Exception as exc:
                error_text = str(exc or "").strip()
                if error_text and error_text != self._last_poll_error:
                    self._last_poll_error = error_text
                    self._log(f"[EmailAPI] 收码接口暂未可用，继续轮询: {error_text[:240]}")
                return None

            codes = self.codes_from_payload(payload)
            if not codes:
                return None
            code = ""
            message_id = ""
            for candidate in codes:
                candidate_message_id = f"status:{candidate}"
                if candidate_message_id in seen or candidate in exclude_codes:
                    continue
                code = candidate
                message_id = candidate_message_id
                break
            if not code or not message_id:
                return None
            seen.add(message_id)
            self._record_verification_result(
                message_id=message_id,
                code=code,
                phase=kwargs.get("phase") or "",
                provider="EmailApiMailbox",
                metadata={
                    "email": str(getattr(account, "email", "") or ""),
                    "source_email": str((getattr(account, "extra", None) or {}).get("source_email") or ""),
                    "gmail_root": str((getattr(account, "extra", None) or {}).get("gmail_root") or ""),
                    "variant": str((getattr(account, "extra", None) or {}).get("variant") or ""),
                    "api_url": _redact_email_api_url(api_url),
                    "submission_source": "email_api_status",
                },
            )
            self._log(f"[EmailAPI] 收到验证码: {code}")
            return code

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=self.poll_interval_seconds,
            poll_once=poll_once,
            timeout_message=f"等待 Email API 验证码超时 ({max(int(timeout or 0), 1)}s)",
        )

    def _finalize_pool_item(self, status: str) -> None:
        if self._pool is None or not self._allocated_item:
            return
        self._pool.finalize(self._allocated_item, status)
        self._allocated_item = None

    def finalize_success(self, account: MailboxAccount, registered_email: str = "", task_id: str = "") -> None:
        self._finalize_pool_item("registered")

    def finalize_failure(self, account: MailboxAccount, error_message: str = "", task_id: str = "") -> None:
        self._finalize_pool_item("failed")

    def export_state_config(self, account: MailboxAccount | None = None, extra_config: dict | None = None) -> dict[str, Any]:
        return {
            "mail_provider": "email_api",
            "email_api_poll_interval_seconds": self.poll_interval_seconds,
            "email_api_request_timeout_seconds": self.request_timeout_seconds,
            "email_api_gmail_dot_variant_enabled": self.gmail_dot_variant_enabled,
            "email_api_gmail_variant_count": self.gmail_variant_count,
            "email_api_gmail_variant_rules": self.gmail_variant_rules,
            "email_api_gmail_plus_tag_template": self.gmail_plus_tag_template,
            "email_api_default_scheme": self.default_scheme,
        }


def _mailbox_bool(value, *, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y", "是", "开启", "启用"}:
        return True
    if text in {"0", "false", "no", "off", "n", "否", "关闭", "禁用"}:
        return False
    return default


def _mailbox_api_proxy(extra: dict, task_proxy: str | None = None, *, prefix: str = "") -> str | None:
    """Return an explicit mailbox-provider API proxy.

    Registration proxies are for ChatGPT/OpenAI traffic.  Mail-provider control
    planes such as TempMail Ready are normally host-local/admin APIs and should
    not inherit a random ChatGPT exit proxy unless explicitly requested.
    """
    keys = []
    if prefix:
        keys.extend((f"{prefix}_proxy", f"{prefix}_api_proxy"))
    keys.extend(("mailbox_proxy", "email_proxy", "mail_api_proxy"))
    for key in keys:
        value = str((extra or {}).get(key) or "").strip()
        if value:
            return value
    if _mailbox_bool((extra or {}).get("mailbox_use_task_proxy"), default=False):
        return task_proxy
    if prefix and _mailbox_bool((extra or {}).get(f"{prefix}_use_task_proxy"), default=False):
        return task_proxy
    return None


def create_mailbox(
    provider: str, extra: dict = None, proxy: str = None
) -> "BaseMailbox":
    """工厂方法：根据 provider 创建对应的 mailbox 实例"""
    extra = extra or {}
    provider = str(provider or "").strip().lower()
    tempmail_proxy = _mailbox_api_proxy(extra, proxy, prefix="tempmail")
    if provider == "manual_email_otp":
        return ManualEmailOtpMailbox(
            email=extra.get("manual_email_address") or extra.get("email") or "",
            extra=extra,
            proxy=proxy,
        )
    elif provider in EMAIL_API_PROVIDER_VALUES:
        email_api_proxy = _mailbox_api_proxy(extra, proxy, prefix="email_api")
        return EmailApiMailbox(
            lines=extra.get("email_api_lines") or extra.get("email_api_accounts") or "",
            candidates=extra.get("email_api_candidates") if isinstance(extra.get("email_api_candidates"), list) else None,
            api_url=extra.get("email_api_url") or extra.get("api_url") or "",
            email=extra.get("email_api_email") or extra.get("manual_email_address") or extra.get("email") or "",
            poll_interval_seconds=extra.get("email_api_poll_interval_seconds", 3),
            request_timeout_seconds=extra.get("email_api_request_timeout_seconds", 15),
            gmail_dot_variant_enabled=extra.get("email_api_gmail_dot_variant_enabled", True),
            gmail_variant_count=extra.get("email_api_gmail_variant_count", 2),
            gmail_variant_rules=extra.get("email_api_gmail_variant_rules", "all"),
            gmail_plus_tag_template=extra.get("email_api_gmail_plus_tag_template", DEFAULT_EMAIL_API_GMAIL_PLUS_TAG_TEMPLATE),
            gmail_variant_random_seed=extra.get("email_api_gmail_variant_random_seed", ""),
            default_scheme=extra.get("email_api_default_scheme", "https"),
            pool_key=extra.get("email_api_pool_key") or extra.get("_current_task_id") or "",
            proxy=email_api_proxy,
        )
    elif provider == "tempmail_lol":
        return TempMailLolMailbox(proxy=proxy)
    elif provider in ("tempmail_local", "tempmail_api"):
        return TempMailLocalMailbox(
            api_url=extra.get("tempmail_api_url", ""),
            api_key=extra.get("tempmail_api_key", ""),
            api_key_header=extra.get("tempmail_api_key_header", "Authorization"),
            primary_domain=extra.get("tempmail_primary_domain", ""),
            primary_domains=extra.get("tempmail_fixed_domains", ""),
            mode=extra.get("tempmail_mode", "fixed_domain"),
            wait_timeout_seconds=extra.get("tempmail_wait_timeout_seconds", 180),
            ttl_minutes=extra.get("tempmail_ttl_minutes", 30),
            reuse_window_minutes=extra.get("tempmail_reuse_window_minutes", 20),
            permanent=extra.get("tempmail_permanent", False),
            platform=extra.get("tempmail_platform", "chatgpt"),
            proxy=tempmail_proxy,
        )
    elif provider in ("hme_ready_api", "icloud_hme", "icloud_hme_ready", "icloud_hme_helper_ready", "helper_ready_api"):
        # HME Ready is the only supported HME provider.  The legacy provider
        # names are accepted only while reading historical account state and
        # are normalized into this helper-backed implementation.
        return HmeReadyMailbox(
            mail_provider_name="hme_ready_api",
            icloud_hme_mode="helper_ready_api",
            icloud_cookie="",
            icloud_domain_base="icloud.com",
            icloud_forward_to=extra.get("icloud_forward_to", "b@cccy.me"),
            # A physical TempMail id is a refreshable per-account cache, not a
            # global HME Ready setting.  Account metadata is handled by the
            # polling path below.
            icloud_forward_mailbox_id="",
            icloud_hme_helper_api_url=extra.get("icloud_hme_helper_api_url", ""),
            icloud_hme_helper_internal_key=(
                extra.get("icloud_hme_helper_internal_key")
                or extra.get("icloud_hme_helper_api_key")
                or ""
            ),
            icloud_hme_helper_api_key_header=extra.get(
                "icloud_hme_helper_api_key_header",
                extra.get("icloud_hme_helper_header", "X-Internal-Key"),
            ),
            icloud_hme_helper_consumer=extra.get("icloud_hme_helper_consumer", "auto-gpt/chatgpt_register"),
            icloud_hme_helper_checkout_ttl_seconds=extra.get("icloud_hme_helper_checkout_ttl_seconds", ""),
            icloud_hme_helper_wait_timeout_seconds=extra.get("icloud_hme_helper_wait_timeout_seconds", ""),
            icloud_hme_helper_max_cache_age_seconds=extra.get("icloud_hme_helper_max_cache_age_seconds", ""),
            icloud_hme_test_mode=extra.get("icloud_hme_test_mode", False),
            icloud_hme_test_tag=extra.get("icloud_hme_test_tag", ""),
            icloud_hme_test_tag_scheme=extra.get("icloud_hme_test_tag_scheme", ""),
            icloud_hme_test_physical_alias_id=extra.get("icloud_hme_test_physical_alias_id", ""),
            icloud_hme_test_run_id=extra.get("icloud_hme_test_run_id", ""),
            tempmail_api_url=extra.get("tempmail_api_url", ""),
            tempmail_api_key=extra.get("tempmail_api_key", ""),
            tempmail_api_key_header=extra.get("tempmail_api_key_header", "Authorization"),
            wait_timeout_seconds=extra.get("tempmail_wait_timeout_seconds", 300),
            tempmail_proxy=tempmail_proxy,
            proxy=proxy,
        )
    elif provider == "skymail":
        return SkyMailMailbox(
            api_base=extra.get("skymail_api_base", "https://api.skymail.ink"),
            auth_token=extra.get("skymail_token", ""),
            domain=extra.get("skymail_domain", ""),
            proxy=proxy,
        )
    elif provider == "cloudmail":
        timeout_raw = extra.get("cloudmail_timeout", extra.get("timeout", 30))
        try:
            timeout_value = int(timeout_raw)
        except (TypeError, ValueError):
            timeout_value = 30
        return CloudMailMailbox(
            api_base=extra.get("cloudmail_api_base")
            or extra.get("base_url")
            or "",
            admin_email=extra.get("cloudmail_admin_email")
            or extra.get("admin_email")
            or "",
            admin_password=extra.get("cloudmail_admin_password")
            or extra.get("admin_password")
            or extra.get("api_key")
            or "",
            domain=extra.get("cloudmail_domain") or extra.get("domain") or "",
            subdomain=extra.get("cloudmail_subdomain")
            or extra.get("subdomain")
            or "",
            timeout=timeout_value,
            proxy=proxy,
        )
    elif provider == "duckmail":
        return DuckMailMailbox(
            api_url=(extra.get("duckmail_api_url") or "https://www.duckmail.sbs"),
            provider_url=(
                extra.get("duckmail_provider_url") or "https://api.duckmail.sbs"
            ),
            bearer=(extra.get("duckmail_bearer") or "kevin273945"),
            domain=extra.get("duckmail_domain", ""),
            api_key=extra.get("duckmail_api_key", ""),
            proxy=proxy,
        )
    elif provider == "freemail":
        return FreemailMailbox(
            api_url=extra.get("freemail_api_url", ""),
            admin_token=extra.get("freemail_admin_token", ""),
            username=extra.get("freemail_username", ""),
            password=extra.get("freemail_password", ""),
            domain=extra.get("freemail_domain", ""),
            proxy=proxy,
        )
    elif provider == "moemail":
        return MoeMailMailbox(
            api_url=extra.get("moemail_api_url", "https://sall.cc"),
            api_key=extra.get("moemail_api_key", ""),
            proxy=proxy,
        )
    elif provider == "maliapi":
        return MaliAPIMailbox(
            api_url=extra.get("maliapi_base_url", "https://maliapi.215.im/v1"),
            api_key=extra.get("maliapi_api_key", ""),
            domain=extra.get("maliapi_domain", ""),
            auto_domain_strategy=extra.get("maliapi_auto_domain_strategy", ""),
            proxy=proxy,
        )
    elif provider == "gptmail":
        return GPTMailMailbox(
            api_url=extra.get("gptmail_base_url", "https://mail.chatgpt.org.uk"),
            api_key=extra.get("gptmail_api_key", ""),
            domain=extra.get("gptmail_domain", ""),
            proxy=proxy,
        )
    elif provider == "applemail":
        return AppleMailMailbox(
            api_url=extra.get("applemail_base_url", "https://www.appleemail.top"),
            pool_file=extra.get("applemail_pool_file", ""),
            pool_dir=extra.get("applemail_pool_dir", "mail"),
            mailboxes=extra.get("applemail_mailboxes", "INBOX,Junk"),
            proxy=proxy,
        )
    elif provider == "opentrashmail":
        return OpenTrashMailMailbox(
            api_url=extra.get("opentrashmail_api_url", ""),
            domain=extra.get("opentrashmail_domain", ""),
            password=extra.get("opentrashmail_password", ""),
            proxy=proxy,
        )
    elif provider == "cfworker":
        return CFWorkerMailbox(
            api_url=extra.get("cfworker_api_url", ""),
            admin_token=extra.get("cfworker_admin_token", ""),
            domain=extra.get("cfworker_domain", ""),
            domain_override=extra.get("cfworker_domain_override", ""),
            domains=extra.get("cfworker_domains", ""),
            enabled_domains=extra.get("cfworker_enabled_domains", ""),
            subdomain=extra.get("cfworker_subdomain", ""),
            random_subdomain=extra.get("cfworker_random_subdomain", False),
            fingerprint=extra.get("cfworker_fingerprint", ""),
            custom_auth=extra.get("cfworker_custom_auth", ""),
            proxy=proxy,
        )
    elif provider == "luckmail":
        return LuckMailMailbox(
            base_url=extra.get("luckmail_base_url") or "https://mails.luckyous.com/",
            api_key=extra.get("luckmail_api_key", ""),
            project_code=extra.get("luckmail_project_code", ""),
            email_type=extra.get("luckmail_email_type", ""),
            domain=extra.get("luckmail_domain", ""),
            proxy=proxy,
        )
    elif provider == "outlook":
        return OutlookMailbox(
            imap_server=extra.get("outlook_imap_server", ""),
            imap_port=extra.get("outlook_imap_port", ""),
            token_endpoint=extra.get("outlook_token_endpoint", ""),
            proxy=proxy,
        )
    else:  # laoudo
        return LaoudoMailbox(
            auth_token=extra.get("laoudo_auth", ""),
            email=extra.get("laoudo_email", ""),
            account_id=extra.get("laoudo_account_id", ""),
        )


class ICloudHmeError(RuntimeError):
    pass


class ICloudServiceDiscoveryError(ICloudHmeError):
    pass


class ICloudAuthExpiredError(ICloudHmeError):
    pass


class ICloudAliasLimitError(ICloudHmeError):
    def __init__(self, message: str, *, retry_after: int = 0):
        super().__init__(message)
        self.retry_after = max(int(retry_after or 0), 0)


class ICloudBusinessError(ICloudHmeError):
    pass


class ICloudHmeClient:
    CLIENT_BUILD_NUMBER = "2412Project35"
    CLIENT_MASTERING_NUMBER = "2412Project35"
    CLIENT_ID = "37bd9669-50c3-4d52-af42-1d240d3ac4f3"
    DISCOVERY_TTL_SECONDS = 30 * 60
    REQUEST_TIMEOUT_SECONDS = 20
    SERVICE_KEYS = ("maildomainws", "premiummailsettings")
    SERVICE_FUZZY_PATTERNS = ("mail", "hme", "hide")

    def __init__(self, cookie: str, domain_base: str = "icloud.com", proxy: str | None = None):
        self.cookie = str(cookie or "").strip()
        self.domain_base = str(domain_base or "icloud.com").strip().lower() or "icloud.com"
        self.origin_base = f"https://www.{self.domain_base}"
        self.proxy = build_requests_proxy_config(proxy)
        self._discovery_lock = threading.Lock()
        self._cached_api_base = ""
        self._cached_api_base_expires_at = 0.0
        self._dsid = self.extract_dsid()

    @staticmethod
    def _truncate(value: Any, limit: int = 200) -> str:
        return str(value or "")[:limit]

    @staticmethod
    def _find_first_non_empty(payload: Any, keys: tuple[str, ...]) -> Any:
        if isinstance(payload, dict):
            for key in keys:
                if key in payload and payload.get(key) not in (None, ""):
                    return payload.get(key)
            for value in payload.values():
                found = ICloudHmeClient._find_first_non_empty(value, keys)
                if found not in (None, ""):
                    return found
        elif isinstance(payload, list):
            for item in payload:
                found = ICloudHmeClient._find_first_non_empty(item, keys)
                if found not in (None, ""):
                    return found
        return None

    def extract_dsid(self) -> str:
        import re

        match = re.search(r'X-APPLE-WEBAUTH-USER=[^;]*d=([^&;"]+)', self.cookie)
        if not match:
            return ""
        return str(match.group(1) or "").replace('"', "").strip()

    def _ensure_config(self) -> None:
        if not self.cookie:
            raise ICloudBusinessError("iCloud HME 未配置：请设置 icloud_cookie")
        if not self._dsid:
            raise ICloudBusinessError("iCloud HME cookie 缺少 DSID：请检查 Settings.icloud_cookie")

    def _headers(self) -> dict[str, str]:
        return {
            "accept": "application/json, text/plain, */*",
            "content-type": "text/plain",
            "origin": self.origin_base,
            "referer": f"{self.origin_base}/",
            "cookie": self.cookie,
        }

    def _query_params(self) -> dict[str, str]:
        return {
            "clientBuildNumber": self.CLIENT_BUILD_NUMBER,
            "clientMasteringNumber": self.CLIENT_MASTERING_NUMBER,
            "clientId": self.CLIENT_ID,
            "dsid": self._dsid,
        }

    @staticmethod
    def _resolve_hme_url(webservices: Any) -> str:
        if not isinstance(webservices, dict):
            return ""
        for key in ICloudHmeClient.SERVICE_KEYS:
            candidate = webservices.get(key)
            if isinstance(candidate, dict) and candidate.get("url"):
                return str(candidate.get("url") or "").strip()
        for key, candidate in webservices.items():
            lower_key = str(key or "").lower()
            if not any(marker in lower_key for marker in ICloudHmeClient.SERVICE_FUZZY_PATTERNS):
                continue
            if isinstance(candidate, dict) and candidate.get("url"):
                return str(candidate.get("url") or "").strip()
        return ""

    def _extract_error_message(self, payload: Any) -> str:
        message = self._find_first_non_empty(
            payload,
            ("errorMessage", "error_message", "message", "reason", "errorReason", "error"),
        )
        if isinstance(message, dict):
            return self._truncate(json.dumps(message, ensure_ascii=False))
        return str(message or "").strip()

    def _looks_like_auth_error(self, status_code: int, payload: Any, message: str) -> bool:
        if status_code in {401, 403}:
            return True
        try:
            serialized = json.dumps(payload, ensure_ascii=False)
        except Exception:
            serialized = str(payload or "")
        text = f"{message} {self._truncate(serialized, 300)}".lower()
        return any(
            marker in text
            for marker in (
                "auth",
                "unauthor",
                "forbidden",
                "invalid cookie",
                "invalid session",
                "session expired",
                "identity not valid",
                "not signed in",
            )
        )

    @staticmethod
    def _looks_like_alias_limit_error(message: str) -> bool:
        text = str(message or "").lower()
        return "750" in text or ("limit" in text and "alias" in text)

    @staticmethod
    def _looks_like_rate_limit_error(status_code: int, message: str) -> bool:
        if int(status_code or 0) == 429:
            return True
        text = str(message or "").lower()
        return any(
            marker in text
            for marker in (
                "rate limit",
                "rate_limited",
                "too many requests",
                "try again later",
                "请求过快",
                "限流",
            )
        )

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any, str]:
        import requests

        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False)
        elif method.upper() == "POST":
            body = "{}"

        response = requests.request(
            method.upper(),
            url,
            headers=self._headers(),
            params=params,
            data=body,
            timeout=self.REQUEST_TIMEOUT_SECONDS,
            proxies=self.proxy,
        )
        raw_text = response.text or ""
        try:
            data = response.json()
        except Exception:
            data = {}
        return response.status_code, data, raw_text

    def _fetch_api_base_uncached(self) -> str:
        self._ensure_config()
        setup_url = f"https://setup.{self.domain_base}/setup/ws/1/validate"
        params = self._query_params()
        last_status = 0
        last_text = ""

        for method in ("POST", "GET"):
            try:
                status_code, payload, raw_text = self._request_json(
                    method,
                    setup_url,
                    payload={} if method == "POST" else None,
                    params=params,
                )
            except Exception as exc:
                last_text = str(exc)
                continue

            last_status = status_code
            last_text = raw_text
            if status_code != 200:
                continue
            api_base = self._resolve_hme_url((payload or {}).get("webservices"))
            if api_base:
                return api_base.rstrip("/")

        raise ICloudServiceDiscoveryError(
            f"iCloud 服务发现失败: status={last_status or 'n/a'} body={self._truncate(last_text, 200)}"
        )

    def clear_cached_api_base(self) -> None:
        with self._discovery_lock:
            self._cached_api_base = ""
            self._cached_api_base_expires_at = 0.0

    def get_api_base(self, *, force_refresh: bool = False) -> str:
        self._ensure_config()
        now = time.time()
        with self._discovery_lock:
            if (
                not force_refresh
                and self._cached_api_base
                and now < self._cached_api_base_expires_at
            ):
                return self._cached_api_base
            api_base = self._fetch_api_base_uncached()
            self._cached_api_base = api_base
            self._cached_api_base_expires_at = now + self.DISCOVERY_TTL_SECONDS
            return api_base

    def _request_action(
        self,
        action: str,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        api_base = self.get_api_base()
        try:
            status_code, response_payload, raw_text = self._request_json(
                method,
                f"{api_base}{path}",
                payload=payload,
                params=self._query_params(),
            )
        except Exception as exc:
            raise ICloudBusinessError(f"iCloud HME 调用失败: {action} error={exc}") from exc

        error_message = self._extract_error_message(response_payload) or self._truncate(raw_text, 200)
        success = True
        if isinstance(response_payload, dict) and "success" in response_payload:
            success = bool(response_payload.get("success"))
        if status_code != 200 or not success:
            if self._looks_like_auth_error(status_code, response_payload, error_message):
                self.clear_cached_api_base()
                raise ICloudAuthExpiredError("iCloud cookie 已失效，请更新 Settings.icloud_cookie")
            if self._looks_like_alias_limit_error(error_message) or self._looks_like_rate_limit_error(
                status_code, error_message
            ):
                retry_after = 0
                if isinstance(response_payload, dict):
                    try:
                        retry_after = int(str(response_payload.get("retryAfter") or "0").strip() or "0")
                    except Exception:
                        retry_after = 0
                raise ICloudAliasLimitError(
                    "iCloud HME 当前请求被临时限流，请稍后重试",
                    retry_after=retry_after,
                )
            raise ICloudBusinessError(
                f"iCloud HME 调用失败: {action} status={status_code} error={error_message}"
            )
        return response_payload

    def generate(self) -> Any:
        return self._request_action("generate", "POST", "/v1/hme/generate", payload={})

    def reserve(self, *, hme: str, label: str, note: str) -> Any:
        return self._request_action(
            "reserve",
            "POST",
            "/v1/hme/reserve",
            payload={
                "hme": str(hme or "").strip(),
                "label": str(label or "").strip(),
                "note": str(note or "").strip(),
            },
        )

    def update_metadata(
        self,
        *,
        anonymous_id: str,
        label: str | None = None,
        note: str | None = None,
    ) -> Any:
        payload = {"anonymousId": str(anonymous_id or "").strip()}
        if label is not None:
            payload["label"] = str(label or "").strip()
        if note is not None:
            payload["note"] = str(note or "").strip()
        return self._request_action("update_metadata", "POST", "/v1/hme/updateMetaData", payload=payload)

    def deactivate(self, *, anonymous_id: str) -> Any:
        """停用一个 HME 别名。Apple 要求删除前必须先停用。"""
        return self._request_action(
            "deactivate",
            "POST",
            "/v1/hme/deactivate",
            payload={"anonymousId": str(anonymous_id or "").strip()},
        )

    def delete(self, *, anonymous_id: str) -> Any:
        """永久删除一个 HME 别名（不可恢复）。需先调用 deactivate。"""
        return self._request_action(
            "delete",
            "POST",
            "/v1/hme/delete",
            payload={"anonymousId": str(anonymous_id or "").strip()},
        )


class HmeReadyApiClient:
    """Client for icloud-hide-email-helper HME Ready API."""

    REQUEST_TIMEOUT_SECONDS = 20
    # HME prepare mutates a single shared Helper ledger.  Serializing it inside
    # one auto-gpt process keeps registration workers from simultaneously
    # queueing the same control-plane endpoint; the lock wait happens before
    # the HTTP read timeout starts.
    _PREPARE_LOCK = threading.Lock()

    def __init__(
        self,
        *,
        api_url: str = "",
        api_key: str = "",
        api_key_header: str = "X-Internal-Key",
        proxy: str | None = None,
    ):
        self.api_url = self._normalize_api_url(api_url)
        self.api_key = str(api_key or "").strip()
        self.api_key_header = str(api_key_header or "X-Internal-Key").strip() or "X-Internal-Key"
        self.proxy = build_requests_proxy_config(proxy)

    @staticmethod
    def _normalize_api_url(api_url: str) -> str:
        """Map retired local/public Helper entries to this deployment's control plane."""
        from urllib.parse import urlsplit

        raw = str(api_url or "").strip().rstrip("/")
        if not raw:
            return ""
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        try:
            port = parts.port
        except ValueError:
            return raw
        if host == "hme.cccy.me" or (
            host in {"127.0.0.1", "localhost", "host.docker.internal"}
            and port == 18765
        ):
            return str(
                os.getenv("HME_READY_INTERNAL_API_URL") or "http://172.20.0.1:18765"
            ).strip().rstrip("/")
        return raw

    @staticmethod
    def _unwrap_payload(payload: Any) -> Any:
        if isinstance(payload, dict) and "data" in payload and set(payload.keys()).issubset(
            {"status", "data", "ok", "success"}
        ):
            return payload.get("data")
        return payload

    @staticmethod
    def _extract_error(payload: Any, raw_text: str = "") -> str:
        if isinstance(payload, dict):
            for key in ("error", "message", "detail", "reason"):
                value = payload.get(key)
                if value:
                    return str(value).strip()
            data = payload.get("data")
            if isinstance(data, dict):
                for key in ("error", "message", "detail", "reason"):
                    value = data.get(key)
                    if value:
                        return str(value).strip()
        return str(raw_text or payload or "").strip()[:500]

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json", "content-type": "application/json"}
        if not self.api_key:
            return headers
        header = self.api_key_header or "X-Internal-Key"
        if header.lower() == "authorization":
            headers["Authorization"] = (
                self.api_key if self.api_key.lower().startswith("bearer ") else f"Bearer {self.api_key}"
            )
        else:
            headers[header] = self.api_key
        return headers

    def _ensure_config(self) -> None:
        if not self.api_url:
            raise RuntimeError("iCloud HME Helper Ready API 未配置：请设置 icloud_hme_helper_api_url")
        if not self.api_key:
            raise RuntimeError("iCloud HME Helper Ready API 未配置：请设置 icloud_hme_helper_internal_key")

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> Any:
        import requests

        self._ensure_config()
        url = f"{self.api_url}{path}"
        try:
            response = requests.request(
                method.upper(),
                url,
                headers=self._headers(),
                params=params or None,
                data=json.dumps(payload or {}, ensure_ascii=False) if method.upper() != "GET" else None,
                timeout=max(int(timeout or self.REQUEST_TIMEOUT_SECONDS), 1),
                proxies=self.proxy,
            )
        except Exception as exc:
            raise RuntimeError(f"HME Ready API 调用失败: {method.upper()} {path} error={exc}") from exc

        raw_text = response.text or ""
        try:
            data = response.json() if raw_text else {}
        except Exception as exc:
            raise RuntimeError(
                f"HME Ready API 返回不是 JSON: {method.upper()} {path} status={response.status_code}"
            ) from exc

        if response.status_code >= 400:
            message = self._extract_error(data, raw_text)
            raise RuntimeError(f"HME Ready API 调用失败: {method.upper()} {path} status={response.status_code} error={message}")
        return self._unwrap_payload(data)

    def prepare(
        self,
        *,
        forward_to: str,
        platform: str = "chatgpt",
        request_id: str = "",
        task_id: str = "",
        consumer: str = "",
        address_mode: str = "",
        ttl_ms: int | None = None,
        max_cache_age_ms: int | None = None,
        test_mode: bool = False,
        test_tag: str = "",
        test_tag_scheme: str = "",
        test_physical_alias_id: str = "",
        test_run_id: str = "",
    ) -> Any:
        body = {
            "forward_to": str(forward_to or "").strip(),
            # ChatGPT is the only platform consumed by this checkout.  Keep
            # the field explicit even while talking to an older Helper which
            # ignores unknown JSON keys.
            "platform": str(platform or "chatgpt").strip().lower() or "chatgpt",
            "request_id": str(request_id or "").strip(),
            "task_id": str(task_id or "").strip(),
            "consumer": str(consumer or "").strip(),
        }
        normalized_address_mode = str(address_mode or "").strip().lower()
        if normalized_address_mode:
            body["address_mode"] = normalized_address_mode
        if ttl_ms:
            body["ttl_ms"] = int(ttl_ms)
        if max_cache_age_ms:
            body["max_cache_age_ms"] = int(max_cache_age_ms)
        if test_mode:
            body.update(
                {
                    "test_mode": True,
                    "test_tag": str(test_tag or "").strip(),
                    "test_tag_scheme": str(test_tag_scheme or "").strip(),
                    "test_physical_alias_id": str(test_physical_alias_id or "").strip(),
                    "test_run_id": str(test_run_id or "").strip(),
                }
            )
        with self._PREPARE_LOCK:
            return self._request("POST", "/api/hme-ready/mailboxes/prepare", payload=body)

    def finalize(
        self,
        lease_id: str,
        *,
        outcome: str,
        reason: str = "",
        registration_id: str = "",
        logical_address_id: str = "",
        physical_alias_id: str = "",
        platform: str = "chatgpt",
        bound_account_email: str = "",
        external_account_ref: str = "",
        chatgpt_account_email: str = "",
        task_id: str = "",
    ) -> dict[str, Any]:
        normalized_bound_email = str(bound_account_email or chatgpt_account_email or "").strip()
        payload = self._request(
            "POST",
            f"/api/hme-ready/mailboxes/{lease_id}/finalize",
            payload={
                "outcome": str(outcome or "").strip(),
                "reason": str(reason or "").strip(),
                "platform": str(platform or "chatgpt").strip().lower() or "chatgpt",
                "lease_id": str(lease_id or "").strip(),
                "registration_id": str(registration_id or "").strip(),
                "logical_address_id": str(logical_address_id or "").strip(),
                "physical_alias_id": str(physical_alias_id or "").strip(),
                "bound_account_email": normalized_bound_email,
                "external_account_ref": str(external_account_ref or "").strip(),
                # Legacy Helper releases still read this field.  It remains a
                # projection only; new callers use bound_account_email above.
                "chatgpt_account_email": normalized_bound_email,
                "task_id": str(task_id or "").strip(),
            },
        )
        return payload if isinstance(payload, dict) else {}


class HmeReadyMailbox(BaseMailbox):
    BASE_ADDRESS_MODE_CONSUMER = "auto-gpt/chatgpt_register"
    PLATFORM_DEFAULT_ADDRESS_MODE = "platform_default"
    RANDOM_TAG_ADDRESS_MODE = "random_tag"

    def __init__(
        self,
        *,
        mail_provider_name: str = "hme_ready_api",
        icloud_hme_mode: str = "helper_ready_api",
        icloud_cookie: str,
        icloud_domain_base: str = "icloud.com",
        icloud_forward_to: str = "b@cccy.me",
        icloud_forward_mailbox_id: str = "",
        tempmail_api_url: str,
        tempmail_api_key: str,
        tempmail_api_key_header: str = "Authorization",
        wait_timeout_seconds: int = 300,
        forward_ttl_minutes: int = 525600,
        forward_permanent: Any = True,
        proxy: str | None = None,
        tempmail_proxy: str | None = None,
        icloud_hme_helper_api_url: str = "",
        icloud_hme_helper_internal_key: str = "",
        icloud_hme_helper_api_key_header: str = "X-Internal-Key",
        icloud_hme_helper_consumer: str = "auto-gpt/chatgpt_register",
        icloud_hme_helper_checkout_ttl_seconds: Any = "",
        icloud_hme_helper_wait_timeout_seconds: Any = "",
        icloud_hme_helper_max_cache_age_seconds: Any = "",
        icloud_hme_test_mode: Any = False,
        icloud_hme_test_tag: str = "",
        icloud_hme_test_tag_scheme: str = "",
        icloud_hme_test_physical_alias_id: str = "",
        icloud_hme_test_run_id: str = "",
    ):
        requested_mode = str(icloud_hme_mode or "helper_ready_api").strip().lower()
        # Keep the marker only for direct callers that still construct the old
        # class shape.  Persisted account state is normalized by the factory
        # and never enters this compatibility branch.
        self._legacy_mode_requested = requested_mode in {"live", "import_pool", "prefer_import"}
        self._mail_provider_name = "hme_ready_api"
        self._icloud_hme_mode = "helper_ready_api"
        # Direct Apple/iCloud access was removed from the active provider.  The
        # argument remains accepted only so old serialized call sites can be
        # opened and migrated without copying the credential forward.
        raw_forward_to = str(icloud_forward_to or "b@cccy.me").strip() or "b@cccy.me"
        self._icloud_forward_tos = [
            item.strip()
            for item in re.split(r"[,;\s]+", raw_forward_to)
            if item.strip()
        ]
        if not self._icloud_forward_tos:
            self._icloud_forward_tos = ["b@cccy.me"]
        self._icloud_forward_to = self._icloud_forward_tos[0]
        self._wait_timeout_seconds = max(int(wait_timeout_seconds or 300), 1)
        helper_wait_timeout = str(icloud_hme_helper_wait_timeout_seconds or "").strip()
        if helper_wait_timeout:
            try:
                self._wait_timeout_seconds = max(int(helper_wait_timeout), 1)
            except (TypeError, ValueError):
                pass
        try:
            self._helper_checkout_ttl_seconds = max(
                int(str(icloud_hme_helper_checkout_ttl_seconds or "").strip() or "0"),
                0,
            )
        except (TypeError, ValueError):
            self._helper_checkout_ttl_seconds = 0
        try:
            self._helper_max_cache_age_seconds = max(
                int(str(icloud_hme_helper_max_cache_age_seconds or "").strip() or "86400"),
                60,
            )
        except (TypeError, ValueError):
            self._helper_max_cache_age_seconds = 86400
        self._helper_consumer = str(icloud_hme_helper_consumer or "auto-gpt/chatgpt_register").strip()
        self._helper_test_mode = _mailbox_bool(icloud_hme_test_mode, default=False)
        self._helper_test_tag = str(icloud_hme_test_tag or "").strip().lower()
        self._helper_test_tag_scheme = str(icloud_hme_test_tag_scheme or "").strip().lower()
        self._helper_test_physical_alias_id = str(icloud_hme_test_physical_alias_id or "").strip()
        self._helper_test_run_id = str(icloud_hme_test_run_id or "").strip().lower()
        self._helper_wait_started_leases: set[str] = set()
        self._helper_client = HmeReadyApiClient(
            api_url=icloud_hme_helper_api_url,
            api_key=icloud_hme_helper_internal_key,
            api_key_header=icloud_hme_helper_api_key_header,
            proxy=tempmail_proxy,
        )
        self._tempmail_mailbox = TempMailLocalMailbox(
            api_url=tempmail_api_url,
            api_key=tempmail_api_key,
            api_key_header=tempmail_api_key_header,
            mode="fixed_domain",
            wait_timeout_seconds=self._wait_timeout_seconds,
            ttl_minutes=forward_ttl_minutes,
            permanent=forward_permanent,
            platform="chatgpt",
            proxy=tempmail_proxy,
        )
        self._forward_mailbox_account: MailboxAccount | None = None
        self._forward_mailbox_accounts: list[MailboxAccount] = []
        self._forward_mailbox_by_email_cache: dict[str, MailboxAccount] = {}

    def _bind_runtime_state(self) -> None:
        self._tempmail_mailbox._log_fn = getattr(self, "_log_fn", None)
        self._tempmail_mailbox._task_control = getattr(self, "_task_control", None)
        self._tempmail_mailbox._task_attempt_token = getattr(self, "_task_attempt_token", None)

    def _ensure_config(self) -> None:
        if self._legacy_mode_requested:
            raise RuntimeError("旧 iCloud HME provider 已移除，请使用 hme_ready_api")
        if self._icloud_hme_mode != "helper_ready_api":
            raise RuntimeError("旧 iCloud HME provider 已移除，请使用 hme_ready_api")
        self._helper_client._ensure_config()
        self._bind_runtime_state()
        self._tempmail_mailbox._ensure_config()

    @staticmethod
    def _normalize_email(value: str) -> str:
        return str(value or "").strip().lower()

    @staticmethod
    def _find_first_non_empty(payload: Any, keys: tuple[str, ...]) -> Any:
        if isinstance(payload, dict):
            for key in keys:
                if key in payload and payload.get(key) not in (None, ""):
                    return payload.get(key)
            for value in payload.values():
                found = HmeReadyMailbox._find_first_non_empty(value, keys)
                if found not in (None, ""):
                    return found
        elif isinstance(payload, list):
            for item in payload:
                found = HmeReadyMailbox._find_first_non_empty(item, keys)
                if found not in (None, ""):
                    return found
        return None

    @staticmethod
    def _is_tagged_hme_alias(alias: str) -> bool:
        """Return whether an HME logical address uses an iCloud ``+tag`` slot."""
        normalized = HmeReadyMailbox._normalize_email(alias)
        local, separator, domain = normalized.rpartition("@")
        return bool(separator and local and domain and "+" in local)

    @staticmethod
    def _raw_header_block(raw_message: str) -> str:
        """Keep transport headers separate from an untrusted message body."""
        return re.split(r"\r?\n\r?\n", str(raw_message or ""), maxsplit=1)[0]

    @classmethod
    def _trusted_transport_header_values(cls, raw_message: str) -> list[str]:
        """Extract routing headers while excluding Subject/From/body text."""

        header_block = cls._raw_header_block(raw_message)
        if not header_block:
            return []
        unfolded = re.sub(r"\r?\n[ \t]+", " ", header_block)
        allowed = {
            "return-path",
            "received",
            "delivered-to",
            "x-original-to",
            "x-forwarded-to",
            "envelope-to",
            "x-envelope-to",
            "original-recipient",
            "final-recipient",
            "x-icloud-hme",
        }
        values: list[str] = []
        for line in unfolded.splitlines():
            name, separator, value = line.partition(":")
            if separator and name.strip().lower() in allowed:
                values.append(value.strip().lower())
        return values

    @classmethod
    def _tagged_hme_headers_match_alias(cls, raw_message: str, alias: str) -> bool:
        """Match a tagged HME only through trusted transport-header tokens.

        Apple normalizes the visible ``To`` and ``X-ICLOUD-HME p=`` values to
        the physical HME alias.  The logical ``+tag`` destination survives in
        ``Return-Path`` as ``local+tag=domain_at_...``.  Matching the physical
        alias would let sibling tag leases consume each other's OTP, and
        searching the body would turn quoted addresses into routing evidence.
        """
        normalized = cls._normalize_email(alias)
        if not cls._is_tagged_hme_alias(normalized):
            return False

        headers = "\n".join(cls._trusted_transport_header_values(raw_message))
        if not headers:
            return False

        def contains_transport_token(token: str) -> bool:
            escaped = re.escape(token)
            return bool(
                re.search(
                    rf"(?:^|[<\s:;=,\-+]){escaped}(?=$|[\s_@>;,)])",
                    headers,
                    flags=re.IGNORECASE,
                )
            )

        # Keep the literal address form for providers that preserve it, then
        # accept Apple's transport-safe @ -> = representation.  The boundary
        # rejects sibling-tag prefix collisions and only considers headers.
        return contains_transport_token(normalized) or contains_transport_token(
            normalized.replace("@", "=", 1)
        )

    @classmethod
    def _base_hme_headers_match_alias(
        cls,
        raw_message: str,
        alias: str,
        received_for: Any = None,
    ) -> bool:
        """Match an untagged/base HME without searching the message body.

        ``received_for`` and the transport header block are routing metadata;
        the decoded body is untrusted content and may quote an arbitrary
        address.  This intentionally mirrors the tagged path's trust boundary
        while allowing providers that expose only ``Delivered-To`` or
        ``X-Original-To``.
        """

        normalized = cls._normalize_email(alias)
        if not normalized or cls._is_tagged_hme_alias(normalized):
            return False
        if isinstance(received_for, str):
            received_for = [received_for]
        targets = {
            cls._normalize_email(value)
            for value in (received_for if isinstance(received_for, (list, tuple, set)) else [])
            if str(value or "").strip()
        }
        if normalized in targets:
            return True
        headers = "\n".join(cls._trusted_transport_header_values(raw_message))
        if not headers:
            return False
        escaped = re.escape(normalized)
        transport_safe = re.escape(normalized.replace("@", "=", 1))
        # Restrict matches to the extracted routing values.  A bare address in
        # a Subject or other non-transport header is not evidence.
        return bool(
            re.search(rf"(?<![\w.+\-]){escaped}(?![\w.+\-])", headers, flags=re.IGNORECASE)
            or re.search(rf"(?<![\w.+\-]){transport_safe}(?![\w.+\-])", headers, flags=re.IGNORECASE)
        )

    @staticmethod
    def _extract_email_like_text(candidate: Any) -> str:
        import re

        value = candidate
        if isinstance(value, dict):
            nested = HmeReadyMailbox._find_first_non_empty(value, ("hme", "email", "address"))
            if nested is not None and nested is not value:
                value = nested
        text = str(value or "").strip()
        if not text:
            return ""
        if re.fullmatch(r"[^@\s{}'\",;<>]+@[^@\s{}'\",;<>]+", text):
            return text
        return ""

    @staticmethod
    def _extract_hme(payload: Any) -> str:
        for candidate in (
            HmeReadyMailbox._find_first_non_empty(payload, ("hme",)),
            HmeReadyMailbox._find_first_non_empty(payload, ("email",)),
            HmeReadyMailbox._find_first_non_empty(payload, ("address",)),
        ):
            text = HmeReadyMailbox._extract_email_like_text(candidate)
            if text:
                return text
        return ""

    def _resolve_all_forward_mailboxes(self) -> list[MailboxAccount]:
        self._ensure_config()
        if self._forward_mailbox_accounts:
            return self._forward_mailbox_accounts
        accounts = []
        for fwd_to in self._icloud_forward_tos:
            accounts.append(self._tempmail_mailbox.ensure_mailbox_by_email(
                fwd_to,
                force_lookup=True,
            ))
        self._forward_mailbox_accounts = accounts
        return self._forward_mailbox_accounts

    def _forward_to_candidates_from_account(self, account: MailboxAccount | None) -> list[str]:
        extra = dict(getattr(account, "extra", None) or {}) if account is not None else {}
        raw_values = [
            extra.get("forward_to"),
        ]
        nested_account = extra.get("account")
        if isinstance(nested_account, dict):
            nested_extra = nested_account.get("extra")
            if isinstance(nested_extra, dict):
                raw_values.append(nested_extra.get("forward_to"))
        candidates: list[str] = []
        seen: set[str] = set()

        def append_raw(raw: Any) -> None:
            if raw in (None, ""):
                return
            if isinstance(raw, (list, tuple, set)):
                for item in raw:
                    append_raw(item)
                return
            for item in re.split(r"[,;\s]+", str(raw or "")):
                normalized = self._normalize_email(item)
                if not normalized or normalized == "*" or "@" not in normalized or normalized in seen:
                    continue
                seen.add(normalized)
                candidates.append(normalized)

        for value in raw_values:
            append_raw(value)
        return candidates

    def _candidate_forward_mailboxes_for_account(self, account: MailboxAccount | None) -> list[MailboxAccount]:
        self._ensure_config()
        extra = dict(getattr(account, "extra", None) or {}) if account is not None else {}
        accounts: list[MailboxAccount] = []
        seen_ids: set[str] = set()
        seen_forward_to: set[str] = set()

        def add_account(candidate: MailboxAccount | None) -> None:
            if candidate is None:
                return
            mailbox_id = str(getattr(candidate, "account_id", "") or "").strip()
            if not mailbox_id or mailbox_id in seen_ids:
                return
            seen_ids.add(mailbox_id)
            accounts.append(candidate)

        forward_candidates = self._forward_to_candidates_from_account(account)
        bound_mailbox_id = str(extra.get("forward_mailbox_id") or "").strip()
        if bound_mailbox_id:
            add_account(
                MailboxAccount(
                    email=forward_candidates[0] if forward_candidates else self._icloud_forward_to,
                    account_id=bound_mailbox_id,
                    extra={"mailbox_action": "bound_forward_mailbox"},
                )
            )
            return accounts

        for forward_to in forward_candidates:
            if forward_to in seen_forward_to:
                continue
            seen_forward_to.add(forward_to)
            cached = self._forward_mailbox_by_email_cache.get(forward_to)
            if cached is not None:
                add_account(cached)
                continue
            resolved = self._tempmail_mailbox.ensure_mailbox_by_email(
                forward_to,
                force_lookup=True,
            )
            self._forward_mailbox_by_email_cache[forward_to] = resolved
            add_account(
                resolved
            )
        if accounts:
            return accounts

        for forward_account in self._resolve_all_forward_mailboxes():
            add_account(forward_account)
        return accounts

    def _claim_imported_alias(self) -> MailboxAccount | None:
        raise RuntimeError("旧 iCloud HME 导入池已移除，请使用 HME Ready API 出池")

    def _refresh_forward_mailbox_binding(
        self,
        account: MailboxAccount | None = None,
        *,
        forward_to: str = "",
    ) -> MailboxAccount:
        target = self._normalize_email(
            forward_to
            or (
                str((getattr(account, "extra", None) or {}).get("forward_to") or "")
                if account is not None
                else ""
            )
            or self._icloud_forward_to
        )
        if "," in target or " " in target:
            target = ""
        if not target:
            raise RuntimeError("HME Ready 转发邮箱目标缺失，无法刷新 mailbox_id")
        refreshed = self._tempmail_mailbox.ensure_mailbox_by_email(
            target,
            force_lookup=True,
        )
        self._forward_mailbox_by_email_cache[target] = refreshed
        self._forward_mailbox_account = refreshed
        if account is not None:
            extra = dict(getattr(account, "extra", None) or {})
            extra["forward_mailbox_id"] = refreshed.account_id
            existing_targets = self._forward_to_candidates_from_account(account)
            # Keep an account's explicit candidate list intact.  The refreshed
            # mailbox id identifies the target that was actually resolved; it
            # must not collapse a historical multi-target route into the
            # current global first candidate.
            if not existing_targets or target not in existing_targets:
                extra["forward_to"] = target
            account.extra = extra
        return refreshed

    def _invalidate_forward_mailbox_cache(
        self,
        account: MailboxAccount | None,
        mailbox_id: str,
        *,
        forward_to: str = "",
    ) -> None:
        """Drop a dead TempMail pointer without changing HME identity."""

        stale_id = str(mailbox_id or "").strip()
        if not stale_id:
            return
        if account is not None:
            extra = dict(getattr(account, "extra", None) or {})
            if str(extra.get("forward_mailbox_id") or "").strip() == stale_id:
                extra.pop("forward_mailbox_id", None)
                account.extra = extra
        for address, cached in list(self._forward_mailbox_by_email_cache.items()):
            if str(getattr(cached, "account_id", "") or "").strip() == stale_id:
                self._forward_mailbox_by_email_cache.pop(address, None)
        if str(getattr(self._forward_mailbox_account, "account_id", "") or "").strip() == stale_id:
            self._forward_mailbox_account = None
        self._forward_mailbox_accounts = []
        self._log(
            "[HME Ready] TempMail mailbox_id 已失效，"
            f"将按转发地址重新解析: forward_to={forward_to or '-'} mailbox_id={stale_id}"
        )

    def create_alias_for_import_pool(
        self,
        *,
        enabled: bool = False,
        note: str | None = None,
        task_id: str | None = None,
        mailbox_action: str = "created",
    ) -> MailboxAccount:
        raise RuntimeError("旧 iCloud HME 直连创建已移除，请使用 HME Ready API 出池")

    @staticmethod
    def _message_id_from_helper_email(message: dict[str, Any], index: int = 0) -> str:
        for key in ("id", "message_id", "messageId", "uid"):
            value = str((message or {}).get(key) or "").strip()
            if value:
                return value
        return f"idx-{index}-{(message or {}).get('received_at') or (message or {}).get('subject') or ''}"

    @staticmethod
    def _helper_lease_id(account: MailboxAccount | None) -> str:
        extra = dict(getattr(account, "extra", None) or {}) if account is not None else {}
        explicit = str(extra.get("lease_id") or extra.get("checkout_id") or "").strip()
        if explicit:
            return explicit
        if str(extra.get("anonymous_id") or "").strip() and str(
            extra.get("source") or ""
        ).strip().lower() in {"legacy-icloud-hme", "icloud-hme-legacy"}:
            return ""
        helper_markers = {
            str(extra.get("mode") or "").strip().lower(),
            str(extra.get("provider") or "").strip().lower(),
            str(extra.get("source") or "").strip().lower(),
        }
        is_helper_state = bool(
            helper_markers
            & {
                "helper_ready_api",
                "hme_ready_api",
                "icloud_hme_ready",
                "icloud_hme_helper_ready",
                "icloud-hide-email-helper",
            }
        )
        if not is_helper_state:
            return ""
        return str(extra.get("mailbox_id") or getattr(account, "account_id", "") or "").strip()

    @staticmethod
    def _helper_identity_from_payload(payload: Any) -> dict[str, Any]:
        """Extract canonical Helper identity fields from old and new shapes.

        The Helper compatibility window has returned the same lease in several
        projections (``auto_gpt``, ``mailbox``, ``lease`` and, in newer builds,
        top-level canonical fields).  Consumers must not derive an address from
        a slot number, so normalize every projection here and leave absent new
        IDs empty for the legacy fallback path.
        """

        root = payload if isinstance(payload, dict) else {}
        sources: list[dict[str, Any]] = [root]
        for key in ("registration", "logical_address", "physical_alias", "lease", "mailbox", "auto_gpt"):
            value = root.get(key)
            if isinstance(value, dict):
                sources.append(value)
                if key == "auto_gpt" and isinstance(value.get("extra"), dict):
                    sources.append(value["extra"])

        def first(*keys: str) -> str:
            for source in sources:
                for key in keys:
                    value = source.get(key)
                    if value not in (None, ""):
                        text = str(value).strip()
                        if text:
                            return text
            return ""

        def first_int(*keys: str) -> Any:
            for source in sources:
                for key in keys:
                    value = source.get(key)
                    if value not in (None, ""):
                        try:
                            return int(value)
                        except (TypeError, ValueError):
                            return value
            return ""

        identity = {
            "email": HmeReadyMailbox._extract_hme(root),
            "registration_id": first("registration_id", "registrationId"),
            "logical_address_id": first("logical_address_id", "logicalAddressId"),
            "physical_alias_id": first("physical_alias_id", "physicalAliasId"),
            "lease_id": first("lease_id", "leaseId", "checkout_id", "checkoutId"),
            "platform": first("platform", "registration_platform") or "chatgpt",
            "lease_state": first("lease_state", "leaseState"),
            "physical_hme": first("physical_hme", "physicalHme", "apple_hme", "appleHme"),
            "address_mode": first("address_mode", "addressMode"),
            "effective_address_mode": first("effective_address_mode", "effectiveAddressMode"),
            "logical_type": first("logical_type", "logicalType"),
            "tag": first("tag"),
            "tag_namespace": first("tag_namespace", "tagNamespace", "slot_namespace", "slotNamespace"),
            "tag_slot": first_int("tag_slot", "tagSlot", "slot_index", "slotIndex"),
            "forward_to": first("forward_to", "forwardTo"),
            "forward_mailbox_id": first("forward_mailbox_id", "forwardMailboxId"),
            "external_account_ref": first("external_account_ref", "externalAccountRef"),
        }
        if not identity["lease_state"]:
            for object_key in ("lease", "registration"):
                obj = root.get(object_key)
                if not isinstance(obj, dict):
                    continue
                identity["lease_state"] = str(
                    obj.get("lease_state")
                    or obj.get("leaseState")
                    or obj.get("state")
                    or obj.get("status")
                    or ""
                ).strip()
                if identity["lease_state"]:
                    break
        if not identity["lease_state"]:
            # Legacy responses sometimes exposed only a top-level state.  Do
            # not use top-level ``status=ok`` from newer envelope responses.
            identity["lease_state"] = str(root.get("state") or "").strip()
        # Some Helper responses expose resource IDs only as ``{id: ...}``
        # inside their typed object.  Map those explicitly without treating a
        # generic mailbox/lease ID as a registration ID.
        for object_key, identity_key in (
            ("registration", "registration_id"),
            ("logical_address", "logical_address_id"),
            ("physical_alias", "physical_alias_id"),
        ):
            if not identity[identity_key]:
                obj = root.get(object_key)
                if isinstance(obj, dict):
                    identity[identity_key] = str(obj.get("id") or "").strip()

        # Legacy responses expose the checkout as auto_gpt.account_id or
        # mailbox.id.  This is deliberately the only ID fallback; registration
        # and logical IDs remain empty so callers can distinguish old Helper
        # state from a stable new identity.
        if not identity["lease_id"]:
            auto_gpt = root.get("auto_gpt") if isinstance(root.get("auto_gpt"), dict) else {}
            mailbox = root.get("mailbox") if isinstance(root.get("mailbox"), dict) else {}
            lease = root.get("lease") if isinstance(root.get("lease"), dict) else {}
            identity["lease_id"] = str(
                auto_gpt.get("account_id")
                or lease.get("id")
                or lease.get("checkout_id")
                or mailbox.get("id")
                or ""
            ).strip()
        if not identity["email"]:
            for source in sources:
                for key in ("email", "full_address", "fullAddress", "address"):
                    candidate = HmeReadyMailbox._extract_email_like_text(source.get(key))
                    if candidate:
                        identity["email"] = candidate
                        break
                if identity["email"]:
                    break
        identity["platform"] = str(identity["platform"] or "chatgpt").strip().lower() or "chatgpt"
        return identity

    @staticmethod
    def _merge_helper_identity(extra: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
        """Merge non-empty canonical identity fields into mailbox metadata."""

        result = dict(extra or {})
        key_map = {
            "registration_id": "registration_id",
            "logical_address_id": "logical_address_id",
            "physical_alias_id": "physical_alias_id",
            "lease_id": "lease_id",
            "platform": "platform",
            "lease_state": "lease_state",
            "physical_hme": "physical_hme",
            "address_mode": "address_mode",
            "effective_address_mode": "effective_address_mode",
            "logical_type": "logical_type",
            "tag": "tag",
            "tag_namespace": "tag_namespace",
            "tag_slot": "tag_slot",
            "external_account_ref": "external_account_ref",
        }
        for source_key, target_key in key_map.items():
            value = identity.get(source_key)
            if value not in (None, ""):
                result[target_key] = value
        # Keep the historical field for downstream account snapshots.
        if identity.get("platform"):
            result["registration_platform"] = str(identity["platform"]).strip().lower()
        if identity.get("lease_id"):
            result.setdefault("checkout_id", str(identity["lease_id"]).strip())
        return result

    def _finalize_invalid_helper_prepare(
        self,
        identity: dict[str, Any],
        *,
        reason: str,
        task_id: str,
    ) -> None:
        """Release a lease returned with an unusable mailbox before raising."""

        lease_id = str(identity.get("lease_id") or "").strip()
        if not lease_id:
            return
        try:
            self._helper_client.finalize(
                lease_id,
                outcome="early_failure",
                reason=reason,
                registration_id=str(identity.get("registration_id") or "").strip(),
                logical_address_id=str(identity.get("logical_address_id") or "").strip(),
                physical_alias_id=str(identity.get("physical_alias_id") or "").strip(),
                platform="chatgpt",
                task_id=task_id,
            )
        except Exception as exc:
            # Preserve the original malformed-prepare error while making the
            # release failure visible for reconciliation.
            self._log(f"[HME Ready] 无效 prepare lease early finalize 失败: {exc}")

    @staticmethod
    def _helper_prepare_request_id(*, attempt_id: str, parent_task_id: str) -> str:
        """Build a globally scoped idempotency key for one registration attempt."""

        normalized_attempt_id = str(attempt_id or "").strip()
        normalized_parent_task_id = str(parent_task_id or "").strip()
        if not normalized_parent_task_id or not normalized_attempt_id:
            return normalized_attempt_id
        instance_id = (
            str(os.getenv("APP_INSTANCE_ID") or "unknown-instance").strip()
            or "unknown-instance"
        )
        scope = "\0".join(
            (instance_id, normalized_parent_task_id, normalized_attempt_id)
        )
        return f"auto-gpt-attempt:{hashlib.sha256(scope.encode('utf-8')).hexdigest()}"

    def _helper_get_email(self) -> MailboxAccount:
        self._ensure_config()
        self._log("[HME Ready] 使用 Helper Ready API 出池")
        # request_id is an attempt-scoped idempotency key.  Keep the durable
        # parent task id separate so the Helper can associate a checkout even
        # when the caller times out before it receives the prepare response.
        attempt_id = str(getattr(self, "_task_attempt_token", "") or "").strip()
        parent_task_id = str(getattr(self, "_registration_task_id", "") or "").strip()
        request_id = self._helper_prepare_request_id(
            attempt_id=attempt_id,
            parent_task_id=parent_task_id,
        )
        ttl_ms = self._helper_checkout_ttl_seconds * 1000 if self._helper_checkout_ttl_seconds else None
        prepare_kwargs = {
            "forward_to": "*",
            "platform": "chatgpt",
            "request_id": request_id,
            "task_id": parent_task_id,
            "consumer": self._helper_consumer,
            "ttl_ms": ttl_ms,
            "max_cache_age_ms": self._helper_max_cache_age_seconds * 1000,
        }
        request_platform_default = (
            self._helper_consumer == self.BASE_ADDRESS_MODE_CONSUMER
            and not self._helper_test_mode
        )
        if request_platform_default:
            prepare_kwargs["address_mode"] = self.PLATFORM_DEFAULT_ADDRESS_MODE
            self._log(
                "[HME Ready] ChatGPT 注册请求原地址加单一平台 Tag 组合 "
                "address_mode=platform_default"
            )
        if self._helper_test_mode:
            prepare_kwargs["address_mode"] = self.RANDOM_TAG_ADDRESS_MODE
            prepare_kwargs.update(
                {
                    "test_mode": True,
                    "test_tag": self._helper_test_tag,
                    "test_tag_scheme": self._helper_test_tag_scheme,
                    "test_physical_alias_id": self._helper_test_physical_alias_id,
                    "test_run_id": self._helper_test_run_id,
                }
            )
        payload = self._helper_client.prepare(**prepare_kwargs)
        auto_gpt = payload.get("auto_gpt") if isinstance(payload, dict) else {}
        mailbox = payload.get("mailbox") if isinstance(payload, dict) else {}
        lease = payload.get("lease") if isinstance(payload, dict) else {}
        if not isinstance(auto_gpt, dict):
            auto_gpt = {}
        if not isinstance(mailbox, dict):
            mailbox = {}
        if not isinstance(lease, dict):
            lease = {}

        identity = self._helper_identity_from_payload(payload)
        # This checkout is the ChatGPT consumer; do not let a malformed or
        # cross-platform response re-label its registration metadata.
        identity["platform"] = "chatgpt"
        email = str(identity.get("email") or "").strip()
        lease_id = str(identity.get("lease_id") or "").strip()
        extra = dict(auto_gpt.get("extra") or {})
        extra = self._merge_helper_identity(extra, identity)
        helper_forward_to = self._normalize_email(
            extra.get("forward_to")
            or mailbox.get("forward_to")
            or mailbox.get("forwardTo")
            or ""
        )
        if helper_forward_to == "*" or "@" not in helper_forward_to:
            helper_forward_to = ""
        helper_forward_mailbox_id = str(
            extra.get("forward_mailbox_id")
            or mailbox.get("forward_mailbox_id")
            or mailbox.get("forwardMailboxId")
            or ""
        ).strip()
        resolved_task_id = str(getattr(self, "_registration_task_id", "") or "").strip()
        if not email:
            self._finalize_invalid_helper_prepare(
                identity,
                reason="invalid_prepare_email",
                task_id=resolved_task_id,
            )
            raise RuntimeError(f"HME Ready API prepare 返回异常邮箱: {payload}")
        if not lease_id:
            raise RuntimeError(f"HME Ready API prepare 返回异常: {payload}")
        extra.update(
            {
                "provider": self._mail_provider_name,
                "mode": "helper_ready_api",
                "source": extra.get("source") or "icloud-hide-email-helper",
                "lease_id": extra.get("lease_id") or lease_id,
                "checkout_id": extra.get("checkout_id") or lease_id,
                "lease_state": extra.get("lease_state") or "checked_out",
                "hme": extra.get("hme") or email,
                "configured_forward_to": self._icloud_forward_to,
                "configured_forward_tos": list(self._icloud_forward_tos),
                "mailbox_action": extra.get("mailbox_action") or "claimed_helper",
            }
        )
        if helper_forward_to:
            extra["forward_to"] = helper_forward_to
        else:
            extra.pop("forward_to", None)
        if helper_forward_mailbox_id:
            extra["forward_mailbox_id"] = helper_forward_mailbox_id
        listen_hint = (
            f"监听转发箱 {helper_forward_to or '-'} mailbox_id={helper_forward_mailbox_id or '-'}"
            if helper_forward_to or helper_forward_mailbox_id
            else "未返回转发箱，验证码回退扫描配置的全部转发箱"
        )
        self._log(f"[HME Ready] Helper 已领取别名: {email} lease={lease_id}，{listen_hint}")
        return MailboxAccount(email=email, account_id=lease_id, extra=extra)

    def get_email(self) -> MailboxAccount:
        self._ensure_config()
        self._log(f"[邮箱] mail_provider={self._mail_provider_name}")
        return self._helper_get_email()

    def get_current_ids(self, account: MailboxAccount) -> set:
        self._ensure_config()
        blocked_ids: set[str] = set()

        def scan(candidates: list[MailboxAccount]) -> tuple[set[str], list[tuple[MailboxAccount, str]]]:
            all_ids: set[str] = set()
            stale: list[tuple[MailboxAccount, str]] = []
            namespace_ids = len(candidates) > 1
            for forward_mailbox in candidates:
                m_id = str(getattr(forward_mailbox, "account_id", "") or "").strip()
                if not m_id or m_id in blocked_ids:
                    continue
                try:
                    mails = self._tempmail_mailbox._list_emails(m_id)
                except Exception as exc:
                    if self._tempmail_mailbox._is_mailbox_not_found_error(exc):
                        stale.append((forward_mailbox, m_id))
                        blocked_ids.add(m_id)
                        continue
                    raise
                for idx, msg in enumerate(mails):
                    raw_mid = self._tempmail_mailbox._message_id(msg, idx)
                    all_ids.add(f"{m_id}:{raw_mid}" if namespace_ids else raw_mid)
            return all_ids, stale

        forward_mailboxes = self._candidate_forward_mailboxes_for_account(account)
        all_ids, stale = scan(forward_mailboxes)
        if stale:
            for forward_mailbox, m_id in stale:
                self._invalidate_forward_mailbox_cache(
                    account,
                    m_id,
                    forward_to=str(getattr(forward_mailbox, "email", "") or "").strip(),
                )
                target = self._normalize_email(
                    str(getattr(forward_mailbox, "email", "") or "").strip()
                    or str((getattr(account, "extra", None) or {}).get("forward_to") or "").strip()
                )
                if target and "@" in target:
                    try:
                        self._refresh_forward_mailbox_binding(
                            account,
                            forward_to=target,
                        )
                    except Exception as refresh_exc:
                        self._log(
                            "[HME Ready] get_current_ids 重绑 TempMail mailbox 失败: "
                            f"forward_to={target} error={refresh_exc}"
                        )
            # Re-resolve by the account's forwarding address(es).  Do not
            # refresh the OTP baseline here; callers still apply their original
            # before_ids/otp_sent_at boundary to newly found messages.
            refreshed_ids, remaining_stale = scan(
                self._candidate_forward_mailboxes_for_account(account)
            )
            all_ids.update(refreshed_ids)
            for forward_mailbox, m_id in remaining_stale:
                self._log(
                    "[HME Ready] 重绑后的 TempMail mailbox 仍不可用，"
                    f"跳过: forward_to={getattr(forward_mailbox, 'email', '') or '-'} "
                    f"mailbox_id={m_id}"
                )
        return all_ids

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        self._ensure_config()
        alias = self._normalize_email(getattr(account, "email", "") or "")
        if not alias:
            raise RuntimeError("HME Ready 当前任务缺少 alias 邮箱地址")
        helper_lease_id = self._helper_lease_id(account)
        if self._icloud_hme_mode == "helper_ready_api" and helper_lease_id:
            self._helper_wait_started_leases.add(helper_lease_id)

        seen = set(before_ids or [])
        otp_sent_at = kwargs.get("otp_sent_at")
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        blocked_mailbox_ids: set[str] = set()
        refresh_attempted_targets: set[str] = set()

        def _forward_target(forward_mailbox: MailboxAccount | None = None) -> str:
            candidate = self._normalize_email(
                str(getattr(forward_mailbox, "email", "") or "").strip()
                if forward_mailbox is not None
                else ""
            )
            if not candidate:
                candidate = self._normalize_email(
                    str((getattr(account, "extra", None) or {}).get("forward_to") or "").strip()
                )
            if not candidate or "," in candidate or " " in candidate or "@" not in candidate:
                return ""
            return candidate

        def _rebind_after_not_found(forward_mailbox: MailboxAccount, mailbox_id: str) -> bool:
            target = _forward_target(forward_mailbox)
            if mailbox_id:
                blocked_mailbox_ids.add(mailbox_id)
            if not target or target in refresh_attempted_targets:
                return False
            refresh_attempted_targets.add(target)
            self._invalidate_forward_mailbox_cache(
                account,
                mailbox_id,
                forward_to=target,
            )
            try:
                refreshed = self._refresh_forward_mailbox_binding(
                    account,
                    forward_to=target,
                )
            except Exception as refresh_exc:
                self._log(
                    "[HME Ready] TempMail mailbox 失效后按转发地址重绑失败: "
                    f"forward_to={target} error={refresh_exc}"
                )
                return False
            refreshed_id = str(getattr(refreshed, "account_id", "") or "").strip()
            if not refreshed_id:
                return False
            self._log(
                "[HME Ready] 已按转发地址刷新 TempMail mailbox: "
                f"forward_to={target} mailbox_id={refreshed_id}"
            )
            return True

        def _persist_actual_binding(forward_mailbox: MailboxAccount, mailbox_id: str) -> None:
            target = _forward_target(forward_mailbox)
            if not target or not mailbox_id:
                return
            extra = dict(getattr(account, "extra", None) or {})
            extra["provider"] = "hme_ready_api"
            extra["mode"] = "helper_ready_api"
            existing_targets = self._forward_to_candidates_from_account(account)
            if not existing_targets or target not in existing_targets:
                extra["forward_to"] = target
            extra["forward_mailbox_id"] = mailbox_id
            account.extra = extra

        def looks_like_verification_mail(subject: str) -> bool:
            lowered_subject = str(subject or "").lower()
            return any(
                marker in lowered_subject
                for marker in (
                    "chatgpt",
                    "openai",
                    "verification",
                    "verify",
                    "code",
                    "验证",
                    "認証",
                    "one-time",
                    "otp",
                )
            )

        def poll_once() -> Optional[str]:
            # A stale mailbox is rebound at most once per forwarding address for
            # this wait.  The second pass re-reads the original account boundary
            # from the newly resolved TempMail mailbox without clearing `seen`.
            for pass_index in range(2):
                retry_requested = False
                forward_mailboxes = self._candidate_forward_mailboxes_for_account(account)
                namespace_ids = len(forward_mailboxes) > 1
                for forward_mailbox in forward_mailboxes:
                    m_id = str(getattr(forward_mailbox, "account_id", "") or "").strip()
                    if not m_id or m_id in blocked_mailbox_ids:
                        continue
                    try:
                        mails = self._tempmail_mailbox._list_emails(m_id)
                    except Exception as exc:
                        if self._tempmail_mailbox._is_mailbox_not_found_error(exc):
                            retry_requested = _rebind_after_not_found(forward_mailbox, m_id) or retry_requested
                            continue
                        raise

                    for idx, msg in enumerate(mails):
                        raw_mid = self._tempmail_mailbox._message_id(msg, idx)
                        scoped_mid = f"{m_id}:{raw_mid}" if namespace_ids else raw_mid
                        # A multi-forward scan must namespace IDs; accepting a
                        # raw ID there would let mailbox A suppress mailbox B.
                        if scoped_mid in seen or (not namespace_ids and raw_mid in seen):
                            continue
                        subject_hint = str(msg.get("subject") or "").strip()
                        try:
                            detail = self._tempmail_mailbox._get_email_detail(m_id, raw_mid)
                        except Exception as exc:
                            if self._tempmail_mailbox._is_mailbox_not_found_error(exc):
                                retry_requested = _rebind_after_not_found(forward_mailbox, m_id) or retry_requested
                                break
                            raise
                        detail = detail if isinstance(detail, dict) else {}
                        received_for = detail.get("received_for") or []
                        raw_message = str(detail.get("raw_message") or "")
                        if self._is_tagged_hme_alias(alias):
                            # A tagged lease must never fall back to its physical
                            # HME address in received_for / X-ICLOUD-HME p=.
                            matched_alias = self._tagged_hme_headers_match_alias(raw_message, alias)
                            match_source = "tagged_hme_transport_header"
                        else:
                            matched_alias = self._base_hme_headers_match_alias(
                                raw_message,
                                alias,
                                received_for,
                            )
                            match_source = "base_hme_transport_header"
                        subject_hint = str(
                            detail.get("subject") or msg.get("subject") or subject_hint or ""
                        ).strip()
                        if not matched_alias:
                            if looks_like_verification_mail(subject_hint):
                                self._log(
                                    "[HME Ready] 转发箱有疑似验证码邮件但未匹配当前 HME 别名: "
                                    f"alias={alias} forward={getattr(forward_mailbox, 'email', '')} "
                                    f"subject={subject_hint[:80]}"
                                )
                            seen.add(scoped_mid)
                            continue

                        msg_ts = self._tempmail_mailbox._parse_message_timestamp(msg)
                        if otp_sent_at and msg_ts and msg_ts < float(otp_sent_at):
                            if looks_like_verification_mail(subject_hint):
                                age = max(0, int(float(otp_sent_at) - float(msg_ts)))
                                self._log(
                                    "[HME Ready] 转发箱有疑似验证码邮件但早于 otp_sent_at 被跳过: "
                                    f"alias={alias} forward={getattr(forward_mailbox, 'email', '')} "
                                    f"subject={subject_hint[:80]} age_before_cutoff={age}s"
                                )
                            seen.add(scoped_mid)
                            continue

                        full_text = " ".join(
                            [
                                str(msg.get("subject") or ""),
                                str(detail.get("subject") or ""),
                                str(detail.get("body_text") or ""),
                                str(detail.get("body_html") or ""),
                                str(detail.get("raw_message") or ""),
                            ]
                        )
                        if not str(detail.get("body_text") or "").strip() and raw_message:
                            decoded_raw = self._decode_raw_content(raw_message)
                            if decoded_raw:
                                full_text = f"{full_text} {decoded_raw}"
                        code = self._safe_extract(full_text, code_pattern)
                        seen.add(scoped_mid)
                        if not code:
                            self._log(
                                "[HME Ready] 别名已匹配但未能从邮件解析验证码: "
                                f"alias={alias} subject={subject_hint[:80]} mid={raw_mid}"
                            )
                            continue
                        if str(code).strip() in exclude_codes:
                            self._log(
                                "[HME Ready] 解析到验证码但在排除列表中已跳过: "
                                f"alias={alias} code={code}"
                            )
                            continue

                        _persist_actual_binding(forward_mailbox, m_id)
                        self._record_verification_result(
                            message_id=scoped_mid,
                            code=code,
                            phase=kwargs.get("phase") or "",
                            # Keep the historical telemetry label so older task
                            # snapshots remain queryable; the persisted mailbox
                            # provider itself is always `hme_ready_api`.
                            provider="IcloudHmeTempMailForwardMailbox",
                            metadata={
                                "received_for": list(received_for) if isinstance(received_for, (list, tuple, set)) else [],
                                "matched_alias": getattr(account, "email", "") or "",
                                "alias_match_source": match_source,
                                "mailbox": {"id": m_id},
                                "raw_message_id": raw_mid,
                                "message_id_namespace": m_id,
                                "lease_id": helper_lease_id,
                                "matched_forward_to": str(getattr(forward_mailbox, "email", "") or ""),
                                "matched_mailbox_id": m_id,
                            },
                        )
                        self._log(f"[HME Ready] TempMail 转发箱命中验证码: {code}")
                        return code
                    if retry_requested:
                        break
                if not retry_requested or pass_index >= 1:
                    return None
            return None

        return self._run_polling_wait(
            timeout=max(int(timeout or self._wait_timeout_seconds), 1),
            poll_interval=3,
            poll_once=poll_once,
            timeout_message=f"等待验证码超时 ({max(int(timeout or self._wait_timeout_seconds), 1)}s)",
        )

    def _apply_helper_finalize_payload(
        self,
        account: MailboxAccount,
        payload: Any,
        *,
        outcome: str,
    ) -> None:
        """Keep the authoritative registration/lease projection for export."""

        extra = dict(getattr(account, "extra", None) or {})
        identity = self._helper_identity_from_payload(payload)
        extra = self._merge_helper_identity(extra, identity)
        if outcome:
            extra["lease_state"] = str(identity.get("lease_state") or outcome).strip().lower()
        extra["platform"] = "chatgpt"
        extra["registration_platform"] = "chatgpt"
        account.extra = extra

    def finalize_success(
        self,
        account: MailboxAccount,
        *,
        registered_email: str = "",
        task_id: str = "",
    ) -> None:
        from core.db import update_icloud_hme_alias_on_success

        account_extra = dict(getattr(account, "extra", None) or {})
        legacy_local_compat = self._legacy_mode_requested and not (
            str(account_extra.get("lease_id") or account_extra.get("checkout_id") or "").strip()
            or str(account_extra.get("provider") or "").strip().lower()
            in {"hme_ready_api", "helper_ready_api", "icloud_hme_ready", "icloud_hme_helper_ready"}
        )
        if self._icloud_hme_mode == "helper_ready_api" and not legacy_local_compat:
            lease_id = self._helper_lease_id(account)
            if not lease_id:
                return
            bound_email = str(registered_email or getattr(account, "email", "") or "").strip()
            resolved_task_id = str(task_id or getattr(self, "_task_attempt_token", "") or "").strip()
            extra = account_extra
            response = self._helper_client.finalize(
                lease_id,
                outcome="success",
                reason="registered",
                registration_id=str(extra.get("registration_id") or "").strip(),
                logical_address_id=str(extra.get("logical_address_id") or "").strip(),
                physical_alias_id=str(extra.get("physical_alias_id") or "").strip(),
                platform="chatgpt",
                bound_account_email=bound_email,
                chatgpt_account_email=bound_email,
                task_id=resolved_task_id,
            )
            self._apply_helper_finalize_payload(account, response, outcome="committed")
            self._helper_wait_started_leases.discard(lease_id)
            self._log(f"[HME Ready] Helper 已提交成功: {getattr(account, 'email', '')}")
            return

        anonymous_id = str(getattr(account, "account_id", "") or "").strip()
        if not anonymous_id:
            return
        bound_email = str(registered_email or getattr(account, "email", "") or "").strip()
        resolved_task_id = str(task_id or getattr(self, "_task_attempt_token", "") or "").strip()
        note = f"chatgpt:{bound_email} task:{resolved_task_id}".strip()[:256]
        update_icloud_hme_alias_on_success(
            anonymous_id,
            bound_account_email=bound_email,
            task_id=resolved_task_id,
            note=note,
        )

    @staticmethod
    def _classify_helper_failure_outcome(
        error_message: str,
        *,
        lease_id: str = "",
        wait_started: bool = False,
    ) -> str:
        """Map registration failure text to Helper finalize outcome.

        - ``early_failure``: safe to re-issue alias (never reached OpenAI signup
          commit). Includes geoip/InvalidIP and pure homepage/CSRF failures.
        - ``keep``: permanently retire as used (already exists / paid / etc.).
        - ``late_failure``: uncertain partial signup (password/OTP/about_you or
          manual stop after lease). Helper must not return the same ready lease.
        """
        error_text = str(error_message or "").strip()
        lowered = error_text.lower()

        keep_alias_failure_markers = (
            "already paid",
            "user is already paid",
            "you have paid",
            "已付费",
            "amount != 0",
            "checkout amount",
            "chatgpt_payment_already_paid",
            "chatgpt_nonzero_checkout_amount_failure",
            # OpenAI already has this HME — never re-issue as fresh ready stock.
            "user_already_exists",
            "already exists",
            "邮箱已存在",
            "该邮箱已存在",
            "existing-account capture",
            "existing_account",
            "login_password",
            "reached login_password",
            "use explicit existing-account",
        )
        early_failure_markers = (
            "访问首页失败",
            "获取 csrf token 失败",
            "提交邮箱失败",
            "邮箱页填写失败",
            "邮箱页提交后未进入",
            "注册入口访问失败",
            "未找到可用 openai 注册入口",
            "page.goto: timeout",
            "timeout 30000ms exceeded",
            "timeout 45000ms exceeded",
            "authorize 失败",
            "preauth",
            "预授权",
            "homepage",
            "csrf",
            # Browser never opened OpenAI signup: free the lease for reuse.
            "invalidip",
            "failed to get ip address",
            "ipecho.net",
            "api.ipify.org",
            "geoip",
            "camoufox geoip",
        )
        # Manual stop after prepare is uncertain if signup may have advanced.
        stop_uncertain_markers = (
            "任务已手动停止",
            "task interruption",
            "taskinterruption",
            "stop requested",
            "已请求立即停止",
        )

        if any(marker.lower() in lowered for marker in keep_alias_failure_markers):
            return "keep"
        if any(marker.lower() in lowered for marker in stop_uncertain_markers):
            return "late_failure"
        if (not wait_started) and any(marker.lower() in lowered for marker in early_failure_markers):
            return "early_failure"
        return "late_failure"

    def finalize_failure(
        self,
        account: MailboxAccount,
        *,
        error_message: str = "",
        task_id: str = "",
    ) -> str | None:
        from core.db import (
            release_icloud_hme_alias_after_early_failure,
            update_icloud_hme_alias_on_account_deactivated,
            update_icloud_hme_alias_on_failure,
        )
        from services.chatgpt_account_state import is_account_deactivated_message

        account_extra = dict(getattr(account, "extra", None) or {})
        legacy_local_compat = self._legacy_mode_requested and not (
            str(account_extra.get("lease_id") or account_extra.get("checkout_id") or "").strip()
            or str(account_extra.get("provider") or "").strip().lower()
            in {"hme_ready_api", "helper_ready_api", "icloud_hme_ready", "icloud_hme_helper_ready"}
        )
        if self._icloud_hme_mode == "helper_ready_api" and not legacy_local_compat:
            lease_id = self._helper_lease_id(account)
            if not lease_id:
                return None
            resolved_task_id = str(task_id or getattr(self, "_task_attempt_token", "") or "").strip()
            error_text = str(error_message or "").strip()
            if is_account_deactivated_message("", error_text):
                outcome = "account_deactivated"
            else:
                outcome = self._classify_helper_failure_outcome(
                    error_text,
                    lease_id=lease_id,
                    wait_started=lease_id in self._helper_wait_started_leases,
                )
            extra = dict(getattr(account, "extra", None) or {})
            response = self._helper_client.finalize(
                lease_id,
                outcome=outcome,
                reason=error_text or outcome,
                registration_id=str(extra.get("registration_id") or "").strip(),
                logical_address_id=str(extra.get("logical_address_id") or "").strip(),
                physical_alias_id=str(extra.get("physical_alias_id") or "").strip(),
                platform="chatgpt",
                task_id=resolved_task_id,
            )
            self._apply_helper_finalize_payload(account, response, outcome=outcome)
            self._helper_wait_started_leases.discard(lease_id)
            self._log(f"[HME Ready] Helper 已处理失败 outcome={outcome}: {getattr(account, 'email', '')}")
            return outcome

        anonymous_id = str(getattr(account, "account_id", "") or "").strip()
        if not anonymous_id:
            return None
        resolved_task_id = str(task_id or getattr(self, "_task_attempt_token", "") or "").strip()
        error_text = str(error_message or "").strip()
        if is_account_deactivated_message("", error_text):
            update_icloud_hme_alias_on_account_deactivated(
                anonymous_id,
                error_message=error_text,
                task_id=resolved_task_id,
            )
            self._log(f"[HME Ready] 账号已删除/停用，别名标记为账号已禁用: {getattr(account, 'email', '')}")
            return "account_deactivated"
        outcome = self._classify_helper_failure_outcome(
            error_text,
            lease_id="",
            wait_started=False,
        )
        if outcome == "early_failure":
            release_icloud_hme_alias_after_early_failure(
                anonymous_id,
                error_message=error_text,
                task_id=resolved_task_id,
            )
            self._log(f"[HME Ready] 早期失败，别名回退为 reserved: {getattr(account, 'email', '')}")
            return outcome
        # keep / late_failure / 其它：标记失败占用，禁止再当 ready 出池
        update_icloud_hme_alias_on_failure(
            anonymous_id,
            error_message=error_text,
            task_id=resolved_task_id,
        )
        return outcome

    def export_state_config(
        self,
        account: MailboxAccount | None = None,
        extra_config: dict | None = None,
    ) -> dict[str, Any]:
        """Export only the provider settings needed to reopen this mailbox.

        The registration config also carries unrelated global runtime state.
        Reading attributes from the constructed mailbox keeps this contract
        independent from that unbounded object.
        """

        config: dict[str, Any] = {
            "icloud_hme_mode": "helper_ready_api",
            "icloud_forward_to": ",".join(self._icloud_forward_tos),
            "icloud_hme_helper_api_url": self._helper_client.api_url,
            "icloud_hme_helper_internal_key": self._helper_client.api_key,
            "icloud_hme_helper_api_key_header": self._helper_client.api_key_header,
            "icloud_hme_helper_consumer": self._helper_consumer,
            "icloud_hme_helper_checkout_ttl_seconds": self._helper_checkout_ttl_seconds,
            "icloud_hme_helper_wait_timeout_seconds": self._wait_timeout_seconds,
            "icloud_hme_helper_max_cache_age_seconds": self._helper_max_cache_age_seconds,
            "tempmail_api_url": self._tempmail_mailbox.api,
            "tempmail_api_key": self._tempmail_mailbox.api_key,
            "tempmail_api_key_header": self._tempmail_mailbox.api_key_header,
            "tempmail_wait_timeout_seconds": self._wait_timeout_seconds,
        }
        return {
            key: value
            for key, value in config.items()
            if value not in (None, "")
        }


# Backward-compatible import for historical integrations and persisted test
# fixtures.  The provider factory never exposes this legacy name anymore.
IcloudHmeMailbox = HmeReadyMailbox


class AppleMailMailbox(BaseMailbox):
    """小苹果取件邮箱服务，基于本地邮箱池文件轮转邮箱账号"""

    def __init__(
        self,
        api_url: str = "https://www.appleemail.top",
        pool_file: str = "",
        pool_dir: str = "mail",
        mailboxes: str = "INBOX,Junk",
        proxy: str = None,
    ):
        self.api = (api_url or "https://www.appleemail.top").rstrip("/")
        self.pool_file = str(pool_file or "").strip()
        self.pool_dir = str(pool_dir or "mail").strip() or "mail"
        self.mailboxes = self._normalize_mailboxes(mailboxes)
        self.proxy = build_requests_proxy_config(proxy)
        self._email = None
        self._selected_record = None
        self._selected_pool_path = None

    @staticmethod
    def _normalize_mailboxes(value: Any) -> list[str]:
        if isinstance(value, (list, tuple, set)):
            items = [str(item or "").strip() for item in value]
        else:
            raw = str(value or "INBOX,Junk").strip() or "INBOX,Junk"
            items = [item.strip() for item in raw.split(",")]

        result = []
        seen = set()
        for item in items:
            if not item:
                continue
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result or ["INBOX", "Junk"]

    def _headers(self) -> dict[str, str]:
        return {"accept": "application/json"}

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any],
        timeout: int = 15,
    ) -> Any:
        import requests

        response = requests.request(
            method,
            f"{self.api}{path}",
            params=payload,
            json=None,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=timeout,
        )
        try:
            data = response.json()
        except Exception as exc:
            preview = (response.text or "")[:200]
            raise RuntimeError(
                f"AppleMail API {path} 返回非 JSON: HTTP {response.status_code} {preview}"
            ) from exc

        if response.status_code >= 400:
            if isinstance(data, dict):
                message = (
                    data.get("detail")
                    or data.get("message")
                    or data.get("error")
                    or response.text
                )
            else:
                message = response.text
            raise RuntimeError(
                f"AppleMail API {path} 失败: {str(message or f'HTTP {response.status_code}').strip()}"
            )

        if isinstance(data, dict) and data.get("success") is False:
            message = (
                data.get("message")
                or data.get("detail")
                or data.get("error")
                or "unknown error"
            )
            raise RuntimeError(f"AppleMail API {path} 失败: {str(message).strip()}")

        return data

    @staticmethod
    def _unwrap_message_payload(payload: Any) -> list[dict[str, Any]]:
        if payload is None:
            return []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("data", "result", "results", "messages", "mails", "emails", "items", "list"):
                if key in payload:
                    nested = AppleMailMailbox._unwrap_message_payload(payload.get(key))
                    if nested:
                        return nested
            if any(
                key in payload
                for key in (
                    "id",
                    "message_id",
                    "uid",
                    "mail_id",
                    "subject",
                    "content",
                    "text",
                    "html",
                    "body",
                    "preview",
                    "verification_code",
                    "code",
                    "otp",
                )
            ):
                return [payload]

            collected = []
            for value in payload.values():
                collected.extend(AppleMailMailbox._unwrap_message_payload(value))
            return collected
        return []

    @staticmethod
    def _resolve_message_id(message: dict[str, Any], mailbox: str) -> str:
        import hashlib

        for key in ("id", "message_id", "uid", "mail_id", "mid", "_id"):
            value = str(message.get(key) or "").strip()
            if value:
                return value

        raw = json.dumps(message, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha1(
            f"{mailbox}:{raw}".encode("utf-8"),
            usedforsecurity=False,
        ).hexdigest()
        return f"{mailbox}:{digest}"

    def _build_search_text(self, message: dict[str, Any]) -> str:
        parts = []
        for key in (
            "subject",
            "from",
            "from_address",
            "sender",
            "preview",
            "text",
            "content",
            "body",
            "html",
            "html_content",
            "raw",
            "raw_content",
            "mail_text",
        ):
            value = message.get(key)
            if value:
                parts.append(str(value))

        if not parts:
            parts.append(json.dumps(message, ensure_ascii=False))

        text = " ".join(parts).strip()
        return self._decode_raw_content(text) or text

    def _extract_code_from_message(
        self,
        message: dict[str, Any],
        code_pattern: str = None,
    ) -> Optional[str]:
        for key in ("verification_code", "code", "otp", "captcha", "verify_code"):
            value = str(message.get(key) or "").strip()
            if value:
                code = self._safe_extract(value, code_pattern)
                if code:
                    return code
        return self._safe_extract(self._build_search_text(message), code_pattern)

    def _resolve_mailboxes_for_account(self, account: MailboxAccount) -> list[str]:
        account_mailbox = ""
        if isinstance(account.extra, dict):
            account_mailbox = str(account.extra.get("mailbox") or "").strip()

        result = []
        seen = set()
        for mailbox in ([account_mailbox] if account_mailbox else []) + list(self.mailboxes):
            name = str(mailbox or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            result.append(name)
        return result or ["INBOX"]

    def _build_request_payload(self, account: MailboxAccount, mailbox: str) -> dict[str, Any]:
        extra = account.extra or {}
        refresh_token = str(extra.get("refresh_token") or "").strip()
        client_id = str(extra.get("client_id") or "").strip()
        if not refresh_token or not client_id:
            raise RuntimeError("AppleMail 邮箱记录缺少 refresh_token 或 client_id")

        return {
            "refresh_token": refresh_token,
            "client_id": client_id,
            "email": account.email,
            "mailbox": mailbox,
        }

    def _list_messages(self, account: MailboxAccount, mailbox: str) -> list[dict[str, Any]]:
        data = self._request_json(
            "GET",
            "/api/mail-all",
            payload=self._build_request_payload(account, mailbox),
            timeout=15,
        )
        if isinstance(data, dict):
            new_refresh_token = str(data.get("new_refresh_token") or "").strip()
            if new_refresh_token:
                if account.extra is None:
                    account.extra = {}
                account.extra["refresh_token"] = new_refresh_token
        return self._unwrap_message_payload(data)

    def get_email(self) -> MailboxAccount:
        from .applemail_pool import take_next_applemail_record

        pool_path, record = take_next_applemail_record(
            pool_file=self.pool_file,
            pool_dir=self.pool_dir,
        )
        self._selected_pool_path = pool_path
        self._selected_record = record
        self._email = record["email"]
        self._log(f"[AppleMail] 使用邮箱池: {pool_path.name}")
        self._log(f"[AppleMail] 分配邮箱: {record['email']}")
        return MailboxAccount(
            email=record["email"],
            account_id=record["email"],
            extra={
                "provider": "applemail",
                "client_id": record["client_id"],
                "refresh_token": record["refresh_token"],
                "mailbox": record.get("mailbox") or "INBOX",
                "pool_file": pool_path.name,
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        ids = set()
        for mailbox in self._resolve_mailboxes_for_account(account):
            try:
                messages = self._list_messages(account, mailbox)
            except Exception:
                continue
            ids.update(
                self._resolve_message_id(message, mailbox)
                for message in messages
            )
        return ids

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }

        def poll_once() -> Optional[str]:
            for mailbox in self._resolve_mailboxes_for_account(account):
                try:
                    messages = self._list_messages(account, mailbox)
                except Exception:
                    continue

                for message in messages:
                    message_id = self._resolve_message_id(message, mailbox)
                    if message_id in seen:
                        continue
                    seen.add(message_id)

                    search_text = self._build_search_text(message)
                    if keyword and keyword.lower() not in search_text.lower():
                        continue

                    code = self._extract_code_from_message(message, code_pattern)
                    if code:
                        self._record_verification_result(
                            message_id=message_id,
                            code=code,
                            phase=kwargs.get("phase") or "",
                            provider="AppleMailMailbox",
                            metadata={"mailbox": mailbox},
                        )
                        self._log(f"[AppleMail] {mailbox} 收到验证码: {code}")
                        return code
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class LaoudoMailbox(BaseMailbox):
    """laoudo.com 邮箱服务"""

    def __init__(self, auth_token: str, email: str, account_id: str):
        self.auth = auth_token
        self._email = email
        self._account_id = account_id
        self.api = "https://laoudo.com/api/email"
        self._ua = "Mozilla/5.0"

    def get_email(self) -> MailboxAccount:
        if not self._email:
            raise RuntimeError(
                "Laoudo 邮箱未配置或已失效，请检查 laoudo_auth、laoudo_email、laoudo_account_id 配置，"
                "或切换到 tempmail_lol（无需配置）"
            )
        return MailboxAccount(email=self._email, account_id=self._account_id)

    def get_current_ids(self, account: MailboxAccount) -> set:
        from curl_cffi import requests as curl_requests

        try:
            r = curl_requests.get(
                f"{self.api}/list",
                params={
                    "accountId": account.account_id,
                    "allReceive": 0,
                    "emailId": 0,
                    "timeSort": 1,
                    "size": 50,
                    "type": 0,
                },
                headers={"authorization": self.auth, "user-agent": self._ua},
                timeout=15,
                impersonate="chrome131",
            )
            if r.status_code == 200:
                mails = r.json().get("data", {}).get("list", []) or []
                return {
                    m.get("id") or m.get("emailId")
                    for m in mails
                    if m.get("id") or m.get("emailId")
                }
        except Exception:
            pass
        return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        from curl_cffi import requests as curl_requests

        seen = set(before_ids) if before_ids else set()
        h = {"authorization": self.auth, "user-agent": self._ua}

        def poll_once() -> Optional[str]:
            try:
                r = curl_requests.get(
                    f"{self.api}/list",
                    params={
                        "accountId": account.account_id,
                        "allReceive": 0,
                        "emailId": 0,
                        "timeSort": 1,
                        "size": 50,
                        "type": 0,
                    },
                    headers=h,
                    timeout=15,
                    impersonate="chrome131",
                )
                if r.status_code == 200:
                    mails = r.json().get("data", {}).get("list", []) or []
                    for mail in mails:
                        mid = mail.get("id") or mail.get("emailId")
                        if not mid or mid in seen:
                            continue
                        seen.add(mid)
                        text = (
                            str(mail.get("subject", ""))
                            + " "
                            + str(mail.get("content") or mail.get("html") or "")
                        )
                        if keyword and keyword.lower() not in text.lower():
                            continue
                        code = self._safe_extract(text, code_pattern)
                        if code:
                            return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=4,
            poll_once=poll_once,
        )


class AitreMailbox(BaseMailbox):
    """mail.aitre.cc 临时邮箱"""

    def __init__(self, email: str):
        self._email = email
        self.api = "https://mail.aitre.cc/api/tempmail"

    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email=self._email)

    def get_current_ids(self, account: MailboxAccount) -> set:
        import requests

        try:
            r = requests.get(
                f"{self.api}/emails", params={"email": account.email}, timeout=10
            )
            emails = r.json().get("emails", [])
            return {str(m["id"]) for m in emails if "id" in m}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import requests

        seen = set(before_ids) if before_ids else set()
        last_check = None

        def poll_once() -> Optional[str]:
            nonlocal last_check
            params = {"email": account.email}
            if last_check:
                params["lastCheck"] = last_check
            try:
                r = requests.get(f"{self.api}/poll", params=params, timeout=10)
                data = r.json()
                last_check = data.get("lastChecked")
                if data.get("count", 0) > 0:
                    r2 = requests.get(
                        f"{self.api}/emails",
                        params={"email": account.email},
                        timeout=10,
                    )
                    for mail in r2.json().get("emails", []):
                        mid = str(mail.get("id", ""))
                        if mid in seen:
                            continue
                        seen.add(mid)
                        text = mail.get("preview", "") + mail.get("content", "")
                        if keyword and keyword.lower() not in text.lower():
                            continue
                        code = self._safe_extract(text, code_pattern)
                        if code:
                            return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class TempMailLolMailbox(BaseMailbox):
    """tempmail.lol 免费临时邮箱（无需注册，自动生成）"""

    def __init__(self, proxy: str = None):
        self.api = "https://api.tempmail.lol/v2"
        self.proxy = build_requests_proxy_config(proxy)
        self._token = None
        self._email = None

    def get_email(self) -> MailboxAccount:
        import requests

        r = requests.post(
            f"{self.api}/inbox/create", json={}, proxies=self.proxy, timeout=15
        )
        data = r.json()
        email = data.get("address") or data.get("email", "")
        if not email:
            raise RuntimeError(f"tempmail.lol API 返回空邮箱: {data}")
        self._email = email
        self._token = data.get("token", "")
        print(f"[TempMailLol] 生成邮箱: {self._email}")
        return MailboxAccount(email=self._email, account_id=self._token)

    def get_current_ids(self, account: MailboxAccount) -> set:
        import requests

        try:
            r = requests.get(
                f"{self.api}/inbox",
                params={"token": account.account_id},
                proxies=self.proxy,
                timeout=10,
            )
            return {str(m["id"]) for m in r.json().get("emails", [])}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import requests

        seen = set(before_ids or [])
        otp_sent_at = kwargs.get("otp_sent_at")

        def poll_once() -> Optional[str]:
            try:
                r = requests.get(
                    f"{self.api}/inbox",
                    params={"token": account.account_id},
                    proxies=self.proxy,
                    timeout=10,
                )
                for mail in sorted(
                    r.json().get("emails", []),
                    key=lambda x: x.get("date", 0),
                    reverse=True,
                ):
                    mid = str(mail.get("id", ""))
                    if mid in seen:
                        continue
                    if otp_sent_at and mail.get("date", 0) / 1000 < otp_sent_at:
                        continue
                    seen.add(mid)
                    text = (
                        mail.get("subject", "")
                        + " "
                        + mail.get("body", "")
                        + " "
                        + mail.get("html", "")
                    )
                    if keyword and keyword.lower() not in text.lower():
                        continue
                    code = self._safe_extract(text, code_pattern)
                    if code:
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class TempMailLocalMailbox(BaseMailbox):
    """TempMail 本地接口：支持固定域名直建邮箱，也支持任务级随机子域 ready 建箱"""

    def __init__(
        self,
        api_url: str,
        api_key: str = "",
        api_key_header: str = "Authorization",
        primary_domain: str = "",
        primary_domains: Any = None,
        mode: str = "fixed_domain",
        wait_timeout_seconds: int = 180,
        ttl_minutes: int = 30,
        reuse_window_minutes: int = 20,
        permanent: Any = False,
        platform: str = "chatgpt",
        proxy: str = None,
    ):
        self.api = self._normalize_api_url(api_url)
        self.api_key = str(api_key or "").strip()
        self.api_key_header = str(api_key_header or "Authorization").strip() or "Authorization"
        self.primary_domain = str(primary_domain or "").strip().lstrip("@.")
        self.primary_domains = self._parse_domain_candidates(primary_domains, fallback=self.primary_domain)
        self.mode = str(mode or "fixed_domain").strip().lower() or "fixed_domain"
        self._bypass_proxy = self._should_bypass_proxy(self.api)
        self.proxy = None if self._bypass_proxy else build_requests_proxy_config(proxy)
        self.platform = str(platform or "chatgpt").strip() or "chatgpt"
        self._wait_timeout_seconds = self._to_int(wait_timeout_seconds, 180)
        self._ttl_minutes = self._to_int(ttl_minutes, 30)
        self._reuse_window_minutes = self._to_int(reuse_window_minutes, 20)
        self._permanent = self._to_bool(permanent)

    @staticmethod
    def _parse_domain_candidates(value: Any, fallback: str = "") -> list[str]:
        raw_items: list[Any]
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                    raw_items = parsed if isinstance(parsed, list) else [text]
                except Exception:
                    raw_items = re.split(r"[\n,;]+", text)
            else:
                raw_items = re.split(r"[\n,;]+", text)
        elif isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raw_items = []
        if fallback:
            raw_items.append(fallback)

        domains: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            domain = str(item or "").strip().lower().lstrip("@.")
            if not domain or domain in seen:
                continue
            seen.add(domain)
            domains.append(domain)
        return domains

    def _choose_fixed_domain(self) -> str:
        domains = self.primary_domains or self._parse_domain_candidates(None, fallback=self.primary_domain)
        if not domains:
            return ""
        if len(domains) == 1:
            return domains[0]
        return random.choice(domains)

    @staticmethod
    def _to_int(value: Any, default: int) -> int:
        try:
            parsed = int(value)
            return parsed if parsed > 0 else default
        except Exception:
            return default

    @staticmethod
    def _to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        return text in {"1", "true", "yes", "on"}

    @staticmethod
    def _normalize_api_url(api_url: str) -> str:
        from urllib.parse import urlsplit

        raw = str(api_url or "").strip().rstrip("/")
        if not raw:
            return ""
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        try:
            port = parts.port
        except ValueError:
            return raw
        if host == "tempmail.cccy.me" or (
            host in {
                "127.0.0.1",
                "localhost",
                "138.197.33.125",
                "any-auto-local.666800.xyz",
            }
            and port in {18080, 18081, 18082, 18083, 8080}
        ):
            # Do not preserve a stale https scheme from the retired public
            # ingress when routing to the in-cluster HTTP API.
            return str(
                os.getenv("TEMPMAIL_INTERNAL_API_URL") or "http://tempmail-api-1:8080"
            ).strip().rstrip("/")
        return raw

    @staticmethod
    def _should_bypass_proxy(api_url: str) -> bool:
        from urllib.parse import urlsplit

        host = (urlsplit(str(api_url or "")).hostname or "").lower()
        if not host:
            return False
        return host in {
            "127.0.0.1",
            "localhost",
            "tempmail-api-1",
            "host.docker.internal",
            "openclaw.666800.xyz",
        }

    def _headers(self) -> dict:
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
        }
        if not self.api_key:
            return headers
        if self.api_key_header.lower() == "authorization":
            token = self.api_key
            headers["Authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
        else:
            headers[self.api_key_header] = self.api_key
        return headers

    @staticmethod
    def _is_auth_error_response(status_code: int, body: str = "") -> bool:
        text = f"{status_code} {body or ''}".lower()
        return (
            int(status_code or 0) == 401
            or "invalid api_key" in text
            or "missing api_key" in text
        )

    @staticmethod
    def _is_auth_exception(exc: Exception) -> bool:
        text = str(exc or "").lower()
        return (
            "tempmail ready api" in text
            and (
                "401" in text
                or "invalid api_key" in text
                or "missing api_key" in text
            )
        )

    def _raise_api_error(self, action: str, response, *, limit: int = 200) -> None:
        body = str(getattr(response, "text", "") or "")[:limit]
        message = f"TempMail Ready API {action}: {response.status_code} {body}"
        if self._is_auth_error_response(response.status_code, body):
            raise TempMailReadyAuthError(
                f"{message}（请检查 tempmail_api_key / tempmail_api_key_header）"
            )
        raise RuntimeError(message)

    def _request(self, method: str, path: str, *, timeout: int, **kwargs):
        import requests

        url = f"{self.api}{path}"
        if self._bypass_proxy:
            with requests.Session() as session:
                session.trust_env = False
                return session.request(method, url, timeout=timeout, proxies={}, **kwargs)
        return requests.request(method, url, timeout=timeout, proxies=self.proxy, **kwargs)

    def _ensure_config(self) -> None:
        if not self.api:
            raise RuntimeError("TempMail Ready API 未配置：请设置 tempmail_api_url")
        if not self.api_key:
            raise RuntimeError("TempMail Ready API 未配置：请设置 tempmail_api_key")

    def _new_task_key(self) -> str:
        import secrets

        return f"anyauto-{int(time.time() * 1000)}-{threading.get_ident()}-{secrets.token_hex(4)}"

    @staticmethod
    def _parse_message_timestamp(message: dict) -> Optional[float]:
        from datetime import datetime

        for key in ("received_at", "receivedAt", "created_at", "createdAt", "date", "timestamp"):
            value = message.get(key)
            if value in (None, ""):
                continue
            if isinstance(value, (int, float)):
                numeric = float(value)
                return numeric / 1000 if numeric > 10_000_000_000 else numeric
            text = str(value).strip()
            if not text:
                continue
            try:
                numeric = float(text)
                return numeric / 1000 if numeric > 10_000_000_000 else numeric
            except (TypeError, ValueError):
                pass
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
        return None

    @staticmethod
    def _message_id(message: dict, index: int = 0) -> str:
        value = message.get("id")
        if value not in (None, ""):
            return str(value)
        return f"idx-{index}-{message.get('received_at') or message.get('subject') or ''}"

    def _list_emails(self, mailbox_id: str) -> list[dict]:
        r = self._request(
            "GET",
            f"/api/mailboxes/{mailbox_id}/emails",
            headers=self._headers(),
            params={"page": 1, "size": 100},
            timeout=15,
        )
        if r.status_code != 200:
            self._raise_api_error("列邮件失败", r)
        data = r.json()
        items = data.get("data") if isinstance(data, dict) else []
        return items if isinstance(items, list) else []

    def _get_email_detail(self, mailbox_id: str, email_id: str) -> dict:
        r = self._request(
            "GET",
            f"/api/mailboxes/{mailbox_id}/emails/{email_id}",
            headers=self._headers(),
            timeout=15,
        )
        if r.status_code != 200:
            self._raise_api_error("邮件详情失败", r)
        data = r.json()
        detail = data.get("email") if isinstance(data, dict) else {}
        detail = detail if isinstance(detail, dict) else {}
        if isinstance(data, dict):
            if "raw_message" in data and "raw_message" not in detail:
                detail["raw_message"] = str(data.get("raw_message") or "")
            if "received_for" in data:
                received_for = data.get("received_for")
                detail["received_for"] = received_for if isinstance(received_for, list) else []
        return detail

    @staticmethod
    def _normalize_email_address(email: str) -> str:
        return str(email or "").strip().lower()

    @staticmethod
    def _mailbox_full_address(mailbox: dict) -> str:
        if not isinstance(mailbox, dict):
            return ""
        full_address = str(
            mailbox.get("full_address")
            or mailbox.get("email")
            or ""
        ).strip()
        if full_address:
            return full_address
        address = str(mailbox.get("address") or "").strip().strip("@")
        if "@" in address:
            return address
        domain_value = mailbox.get("domain") or mailbox.get("domain_name") or mailbox.get("domain_value")
        if isinstance(domain_value, dict):
            domain_value = domain_value.get("name") or domain_value.get("domain") or domain_value.get("value")
        domain = str(domain_value or "").strip().lstrip("@.")
        return f"{address}@{domain}" if address and domain else ""

    @classmethod
    def _mailbox_account_from_item(cls, mailbox: dict, email: str = "") -> MailboxAccount | None:
        if not isinstance(mailbox, dict):
            return None
        mailbox_id = str(mailbox.get("id") or mailbox.get("mailbox_id") or "").strip()
        full_address = cls._normalize_email_address(cls._mailbox_full_address(mailbox))
        target_email = cls._normalize_email_address(email)
        if not mailbox_id or not full_address:
            return None
        if target_email and full_address != target_email:
            return None
        return MailboxAccount(
            email=target_email or full_address,
            account_id=mailbox_id,
            extra={"mailbox": mailbox},
        )

    @staticmethod
    def _extract_mailbox_payload(payload: Any) -> dict:
        if not isinstance(payload, dict):
            return {}
        for key in ("mailbox", "data"):
            value = payload.get(key)
            if isinstance(value, dict):
                return value
        if payload.get("id") or payload.get("full_address") or payload.get("address"):
            return payload
        return {}

    @staticmethod
    def _is_mailbox_not_found_error(exc: Exception) -> bool:
        text = str(exc or "").lower()
        return any(
            marker in text
            for marker in (
                "mailbox not found",
                "invalid mailbox id",
                "not found",
                "404",
                "当前没有邮箱",
                "邮箱不存在",
            )
        )

    @staticmethod
    def _replace_account(target: MailboxAccount, source: MailboxAccount) -> MailboxAccount:
        target.email = source.email
        target.account_id = source.account_id
        target.extra = source.extra
        return target

    @staticmethod
    def _mark_mailbox_action(account: MailboxAccount, action: str) -> MailboxAccount:
        extra = dict(getattr(account, "extra", None) or {})
        extra["mailbox_action"] = action
        account.extra = extra
        return account

    def find_mailbox_by_email(self, email: str) -> MailboxAccount | None:
        self._ensure_config()
        target_email = self._normalize_email_address(email)
        if not target_email:
            return None

        page_size = 100
        for page in range(1, 11):
            r = self._request(
                "GET",
                "/api/mailboxes",
                headers=self._headers(),
                params={"page": page, "size": page_size},
                timeout=15,
            )
            if r.status_code != 200:
                self._raise_api_error("查询邮箱失败", r)
            payload = r.json()
            if isinstance(payload, list):
                items = payload
            elif isinstance(payload, dict):
                items = (
                    payload.get("data")
                    or payload.get("mailboxes")
                    or payload.get("items")
                    or []
                )
            else:
                items = []
            if not isinstance(items, list):
                return None

            for item in items:
                account = self._mailbox_account_from_item(item, target_email)
                if account is not None:
                    return self._mark_mailbox_action(account, "reused_existing")
            if len(items) < page_size:
                break
        return None

    def ensure_mailbox_by_email(self, email: str, *, force_lookup: bool = False) -> MailboxAccount:
        self._ensure_config()
        target_email = self._normalize_email_address(email)
        if not target_email or "@" not in target_email:
            raise RuntimeError("TempMail 精确建箱需要完整邮箱地址")

        existing = self.find_mailbox_by_email(target_email)
        if existing is not None and getattr(existing, "account_id", ""):
            self._log(f"[TempMailLocal] 复用远端邮箱: {target_email}")
            return existing
        if force_lookup:
            self._log(f"[TempMailLocal] 远端邮箱不存在，按原地址新建: {target_email}")
        else:
            self._log(f"[TempMailLocal] 未找到远端邮箱，按原地址新建: {target_email}")

        address, domain = target_email.split("@", 1)
        address = address.strip()
        domain = domain.strip().lstrip("@.")
        if not address or not domain:
            raise RuntimeError("TempMail 精确建箱邮箱格式异常")

        payload = {
            "domain": domain,
            "address": address,
            "ttl_minutes": self._ttl_minutes,
            "permanent": self._permanent,
        }
        r = self._request(
            "POST",
            "/api/mailboxes",
            json=payload,
            headers=self._headers(),
            timeout=20,
        )
        if r.status_code not in (200, 201):
            if r.status_code in (409, 422):
                rebound = self.find_mailbox_by_email(target_email)
                if rebound is not None and getattr(rebound, "account_id", ""):
                    self._log(f"[TempMailLocal] 建箱冲突后已重新绑定远端邮箱: {target_email}")
                    return rebound
            if self._is_auth_error_response(r.status_code, r.text[:300]):
                self._raise_api_error("精确建箱失败", r, limit=300)
            raise RuntimeError(f"TempMail 精确建箱失败: {r.status_code} {r.text[:300]}")

        data = r.json()
        mailbox = self._extract_mailbox_payload(data)
        account = self._mailbox_account_from_item(mailbox, target_email)
        if account is None or not getattr(account, "account_id", ""):
            raise RuntimeError(f"TempMail 精确建箱返回异常: {data}")
        self._mark_mailbox_action(account, "created_exact_address")
        self._log(f"[TempMailLocal] 已按原地址新建远端邮箱: {target_email}")
        return account

    def get_email(self) -> MailboxAccount:
        self._ensure_config()

        if self.mode in {"task_subdomain", "ready_subdomain", "random_domain"}:
            payload = {
                "task_key": self._new_task_key(),
                "platform": self.platform,
                "wait_timeout_seconds": self._wait_timeout_seconds,
                "permanent": self._permanent,
            }
            if self.primary_domain:
                payload["primary_domain"] = self.primary_domain
            if self._ttl_minutes > 0:
                payload["ttl_minutes"] = self._ttl_minutes
            if self._reuse_window_minutes > 0:
                payload["reuse_window_minutes"] = self._reuse_window_minutes

            r = self._request(
                "POST",
                "/api/task-mailboxes/prepare",
                json=payload,
                headers=self._headers(),
                timeout=max(30, self._wait_timeout_seconds + 20),
            )
            if r.status_code not in (200, 201):
                self._raise_api_error("建箱失败", r, limit=300)
            data = r.json()
            mailbox = data.get("mailbox") if isinstance(data, dict) else {}
            lease = data.get("lease") if isinstance(data, dict) else {}
            email = str((mailbox or {}).get("full_address") or "").strip()
            mailbox_id = str((mailbox or {}).get("id") or "").strip()
            if not email or not mailbox_id:
                raise RuntimeError(f"TempMail Ready API 返回异常: {data}")
            self._log(f"[TempMailLocal] 生成随机子域邮箱: {email}")
            return MailboxAccount(
                email=email,
                account_id=mailbox_id,
                extra={
                    "mailbox": mailbox,
                    "lease": lease,
                    "task_key": payload["task_key"],
                    "tempmail_mode": self.mode,
                },
            )

        payload = {
            "ttl_minutes": self._ttl_minutes,
            "permanent": self._permanent,
        }
        selected_domain = self._choose_fixed_domain()
        if selected_domain:
            payload["domain"] = selected_domain
        else:
            raise RuntimeError("TempMail 固定域名模式未选择可用域名")
        r = self._request(
            "POST",
            "/api/mailboxes",
            json=payload,
            headers=self._headers(),
            timeout=20,
        )
        if r.status_code not in (200, 201):
            if self._is_auth_error_response(r.status_code, r.text[:300]):
                self._raise_api_error("固定域名建箱失败", r, limit=300)
            raise RuntimeError(f"TempMail 固定域名建箱失败: {r.status_code} {r.text[:300]}")
        data = r.json()
        mailbox = self._extract_mailbox_payload(data)
        email = str((mailbox or {}).get("full_address") or "").strip()
        mailbox_id = str((mailbox or {}).get("id") or "").strip()
        if not email or not mailbox_id:
            raise RuntimeError(f"TempMail 固定域名返回异常: {data}")
        self._log(f"[TempMailLocal] 生成固定域名邮箱: {email} ({selected_domain})")
        return MailboxAccount(
            email=email,
            account_id=mailbox_id,
            extra={
                "mailbox": mailbox,
                "tempmail_mode": self.mode,
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            mails = self._list_emails(account.account_id)
            return {self._message_id(msg, idx) for idx, msg in enumerate(mails)}
        except Exception as exc:
            if self._is_mailbox_not_found_error(exc) and str(getattr(account, "email", "") or "").strip():
                try:
                    refreshed = self.ensure_mailbox_by_email(account.email, force_lookup=True)
                    self._replace_account(account, refreshed)
                    mails = self._list_emails(account.account_id)
                    return {self._message_id(msg, idx) for idx, msg in enumerate(mails)}
                except Exception as refresh_exc:
                    self._log(f"[TempMailLocal] 邮箱基线重绑失败: {refresh_exc}")
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        seen = set(before_ids or [])
        otp_sent_at = kwargs.get("otp_sent_at")
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        current_account = account
        mailbox_rebound = False

        def poll_once() -> Optional[str]:
            nonlocal current_account, mailbox_rebound
            try:
                mails = self._list_emails(current_account.account_id)
                for idx, msg in enumerate(mails):
                    mid = self._message_id(msg, idx)
                    if mid in seen:
                        continue
                    msg_ts = self._parse_message_timestamp(msg)
                    if otp_sent_at and msg_ts and msg_ts < float(otp_sent_at):
                        seen.add(mid)
                        continue
                    try:
                        detail = self._get_email_detail(current_account.account_id, mid)
                    except Exception as detail_exc:
                        if (
                            not mailbox_rebound
                            and self._is_mailbox_not_found_error(detail_exc)
                            and str(getattr(current_account, "email", "") or "").strip()
                        ):
                            refreshed = self.ensure_mailbox_by_email(current_account.email, force_lookup=True)
                            self._replace_account(current_account, refreshed)
                            mailbox_rebound = True
                            self._log(f"[TempMailLocal] 邮件详情读取时 mailbox 失效，已重新绑定: {current_account.email}")
                            return None
                        detail = {}
                    full_text = " ".join(
                        [
                            str(msg.get("subject") or ""),
                            str(detail.get("subject") or ""),
                            str(detail.get("body_text") or ""),
                            str(detail.get("body_html") or ""),
                            str(detail.get("raw_message") or ""),
                        ]
                    )
                    if keyword and keyword.lower() not in full_text.lower():
                        if full_text.strip():
                            seen.add(mid)
                        continue
                    code = self._safe_extract(full_text, code_pattern)
                    if code:
                        seen.add(mid)
                        self._record_verification_result(
                            message_id=mid,
                            code=code,
                            phase=kwargs.get("phase") or "",
                            provider="TempMailLocalMailbox",
                        )
                        self._log(f"[TempMailLocal] 命中验证码: {code}")
                        return code
            except Exception as exc:
                if self._is_auth_exception(exc):
                    raise
                if (
                    not mailbox_rebound
                    and self._is_mailbox_not_found_error(exc)
                    and str(getattr(current_account, "email", "") or "").strip()
                ):
                    try:
                        refreshed = self.ensure_mailbox_by_email(current_account.email, force_lookup=True)
                        self._replace_account(current_account, refreshed)
                        mailbox_rebound = True
                        seen.clear()
                        self._log(f"[TempMailLocal] mailbox 已失效，已按原地址重新绑定: {current_account.email}")
                    except Exception as refresh_exc:
                        self._log(f"[TempMailLocal] mailbox 失效后重绑失败: {refresh_exc}")
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class SkyMailMailbox(BaseMailbox):
    """SkyMail / CloudMail 自建邮箱服务"""

    def __init__(self, api_base: str, auth_token: str, domain: str, proxy: str = None):
        self.api = (api_base or "").rstrip("/")
        self.auth_token = auth_token or ""
        self.domain = domain or ""
        self.proxy = build_requests_proxy_config(proxy)

    def _headers(self) -> dict:
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": self.auth_token,
        }

    def _ensure_config(self) -> None:
        if not self.api or not self.auth_token or not self.domain:
            raise RuntimeError(
                "SkyMail 未配置完整：请设置 skymail_api_base、skymail_token、skymail_domain"
            )

    def _gen_prefix(self) -> str:
        import random
        import string

        length = random.randint(8, 13)
        chars = string.ascii_lowercase + string.digits
        return "".join(random.choice(chars) for _ in range(length))

    def get_email(self) -> MailboxAccount:
        import requests

        self._ensure_config()
        email = f"{self._gen_prefix()}@{self.domain}"
        payload = {"list": [{"email": email}]}
        r = requests.post(
            f"{self.api}/api/public/addUser",
            json=payload,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"SkyMail 创建邮箱失败: {r.status_code} {r.text[:200]}")

        data = r.json()
        if data.get("code") != 200:
            raise RuntimeError(f"SkyMail 创建邮箱失败: {data}")

        self._log(f"[SkyMail] 生成邮箱: {email}")
        return MailboxAccount(email=email, account_id=email)

    def _list_mails(self, email: str) -> list:
        import requests

        payload = {
            "toEmail": email,
            "num": 1,
            "size": 20,
        }
        r = requests.post(
            f"{self.api}/api/public/emailList",
            json=payload,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=15,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        if data.get("code") != 200:
            return []
        return data.get("data") or []

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            mails = self._list_mails(account.account_id or account.email)
            ids = set()
            for i, msg in enumerate(mails):
                mid = msg.get("id") or msg.get("mailId") or msg.get("messageId")
                if mid:
                    ids.add(str(mid))
                else:
                    digest = (
                        str(msg.get("date") or msg.get("time") or "")
                        + "|"
                        + str(msg.get("subject") or "")
                    )
                    ids.add(f"idx-{i}-{digest}")
            return ids
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        target = account.account_id or account.email
        seen = set(before_ids or [])

        def poll_once() -> Optional[str]:
            try:
                mails = self._list_mails(target)
                for i, msg in enumerate(mails):
                    mid = msg.get("id") or msg.get("mailId") or msg.get("messageId")
                    if not mid:
                        digest = (
                            str(msg.get("date") or msg.get("time") or "")
                            + "|"
                            + str(msg.get("subject") or "")
                        )
                        mid = f"idx-{i}-{digest}"
                    mid = str(mid)
                    if mid in seen:
                        continue
                    seen.add(mid)

                    content = " ".join(
                        [
                            str(msg.get("subject") or ""),
                            str(msg.get("content") or ""),
                            str(msg.get("text") or ""),
                            str(msg.get("html") or ""),
                        ]
                    )
                    if keyword and keyword.lower() not in content.lower():
                        continue

                    code = self._safe_extract(content, code_pattern)
                    if code:
                        self._log(f"[SkyMail] 命中验证码: {code}")
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class CloudMailMailbox(BaseMailbox):
    """CloudMail 自建邮箱服务（genToken + emailList）"""

    _token_lock = threading.Lock()
    _token_cache: dict[str, tuple[str, float]] = {}
    _seen_ids_lock = threading.Lock()
    _seen_ids: dict[str, set[str]] = {}

    def __init__(
        self,
        api_base: str,
        admin_email: str,
        admin_password: str,
        domain: Any = "",
        subdomain: str = "",
        timeout: int = 30,
        proxy: str = None,
    ):
        self.api = str(api_base or "").rstrip("/")
        self.admin_email = str(admin_email or "").strip()
        self.admin_password = str(admin_password or "").strip()
        self.domain = domain
        self.subdomain = str(subdomain or "").strip()
        self.timeout = max(int(timeout or 30), 5)
        self.proxy = build_requests_proxy_config(proxy)

    @staticmethod
    def _extract_domain_from_url(url: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(str(url or ""))
        host = (parsed.netloc or parsed.path.split("/")[0] or "").strip()
        if ":" in host:
            host = host.split(":", 1)[0].strip()
        return host

    @staticmethod
    def _normalize_domain(value: str) -> str:
        domain = str(value or "").strip().lstrip("@")
        if "://" in domain:
            domain = CloudMailMailbox._extract_domain_from_url(domain)
        return domain.strip()

    def _domain_candidates(self) -> list[str]:
        candidates: list[str] = []

        if isinstance(self.domain, (list, tuple, set)):
            iterable = self.domain
        else:
            raw = str(self.domain or "").strip()
            parsed = None
            if raw.startswith("[") and raw.endswith("]"):
                try:
                    parsed = json.loads(raw)
                except Exception:
                    parsed = None
            if isinstance(parsed, list):
                iterable = parsed
            elif raw:
                normalized = (
                    raw.replace(";", "\n")
                    .replace(",", "\n")
                    .replace("|", "\n")
                    .splitlines()
                )
                iterable = [item for item in normalized if item]
            else:
                iterable = []

        for item in iterable:
            normalized = self._normalize_domain(item)
            if normalized:
                candidates.append(normalized)

        if not candidates:
            inferred = self._normalize_domain(self._extract_domain_from_url(self.api))
            if inferred:
                candidates.append(inferred)
        return candidates

    def _resolve_admin_email(self) -> str:
        if self.admin_email:
            return self.admin_email
        domains = self._domain_candidates()
        if domains:
            return f"admin@{domains[0]}"
        return "admin@example.com"

    def _cache_key(self) -> str:
        return f"{self.api}|{self._resolve_admin_email()}|{self.admin_password}"

    def _ensure_config(self) -> None:
        if not self.api or not self.admin_password:
            raise RuntimeError(
                "CloudMail 未配置完整：请设置 cloudmail_api_base 与 cloudmail_admin_password"
            )

    def _headers(self, token: str = "") -> dict:
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
        }
        if token:
            headers["authorization"] = token
        return headers

    def _generate_token(self) -> str:
        import requests

        self._ensure_config()
        payload = {
            "email": self._resolve_admin_email(),
            "password": self.admin_password,
        }
        r = requests.post(
            f"{self.api}/api/public/genToken",
            json=payload,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"CloudMail 生成 token 失败: {r.status_code} {str(r.text or '')[:200]}"
            )

        try:
            data = r.json()
        except Exception:
            data = {}
        if data.get("code") != 200:
            raise RuntimeError(f"CloudMail 生成 token 失败: {data}")
        token = ((data.get("data") or {}).get("token") or "").strip()
        if not token:
            raise RuntimeError("CloudMail 生成 token 失败: 响应未返回 token")
        return token

    def _get_token(self, *, force_refresh: bool = False) -> str:
        cache_key = self._cache_key()
        now = time.time()
        with CloudMailMailbox._token_lock:
            if not force_refresh:
                cached = CloudMailMailbox._token_cache.get(cache_key)
                if cached and now < cached[1]:
                    return cached[0]

            token = self._generate_token()
            CloudMailMailbox._token_cache[cache_key] = (token, now + 3600)
            return token

    def _list_mails(self, email: str, *, retry_auth: bool = True) -> list:
        import requests

        token = self._get_token()
        payload = {
            "toEmail": email,
            "timeSort": "desc",
        }
        r = requests.post(
            f"{self.api}/api/public/emailList",
            json=payload,
            headers=self._headers(token),
            proxies=self.proxy,
            timeout=self.timeout,
        )
        if r.status_code == 401 and retry_auth:
            token = self._get_token(force_refresh=True)
            r = requests.post(
                f"{self.api}/api/public/emailList",
                json=payload,
                headers=self._headers(token),
                proxies=self.proxy,
                timeout=self.timeout,
            )
        if r.status_code != 200:
            return []

        try:
            data = r.json()
        except Exception:
            data = {}
        if data.get("code") != 200:
            return []
        return data.get("data") or []

    def _gen_prefix(self) -> str:
        import random
        import string

        first = random.choice(string.ascii_lowercase)
        rest = "".join(random.choices(string.ascii_lowercase + string.digits, k=9))
        return first + rest

    def _build_email(self) -> str:
        domains = self._domain_candidates()
        if not domains:
            raise RuntimeError("CloudMail 未配置可用域名")
        domain = random.choice(domains)
        if self.subdomain:
            domain = f"{self.subdomain}.{domain}"
        return f"{self._gen_prefix()}@{domain}"

    @staticmethod
    def _parse_message_timestamp(message: dict) -> Optional[float]:
        from datetime import datetime

        keys = [
            "time",
            "date",
            "created",
            "createdAt",
            "created_at",
            "receivedAt",
            "received_at",
            "sendTime",
            "timestamp",
        ]
        for key in keys:
            value = message.get(key)
            if value in (None, ""):
                continue
            if isinstance(value, (int, float)):
                numeric = float(value)
                return numeric / 1000 if numeric > 10_000_000_000 else numeric
            text = str(value).strip()
            if not text:
                continue
            try:
                numeric = float(text)
                return numeric / 1000 if numeric > 10_000_000_000 else numeric
            except (TypeError, ValueError):
                pass
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
        return None

    @staticmethod
    def _mail_id(message: dict, index: int = 0) -> str:
        for key in ("emailId", "id", "mailId", "messageId"):
            value = message.get(key)
            if value not in (None, ""):
                return str(value)
        digest = (
            str(message.get("date") or message.get("time") or "")
            + "|"
            + str(message.get("subject") or "")
        )
        return f"idx-{index}-{digest}"

    def _remember_seen_id(self, email: str, message_id: str) -> None:
        with CloudMailMailbox._seen_ids_lock:
            CloudMailMailbox._seen_ids.setdefault(email, set()).add(message_id)

    def _load_seen_ids(self, email: str) -> set[str]:
        with CloudMailMailbox._seen_ids_lock:
            return set(CloudMailMailbox._seen_ids.get(email, set()))

    def get_email(self) -> MailboxAccount:
        self._ensure_config()
        email = self._build_email()
        self._log(f"[CloudMail] 生成邮箱: {email}")
        return MailboxAccount(email=email, account_id=email)

    def get_current_ids(self, account: MailboxAccount) -> set:
        target = account.account_id or account.email
        try:
            mails = self._list_mails(target)
            return {self._mail_id(msg, idx) for idx, msg in enumerate(mails)}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        target = account.account_id or account.email
        seen = set(before_ids or set())
        seen.update(self._load_seen_ids(target))
        otp_sent_at = kwargs.get("otp_sent_at")
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }

        def poll_once() -> Optional[str]:
            try:
                mails = self._list_mails(target)
                for idx, msg in enumerate(mails):
                    mid = self._mail_id(msg, idx)
                    if mid in seen:
                        continue
                    seen.add(mid)
                    self._remember_seen_id(target, mid)

                    msg_ts = self._parse_message_timestamp(msg)
                    if otp_sent_at and msg_ts and msg_ts < float(otp_sent_at):
                        continue

                    content = " ".join(
                        [
                            str(msg.get("subject") or ""),
                            str(msg.get("content") or ""),
                            str(msg.get("text") or ""),
                            str(msg.get("html") or ""),
                        ]
                    )
                    if keyword and keyword.lower() not in content.lower():
                        continue
                    code = self._safe_extract(content, code_pattern)
                    if code:
                        self._record_verification_result(
                            message_id=mid,
                            code=code,
                            phase=kwargs.get("phase") or "",
                            provider="CloudMailMailbox",
                        )
                        self._log(f"[CloudMail] 命中验证码: {code}")
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class DuckMailMailbox(BaseMailbox):
    """DuckMail 自动生成邮箱（随机创建账号）"""

    def __init__(
        self,
        api_url: str = "https://www.duckmail.sbs",
        provider_url: str = "https://api.duckmail.sbs",
        bearer: str = "kevin273945",
        domain: str = "",
        api_key: str = "",
        proxy: str = None,
    ):
        self.api = (api_url or "https://www.duckmail.sbs").rstrip("/")
        self.provider_url = (provider_url or "https://api.duckmail.sbs").rstrip("/")
        self.bearer = bearer or "kevin273945"
        self.domain = str(domain or "").strip()
        self.api_key = str(api_key or "").strip()
        self.proxy = build_requests_proxy_config(proxy)
        self._token = None
        self._address = None
        # 如果配置了 API Key，直接请求 DuckMail API；否则走前端代理
        self._direct = bool(self.api_key)

    def _proxy_headers(self) -> dict:
        return {
            "authorization": f"Bearer {self.bearer}",
            "content-type": "application/json",
            "x-api-provider-base-url": self.provider_url,
        }

    def _direct_headers(self, token: str = "") -> dict:
        auth = token or self.api_key
        return {
            "authorization": f"Bearer {auth}",
            "content-type": "application/json",
        }

    def _request(self, method: str, endpoint: str, token: str = "", **kwargs):
        """统一请求方法，根据模式选择直连或代理"""
        import requests

        if self._direct:
            url = f"{self.provider_url}{endpoint}"
            headers = self._direct_headers(token)
        else:
            from urllib.parse import quote

            url = f"{self.api}/api/mail?endpoint={quote(endpoint, safe='')}"
            headers = (
                self._proxy_headers()
                if not token
                else {
                    "authorization": f"Bearer {token}",
                    "x-api-provider-base-url": self.provider_url,
                }
            )
        r = requests.request(
            method, url, headers=headers, proxies=self.proxy, timeout=15, **kwargs
        )
        return r

    def get_email(self) -> MailboxAccount:
        import random, string

        username = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        password = "Test" + "".join(random.choices(string.digits, k=8)) + "!"
        domain = self.domain or self.provider_url.replace("https://api.", "").replace(
            "https://", ""
        )
        address = f"{username}@{domain}"
        print(f"[DuckMail] 创建账号: {address} direct={self._direct}")
        # 创建账号
        r = self._request(
            "POST", "/accounts", json={"address": address, "password": password}
        )
        if r.status_code >= 400 or not r.text.strip().startswith("{"):
            raise RuntimeError(
                f"[DuckMail] 创建账号失败: HTTP {r.status_code} body={r.text[:300]}"
            )
        data = r.json()
        self._address = data.get("address", address)
        # 登录获取 token
        r2 = self._request(
            "POST", "/token", json={"address": self._address, "password": password}
        )
        if r2.status_code >= 400 or not r2.text.strip().startswith(("{", "[")):
            raise RuntimeError(
                f"[DuckMail] 登录失败: HTTP {r2.status_code} body={r2.text[:300]}"
            )
        self._token = r2.json().get("token", "")
        return MailboxAccount(email=self._address, account_id=self._token)

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            r = self._request("GET", "/messages?page=1", token=account.account_id)
            return {str(m["id"]) for m in r.json().get("hydra:member", [])}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        from datetime import datetime
        import re

        seen = set(before_ids or [])
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        otp_sent_at = kwargs.get("otp_sent_at")

        def _parse_message_timestamp(*values) -> Optional[float]:
            for value in values:
                if value in (None, ""):
                    continue
                if isinstance(value, (int, float)):
                    numeric = float(value)
                    return numeric / 1000 if numeric > 10_000_000_000 else numeric
                text = str(value).strip()
                if not text:
                    continue
                try:
                    numeric = float(text)
                    return numeric / 1000 if numeric > 10_000_000_000 else numeric
                except (TypeError, ValueError):
                    pass
                try:
                    normalized = text.replace("Z", "+00:00")
                    return datetime.fromisoformat(normalized).timestamp()
                except ValueError:
                    continue
            return None

        def poll_once() -> Optional[str]:
            try:
                r = self._request("GET", "/messages?page=1", token=account.account_id)
                msgs = r.json().get("hydra:member", [])
                for msg in msgs:
                    mid = str(msg.get("id") or msg.get("msgid") or "")
                    if mid in seen:
                        continue
                    seen.add(mid)
                    # 请求邮件详情获取完整 text
                    try:
                        r2 = self._request(
                            "GET", f"/messages/{mid}", token=account.account_id
                        )
                        detail = r2.json()
                        body = (
                            str(detail.get("text") or "")
                            + " "
                            + str(detail.get("subject") or "")
                        )
                    except Exception:
                        detail = {}
                        body = str(msg.get("subject") or "")
                    message_ts = _parse_message_timestamp(
                        detail.get("createdAt"),
                        detail.get("created_at"),
                        detail.get("receivedAt"),
                        detail.get("received_at"),
                        detail.get("date"),
                        detail.get("created"),
                        msg.get("createdAt"),
                        msg.get("created_at"),
                        msg.get("receivedAt"),
                        msg.get("received_at"),
                        msg.get("date"),
                        msg.get("created"),
                    )
                    if otp_sent_at and message_ts and message_ts < float(otp_sent_at):
                        continue
                    body = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "", body
                    )
                    code = self._safe_extract(body, code_pattern)
                    if code:
                        if str(code).strip() in exclude_codes:
                            continue
                        self._record_verification_result(
                            message_id=mid,
                            code=code,
                            phase=kwargs.get("phase") or "",
                            provider="OpenTrashMailMailboxApi",
                        )
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class MaliAPIMailbox(BaseMailbox):
    """YYDS Mail / MaliAPI 临时邮箱服务"""

    def __init__(
        self,
        api_url: str = "https://maliapi.215.im/v1",
        api_key: str = "",
        domain: str = "",
        auto_domain_strategy: str = "",
        proxy: str = None,
    ):
        self.api = (api_url or "https://maliapi.215.im/v1").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.domain = str(domain or "").strip()
        self.auto_domain_strategy = str(auto_domain_strategy or "").strip()
        self.proxy = build_requests_proxy_config(proxy)
        self._email = None
        self._temp_token = None

    def _headers(self, bearer: str = "") -> dict[str, str]:
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict = None,
        params: dict = None,
        bearer: str = "",
    ) -> Any:
        import requests

        response = requests.request(
            method,
            f"{self.api}{path}",
            headers=self._headers(bearer),
            json=json_body,
            params=params,
            proxies=self.proxy,
            timeout=15,
        )
        try:
            payload = response.json()
        except Exception:
            payload = {}

        if response.status_code >= 400:
            error = response.text or f"HTTP {response.status_code}"
            error_code = ""
            if isinstance(payload, dict):
                error = str(payload.get("error") or error).strip()
                error_code = str(payload.get("errorCode") or "").strip()
            if error_code:
                raise RuntimeError(f"MaliAPI 请求失败: {error} ({error_code})")
            raise RuntimeError(f"MaliAPI 请求失败: {str(error).strip()}")

        if isinstance(payload, dict):
            if payload.get("success") is False:
                error = str(payload.get("error") or "unknown error").strip()
                error_code = str(payload.get("errorCode") or "").strip()
                if error_code:
                    raise RuntimeError(f"MaliAPI 请求失败: {error} ({error_code})")
                raise RuntimeError(f"MaliAPI 请求失败: {error}")
            if "data" in payload:
                return payload.get("data")
        return payload

    def _ensure_api_key(self) -> None:
        if not self.api_key:
            raise RuntimeError("MaliAPI 未配置：请在全局设置中填写 maliapi_api_key")

    def _list_messages(self, account: MailboxAccount) -> list[dict]:
        data = self._request("GET", "/messages", params={"address": account.email})
        if isinstance(data, dict):
            messages = data.get("messages", [])
        else:
            messages = data
        return [item for item in (messages or []) if isinstance(item, dict)]

    def _get_message_detail(self, message_id: str) -> dict:
        data = self._request("GET", f"/messages/{message_id}")
        if isinstance(data, dict) and isinstance(data.get("message"), dict):
            return data["message"]
        return data if isinstance(data, dict) else {}

    def get_email(self) -> MailboxAccount:
        self._ensure_api_key()
        body = {}
        if self.domain:
            body["domain"] = self.domain
        if self.auto_domain_strategy:
            body["autoDomainStrategy"] = self.auto_domain_strategy

        data = self._request("POST", "/accounts", json_body=body)
        if not isinstance(data, dict):
            raise RuntimeError(f"MaliAPI 返回异常: {data}")

        email = str(data.get("address") or data.get("email") or "").strip()
        temp_token = str(
            data.get("tempToken") or data.get("temp_token") or data.get("token") or ""
        ).strip()
        inbox_id = str(data.get("id") or "").strip()
        if not email:
            raise RuntimeError(f"MaliAPI 返回空邮箱: {data}")

        self._email = email
        self._temp_token = temp_token
        self._log(f"[MaliAPI] 生成邮箱: {email}")
        return MailboxAccount(
            email=email,
            account_id=temp_token or inbox_id or email,
            extra={
                "provider": "maliapi",
                "temp_token": temp_token,
                "inbox_id": inbox_id,
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        self._ensure_api_key()
        try:
            return {
                str(message.get("id"))
                for message in self._list_messages(account)
                if message.get("id") is not None
            }
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re

        self._ensure_api_key()
        seen = {str(mid) for mid in (before_ids or set())}

        def poll_once() -> Optional[str]:
            try:
                for message in self._list_messages(account):
                    message_id = str(message.get("id") or "").strip()
                    if not message_id or message_id in seen:
                        continue
                    seen.add(message_id)

                    try:
                        detail = self._get_message_detail(message_id)
                    except Exception:
                        detail = message

                    search_text = " ".join(
                        [
                            str(detail.get("subject") or message.get("subject") or ""),
                            str(detail.get("text") or ""),
                            str(detail.get("html") or ""),
                            str(message.get("subject") or ""),
                            str(message.get("snippet") or ""),
                        ]
                    ).strip()
                    search_text = self._yyds_decode_raw_content(search_text) or search_text
                    search_text = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                        "",
                        search_text,
                    )
                    if keyword and keyword.lower() not in search_text.lower():
                        continue

                    code = self._yyds_safe_extract(search_text, code_pattern)
                    if code:
                        self._log(f"[MaliAPI] 收到验证码: {code}")
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class GPTMailMailbox(BaseMailbox):
    """GPTMail 临时邮箱服务"""

    def __init__(
        self,
        api_url: str = "https://mail.chatgpt.org.uk",
        api_key: str = "",
        domain: str = "",
        proxy: str = None,
    ):
        self.api = (api_url or "https://mail.chatgpt.org.uk").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.domain = self._normalize_domain(domain)
        self.proxy = build_requests_proxy_config(proxy)
        self._email = None

    @staticmethod
    def _normalize_domain(value: Any) -> str:
        domain = str(value or "").strip().lower()
        if domain.startswith("@"):
            domain = domain[1:]
        return domain

    @staticmethod
    def _generate_local_part() -> str:
        import string

        prefix = "".join(random.choices(string.ascii_lowercase, k=6))
        suffix = "".join(random.choices(string.digits, k=4))
        return f"{prefix}{suffix}"

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        timeout: int = 15,
    ) -> Any:
        import requests

        response = requests.request(
            method,
            f"{self.api}{path}",
            params=params,
            json=json_body,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=timeout,
        )
        try:
            payload = response.json()
        except Exception as exc:
            preview = (response.text or "")[:200]
            raise RuntimeError(
                f"GPTMail API {path} 返回非 JSON: HTTP {response.status_code} {preview}"
            ) from exc

        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload, dict) else ""
            message = str(error or response.text or f"HTTP {response.status_code}").strip()
            raise RuntimeError(f"GPTMail API {path} 失败: {message}")

        if isinstance(payload, dict) and payload.get("success") is False:
            error = str(payload.get("error") or "unknown error").strip()
            raise RuntimeError(f"GPTMail API {path} 失败: {error}")

        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data")
        return payload

    def _list_messages(self, email: str) -> list[dict]:
        data = self._request_json("GET", "/api/emails", params={"email": email}, timeout=10)
        if isinstance(data, dict):
            messages = data.get("emails", [])
        else:
            messages = data
        return [item for item in (messages or []) if isinstance(item, dict)]

    def _get_message_detail(self, message_id: str) -> dict[str, Any]:
        data = self._request_json("GET", f"/api/email/{message_id}", timeout=10)
        return data if isinstance(data, dict) else {}

    def get_email(self) -> MailboxAccount:
        if self.domain:
            email = f"{self._generate_local_part()}@{self.domain}"
            self._email = email
            self._log(f"[GPTMail] 本地拼装邮箱: {email}")
            return MailboxAccount(
                email=email,
                account_id=email,
                extra={"provider": "gptmail", "domain": self.domain, "local_address": True},
            )

        data = self._request_json("GET", "/api/generate-email")
        if not isinstance(data, dict):
            raise RuntimeError(f"GPTMail 返回异常: {data}")

        email = str(data.get("email") or "").strip()
        if not email:
            raise RuntimeError(f"GPTMail 返回空邮箱: {data}")

        self._email = email
        self._log(f"[GPTMail] 生成邮箱: {email}")
        return MailboxAccount(
            email=email,
            account_id=email,
            extra={"provider": "gptmail"},
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {
                str(message.get("id"))
                for message in self._list_messages(account.email)
                if message.get("id") is not None
            }
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re

        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code) for code in (kwargs.get("exclude_codes") or set()) if code
        }

        def poll_once() -> Optional[str]:
            try:
                messages = self._list_messages(account.email)
                for message in messages:
                    message_id = str(message.get("id") or "").strip()
                    if not message_id or message_id in seen:
                        continue
                    seen.add(message_id)

                    try:
                        detail = self._get_message_detail(message_id)
                    except Exception:
                        detail = {}

                    search_text = " ".join(
                        [
                            str(message.get("subject") or ""),
                            str(message.get("from_address") or ""),
                            str(message.get("content") or ""),
                            str(message.get("html_content") or ""),
                            str(detail.get("subject") or ""),
                            str(detail.get("content") or ""),
                            str(detail.get("html_content") or ""),
                            str(detail.get("raw_headers") or ""),
                        ]
                    ).strip()
                    search_text = self._decode_raw_content(search_text) or search_text
                    search_text = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                        "",
                        search_text,
                    )
                    if keyword and keyword.lower() not in search_text.lower():
                        continue

                    code = self._safe_extract(search_text, code_pattern)
                    if code:
                        if str(code).strip() in exclude_codes:
                            continue
                        self._record_verification_result(
                            message_id=message_id,
                            code=code,
                            phase=kwargs.get("phase") or "",
                            provider="GPTMailMailbox",
                        )
                        self._log(f"[GPTMail] 收到验证码: {code}")
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class OpenTrashMailMailbox(BaseMailbox):
    """OpenTrashMail 临时邮箱服务"""

    def __init__(
        self,
        api_url: str = "",
        domain: str = "",
        password: str = "",
        proxy: str = None,
    ):
        self.api = str(api_url or "").strip().rstrip("/")
        self.domain = self._normalize_domain(domain)
        self.password = str(password or "").strip()
        self.proxy = build_requests_proxy_config(proxy)

    @staticmethod
    def _normalize_domain(value: Any) -> str:
        domain = str(value or "").strip().lower()
        if domain.startswith("@"):
            domain = domain[1:]
        return domain

    @staticmethod
    def _generate_local_part() -> str:
        import string

        prefix = "".join(random.choices(string.ascii_lowercase, k=8))
        suffix = "".join(random.choices(string.digits, k=2))
        return f"{prefix}{suffix}"

    def _headers(self) -> dict[str, str]:
        return {"accept": "application/json, text/plain, */*"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        timeout: int = 15,
    ):
        import requests

        request_params = dict(params or {})
        if self.password and "password" not in request_params:
            request_params["password"] = self.password

        return requests.request(
            method,
            f"{self.api}{path}",
            params=request_params or None,
            json=None,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=timeout,
        )

    def _require_api(self) -> None:
        if not self.api:
            raise RuntimeError(
                "OpenTrashMail 未配置 API URL，请检查 opentrashmail_api_url"
            )

    def _build_email_path(self, email: str) -> str:
        from urllib.parse import quote

        return quote(str(email or "").strip(), safe="@")

    def _parse_random_email(self, html_text: str) -> str:
        import re

        text = str(html_text or "")
        if not text:
            return ""

        match = re.search(r"/address/([^\"'<>\s]+@[^\"'<>\s]+)", text, re.IGNORECASE)
        if match:
            return str(match.group(1) or "").strip()

        match = re.search(
            r"([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})",
            text,
            re.IGNORECASE,
        )
        if match:
            return str(match.group(1) or "").strip()
        return ""

    def _list_messages(self, email: str) -> list[dict[str, Any]]:
        self._require_api()
        response = self._request(
            "GET",
            f"/json/{self._build_email_path(email)}",
            timeout=10,
        )
        if response.status_code == 404:
            return []
        try:
            payload = response.json()
        except Exception as exc:
            preview = (response.text or "")[:200]
            raise RuntimeError(
                f"OpenTrashMail 收件箱返回非 JSON: HTTP {response.status_code} {preview}"
            ) from exc

        if response.status_code >= 400:
            if isinstance(payload, dict) and payload.get("error"):
                error = payload.get("error")
            else:
                error = response.text or f"HTTP {response.status_code}"
            raise RuntimeError(f"OpenTrashMail 收件箱查询失败: {str(error).strip()}")

        if not payload:
            return []

        messages: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            for message_id, item in payload.items():
                if not isinstance(item, dict):
                    continue
                message = dict(item)
                message.setdefault("id", str(message_id))
                messages.append(message)
        elif isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    messages.append(item)
        return messages

    def _get_message_detail(self, email: str, message_id: str) -> dict[str, Any]:
        self._require_api()
        response = self._request(
            "GET",
            f"/json/{self._build_email_path(email)}/{message_id}",
            timeout=10,
        )
        if response.status_code == 404:
            return {}
        try:
            payload = response.json()
        except Exception as exc:
            preview = (response.text or "")[:200]
            raise RuntimeError(
                f"OpenTrashMail 邮件详情返回非 JSON: HTTP {response.status_code} {preview}"
            ) from exc

        if response.status_code >= 400:
            if isinstance(payload, dict) and payload.get("error"):
                error = payload.get("error")
            else:
                error = response.text or f"HTTP {response.status_code}"
            raise RuntimeError(f"OpenTrashMail 邮件详情查询失败: {str(error).strip()}")

        return payload if isinstance(payload, dict) else {}

    def get_email(self) -> MailboxAccount:
        if self.domain:
            email = f"{self._generate_local_part()}@{self.domain}"
            self._log(f"[OpenTrashMail] 本地拼装邮箱: {email}")
            return MailboxAccount(
                email=email,
                account_id=email,
                extra={
                    "provider": "opentrashmail",
                    "domain": self.domain,
                    "local_address": True,
                },
            )

        self._require_api()
        response = self._request("GET", "/api/random", timeout=15)
        if response.status_code >= 400:
            raise RuntimeError(
                f"OpenTrashMail 随机邮箱生成失败: HTTP {response.status_code}"
            )

        email = self._parse_random_email(response.text)
        if not email:
            preview = (response.text or "")[:200]
            raise RuntimeError(f"OpenTrashMail 未能解析随机邮箱: {preview}")

        self._log(f"[OpenTrashMail] 生成邮箱: {email}")
        return MailboxAccount(
            email=email,
            account_id=email,
            extra={"provider": "opentrashmail"},
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {
                str(message.get("id"))
                for message in self._list_messages(account.email)
                if message.get("id") is not None
            }
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re

        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code) for code in (kwargs.get("exclude_codes") or set()) if code
        }

        def poll_once() -> Optional[str]:
            try:
                messages = self._list_messages(account.email)
                for message in messages:
                    message_id = str(message.get("id") or "").strip()
                    if not message_id or message_id in seen:
                        continue
                    seen.add(message_id)

                    detail = self._get_message_detail(account.email, message_id)
                    parsed = detail.get("parsed") if isinstance(detail, dict) else {}
                    if not isinstance(parsed, dict):
                        parsed = {}

                    decoded_raw = self._decode_raw_content(detail.get("raw") or "")
                    search_text = " ".join(
                        [
                            str(message.get("subject") or ""),
                            str(message.get("from") or ""),
                            str(message.get("body") or ""),
                            str(detail.get("from") or ""),
                            str(parsed.get("subject") or ""),
                            str(parsed.get("body") or ""),
                            str(parsed.get("htmlbody") or ""),
                            decoded_raw,
                        ]
                    ).strip()
                    search_text = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                        "",
                        search_text,
                    )
                    if keyword and keyword.lower() not in search_text.lower():
                        continue

                    code = self._safe_extract(search_text, code_pattern)
                    if code:
                        if str(code).strip() in exclude_codes:
                            continue
                        self._record_verification_result(
                            message_id=message_id,
                            code=code,
                            phase=kwargs.get("phase") or "",
                            provider="OpenTrashMailMailbox",
                        )
                        self._log(f"[OpenTrashMail] 收到验证码: {code}")
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class CFWorkerMailbox(BaseMailbox):
    """Cloudflare Worker 自建临时邮箱服务"""

    def __init__(
        self,
        api_url: str,
        admin_token: str = "",
        domain: str = "",
        domain_override: str = "",
        domains: Any = None,
        enabled_domains: Any = None,
        subdomain: str = "",
        random_subdomain: Any = False,
        fingerprint: str = "",
        custom_auth: str = "",
        proxy: str = None,
    ):
        self.api = api_url.rstrip("/")
        self.admin_token = admin_token
        self.domain = self._normalize_domain(domain)
        self.domain_override = self._normalize_domain(domain_override)
        self.domains = self._parse_domains(domains)
        raw_enabled_domains = self._parse_domains(enabled_domains)
        if self.domains:
            allowed = set(self.domains)
            self.enabled_domains = [d for d in raw_enabled_domains if d in allowed]
        else:
            self.enabled_domains = raw_enabled_domains
        self.subdomain = self._normalize_subdomain(subdomain)
        self.random_subdomain = self._to_bool(random_subdomain)
        self.fingerprint = fingerprint
        self.custom_auth = custom_auth
        self.proxy = build_requests_proxy_config(proxy)
        self._token = None

    def _headers(self) -> dict:
        h = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "x-admin-auth": self.admin_token,
        }
        if self.fingerprint:
            h["x-fingerprint"] = self.fingerprint
        if self.custom_auth:
            h["x-custom-auth"] = self.custom_auth
        return h

    def _ensure_api_configured(self) -> None:
        if not self.api:
            raise RuntimeError("CF Worker API URL 未配置")

    def _read_json(self, response, action: str):
        try:
            return response.json()
        except Exception:
            body = (response.text or "").strip()
            snippet = body[:200] if body else "<empty>"
            raise RuntimeError(
                f"CF Worker {action} 返回非 JSON 响应: HTTP {response.status_code}, body={snippet}"
            )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        payload: Optional[dict] = None,
        timeout: int = 15,
    ):
        import requests

        url = f"{self.api}{path}"
        response = requests.request(
            method,
            url,
            params=params,
            json=payload,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=timeout,
        )
        body = (response.text or "").strip()
        preview = body[:200] or "<empty>"

        if response.status_code >= 400:
            if "private site password" in body.lower():
                raise RuntimeError(
                    "CFWorker API 需要私有站点密码，请配置 cfworker_custom_auth"
                )
            raise RuntimeError(
                f"CFWorker API {path} 失败: HTTP {response.status_code} {preview}"
            )

        try:
            return response.json()
        except Exception as e:
            raise RuntimeError(
                f"CFWorker API {path} 返回非 JSON: HTTP {response.status_code} {preview}"
            ) from e

    def _generate_local_part(self) -> str:
        import string

        # 避免纯数字开头，提高邮箱格式“像真人”的程度
        prefix = "".join(random.choices(string.ascii_lowercase, k=6))
        suffix = "".join(random.choices(string.digits, k=4))
        return f"{prefix}{suffix}"

    @staticmethod
    def _normalize_domain(domain: Any) -> str:
        value = str(domain or "").strip().lower()
        if value.startswith("@"):
            value = value[1:]
        return value

    @staticmethod
    def _normalize_subdomain(value: Any) -> str:
        sub = str(value or "").strip().lower().strip(".")
        if sub.startswith("@"):
            sub = sub[1:]
        parts = [part for part in sub.split(".") if part]
        return ".".join(parts)

    @staticmethod
    def _to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        return text in {"1", "true", "yes", "on"}

    @classmethod
    def _parse_domains(cls, value: Any) -> list[str]:
        if not value:
            return []

        items: list[Any]
        if isinstance(value, (list, tuple, set)):
            items = list(value)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                items = parsed
            else:
                items = [
                    part for chunk in text.splitlines() for part in chunk.split(",")
                ]
        else:
            items = [value]

        domains: list[str] = []
        seen = set()
        for item in items:
            domain = cls._normalize_domain(item)
            if not domain or domain in seen:
                continue
            seen.add(domain)
            domains.append(domain)
        return domains

    def _pick_domain(self) -> str:
        if self.domain_override:
            return self.domain_override
        if self.enabled_domains:
            return random.choice(self.enabled_domains)
        return self.domain

    def _generate_subdomain_label(self, length: int = 6) -> str:
        import string

        alphabet = string.ascii_lowercase + string.digits
        return "".join(random.choices(alphabet, k=length))

    def _compose_domain(self, base_domain: str) -> str:
        domain = self._normalize_domain(base_domain)
        if not domain:
            return ""

        sub_parts: list[str] = []
        if self.random_subdomain:
            sub_parts.append(self._generate_subdomain_label())
        if self.subdomain:
            sub_parts.append(self.subdomain)

        if not sub_parts:
            return domain
        return f"{'.'.join(sub_parts)}.{domain}"

    def get_email(self) -> MailboxAccount:
        self._ensure_api_configured()
        name = self._generate_local_part()
        payload = {"enablePrefix": True, "name": name}
        selected_domain = self._compose_domain(self._pick_domain())
        if selected_domain:
            payload["domain"] = selected_domain
            self._log(f"[CFWorker] 本次使用域名: {selected_domain}")
        data = self._request_json(
            "POST", "/admin/new_address", payload=payload, timeout=15
        )
        email = data.get("email", data.get("address", ""))
        token = data.get("token", data.get("jwt", ""))
        if not email or not token:
            raise RuntimeError(
                f"CFWorker API /admin/new_address 返回缺少 email/jwt: {data}"
            )
        self._token = token
        print(
            f"[CFWorker] 生成邮箱: {email} token={token[:40] if token else 'NONE'}..."
        )
        return MailboxAccount(
            email=email,
            account_id=token,
            extra={"cfworker_domain": selected_domain} if selected_domain else None,
        )

    def _get_mails(self, email: str) -> list:
        self._ensure_api_configured()
        data = self._request_json(
            "GET",
            "/admin/mails",
            params={"limit": 20, "offset": 0, "address": email},
            timeout=10,
        )
        return data.get("results", data) if isinstance(data, dict) else data

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            mails = self._get_mails(account.email)
            return {str(m.get("id", "")) for m in mails}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re
        from datetime import datetime, timezone

        seen = set(before_ids or [])
        exclude_codes = set(kwargs.get("exclude_codes") or [])
        otp_sent_at = kwargs.get("otp_sent_at")
        otp_cutoff = float(otp_sent_at) - 2 if otp_sent_at else None

        def poll_once() -> Optional[str]:
            try:
                mails = self._get_mails(account.email)
                for mail in sorted(mails, key=lambda x: x.get("id", 0), reverse=True):
                    mid = str(mail.get("id", ""))
                    if not mid or mid in seen:
                        continue

                    created_at = str(mail.get("created_at", "") or "").strip()
                    if otp_cutoff and created_at:
                        try:
                            mail_ts = (
                                datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
                                .replace(tzinfo=timezone.utc)
                                .timestamp()
                            )
                            if mail_ts < otp_cutoff:
                                self._log(
                                    f"[CFWorker] \u8df3\u8fc7\u65e7\u90ae\u4ef6 id={mid} created_at={created_at}"
                                )
                                continue
                        except Exception:
                            pass

                    # 仅在通过时间边界筛选后再标记为已处理，避免边界邮件被过早加入 seen。
                    seen.add(mid)

                    raw = str(mail.get("raw", ""))
                    subject = str(mail.get("subject", ""))
                    search_text = f"{subject} {self._decode_raw_content(raw)}".strip()
                    search_text = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                        "",
                        search_text,
                    )
                    search_text = re.sub(r"m=\+\d+\.\d+", "", search_text)
                    search_text = re.sub(r"\bt=\d+\b", "", search_text)
                    if keyword and keyword.lower() not in search_text.lower():
                        continue

                    code = self._safe_extract(search_text, code_pattern)
                    if code:
                        self._record_verification_result(
                            message_id=mid,
                            code=code,
                            phase=kwargs.get("phase") or "",
                            provider="CFWorkerMailbox",
                            metadata={"created_at": created_at},
                        )
                        self._log(
                            f"[CFWorker] \u547d\u4e2d\u65b0\u9a8c\u8bc1\u7801 id={mid} created_at={created_at} code={code}"
                        )
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
            timeout_message=f"\u7b49\u5f85\u9a8c\u8bc1\u7801\u8d85\u65f6 ({timeout}s)",
        )


class MoeMailMailbox(BaseMailbox):
    """MoeMail (sall.cc) 邮箱服务 - 自动注册账号并生成临时邮箱"""

    def __init__(
        self, api_url: str = "https://sall.cc", api_key: str = "", proxy: str = None
    ):
        self.api = api_url.rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.proxy = build_requests_proxy_config(proxy)
        self._session_token = None
        self._email = None

    def _api_headers(self) -> dict:
        if not self.api_key:
            return {}
        return {"X-API-Key": self.api_key}

    def _register_and_login(self) -> str:
        import requests, random, string

        s = requests.Session()
        s.proxies = self.proxy
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        s.headers.update(
            {"user-agent": ua, "origin": self.api, "referer": f"{self.api}/zh-CN/login"}
        )
        s.headers.update(self._api_headers())
        # 注册
        username = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        password = "Test" + "".join(random.choices(string.digits, k=8)) + "!"
        print(f"[MoeMail] 注册账号: {username} / {password}")
        r_reg = s.post(
            f"{self.api}/api/auth/register",
            json={"username": username, "password": password, "turnstileToken": ""},
            timeout=15,
        )
        print(f"[MoeMail] 注册结果: {r_reg.status_code} {r_reg.text[:80]}")
        # 获取 CSRF
        csrf_r = s.get(f"{self.api}/api/auth/csrf", timeout=10)
        csrf = csrf_r.json().get("csrfToken", "")
        # 登录
        s.post(
            f"{self.api}/api/auth/callback/credentials",
            headers={"content-type": "application/x-www-form-urlencoded"},
            data=f"username={username}&password={password}&csrfToken={csrf}&redirect=false&callbackUrl={self.api}",
            allow_redirects=True,
            timeout=15,
        )
        self._session = s
        for cookie in s.cookies:
            if "session-token" in cookie.name:
                self._session_token = cookie.value
                print(f"[MoeMail] 登录成功")
                return cookie.value
        print(f"[MoeMail] 登录失败，cookies: {[c.name for c in s.cookies]}")
        return ""

    def get_email(self) -> MailboxAccount:
        # 每次调用都重新注册新账号，保证邮箱唯一
        self._session_token = None
        self._register_and_login()
        import random, string

        name = "".join(random.choices(string.ascii_letters + string.digits, k=8))
        # 获取可用域名列表，随机选一个
        domain = "sall.cc"
        try:
            cfg_r = self._session.get(
                f"{self.api}/api/config", headers=self._api_headers(), timeout=10
            )
            domains = [
                d.strip()
                for d in cfg_r.json().get("emailDomains", "sall.cc").split(",")
                if d.strip()
            ]
            if domains:
                domain = random.choice(domains)
        except Exception:
            pass
        r = self._session.post(
            f"{self.api}/api/emails/generate",
            headers=self._api_headers(),
            json={"name": name, "domain": domain, "expiryTime": 86400000},
            timeout=15,
        )
        data = r.json()
        self._email = data.get("email", data.get("address", ""))
        email_id = data.get("id", "")
        print(
            f"[MoeMail] 生成邮箱: {self._email} id={email_id} domain={domain} status={r.status_code}"
        )
        if not email_id:
            print(f"[MoeMail] 生成失败: {data}")
        if email_id:
            self._email_count = getattr(self, "_email_count", 0) + 1
        return MailboxAccount(email=self._email, account_id=str(email_id))

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            r = self._session.get(
                f"{self.api}/api/emails/{account.account_id}",
                headers=self._api_headers(),
                timeout=10,
            )
            return {str(m.get("id", "")) for m in r.json().get("messages", [])}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re

        seen = set(before_ids or [])

        def poll_once() -> Optional[str]:
            try:
                r = self._session.get(
                    f"{self.api}/api/emails/{account.account_id}",
                    headers=self._api_headers(),
                    timeout=10,
                )
                msgs = r.json().get("messages", [])
                for msg in msgs:
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    body = (
                        str(
                            msg.get("content")
                            or msg.get("text")
                            or msg.get("body")
                            or msg.get("html")
                            or ""
                        )
                        + " "
                        + str(msg.get("subject") or "")
                    )
                    body = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "", body
                    )
                    code = self._safe_extract(body, code_pattern)
                    if code:
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class LuckMailMailbox(BaseMailbox):
    """LuckMail 混合模式：ChatGPT 走购买邮箱，其他平台走订单接码"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        project_code: str = "",
        email_type: str = "",
        domain: str = "",
        proxy: str = None,
    ):
        if not base_url or not api_key:
            raise RuntimeError(
                "LuckMail 未配置：请在全局设置中填写 luckmail_base_url 和 luckmail_api_key"
            )
        from .luckmail import LuckMailClient

        self._client = LuckMailClient(
            base_url=base_url,
            api_key=api_key,
            proxy_url=proxy,
        )
        self._project_code = project_code
        self._email_type = email_type or None
        self._domain = domain or None
        self._order_no = None
        self._token = None
        self._email = None

    def _use_purchase_mode(self, account: MailboxAccount = None) -> bool:
        if (
            account
            and account.account_id
            and str(account.account_id).startswith("tok_")
        ):
            return True
        if self._token:
            return True
        return self._project_code == "openai"

    def _resolve_token(self, account: MailboxAccount = None) -> str:
        token = (account.account_id if account else "") or self._token
        if token:
            self._token = token
            return token

        email = (account.email if account else "") or self._email
        if not email:
            return ""

        try:
            purchases = self._client.user.get_purchases(
                page=1,
                page_size=100,
                keyword=email,
            )
        except Exception:
            return ""

        email_lower = str(email).strip().lower()
        for item in purchases.list:
            if str(item.email_address).strip().lower() == email_lower and item.token:
                self._token = item.token
                self._email = item.email_address
                return item.token
        return ""

    def _cancel_order_silently(self, order_no: str) -> None:
        if not order_no:
            return
        try:
            self._client.user.cancel_order(order_no)
            self._log(f"[LuckMail] 已取消订单: {order_no}")
        except Exception:
            pass

    def _extract_code_from_token_mails(
        self,
        token: str,
        code_pattern: str = None,
        before_ids: set = None,
        exclude_codes: set = None,
    ) -> Optional[str]:
        try:
            mail_list = self._client.user.get_token_mails(token)
        except Exception:
            return None

        seen = {str(mid) for mid in (before_ids or set())}
        excluded = {str(code) for code in (exclude_codes or set()) if code}
        for mail in mail_list.mails:
            message_id = str(mail.message_id or "")
            if message_id and message_id in seen:
                continue
            body = " ".join(
                [
                    str(mail.subject or ""),
                    str(mail.body or ""),
                    str(mail.html_body or ""),
                ]
            )
            code = self._safe_extract(body, code_pattern)
            if code and code in excluded:
                continue
            if code:
                return code
        return None

    def get_email(self) -> MailboxAccount:
        if not self._project_code:
            raise RuntimeError("LuckMail 未设置 project_code，无法创建邮箱")

        if self._use_purchase_mode():
            self._log(
                f"[LuckMail] 分支: ChatGPT + LuckMail -> 购买邮箱接口 "
                f"(project_code={self._project_code}, email_type={self._email_type or '-'}, domain={self._domain or '-'})"
            )
            try:
                result = self._client.user.purchase_emails(
                    project_code=self._project_code,
                    quantity=1,
                    email_type=self._email_type,
                    domain=self._domain,
                )
            except Exception as e:
                raise RuntimeError(f"LuckMail 购买邮箱失败: {e}") from e

            purchases = (result or {}).get("purchases") or []
            if not purchases:
                raise RuntimeError(f"LuckMail 购买邮箱返回为空: {result}")

            item = purchases[0]
            email = str(item.get("email_address") or "").strip()
            token = str(item.get("token") or "").strip()
            if not email or not token:
                raise RuntimeError(f"LuckMail 返回缺少 email/token: {item}")

            self._email = email
            self._token = token
            self._log(f"[LuckMail] 已购邮箱: {email}")
            if item.get("warranty_until"):
                self._log(f"[LuckMail] 质保到期: {item.get('warranty_until')}")
            return MailboxAccount(
                email=email,
                account_id=token,
                extra={
                    "provider": "luckmail",
                    "token": token,
                    "project_code": self._project_code,
                },
            )

        self._log(
            f"[LuckMail] 分支: 其他平台 + LuckMail -> 创建订单/订单接码 "
            f"(project_code={self._project_code}, email_type={self._email_type or '-'})"
        )
        try:
            body = {"project_code": self._project_code}
            if self._email_type:
                body["email_type"] = self._email_type
            order = self._client.user._sync_create_order(body)
        except Exception as e:
            raise RuntimeError(f"LuckMail 创建订单失败: {e}") from e
        self._order_no = order.order_no
        email = order.email_address
        self._email = email
        self._log(f"[LuckMail] 订单 {order.order_no} 分配邮箱: {email}")
        self._log(f"[LuckMail] 超时时间: {order.expired_at}")
        return MailboxAccount(email=email, account_id=order.order_no)

    def get_current_ids(self, account: MailboxAccount) -> set:
        if not self._use_purchase_mode(account):
            return set()
        token = self._resolve_token(account)
        if not token:
            return set()
        try:
            mail_list = self._client.user.get_token_mails(token)
            return {str(m.message_id) for m in (mail_list.mails or []) if m.message_id}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        if not self._use_purchase_mode(account):
            self._log("[LuckMail] 等验证码分支: 订单接码")
            order_no = account.account_id or self._order_no
            if not order_no:
                raise RuntimeError("LuckMail 未创建订单，无法等待验证码")

            def on_poll_order(result):
                self._log(f"[LuckMail] 轮询中... 状态: {result.status}")

            deadline = time.monotonic() + max(int(timeout or 0), 1)
            last_status = "pending"
            try:
                while time.monotonic() < deadline:
                    self._checkpoint()
                    remaining = max(1, int(deadline - time.monotonic()))
                    slice_timeout = min(remaining, 6)
                    try:
                        code_result = self._client.user._sync_wait_for_code(
                            order_no=order_no,
                            timeout=slice_timeout,
                            interval=3.0,
                            on_poll=on_poll_order,
                        )
                    except Exception as e:
                        raise TimeoutError(f"LuckMail 等待验证码失败: {e}") from e

                    last_status = str(code_result.status or "pending")
                    if code_result.status == "success" and code_result.verification_code:
                        code = code_result.verification_code
                        self._log(f"[LuckMail] 收到验证码: {code}")
                        return code
                    if code_result.status in {"cancelled", "timeout"}:
                        break
            except Exception:
                self._cancel_order_silently(order_no)
                raise

            self._cancel_order_silently(order_no)
            raise TimeoutError(
                f"LuckMail 等待验证码超时 ({timeout}s)，最终状态: {last_status}"
            )

        token = self._resolve_token(account)
        if not token:
            raise RuntimeError("LuckMail 未找到已购邮箱 Token，无法等待验证码")
        self._log("[LuckMail] 等验证码分支: 已购邮箱 Token 收码")

        exclude_codes = {
            str(code) for code in (kwargs.get("exclude_codes") or set()) if code
        }
        seen_message_ids = {str(mid) for mid in (before_ids or set()) if mid}
        if before_ids is None:
            seen_message_ids = self.get_current_ids(account)
            if seen_message_ids:
                self._log(
                    f"[LuckMail] 已建立旧邮件基线，先跳过 {len(seen_message_ids)} 封历史邮件"
                )

        saw_new_mail = False

        def poll_once() -> Optional[str]:
            nonlocal saw_new_mail
            found_new_mail = False
            try:
                mail_list = self._client.user.get_token_mails(token)
            except Exception as e:
                raise TimeoutError(f"LuckMail 等待验证码失败: {e}") from e

            for mail in mail_list.mails:
                message_id = str(mail.message_id or "").strip()
                if message_id and message_id in seen_message_ids:
                    continue

                found_new_mail = True
                saw_new_mail = True
                if message_id:
                    seen_message_ids.add(message_id)

                body = " ".join(
                    [
                        str(mail.subject or ""),
                        str(mail.body or ""),
                        str(mail.html_body or ""),
                    ]
                )
                code = self._safe_extract(body, code_pattern)
                if code:
                    self._record_verification_result(
                        message_id=message_id,
                        code=code,
                        phase=kwargs.get("phase") or "",
                        provider="LuckMailMailbox",
                    )
                    self._log(f"[LuckMail] 收到验证码: {code}")
                    return code

            self._log(
                f"[LuckMail] 轮询中... 新邮件: {'是' if found_new_mail else '否'}"
            )

            if found_new_mail:
                self._log("[LuckMail] 新邮件还不是可用验证码，继续等下一封...")
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
            timeout_message=(
                f"LuckMail 等待验证码超时 ({timeout}s)，最终状态: "
                f"has_new_mail={saw_new_mail}"
            ),
        )


class OutlookMailbox(BaseMailbox):
    """Outlook 本地账号池（IMAP / OAuth）"""

    def __init__(
        self,
        imap_server: str = "",
        imap_port: int | str = 993,
        token_endpoint: str = "",
        proxy: str = None,
    ):
        self._lock = threading.Lock()
        self._proxy = build_requests_proxy_config(proxy)
        self._imap_servers = []
        if imap_server:
            self._imap_servers.append(str(imap_server).strip())
        else:
            try:
                from services.chatgpt_core.constants import OUTLOOK_IMAP_SERVERS

                self._imap_servers.extend(
                    [
                        str(OUTLOOK_IMAP_SERVERS.get("NEW") or "").strip(),
                        str(OUTLOOK_IMAP_SERVERS.get("OLD") or "").strip(),
                    ]
                )
            except Exception:
                self._imap_servers.extend(
                    ["outlook.live.com", "outlook.office365.com"]
                )
        self._imap_servers = [
            host for host in self._imap_servers if isinstance(host, str) and host
        ]
        try:
            self._imap_port = int(imap_port or 993)
        except (TypeError, ValueError):
            self._imap_port = 993
        self._token_endpoint = str(token_endpoint or "").strip()

    def _acquire_pool_lock(self, timeout: float = 20.0):
        lock_path = os.path.join(
            tempfile.gettempdir(), "auto-chatgpt.outlook.lock"
        )
        lock_file = open(lock_path, "a+")
        start = time.monotonic()
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock_file
            except OSError:
                if time.monotonic() - start >= float(timeout or 0):
                    lock_file.close()
                    raise RuntimeError("Outlook 账号池锁获取超时")
                time.sleep(0.05)

    def _release_pool_lock(self, lock_file) -> None:
        if not lock_file:
            return
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                lock_file.close()
            except Exception:
                pass

    def _pop_account(self) -> dict:
        from sqlmodel import Session, select
        from core.db import engine, OutlookAccountModel

        lock_file = self._acquire_pool_lock()
        try:
            with self._lock:
                with Session(engine) as session:
                    account = (
                        session.exec(
                            select(OutlookAccountModel)
                            .where(OutlookAccountModel.enabled == True)
                            .order_by(OutlookAccountModel.id)
                        )
                        .first()
                    )
                    if not account:
                        raise RuntimeError("Outlook 账号池为空，请先在设置页批量导入")

                    payload = {
                        "id": account.id,
                        "email": account.email,
                        "password": account.password,
                        "client_id": account.client_id,
                        "refresh_token": account.refresh_token,
                    }
                    session.delete(account)
                    session.commit()
                    return payload
        finally:
            self._release_pool_lock(lock_file)

    def get_email(self) -> MailboxAccount:
        payload = self._pop_account()
        email = str(payload.get("email") or "").strip()
        if not email:
            raise RuntimeError("Outlook 账号邮箱为空")
        self._log(f"[Outlook] 取出账号: {email}（已从本地池移除）")
        return MailboxAccount(
            email=email,
            account_id=str(payload.get("id") or ""),
            extra={
                "provider": "outlook",
                "password": payload.get("password") or "",
                "client_id": payload.get("client_id") or "",
                "refresh_token": payload.get("refresh_token") or "",
            },
        )

    def _token_endpoints(self) -> list[str]:
        if self._token_endpoint:
            return [self._token_endpoint]
        try:
            from services.chatgpt_core.constants import MICROSOFT_TOKEN_ENDPOINTS

            return [
                MICROSOFT_TOKEN_ENDPOINTS.get("CONSUMERS", ""),
                MICROSOFT_TOKEN_ENDPOINTS.get("LIVE", ""),
                MICROSOFT_TOKEN_ENDPOINTS.get("COMMON", ""),
            ]
        except Exception:
            return [
                "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                "https://login.live.com/oauth20_token.srf",
                "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            ]

    def _fetch_oauth_token(self, *, email: str, client_id: str, refresh_token: str) -> str:
        if not client_id or not refresh_token:
            return ""
        import requests

        scope = ""
        try:
            from services.chatgpt_core.constants import MICROSOFT_SCOPES

            scope = str(MICROSOFT_SCOPES.get("IMAP_NEW") or "").strip()
        except Exception:
            scope = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"

        for endpoint in self._token_endpoints():
            endpoint = str(endpoint or "").strip()
            if not endpoint:
                continue
            payload = {
                "client_id": client_id,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
            if scope:
                payload["scope"] = scope
            try:
                resp = requests.post(
                    endpoint,
                    data=payload,
                    timeout=20,
                    proxies=self._proxy,
                )
                if resp.status_code >= 400:
                    continue
                data = resp.json() if resp.content else {}
                access_token = str(data.get("access_token") or "").strip()
                if access_token:
                    self._log(f"[Outlook] OAuth access token 获取成功: {email}")
                    return access_token
            except Exception:
                continue
        return ""

    def _imap_auth_oauth(self, imap_conn, *, email: str, access_token: str) -> None:
        auth_string = f"user={email}\x01auth=Bearer {access_token}\x01\x01"
        imap_conn.authenticate("XOAUTH2", lambda _: auth_string.encode("utf-8"))

    def _open_imap(self, account: MailboxAccount):
        import imaplib

        email_addr = str(account.email or "").strip()
        extra = account.extra or {}
        password = str(extra.get("password") or "").strip()
        client_id = str(extra.get("client_id") or "").strip()
        refresh_token = str(extra.get("refresh_token") or "").strip()

        access_token = ""
        if client_id and refresh_token:
            access_token = self._fetch_oauth_token(
                email=email_addr,
                client_id=client_id,
                refresh_token=refresh_token,
            )

        last_error = None
        for host in self._imap_servers:
            if not host:
                continue
            if access_token:
                try:
                    imap_conn = imaplib.IMAP4_SSL(host, self._imap_port, timeout=30)
                    self._imap_auth_oauth(
                        imap_conn, email=email_addr, access_token=access_token
                    )
                    return imap_conn
                except Exception as exc:
                    last_error = exc
                    try:
                        imap_conn.logout()
                    except Exception:
                        pass
            if password:
                try:
                    imap_conn = imaplib.IMAP4_SSL(host, self._imap_port, timeout=30)
                    imap_conn.login(email_addr, password)
                    return imap_conn
                except Exception as exc:
                    last_error = exc
                    try:
                        imap_conn.logout()
                    except Exception:
                        pass

        raise RuntimeError(f"Outlook IMAP 登录失败: {last_error}")

    def _decode_header_value(self, value: str) -> str:
        from email.header import decode_header

        if not value:
            return ""
        parts = decode_header(value)
        decoded = []
        for part, charset in parts:
            if isinstance(part, bytes):
                try:
                    decoded.append(part.decode(charset or "utf-8", errors="ignore"))
                except Exception:
                    decoded.append(part.decode("utf-8", errors="ignore"))
            else:
                decoded.append(str(part))
        return "".join(decoded)

    def _extract_message_text(self, message) -> str:
        subject = self._decode_header_value(message.get("Subject", ""))
        body_chunks = []
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                content_type = part.get_content_type()
                if content_type not in ("text/plain", "text/html"):
                    continue
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                try:
                    body_chunks.append(payload.decode(charset, errors="ignore"))
                except Exception:
                    body_chunks.append(payload.decode("utf-8", errors="ignore"))
        else:
            payload = message.get_payload(decode=True)
            if payload is None:
                payload = message.get_payload()
            if isinstance(payload, bytes):
                try:
                    body_chunks.append(payload.decode("utf-8", errors="ignore"))
                except Exception:
                    body_chunks.append(payload.decode("latin1", errors="ignore"))
            elif payload:
                body_chunks.append(str(payload))

        combined = (subject + " " + " ".join(body_chunks)).strip()
        return self._decode_raw_content(combined)

    def _extract_message_parts(self, message) -> tuple[str, str, str]:
        subject = self._decode_header_value(message.get("Subject", ""))
        text_chunks: list[str] = []
        html_chunks: list[str] = []
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                content_type = part.get_content_type()
                if content_type not in ("text/plain", "text/html"):
                    continue
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                try:
                    decoded = payload.decode(charset, errors="ignore")
                except Exception:
                    decoded = payload.decode("utf-8", errors="ignore")
                if content_type == "text/plain":
                    text_chunks.append(decoded)
                else:
                    html_chunks.append(decoded)
        else:
            payload = message.get_payload(decode=True)
            if payload is None:
                payload = message.get_payload()
            if isinstance(payload, bytes):
                try:
                    decoded = payload.decode("utf-8", errors="ignore")
                except Exception:
                    decoded = payload.decode("latin1", errors="ignore")
            else:
                decoded = str(payload or "")
            text_chunks.append(decoded)

        return subject, " ".join(text_chunks).strip(), " ".join(html_chunks).strip()

    def get_current_ids(self, account: MailboxAccount) -> set:
        imap_conn = None
        try:
            imap_conn = self._open_imap(account)
            imap_conn.select("INBOX", readonly=True)
            status, data = imap_conn.uid("search", None, "ALL")
            if status != "OK":
                return set()
            ids = data[0].split() if data and data[0] else []
            return {uid.decode("utf-8", errors="ignore") for uid in ids}
        except Exception:
            return set()
        finally:
            try:
                if imap_conn:
                    imap_conn.logout()
            except Exception:
                pass

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        from email import message_from_bytes
        from email.policy import default as email_default_policy

        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        keyword_lower = str(keyword or "").strip().lower()

        def poll_once() -> Optional[str]:
            imap_conn = None
            try:
                imap_conn = self._open_imap(account)
                imap_conn.select("INBOX", readonly=True)
                status, data = imap_conn.uid("search", None, "ALL")
                if status != "OK":
                    return None
                ids = data[0].split() if data and data[0] else []
                if len(ids) > 50:
                    ids = ids[-50:]
                for uid in ids:
                    uid_str = (
                        uid.decode("utf-8", errors="ignore")
                        if isinstance(uid, bytes)
                        else str(uid)
                    )
                    if not uid_str or uid_str in seen:
                        continue
                    seen.add(uid_str)
                    status, msg_data = imap_conn.uid("fetch", uid, "(RFC822)")
                    if status != "OK":
                        continue
                    raw = None
                    for item in msg_data or []:
                        if isinstance(item, tuple) and item[1]:
                            raw = item[1]
                            break
                    if not raw:
                        continue
                    msg = message_from_bytes(raw, policy=email_default_policy)
                    subject, text_part, html_part = self._extract_message_parts(msg)
                    combined = self._decode_raw_content(
                        " ".join([subject, text_part, html_part]).strip()
                    )
                    if keyword_lower and keyword_lower not in combined.lower():
                        continue
                    code, source = self._extract_verification_code_scored(
                        subject, text_part, html_part
                    )
                    if code and code not in exclude_codes:
                        if source:
                            self._log(f"[Outlook] 命中: {source} code={code}")
                        return code
                    code = self._safe_extract(combined, code_pattern)
                    if code:
                        if code in exclude_codes:
                            continue
                        return code
            except Exception:
                return None
            finally:
                try:
                    if imap_conn:
                        imap_conn.logout()
                except Exception:
                    pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=5,
            poll_once=poll_once,
        )


class FreemailMailbox(BaseMailbox):
    """
    Freemail 自建邮箱服务（基于 Cloudflare Worker）
    项目: https://github.com/idinging/freemail
    支持管理员令牌或账号密码两种认证方式
    """

    def __init__(
        self,
        api_url: str,
        admin_token: str = "",
        username: str = "",
        password: str = "",
        domain: str = "",
        proxy: str = None,
    ):
        self.api = api_url.rstrip("/")
        self.admin_token = admin_token
        self.username = username
        self.password = password
        self.domain = str(domain or "").strip().lstrip("@")
        self.proxy = build_requests_proxy_config(proxy)
        self._session = None
        self._email = None
        self._domains = None

    def _get_session(self):
        import requests

        s = requests.Session()
        s.proxies = self.proxy
        if self.admin_token:
            s.headers.update({"Authorization": f"Bearer {self.admin_token}"})
        elif self.username and self.password:
            s.post(
                f"{self.api}/api/login",
                json={"username": self.username, "password": self.password},
                timeout=15,
            )
        self._session = s
        return s

    def get_email(self) -> MailboxAccount:
        if not self._session:
            self._get_session()

        target_domain = self.domain
        domain_index = 0
        if target_domain:
            domains = self._ensure_domains()
            if domains:
                lookup = str(target_domain).lower()
                for idx, domain in enumerate(domains):
                    if str(domain or "").strip().lower() == lookup:
                        domain_index = idx
                        break

        params = {"domainIndex": domain_index} if target_domain else {}
        r = self._session.get(f"{self.api}/api/generate", params=params, timeout=15)
        data = r.json()
        email = str(data.get("email", "") or "")
        if target_domain and email and "@" in email:
            actual_domain = email.split("@", 1)[1].strip().lower()
            if actual_domain != target_domain.lower():
                self._log(
                    f"[Freemail] 指定域名 {target_domain} 未命中，实际返回 {actual_domain}"
                )

        self._email = email
        print(f"[Freemail] 生成邮箱: {email}")
        return MailboxAccount(email=email, account_id=email)

    def _ensure_domains(self) -> list:
        if self._domains is not None:
            return self._domains
        self._domains = []
        if not self._session:
            self._get_session()
        try:
            r = self._session.get(f"{self.api}/api/domains", timeout=15)
            payload = r.json()
            normalized = []
            def _append_domain(value):
                domain = str(value or "").strip().lstrip("@")
                if domain and domain not in normalized:
                    normalized.append(domain)
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict):
                        _append_domain(
                            item.get("domain")
                            or item.get("name")
                            or item.get("value")
                        )
                    else:
                        _append_domain(item)
            elif isinstance(payload, dict):
                candidates = payload.get("domains") or payload.get("data") or []
                if isinstance(candidates, list):
                    for item in candidates:
                        if isinstance(item, dict):
                            _append_domain(
                                item.get("domain")
                                or item.get("name")
                                or item.get("value")
                            )
                        else:
                            _append_domain(item)
            self._domains = normalized
        except Exception:
            self._domains = []
        return self._domains

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            r = self._session.get(
                f"{self.api}/api/emails",
                params={"mailbox": account.email, "limit": 50},
                timeout=10,
            )
            return {str(m["id"]) for m in r.json() if "id" in m}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        seen = set(before_ids or [])
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }

        def poll_once() -> Optional[str]:
            try:
                r = self._session.get(
                    f"{self.api}/api/emails",
                    params={"mailbox": account.email, "limit": 20},
                    timeout=10,
                )
                for msg in r.json():
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    # 直接用 verification_code 字段
                    code = str(msg.get("verification_code") or "").strip()
                    if code and code != "None":
                        if code in exclude_codes:
                            continue
                        return code
                    # 兜底：从 preview 提取
                    text = (
                        str(msg.get("preview", "")) + " " + str(msg.get("subject", ""))
                    )
                    code = self._safe_extract(text, code_pattern)
                    if code:
                        if code in exclude_codes:
                            continue
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )
