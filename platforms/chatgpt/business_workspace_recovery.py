"""Business workspace recovery for ChatGPT registration flow."""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

import requests
from urllib.parse import parse_qs, urlparse

from core.browser_runtime import ensure_browser_display_available, resolve_browser_headless
from core.proxy_utils import build_playwright_proxy_config
from services.team_embedded_backend import team_embedded_backend
from .oauth_client import OAuthClient

logger = logging.getLogger(__name__)


class BusinessWorkspaceRecovery:
    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        proxy: Optional[str] = None,
        browser_mode: str = "protocol",
        log_fn: Optional[Callable[[str], None]] = None,
    ):
        self.config = dict(config or {})
        self.proxy = proxy
        self.browser_mode = str(browser_mode or "protocol").strip().lower() or "protocol"
        self.log_fn = log_fn
        self.max_teams = self._read_int("business_recovery_max_teams", default=5, minimum=1, maximum=20)
        self.max_login_attempts = self._read_int("business_recovery_max_login_attempts", default=3, minimum=1, maximum=10)
        self.retry_delay_seconds = self._read_int("business_recovery_retry_delay_seconds", default=5, minimum=1, maximum=60)
        self.last_invite_failure_summary = ""

    def _log_stage(self, title: str) -> None:
        self._log(f"================ {title} ================", "debug")

    def _read_int(self, key: str, *, default: int, minimum: int, maximum: int) -> int:
        value = self.config.get(key, default)
        try:
            parsed = int(value)
        except Exception:
            parsed = int(default)
        return max(minimum, min(parsed, maximum))

    def _log(self, message: str, level: str = "info") -> None:
        if callable(self.log_fn):
            try:
                self.log_fn(message, level)
            except TypeError:
                try:
                    self.log_fn(message)
                except Exception:
                    pass
            except Exception:
                pass
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        elif level == "debug":
            logger.debug(message)
        else:
            logger.info(message)

    def is_enabled(self) -> bool:
        return team_embedded_backend.is_available()

    @staticmethod
    def _extract_invite_link_from_text(content: str) -> str:
        text = str(content or "")
        if not text:
            return ""

        candidates: list[str] = []
        for raw in re.findall(r"https?://[^\s\"'<>]+", text):
            url = raw.rstrip(")],.;")
            lower = url.lower()
            if "chatgpt.com/auth/login" not in lower:
                continue
            if "accept_wid=" not in lower and "inv_ws_name=" not in lower:
                continue
            candidates.append(url)

        return candidates[0] if candidates else ""

    @staticmethod
    def _extract_workspace_id_from_invite_url(invite_url: str) -> str:
        url = str(invite_url or "").strip()
        if not url:
            return ""
        try:
            params = parse_qs(urlparse(url).query or "")
        except Exception:
            return ""
        for key in ("accept_wId", "accept_wid", "wId", "wid"):
            value = str((params.get(key) or [""])[0] or "").strip()
            if value:
                return value
        return ""

    def _lookup_tempmail_mailbox_account(self, mailbox, email: str):
        target_email = str(email or "").strip().lower()
        if not target_email:
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
                self._log(
                    f"business recovery: TempMail 邮箱查询失败: {response.status_code} {response.text[:200]}",
                    "warning",
                )
                return None

            payload = response.json()
            items = payload.get("data") if isinstance(payload, dict) else []
            if not isinstance(items, list):
                return None

            from core.base_mailbox import MailboxAccount

            for item in items:
                full_address = str(item.get("full_address") or "").strip().lower()
                mailbox_id = str(item.get("id") or "").strip()
                if full_address != target_email or not mailbox_id:
                    continue
                return MailboxAccount(
                    email=target_email,
                    account_id=mailbox_id,
                    extra={"mailbox": item},
                )
        except Exception as exc:
            self._log(f"business recovery: TempMail 邮箱查询异常: {exc}", "warning")
        return None

    def _resolve_invite_mailbox_context(self, email: str, email_adapter=None):
        email_service = getattr(email_adapter, "email_service", None) if email_adapter else None
        provider = str(
            getattr(getattr(email_service, "service_type", None), "value", "")
            or self.config.get("mail_provider")
            or ""
        ).strip()
        before_ids = set(getattr(email_service, "_before_ids", None) or [])
        acct = getattr(email_service, "_acct", None) if email_service else None

        if provider not in {"tempmail_local", "tempmail_api"}:
            return None, None, before_ids

        try:
            from core.base_mailbox import MailboxAccount, create_mailbox

            mailbox = create_mailbox(provider, extra=self.config, proxy=self.proxy)
            if acct and getattr(acct, "account_id", ""):
                return (
                    mailbox,
                    MailboxAccount(
                        email=str(email or getattr(acct, "email", "") or "").strip(),
                        account_id=str(getattr(acct, "account_id", "") or "").strip(),
                        extra=getattr(acct, "extra", None),
                    ),
                    before_ids,
                )

            resolved_account = self._lookup_tempmail_mailbox_account(mailbox, email)
            return mailbox, resolved_account, before_ids
        except Exception as exc:
            self._log(f"business recovery: 初始化邀请邮箱上下文失败: {exc}", "warning")
            return None, None, before_ids

    def _wait_for_invite_link(self, mailbox, account, *, before_ids=None, timeout: int = 120) -> str:
        if mailbox is None or account is None or not getattr(account, "account_id", ""):
            return ""

        seen = set(before_ids or [])
        deadline = time.monotonic() + max(int(timeout or 0), 30)
        warned = False

        while time.monotonic() < deadline:
            try:
                mails = mailbox._list_emails(account.account_id)
                for idx, msg in enumerate(mails):
                    mid = mailbox._message_id(msg, idx)
                    if mid in seen:
                        continue

                    try:
                        detail = mailbox._get_email_detail(account.account_id, mid)
                    except Exception:
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
                    subject = str(detail.get("subject") or msg.get("subject") or "").strip()
                    looks_like_invite = bool(
                        re.search(
                            r"已邀请你使用\s*ChatGPT Business|invited you to use\s*ChatGPT Business|加入\s*ChatGPT Business\s*工作空间|accept_wId=|inv_ws_name=",
                            full_text,
                            re.I,
                        )
                    )
                    if not looks_like_invite:
                        if full_text.strip():
                            seen.add(mid)
                        continue

                    invite_link = self._extract_invite_link_from_text(full_text)
                    if invite_link:
                        seen.add(mid)
                        self._log(
                            f"business recovery: 命中邀请邮件 subject={subject or '(no-subject)'}"
                        )
                        return invite_link
            except Exception as exc:
                if not warned:
                    warned = True
                    self._log(f"business recovery: 轮询邀请邮件失败: {exc}", "warning")

            time.sleep(3)

        self._log("business recovery: 等待邀请邮件超时", "warning")
        return ""

    @staticmethod
    def _export_session_cookies_for_playwright(session) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if session is None:
            return result

        cookies = getattr(session, "cookies", None)
        if cookies is None:
            return result

        iterable = []
        jar = getattr(cookies, "jar", None)
        if jar is not None:
            try:
                iterable = list(jar)
            except Exception:
                iterable = []
        if not iterable:
            try:
                iterable = list(cookies)
            except Exception:
                iterable = []

        for item in iterable:
            name = getattr(item, "name", None) or getattr(item, "key", None)
            value = getattr(item, "value", None)
            domain = (getattr(item, "domain", "") or "chatgpt.com").lstrip(".")
            path = getattr(item, "path", "/") or "/"
            secure = bool(getattr(item, "secure", True))
            if not name or value is None:
                continue

            same_site = "Lax"
            rest = getattr(item, "_rest", {}) or {}
            for key in ("SameSite", "samesite"):
                if key in rest:
                    normalized = str(rest[key]).strip().lower()
                    if normalized.startswith("strict"):
                        same_site = "Strict"
                    elif normalized.startswith("none"):
                        same_site = "None"
                    else:
                        same_site = "Lax"
                    break

            result.append(
                {
                    "name": str(name),
                    "value": str(value),
                    "domain": domain,
                    "path": path,
                    "httpOnly": False,
                    "secure": secure,
                    "sameSite": same_site,
                }
            )
        return result

    def _accept_invite_link_with_browser(
        self,
        invite_url: str,
        *,
        user_agent: Optional[str] = None,
        accept_language: Optional[str] = None,
        browser_session=None,
    ) -> bool:
        invite_url = str(invite_url or "").strip()
        if not invite_url:
            return False

        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            self._log(f"business recovery: 浏览器 accept 不可用: {exc}", "warning")
            return False

        requested_headless = self.browser_mode != "headed"
        headless, reason = resolve_browser_headless(requested_headless)
        ensure_browser_display_available(headless)
        launch_kwargs: dict[str, Any] = {
            "headless": headless,
            "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        }
        proxy_config = build_playwright_proxy_config(self.proxy)
        if proxy_config:
            launch_kwargs["proxy"] = proxy_config

        cookie_payload = self._export_session_cookies_for_playwright(browser_session)
        self._log(
            "business recovery: 浏览器打开邀请链接"
            f" (cookies={len(cookie_payload)}, mode={'headless' if headless else 'headed'}, {reason})"
        )

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(**launch_kwargs)
                try:
                    context = browser.new_context(
                        user_agent=user_agent,
                        locale="en-US",
                        viewport={"width": 1440, "height": 900},
                        ignore_https_errors=True,
                        extra_http_headers={
                            "Accept-Language": str(accept_language or "en-US,en;q=0.9")
                        },
                    )
                    if cookie_payload:
                        context.add_cookies(cookie_payload)
                    page = context.new_page()
                    page.goto(invite_url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(5000)

                    body_hint = ""
                    try:
                        body_hint = (page.locator("body").inner_text(timeout=5000) or "")[:220]
                    except Exception:
                        body_hint = ""

                    # 经验上：页面即使出现“已加入 workspace”提示，仍可能停在 Get started 页。
                    # 若存在 Log in 按钮，点击一次更接近人工浏览器点击链路；
                    # 某些情况下会继续跳到 auth/openai 错页，但 invite accept 已经落地。
                    try:
                        login_button = page.get_by_test_id("login-button")
                        if login_button.count() > 0:
                            self._log("business recovery: 浏览器页面存在 Log in 按钮，尝试点击一次")
                            login_button.click(timeout=5000)
                            page.wait_for_timeout(15000)
                    except Exception as exc:
                        self._log(f"business recovery: 浏览器点击 Log in 按钮失败: {exc}", "warning")

                    final_url = str(page.url or "")
                    try:
                        body_hint = (page.locator("body").inner_text(timeout=5000) or body_hint)[:220]
                    except Exception:
                        pass
                    self._log(
                        "business recovery: 浏览器 accept 完成 -> "
                        f"url={final_url[:180] or invite_url[:180]}"
                    )
                    if body_hint:
                        self._log(f"business recovery: 浏览器页面提示: {body_hint}")
                    return True
                finally:
                    browser.close()
        except Exception as exc:
            self._log(f"business recovery: 浏览器 accept 失败: {exc}", "warning")
            return False

    def _accept_invite_link(
        self,
        invite_url: str,
        *,
        user_agent: Optional[str] = None,
        accept_language: Optional[str] = None,
        browser_session=None,
    ) -> bool:
        invite_url = str(invite_url or "").strip()
        if not invite_url:
            return False

        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "user-agent": user_agent
            or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
            "upgrade-insecure-requests": "1",
        }
        request_kwargs = {
            "headers": headers,
            "timeout": 30,
            "allow_redirects": True,
        }
        if self.proxy:
            request_kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}

        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(invite_url, **request_kwargs)
            status_code = int(getattr(response, "status_code", 0) or 0)
            final_url = str(getattr(response, "url", "") or "")
            self._log(
                f"business recovery: 直接访问邀请链接 -> {status_code}, url={final_url[:180] or invite_url[:180]}"
            )
        except Exception as exc:
            self._log(f"business recovery: 直接访问邀请链接失败: {exc}", "warning")
            status_code = 0
            final_url = ""

        needs_browser_followup = (
            status_code >= 400
            or "accept_wid=" in final_url.lower()
            or "auth/login" in final_url.lower()
        )
        if needs_browser_followup:
            return self._accept_invite_link_with_browser(
                invite_url,
                user_agent=user_agent,
                accept_language=accept_language,
                browser_session=browser_session,
            )

        return bool(status_code and status_code < 500)

    def _wait_for_joined(self, team_id: int, email: str, *, attempts: int = 4) -> bool:
        total_attempts = max(int(attempts or 0), 1)
        for attempt in range(total_attempts):
            joined = self.check_joined(team_id, email, force=False)
            if joined:
                return True
            if attempt < total_attempts - 1:
                time.sleep(self.retry_delay_seconds * (attempt + 1))
        return False

    def list_available_teams(self) -> List[Dict[str, Any]]:
        result = team_embedded_backend.get_available_teams()
        if result and result.get("success"):
            teams = result.get("teams") or []
            if isinstance(teams, list):
                return teams
        return []

    def invite_member(self, email: str, team_id: int) -> tuple[bool, bool, str]:
        result = team_embedded_backend.add_team_member(int(team_id), email, verify_sync=False)
        ok = bool(result and result.get("success"))
        error_text = ""
        if isinstance(result, dict):
            error_text = str(result.get("error") or result.get("message") or "").strip()
        lowered = error_text.lower()
        exhausted = (
            "maximum number of seats allowed" in lowered
            or "no seats available" in lowered
            or "team 已满" in lowered
            or "无法添加成员" in lowered
        )
        if ok:
            self._log(f"[邀请] 已发邀请 team={team_id}")
        elif exhausted:
            self._log(f"[邀请] team={team_id} 已无可用席位", "warning")
        else:
            self._log(f"[邀请] 发邀请失败 team={team_id}: {error_text or 'unknown'}", "warning")
        return ok, exhausted, error_text

    def check_joined(self, team_id: int, email: str, force: bool = False) -> bool:
        result = team_embedded_backend.check_member_status(int(team_id), email, force=bool(force))
        if result and result.get("success"):
            joined = bool(result.get("matched") or result.get("joined"))
            self._log(f"business recovery: joined={joined} team={team_id} force={force}", "debug")
            return joined
        return False

    def _build_oauth_client(self) -> OAuthClient:
        client = OAuthClient(
            self.config,
            proxy=self.proxy,
            verbose=False,
            browser_mode=self.browser_mode,
        )
        client._log = lambda msg: self._log(f"[business-recovery] {msg}")
        return client

    def _attempt_oauth_login(
        self,
        *,
        email: str,
        password: str,
        device_id: str,
        user_agent: Optional[str],
        sec_ch_ua: Optional[str],
        impersonate: Optional[str],
        browser_fingerprint: Optional[Dict[str, Any]],
        email_adapter=None,
        first_name: str,
        last_name: str,
        birthdate: str,
    ) -> tuple[Optional[Dict[str, Any]], OAuthClient]:
        oauth_client = self._build_oauth_client()
        tokens = oauth_client.login_and_get_tokens(
            email,
            password,
            device_id,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            impersonate=impersonate,
            browser_fingerprint=browser_fingerprint,
            skymail_client=email_adapter,
            prefer_passwordless_login=True,
            allow_phone_verification=False,
            force_new_browser=True,
            force_chatgpt_entry=False,
            screen_hint="login",
            force_password_login=False,
            complete_about_you_if_needed=True,
            first_name=first_name,
            last_name=last_name,
            birthdate=birthdate,
            login_source="business_workspace_recovery",
            stop_after_login=False,
            workspace_scope_preference="business",
        )
        return tokens, oauth_client

    def send_invite_only_for_account(
        self,
        *,
        email: str,
    ) -> Optional[Dict[str, Any]]:
        self.last_invite_failure_summary = ""
        available_teams = self.list_available_teams()
        if not available_teams:
            self.last_invite_failure_summary = "当前没有可邀请 team，结束注册阶段并进入激活阶段"
            self._log("business recovery: 没有可用 team", "warning")
            return None

        preferred_team_ids: list[int] = []
        raw_preferred_team_ids = self.config.get("chatgpt_deferred_invite_team_ids")
        if isinstance(raw_preferred_team_ids, list):
            for item in raw_preferred_team_ids:
                try:
                    team_id = int(item or 0)
                except Exception:
                    team_id = 0
                if team_id > 0 and team_id not in preferred_team_ids:
                    preferred_team_ids.append(team_id)
        elif raw_preferred_team_ids not in (None, ""):
            for item in str(raw_preferred_team_ids).split(","):
                try:
                    team_id = int(str(item or "").strip() or 0)
                except Exception:
                    team_id = 0
                if team_id > 0 and team_id not in preferred_team_ids:
                    preferred_team_ids.append(team_id)

        if not preferred_team_ids:
            try:
                preferred_team_id = int(self.config.get("chatgpt_deferred_invite_team_id") or 0)
            except Exception:
                preferred_team_id = 0
            if preferred_team_id > 0:
                preferred_team_ids = [preferred_team_id]

        if preferred_team_ids:
            team_map: Dict[int, Dict[str, Any]] = {}
            for team in available_teams:
                try:
                    team_id = int((team or {}).get("id") or 0)
                except Exception:
                    team_id = 0
                if team_id > 0:
                    team_map[team_id] = team
            ordered_teams = [team_map[team_id] for team_id in preferred_team_ids if team_id in team_map]
            if ordered_teams:
                available_teams = ordered_teams
                self._log(f"[邀请] 按预选顺序尝试 team_id={','.join(str(tid) for tid in preferred_team_ids if tid in team_map)}")
            else:
                self.last_invite_failure_summary = (
                    f"预选 team_id 列表当前不可用：{','.join(str(tid) for tid in preferred_team_ids)}"
                )
                self._log(f"[邀请] {self.last_invite_failure_summary}", "warning")
                return None

        self._log_stage("邀请阶段")
        attempted_team_ids: list[int] = []
        exhausted_team_ids: list[int] = []
        for team in available_teams[: self.max_teams]:
            team_id = team.get("id")
            if not team_id:
                continue
            team_id = int(team_id)
            attempted_team_ids.append(team_id)

            self._log(f"[邀请] 开始处理 team={team_id}")
            ok, exhausted, error_text = self.invite_member(email, team_id)
            if not ok:
                if exhausted and team_id not in exhausted_team_ids:
                    exhausted_team_ids.append(team_id)
                continue

            return {
                "team_id": team_id,
                "team_name": str(team.get("team_name") or team.get("name") or ""),
                "status": "invite_sent_pending_activation",
                "invite_sent": True,
                "source": "delayed_invite",
                "attempted_team_ids": attempted_team_ids,
                "exhausted_team_ids": exhausted_team_ids,
            }

        if attempted_team_ids:
            self.last_invite_failure_summary = (
                f"所有可用 team 都已不可邀请：{','.join(str(team_id) for team_id in attempted_team_ids)}，结束注册阶段并进入激活阶段"
            )
            self._log(
                f"[邀请] 所有可用 team_id 都已尝试仍不可邀请：{','.join(str(team_id) for team_id in attempted_team_ids)}",
                "warning",
            )
        else:
            self.last_invite_failure_summary = "所有可用 team 都已不可邀请，结束注册阶段并进入激活阶段"
            self._log("business recovery: 所有 team 尝试完毕仍未发出邀请", "warning")
        return None

    def fetch_invite_link_for_activation(
        self,
        *,
        email: str,
        email_adapter=None,
    ) -> Optional[Dict[str, Any]]:
        mailbox, mailbox_account, invite_before_ids = self._resolve_invite_mailbox_context(
            email,
            email_adapter=email_adapter,
        )
        if mailbox is None or mailbox_account is None:
            self._log("[邀请] 缺少邮箱上下文，无法读取邀请邮件", "warning")
            return None

        invite_before_ids = set(invite_before_ids or [])

        invite_link = self._wait_for_invite_link(
            mailbox,
            mailbox_account,
            before_ids=invite_before_ids,
            timeout=max(self.retry_delay_seconds * 6, 60),
        )
        if not invite_link:
            self._log("[邀请] 未等到邀请邮件链接", "warning")
            return None

        self._log("[邀请] 已收到邀请邮件")
        self._log("[邀请] 已提取邀请链接")
        return {
            "invite_url": invite_link,
            "workspace_id": self._extract_workspace_id_from_invite_url(invite_link),
        }

    def prepare_pending_invite_for_account(
        self,
        *,
        email: str,
        email_adapter=None,
    ) -> Optional[Dict[str, Any]]:
        return self.send_invite_only_for_account(email=email)

    def activate_pending_invite_for_account(
        self,
        *,
        email: str,
        password: str,
        invite_url: str,
        team_id: int,
        workspace_id: str = "",
        device_id: str = "",
        email_adapter=None,
        user_agent: Optional[str] = None,
        accept_language: Optional[str] = None,
        sec_ch_ua: Optional[str] = None,
        impersonate: Optional[str] = None,
        browser_fingerprint: Optional[Dict[str, Any]] = None,
        first_name: str = "",
        last_name: str = "",
        birthdate: str = "",
    ) -> Optional[Dict[str, Any]]:
        invite_url = str(invite_url or "").strip()
        resolved_workspace_id = str(workspace_id or "").strip()
        if not invite_url:
            self._log("[邀请] 开始读取邀请邮件并提取链接")
            invite_payload = self.fetch_invite_link_for_activation(
                email=email,
                email_adapter=email_adapter,
            )
            if not invite_payload:
                return None
            invite_url = str(invite_payload.get("invite_url") or "").strip()
            resolved_workspace_id = str(
                invite_payload.get("workspace_id") or resolved_workspace_id or ""
            ).strip()

        if not invite_url:
            self._log("[邀请] invite_url 缺失，无法激活", "warning")
            return None
        if not password:
            self._log("[邀请] 缺少密码，无法执行 auth 登录后消费邀请链接", "warning")
            return None

        login_tokens: Optional[Dict[str, Any]] = None
        login_oauth_client: Optional[OAuthClient] = None
        resolved_workspace_id = str(resolved_workspace_id or self._extract_workspace_id_from_invite_url(invite_url) or "")

        self._log("[邀请] 直接走 auth 登录后消费邀请链接")
        for login_attempt in range(self.max_login_attempts):
            if login_attempt > 0:
                wait_seconds = self.retry_delay_seconds * (login_attempt + 1)
                self._log(
                    f"business recovery: 邀请登录重试 {login_attempt + 1}/{self.max_login_attempts}，等待 {wait_seconds}s",
                    "debug",
                )
                time.sleep(wait_seconds)

            tokens, oauth_client = self._attempt_oauth_login(
                email=email,
                password=password,
                device_id=device_id,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                impersonate=impersonate,
                browser_fingerprint=browser_fingerprint,
                email_adapter=email_adapter,
                first_name=first_name,
                last_name=last_name,
                birthdate=birthdate,
            )
            if not tokens or getattr(oauth_client, "session", None) is None:
                last_error = str(getattr(oauth_client, "last_error", "") or "").strip()
                self._log(
                    f"[business] auth 登录失败: {last_error or 'unknown error'}",
                    "warning",
                )
                continue

            login_tokens = tokens
            login_oauth_client = oauth_client
            self._log("[business] auth 登录成功")
            self._log("[邀请] 使用 auth 登录状态消费邀请链接")
            invite_link_accepted = self._accept_invite_link(
                invite_url,
                user_agent=user_agent,
                accept_language=accept_language,
                browser_session=oauth_client.session,
            )
            if not invite_link_accepted:
                self._log("[邀请] auth 登录状态消费邀请链接失败", "warning")
                continue

            joined = self._wait_for_joined(int(team_id), email, attempts=6)
            if joined:
                self._log("[邀请] joined=True")
                return {
                    "team_id": int(team_id),
                    "workspace_id": resolved_workspace_id,
                    "invite_url": invite_url,
                    "joined": True,
                    "tokens": login_tokens,
                    "oauth_client": login_oauth_client,
                    "source": "business_recovery",
                }
            self._log("[邀请] auth 登录状态已打开邀请链接，但 joined 未同步", "warning")

        return None

    def join_business_for_account(
        self,
        *,
        email: str,
        password: str = "",
        device_id: str = "",
        email_adapter=None,
        user_agent: Optional[str] = None,
        accept_language: Optional[str] = None,
        sec_ch_ua: Optional[str] = None,
        impersonate: Optional[str] = None,
        browser_fingerprint: Optional[Dict[str, Any]] = None,
        first_name: str = "",
        last_name: str = "",
        birthdate: str = "",
        browser_session=None,
    ) -> Optional[Dict[str, Any]]:
        pending = self.send_invite_only_for_account(email=email)
        if not pending:
            return None
        result = self.activate_pending_invite_for_account(
            email=email,
            password=password,
            invite_url="",
            team_id=int(pending.get("team_id") or 0),
            workspace_id="",
            device_id=device_id,
            email_adapter=email_adapter,
            user_agent=user_agent,
            accept_language=accept_language,
            sec_ch_ua=sec_ch_ua,
            impersonate=impersonate,
            browser_fingerprint=browser_fingerprint,
            first_name=first_name,
            last_name=last_name,
            birthdate=birthdate,
        )
        if result:
            return result
        self._log(f"business recovery: 账号暂未 joined team={pending.get('team_id')}")
        self._log("business recovery: 所有 team 尝试完毕仍未 joined", "warning")
        return None

    def recover_workspace_for_account(
        self,
        *,
        email: str,
        password: str,
        device_id: str = "",
        user_agent: Optional[str] = None,
        sec_ch_ua: Optional[str] = None,
        impersonate: Optional[str] = None,
        browser_fingerprint: Optional[Dict[str, Any]] = None,
        email_adapter=None,
        first_name: str = "",
        last_name: str = "",
        birthdate: str = "",
    ) -> Optional[Dict[str, Any]]:
        join_result = self.join_business_for_account(
            email=email,
            password=password,
            device_id=device_id,
            email_adapter=email_adapter,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            impersonate=impersonate,
            browser_fingerprint=browser_fingerprint,
            first_name=first_name,
            last_name=last_name,
            birthdate=birthdate,
        )
        if not join_result:
            return None

        team_id = int(join_result.get("team_id") or 0)
        self._log("[business] 开始真实 auth 登录")
        for attempt in range(self.max_login_attempts):
            self._log(f"business recovery: OAuth 重试 {attempt + 1}/{self.max_login_attempts} team={team_id}", "debug")
            tokens, oauth_client = self._attempt_oauth_login(
                email=email,
                password=password,
                device_id=device_id,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                impersonate=impersonate,
                browser_fingerprint=browser_fingerprint,
                email_adapter=email_adapter,
                first_name=first_name,
                last_name=last_name,
                birthdate=birthdate,
            )
            if tokens:
                self._log("[business] OAuth 登录成功")
                return {
                    "tokens": tokens,
                    "oauth_client": oauth_client,
                    "team_id": team_id,
                    "workspace_id": join_result.get("workspace_id") or "",
                    "joined": True,
                }

            joined = self.check_joined(team_id, email, force=False)
            if joined:
                self._log(f"business recovery: 已 joined team={team_id}，追加一次 fresh OAuth", "debug")
                tokens, oauth_client = self._attempt_oauth_login(
                    email=email,
                    password=password,
                    device_id=device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    browser_fingerprint=browser_fingerprint,
                    email_adapter=email_adapter,
                    first_name=first_name,
                    last_name=last_name,
                    birthdate=birthdate,
                )
                if tokens:
                    self._log("[business] OAuth 登录成功")
                    return {
                        "tokens": tokens,
                        "oauth_client": oauth_client,
                        "team_id": team_id,
                        "workspace_id": join_result.get("workspace_id") or "",
                        "joined": True,
                    }
            else:
                self._log(f"business recovery: 账号暂未 joined team={team_id}")

            if attempt < self.max_login_attempts - 1:
                time.sleep(self.retry_delay_seconds * (attempt + 1))

        self._log("business recovery: 所有 team 尝试完毕仍未恢复 workspace", "warning")
        return None
