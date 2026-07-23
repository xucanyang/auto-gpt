"""Playwright 版 Sentinel SDK token 获取辅助。"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from core.browser_runtime import (
    ensure_browser_display_available,
    resolve_browser_headless,
)
from core.playwright_proxy import playwright_proxy_context

from .sentinel_constants import (
    DEFAULT_SENTINEL_FRAME_URL,
    DEFAULT_SENTINEL_SDK_URL,
    PINNED_CHROMIUM_VERSION,
)
from .utils import build_sec_ch_ua_full_version_list, extract_chrome_full_version


@dataclass
class BrowserAccountCreateResult:
    """Result of the browser-owned about-you account creation transaction."""

    status_code: int = 0
    response_url: str = ""
    response_text: str = ""
    response_json: dict[str, Any] = field(default_factory=dict)
    cookies: list[dict[str, Any]] = field(default_factory=list)
    cookie_names: tuple[str, ...] = ()
    sentinel_field_lengths: dict[str, int] = field(default_factory=dict)
    cf_clearance_present: bool = False
    oai_sc_present: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= int(self.status_code or 0) < 300


def export_session_cookies_for_playwright(
    session: Any,
    *,
    fallback_domain: str = "auth.openai.com",
) -> list[dict[str, Any]]:
    """Export a requests-compatible cookie jar without losing domain scope."""
    cookies = getattr(session, "cookies", None)
    if cookies is None:
        return []

    jar = getattr(cookies, "jar", None)
    try:
        iterable = list(jar if jar is not None else cookies)
    except Exception:
        return []

    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in iterable:
        name = str(getattr(item, "name", None) or getattr(item, "key", None) or "").strip()
        value = getattr(item, "value", None)
        if not name or value is None:
            continue

        domain = str(getattr(item, "domain", "") or "").strip() or fallback_domain
        path = str(getattr(item, "path", "/") or "/").strip() or "/"
        key = (name, str(value), domain, path)
        if key in seen:
            continue
        seen.add(key)

        cookie: dict[str, Any] = {
            "name": name,
            "value": str(value),
            "domain": domain,
            "path": path,
            "secure": bool(getattr(item, "secure", False)),
        }
        expires = getattr(item, "expires", None)
        try:
            if expires is not None and float(expires) > 0:
                cookie["expires"] = float(expires)
        except (TypeError, ValueError):
            pass

        rest = getattr(item, "_rest", {}) or {}
        if any(str(key).lower() == "httponly" for key in rest):
            cookie["httpOnly"] = True
        same_site = next(
            (value for key, value in rest.items() if str(key).lower() == "samesite"),
            None,
        )
        normalized_same_site = str(same_site or "").strip().lower()
        if normalized_same_site in {"strict", "lax", "none"}:
            cookie["sameSite"] = normalized_same_site.title()
        result.append(cookie)
    return result


def merge_playwright_cookies_into_session(
    session: Any,
    cookies: list[dict[str, Any]],
    *,
    fallback_domain: str = "auth.openai.com",
) -> int:
    """Merge browser cookies back into the protocol session using their exact scope."""
    target = getattr(session, "cookies", None)
    setter = getattr(target, "set", None)
    if not callable(setter):
        return 0

    merged = 0
    seen: set[tuple[str, str, str, str]] = set()
    for item in cookies or []:
        name = str(item.get("name") or "").strip()
        value = item.get("value")
        if not name or value is None:
            continue
        domain = str(item.get("domain") or "").strip() or fallback_domain
        path = str(item.get("path") or "/").strip() or "/"
        key = (name, str(value), domain, path)
        if key in seen:
            continue
        seen.add(key)
        try:
            setter(
                name,
                str(value),
                domain=domain,
                path=path,
                secure=bool(item.get("secure")),
            )
            merged += 1
        except Exception:
            continue
    return merged


def _flow_page_url(flow: str) -> str:
    flow_name = str(flow or "").strip().lower()
    mapping = {
        "authorize_continue": "https://auth.openai.com/create-account",
        "username_password_create": "https://auth.openai.com/create-account/password",
        "password_verify": "https://auth.openai.com/log-in/password",
        "email_otp_validate": "https://auth.openai.com/email-verification",
        "oauth_create_account": "https://auth.openai.com/about-you",
    }
    return mapping.get(flow_name, "https://auth.openai.com/about-you")


def _sentinel_token_field_state(token: str) -> Optional[dict[str, bool]]:
    try:
        parsed = json.loads(str(token or ""))
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return {key: bool(parsed.get(key)) for key in ("p", "t", "c")}


def _thread_has_running_asyncio_loop() -> bool:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    return bool(loop and loop.is_running())


def run_sync_playwright_safely(
    fn: Callable[[], Any],
    *,
    logger: Optional[Callable[[str], None]] = None,
    label: str = "Playwright Sync API",
) -> Any:
    """Run Playwright sync API outside an already-running asyncio loop.

    Playwright's sync API intentionally refuses to start in a thread that already
    owns a running asyncio loop.  FastAPI/uvicorn and some worker wrappers can put
    our otherwise-synchronous registration code in exactly that situation, so move
    only the Playwright sync section into a short-lived clean thread.
    """
    if not _thread_has_running_asyncio_loop():
        return fn()

    log = logger or (lambda _msg: None)
    log(f"{label}: 当前线程已有 asyncio loop，切换到隔离线程执行")
    result_box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            result_box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - must propagate across thread boundary
            result_box["exc"] = exc

    thread = threading.Thread(target=_runner, name="sentinel-playwright-sync", daemon=True)
    thread.start()
    thread.join()
    if "exc" in result_box:
        raise result_box["exc"]
    return result_box.get("value")


def _evaluate_complete_sentinel_token(
    target: Any,
    *,
    flow: str,
    sdk_wait_timeout_ms: int,
    token_eval_timeout_ms: int,
    require_complete_signals: bool,
    logger: Callable[[str], None],
) -> Optional[str]:
    """Evaluate Sentinel in a Page or Frame and validate the returned signals."""
    logger("Sentinel Browser 阶段: wait SentinelSDK ready")
    try:
        target.wait_for_function(
            "() => typeof window.SentinelSDK !== 'undefined' && typeof window.SentinelSDK.token === 'function'",
            timeout=sdk_wait_timeout_ms,
        )
    except Exception:
        logger("Sentinel Browser 未发现 SDK，注入当前固定版本")
        target.evaluate(
            """
            async (sdkUrl) => {
                const existing = Array.from(document.scripts || [])
                    .some((item) => item.src === sdkUrl);
                if (existing) return;
                await new Promise((resolve, reject) => {
                    const script = document.createElement('script');
                    script.src = sdkUrl;
                    script.async = true;
                    script.onload = () => resolve(true);
                    script.onerror = () => reject(new Error(`Failed to load ${sdkUrl}`));
                    document.head.appendChild(script);
                });
            }
            """,
            DEFAULT_SENTINEL_SDK_URL,
        )
        target.wait_for_function(
            "() => typeof window.SentinelSDK !== 'undefined' && typeof window.SentinelSDK.token === 'function'",
            timeout=sdk_wait_timeout_ms,
        )
    logger("Sentinel Browser 阶段完成: wait SentinelSDK ready")

    logger(f"Sentinel Browser 阶段: evaluate SentinelSDK.token({flow})")
    result = target.evaluate(
        """
        async ({ flow, timeoutMs }) => {
            try {
                if (typeof window.SentinelSDK.init === 'function') {
                    await window.SentinelSDK.init(flow);
                }
                const token = await Promise.race([
                    window.SentinelSDK.token(flow),
                    new Promise((_, reject) =>
                        setTimeout(() => reject(new Error(`sentinel token timeout ${timeoutMs}ms`)), timeoutMs)
                    ),
                ]);
                return { success: true, token };
            } catch (e) {
                return {
                    success: false,
                    error: (e && (e.message || String(e))) || "unknown",
                };
            }
        }
        """,
        {"flow": flow, "timeoutMs": token_eval_timeout_ms},
    )
    logger("Sentinel Browser 阶段完成: evaluate SentinelSDK.token")

    if not result or not result.get("success") or not result.get("token"):
        logger(
            "Sentinel Browser 获取失败: "
            + str((result or {}).get("error") or "no result")
        )
        return None

    token = str(result["token"] or "").strip()
    if not token:
        logger("Sentinel Browser 返回空 token")
        return None

    field_state = _sentinel_token_field_state(token)
    if field_state is None:
        logger(f"Sentinel Browser 成功: len={len(token)}")
        if require_complete_signals:
            logger("Sentinel Browser 令牌格式不可验证，拒绝降级使用")
            return None
        return token

    logger(
        "Sentinel Browser 成功: "
        f"p={'✓' if field_state['p'] else '✗'} "
        f"t={'✓' if field_state['t'] else '✗'} "
        f"c={'✓' if field_state['c'] else '✗'}"
    )
    if require_complete_signals and not all(field_state.values()):
        logger("Sentinel Browser 令牌缺少完整 p/t/c 信号，拒绝降级使用")
        return None
    return token


def get_sentinel_token_via_browser(
    *,
    flow: str,
    proxy: Optional[str] = None,
    timeout_ms: int = 45000,
    page_url: Optional[str] = None,
    headless: bool = True,
    device_id: Optional[str] = None,
    user_agent: Optional[str] = None,
    sec_ch_ua: Optional[str] = None,
    chrome_full_version: Optional[str] = None,
    accept_language: Optional[str] = None,
    platform_version: Optional[str] = None,
    viewport_width: Optional[int] = None,
    viewport_height: Optional[int] = None,
    cookie_header: Optional[str] = None,
    require_complete_signals: bool = False,
    log_fn: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """通过浏览器直接调用 SentinelSDK.token(flow) 获取完整 token。"""
    logger = log_fn or (lambda _msg: None)
    return run_sync_playwright_safely(
        lambda: _get_sentinel_token_via_browser_sync(
            flow=flow,
            proxy=proxy,
            timeout_ms=timeout_ms,
            page_url=page_url,
            headless=headless,
            device_id=device_id,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            chrome_full_version=chrome_full_version,
            accept_language=accept_language,
            platform_version=platform_version,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            cookie_header=cookie_header,
            require_complete_signals=require_complete_signals,
            log_fn=logger,
        ),
        logger=logger,
        label="Sentinel Browser",
    )


def _get_sentinel_token_via_browser_sync(
    *,
    flow: str,
    proxy: Optional[str] = None,
    timeout_ms: int = 45000,
    page_url: Optional[str] = None,
    headless: bool = True,
    device_id: Optional[str] = None,
    user_agent: Optional[str] = None,
    sec_ch_ua: Optional[str] = None,
    chrome_full_version: Optional[str] = None,
    accept_language: Optional[str] = None,
    platform_version: Optional[str] = None,
    viewport_width: Optional[int] = None,
    viewport_height: Optional[int] = None,
    cookie_header: Optional[str] = None,
    require_complete_signals: bool = False,
    log_fn: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    logger = log_fn or (lambda _msg: None)

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        logger(f"Sentinel Browser 不可用: {e}")
        return None

    logical_page_url = str(page_url or _flow_page_url(flow)).strip() or _flow_page_url(flow)
    target_url = DEFAULT_SENTINEL_FRAME_URL
    effective_headless, reason = resolve_browser_headless(headless)
    ensure_browser_display_available(effective_headless)
    logger(
        f"Sentinel Browser 模式: {'headless' if effective_headless else 'headed'} ({reason})"
    )

    effective_user_agent = (
        user_agent
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{PINNED_CHROMIUM_VERSION} Safari/537.36"
    )
    effective_chrome_full = chrome_full_version or extract_chrome_full_version(effective_user_agent)
    effective_accept_language = str(accept_language or "en-US,en;q=0.9")
    effective_locale = (
        effective_accept_language.split(",", 1)[0].split(";", 1)[0].strip() or "en-US"
    )
    effective_platform_version = str(platform_version or "15.0.0").strip('"')
    effective_viewport_width = int(viewport_width or 1440)
    effective_viewport_height = int(viewport_height or 900)
    launch_timeout_ms = max(5000, min(int(timeout_ms or 45000), 20000))
    sdk_wait_timeout_ms = max(5000, min(int(timeout_ms or 45000), 15000))
    token_eval_timeout_ms = max(5000, min(int(timeout_ms or 45000), 15000))
    extra_http_headers = {
        "Accept-Language": effective_accept_language,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-arch": '"x86"',
        "sec-ch-ua-bitness": '"64"',
    }
    if sec_ch_ua:
        extra_http_headers["sec-ch-ua"] = sec_ch_ua
    if effective_chrome_full:
        extra_http_headers["sec-ch-ua-full-version"] = f'"{effective_chrome_full}"'
    if effective_platform_version:
        extra_http_headers["sec-ch-ua-platform-version"] = f'"{effective_platform_version}"'
    full_version_list = build_sec_ch_ua_full_version_list(sec_ch_ua, effective_chrome_full)
    if full_version_list:
        extra_http_headers["sec-ch-ua-full-version-list"] = full_version_list

    launch_args: dict[str, Any] = {
        "headless": effective_headless,
        "timeout": launch_timeout_ms,
        "args": [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    logger(
        f"Sentinel Browser 启动: flow={flow}, page={logical_page_url}, frame={target_url}"
    )
    logger(
        "Sentinel Browser 参数: "
        f"launch_timeout={launch_timeout_ms}ms, "
        f"goto_timeout={int(timeout_ms or 45000)}ms, "
        f"sdk_wait_timeout={sdk_wait_timeout_ms}ms, "
        f"token_eval_timeout={token_eval_timeout_ms}ms"
    )

    browser = None
    page = None
    stage = "bootstrap"

    stack = ExitStack()
    try:
        proxy_config = stack.enter_context(
            playwright_proxy_context(proxy, logger=logger)
        )
        if proxy_config:
            launch_args["proxy"] = proxy_config
        p = stack.enter_context(sync_playwright())
    except Exception as exc:
        stack.close()
        logger(f"Sentinel Browser 异常(stage=proxy_setup): {exc}")
        return None

    with stack:
        try:
            stage = "launch"
            logger("Sentinel Browser 阶段: launch chromium")
            browser = p.chromium.launch(**launch_args)
            logger("Sentinel Browser 阶段完成: launch chromium")

            stage = "new_context"
            logger("Sentinel Browser 阶段: create context")
            context = browser.new_context(
                viewport={"width": effective_viewport_width, "height": effective_viewport_height},
                user_agent=effective_user_agent,
                locale=effective_locale,
                extra_http_headers=extra_http_headers,
                ignore_https_errors=True,
            )
            logger("Sentinel Browser 阶段完成: create context")
            cookie_names: set[str] = set()
            if cookie_header:
                cookie_items = []
                target_parts = urlsplit(logical_page_url)
                cookie_url = (
                    f"{target_parts.scheme or 'https'}://{target_parts.netloc}/"
                    if target_parts.netloc
                    else logical_page_url
                )
                for part in str(cookie_header or "").split(";"):
                    text = part.strip()
                    if not text or "=" not in text:
                        continue
                    name, _, value = text.partition("=")
                    name = name.strip()
                    if not name:
                        continue
                    cookie_names.add(name)
                    cookie_items.append(
                        {
                            "name": name,
                            "value": value.strip(),
                            "url": cookie_url,
                            "secure": cookie_url.startswith("https://"),
                            "sameSite": "Lax",
                        }
                    )
                if cookie_items:
                    try:
                        context.add_cookies(cookie_items)
                        logger(f"Sentinel Browser 阶段完成: add cookie_header cookies ({len(cookie_items)})")
                    except Exception as cookie_exc:
                        logger(f"Sentinel Browser add cookie_header 失败: {cookie_exc}")
            if device_id:
                try:
                    logical_parts = urlsplit(logical_page_url)
                    logical_cookie_url = (
                        f"{logical_parts.scheme or 'https'}://{logical_parts.netloc}/"
                        if logical_parts.netloc
                        else "https://auth.openai.com/"
                    )
                    device_cookies = [
                        {
                            "name": "oai-did",
                            "value": str(device_id),
                            "url": "https://sentinel.openai.com/",
                            "secure": True,
                            "sameSite": "Lax",
                        }
                    ]
                    if "oai-did" not in cookie_names:
                        device_cookies.append(
                            {
                                "name": "oai-did",
                                "value": str(device_id),
                                "url": logical_cookie_url,
                                "secure": True,
                                "sameSite": "Lax",
                            }
                        )
                    context.add_cookies(device_cookies)
                    logger("Sentinel Browser 阶段完成: add device cookies")
                except Exception as cookie_exc:
                    logger(f"Sentinel Browser add cookies 失败: {cookie_exc}")

            stage = "new_page"
            logger("Sentinel Browser 阶段: create page")
            page = context.new_page()
            page.set_default_timeout(int(timeout_ms or 45000))
            page.set_default_navigation_timeout(int(timeout_ms or 45000))
            logger("Sentinel Browser 阶段完成: create page")

            stage = "goto"
            logger(f"Sentinel Browser 阶段: page.goto -> {target_url}")
            page.goto(target_url, wait_until="load", timeout=int(timeout_ms or 45000))
            logger(f"Sentinel Browser 阶段完成: page.goto -> {page.url}")

            stage = "wait_sentinel_sdk"
            return _evaluate_complete_sentinel_token(
                page,
                flow=flow,
                sdk_wait_timeout_ms=sdk_wait_timeout_ms,
                token_eval_timeout_ms=token_eval_timeout_ms,
                require_complete_signals=require_complete_signals,
                logger=logger,
            )
        except Exception as e:
            current_url = ""
            if page is not None:
                try:
                    current_url = str(page.url or "")
                except Exception:
                    current_url = ""
            logger(
                f"Sentinel Browser 异常(stage={stage}): {e}"
                + (f" | current_url={current_url}" if current_url else "")
            )
            return None
        finally:
            if browser is not None:
                try:
                    browser.close()
                    logger("Sentinel Browser 阶段完成: browser.close")
                except Exception as close_exc:
                    logger(f"Sentinel Browser browser.close 异常: {close_exc}")


def _cookie_applies_to_host(cookie: dict[str, Any], host: str) -> bool:
    domain = str(cookie.get("domain") or "").strip().lstrip(".").lower()
    target = str(host or "").strip().lower()
    return bool(domain and target and (target == domain or target.endswith(f".{domain}")))


def _add_cookies_best_effort(
    context: Any,
    cookies: list[dict[str, Any]],
    *,
    logger: Callable[[str], None],
) -> int:
    allowed = {
        "name",
        "value",
        "url",
        "domain",
        "path",
        "expires",
        "httpOnly",
        "secure",
        "sameSite",
        "partitionKey",
    }
    added = 0
    for item in cookies or []:
        normalized = {key: value for key, value in item.items() if key in allowed}
        if not normalized.get("name") or normalized.get("value") is None:
            continue
        if normalized.get("url"):
            normalized.pop("domain", None)
            normalized.pop("path", None)
        elif not normalized.get("domain"):
            continue
        try:
            context.add_cookies([normalized])
            added += 1
        except Exception as exc:
            logger(
                "Auth Browser Cookie 导入跳过: "
                f"name={normalized.get('name')} domain={normalized.get('domain') or normalized.get('url')} "
                f"error={exc}"
            )
    return added


def create_account_via_browser(
    *,
    name: str,
    birthdate: str,
    proxy: Optional[str] = None,
    page_url: str = "https://auth.openai.com/about-you",
    timeout_ms: int = 45000,
    headless: bool = True,
    device_id: Optional[str] = None,
    user_agent: Optional[str] = None,
    sec_ch_ua: Optional[str] = None,
    chrome_full_version: Optional[str] = None,
    accept_language: Optional[str] = None,
    platform_version: Optional[str] = None,
    viewport_width: Optional[int] = None,
    viewport_height: Optional[int] = None,
    cookies: Optional[list[dict[str, Any]]] = None,
    trace_headers: Optional[dict[str, str]] = None,
    stop_check: Optional[Callable[[], None]] = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> Optional[BrowserAccountCreateResult]:
    """Load Auth, obtain Sentinel, and submit create_account in one browser context."""
    logger = log_fn or (lambda _msg: None)
    return run_sync_playwright_safely(
        lambda: _create_account_via_browser_sync(
            name=name,
            birthdate=birthdate,
            proxy=proxy,
            page_url=page_url,
            timeout_ms=timeout_ms,
            headless=headless,
            device_id=device_id,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            chrome_full_version=chrome_full_version,
            accept_language=accept_language,
            platform_version=platform_version,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            cookies=cookies,
            trace_headers=trace_headers,
            stop_check=stop_check,
            log_fn=logger,
        ),
        logger=logger,
        label="Auth Browser Create Account",
    )


def _create_account_via_browser_sync(
    *,
    name: str,
    birthdate: str,
    proxy: Optional[str],
    page_url: str,
    timeout_ms: int,
    headless: bool,
    device_id: Optional[str],
    user_agent: Optional[str],
    sec_ch_ua: Optional[str],
    chrome_full_version: Optional[str],
    accept_language: Optional[str],
    platform_version: Optional[str],
    viewport_width: Optional[int],
    viewport_height: Optional[int],
    cookies: Optional[list[dict[str, Any]]],
    trace_headers: Optional[dict[str, str]],
    stop_check: Optional[Callable[[], None]],
    log_fn: Optional[Callable[[str], None]],
) -> Optional[BrowserAccountCreateResult]:
    logger = log_fn or (lambda _msg: None)
    check_stop = stop_check or (lambda: None)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        logger(f"Auth Browser 不可用: {exc}")
        return None

    logical_page_url = str(page_url or "https://auth.openai.com/about-you").strip()
    effective_headless, reason = resolve_browser_headless(headless)
    ensure_browser_display_available(effective_headless)
    effective_user_agent = (
        user_agent
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{PINNED_CHROMIUM_VERSION} Safari/537.36"
    )
    effective_chrome_full = chrome_full_version or extract_chrome_full_version(
        effective_user_agent
    )
    effective_accept_language = str(accept_language or "en-US,en;q=0.9")
    effective_locale = (
        effective_accept_language.split(",", 1)[0].split(";", 1)[0].strip() or "en-US"
    )
    effective_platform_version = str(platform_version or "15.0.0").strip('"')
    effective_viewport_width = int(viewport_width or 1440)
    effective_viewport_height = int(viewport_height or 900)
    effective_timeout_ms = max(10000, int(timeout_ms or 45000))
    sdk_wait_timeout_ms = max(5000, min(effective_timeout_ms, 15000))
    token_eval_timeout_ms = max(5000, min(effective_timeout_ms, 15000))

    extra_http_headers = {
        "Accept-Language": effective_accept_language,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-arch": '"x86"',
        "sec-ch-ua-bitness": '"64"',
    }
    if sec_ch_ua:
        extra_http_headers["sec-ch-ua"] = sec_ch_ua
    if effective_chrome_full:
        extra_http_headers["sec-ch-ua-full-version"] = f'"{effective_chrome_full}"'
    if effective_platform_version:
        extra_http_headers["sec-ch-ua-platform-version"] = (
            f'"{effective_platform_version}"'
        )
    full_version_list = build_sec_ch_ua_full_version_list(
        sec_ch_ua, effective_chrome_full
    )
    if full_version_list:
        extra_http_headers["sec-ch-ua-full-version-list"] = full_version_list

    launch_args: dict[str, Any] = {
        "headless": effective_headless,
        "timeout": min(effective_timeout_ms, 20000),
        "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    }
    logger(
        "Auth Browser 开户启动: "
        f"page={logical_page_url}, mode={'headless' if effective_headless else 'headed'} ({reason})"
    )

    browser = None
    page = None
    stage = "bootstrap"
    with ExitStack() as stack:
        try:
            proxy_config = stack.enter_context(
                playwright_proxy_context(proxy, logger=logger)
            )
            if proxy_config:
                launch_args["proxy"] = proxy_config
            p = stack.enter_context(sync_playwright())

            check_stop()
            stage = "launch"
            browser = p.chromium.launch(**launch_args)
            context = browser.new_context(
                viewport={
                    "width": effective_viewport_width,
                    "height": effective_viewport_height,
                },
                user_agent=effective_user_agent,
                locale=effective_locale,
                extra_http_headers=extra_http_headers,
                ignore_https_errors=True,
            )

            cookie_payload = list(cookies or [])
            if device_id:
                has_auth_device = any(
                    str(item.get("name") or "") == "oai-did"
                    and _cookie_applies_to_host(item, "auth.openai.com")
                    for item in cookie_payload
                )
                has_sentinel_device = any(
                    str(item.get("name") or "") == "oai-did"
                    and _cookie_applies_to_host(item, "sentinel.openai.com")
                    for item in cookie_payload
                )
                if not has_auth_device:
                    cookie_payload.append(
                        {
                            "name": "oai-did",
                            "value": str(device_id),
                            "url": "https://auth.openai.com/",
                            "secure": True,
                            "sameSite": "Lax",
                        }
                    )
                if not has_sentinel_device:
                    cookie_payload.append(
                        {
                            "name": "oai-did",
                            "value": str(device_id),
                            "url": "https://sentinel.openai.com/",
                            "secure": True,
                            "sameSite": "Lax",
                        }
                    )
            imported = _add_cookies_best_effort(
                context, cookie_payload, logger=logger
            )
            logger(f"Auth Browser 已按域导入 cookies={imported}")

            stage = "new_page"
            page = context.new_page()
            page.set_default_timeout(effective_timeout_ms)
            page.set_default_navigation_timeout(effective_timeout_ms)
            jsd_state = {"seen": False, "status": 0}

            def _observe_response(response: Any) -> None:
                try:
                    response_url = str(response.url or "")
                    if "/cdn-cgi/challenge-platform/" in response_url:
                        jsd_state["seen"] = True
                        jsd_state["status"] = int(response.status or 0)
                except Exception:
                    return

            page.on("response", _observe_response)

            stage = "goto_auth_about_you"
            check_stop()
            page.goto(
                logical_page_url,
                wait_until="domcontentloaded",
                timeout=effective_timeout_ms,
            )
            final_url = str(page.url or "")
            if urlsplit(final_url).hostname != "auth.openai.com":
                return BrowserAccountCreateResult(
                    response_url=final_url,
                    cookies=list(context.cookies()),
                    error=f"auth_about_you_redirected: {final_url[:240]}",
                )

            stage = "wait_auth_browser_signals"
            cf_clearance_present = False
            for _ in range(16):
                check_stop()
                current_cookies = list(context.cookies())
                cf_clearance_present = any(
                    str(item.get("name") or "") == "cf_clearance"
                    and str(item.get("value") or "").strip()
                    for item in current_cookies
                )
                if cf_clearance_present:
                    break
                page.wait_for_timeout(500)
            logger(
                "Auth Browser 页面信号: "
                f"cf_jsd={'✓' if jsd_state['seen'] and jsd_state['status'] < 400 else '✗'} "
                f"cf_clearance={'✓' if cf_clearance_present else '✗'}"
            )

            stage = "find_sentinel_frame"

            def _find_frame() -> Any:
                for candidate in page.frames:
                    if "sentinel.openai.com/backend-api/sentinel/frame.html" in str(
                        candidate.url or ""
                    ):
                        return candidate
                return None

            sentinel_frame = None
            for _ in range(10):
                check_stop()
                sentinel_frame = _find_frame()
                if sentinel_frame is not None:
                    break
                page.wait_for_timeout(500)
            if sentinel_frame is None:
                logger("Auth Browser 未发现现成 Sentinel iframe，按官方 frame URL 注入")
                page.evaluate(
                    """
                    (frameUrl) => {
                        let frame = document.querySelector('iframe[data-codex-sentinel-frame="1"]');
                        if (!frame) {
                            frame = document.createElement('iframe');
                            frame.dataset.codexSentinelFrame = '1';
                            frame.style.position = 'fixed';
                            frame.style.width = '1px';
                            frame.style.height = '1px';
                            frame.style.opacity = '0';
                            frame.style.pointerEvents = 'none';
                            document.body.appendChild(frame);
                        }
                        frame.src = frameUrl;
                    }
                    """,
                    DEFAULT_SENTINEL_FRAME_URL,
                )
                for _ in range(20):
                    check_stop()
                    sentinel_frame = _find_frame()
                    if sentinel_frame is not None:
                        break
                    page.wait_for_timeout(500)
            if sentinel_frame is None:
                return BrowserAccountCreateResult(
                    response_url=final_url,
                    cookies=list(context.cookies()),
                    cf_clearance_present=cf_clearance_present,
                    error="sentinel_frame_unavailable",
                )

            stage = "sentinel_token"
            token = _evaluate_complete_sentinel_token(
                sentinel_frame,
                flow="oauth_create_account",
                sdk_wait_timeout_ms=sdk_wait_timeout_ms,
                token_eval_timeout_ms=token_eval_timeout_ms,
                require_complete_signals=True,
                logger=logger,
            )
            if not token:
                return BrowserAccountCreateResult(
                    response_url=final_url,
                    cookies=list(context.cookies()),
                    cf_clearance_present=cf_clearance_present,
                    error="sentinel_browser_unavailable",
                )

            parsed_token = json.loads(token)
            field_lengths = {
                key: len(str(parsed_token.get(key) or "")) for key in ("p", "t", "c")
            }
            before_create_cookies = list(context.cookies())
            before_cookie_names = tuple(
                sorted(
                    {
                        str(item.get("name") or "")
                        for item in before_create_cookies
                        if item.get("name")
                    }
                )
            )
            oai_sc_present = "oai-sc" in before_cookie_names
            logger(
                "Auth Browser 开户前上下文: "
                f"cookies={','.join(before_cookie_names)} "
                f"sentinel_lengths={field_lengths}"
            )

            stage = "create_account_fetch"
            check_stop()
            allowed_trace_headers = {
                str(key): str(value)
                for key, value in (trace_headers or {}).items()
                if str(key).lower()
                in {
                    "traceparent",
                    "tracestate",
                    "x-datadog-origin",
                    "x-datadog-parent-id",
                    "x-datadog-sampling-priority",
                    "x-datadog-trace-id",
                }
            }
            fetch_result = page.evaluate(
                """
                async ({ token, name, birthdate, traceHeaders, invocationId, timeoutMs }) => {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), timeoutMs);
                    try {
                        const response = await fetch('/api/accounts/create_account', {
                            method: 'POST',
                            credentials: 'include',
                            cache: 'no-store',
                            redirect: 'manual',
                            signal: controller.signal,
                            headers: {
                                accept: 'application/json',
                                'content-type': 'application/json',
                                'openai-sentinel-token': token,
                                'x-access-flow-invocation-id': invocationId,
                                ...traceHeaders,
                            },
                            body: JSON.stringify({ name, birthdate }),
                        });
                        return {
                            status: response.status,
                            url: response.url,
                            text: await response.text(),
                        };
                    } catch (error) {
                        return {
                            status: 0,
                            url: location.href,
                            text: '',
                            error: (error && (error.message || String(error))) || 'unknown',
                        };
                    } finally {
                        clearTimeout(timer);
                    }
                }
                """,
                {
                    "token": token,
                    "name": str(name or "").strip(),
                    "birthdate": str(birthdate or "").strip(),
                    "traceHeaders": allowed_trace_headers,
                    "invocationId": str(uuid.uuid4()),
                    "timeoutMs": effective_timeout_ms,
                },
            )
            page.wait_for_timeout(250)
            response_status = int((fetch_result or {}).get("status") or 0)
            response_text = str((fetch_result or {}).get("text") or "")
            response_url = str((fetch_result or {}).get("url") or final_url)
            response_error = str((fetch_result or {}).get("error") or "")
            response_json: dict[str, Any] = {}
            try:
                parsed_response = json.loads(response_text or "{}")
                if isinstance(parsed_response, dict):
                    response_json = parsed_response
            except (TypeError, ValueError):
                pass
            final_cookies = list(context.cookies())
            final_cookie_names = tuple(
                sorted(
                    {
                        str(item.get("name") or "")
                        for item in final_cookies
                        if item.get("name")
                    }
                )
            )
            logger(
                "Auth Browser create_account 完成: "
                f"status={response_status} cookies={','.join(final_cookie_names)}"
            )
            return BrowserAccountCreateResult(
                status_code=response_status,
                response_url=response_url,
                response_text=response_text,
                response_json=response_json,
                cookies=final_cookies,
                cookie_names=final_cookie_names,
                sentinel_field_lengths=field_lengths,
                cf_clearance_present=cf_clearance_present,
                oai_sc_present=oai_sc_present,
                error=response_error,
            )
        except Exception as exc:
            if stop_check is not None:
                try:
                    stop_check()
                except Exception:
                    raise
            current_url = ""
            if page is not None:
                try:
                    current_url = str(page.url or "")
                except Exception:
                    current_url = ""
            logger(
                f"Auth Browser 开户异常(stage={stage}): {exc}"
                + (f" | current_url={current_url}" if current_url else "")
            )
            cookies_now: list[dict[str, Any]] = []
            try:
                if page is not None:
                    cookies_now = list(page.context.cookies())
            except Exception:
                pass
            return BrowserAccountCreateResult(
                response_url=current_url,
                cookies=cookies_now,
                error=f"{stage}: {exc}",
            )
        finally:
            if browser is not None:
                try:
                    browser.close()
                    logger("Auth Browser 阶段完成: browser.close")
                except Exception as close_exc:
                    logger(f"Auth Browser browser.close 异常: {close_exc}")
