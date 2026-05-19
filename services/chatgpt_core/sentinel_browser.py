"""Playwright 版 Sentinel SDK token 获取辅助。"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from core.browser_runtime import (
    ensure_browser_display_available,
    resolve_browser_headless,
)
from core.proxy_utils import build_playwright_proxy_config

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
    log_fn: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """通过浏览器直接调用 SentinelSDK.token(flow) 获取完整 token。"""
    logger = log_fn or (lambda _msg: None)

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        logger(f"Sentinel Browser 不可用: {e}")
        return None

    target_url = str(page_url or _flow_page_url(flow)).strip() or _flow_page_url(flow)
    effective_headless, reason = resolve_browser_headless(headless)
    ensure_browser_display_available(effective_headless)
    logger(
        f"Sentinel Browser 模式: {'headless' if effective_headless else 'headed'} ({reason})"
    )

    effective_user_agent = (
        user_agent
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.7103.92 Safari/537.36"
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
    proxy_config = build_playwright_proxy_config(proxy)
    if proxy_config:
        launch_args["proxy"] = proxy_config

    logger(f"Sentinel Browser 启动: flow={flow}, url={target_url}")
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

    with sync_playwright() as p:
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
            if device_id:
                try:
                    context.add_cookies(
                        [
                            {
                                "name": "oai-did",
                                "value": str(device_id),
                                "url": "https://auth.openai.com/",
                                "path": "/",
                                "secure": True,
                                "sameSite": "Lax",
                            }
                        ]
                    )
                    logger("Sentinel Browser 阶段完成: add cookies")
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
            page.goto(target_url, wait_until="domcontentloaded", timeout=int(timeout_ms or 45000))
            logger(f"Sentinel Browser 阶段完成: page.goto -> {page.url}")

            stage = "wait_sentinel_sdk"
            logger("Sentinel Browser 阶段: wait SentinelSDK ready")
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
                parsed = json.loads(token)
                logger(
                    "Sentinel Browser 成功: "
                    f"p={'✓' if parsed.get('p') else '✗'} "
                    f"t={'✓' if parsed.get('t') else '✗'} "
                    f"c={'✓' if parsed.get('c') else '✗'}"
                )
            except Exception:
                logger(f"Sentinel Browser 成功: len={len(token)}")

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
