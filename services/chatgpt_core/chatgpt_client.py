"""
ChatGPT 注册客户端模块
使用 curl_cffi 模拟浏览器行为
"""

import time
import uuid
from urllib.parse import urlparse
from core.proxy_utils import build_requests_proxy_config

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    print("❌ 需要安装 curl_cffi: pip install curl_cffi")
    import sys

    sys.exit(1)

from .sentinel_token import build_sentinel_token
from .sentinel_browser import get_sentinel_token_via_browser
from .utils import (
    FlowState,
    apply_browser_fingerprint,
    build_browser_headers,
    coerce_browser_fingerprint,
    decode_jwt_payload,
    describe_flow_state,
    extract_flow_state,
    generate_datadog_trace,
    normalize_flow_url,
    random_delay,
)


class ChatGPTClient:
    """ChatGPT 注册客户端"""

    BASE = "https://chatgpt.com"
    AUTH = "https://auth.openai.com"

    def __init__(self, proxy=None, verbose=True, browser_mode="protocol", fingerprint=None):
        """
        初始化 ChatGPT 客户端

        Args:
            proxy: 代理地址
            verbose: 是否输出详细日志
            browser_mode: protocol | headless | headed
        """
        self.proxy = proxy
        self.verbose = verbose
        self.browser_mode = browser_mode or "protocol"
        self.fingerprint = coerce_browser_fingerprint(fingerprint)
        self._apply_fingerprint_meta(self.fingerprint)

        # 创建 session
        self.session = curl_requests.Session(impersonate=self.impersonate)

        if self.proxy:
            self.session.proxies = build_requests_proxy_config(self.proxy)

        apply_browser_fingerprint(self.session, self.fingerprint)
        self.last_registration_state = FlowState()
        self.last_registration_route_event = None
        self.last_homepage_probe = {
            "ok": False,
            "status_code": 0,
            "reason": "",
            "detail": "",
            "url": f"{self.BASE}/",
        }
        self.last_authorize_probe = {
            "ok": False,
            "status_code": 0,
            "reason": "",
            "detail": "",
            "url": "",
        }

    def _get_sentinel_token(self, flow: str, *, page_url: str | None = None):
        prefer_browser = flow in {"username_password_create", "oauth_create_account"}
        if prefer_browser:
            token = get_sentinel_token_via_browser(
                flow=flow,
                proxy=self.proxy,
                page_url=page_url,
                headless=self.browser_mode != "headed",
                device_id=self.device_id,
                user_agent=self.ua,
                sec_ch_ua=self.sec_ch_ua,
                chrome_full_version=self.chrome_full,
                accept_language=self.accept_language,
                platform_version=self.chrome_platform_version,
                viewport_width=self.viewport_width,
                viewport_height=self.viewport_height,
                log_fn=lambda msg: self._log(msg),
            )
            if token:
                self._log(f"{flow}: 已通过 Playwright SentinelSDK 获取 token")
                return token

        token = build_sentinel_token(
            self.session,
            self.device_id,
            flow=flow,
            user_agent=self.ua,
            sec_ch_ua=self.sec_ch_ua,
            impersonate=self.impersonate,
        )
        if token:
            self._log(f"{flow}: 已通过 HTTP PoW 获取 token")
        return token

    def _log(self, msg):
        """输出日志"""
        if self.verbose:
            print(f"  {msg}")

    def _browser_pause(self, low=0.15, high=0.45):
        """在 headed 模式下加入轻微停顿，模拟有头浏览器节奏。"""
        if self.browser_mode == "headed":
            random_delay(low, high)

    def _headers(
        self,
        url,
        *,
        accept,
        referer=None,
        origin=None,
        content_type=None,
        navigation=False,
        fetch_mode=None,
        fetch_dest=None,
        fetch_site=None,
        extra_headers=None,
    ):
        return build_browser_headers(
            url=url,
            user_agent=self.ua,
            sec_ch_ua=self.sec_ch_ua,
            chrome_full_version=self.chrome_full,
            sec_ch_platform_version=self.chrome_platform_version,
            accept=accept,
            accept_language=self.accept_language,
            referer=referer,
            origin=origin,
            content_type=content_type,
            navigation=navigation,
            fetch_mode=fetch_mode,
            fetch_dest=fetch_dest,
            fetch_site=fetch_site,
            headed=self.browser_mode == "headed",
            extra_headers=extra_headers,
        )

    def _apply_fingerprint_meta(self, fingerprint):
        self.fingerprint = fingerprint
        self.device_id = fingerprint.device_id
        self.accept_language = fingerprint.accept_language
        self.impersonate = fingerprint.impersonate
        self.chrome_major = fingerprint.chrome_major
        self.chrome_full = fingerprint.chrome_full_version
        self.ua = fingerprint.user_agent
        self.sec_ch_ua = fingerprint.sec_ch_ua
        self.chrome_platform_version = fingerprint.platform_version
        self.viewport_width = fingerprint.viewport_width
        self.viewport_height = fingerprint.viewport_height

    def _reset_session(self):
        """重建会话容器，但保持任务级指纹一致。"""
        self.session = curl_requests.Session(impersonate=self.impersonate)
        if self.proxy:
            self.session.proxies = build_requests_proxy_config(self.proxy)
        apply_browser_fingerprint(self.session, self.fingerprint)

    def _state_from_url(self, url, method="GET"):
        state = extract_flow_state(
            current_url=normalize_flow_url(url, auth_base=self.AUTH),
            auth_base=self.AUTH,
            default_method=method,
        )
        if method:
            state.method = str(method).upper()
        return state

    def _state_from_payload(self, data, current_url=""):
        return extract_flow_state(
            data=data,
            current_url=current_url,
            auth_base=self.AUTH,
        )

    def _state_signature(self, state: FlowState):
        return (
            state.page_type or "",
            state.method or "",
            state.continue_url or "",
            state.current_url or "",
        )

    def _is_registration_complete_state(self, state: FlowState):
        current_url = (state.current_url or "").lower()
        continue_url = (state.continue_url or "").lower()
        page_type = state.page_type or ""
        return (
            page_type in {"callback", "chatgpt_home", "oauth_callback"}
            or ("chatgpt.com" in current_url and "redirect_uri" not in current_url)
            or (
                "chatgpt.com" in continue_url
                and "redirect_uri" not in continue_url
                and page_type != "external_url"
            )
        )

    def _state_is_password_registration(self, state: FlowState):
        return state.page_type in {"create_account_password", "password"}

    def _state_is_email_otp(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        return (
            state.page_type == "email_otp_verification"
            or "email-verification" in target
            or "email-otp" in target
        )

    def _state_is_existing_account_login_route(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        return (
            state.page_type in {"login_password", "log_in_password"}
            or "log-in/password" in target
            or "login/password" in target
        )

    def _state_is_about_you(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        return state.page_type == "about_you" or "about-you" in target

    def _state_requires_navigation(self, state: FlowState):
        if (state.method or "GET").upper() != "GET":
            return False
        if state.page_type == "external_url" and state.continue_url:
            return True
        if state.continue_url and state.continue_url != state.current_url:
            return True
        return False

    def _remember_existing_account_login_route(
        self,
        email,
        state: FlowState,
        *,
        reason: str,
        stage: str = "register_complete_flow",
        allow_existing_account_login_route: bool = True,
    ):
        event = {
            "email": str(email or "").strip(),
            "stage": stage,
            "reason": str(reason or "").strip(),
            "state": describe_flow_state(state),
            "page_type": str(state.page_type or ""),
            "current_url": str(state.current_url or ""),
            "continue_url": str(state.continue_url or ""),
            "enabled": bool(allow_existing_account_login_route),
            "routed": bool(allow_existing_account_login_route),
            "blocked": not bool(allow_existing_account_login_route),
            "action": "login_recovery" if allow_existing_account_login_route else "skip_save",
        }
        self.last_registration_route_event = event
        self._log(
            "[已有账号] 注册状态机检测到邮箱已进入登录恢复路径: "
            f"email={event['email'] or '-'} reason={event['reason']} "
            f"state={event['state']}"
        )
        return event

    def _follow_flow_state(self, state: FlowState, referer=None):
        """跟随服务端返回的 continue_url，推进注册状态机。"""
        target_url = state.continue_url or state.current_url
        if not target_url:
            return False, "缺少可跟随的 continue_url"

        try:
            self._browser_pause()
            r = self.session.get(
                target_url,
                headers=self._headers(
                    target_url,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    referer=referer,
                    navigation=True,
                ),
                allow_redirects=True,
                timeout=30,
            )
            final_url = str(r.url)
            self._log(f"follow -> {r.status_code} {final_url}")

            content_type = (r.headers.get("content-type", "") or "").lower()
            if "application/json" in content_type:
                try:
                    next_state = self._state_from_payload(
                        r.json(), current_url=final_url
                    )
                except Exception:
                    next_state = self._state_from_url(final_url)
            else:
                next_state = self._state_from_url(final_url)

            self._log(f"follow state -> {describe_flow_state(next_state)}")
            return True, next_state
        except Exception as e:
            self._log(f"跟随 continue_url 失败: {e}")
            return False, str(e)

    def _get_cookie_value(self, name, domain_hint=None):
        """读取当前会话中的 Cookie。"""
        for cookie in self.session.cookies.jar:
            if cookie.name != name:
                continue
            if domain_hint and domain_hint not in (cookie.domain or ""):
                continue
            return cookie.value
        return ""

    def _get_cookie_chunks_value(self, name, domain_hint=None):
        """读取被 next-auth/authjs 拆分的 Cookie，并按序拼回完整值。"""
        chunks = {}
        prefix = f"{name}."
        for cookie in self.session.cookies.jar:
            cookie_name = cookie.name or ""
            if not cookie_name.startswith(prefix):
                continue
            if domain_hint and domain_hint not in (cookie.domain or ""):
                continue
            suffix = cookie_name[len(prefix):]
            if not suffix.isdigit():
                continue
            chunks[int(suffix)] = cookie.value

        if not chunks:
            return ""

        ordered_indexes = sorted(chunks)
        if ordered_indexes != list(range(len(ordered_indexes))):
            self._log(f"session cookie 分片不连续: {name} indexes={ordered_indexes}")
            return ""
        return "".join(str(chunks[index] or "") for index in ordered_indexes)

    def get_chatgpt_cookie_header(self):
        """导出当前 chatgpt.com 会话 Cookie，供支付/后续动作复用同一 Web 会话。"""
        pairs = []
        seen = set()
        for cookie in self.session.cookies.jar:
            domain = cookie.domain or ""
            name = cookie.name or ""
            value = cookie.value or ""
            if not name or not value:
                continue
            if "chatgpt.com" not in domain:
                continue
            key = (name, value)
            if key in seen:
                continue
            seen.add(key)
            pairs.append(f"{name}={value}")
        return "; ".join(pairs)

    def _describe_auth_cookie_names(self):
        """返回当前认证相关 Cookie 名称摘要，不输出 Cookie 值。"""
        names = []
        for cookie in self.session.cookies.jar:
            domain = cookie.domain or ""
            name = cookie.name or ""
            if (
                "chatgpt.com" not in domain
                and "openai.com" not in domain
                and "auth0.openai.com" not in domain
            ):
                continue
            if (
                "session-token" not in name
                and "callback-url" not in name
                and "csrf-token" not in name
                and "authjs" not in name
                and "next-auth" not in name
            ):
                continue
            names.append(f"{domain}:{name}")
        return ", ".join(sorted(set(names))) or "-"

    def get_next_auth_session_token(self):
        """获取 ChatGPT Web 会话 Cookie，兼容 next-auth / authjs 命名和分片。"""
        for cookie_name in (
            "__Secure-next-auth.session-token",
            "__Secure-authjs.session-token",
            "next-auth.session-token",
            "authjs.session-token",
        ):
            value = self._get_cookie_value(cookie_name, "chatgpt.com")
            if value:
                return value
            value = self._get_cookie_chunks_value(cookie_name, "chatgpt.com")
            if value:
                self._log(f"命中分片 session cookie: {cookie_name}")
                return value
        return ""

    def fetch_chatgpt_session(self, workspace_id: str = "", workspace_reason: str = "setCurrentAccount"):
        """请求 ChatGPT Session 接口并返回原始会话数据。"""
        url = f"{self.BASE}/api/auth/session"
        workspace_id = str(workspace_id or "").strip()
        if workspace_id:
            from urllib.parse import quote

            url = (
                f"{self.BASE}/api/auth/session?exchange_workspace_token=true"
                f"&workspace_id={quote(workspace_id, safe='')}"
                f"&reason={quote(str(workspace_reason or 'setCurrentAccount'), safe='')}"
            )
        self._browser_pause()
        response = self.session.get(
            url,
            headers=self._headers(
                url,
                accept="application/json",
                referer=f"{self.BASE}/",
                fetch_site="same-origin",
            ),
            timeout=30,
        )
        if response.status_code != 200:
            return False, f"/api/auth/session -> HTTP {response.status_code}"

        try:
            data = response.json()
        except Exception as exc:
            return False, f"/api/auth/session 返回非 JSON: {exc}"

        access_token = str(data.get("accessToken") or "").strip()
        if not access_token:
            return False, "/api/auth/session 未返回 accessToken"
        return True, data

    def reuse_session_and_get_tokens(self, workspace_id: str = "", workspace_reason: str = "setCurrentAccount"):
        """
        复用注册阶段已建立的 ChatGPT 会话，直接读取 Session / AccessToken。

        Returns:
            tuple[bool, dict|str]: 成功时返回标准化 token/session 数据；失败时返回错误信息。
        """
        state = self.last_registration_state or FlowState()
        self._log("步骤 1/4: 跟随注册回调 external_url ...")
        if state.page_type == "external_url" or self._state_requires_navigation(state):
            ok, followed = self._follow_flow_state(
                state,
                referer=state.current_url or f"{self.AUTH}/about-you",
            )
            if not ok:
                return False, f"注册回调落地失败: {followed}"
            self.last_registration_state = followed
        else:
            self._log("注册回调已落地，跳过额外跟随")

        self._log("步骤 2/4: 检查 ChatGPT session token（next-auth/authjs）...")
        session_cookie = self.get_next_auth_session_token()
        if not session_cookie:
            self._log(
                f"当前认证相关 Cookie 名称: {self._describe_auth_cookie_names()}"
            )
            return False, "缺少 ChatGPT session token（next-auth/authjs），注册回调可能未落地"

        self._log("步骤 3/4: 请求 ChatGPT /api/auth/session ...")
        ok, session_or_error = self.fetch_chatgpt_session(
            workspace_id=workspace_id,
            workspace_reason=workspace_reason,
        )
        if not ok:
            return False, session_or_error

        session_data = session_or_error
        access_token = str(session_data.get("accessToken") or "").strip()
        session_token = str(
            session_data.get("sessionToken") or session_cookie or ""
        ).strip()
        user = session_data.get("user") or {}
        account = session_data.get("account") or {}
        jwt_payload = decode_jwt_payload(access_token)
        auth_payload = jwt_payload.get("https://api.openai.com/auth") or {}

        account_id = (
            str(account.get("id") or "").strip()
            or str(auth_payload.get("chatgpt_account_id") or "").strip()
        )
        user_id = (
            str(user.get("id") or "").strip()
            or str(auth_payload.get("chatgpt_user_id") or "").strip()
            or str(auth_payload.get("user_id") or "").strip()
        )

        normalized = {
            "access_token": access_token,
            "session_token": session_token,
            "cookies": self.get_chatgpt_cookie_header(),
            "account_id": account_id,
            "user_id": user_id,
            "workspace_id": account_id,
            "expires": session_data.get("expires"),
            "user": user,
            "account": account,
            "auth_provider": session_data.get("authProvider"),
            "raw_session": session_data,
        }

        self._log("步骤 4/4: 已从复用会话中提取 accessToken")
        if account_id:
            self._log(f"Session Account ID: {account_id}")
        if user_id:
            self._log(f"Session User ID: {user_id}")
        return True, normalized

    def visit_homepage(self):
        """访问首页，建立 session"""
        self._log("访问 ChatGPT 首页...")
        url = f"{self.BASE}/"
        self.last_homepage_probe = {
            "ok": False,
            "status_code": 0,
            "reason": "",
            "detail": "",
            "url": url,
        }
        try:
            self._browser_pause()
            r = self.session.get(
                url,
                headers=self._headers(
                    url,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    navigation=True,
                ),
                allow_redirects=True,
                timeout=30,
            )
            final_url = str(getattr(r, "url", "") or url)
            status_code = int(getattr(r, "status_code", 0) or 0)
            if status_code == 200:
                self.last_homepage_probe = {
                    "ok": True,
                    "status_code": status_code,
                    "reason": "ok",
                    "detail": "",
                    "url": final_url,
                }
                return True
            detail = f"status={status_code} final_url={final_url}"
            self.last_homepage_probe = {
                "ok": False,
                "status_code": status_code,
                "reason": "non_200",
                "detail": detail[:500],
                "url": final_url,
            }
            self._log(f"访问首页失败: {detail}")
            return False
        except Exception as e:
            detail = str(e or "").strip()
            lowered = detail.lower()
            reason = "request_error"
            if "timed out" in lowered or "timeout" in lowered:
                reason = "timeout"
            elif "proxy" in lowered:
                reason = "proxy_error"
            elif "tls" in lowered or "ssl" in lowered or "handshake" in lowered:
                reason = "tls_error"
            elif "connection" in lowered or "connect" in lowered:
                reason = "connection_error"
            self.last_homepage_probe = {
                "ok": False,
                "status_code": 0,
                "reason": reason,
                "detail": detail[:500],
                "url": url,
            }
            self._log(f"访问首页失败 [{reason}]: {detail}")
            return False

    def get_csrf_token(self):
        """获取 CSRF token"""
        self._log("获取 CSRF token...")
        url = f"{self.BASE}/api/auth/csrf"
        try:
            r = self.session.get(
                url,
                headers=self._headers(
                    url,
                    accept="application/json",
                    referer=f"{self.BASE}/",
                    fetch_site="same-origin",
                ),
                timeout=30,
            )

            if r.status_code == 200:
                data = r.json()
                token = data.get("csrfToken", "")
                if token:
                    self._log(f"CSRF token: {token[:20]}...")
                    return token
        except Exception as e:
            self._log(f"获取 CSRF token 失败: {e}")

        return None

    def signin(self, email, csrf_token):
        """
        提交邮箱，获取 authorize URL

        Returns:
            str: authorize URL
        """
        self._log(f"提交邮箱: {email}")
        url = f"{self.BASE}/api/auth/signin/openai"

        params = {
            "prompt": "login",
            "ext-oai-did": self.device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "screen_hint": "login_or_signup",
            "login_hint": email,
        }

        form_data = {
            "callbackUrl": f"{self.BASE}/",
            "csrfToken": csrf_token,
            "json": "true",
        }

        try:
            self._browser_pause()
            r = self.session.post(
                url,
                params=params,
                data=form_data,
                headers=self._headers(
                    url,
                    accept="application/json",
                    referer=f"{self.BASE}/",
                    origin=self.BASE,
                    content_type="application/x-www-form-urlencoded",
                    fetch_site="same-origin",
                ),
                timeout=30,
            )

            if r.status_code == 200:
                data = r.json()
                authorize_url = data.get("url", "")
                if authorize_url:
                    self._log(f"获取到 authorize URL")
                    return authorize_url
        except Exception as e:
            self._log(f"提交邮箱失败: {e}")

        return None

    def authorize(self, url, max_retries=3):
        """
        访问 authorize URL，跟随重定向（带重试机制）
        这是关键步骤，建立 auth.openai.com 的 session

        Returns:
            str: 最终重定向的 URL
        """
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    self._log(
                        f"访问 authorize URL... (尝试 {attempt + 1}/{max_retries})"
                    )
                    time.sleep(1)  # 重试前等待
                else:
                    self._log("访问 authorize URL...")

                self._browser_pause()
                r = self.session.get(
                    url,
                    headers=self._headers(
                        url,
                        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        referer=f"{self.BASE}/",
                        navigation=True,
                    ),
                    allow_redirects=True,
                    timeout=30,
                )

                final_url = str(r.url)
                status_code = int(getattr(r, "status_code", 0) or 0)
                body = str(getattr(r, "text", "") or "")[:300]
                self.last_authorize_probe = {
                    "ok": status_code < 400,
                    "status_code": status_code,
                    "reason": "ok" if status_code < 400 else f"http_{status_code}",
                    "detail": body,
                    "url": final_url,
                }
                self._log(f"重定向到: {final_url}")
                return final_url

            except Exception as e:
                error_msg = str(e)
                self.last_authorize_probe = {
                    "ok": False,
                    "status_code": 0,
                    "reason": "request_error",
                    "detail": error_msg[:300],
                    "url": url,
                }
                is_tls_error = (
                    "TLS" in error_msg
                    or "SSL" in error_msg
                    or "curl: (35)" in error_msg
                )

                if is_tls_error and attempt < max_retries - 1:
                    self._log(
                        f"Authorize TLS 错误 (尝试 {attempt + 1}/{max_retries}): {error_msg[:100]}"
                    )
                    continue
                else:
                    self._log(f"Authorize 失败: {e}")
                    return ""

        return ""

    def callback(self, callback_url=None, referer=None):
        """完成注册回调"""
        self._log("执行回调...")
        url = callback_url or f"{self.AUTH}/api/accounts/authorize/callback"
        ok, _ = self._follow_flow_state(
            self._state_from_url(url),
            referer=referer or f"{self.AUTH}/about-you",
        )
        return ok

    def register_user(self, email, password):
        """
        注册用户（邮箱 + 密码）

        Returns:
            tuple: (success, message)
        """
        self._log(f"注册用户: {email}")
        url = f"{self.AUTH}/api/accounts/user/register"

        headers = self._headers(
            url,
            accept="application/json",
            referer=f"{self.AUTH}/create-account/password",
            origin=self.AUTH,
            content_type="application/json",
            fetch_site="same-origin",
        )
        headers.update(generate_datadog_trace())
        headers["oai-device-id"] = self.device_id

        sentinel_token = self._get_sentinel_token(
            "username_password_create",
            page_url=f"{self.AUTH}/create-account/password",
        )
        if sentinel_token:
            headers["openai-sentinel-token"] = sentinel_token

        payload = {
            "username": email,
            "password": password,
        }

        try:
            self._browser_pause()
            r = self.session.post(url, json=payload, headers=headers, timeout=30)

            if r.status_code == 200:
                data = r.json()
                self._log("注册成功")
                return True, "注册成功"
            else:
                try:
                    error_data = r.json()
                    error_msg = error_data.get("error", {}).get("message", r.text[:200])
                except:
                    error_msg = r.text[:200]
                self._log(f"注册失败: {r.status_code} - {error_msg}")
                return False, f"HTTP {r.status_code}: {error_msg}"

        except Exception as e:
            self._log(f"注册异常: {e}")
            return False, str(e)

    def send_email_otp(self, referer=None):
        """触发发送邮箱验证码"""
        self._log("触发发送验证码...")
        url = f"{self.AUTH}/api/accounts/email-otp/send"

        try:
            self._browser_pause()
            headers = self._headers(
                url,
                accept="application/json, text/plain, */*",
                referer=referer or f"{self.AUTH}/create-account/password",
                fetch_site="same-origin",
                extra_headers={
                    "oai-device-id": self.device_id,
                },
            )
            headers.update(generate_datadog_trace())
            r = self.session.get(
                url,
                headers=headers,
                allow_redirects=True,
                timeout=30,
            )
            self._log(f"验证码发送状态: {r.status_code}")
            if r.status_code != 200:
                self._log(f"验证码发送失败响应: {r.text[:180]}")
                return False

            try:
                payload = r.json()
            except Exception:
                payload = {}

            if isinstance(payload, dict) and payload:
                next_state = self._state_from_payload(payload, current_url=str(r.url) or url)
                self._log(f"验证码发送响应: {describe_flow_state(next_state)}")
            else:
                self._log("验证码发送响应: 非 JSON（按已触发处理）")
            return True
        except Exception as e:
            self._log(f"发送验证码失败: {e}")
            return False

    def verify_email_otp(self, otp_code, return_state=False):
        """
        验证邮箱 OTP 码

        Args:
            otp_code: 6位验证码

        Returns:
            tuple: (success, message)
        """
        self._log(f"验证 OTP 码: {otp_code}")
        url = f"{self.AUTH}/api/accounts/email-otp/validate"

        headers = self._headers(
            url,
            accept="application/json",
            referer=f"{self.AUTH}/email-verification",
            origin=self.AUTH,
            content_type="application/json",
            fetch_site="same-origin",
        )
        headers.update(generate_datadog_trace())

        payload = {"code": otp_code}

        try:
            self._browser_pause()
            r = self.session.post(url, json=payload, headers=headers, timeout=30)

            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    data = {}
                next_state = self._state_from_payload(
                    data, current_url=str(r.url) or f"{self.AUTH}/about-you"
                )
                self._log(f"验证成功 {describe_flow_state(next_state)}")
                return (True, next_state) if return_state else (True, "验证成功")
            else:
                error_code = ""
                error_msg = r.text[:500]
                try:
                    error_data = r.json() or {}
                    error_info = error_data.get("error") or {}
                    error_code = str(error_info.get("code") or "").strip()
                    error_msg = str(error_info.get("message") or error_msg).strip()
                except Exception:
                    pass
                self._log(f"验证失败: {r.status_code} - {error_msg}")
                detail = f"HTTP {r.status_code}"
                if error_code:
                    detail += f": {error_code}"
                if error_msg:
                    detail += f": {error_msg[:300]}"
                return False, detail

        except Exception as e:
            self._log(f"验证异常: {e}")
            return False, str(e)

    def create_account(self, first_name, last_name, birthdate, return_state=False):
        """
        完成账号创建（提交姓名和生日）

        Args:
            first_name: 名
            last_name: 姓
            birthdate: 生日 (YYYY-MM-DD)

        Returns:
            tuple: (success, message)
        """
        name = f"{first_name} {last_name}"
        self._log(f"完成账号创建: {name}")
        url = f"{self.AUTH}/api/accounts/create_account"

        sentinel_token = self._get_sentinel_token(
            "oauth_create_account",
            page_url=f"{self.AUTH}/about-you",
        )
        if sentinel_token:
            self._log("create_account: 已生成 sentinel token")
        else:
            self._log("create_account: 未生成 sentinel token，降级继续请求")

        headers = self._headers(
            url,
            accept="application/json",
            referer=f"{self.AUTH}/about-you",
            origin=self.AUTH,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={
                "oai-device-id": self.device_id,
            },
        )
        if sentinel_token:
            headers["openai-sentinel-token"] = sentinel_token
        headers.update(generate_datadog_trace())

        payload = {
            "name": name,
            "birthdate": birthdate,
        }

        try:
            self._browser_pause()
            r = self.session.post(url, json=payload, headers=headers, timeout=30)

            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    data = {}
                next_state = self._state_from_payload(
                    data, current_url=str(r.url) or self.BASE
                )
                self._log(f"账号创建成功 {describe_flow_state(next_state)}")
                return (True, next_state) if return_state else (True, "账号创建成功")
            else:
                error_code = ""
                error_msg = r.text[:200]
                try:
                    error_data = r.json() or {}
                    error_info = error_data.get("error") or {}
                    error_code = str(error_info.get("code") or "").strip()
                    error_msg = str(error_info.get("message") or error_msg).strip()
                except Exception:
                    pass

                detail = f"HTTP {r.status_code}"
                if error_code:
                    detail += f": {error_code}"
                elif error_msg:
                    detail += f": {error_msg}"

                self._log(f"创建失败: {detail} - {error_msg[:200]}")
                return False, detail

        except Exception as e:
            self._log(f"创建异常: {e}")
            return False, str(e)

    def register_complete_flow(
        self,
        email,
        password,
        first_name,
        last_name,
        birthdate,
        skymail_client,
        stop_before_about_you_submission=False,
        otp_wait_timeout=120,
        otp_resend_wait_timeout=90,
        otp_account_budget_timeout=None,
        allow_existing_account_login_route=True,
    ):
        """
        完整的注册流程（基于原版 run_register 方法）

        Args:
            email: 邮箱
            password: 密码
            first_name: 名
            last_name: 姓
            birthdate: 生日
            skymail_client: Skymail 客户端（用于获取验证码）

        Returns:
            tuple: (success, message)
        """
        from urllib.parse import urlparse

        self._log(
            "注册状态机参数: "
            f"stop_before_about_you_submission={'on' if stop_before_about_you_submission else 'off'}, "
            f"otp_wait_timeout={otp_wait_timeout}s, otp_resend_wait_timeout={otp_resend_wait_timeout}s, "
            f"otp_account_budget_timeout={otp_account_budget_timeout if otp_account_budget_timeout is not None else 'off'}, "
            f"existing_account_login_route={'on' if allow_existing_account_login_route else 'off'}"
        )

        self.last_registration_route_event = None

        try:
            otp_wait_timeout = max(30, int(otp_wait_timeout or 120))
        except Exception:
            otp_wait_timeout = 120
        try:
            otp_resend_wait_timeout = max(30, int(otp_resend_wait_timeout or 90))
        except Exception:
            otp_resend_wait_timeout = 90

        max_auth_attempts = 3
        final_url = ""
        final_path = ""

        for auth_attempt in range(max_auth_attempts):
            if auth_attempt > 0:
                self._log(f"预授权阶段重试 {auth_attempt + 1}/{max_auth_attempts}...")
                self._reset_session()

            # 1. 访问首页
            if not self.visit_homepage():
                if auth_attempt < max_auth_attempts - 1:
                    continue
                return False, "访问首页失败"

            # 2. 获取 CSRF token
            csrf_token = self.get_csrf_token()
            if not csrf_token:
                if auth_attempt < max_auth_attempts - 1:
                    continue
                return False, "获取 CSRF token 失败"

            # 3. 提交邮箱，获取 authorize URL
            auth_url = self.signin(email, csrf_token)
            if not auth_url:
                if auth_attempt < max_auth_attempts - 1:
                    continue
                return False, "提交邮箱失败"

            # 4. 访问 authorize URL（关键步骤！）
            final_url = self.authorize(auth_url)
            if not final_url:
                if auth_attempt < max_auth_attempts - 1:
                    continue
                return False, "Authorize 失败"

            final_path = urlparse(final_url).path
            self._log(f"Authorize → {final_path}")

            # /api/accounts/authorize 以前常对应 Cloudflare 403 中间页；但现在也会出现
            # status=200 停在该 URL、随后仍可继续注册状态机的情况。只有明确 403/429 或
            # /error 时才把它当拦截；否则交给下面的状态机 fallback 进入密码注册阶段。
            if "api/accounts/authorize" in final_path or final_path == "/error":
                authorize_probe = dict(getattr(self, "last_authorize_probe", {}) or {})
                authorize_status = int(authorize_probe.get("status_code") or 0)
                if final_path == "/error" or authorize_status in {403, 429}:
                    self._log(
                        f"检测到 Cloudflare/SPA 中间页，准备重试预授权: status={authorize_status or '-'} {final_url[:160]}..."
                    )
                    if auth_attempt < max_auth_attempts - 1:
                        continue
                    return False, f"预授权被拦截: {final_path}"
                self._log(
                    f"Authorize 停在 {final_path} status={authorize_status or '-'}，继续进入注册状态机 fallback"
                )

            break

        state = self._state_from_url(final_url)
        self._log(f"注册状态起点: {describe_flow_state(state)}")

        register_submitted = False
        otp_verified = False
        account_created = False
        seen_states = {}

        otp_send_attempts = 0

        def _otp_wait_budget_exhausted() -> bool:
            checker = getattr(skymail_client, "is_otp_wait_budget_exhausted", None)
            if not callable(checker):
                return False
            try:
                return checker() is True
            except Exception:
                return False

        for _ in range(12):
            signature = self._state_signature(state)
            seen_states[signature] = seen_states.get(signature, 0) + 1
            self._log(
                f"注册状态推进: step={sum(seen_states.values())} "
                f"state={describe_flow_state(state)} seen={seen_states[signature]}"
            )
            if seen_states[signature] > 2:
                return False, f"注册状态卡住: {describe_flow_state(state)}"

            if self._state_is_existing_account_login_route(state):
                event = self._remember_existing_account_login_route(
                    email,
                    state,
                    reason="login_password",
                    allow_existing_account_login_route=allow_existing_account_login_route,
                )
                return False, f"user_already_exists: existing_account_login_route:{event.get('reason') or 'login_password'}"

            if self._is_registration_complete_state(state):
                self.last_registration_state = state
                if (not register_submitted) and (not account_created):
                    event = self._remember_existing_account_login_route(
                        email,
                        state,
                        reason=(
                            "registration_completed_without_create_account_after_otp"
                            if otp_verified
                            else "registration_completed_without_create_account"
                        ),
                        allow_existing_account_login_route=allow_existing_account_login_route,
                    )
                    return False, f"user_already_exists: existing_account_login_route:{event.get('reason') or 'completed_without_create_account'}"
                self._log("✅ 注册流程完成")
                return True, "注册成功"

            if self._state_is_password_registration(state):
                self._log("全新注册流程")
                if register_submitted:
                    return False, "注册密码阶段重复进入"
                success, msg = self.register_user(email, password)
                if not success:
                    return False, f"注册失败: {msg}"
                register_submitted = True
                otp_send_attempts += 1
                self._log(f"发送注册验证码: attempt={otp_send_attempts}")
                if not self.send_email_otp(
                    referer=state.current_url or state.continue_url or f"{self.AUTH}/create-account/password"
                ):
                    self._log("发送验证码接口返回失败，继续等待邮箱中的验证码...")
                else:
                    self._log("发送注册验证码成功，进入收码阶段")
                state = self._state_from_url(f"{self.AUTH}/email-verification")
                continue

            if self._state_is_email_otp(state):
                if otp_send_attempts <= 0:
                    self._log("已进入邮箱验证码页，优先等待 OpenAI 自动发送的验证码；若超时再触发 email-otp/send")
                self._log("等待邮箱验证码...")
                otp_code = skymail_client.wait_for_verification_code(
                    email,
                    timeout=otp_wait_timeout,
                    phase="register_email_otp",
                    phase_label="注册阶段邮箱验证码",
                )
                if not otp_code:
                    if _otp_wait_budget_exhausted():
                        return False, "单账号验证码等待超时"
                    self._log(
                        "首次等待未收到验证码，尝试触发/重发一次 email-otp/send "
                        f"后再等待 {otp_resend_wait_timeout}s"
                    )
                    otp_send_attempts += 1
                    resend_ok = self.send_email_otp(
                        referer=state.current_url or state.continue_url or f"{self.AUTH}/email-verification"
                    )
                    if resend_ok:
                        self._log(f"重发验证码成功: attempt={otp_send_attempts}")
                    else:
                        self._log(f"重发验证码失败: attempt={otp_send_attempts}")
                    otp_code = skymail_client.wait_for_verification_code(
                        email,
                        timeout=otp_resend_wait_timeout,
                        phase="register_email_otp",
                        phase_label="注册阶段邮箱验证码",
                    )
                if not otp_code:
                    if _otp_wait_budget_exhausted():
                        return False, "单账号验证码等待超时"
                    return False, "未收到验证码"

                success, next_state = self.verify_email_otp(otp_code, return_state=True)
                if not success:
                    return False, f"验证码失败: {next_state}"
                otp_verified = True
                state = next_state
                self.last_registration_state = state
                continue

            if self._state_is_about_you(state):
                if stop_before_about_you_submission:
                    self.last_registration_state = state
                    self._log(
                        "注册链路已到 about_you，按 interrupt 流程停止。"
                        "下一步交由 OAuth 新会话提交姓名+生日。"
                    )
                    return True, "pending_about_you_submission"
                if account_created:
                    return False, "填写信息阶段重复进入"
                success, next_state = self.create_account(
                    first_name,
                    last_name,
                    birthdate,
                    return_state=True,
                )
                if not success:
                    return False, f"创建账号失败: {next_state}"
                account_created = True
                state = next_state
                self.last_registration_state = state
                continue

            if self._state_requires_navigation(state):
                success, next_state = self._follow_flow_state(
                    state,
                    referer=state.current_url or f"{self.AUTH}/about-you",
                )
                if not success:
                    return False, f"跳转失败: {next_state}"
                state = next_state
                self.last_registration_state = state
                continue

            if (
                (not register_submitted)
                and (not otp_verified)
                and (not account_created)
            ):
                self._log(
                    f"未知起始状态，回退为全新注册流程: {describe_flow_state(state)}"
                )
                state = self._state_from_url(f"{self.AUTH}/create-account/password")
                continue

            return False, f"未支持的注册状态: {describe_flow_state(state)}"

        return False, "注册状态机超出最大步数"
