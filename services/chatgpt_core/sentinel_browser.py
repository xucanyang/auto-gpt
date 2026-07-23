"""Playwright 版 Sentinel SDK token 获取辅助。"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import ExitStack
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
            logger("Sentinel Browser 阶段: wait SentinelSDK ready")
            try:
                page.wait_for_function(
                    "() => typeof window.SentinelSDK !== 'undefined' && typeof window.SentinelSDK.token === 'function'",
                    timeout=sdk_wait_timeout_ms,
                )
            except Exception:
                logger("Sentinel Browser 未发现 SDK，注入当前固定版本")
                page.evaluate(
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
                page.wait_for_function(
                    "() => typeof window.SentinelSDK !== 'undefined' && typeof window.SentinelSDK.token === 'function'",
                    timeout=sdk_wait_timeout_ms,
                )
            logger("Sentinel Browser 阶段完成: wait SentinelSDK ready")

            stage = "evaluate_token"
            logger(f"Sentinel Browser 阶段: evaluate SentinelSDK.token({flow})")
            result = page.evaluate(
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

            try:
                field_state = _sentinel_token_field_state(token)
                if field_state is None:
                    raise ValueError("not a Sentinel JSON object")
                logger(
                    "Sentinel Browser 成功: "
                    f"p={'✓' if field_state['p'] else '✗'} "
                    f"t={'✓' if field_state['t'] else '✗'} "
                    f"c={'✓' if field_state['c'] else '✗'}"
                )
                if require_complete_signals and not all(field_state.values()):
                    logger("Sentinel Browser 令牌缺少完整 p/t/c 信号，拒绝降级使用")
                    return None
            except Exception:
                logger(f"Sentinel Browser 成功: len={len(token)}")
                if require_complete_signals:
                    logger("Sentinel Browser 令牌格式不可验证，拒绝降级使用")
                    return None

            return token
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
