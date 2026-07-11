"""ChatGPT Web 会话退出。

该流程依据浏览器抓包：先访问 ``/auth/logout.data``，再以 NextAuth CSRF
cookie 发起 ``POST /api/auth/signout``。它是 Web Cookie 会话退出，不使用、
不撤销 OAuth refresh token，也不会在日志或返回值中暴露任何凭证。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from curl_cffi import requests as cffi_requests


CHATGPT_BASE_URL = "https://chatgpt.com"
LOGOUT_DATA_URL = f"{CHATGPT_BASE_URL}/auth/logout.data?account_switch_action=logout&_routes=routes%2Fauth.logout"
SIGNOUT_URL = f"{CHATGPT_BASE_URL}/api/auth/signout"
SESSION_COOKIE_NAMES = (
    "__Secure-next-auth.session-token",
    "__Secure-authjs.session-token",
    "next-auth.session-token",
    "authjs.session-token",
)
CSRF_COOKIE_NAMES = (
    "__Host-next-auth.csrf-token",
    "__Host-authjs.csrf-token",
    "next-auth.csrf-token",
    "authjs.csrf-token",
)


@dataclass(frozen=True)
class WebLogoutResult:
    success: bool
    status_code: int = 0
    error_message: str = ""
    used_session_cookie: bool = False


def _parse_cookie_header(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for item in str(cookie_header or "").split(";"):
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


def _cookie_jar_values(cookie_jar: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        iterable: Iterable[Any] = cookie_jar.jar
    except Exception:
        iterable = ()
    for cookie in iterable:
        name = str(getattr(cookie, "name", "") or "").strip()
        if name:
            values[name] = str(getattr(cookie, "value", "") or "")
    return values


def _has_session_cookie(cookies: dict[str, str]) -> bool:
    for name, value in cookies.items():
        if value and any(name == root or name.startswith(f"{root}.") for root in SESSION_COOKIE_NAMES):
            return True
    return False


def _csrf_token(cookies: dict[str, str]) -> str:
    for name in CSRF_COOKIE_NAMES:
        value = str(cookies.get(name) or "").strip()
        if value:
            # NextAuth 的 cookie 值为 ``token|hash``，请求体只发送 token 部分。
            return value.split("|", 1)[0].strip()
    return ""


def _seed_session_cookies(session: Any, cookies: dict[str, str]) -> None:
    for name, value in cookies.items():
        if not name or not value:
            continue
        try:
            session.cookies.set(name, value, domain=".chatgpt.com", path="/")
        except Exception:
            # curl_cffi CookieJar normally supports ``set``. Do not silently send
            # an unauthenticated logout when a non-compatible session is supplied.
            raise RuntimeError("无法载入账号保存的 ChatGPT cookies")


def logout_chatgpt_web_session(
    *,
    cookies: str = "",
    session_token: str = "",
    proxy_url: str | None = None,
    user_agent: str = "",
    accept_language: str = "",
    session: Any | None = None,
) -> WebLogoutResult:
    """退出一个已保存的 ChatGPT 网页 Cookie 会话。

    ``session_token`` 只是兼容旧账号记录的回退；完整 cookies（尤其是 CSRF
    cookie）才是退出请求的正式凭证。调用成功后，调用方应删除本地 cookies 和
    session_token，避免把已退出的会话继续保存。
    """
    saved_cookies = _parse_cookie_header(cookies)
    session_token = str(session_token or "").strip()
    if not _has_session_cookie(saved_cookies) and session_token:
        saved_cookies["__Secure-next-auth.session-token"] = session_token
    if not _has_session_cookie(saved_cookies):
        return WebLogoutResult(False, error_message="账号缺少 ChatGPT 网页 session cookie，无法执行退出")

    csrf_token = _csrf_token(saved_cookies)
    if not csrf_token:
        return WebLogoutResult(False, error_message="账号缺少 NextAuth CSRF cookie，无法安全执行退出")

    browser_session = session or cffi_requests.Session(
        impersonate="chrome120",
        proxy=str(proxy_url or "").strip() or None,
    )
    try:
        _seed_session_cookies(browser_session, saved_cookies)
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": str(accept_language or "en-US,en;q=0.9").strip() or "en-US,en;q=0.9",
            "user-agent": str(user_agent or "Mozilla/5.0").strip() or "Mozilla/5.0",
        }
        # 浏览器先加载该 data route；抓包中它会维护 logout/account-switch
        # 上下文并回写 session cookie 分片，随后 signout 才会清除它们。
        preflight = browser_session.get(
            LOGOUT_DATA_URL,
            headers={**headers, "referer": f"{CHATGPT_BASE_URL}/auth/logout?account_switch_action=logout"},
            timeout=30,
        )
        if int(getattr(preflight, "status_code", 0) or 0) >= 400:
            return WebLogoutResult(
                False,
                status_code=int(preflight.status_code),
                error_message=f"ChatGPT 退出预检失败: HTTP {preflight.status_code}",
                used_session_cookie=True,
            )

        merged_cookies = {**saved_cookies, **_cookie_jar_values(browser_session.cookies)}
        csrf_token = _csrf_token(merged_cookies) or csrf_token
        response = browser_session.post(
            SIGNOUT_URL,
            headers={
                **headers,
                "content-type": "application/x-www-form-urlencoded",
                "origin": CHATGPT_BASE_URL,
                "referer": f"{CHATGPT_BASE_URL}/auth/logout?account_switch_action=logout",
            },
            data={
                "csrfToken": csrf_token,
                "callbackUrl": f"{CHATGPT_BASE_URL}/",
                "json": "true",
            },
            timeout=30,
        )
        status_code = int(getattr(response, "status_code", 0) or 0)
        if 200 <= status_code < 300:
            return WebLogoutResult(True, status_code=status_code, used_session_cookie=True)
        return WebLogoutResult(
            False,
            status_code=status_code,
            error_message=f"ChatGPT 网页退出失败: HTTP {status_code or '未知'}",
            used_session_cookie=True,
        )
    except Exception as exc:
        return WebLogoutResult(False, error_message=f"ChatGPT 网页退出请求异常: {exc}", used_session_cookie=True)
