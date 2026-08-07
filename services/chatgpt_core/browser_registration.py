"""Browser-owned ChatGPT registration flow adapted from any-auto-register.

The registration stage and the optional isolated OAuth recovery stage are
consumed by auto-gpt. Account persistence and mailbox ownership remain owned by
auto-gpt's registration engine.
"""
import base64
import json
import os
import random
import re
import secrets
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urljoin, urlparse

from camoufox.sync_api import Camoufox

from core.browser_runtime import (
    ensure_browser_display_available,
    resolve_browser_headless,
)
from core.playwright_proxy import playwright_proxy_context

from .sentinel_constants import (
    DEFAULT_SENTINEL_FRAME_URL,
    DEFAULT_SENTINEL_SDK_URL,
)
from .registration_route_policy import (
    ExistingAccountDetected,
    is_existing_account_detected_message,
)


OPENAI_AUTH = os.environ.get("OPENAI_AUTH_BASE_URL", "https://auth.openai.com")
CHATGPT_APP = os.environ.get("CHATGPT_APP_URL", "https://chatgpt.com")
PLATFORM_LOGIN_ENTRY = os.environ.get(
    "PLATFORM_LOGIN_ENTRY", "https://platform.openai.com/login"
)
# Fallback mailbox cutoff when the exact OTP-send timestamp is unknown.
# Password SPA can hang 30-60s after /user/register while OpenAI has already
# delivered the first code to the HME→TempMail forward path; an 8s grace
# silently drops that mail (visible in TempMail UI, missed by the waiter).
OTP_SENT_AT_FALLBACK_GRACE_SECONDS = 60
SENTINEL_BASE = os.environ.get("SENTINEL_BASE_URL", "https://sentinel.openai.com")
SENTINEL_SDK_URL = os.environ.get("SENTINEL_SDK_URL", DEFAULT_SENTINEL_SDK_URL)
SENTINEL_FRAME_URL = os.environ.get(
    "SENTINEL_FRAME_URL", DEFAULT_SENTINEL_FRAME_URL
)
SENTINEL_REQ_URL = f"{SENTINEL_BASE}/backend-api/sentinel/req"
OAUTH_CONSENT_FORM_SELECTOR = (
    'form[action*="/sign-in-with-chatgpt/"][action*="/consent"]'
)

EMAIL_OTP_RESEND_SELECTORS = [
    'button:has-text("Resend")',
    'button:has-text("resend")',
    'button:has-text("Resend code")',
    'button:has-text("Resend email")',
    'button:has-text("Send again")',
    'button:has-text("重新发送")',
    'button:has-text("重发")',
    'a:has-text("Resend")',
    'a:has-text("resend")',
    'a:has-text("Resend code")',
    'a:has-text("重新发送")',
    'button[data-testid*="resend"]',
    'button[name*="resend" i]',
]

EMAIL_INPUT_SELECTORS = [
    'input#login-email',
    'input[type="email"]',
    'input[name="email"]',
    'input[name="username"]',
    'input[autocomplete="username"]',
    'input[autocomplete*="username"]',
    'input[inputmode="email"]',
    'input[id*="email"]',
]

PASSWORD_INPUT_SELECTORS = [
    'input[type="password"]',
    'input[name="password"]',
    'input[autocomplete="new-password"]',
]

EMAIL_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button[data-testid="continue-button"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("Next")',
    'button:has-text("next")',
]

PASSWORD_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button[data-testid="continue-button"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("Sign up")',
    'button:has-text("sign up")',
    'button:has-text("Create account")',
    'button:has-text("create account")',
]

OTP_INPUT_SELECTORS = [
    "input[inputmode='numeric']",
    "input[autocomplete='one-time-code']",
    "input[type='tel']",
    "input[type='number']",
    "input[name*='code' i]",
    "input[id*='code' i]",
]

PASSWORDLESS_LOGIN_SELECTORS = [
    'button[name="intent"][value="passwordless_login_send_otp"]',
    'button[value="passwordless_login_send_otp"]',
    'button:has-text("one-time code")',
    'button:has-text("one time code")',
    'button:has-text("passwordless")',
    'button:has-text("一次性验证码")',
    'button:has-text("驗證碼")',
    'button:has-text("验证码")',
    'button:has-text("código único")',
    'button:has-text("code unique")',
    'button:has-text("Einmalcode")',
    'button:has-text("código de uso único")',
]


class _BrowserSignupEntryUnavailable(RuntimeError):
    """The browser could not reach an email entry form before signup began."""

# add-phone 页面国际拨号码 -> 国家名映射（用于 UI 下拉选择）
PHONE_COUNTRY_CODE_MAP = {
    "1": "United States", "7": "Russia", "20": "Egypt", "27": "South Africa",
    "30": "Greece", "31": "Netherlands", "32": "Belgium", "33": "France",
    "34": "Spain", "36": "Hungary", "39": "Italy", "40": "Romania",
    "44": "United Kingdom", "45": "Denmark", "46": "Sweden", "47": "Norway",
    "48": "Poland", "49": "Germany", "51": "Peru", "52": "Mexico",
    "53": "Cuba", "54": "Argentina", "55": "Brazil", "56": "Chile",
    "57": "Colombia", "58": "Venezuela", "60": "Malaysia", "61": "Australia",
    "62": "Indonesia", "63": "Philippines", "64": "New Zealand",
    "65": "Singapore", "66": "Thailand", "81": "Japan", "82": "South Korea",
    "84": "Vietnam", "86": "China", "90": "Turkey", "91": "India",
    "92": "Pakistan", "93": "Afghanistan", "94": "Sri Lanka", "95": "Myanmar",
    "98": "Iran", "212": "Morocco", "213": "Algeria", "216": "Tunisia",
    "218": "Libya", "220": "Gambia", "221": "Senegal", "234": "Nigeria",
    "254": "Kenya", "255": "Tanzania", "256": "Uganda", "260": "Zambia",
    "263": "Zimbabwe", "351": "Portugal", "353": "Ireland", "354": "Iceland",
    "358": "Finland", "370": "Lithuania", "371": "Latvia", "372": "Estonia",
    "374": "Armenia", "375": "Belarus", "380": "Ukraine", "381": "Serbia",
    "385": "Croatia", "420": "Czech Republic", "421": "Slovakia",
    "855": "Cambodia", "856": "Laos", "880": "Bangladesh", "886": "Taiwan",
    "960": "Maldives", "966": "Saudi Arabia", "971": "United Arab Emirates",
    "972": "Israel", "977": "Nepal", "992": "Tajikistan",
    "993": "Turkmenistan", "994": "Azerbaijan", "995": "Georgia",
    "996": "Kyrgyzstan", "998": "Uzbekistan",
}

# 拨号码 -> ISO 3166-1 alpha-2 国家代码（用于 React Aria <select> 的 value 匹配）
PHONE_DIAL_TO_ISO = {
    "1": "US", "7": "RU", "20": "EG", "27": "ZA",
    "30": "GR", "31": "NL", "32": "BE", "33": "FR",
    "34": "ES", "36": "HU", "39": "IT", "40": "RO",
    "44": "GB", "45": "DK", "46": "SE", "47": "NO",
    "48": "PL", "49": "DE", "51": "PE", "52": "MX",
    "53": "CU", "54": "AR", "55": "BR", "56": "CL",
    "57": "CO", "58": "VE", "60": "MY", "61": "AU",
    "62": "ID", "63": "PH", "64": "NZ",
    "65": "SG", "66": "TH", "81": "JP", "82": "KR",
    "84": "VN", "86": "CN", "90": "TR", "91": "IN",
    "92": "PK", "93": "AF", "94": "LK", "95": "MM",
    "98": "IR", "212": "MA", "213": "DZ", "216": "TN",
    "218": "LY", "220": "GM", "221": "SN", "234": "NG",
    "254": "KE", "255": "TZ", "256": "UG", "260": "ZM",
    "263": "ZW", "351": "PT", "353": "IE", "354": "IS",
    "358": "FI", "370": "LT", "371": "LV", "372": "EE",
    "374": "AM", "375": "BY", "380": "UA", "381": "RS",
    "385": "HR", "420": "CZ", "421": "SK",
    "855": "KH", "856": "LA", "880": "BD", "886": "TW",
    "960": "MV", "966": "SA", "971": "AE",
    "972": "IL", "977": "NP", "992": "TJ",
    "993": "TM", "994": "AZ", "995": "GE",
    "996": "KG", "998": "UZ",
}

PHONE_INPUT_SELECTORS = [
    'input[type="tel"]',
    'input[name="phone"]',
    'input[name="phone_number"]',
    'input[name="phoneNumber"]',
    'input[id*="phone" i]',
    'input[placeholder*="phone" i]',
    'input[autocomplete="tel"]',
    'input[autocomplete="tel-national"]',
]

PHONE_SEND_SELECTORS = [
    'button:has-text("Send code via SMS")',
    'button:has-text("Send code")',
    'button:has-text("Send via SMS")',
    'button:has-text("Send link via SMS")',
    'button:has-text("Send")',
    'button[type="submit"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("发送")',
]

PHONE_VERIFY_SELECTORS = [
    'button:has-text("Verify")',
    'button:has-text("verify")',
    'button:has-text("Check")',
    'button[type="submit"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("验证")',
    'button:has-text("确认")',
]


def _parse_phone_country_and_local(phone_number: str) -> tuple[str, str, str]:
    """从完整手机号解析出 (拨号码, 本地号码, 国家名)。

    例: +66959075673 -> ("66", "959075673", "Thailand")
    """
    num = str(phone_number or "").lstrip("+").strip()
    for length in (3, 2, 1):
        if length > len(num):
            continue
        prefix = num[:length]
        if prefix in PHONE_COUNTRY_CODE_MAP:
            return prefix, num[length:], PHONE_COUNTRY_CODE_MAP[prefix]
    return "", num, ""


def _select_phone_country_ui(page, dial_code: str, country_name: str, log) -> bool:
    """在 add-phone 页面的国家下拉框中选择对应国家。

    OpenAI add-phone 页面使用 React Aria Select 组件，底层有一个隐藏的原生 <select>
    和一个可视的 button trigger + listbox 弹出层。
    """
    if not dial_code and not country_name:
        log("  无法识别国家码，跳过国家选择")
        return False

    iso_code = PHONE_DIAL_TO_ISO.get(dial_code, "")
    log(f"  目标国家: {country_name} (+{dial_code}) ISO={iso_code}")

    # 先检查当前下拉框是否已经是目标国家
    dial_pattern = f"(+{dial_code})"
    already = page.evaluate(
        """
        (dialPattern) => {
          const visible = (el) => {
            if (!el) return false;
            const s = window.getComputedStyle(el);
            const r = el.getBoundingClientRect();
            return s && s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
          };
          const all = Array.from(document.querySelectorAll('button, div, span, a, [role="button"], [role="combobox"], select'));
          for (const el of all) {
            if (!visible(el)) continue;
            const text = (el.innerText || el.textContent || '').trim();
            if (text.includes(dialPattern) && text.length < 80) return true;
          }
          return false;
        }
        """,
        dial_pattern,
    )
    if already:
        log(f"  国家已是目标值: (+{dial_code})")
        return True

    # ═══════════════════════════════════════════════════════════════════
    # 策略 1: 通过底层原生 <select> 直接设置值（最可靠）
    # React Aria Select 底层会有一个隐藏的 <select> 用于表单提交和无障碍。
    # 直接修改它的值并触发 change 事件可以同步 React 状态。
    # ═══════════════════════════════════════════════════════════════════
    native_selected = page.evaluate(
        """
        ({ isoCode, dialCode, countryName }) => {
          const selects = document.querySelectorAll('select');
          for (const sel of selects) {
            if (sel.options.length < 10) continue;  // 排除非国家的 select

            // 尝试多种匹配策略找到目标 option
            let targetValue = null;
            for (const opt of sel.options) {
              const v = (opt.value || '').trim();
              const t = (opt.text || opt.label || '').trim();
              // 匹配 ISO 代码 (如 "TH")
              if (isoCode && v === isoCode) { targetValue = v; break; }
              // 匹配拨号码 (如 value 包含 "66" 或 text 包含 "+66")
              if (t.includes('(+' + dialCode + ')')) { targetValue = v; break; }
              if (t.includes(countryName)) { targetValue = v; break; }
            }

            if (targetValue !== null) {
              // 使用 React 兼容的方式设置值
              const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLSelectElement.prototype, 'value'
              )?.set;
              if (nativeInputValueSetter) {
                nativeInputValueSetter.call(sel, targetValue);
              } else {
                sel.value = targetValue;
              }
              sel.dispatchEvent(new Event('change', { bubbles: true }));
              sel.dispatchEvent(new Event('input', { bubbles: true }));
              return { ok: true, value: targetValue, method: 'native_setter' };
            }
          }
          return { ok: false };
        }
        """,
        {"isoCode": iso_code, "dialCode": dial_code, "countryName": country_name},
    )
    if native_selected and native_selected.get("ok"):
        log(f"  ✓ 通过原生 <select> 选择成功: value={native_selected.get('value')}")
        time.sleep(0.5)
        # 验证 UI 是否同步更新
        verify = page.evaluate(
            "(dp) => { const b = document.querySelector('button[aria-haspopup=\"listbox\"]'); return b ? (b.innerText || '').trim() : ''; }",
            dial_pattern,
        )
        if f"+{dial_code}" in (verify or ""):
            log(f"  ✓ UI 已同步: {verify}")
            return True
        log(f"  原生 select 已设置但 UI 未同步 ({verify})，尝试 UI 交互...")

    # ═══════════════════════════════════════════════════════════════════
    # 策略 2: 通过 React Aria 的 key 属性直接操作
    # ═══════════════════════════════════════════════════════════════════
    key_selected = page.evaluate(
        """
        ({ isoCode, dialCode, countryName }) => {
          // 找到 React Aria Select 的隐藏 <select> 并通过 selectOption 模拟
          const selects = document.querySelectorAll('select');
          for (const sel of selects) {
            if (sel.options.length < 10) continue;
            for (const opt of sel.options) {
              const v = (opt.value || '').trim();
              const t = (opt.text || opt.label || '').trim();
              if ((isoCode && v === isoCode) || t.includes('(+' + dialCode + ')') || t.includes(countryName)) {
                sel.value = v;
                // 触发 React 合成事件
                const ev = new Event('change', { bubbles: true });
                Object.defineProperty(ev, 'target', { writable: false, value: sel });
                sel.dispatchEvent(ev);
                return { ok: true, value: v, text: t };
              }
            }
          }
          return { ok: false };
        }
        """,
        {"isoCode": iso_code, "dialCode": dial_code, "countryName": country_name},
    )

    # ═══════════════════════════════════════════════════════════════════
    # 策略 3: 使用 Playwright 的 selectOption API（对原生 select 最可靠）
    # ═══════════════════════════════════════════════════════════════════
    try:
        select_el = page.query_selector("select")
        if select_el:
            # 尝试用 ISO 代码选择
            if iso_code:
                try:
                    select_el.select_option(value=iso_code)
                    log(f"  ✓ Playwright selectOption(value={iso_code}) 成功")
                    time.sleep(0.5)
                    return True
                except Exception:
                    pass
            # 尝试用 label 匹配（包含国家名或拨号码）
            try:
                # 获取所有 option 的 value 和 text，找到匹配的
                match_value = page.evaluate(
                    """
                    ({ dialCode, countryName }) => {
                      const sel = document.querySelector('select');
                      if (!sel) return '';
                      for (const opt of sel.options) {
                        const t = (opt.text || opt.label || '').trim();
                        const v = (opt.value || '').trim();
                        if (t.includes('(+' + dialCode + ')') || t.includes(countryName)) return v;
                      }
                      return '';
                    }
                    """,
                    {"dialCode": dial_code, "countryName": country_name},
                )
                if match_value:
                    select_el.select_option(value=match_value)
                    log(f"  ✓ Playwright selectOption(value={match_value}) 成功")
                    time.sleep(0.5)
                    return True
            except Exception as e:
                log(f"  selectOption label 匹配失败: {e}")
    except Exception as e:
        log(f"  Playwright selectOption 策略失败: {e}")

    # ═══════════════════════════════════════════════════════════════════
    # 策略 4: 点击 trigger 按钮打开 listbox，然后在 listbox 中选择
    # ═══════════════════════════════════════════════════════════════════
    trigger = None
    for sel in [
        'button[aria-haspopup="listbox"]',
        '.react-aria-Select button',
        'button[class*="select" i]',
        'button[class*="country" i]',
    ]:
        trigger = page.query_selector(sel)
        if trigger:
            break

    if not trigger:
        trigger = page.evaluate(
            r"""
            () => {
              const pattern = /\(\+\d{1,4}\)/;
              const all = document.querySelectorAll('button, [role="button"], [role="combobox"]');
              for (const el of all) {
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                const text = (el.innerText || '').trim();
                if (pattern.test(text)) {
                  el.scrollIntoView({ block: 'center' });
                  el.click();
                  return true;
                }
              }
              return false;
            }
            """,
        )
        if not trigger:
            log("  ⚠️ 未找到国家选择器触发按钮")
            return False
        log("  已通过 JS 点击触发按钮")
    else:
        trigger.scroll_into_view_if_needed()
        trigger.click()
        log("  已点击国家选择器下拉框")

    time.sleep(0.8)

    # 等待 listbox 出现
    listbox = None
    for _ in range(10):
        listbox = page.query_selector('[role="listbox"]')
        if listbox:
            break
        time.sleep(0.3)

    if not listbox:
        log("  ⚠️ 下拉框 listbox 未出现")
        return False

    log("  listbox 已出现")

    # 在 listbox 中查找并点击目标 option
    option = None
    if iso_code:
        for attr in ["data-key", "data-value", "value", "id"]:
            # 尝试精确匹配和包含匹配
            option = page.query_selector(f'[role="option"][{attr}="{iso_code}"]')
            if not option:
                option = page.query_selector(f'[role="option"][{attr}*="{iso_code}"]')
            if option:
                log(f"  找到 option: [{attr} 含 {iso_code}]")
                break

    if not option:
        option_idx = page.evaluate(
            """
            ({ countryName, dialCode }) => {
              const options = document.querySelectorAll('[role="option"]');
              for (let i = 0; i < options.length; i++) {
                const text = (options[i].innerText || options[i].textContent || '').trim();
                if (text.includes(countryName) || text.includes('(+' + dialCode + ')') || text.includes('+' + dialCode)) {
                  return i;
                }
              }
              // 宽松匹配：只匹配拨号码数字
              for (let i = 0; i < options.length; i++) {
                const text = (options[i].innerText || options[i].textContent || '').trim();
                if (text.includes(dialCode)) {
                  return i;
                }
              }
              return -1;
            }
            """,
            {"countryName": country_name, "dialCode": dial_code},
        )
        if option_idx >= 0:
            options = page.query_selector_all('[role="option"]')
            if option_idx < len(options):
                option = options[option_idx]
                log(f"  找到 option: 文本匹配 index={option_idx}")

    if option:
        option.scroll_into_view_if_needed()
        option.click()
        time.sleep(0.5)
        new_text = page.evaluate(
            """() => {
              const btn = document.querySelector('button[aria-haspopup="listbox"]') ||
                          document.querySelector('.react-aria-Select button');
              return btn ? (btn.innerText || '').trim() : '';
            }""",
        )
        log(f"  选择后下拉框显示: {new_text}")
        if f"+{dial_code}" in (new_text or ""):
            log(f"  ✓ 国家选择成功: {new_text}")
            return True

    # 键盘 type-ahead 搜索
    log(f"  尝试键盘 type-ahead: {country_name}")
    page.keyboard.type(country_name, delay=80)
    time.sleep(0.8)

    # 按 Enter 确认选择
    page.keyboard.press("Enter")
    time.sleep(0.5)

    # 验证
    final_text = page.evaluate(
        """() => {
          const btn = document.querySelector('button[aria-haspopup="listbox"]') ||
                      document.querySelector('.react-aria-Select button');
          return btn ? (btn.innerText || '').trim() : '';
        }""",
    )
    if f"+{dial_code}" in (final_text or ""):
        log(f"  ✓ type-ahead 选择成功: {final_text}")
        return True

    log(f"  ⚠️ 下拉框已展开但未找到匹配国家: {country_name} (+{dial_code})")
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return False


def _build_proxy_config(proxy: Optional[str]) -> Optional[dict]:
    if not proxy:
        return None
    parsed = urlparse(proxy)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        return {"server": proxy}
    config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password
    return config


def _ensure_camoufox_geoip_ready() -> None:
    """代理模式下 Camoufox geoip=True 需要 camoufox[geoip]（geoip2/maxminddb）。"""
    try:
        from camoufox.geolocation import geoip_allowed

        geoip_allowed()
    except Exception as exc:
        raise RuntimeError(
            "Camoufox geoip 依赖未就绪，请安装 camoufox[geoip]（含 geoip2/maxminddb）"
            f" 并确保 GeoIP MMDB 可用: {exc}"
        ) from exc


def _is_camoufox_geoip_ip_failure(exc: BaseException) -> bool:
    """True when Camoufox failed only while resolving public IP for geoip."""
    name = type(exc).__name__
    text = str(exc or "")
    if name in {"InvalidIP", "InvalidProxy"}:
        return True
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "failed to get ip address",
            "invalid ip address",
            "ipecho.net",
            "api.ipify.org",
            "checkip.amazonaws.com",
        )
    )


def _resolve_proxy_exit_ip_for_geoip(proxy_url: str | None) -> str:
    """Best-effort public IP via the browser proxy; empty string on total failure."""
    endpoints = (
        "https://api.ipify.org",
        "https://checkip.amazonaws.com",
        "https://ipinfo.io/ip",
        "https://icanhazip.com",
        "https://ifconfig.co/ip",
        "https://ipecho.net/plain",
    )
    proxies = None
    raw = str(proxy_url or "").strip()
    if raw:
        # Camoufox/Playwright proxy already adapted; raw socks/http URL still works for requests.
        proxies = {"http": raw, "https": raw}
    try:
        import requests
    except Exception:
        return ""
    for url in endpoints:
        try:
            resp = requests.get(url, proxies=proxies, timeout=5, verify=False)
            resp.raise_for_status()
            ip = str(resp.text or "").strip()
            if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", ip) or ":" in ip:
                return ip
        except Exception:
            continue
    return ""


def _enter_camoufox_with_geoip_fallback(
    stack: ExitStack,
    launch_opts: dict[str, Any],
    logger: Callable[[str], None] | None = None,
):
    """Enter Camoufox; if geoip public-IP probe fails, retry without geoip."""
    log = logger or (lambda _message: None)
    try:
        return stack.enter_context(Camoufox(**launch_opts))
    except Exception as exc:
        if "geoip" not in launch_opts or not _is_camoufox_geoip_ip_failure(exc):
            raise
        log(f"Camoufox geoip 探测出口 IP 失败，降级关闭 geoip 后重试: {exc}")
        fallback = dict(launch_opts)
        fallback.pop("geoip", None)
        return stack.enter_context(Camoufox(**fallback))


def _camoufox_executable_path() -> Optional[Path]:
    """Resolve an explicitly installed Camoufox binary without downloading one.

    The image now uses Camoufox's multiversion layout and therefore normally
    lets the package choose the active install.  Keep an explicit override and
    the pre-0.5 flat layout as compatibility paths for older running images.
    """
    configured = str(os.environ.get("CAMOUFOX_EXECUTABLE_PATH") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError(
                "CAMOUFOX_EXECUTABLE_PATH 不存在或不可执行: " f"{path}"
            )
        return path

    try:
        from camoufox.pkgman import INSTALL_DIR
    except Exception:
        return None

    legacy_path = Path(INSTALL_DIR) / "camoufox-bin"
    if legacy_path.is_file() and os.access(legacy_path, os.X_OK):
        return legacy_path
    return None


def _camoufox_major_version(executable_path: Path) -> Optional[int]:
    """Read the browser major version needed by the legacy flat installer."""
    try:
        metadata = json.loads(
            (executable_path.parent / "version.json").read_text(encoding="utf-8")
        )
        value = str(metadata.get("version") or "").split(".", 1)[0]
        major = int(value)
        return major if major > 0 else None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _camoufox_executable_options() -> dict:
    executable_path = _camoufox_executable_path()
    if executable_path is None:
        return {}

    options = {"executable_path": str(executable_path)}
    major_version = _camoufox_major_version(executable_path)
    if major_version is not None:
        # The old flat installer has no active_version entry.  Supplying the
        # matching major lets camoufox build its fingerprint without invoking
        # the package manager; it is not a version spoof.
        options.update(
            {
                "ff_version": major_version,
                "i_know_what_im_doing": True,
            }
        )
    return options


def _camoufox_launch_opts(
    *,
    headless: bool,
    proxy: Optional[str],
    enable_geoip: bool = True,
) -> dict:
    """统一 Camoufox 启动参数：有代理时尽量启用 geoip，避免时区/locale 与出口 IP 不一致。

    ``geoip`` prefers an explicit exit IP string (resolved via multiple public
    endpoints through the proxy). Falling back to ``True`` lets Camoufox probe
    itself; callers should still use ``_enter_camoufox_with_geoip_fallback`` so
    ipecho/ipify SSL failures do not abort the whole registration.
    """
    launch_opts: dict = {"headless": headless}
    launch_opts.update(_camoufox_executable_options())
    proxy_cfg = _build_proxy_config(proxy)
    if proxy_cfg:
        launch_opts["proxy"] = proxy_cfg
        if enable_geoip:
            try:
                _ensure_camoufox_geoip_ready()
            except Exception:
                # GeoIP DB missing: still launch with proxy, without locale alignment.
                return launch_opts
            explicit_ip = _resolve_proxy_exit_ip_for_geoip(proxy)
            launch_opts["geoip"] = explicit_ip or True
    return launch_opts


def _wait_for_url(page, substring: str, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if substring in page.url:
            return True
        time.sleep(1)
    return False


def _find_first_selector(page, selectors: list[str]) -> str | None:
    for sel in selectors:
        try:
            node = page.query_selector(sel)
        except Exception:
            node = None
        if node:
            return sel
    return None


def _find_first_visible_selector(page, selectors: list[str]) -> str | None:
    for selector in selectors:
        if _first_visible_locator(page, selector) is not None:
            return selector
    return None


def _first_visible_locator(page, selector: str):
    try:
        locator = page.locator(selector)
        count = min(int(locator.count() or 0), 20)
    except Exception:
        return None
    for index in range(count):
        try:
            candidate = locator.nth(index) if hasattr(locator, "nth") else locator.first
            if candidate.is_visible(timeout=150):
                return candidate
        except Exception:
            continue
    return None


def _wait_for_any_selector(page, selectors: list[str], timeout: int = 30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = _find_first_selector(page, selectors)
        if found:
            return found
        time.sleep(0.5)
    return None


def _click_first(page, selectors: list[str], *, timeout: int = 10) -> str | None:
    found = _wait_for_any_selector(page, selectors, timeout=timeout)
    if not found:
        return None
    try:
        page.click(found)
        return found
    except Exception:
        return None


class _NetworkActivityObserver:
    """Track one browser transaction without moving it to another transport."""

    _SENTINEL_MARKERS = (
        "sentinel.openai.com/backend-api/sentinel/req",
        "/backend-api/sentinel/req",
        "challenges.cloudflare.com",
    )

    def __init__(self, page, business_markers: tuple[str, ...]):
        self.page = page
        self.business_markers = tuple(str(item) for item in business_markers if item)
        self.business_requests: list[Any] = []
        self.business_responses: list[Any] = []
        self.business_failures: list[str] = []
        self.sentinel_requests: list[Any] = []
        self.sentinel_responses: list[Any] = []
        self.sentinel_failures: list[str] = []
        self._listeners: list[tuple[str, Any]] = []
        self._install()

    @staticmethod
    def _url(item) -> str:
        try:
            return str(getattr(item, "url", "") or "")
        except Exception:
            return ""

    @staticmethod
    def _failure(item) -> str:
        try:
            failure = getattr(item, "failure", "")
            if callable(failure):
                failure = failure()
            if isinstance(failure, dict):
                failure = failure.get("errorText") or failure.get("error_text") or ""
            return str(failure or "request failed")[:300]
        except Exception:
            return "request failed"

    def _is_business(self, url: str) -> bool:
        return any(marker in url for marker in self.business_markers)

    def _is_sentinel(self, url: str) -> bool:
        return any(marker in url for marker in self._SENTINEL_MARKERS)

    def _install(self) -> None:
        if not hasattr(self.page, "on"):
            return

        def on_request(request):
            url = self._url(request)
            if self._is_business(url):
                self.business_requests.append(request)
            elif self._is_sentinel(url):
                self.sentinel_requests.append(request)

        def on_response(response):
            url = self._url(response)
            if self._is_business(url):
                self.business_responses.append(response)
            elif self._is_sentinel(url):
                self.sentinel_responses.append(response)

        def on_request_failed(request):
            url = self._url(request)
            if self._is_business(url):
                self.business_failures.append(self._failure(request))
            elif self._is_sentinel(url):
                self.sentinel_failures.append(self._failure(request))

        for event, listener in (
            ("request", on_request),
            ("response", on_response),
            ("requestfailed", on_request_failed),
        ):
            try:
                self.page.on(event, listener)
                self._listeners.append((event, listener))
            except Exception:
                continue

    @property
    def has_business_request(self) -> bool:
        return bool(
            self.business_requests
            or self.business_responses
            or self.business_failures
        )

    @property
    def sentinel_pending(self) -> bool:
        completed = len(self.sentinel_responses) + len(self.sentinel_failures)
        return len(self.sentinel_requests) > completed

    def close(self) -> None:
        if hasattr(self.page, "remove_listener"):
            for event, listener in self._listeners:
                try:
                    self.page.remove_listener(event, listener)
                except Exception:
                    pass
        self._listeners.clear()
        self.business_requests.clear()
        self.business_responses.clear()
        self.business_failures.clear()
        self.sentinel_requests.clear()
        self.sentinel_responses.clear()
        self.sentinel_failures.clear()


class _PasswordFormSubmission:
    """Submit the password form with trusted, bounded browser actions."""

    REQUEST_SUBMIT_DELAY_SECONDS = 10.0
    ENTER_DELAY_SECONDS = 10.0

    def __init__(self, page, input_selector: str, log, *, context: str, business_markers: tuple[str, ...]):
        self.page = page
        self.input_selector = input_selector
        self.log = log
        self.context = context
        self.input = _first_visible_locator(page, input_selector)
        if self.input is None:
            raise RuntimeError(f"{context}未找到可见密码输入框")
        try:
            form = self.input.locator("xpath=ancestor::form[1]")
            self.form = form.first if int(form.count() or 0) > 0 else None
        except Exception:
            self.form = None
        if self.form is None:
            raise RuntimeError(f"{context}密码输入框不属于可提交表单")
        try:
            buttons = self.form.locator(
                'button[type="submit"], input[type="submit"], button[data-testid="continue-button"]'
            )
            self.submit_button = None
            for index in range(min(int(buttons.count() or 0), 10)):
                candidate = buttons.nth(index) if hasattr(buttons, "nth") else buttons.first
                if candidate.is_visible(timeout=150):
                    self.submit_button = candidate
                    break
        except Exception:
            self.submit_button = None
        if self.submit_button is None:
            raise RuntimeError(f"{context}未找到密码所属表单的可见提交按钮")
        self.observer = _NetworkActivityObserver(page, business_markers)
        self.started_at = 0.0
        self.request_submit_at: float | None = None
        self.enter_at: float | None = None
        self.initial_click_error = ""

    def _validity(self) -> tuple[bool, str]:
        try:
            result = self.input.evaluate(
                """
                (input) => {
                  const form = input.form || input.closest?.('form');
                  const valid = Boolean(form?.checkValidity ? form.checkValidity() : input.checkValidity?.());
                  return { valid, message: String(input.validationMessage || '') };
                }
                """
            )
        except Exception:
            return True, ""
        if not isinstance(result, dict):
            return True, ""
        return bool(result.get("valid", True)), str(result.get("message") or "").strip()

    def start(self) -> None:
        try:
            self.input.press("Tab", timeout=3000)
        except Exception:
            try:
                self.page.keyboard.press("Tab")
            except Exception:
                pass
        valid, message = self._validity()
        if not valid:
            self.close()
            raise RuntimeError(message or f"{self.context}密码表单校验失败")
        self.started_at = time.time()
        try:
            self.submit_button.click(timeout=8000)
            self.log(f"{self.context}已点击密码所属表单提交按钮")
            return
        except Exception as click_error:
            if self.observer.has_business_request:
                self.log(f"{self.context}提交按钮点击返回异常，但已观察到业务请求")
                return
            self.initial_click_error = str(click_error or "").strip()
            self.log(
                f"{self.context}提交按钮点击返回异常，先观察现有事务；"
                "确认无业务请求后再执行下一阶提交动作"
            )

    def _request_submit(self) -> bool:
        try:
            return bool(
                self.input.evaluate(
                    """
                    (input) => {
                      const form = input.form || input.closest?.('form');
                      if (!form?.requestSubmit) return false;
                      const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                      };
                      const button = Array.from(form.querySelectorAll(
                        'button[type="submit"], input[type="submit"], button[data-testid="continue-button"]'
                      )).find(visible);
                      if (!button) return false;
                      form.requestSubmit(button);
                      return true;
                    }
                    """
                )
            )
        except Exception:
            return False

    def advance_if_idle(self, now: float | None = None) -> None:
        if self.observer.has_business_request:
            return
        current = float(now if now is not None else time.time())
        if self.request_submit_at is None:
            if current - self.started_at < self.REQUEST_SUBMIT_DELAY_SECONDS:
                return
            if self.observer.sentinel_pending and current - self.started_at < 25:
                return
            self.request_submit_at = current
            if self._request_submit():
                self.log(f"{self.context}点击后无业务请求，已执行一次同表单 requestSubmit")
            else:
                self.log(f"{self.context}点击后无业务请求，同表单 requestSubmit 不可用")
            return
        if self.enter_at is not None:
            return
        if current - self.request_submit_at < self.ENTER_DELAY_SECONDS:
            return
        if self.observer.sentinel_pending and current - self.request_submit_at < 25:
            return
        try:
            self.input.press("Enter", timeout=5000)
            self.enter_at = current
            self.log(f"{self.context}requestSubmit 后仍无业务请求，已执行一次可信 Enter")
        except Exception:
            self.enter_at = current

    def close(self) -> None:
        self.observer.close()


def _browser_response_details(response) -> tuple[int, str, dict, str]:
    status = int(getattr(response, "status", 0) or 0)
    response_url = str(getattr(response, "url", "") or "")
    text = ""
    data: dict = {}
    try:
        text = str(response.text() or "")
    except Exception:
        pass
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            data = parsed
    except Exception:
        if text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    data = parsed
            except (TypeError, ValueError):
                pass
    return status, response_url, data, text


def _browser_response_error(data: dict, text: str) -> str:
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        message = str(
            error.get("message") or error.get("detail") or error.get("code") or ""
        ).strip()
        if message:
            return message
    for key in ("message", "detail", "error"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(text or "").strip()[:500]


def _password_submission_timeout_text(submission: _PasswordFormSubmission, fallback: str) -> str:
    if submission.observer.sentinel_failures:
        return f"Sentinel/Cloudflare 前置请求失败: {submission.observer.sentinel_failures[-1]}"
    if submission.observer.sentinel_pending:
        return "Sentinel/Cloudflare 前置请求未完成，密码表单未发出业务请求"
    initial_click_error = getattr(submission, "initial_click_error", "")
    if isinstance(initial_click_error, str) and initial_click_error.strip():
        return f"{fallback}；初始提交按钮点击失败: {initial_click_error.strip()[:300]}"
    return fallback


def _is_navigation_context_error(exc: BaseException | str) -> bool:
    text = str(exc or "").lower()
    return any(
        marker in text
        for marker in (
            "execution context was destroyed",
            "most likely because of a navigation",
            "target closed",
            "target page, context or browser has been closed",
            "frame was detached",
            "navigation",
        )
    )


def _wait_for_auth_page_settle(page, *, timeout: float = 12.0, log=None) -> str:
    """Wait for SPA navigations to settle before page.evaluate / form work."""
    deadline = time.time() + max(1.0, float(timeout or 1.0))
    last_url = ""
    stable_hits = 0
    while time.time() < deadline:
        try:
            current = str(page.url or "")
        except Exception as exc:
            if _is_navigation_context_error(exc):
                time.sleep(0.35)
                continue
            current = ""
        try:
            page.wait_for_load_state("domcontentloaded", timeout=1500)
        except Exception:
            pass
        if current and current == last_url:
            stable_hits += 1
            if stable_hits >= 2:
                return current
        else:
            stable_hits = 0
            last_url = current
        time.sleep(0.35)
    try:
        return str(page.url or last_url or "")
    except Exception:
        return last_url or ""


def _page_evaluate_safe(page, script, arg=None, *, retries: int = 4, settle: bool = True):
    """page.evaluate with navigation-race retries."""
    last_exc: Exception | None = None
    for attempt in range(max(1, int(retries or 1))):
        if settle and attempt > 0:
            _wait_for_auth_page_settle(page, timeout=4.0)
        try:
            if arg is None:
                return page.evaluate(script)
            return page.evaluate(script, arg)
        except Exception as exc:
            last_exc = exc
            if not _is_navigation_context_error(exc) or attempt >= max(1, int(retries or 1)) - 1:
                raise
            time.sleep(0.4 + 0.2 * attempt)
    if last_exc:
        raise last_exc
    return None


def _is_login_password_url(url: str) -> bool:

    return bool(re.search(r"(?:auth|accounts)\.openai\.com/.*log-?in/password", str(url or ""), flags=re.I))


def _build_manual_flow_state(page_type: str, current_url: str) -> dict:
    state = _extract_flow_state(None, current_url)
    state["page_type"] = page_type
    state["current_url"] = current_url
    return state


def _click_passwordless_login_if_available(page, log, *, context: str) -> bool:
    selector = _click_first(page, PASSWORDLESS_LOGIN_SELECTORS, timeout=1)
    if selector:
        log(f"{context} 已选择一次性验证码登录: {selector}")
        time.sleep(1)
        return True
    try:
        clicked = bool(
            page.evaluate(
                """
                () => {
                  const nodes = Array.from(document.querySelectorAll('button, [role="button"], a'));
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                  };
                  const target = nodes.find((el) => {
                    const text = String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    return visible(el) && /使用一次性验证码登录|使用一次性驗證碼登入|one-time code|one time code|passwordless/i.test(text);
                  });
                  if (!target) return false;
                  target.click();
                  return true;
                }
                """
            )
        )
    except Exception:
        clicked = False
    if clicked:
        log(f"{context} 已选择一次性验证码登录")
        time.sleep(1)
    return clicked


def _get_page_oauth_url(page) -> str:
    try:
        return str(
            page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                  };
                  const anchors = Array.from(document.querySelectorAll('a[href*="/api/oauth/authorize"]'));
                  const anchor = anchors.find((el) => visible(el));
                  return anchor ? String(anchor.href || anchor.getAttribute('href') || '') : '';
                }
                """
            )
            or ""
        ).strip()
    except Exception:
        return ""


def _oauth_url_matches_state(url: str, state: str) -> bool:
    if not url or not state:
        return False
    return f"state={state}" in url or f"state%3D{state}" in url


def _extract_auth_error_text(page) -> str:
    selectors = [
        "text=Failed to create account",
        "text=Sorry, we cannot create your account",
        "text=Please try again",
        "text=Invalid code",
        "text=Enter a valid age to continue",
        "text=doesn't look right",
        "[role='alert']",
        ".error, [class*='error'], [class*='Error']",
    ]
    for selector in selectors:
        try:
            text = str(page.locator(selector).first.text_content(timeout=350) or "").strip()
        except Exception:
            text = ""
        if text and "oai_log" not in text and "SSR_HTML" not in text:
            return text
    return ""


def _extract_input_validation_message(page, selector: str) -> str:
    try:
        return str(
            page.locator(selector).first.evaluate(
                """
                (input) => {
                  if (!(input instanceof HTMLInputElement)) return '';
                  if (input.validationMessage) return String(input.validationMessage);
                  if (input.getAttribute('aria-invalid') !== 'true') return '';
                  const errorId = input.getAttribute('aria-errormessage') || input.getAttribute('aria-describedby');
                  const errorNode = errorId ? document.getElementById(errorId.split(/\\s+/)[0]) : null;
                  return String(errorNode?.textContent || 'Password input validation failed');
                }
                """
            )
            or ""
        ).strip()
    except Exception:
        return ""


def _fill_input_like_user(page, selector: str, value: str) -> bool:
    target = str(value or "")
    for attempt in range(3):
        try:
            if attempt:
                _wait_for_auth_page_settle(page, timeout=5.0)
            locator = _first_visible_locator(page, selector) or page.locator(selector).first
            locator.wait_for(state="visible", timeout=4000)
            current = str(locator.input_value() or "").strip()
            if current == target.strip():
                return True
            locator.click(timeout=2500)
            _browser_pause(page)
            try:
                locator.fill("")
            except Exception:
                pass
            _browser_pause(page, headed=False)
            try:
                locator.type(target, delay=random.randint(35, 85))
            except Exception:
                try:
                    page.fill(selector, target)
                except Exception:
                    if attempt >= 2:
                        break
                    continue
            final_value = str(locator.input_value() or "").strip()
            if final_value == target:
                return True
            # 部分 SPA 会规范化空白；宽松比对
            if final_value.replace(" ", "") == target.replace(" ", ""):
                return True
        except Exception as exc:
            if attempt >= 2 and not _is_navigation_context_error(exc):
                break
            time.sleep(0.4)
            continue

    try:
        ok = _page_evaluate_safe(
            page,
            """
            ({ selector, value }) => {
              const input = Array.from(document.querySelectorAll(selector)).find((candidate) => {
                const style = window.getComputedStyle(candidate);
                const rect = candidate.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              });
              if (!input) return false;
              const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
              if (!setter) return false;
              setter.call(input, value);
              input.dispatchEvent(new Event('input', { bubbles: true }));
              input.dispatchEvent(new Event('change', { bubbles: true }));
              return String(input.value || '') === String(value || '');
            }
            """,
            {"selector": selector, "value": value},
            retries=3,
        )
        return bool(ok)
    except Exception:
        return False


def _submit_form_with_fallback(page, input_selector: str) -> bool:
    try:
        requested = bool(
            page.evaluate(
                """
                (selector) => {
                  const input = document.querySelector(selector);
                  if (!input) return false;
                  const form = input.form || input.closest?.('form');
                  if (form?.requestSubmit) {
                    form.requestSubmit();
                    return true;
                  }
                  return false;
                }
                """,
                input_selector,
            )
        )
        if requested:
            return True
    except Exception:
        pass
    try:
        locator = _first_visible_locator(page, input_selector) or page.locator(input_selector).first
        locator.press("Enter", timeout=5000)
        return True
    except Exception:
        return False


def _sync_hidden_birthday_input(page, birthdate: str, log) -> bool:
    try:
        synced = bool(
            page.evaluate(
                """
                (value) => {
                  const input = document.querySelector("input[name='birthday']");
                  if (!input) return false;
                  input.value = value;
                  input.dispatchEvent(new Event('input', { bubbles: true }));
                  input.dispatchEvent(new Event('change', { bubbles: true }));
                  return String(input.value || '') === String(value || '');
                }
                """,
                birthdate,
            )
        )
    except Exception:
        synced = False
    if synced:
        log(f"about_you 已同步隐藏 birthday: {birthdate}")
    return synced


def _collect_visible_text_inputs(page) -> list[dict]:
    try:
        inputs = page.evaluate(
            """
            () => {
              const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
              const nodes = Array.from(document.querySelectorAll("input:not([type='hidden']):not([disabled]):not([readonly])"));
              const visible = nodes.filter((el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style
                  && style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && rect.width > 0
                  && rect.height > 0;
              });
              return visible.map((el, visibleIndex) => {
                const explicitLabels = Array.from(document.querySelectorAll('label'))
                  .filter((label) => String(label.getAttribute('for') || '') === String(el.id || ''))
                  .map((label) => normalize(label.textContent));
                const wrappedLabel = normalize(el.closest('label')?.textContent || '');
                const ariaLabel = normalize(el.getAttribute('aria-label'));
                const labelledByText = normalize(
                  String(el.getAttribute('aria-labelledby') || '')
                    .split(/\\s+/)
                    .filter(Boolean)
                    .map((id) => normalize(document.getElementById(id)?.textContent || ''))
                    .join(' ')
                );
                const parentText = normalize(el.parentElement?.textContent || '');
                return {
                  visibleIndex,
                  type: normalize(el.getAttribute('type') || el.type || ''),
                  name: normalize(el.getAttribute('name') || ''),
                  id: normalize(el.id || ''),
                  placeholder: normalize(el.getAttribute('placeholder') || ''),
                  ariaLabel,
                  labels: explicitLabels.filter(Boolean),
                  wrappedLabel,
                  labelledByText,
                  parentText,
                };
              });
            }
            """
        ) or []
    except Exception:
        inputs = []
    return [item for item in inputs if isinstance(item, dict)]


def _about_you_input_hints(entry: dict) -> str:
    parts: list[str] = []
    labels = entry.get("labels") or []
    if isinstance(labels, list):
        parts.extend(str(item or "") for item in labels)
    parts.extend(
        [
            str(entry.get("wrappedLabel") or ""),
            str(entry.get("labelledByText") or ""),
            str(entry.get("ariaLabel") or ""),
            str(entry.get("placeholder") or ""),
            str(entry.get("name") or ""),
            str(entry.get("id") or ""),
            str(entry.get("parentText") or ""),
        ]
    )
    return " ".join(part for part in parts if part).strip().lower()


def _pick_best_about_you_input(entries: list[dict], field: str, exclude_visible_indices: set[int] | None = None) -> dict | None:
    exclude = {int(value) for value in (exclude_visible_indices or set())}
    best_entry = None
    best_score = float("-inf")
    for entry in entries:
        try:
            visible_index = int(entry.get("visibleIndex"))
        except Exception:
            continue
        if visible_index in exclude:
            continue
        hints = _about_you_input_hints(entry)
        if not hints:
            continue

        score = 0
        if field == "name":
            if any(token in hints for token in ("full name", "fullname", "全名", "姓名", "氏名", "nombre completo", "nom complet", "vollständiger name", "nome completo")):
                score += 10
            if any(token in hints for token in (" name ", "name", "autocomplete=name", "nombre", "nom", "nome")):
                score += 3
            if any(token in hints for token in ("age", "年龄", "年齢", "edad", "âge", "alter", "idade", "umur", "usia", "birthday", "birth", "date of birth", "出生", "生日", "生年月日")):
                score -= 8
        elif field == "age":
            if any(token in hints for token in ("age", "年龄", "年齢", "how old", "edad", "âge", "alter", "idade", "umur", "usia", "나이")):
                score += 10
            if any(token in hints for token in ("full name", "fullname", "全名", "姓名", "氏名", "nombre completo", "nom complet")):
                score -= 10
            if (
                "name" in hints
                and not any(token in hints for token in ("age", "年龄", "年齢", "edad", "umur", "usia"))
            ):
                score -= 6
            if any(token in hints for token in ("birthday", "birth", "date of birth", "出生", "生日", "生年月日", "fecha de nacimiento", "nascimento")):
                score -= 3
        else:
            continue

        if score > best_score:
            best_score = score
            best_entry = entry

    if best_score > 0:
        return best_entry

    if field == "age" and len(entries) == 2:
        ordered = []
        for entry in entries:
            try:
                visible_index = int(entry.get("visibleIndex"))
            except Exception:
                continue
            if visible_index not in exclude:
                ordered.append(entry)
        if len(ordered) == 1:
            return ordered[0]
        if len(ordered) == 2:
            return ordered[1]
    return None


def _derive_registration_state_from_page(page) -> dict:
    current_url = str(page.url or "")
    state = _extract_flow_state(None, current_url)
    # A successful SPA transition can keep /email-verification in the address
    # bar while replacing the form. Inspect the live DOM before trusting that
    # URL-derived OTP state.

    try:
        about_visible = bool(
            _page_evaluate_safe(
                page,
                """
                () => {
                  const inputs = Array.from(document.querySelectorAll("input:not([type='hidden'])"));
                  const text = String(document.body?.innerText || '').toLowerCase();
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                  };
                  const hasName = inputs.some((el) => {
                    if (!visible(el)) return false;
                    const hint = `${el.name || ''} ${el.id || ''} ${el.placeholder || ''}`.toLowerCase();
                    return hint.includes('name') || hint.includes('姓名') || hint.includes('全名') || hint.includes('氏名');
                  });
                  const hasAgeOrBirth = inputs.some((el) => {
                    if (!visible(el)) return false;
                    const hint = `${el.name || ''} ${el.id || ''} ${el.placeholder || ''}`.toLowerCase();
                    return hint.includes('age') || hint.includes('birth') || hint.includes('birthday') || hint.includes('年龄') || hint.includes('年齢') || hint.includes('生日') || hint.includes('生年月日') || hint.includes('umur') || hint.includes('usia');
                  });
                  return (hasName && hasAgeOrBirth) || text.includes('about you') || text.includes('あなたについて');
                }
                """,
                retries=3,
            )
        )
    except Exception:
        about_visible = False
    if about_visible:
        return _build_manual_flow_state("about_you", current_url)

    otp_selector = _find_first_visible_selector(page, OTP_INPUT_SELECTORS)
    if otp_selector:
        return _build_manual_flow_state("email_otp_verification", current_url)

    if _find_first_visible_selector(page, PASSWORD_INPUT_SELECTORS):
        page_type = "login_password" if _is_login_password_url(current_url) else "create_account_password"
        return _build_manual_flow_state(page_type, current_url)

    if state.get("page_type"):
        return state

    return state


def _wait_for_signup_entry_transition(
    page,
    log,
    timeout: int = 60,
    *,
    response_observer: _NetworkActivityObserver | None = None,
) -> dict:
    deadline = time.time() + timeout
    passwordless_clicked = False
    otp_sent_at = None
    processed_responses = 0
    while time.time() < deadline:
        if response_observer is not None:
            while processed_responses < len(response_observer.business_responses):
                response = response_observer.business_responses[processed_responses]
                processed_responses += 1
                status, response_url, data, response_text = _browser_response_details(response)
                response_state = _extract_flow_state(data or None, response_url)
                response_page_type = str(response_state.get("page_type") or "")
                if 200 <= status < 300 and response_page_type in {
                    "create_account_password",
                    "login_password",
                    "email_otp_verification",
                    "email_otp_send",
                    "about_you",
                    "add_phone",
                    "chatgpt_home",
                    "oauth_callback",
                }:
                    response_state["_route_source"] = "authorize_continue_response"
                    response_state["_route_response_status"] = status
                    if response_page_type == "email_otp_send":
                        response_state["page_type"] = "email_otp_verification"
                    if response_page_type in {
                        "email_otp_verification",
                        "email_otp_send",
                    }:
                        response_state["_page_otp_triggered"] = True
                        response_state["_otp_sent_at"] = otp_sent_at
                    return response_state
                if status >= 400:
                    error_text = _browser_response_error(data, response_text)
                    if error_text:
                        raise RuntimeError(
                            f"邮箱页 authorize/continue 失败: {error_text[:300]}"
                        )
        state = _derive_registration_state_from_page(page)
        if state.get("page_type") in {
            "create_account_password",
            "login_password",
            "email_otp_verification",
            "about_you",
            "add_phone",
            "chatgpt_home",
            "oauth_callback",
        }:
            if passwordless_clicked and state.get("page_type") == "email_otp_verification":
                state["_page_otp_triggered"] = True
                state["_otp_sent_at"] = otp_sent_at
            return state
        if not passwordless_clicked:
            click_started_at = time.time() - 8
            if _click_passwordless_login_if_available(
                page,
                log,
                context="邮箱页提交后",
            ):
                passwordless_clicked = True
                otp_sent_at = click_started_at
                time.sleep(0.5)
                continue
        error_text = _extract_auth_error_text(page)
        if error_text:
            raise RuntimeError(f"邮箱页提交失败: {error_text[:300]}")
        time.sleep(0.25)
    raise RuntimeError("邮箱页提交后未进入密码/验证码页面")


def _start_browser_signup_via_page(
    page,
    email: str,
    log,
) -> dict:
    entry_errors: list[str] = []
    for entry_url in (PLATFORM_LOGIN_ENTRY, f"{OPENAI_AUTH}/log-in"):
        try:
            log(f"打开 OpenAI 注册入口: {entry_url}")
            page.goto(entry_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            log(f"注册入口访问失败: {entry_url} -> {exc}")
            entry_errors.append(f"{entry_url}: {exc}")
            continue

        initial_state = _derive_registration_state_from_page(page)
        if initial_state.get("page_type") in {
            "create_account_password",
            "login_password",
            "email_otp_verification",
            "about_you",
            "add_phone",
        }:
            return initial_state

        email_selector = _wait_for_any_selector(page, EMAIL_INPUT_SELECTORS, timeout=12)
        if not email_selector:
            entry_errors.append(f"{entry_url}: 未找到邮箱输入框")
            continue
        if not _fill_input_like_user(page, email_selector, email):
            raise RuntimeError("邮箱页填写失败")
        log(f"邮箱页输入框: {email_selector}")

        inline_state = _derive_registration_state_from_page(page)
        if inline_state.get("page_type") in {"create_account_password", "login_password"}:
            return inline_state

        email_submit_at = time.time() - 8
        email_observer = _NetworkActivityObserver(
            page,
            (
                "/api/accounts/authorize/continue",
                "/api/accounts/login",
                "/api/accounts/log-in",
            ),
        )
        try:
            submit_selector = _click_first(page, EMAIL_SUBMIT_SELECTORS, timeout=8)
            if submit_selector:
                log(f"邮箱页已点击继续按钮: {submit_selector}")
            elif _submit_form_with_fallback(page, email_selector):
                log("邮箱页未找到可点击 Continue，已使用表单 fallback 提交")
            else:
                raise RuntimeError("邮箱页未找到 Continue 按钮")

            state = _wait_for_signup_entry_transition(
                page,
                log,
                timeout=60,
                response_observer=email_observer,
            )
        finally:
            email_observer.close()
        if state.get("page_type") == "email_otp_verification" and not state.get("_otp_sent_at"):
            state["_page_otp_triggered"] = True
            state["_otp_sent_at"] = email_submit_at
        return state

    detail = "; ".join(entry_errors[-2:])
    raise _BrowserSignupEntryUnavailable(
        f"未找到可用 OpenAI 注册入口邮箱输入框{f': {detail}' if detail else ''}"
    )


def _start_browser_signup_via_authorize(page, email: str, device_id: str, log) -> dict:
    log("访问 ChatGPT 首页...")
    page.goto(f"{CHATGPT_APP}/", wait_until="domcontentloaded", timeout=30000)

    log("获取 CSRF token...")
    csrf_token = _get_browser_csrf_token(page)
    if not csrf_token:
        raise RuntimeError("获取 CSRF token 失败")

    log(f"提交邮箱: {email}")
    authorize_url = _start_browser_signin(page, email, device_id, csrf_token)
    if not authorize_url:
        raise RuntimeError("提交邮箱失败，未获取 authorize URL")

    final_url = _browser_authorize(page, authorize_url, log)
    if not final_url:
        raise RuntimeError("访问 authorize URL 失败")
    return _derive_registration_state_from_page(page)


def _dump_debug(page, prefix: str) -> None:
    if str(os.environ.get("CHATGPT_BROWSER_DEBUG_ARTIFACTS") or "").lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    page.screenshot(path=f"/tmp/{prefix}.png")
    with open(f"/tmp/{prefix}.html", "w", encoding="utf-8") as handle:
        handle.write(page.content())


def _get_cookies(page) -> dict:
    return {c["name"]: c["value"] for c in page.context.cookies()}


def _import_browser_context_cookies(page, cookies: list[dict] | None, log) -> int:
    """Import explicit browser cookies without letting one malformed item abort the context."""
    imported = 0
    for item in cookies or []:
        if not isinstance(item, dict):
            continue
        normalized = {
            key: value
            for key, value in item.items()
            if key in {
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
        }
        if not normalized.get("name") or normalized.get("value") is None:
            continue
        if normalized.get("url"):
            normalized.pop("domain", None)
            normalized.pop("path", None)
        elif not normalized.get("domain"):
            continue
        try:
            page.context.add_cookies([normalized])
            imported += 1
        except Exception as exc:
            log(
                "协议 Cookie 导入跳过: "
                f"name={normalized.get('name')} domain={normalized.get('domain') or normalized.get('url')} "
                f"error={exc}"
            )
    return imported


def _random_chrome_ua() -> str:
    patch = random.randint(0, 220)
    return (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/136.0.7103.{patch} Safari/537.36"
    )


def _infer_sec_ch_ua(user_agent: str) -> str:
    match = re.search(r"Chrome/(\d+)", str(user_agent or ""))
    major = str(match.group(1) if match else "136")
    return f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not.A/Brand";v="99"'


def _build_browser_headers(
    *,
    user_agent: str,
    accept: str,
    referer: str = "",
    origin: str = "",
    content_type: str = "",
    navigation: bool = False,
    extra_headers: dict | None = None,
) -> dict:
    headers = {
        "user-agent": user_agent or _random_chrome_ua(),
        "accept-language": "en-US,en;q=0.9",
        "sec-ch-ua": _infer_sec_ch_ua(user_agent),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "accept": accept,
    }
    if referer:
        headers["referer"] = referer
    if origin:
        headers["origin"] = origin
    if content_type:
        headers["content-type"] = content_type
    if navigation:
        headers["sec-fetch-dest"] = "document"
        headers["sec-fetch-mode"] = "navigate"
        headers["sec-fetch-user"] = "?1"
        headers["upgrade-insecure-requests"] = "1"
    else:
        headers["sec-fetch-dest"] = "empty"
        headers["sec-fetch-mode"] = "cors"
    for key, value in dict(extra_headers or {}).items():
        if value is not None:
            headers[key] = value
    return headers


def _browser_pause(page, *, headed: bool = True):
    delay_ms = random.randint(150, 450) if headed else random.randint(60, 180)
    try:
        page.wait_for_timeout(delay_ms)
    except Exception:
        time.sleep(delay_ms / 1000)


def _generate_datadog_trace_headers() -> dict:
    trace_hex = secrets.token_hex(8).rjust(16, "0")
    parent_hex = secrets.token_hex(8).rjust(16, "0")
    trace_id = str(int(trace_hex, 16))
    parent_id = str(int(parent_hex, 16))
    return {
        "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def _infer_page_type(data: dict | None, current_url: str = "") -> str:
    raw = data if isinstance(data, dict) else {}
    page_type = str(((raw.get("page") or {}).get("type")) or "").strip().lower().replace("-", "_").replace("/", "_").replace(" ", "_")
    if page_type:
        return page_type
    url = (current_url or "").lower()
    if "code=" in url:
        return "oauth_callback"
    if "create-account/password" in url:
        return "create_account_password"
    if "email-verification" in url or "email-otp" in url:
        return "email_otp_verification"
    if "about-you" in url:
        return "about_you"
    if "log-in/password" in url:
        return "login_password"
    if "sign-in-with-chatgpt" in url and "consent" in url:
        return "consent"
    if "workspace" in url and "select" in url:
        return "workspace_selection"
    if "organization" in url and "select" in url:
        return "organization_selection"
    if "add-phone" in url:
        return "add_phone"
    if "/api/oauth/oauth2/auth" in url:
        return "external_url"
    if "chatgpt.com" in url:
        return "chatgpt_home"
    return ""


def _extract_flow_state(data: dict | None, current_url: str = "") -> dict:
    raw = data if isinstance(data, dict) else {}
    page = raw.get("page") or {}
    payload = page.get("payload") or {}
    continue_url = str(raw.get("continue_url") or payload.get("url") or "").strip()
    if continue_url and continue_url.startswith("/"):
        continue_url = urljoin(OPENAI_AUTH, continue_url)
    effective_url = continue_url or current_url
    return {
        "page_type": _infer_page_type(raw, effective_url),
        "continue_url": continue_url,
        "method": str(raw.get("method") or payload.get("method") or "GET").upper(),
        "current_url": effective_url,
        "payload": payload if isinstance(payload, dict) else {},
        "raw": raw,
    }


def _extract_code_from_url(url: str) -> str:
    if not url or "code=" not in url:
        return ""
    try:
        from urllib.parse import parse_qs, urlparse as _up

        parsed = _up(url)
        values = parse_qs(parsed.query, keep_blank_values=True)
        return str((values.get("code") or [""])[0] or "").strip()
    except Exception:
        return ""


def _normalize_url(target_url: str, base_url: str = OPENAI_AUTH) -> str:
    value = str(target_url or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    try:
        return urljoin(base_url, value)
    except Exception:
        return value


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        pad = "=" * ((4 - (len(payload) % 4)) % 4)
        return json.loads(base64.urlsafe_b64decode((payload + pad).encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


class _SentinelTokenGenerator:
    def __init__(self, device_id: str, user_agent: str):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent or _random_chrome_ua()
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= (h >> 16)
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= (h >> 13)
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= (h >> 16)
        return f"{h & 0xFFFFFFFF:08x}"

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":")).encode("utf-8")).decode("ascii")

    def _config(self) -> list:
        perf_now = 1000 + random.random() * 49000
        return [
            "1920x1080",
            time.strftime("%a, %d %b %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),
            4294705152,
            random.random(),
            self.user_agent,
            SENTINEL_SDK_URL,
            None,
            None,
            "en-US",
            "en-US,en",
            random.random(),
            "webkitTemporaryStorage−undefined",
            "location",
            "Object",
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16]),
            int(time.time() * 1000 - perf_now),
        ]

    def generate_requirements_token(self) -> str:
        cfg = self._config()
        cfg[3] = 1
        cfg[9] = round(5 + random.random() * 45)
        return "gAAAAAC" + self._b64(cfg)

    def generate_token(self, seed: str, difficulty: str) -> str:
        max_attempts = 500000
        cfg = self._config()
        start_ms = int(time.time() * 1000)
        diff = str(difficulty or "0")
        for nonce in range(max_attempts):
            cfg[3] = nonce
            cfg[9] = round(int(time.time() * 1000) - start_ms)
            encoded = self._b64(cfg)
            digest = self._fnv1a32((seed or "") + encoded)
            if digest[: len(diff)] <= diff:
                return "gAAAAAB" + encoded + "~S"
        return "gAAAAAB" + self._b64(None)


def _browser_fetch(page, url: str, *, method: str = "GET", headers: dict | None = None, body: str | None = None, redirect: str = "manual", timeout_ms: int = 30000) -> dict:
    _wait_for_auth_page_settle(page, timeout=6.0)
    try:
        return _page_evaluate_safe(
            page,
            """
        async ({ url, method, headers, body, redirect, timeoutMs }) => {
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(new Error(`fetch timeout after ${timeoutMs}ms`)), timeoutMs);
          try {
            const resp = await fetch(url, {
              method,
              headers: headers || {},
              body: body === null ? undefined : body,
              redirect,
              signal: controller.signal,
            });
            const respHeaders = {};
            resp.headers.forEach((v, k) => { respHeaders[k] = v; });
            let text = '';
            try { text = await resp.text(); } catch {}
            let data = null;
            try { data = JSON.parse(text); } catch {}
            return { ok: resp.ok, status: resp.status, url: resp.url || url, headers: respHeaders, text, data };
          } catch (e) {
            return { ok: false, status: 0, url, headers: {}, text: String(e && e.message || e), data: null };
          } finally {
            clearTimeout(timer);
          }
        }
        """,
            {
                "url": url,
                "method": method,
                "headers": headers or {},
                "body": body,
                "redirect": redirect,
                "timeoutMs": timeout_ms,
            },
            retries=4,
        )
    except Exception as exc:
        return {
            "ok": False,
            "status": 0,
            "url": url,
            "headers": {},
            "text": f"browser_fetch_failed: {exc}",
            "data": None,
        }


def _build_browser_sentinel_token(page, device_id: str, flow: str, user_agent: str) -> str:
    generator = _SentinelTokenGenerator(device_id, user_agent)
    req_body = json.dumps(
        {"p": generator.generate_requirements_token(), "id": device_id, "flow": flow},
        separators=(",", ":"),
    )
    result = _browser_fetch(
        page,
        SENTINEL_REQ_URL,
        method="POST",
        headers=_build_browser_headers(
            user_agent=user_agent,
            accept="*/*",
            referer=SENTINEL_FRAME_URL,
            origin=SENTINEL_BASE,
            content_type="text/plain;charset=UTF-8",
            extra_headers={
                "sec-fetch-site": "same-origin",
            },
        ),
        body=req_body,
        redirect="follow",
    )
    data = result.get("data") or {}
    challenge_token = str(data.get("token") or "").strip()
    if not challenge_token:
        return ""
    pow_meta = data.get("proofofwork") or {}
    if pow_meta.get("required") and pow_meta.get("seed"):
        p_value = generator.generate_token(str(pow_meta.get("seed") or ""), str(pow_meta.get("difficulty") or "0"))
    else:
        p_value = generator.generate_requirements_token()
    return json.dumps(
        {
            "p": p_value,
            "t": "",
            "c": challenge_token,
            "id": device_id,
            "flow": flow,
        },
        separators=(",", ":"),
    )


def _submit_browser_user_register(page, email: str, password: str, device_id: str, user_agent: str) -> dict:
    headers = _build_browser_headers(
        user_agent=user_agent,
        accept="application/json",
        referer=f"{OPENAI_AUTH}/create-account/password",
        origin=OPENAI_AUTH,
        content_type="application/json",
        extra_headers={
            "sec-fetch-site": "same-origin",
            "oai-device-id": device_id,
            **_generate_datadog_trace_headers(),
        },
    )
    sentinel = _build_browser_sentinel_token(page, device_id, "username_password_create", user_agent)
    if sentinel:
        headers["openai-sentinel-token"] = sentinel
    _browser_pause(page)
    return _browser_fetch(
        page,
        f"{OPENAI_AUTH}/api/accounts/user/register",
        method="POST",
        headers=headers,
        body=json.dumps({"username": email, "password": password}),
        redirect="follow",
    )


def _send_browser_email_otp(
    page,
    *,
    device_id: str = "",
    user_agent: str = "",
    referer: str = "",
) -> dict:
    _browser_pause(page)
    effective_user_agent = user_agent or _random_chrome_ua()
    headers = _build_browser_headers(
        user_agent=effective_user_agent,
        accept="application/json, text/plain, */*",
        referer=referer or f"{OPENAI_AUTH}/create-account/password",
        origin=OPENAI_AUTH,
        extra_headers={
            "sec-fetch-site": "same-origin",
            "oai-device-id": device_id,
            **_generate_datadog_trace_headers(),
        },
    )
    if device_id:
        try:
            sentinel = _build_browser_sentinel_token(
                page,
                device_id,
                "email_otp_send",
                effective_user_agent,
            )
        except Exception:
            sentinel = ""
        if sentinel:
            headers["openai-sentinel-token"] = sentinel
    return _browser_fetch(
        page,
        f"{OPENAI_AUTH}/api/accounts/email-otp/send",
        method="GET",
        headers=headers,
        redirect="follow",
    )


def _decode_oauth_session_cookie(cookies_dict: dict) -> dict:
    raw = str(cookies_dict.get("oai-client-auth-session") or "").strip()
    if not raw:
        return {}
    first = raw.split(".")[0]
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            pad = "=" * ((4 - (len(first) % 4)) % 4)
            decoded = decoder((first + pad).encode("ascii")).decode("utf-8")
            parsed = json.loads(decoded)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    return {}


def _extract_workspace_from_consent_html(session, consent_url: str) -> dict:
    try:
        response = session.get(consent_url, allow_redirects=True, timeout=30)
        html = response.text or ""
        if "workspaces" not in html:
            return {}
        ids = re.findall(r'"id"(?:,|:)"([0-9a-f-]{36})"', html, flags=re.I)
        kinds = re.findall(r'"kind"(?:,|:)"([^"]+)"', html, flags=re.I)
        if not ids:
            return {}
        seen: set[str] = set()
        workspaces: list[dict] = []
        for idx, workspace_id in enumerate(ids):
            if workspace_id in seen:
                continue
            seen.add(workspace_id)
            item = {"id": workspace_id}
            if idx < len(kinds):
                item["kind"] = kinds[idx]
            workspaces.append(item)
        return {"workspaces": workspaces} if workspaces else {}
    except Exception:
        return {}


def _seed_session_cookies(session, cookies_dict: dict):
    for name, value in cookies_dict.items():
        for domain in [".openai.com", ".chatgpt.com", ".auth.openai.com", "auth.openai.com", "chatgpt.com"]:
            try:
                session.cookies.set(name, value, domain=domain, path="/")
            except Exception:
                pass


def _follow_redirects_for_code(session, start_url: str, log, *, max_redirects: int = 12) -> str:
    current_url = start_url
    for idx in range(max_redirects):
        response = session.get(current_url, allow_redirects=False, timeout=30)
        log(f"  redirect-follow[{idx+1}] {response.status_code} {str(current_url)[:140]}")
        location = str(response.headers.get("Location") or "").strip()
        if not location:
            break
        next_url = urljoin(current_url, location)
        code = _extract_code_from_url(next_url)
        if code:
            return next_url
        if response.status_code not in (301, 302, 303, 307, 308):
            break
        current_url = next_url
    return ""


def _complete_oauth_with_session(cookies_dict: dict, oauth_start, proxy: str | None, log) -> dict | None:
    from .oauth import submit_callback_url
    from curl_cffi import requests as cffi_requests

    s = cffi_requests.Session(impersonate="chrome131")
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    _seed_session_cookies(s, cookies_dict)

    try:
        session_meta = _decode_oauth_session_cookie(cookies_dict)
        consent_url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
        workspaces = list(session_meta.get("workspaces") or [])
        if not workspaces:
            session_meta = _extract_workspace_from_consent_html(s, consent_url)
            workspaces = list(session_meta.get("workspaces") or [])
        if not workspaces:
            log("  ⚠️ 缺少 oai-client-auth-session workspaces，OAuth 失败")
            return None
        workspace_id = str((workspaces[0] or {}).get("id") or "").strip()
        log(f"  选择 workspace: {workspace_id}")
        ws_resp = s.post(
            "https://auth.openai.com/api/accounts/workspace/select",
            headers={
                "accept": "application/json",
                "referer": consent_url,
                "origin": OPENAI_AUTH,
                "content-type": "application/json",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            },
            data=json.dumps({"workspace_id": workspace_id}),
            allow_redirects=False,
            timeout=30,
        )
        log(f"  workspace/select -> {ws_resp.status_code}")

        next_url = str(ws_resp.headers.get("Location") or "").strip()
        next_data = {}
        if not next_url:
            try:
                next_data = ws_resp.json() or {}
            except Exception:
                next_data = {}
            next_url = str(next_data.get("continue_url") or "").strip()
        next_url = _normalize_url(next_url, consent_url)
        direct_code = _extract_code_from_url(next_url)
        if direct_code:
            result_json = submit_callback_url(
                callback_url=next_url,
                expected_state=oauth_start.state,
                code_verifier=oauth_start.code_verifier,
                proxy_url=proxy,
            )
            return json.loads(result_json)

        orgs = list((((next_data.get("data") or {}).get("orgs")) or []))
        if orgs and orgs[0].get("id"):
            org_id = str(orgs[0].get("id") or "").strip()
            org_body = {"org_id": org_id}
            projects = list(orgs[0].get("projects") or [])
            if projects and projects[0].get("id"):
                org_body["project_id"] = str(projects[0].get("id") or "").strip()
            log(f"  选择 organization: {org_id}")
            org_resp = s.post(
                "https://auth.openai.com/api/accounts/organization/select",
                headers={
                    "accept": "application/json",
                    "referer": consent_url,
                    "origin": OPENAI_AUTH,
                    "content-type": "application/json",
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
                },
                data=json.dumps(org_body),
                allow_redirects=False,
                timeout=30,
            )
            log(f"  organization/select -> {org_resp.status_code}")
            next_url = str(org_resp.headers.get("Location") or "").strip() or next_url
            if not next_url:
                try:
                    org_data = org_resp.json() or {}
                    next_url = str(org_data.get("continue_url") or "").strip()
                    if not next_url:
                        org_state = _extract_flow_state(org_data, str(org_resp.url))
                        next_url = org_state.get("continue_url") or org_state.get("current_url") or ""
                except Exception:
                    next_url = ""
            next_url = _normalize_url(next_url, consent_url)

        if not next_url and next_data:
            state = _extract_flow_state(next_data, str(ws_resp.url))
            next_url = state.get("continue_url") or state.get("current_url") or ""
            next_url = _normalize_url(next_url, consent_url)

        if not next_url:
            next_url = "https://auth.openai.com/api/oauth/oauth2/auth?" + oauth_start.auth_url.split("?", 1)[1]

        callback_url = _follow_redirects_for_code(s, next_url, log)
        if not callback_url:
            log("  ⚠️ 未能跟到 OAuth callback")
            return None
        result_json = submit_callback_url(
            callback_url=callback_url,
            expected_state=oauth_start.state,
            code_verifier=oauth_start.code_verifier,
            proxy_url=proxy,
        )
        return json.loads(result_json)
    except Exception as e:
        log(f"  OAuth 会话补全异常: {e}")
        return None


def _submit_callback_result(callback_url: str, oauth_start, proxy: str | None) -> dict:
    from .oauth import submit_callback_url

    result_json = submit_callback_url(
        callback_url=callback_url,
        expected_state=oauth_start.state,
        code_verifier=oauth_start.code_verifier,
        redirect_uri=oauth_start.redirect_uri,
        client_id=oauth_start.client_id,
        proxy_url=proxy,
    )
    return json.loads(result_json)


def _extract_callback_url_from_exception(exc: Exception) -> str:
    text = str(exc or "")
    if not text:
        return ""
    match = re.search(r"(https?://localhost[^\s\"')]+)", text, flags=re.I)
    if not match:
        return ""
    callback_url = str(match.group(1) or "").strip().rstrip(".,")
    return callback_url if _extract_code_from_url(callback_url) else ""


def _derive_oauth_state_from_page(page) -> dict:
    state = _derive_registration_state_from_page(page)
    if state.get("page_type"):
        return state
    current_url = str(page.url or "")
    if _find_first_selector(page, EMAIL_INPUT_SELECTORS):
        return _build_manual_flow_state("login_email", current_url)
    return _extract_flow_state(None, current_url)


def _submit_login_email_via_page(page, email: str, log) -> dict:
    input_selector = _wait_for_any_selector(page, EMAIL_INPUT_SELECTORS, timeout=15)
    if not input_selector:
        raise RuntimeError("OAuth 邮箱页未找到输入框")
    if not _fill_input_like_user(page, input_selector, email):
        raise RuntimeError("OAuth 邮箱页填写失败")
    log(f"OAuth 邮箱页输入框: {input_selector}")
    _browser_pause(page)

    start_url = str(page.url or "")
    otp_sent_at = time.time() - 8
    submit_selector = _click_first(page, EMAIL_SUBMIT_SELECTORS, timeout=8)
    if submit_selector:
        log(f"OAuth 邮箱页已点击继续按钮: {submit_selector}")
    elif _submit_form_with_fallback(page, input_selector):
        log("OAuth 邮箱页未找到可点击 Continue，已使用表单 fallback 提交")
    else:
        raise RuntimeError("OAuth 邮箱页未找到 Continue 按钮")

    deadline = time.time() + 60
    last_url = start_url
    passwordless_clicked = False
    while time.time() < deadline:
        current_url = str(page.url or "")
        last_url = current_url or last_url
        if not passwordless_clicked:
            passwordless_started_at = time.time() - 8
            if _click_passwordless_login_if_available(
                page,
                log,
                context="OAuth 邮箱页提交后",
            ):
                passwordless_clicked = True
                otp_sent_at = passwordless_started_at
                time.sleep(0.5)
                continue
        state = _derive_oauth_state_from_page(page)
        page_type = str(state.get("page_type") or "")
        if page_type in {
            "login_password",
            "create_account_password",
            "email_otp_verification",
            "about_you",
            "consent",
            "workspace_selection",
            "organization_selection",
            "add_phone",
            "external_url",
            "oauth_callback",
            "chatgpt_home",
        }:
            return {
                "ok": True,
                "status": 200,
                "url": current_url,
                "data": None,
                "text": "",
                "otp_triggered": page_type == "email_otp_verification" or passwordless_clicked,
                "otp_sent_at": otp_sent_at,
            }
        if current_url != start_url and page_type != "login_email":
            return {
                "ok": True,
                "status": 200,
                "url": current_url,
                "data": None,
                "text": "",
                "otp_triggered": passwordless_clicked,
                "otp_sent_at": otp_sent_at,
            }
        error_text = _extract_auth_error_text(page)
        if error_text:
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
        time.sleep(0.5)
    return {"ok": False, "status": 0, "url": last_url, "data": None, "text": "OAuth 邮箱页提交后未跳转"}


def _invoke_otp_callback(otp_callback, payload: dict | None = None):
    """Call OTP providers from both legacy no-arg and contextual forms."""
    if not callable(otp_callback):
        return None
    context = dict(payload or {})
    try:
        return otp_callback(context)
    except TypeError:
        try:
            return otp_callback(**context)
        except TypeError:
            return otp_callback()


def _invoke_otp_release(otp_callback, code: str, log) -> None:
    """Ask the parent task to un-exclude an OTP that did not advance the SPA."""
    normalized = str(code or "").strip()
    if not normalized or not callable(otp_callback):
        return
    try:
        _invoke_otp_callback(
            otp_callback,
            {
                "action": "release_code",
                "code": normalized,
                "phase": "browser_register_email_otp",
            },
        )
    except Exception as exc:
        log(f"释放可复用验证码失败（可忽略）: {exc}")


def _ensure_about_you_page(page, target_url: str, log) -> None:
    """Reach about_you without racing an in-flight SPA navigation.

    After OTP validate, Auth often already navigates to /about-you. A hard
    page.goto then collides and raises NS_BINDING_ABORTED even though the
    destination is correct.
    """
    current = str(page.url or "")
    if "about-you" in current:
        return
    try:
        live = _derive_registration_state_from_page(page)
    except Exception as exc:
        if not _is_navigation_context_error(exc):
            raise
        _wait_for_auth_page_settle(page, timeout=6.0, log=log)
        live = _derive_registration_state_from_page(page)
    if str(live.get("page_type") or "") == "about_you":
        return

    dest = str(target_url or f"{OPENAI_AUTH}/about-you").strip() or f"{OPENAI_AUTH}/about-you"
    # Never navigate to API continue_url paths from OTP responses.
    if "/api/accounts/" in dest:
        dest = f"{OPENAI_AUTH}/about-you"
    log(f"跳转到 about_you 页面: {dest[:120]}")
    try:
        page.goto(dest, wait_until="domcontentloaded", timeout=30000)
        return
    except Exception as exc:
        message = str(exc or "")
        if "NS_BINDING_ABORTED" not in message and "Navigation" not in message:
            raise
        log(f"about_you 导航被中断，改为等待 SPA settle: {message[:160]}")
        _wait_for_auth_page_settle(page, timeout=12.0, log=log)
        try:
            live = _derive_registration_state_from_page(page)
        except Exception as derive_exc:
            if not _is_navigation_context_error(derive_exc):
                raise
            _wait_for_auth_page_settle(page, timeout=6.0, log=log)
            live = _derive_registration_state_from_page(page)
        if str(live.get("page_type") or "") == "about_you" or "about-you" in str(page.url or ""):
            log("about_you 页面已在导航中断后可用，继续填写")
            return
        # One more bounded attempt after the abort — common when OTP SPA and
        # our goto raced once.
        try:
            page.goto(dest, wait_until="domcontentloaded", timeout=30000)
        except Exception as retry_exc:
            retry_msg = str(retry_exc or "")
            if "NS_BINDING_ABORTED" in retry_msg or "Navigation" in retry_msg:
                _wait_for_auth_page_settle(page, timeout=10.0, log=log)
                if "about-you" in str(page.url or "") or str(
                    (_derive_registration_state_from_page(page) or {}).get("page_type") or ""
                ) == "about_you":
                    log("about_you 重试导航中断后页面已可用")
                    return
            raise


def _do_codex_oauth(
    page,
    cookies_dict: dict,
    email: str,
    password: str,
    otp_callback,
    phone_callback,
    proxy: str | None,
    log,
    *,
    strict_browser: bool = False,
) -> dict | None:
    """在真实浏览器会话内完成 Codex OAuth，返回完整 token 包。"""
    from .oauth import generate_oauth_url
    from .oauth import (
        OAUTH_CLIENT_ID as CODEX_CLIENT_ID,
        OAUTH_REDIRECT_URI as CODEX_REDIRECT_URI,
        OAUTH_SCOPE as CODEX_SCOPE,
    )

    oauth_start = generate_oauth_url(
        redirect_uri=CODEX_REDIRECT_URI,
        scope=CODEX_SCOPE,
        client_id=CODEX_CLIENT_ID,
    )
    try:
        user_agent = str(page.evaluate("() => navigator.userAgent") or "").strip() or _random_chrome_ua()
    except Exception:
        user_agent = _random_chrome_ua()
    device_id = str(cookies_dict.get("oai-did") or uuid.uuid4())
    log(f"  OAuth state={oauth_start.state[:20]}...")
    oauth_email_submitted_at: float | None = None
    oauth_password_verified = False
    oauth_otp_attempt = 0

    try:
        try:
            page.goto(oauth_start.auth_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            callback_url = _extract_callback_url_from_exception(exc)
            if callback_url:
                log(f"  OAuth bootstrap 直接捕获 callback: {callback_url[:100]}...")
                return _submit_callback_result(callback_url, oauth_start, proxy)
            raise

        current_url = str(page.url or "")
        log(f"  OAuth bootstrap -> {current_url[:100]}...")

        for step in range(20):
            state = _derive_oauth_state_from_page(page)
            current_url = str(page.url or "")
            next_url = str(state.get("continue_url") or "").strip()
            log(
                f"  OAuth state step[{step+1}/20]: "
                f"page={state.get('page_type') or '-'} next={next_url[:60]}"
                f" url={current_url[:120]}"
            )

            callback_url = ""
            if _extract_code_from_url(current_url):
                callback_url = current_url
            elif _extract_code_from_url(next_url):
                callback_url = next_url
            if callback_url:
                return _submit_callback_result(callback_url, oauth_start, proxy)

            page_oauth_url = _get_page_oauth_url(page)
            if (
                page_oauth_url
                and page_oauth_url != current_url
                and _oauth_url_matches_state(page_oauth_url, oauth_start.state)
            ):
                log("  OAuth 页面检测到更新的授权链接，跟随页面授权链接...")
                page.goto(page_oauth_url, wait_until="domcontentloaded", timeout=30000)
                continue

            if state["page_type"] == "login_email":
                log("  OAuth 页面需要邮箱登录，提交邮箱...")
                email_resp = _submit_login_email_via_page(page, email, log)
                log(f"  OAuth 邮箱页提交状态: {email_resp.get('status', 0)}")
                if not email_resp.get("ok"):
                    raise RuntimeError(f"OAuth 邮箱页提交失败: {(email_resp.get('text') or '')[:300]}")
                # The transition helper samples before the action that can send
                # the code. Sampling after its 60-second wait can filter out the
                # very OTP this transaction triggered.
                oauth_email_submitted_at = email_resp.get("otp_sent_at")
                if oauth_email_submitted_at is None:
                    oauth_email_submitted_at = time.time() - 8
                continue

            if state["page_type"] in {"login_password", "create_account_password"}:
                if oauth_password_verified:
                    log("  OAuth 密码请求已明确成功，等待页面离开旧密码 DOM...")
                    time.sleep(0.5)
                    continue
                log("  OAuth 页面需要密码登录，提交密码...")
                # OAuth 流程中直接填密码登录，不尝试恢复到注册态
                password_resp = _submit_oauth_password_direct(page, password, log)
                log(f"  OAuth 密码页提交状态: {password_resp.get('status', 0)}")
                if not password_resp.get("ok"):
                    raise RuntimeError(f"OAuth 密码页提交失败: {(password_resp.get('text') or '')[:300]}")
                oauth_password_verified = bool(password_resp.get("password_verified"))
                next_state = password_resp.get("next_state")
                if isinstance(next_state, dict):
                    next_target = _normalize_url(
                        str(next_state.get("continue_url") or next_state.get("current_url") or ""),
                        OPENAI_AUTH,
                    )
                    if (
                        next_target
                        and next_target != str(page.url or "")
                        and "/api/accounts/" not in next_target
                    ):
                        try:
                            page.goto(next_target, wait_until="domcontentloaded", timeout=30000)
                        except Exception as exc:
                            log(f"  OAuth 密码响应后续导航异常: {exc}")
                continue

            if state["page_type"] == "email_otp_verification":
                if not otp_callback:
                    log("  ⚠️ OAuth 需要邮箱 OTP 但没有 otp_callback")
                    return None
                oauth_otp_attempt += 1
                otp_sent_at = oauth_email_submitted_at
                # If the first code was rejected or the flow entered the OTP
                # page directly, explicitly request a fresh code before the
                # next mailbox read. This mirrors OAuthClient's resend path.
                if otp_sent_at is None or oauth_otp_attempt > 1:
                    sent_ok, sent_at = _send_browser_oauth_email_otp(
                        page,
                        device_id=device_id,
                        user_agent=user_agent,
                        referer=str(
                            state.get("current_url")
                            or state.get("continue_url")
                            or f"{OPENAI_AUTH}/email-verification"
                        ),
                        log=log,
                    )
                    if sent_ok and sent_at is not None:
                        otp_sent_at = sent_at
                        oauth_email_submitted_at = sent_at
                    else:
                        log("  OAuth OTP 重发未确认成功，继续按现有页面等待")
                log("  OAuth 等待邮箱验证码...")
                callback_payload = {
                    "otp_sent_at": otp_sent_at,
                    "phase": "browser_oauth_email_otp",
                    "page_type": "email_otp_verification",
                }
                callback_value = _invoke_otp_callback(otp_callback, callback_payload)
                if isinstance(callback_value, dict):
                    code = str(
                        callback_value.get("code")
                        or callback_value.get("otp")
                        or callback_value.get("value")
                        or ""
                    ).strip()
                else:
                    code = str(callback_value or "").strip()
                if not code:
                    log("  ⚠️ OAuth OTP 获取失败")
                    if oauth_otp_attempt < 3:
                        oauth_email_submitted_at = None
                        continue
                    return None
                otp_resp = _submit_otp_via_page(
                    page,
                    code,
                    log,
                    device_id=device_id,
                    user_agent=user_agent,
                    referer=str(
                        state.get("current_url")
                        or state.get("continue_url")
                        or f"{OPENAI_AUTH}/email-verification"
                    ),
                    assume_success_without_state=False,
                )
                log(f"  OAuth 验证码页提交状态: {otp_resp.get('status', 0)}")
                if not otp_resp.get("ok"):
                    detail = str(otp_resp.get("text") or "OAuth 验证码校验失败")[:300]
                    log(f"  OAuth OTP 未推进状态: {detail}")
                    if otp_resp.get("otp_committed"):
                        raise RuntimeError(
                            f"OAuth 验证码已成功提交但页面未推进: {detail}"
                        )
                    if oauth_otp_attempt < 3:
                        oauth_email_submitted_at = None
                        continue
                    raise RuntimeError(f"OAuth 验证码校验失败: {detail}")
                next_state = _extract_flow_state(
                    otp_resp.get("data") if isinstance(otp_resp.get("data"), dict) else None,
                    str(otp_resp.get("url") or page.url or ""),
                )
                if not next_state.get("page_type") or next_state.get("page_type") == "email_otp_verification":
                    next_state = _derive_oauth_state_from_page(page)
                if next_state.get("page_type") and next_state.get("page_type") != "email_otp_verification":
                    state = next_state
                    target_url = _normalize_url(
                        str(next_state.get("continue_url") or next_state.get("current_url") or ""),
                        OPENAI_AUTH,
                    )
                    if target_url and target_url != str(page.url or "") and "api/accounts/" not in target_url:
                        try:
                            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                        except Exception as exc:
                            callback_url = _extract_callback_url_from_exception(exc)
                            if callback_url:
                                return _submit_callback_result(callback_url, oauth_start, proxy)
                            log(f"  OAuth OTP 后续页面导航异常: {exc}")
                continue

            if state["page_type"] == "about_you":
                log("  OAuth 页面出现 about_you，继续页面填写...")
                about_resp = _submit_about_you_via_page(page, log)
                log(f"  OAuth about_you 提交状态: {about_resp.get('status', 0)}")
                if not about_resp.get("ok"):
                    raise RuntimeError(f"OAuth about_you 提交失败: {(about_resp.get('text') or '')[:300]}")
                continue

            if state["page_type"] in {"consent", "workspace_selection", "organization_selection", "external_url"}:
                browser_result = _complete_oauth_in_browser(page, oauth_start, proxy, log)
                if browser_result:
                    return browser_result
                if not strict_browser:
                    cookies_dict = _get_cookies(page)
                    session_result = _complete_oauth_with_session(cookies_dict, oauth_start, proxy, log)
                    if session_result:
                        return session_result
                log("  ⚠️ 页面已到 consent/workspace，但会话补全失败")
                return None

            if state["page_type"] == "add_phone":
                if phone_callback:
                    log("  OAuth 检测到 add_phone，优先执行短信验证...")
                    try:
                        _handle_add_phone_challenge(
                            page, phone_callback,
                            device_id=device_id, user_agent=user_agent,
                            log=log, resume_url=oauth_start.auth_url,
                        )
                        continue
                    except Exception as exc:
                        log(f"  短信验证失败，停止 OAuth 流程: {exc}")
                        return None

                # 先尝试跳过 add_phone，直接重新访问 OAuth 授权 URL
                # 用户已登录，重新访问 auth URL 应该能直接跳到 callback
                log("  检测到 add_phone，尝试跳过...")
                try:
                    page.goto(oauth_start.auth_url, wait_until="domcontentloaded", timeout=15000)
                    time.sleep(2)
                    current_url = str(page.url or "")

                    # 检查是否直接拿到了 callback
                    callback_url = ""
                    if "code=" in current_url:
                        callback_url = current_url
                    else:
                        # 可能需要跟随重定向
                        for _ in range(5):
                            time.sleep(1)
                            current_url = str(page.url or "")
                            if "code=" in current_url:
                                callback_url = current_url
                                break

                    if callback_url:
                        log("  ✓ 成功跳过 add_phone，获取到 OAuth callback")
                        return _submit_callback_result(callback_url, oauth_start, proxy)

                    # 检查页面状态
                    skip_state = _derive_registration_state_from_page(page)
                    if skip_state.get("page_type") in {"consent", "workspace_selection", "organization_selection"}:
                        log("  ✓ 跳过 add_phone 到达 consent 页面")
                        # 尝试在浏览器里完成 consent 流程
                        browser_result = _complete_oauth_in_browser(page, oauth_start, proxy, log)
                        if browser_result:
                            return browser_result
                        if not strict_browser:
                            cookies_dict = _get_cookies(page)
                            session_result = _complete_oauth_with_session(cookies_dict, oauth_start, proxy, log)
                            if session_result:
                                return session_result

                    if skip_state.get("page_type") == "add_phone":
                        log("  跳过失败，仍在 add_phone 页面")
                    else:
                        log(f"  跳过后页面状态: {skip_state.get('page_type') or '-'}")
                        # 继续状态机循环
                        continue

                except Exception as exc:
                    callback_url = _extract_callback_url_from_exception(exc)
                    if callback_url:
                        return _submit_callback_result(callback_url, oauth_start, proxy)
                    log(f"  跳过 add_phone 异常: {exc}")

                log("  ⚠️ add_phone 无法跳过且无可用接码服务")
                return None

            # chatgpt_home: 页面可能正在 JS 重定向（如跳转到 add-phone）
            # 等待更长时间让重定向完成
            if state["page_type"] == "chatgpt_home":
                # 检查是否是错误页面
                if "error" in current_url:
                    error_msg = current_url.split("error=")[-1].split("&")[0] if "error=" in current_url else "unknown"
                    log(f"  OAuth 错误页面: {error_msg} url={current_url[:150]}")
                    raise RuntimeError(f"OpenAI OAuth 错误: {error_msg}")
                time.sleep(2)
                new_url = str(page.url or "")
                if new_url != current_url:
                    continue
                if not strict_browser:
                    cookies_dict = _get_cookies(page)
                    for ck, cv in cookies_dict.items():
                        if "session" in ck.lower() and cv:
                            log(f"  chatgpt_home 检测到 session cookie: {ck}")
                            session_result = _complete_oauth_with_session(cookies_dict, oauth_start, proxy, log)
                            if session_result:
                                return session_result
                            break
                continue

            target_url = _normalize_url(state.get("continue_url") or "", OPENAI_AUTH)
            if target_url and target_url != current_url:
                try:
                    page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                except Exception as exc:
                    callback_url = _extract_callback_url_from_exception(exc)
                    if callback_url:
                        return _submit_callback_result(callback_url, oauth_start, proxy)
                    log(f"  OAuth navigation failed: {exc}")
                    break
                continue

            error_text = _extract_auth_error_text(page)
            if error_text:
                raise RuntimeError(f"OAuth 页面错误: {error_text[:300]}")
            time.sleep(0.5)
    except Exception as e:
        log(f"  OAuth 异常: {e}")
        return None

    cookies_dict = _get_cookies(page)
    if not strict_browser:
        result = _complete_oauth_with_session(cookies_dict, oauth_start, proxy, log)
        if result:
            return result

    session_token = cookies_dict.get("__Secure-next-auth.session-token", "")
    if not session_token:
        log("  ⚠️ 无 session_token，OAuth 失败")
        return None
    log("  ⚠️ 完整 OAuth 失败，回退 session access_token")
    return None


def _wait_for_access_token(page, timeout: int = 60) -> str:
    return str((_wait_for_web_session(page, timeout=timeout) or {}).get("accessToken") or "")


def _cookie_names_summary(page) -> str:
    try:
        cookies = list(page.context.cookies() or [])
    except Exception:
        return "-"
    names = []
    for item in cookies:
        name = str(item.get("name") or "").strip()
        domain = str(item.get("domain") or "").strip()
        if not name:
            continue
        if any(
            key in name.lower()
            for key in (
                "session",
                "auth",
                "csrf",
                "oai-",
                "cf_",
                "login",
                "did",
            )
        ):
            names.append(f"{domain}:{name}")
    return ", ".join(sorted(set(names))[:40]) or f"count={len(cookies)}"


def _fetch_chatgpt_session_payload(page) -> dict:
    """Fetch /api/auth/session from the live browser context."""
    try:
        result = _page_evaluate_safe(
            page,
            """
            async () => {
              try {
                const r = await fetch('https://chatgpt.com/api/auth/session', {
                  credentials: 'include',
                  headers: { 'accept': 'application/json' },
                });
                let data = null;
                let text = '';
                try { text = await r.text(); } catch {}
                try { data = text ? JSON.parse(text) : null; } catch {}
                return {
                  status: r.status,
                  ok: !!r.ok,
                  data: (data && typeof data === 'object') ? data : {},
                  text: String(text || '').slice(0, 180),
                };
              } catch (e) {
                return {
                  status: 0,
                  ok: false,
                  data: {},
                  text: String(e && e.message || e),
                };
              }
            }
            """,
            retries=3,
        )
    except Exception as exc:
        return {"status": 0, "ok": False, "data": {}, "text": str(exc)}
    return result if isinstance(result, dict) else {}


def _browser_chatgpt_openai_signin_bridge(
    page, log, *, email: str = "", device_id: str = ""
) -> dict | None:
    """Use next-auth signin/openai so OpenAI auth cookies mint a ChatGPT session.

    Prefer the same ``_browser_fetch`` / cookie-jar helpers used by the signup
    authorize entry. The previous page-world fetch often returned HTTP 200 with
    an empty body (``missing csrfToken``) after platform callback.

    Returns session JSON when accessToken is already available after the bridge
    (so the outer waiter must not discard a late hit after its deadline).
    """
    email = str(email or "").strip()
    device_id = str(device_id or "").strip()
    log("Web Session 桥接: ChatGPT next-auth signin/openai")
    try:
        page.goto(f"{CHATGPT_APP}/", wait_until="commit", timeout=20000)
    except Exception as exc:
        log(f"Web Session 桥接首页导航异常: {exc}")
    _wait_for_auth_page_settle(page, timeout=5.0, log=log)

    # Seed device id cookies on chatgpt domain before CSRF/signin.
    if device_id:
        try:
            _seed_browser_device_id(page, device_id)
        except Exception as exc:
            log(f"Web Session 桥接写入 oai-did 失败（可继续）: {exc}")

    csrf_token = _get_browser_csrf_token(page, log=log)
    if not csrf_token:
        # Hard reload once — next-auth often sets csrf cookie only after a full document load.
        try:
            page.reload(wait_until="commit", timeout=20000)
        except Exception as exc:
            log(f"Web Session 桥接 reload 异常: {exc}")
        _wait_for_auth_page_settle(page, timeout=5.0, log=log)
        csrf_token = _get_browser_csrf_token(page, log=log)
    if not csrf_token:
        log("Web Session 桥接失败: 无法获取 csrfToken")
        return None

    authorize_url = _start_browser_signin(
        page,
        email,
        device_id,
        csrf_token,
        screen_hint="login",
        log=log,
    )
    if not authorize_url:
        # A stale/encoded CSRF cookie makes next-auth return its own signin page
        # instead of OpenAI authorize. Reload once to mint a fresh transaction
        # before trying the signup-compatible hint.
        log("Web Session 桥接首次 signin 未返回 authorize，刷新 CSRF 后重试")
        try:
            page.reload(wait_until="commit", timeout=20000)
        except Exception as exc:
            log(f"Web Session 桥接刷新 CSRF 导航异常: {exc}")
        _wait_for_auth_page_settle(page, timeout=5.0, log=log)
        refreshed_csrf = _get_browser_csrf_token(page, log=log)
        if refreshed_csrf:
            csrf_token = refreshed_csrf
        authorize_url = _start_browser_signin(
            page,
            email,
            device_id,
            csrf_token,
            screen_hint="login_or_signup",
            log=log,
        )
    if not authorize_url:
        log("Web Session 桥接 signin 未返回 authorize URL")
        return None

    log(f"Web Session 桥接 authorize: {authorize_url[:120]}")
    try:
        page.goto(authorize_url, wait_until="commit", timeout=20000)
    except Exception as exc:
        # Callback 落地时 localhost / 中断导航常见，随后再看最终 URL
        log(f"Web Session 桥接 authorize 导航异常（可继续）: {exc}")
    _wait_for_auth_page_settle(page, timeout=6.0, log=log)
    log(f"Web Session 桥接 authorize 落地 url={(str(page.url or '')[:140])}")

    # 若停在 auth 中页，再推一次 ChatGPT 首页
    current = str(page.url or "")
    if "chatgpt.com" not in current:
        try:
            page.goto(f"{CHATGPT_APP}/", wait_until="commit", timeout=20000)
        except Exception as exc:
            log(f"Web Session 桥接回 ChatGPT 首页异常: {exc}")
        _wait_for_auth_page_settle(page, timeout=5.0, log=log)
        log(f"Web Session 桥接回 ChatGPT 后 url={(str(page.url or '')[:140])}")

    # Immediate session probe — return token to caller so late hits are not dropped.
    try:
        probe = _fetch_chatgpt_session_payload(page)
        data = probe.get("data") if isinstance(probe.get("data"), dict) else {}
        if str(data.get("accessToken") or "").strip():
            log("Web Session 桥接后立即命中 accessToken")
            return dict(data)
        log(
            "Web Session 桥接后仍无 AT "
            f"status={probe.get('status')} keys={sorted(list(data.keys()))[:8]}"
        )
    except Exception as exc:
        log(f"Web Session 桥接后探测失败: {exc}")
    return {}
def _wait_for_web_session(
    page,
    timeout: int = 30,
    *,
    log=None,
    email: str = "",
    device_id: str = "",
    stop_check: Callable[[], None] | None = None,
) -> dict:
    """Read ChatGPT web session; bridge OpenAI cookies via next-auth when needed."""
    logger = log or (lambda _message: None)
    deadline = time.time() + max(int(timeout or 0), 1)
    bridge_attempted = False
    bridge_retried = False
    home_navigated = False
    attempt = 0
    logger(
        "开始抓取 ChatGPT Web Session: "
        f"url={(str(page.url or '')[:120])} cookies={_cookie_names_summary(page)}"
    )

    def _goto_chatgpt_home(reason: str) -> None:
        nonlocal home_navigated
        home_navigated = True
        logger(f"Web Session: 导航 chatgpt.com 首页建立 next-auth 会话 ({reason})")
        try:
            page.goto(f"{CHATGPT_APP}/", wait_until="commit", timeout=20000)
        except Exception as exc:
            message = str(exc or "")
            logger(f"Web Session: 首页导航异常: {message[:180]}")
            # NS_BINDING_ABORTED / detach often means SPA already navigated.
            _wait_for_auth_page_settle(page, timeout=8.0, log=logger)
            if "chatgpt.com" not in str(page.url or ""):
                try:
                    page.goto(f"{CHATGPT_APP}/", wait_until="commit", timeout=30000)
                except Exception as retry_exc:
                    logger(f"Web Session: 首页重试导航异常: {retry_exc}")
                _wait_for_auth_page_settle(page, timeout=8.0, log=logger)
            return
        _wait_for_auth_page_settle(page, timeout=10.0, log=logger)

    while time.time() < deadline:
        if callable(stop_check):
            stop_check()
        attempt += 1
        try:
            current_url = str(page.url or "")
            # platform.openai.com callback 不是 ChatGPT session 域名
            if "chatgpt.com" not in current_url and not home_navigated:
                _goto_chatgpt_home("initial")

            payload = _fetch_chatgpt_session_payload(page)
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            access_token = str(data.get("accessToken") or "").strip()
            if access_token:
                logger(
                    f"Web Session: 已拿到 accessToken status={payload.get('status')} "
                    f"url={(str(page.url or '')[:100])}"
                )
                return dict(data)

            keys = sorted(list(data.keys()))[:8]
            logger(
                "Web Session: 尚未就绪 "
                f"attempt={attempt} status={payload.get('status')} "
                f"keys={keys} "
                f"url={(str(page.url or '')[:100])} "
                f"text={str(payload.get('text') or '')[:120]}"
            )

            # WARNING_BANNER-only / empty session: not authenticated yet → bridge ASAP
            unauthenticated = (
                not data
                or set(keys) <= {"WARNING_BANNER"}
                or (not data.get("user") and not data.get("account") and not data.get("accessToken"))
            )
            elapsed = max(int(timeout or 0), 1) - max(int(deadline - time.time()), 0)
            if (not bridge_attempted) and (
                (attempt >= 1 and unauthenticated) or attempt >= 2 or elapsed >= 6
            ):
                if callable(stop_check):
                    stop_check()
                bridge_attempted = True
                bridged = _browser_chatgpt_openai_signin_bridge(
                    page,
                    logger,
                    email=email,
                    device_id=device_id,
                )
                home_navigated = True
                # Bridge may finish after the outer deadline; never drop a hit.
                if isinstance(bridged, dict) and str(bridged.get("accessToken") or "").strip():
                    logger("Web Session: 桥接返回 accessToken，立即结束等待")
                    return dict(bridged)
                # One immediate re-fetch even if deadline already passed.
                try:
                    post = _fetch_chatgpt_session_payload(page)
                    post_data = post.get("data") if isinstance(post.get("data"), dict) else {}
                    if str(post_data.get("accessToken") or "").strip():
                        logger("Web Session: 桥接后补拉到 accessToken")
                        return dict(post_data)
                except Exception:
                    pass
                continue

            # First bridge often lands authorize but SPA still lacks next-auth;
            # retry once before the hard deadline.
            remaining = deadline - time.time()
            if (
                bridge_attempted
                and (not bridge_retried)
                and remaining > 12
                and unauthenticated
                and attempt >= 3
            ):
                if callable(stop_check):
                    stop_check()
                bridge_retried = True
                logger("Web Session: 首次桥接后仍无 AT，重试 next-auth 桥接")
                bridged = _browser_chatgpt_openai_signin_bridge(
                    page,
                    logger,
                    email=email,
                    device_id=device_id,
                )
                if isinstance(bridged, dict) and str(bridged.get("accessToken") or "").strip():
                    logger("Web Session: 二次桥接返回 accessToken")
                    return dict(bridged)
                continue

            # 仍在 platform / auth 域名时，再次强制回 ChatGPT
            if "chatgpt.com" not in str(page.url or "") and attempt in {3, 5, 7}:
                _goto_chatgpt_home(f"retry-{attempt}")
        except Exception as exc:
            logger(f"Web Session: 轮询异常: {exc}")
        time.sleep(1.2)

    # Final salvage fetch — covers the case where bridge printed a hit after
    # the loop condition already failed.
    if callable(stop_check):
        stop_check()
    try:
        last = _fetch_chatgpt_session_payload(page)
        last_data = last.get("data") if isinstance(last.get("data"), dict) else {}
        if str(last_data.get("accessToken") or "").strip():
            logger("Web Session: 超时前补拉到 accessToken")
            return dict(last_data)
    except Exception:
        pass

    logger(
        "Web Session: 超时未拿到 accessToken "
        f"cookies={_cookie_names_summary(page)} url={(str(page.url or '')[:120])}"
    )
    return {}
def _normalize_browser_web_session(session_data: dict, cookies: list[dict]) -> dict:
    data = dict(session_data or {})
    access_token = str(data.get("accessToken") or "").strip()
    claims = _decode_jwt_payload(access_token)
    auth_claims = claims.get("https://api.openai.com/auth") or {}
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    account_id = str(
        account.get("id")
        or auth_claims.get("chatgpt_account_id")
        or ""
    ).strip()
    session_token = str(data.get("sessionToken") or "").strip()
    cookie_pairs: list[str] = []
    for item in cookies or []:
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if not name or not value:
            continue
        cookie_pairs.append(f"{name}={value}")
        if not session_token and "session-token" in name.lower():
            session_token = value
    cookie_header = "; ".join(cookie_pairs)
    return {
        "access_token": access_token,
        "session_token": session_token,
        "cookies": cookie_header,
        "cookie_header": cookie_header,
        "account_id": account_id,
        "workspace_id": account_id,
        "user_id": str(
            user.get("id")
            or auth_claims.get("chatgpt_user_id")
            or auth_claims.get("user_id")
            or ""
        ).strip(),
        "expires": data.get("expires"),
        "user": user,
        "account": account,
        "auth_provider": data.get("authProvider"),
        "raw_session": data,
    }


def _is_registration_complete(state: dict) -> bool:
    page_type = str(state.get("page_type") or "")
    if page_type == "add_phone" and bool(state.get("signup_committed")):
        return True
    if page_type == "external_url":
        # The URL is a server-provided destination, not proof that the browser
        # has actually followed it. Let the state machine perform navigation.
        return False
    url = str(state.get("current_url") or state.get("continue_url") or "").lower()
    return page_type in {"callback", "oauth_callback", "chatgpt_home"} or (
        "chatgpt.com" in url and "redirect_uri" not in url and "about-you" not in url
    )


def _handle_post_signup_onboarding(page, log) -> None:
    current_url = str(page.url or "")
    if "chatgpt.com" not in current_url:
        return
    try:
        # 可能弹出 persistent storage 提示，优先点 Allow，不影响主流程也可点 Block。
        allow_selector = _click_first(
            page,
            [
                'button:has-text("Allow")',
                'button:has-text("allow")',
                'button:has-text("Block")',
                'button:has-text("block")',
            ],
            timeout=1,
        )
        if allow_selector:
            log(f"已处理浏览器弹窗: {allow_selector}")
    except Exception:
        pass

    # 新账号常见 onboarding 问卷页，优先 Skip。
    try:
        if page.locator("text=What brings you to ChatGPT?").first.count() > 0:
            skip_selector = _click_first(
                page,
                [
                    'button:has-text("Skip")',
                    'button:has-text("skip")',
                    'button:has-text("Next")',
                    'button:has-text("next")',
                ],
                timeout=5,
            )
            if skip_selector:
                log(f"已处理 onboarding 页面: {skip_selector}")
                _browser_pause(page)
    except Exception:
        pass


def _is_password_registration(state: dict) -> bool:
    return str(state.get("page_type") or "") in {"create_account_password", "password"}


def _is_email_otp(state: dict) -> bool:
    page_type = str(state.get("page_type") or "").strip().lower()
    target = f"{state.get('continue_url') or ''} {state.get('current_url') or ''}".lower()
    return (
        page_type in {"email_otp_verification", "email_otp_send", "email_otp_validate"}
        or "email-verification" in target
        or "email-otp" in target
    )


def _is_about_you(state: dict) -> bool:
    target = f"{state.get('continue_url') or ''} {state.get('current_url') or ''}".lower()
    return str(state.get("page_type") or "") == "about_you" or "about-you" in target


def _is_add_phone(state: dict) -> bool:
    target = f"{state.get('continue_url') or ''} {state.get('current_url') or ''}".lower()
    return str(state.get("page_type") or "") == "add_phone" or "add-phone" in target


def _mask_phone_number(phone_number: str) -> str:
    text = str(phone_number or "").strip()
    if len(text) <= 4:
        return text
    if len(text) <= 8:
        return f"{text[:2]}****{text[-2:]}"
    return f"{text[:4]}****{text[-2:]}"


def _is_invalid_phone_otp_response(result: dict) -> bool:
    status = int((result or {}).get("status") or 0)
    if status != 400:
        return False
    data = (result or {}).get("data")
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").lower()
            code = str(error.get("code") or "").lower()
            return code == "invalid_input" and "invalid otp code" in message
    text = str((result or {}).get("text") or "").lower()
    return "invalid otp code" in text


def _handle_add_phone_challenge(
    page,
    phone_callback,
    *,
    device_id: str,
    user_agent: str,
    log,
    resume_url: str = "",
    max_phone_attempts: int = 3,
) -> dict:
    """在 add-phone 页面通过 UI 交互完成手机号验证。

    流程: 选择国家 -> 输入本地号码 -> 点击发送 -> 填写 OTP -> 点击验证。
    如果验证码超时未收到，自动换号重试（最多 max_phone_attempts 次）。
    """
    if not phone_callback:
        raise RuntimeError(
            "ChatGPT 注册遇到手机号验证，但未配置 phone_callback。"
            "请在 RegisterConfig.extra 中配置接码服务，或手动完成手机验证。"
        )

    last_error = None
    for phone_attempt in range(max_phone_attempts):
        if phone_attempt > 0:
            log(f"换号重试第 {phone_attempt + 1}/{max_phone_attempts} 次...")
            # 回到 add-phone 页面
            try:
                page.goto(f"{OPENAI_AUTH}/add-phone", wait_until="domcontentloaded", timeout=15000)
                time.sleep(1)
            except Exception:
                pass

        try:
            result = _do_add_phone_attempt(
                page, phone_callback,
                device_id=device_id, user_agent=user_agent,
                log=log, resume_url=resume_url,
            )
            return result
        except RuntimeError as exc:
            last_error = exc
            error_msg = str(exc)
            # 验证码超时或号码已被使用时换号重试，其他错误直接抛出
            should_retry = (
                "未获取到短信验证码" in error_msg
                or "phone_number_in_use" in error_msg
                or "already" in error_msg.lower()
                or "in use" in error_msg.lower()
            )
            if not should_retry:
                raise
            log(f"⚠️ 验证码超时未收到，准备换号重试...")
            # 取消当前号码
            if hasattr(phone_callback, "cleanup"):
                phone_callback.cleanup()
            # 重置 phone_callback 状态为 need_number
            if hasattr(phone_callback, "phase"):
                phone_callback.phase = "need_number"
                phone_callback.activation = None
                phone_callback.completed = False

    raise last_error or RuntimeError("短信验证失败: 多次换号均未收到验证码")


def _do_add_phone_attempt(
    page,
    phone_callback,
    *,
    device_id: str,
    user_agent: str,
    log,
    resume_url: str = "",
) -> dict:
    """单次手机号验证尝试（内部函数）。"""

    # 保留 HTTP resend 回调供 SMS provider 内部使用
    referer = _normalize_url(str(page.url or ""), OPENAI_AUTH) or f"{OPENAI_AUTH}/add-phone"
    headers = _build_browser_headers(
        user_agent=user_agent,
        accept="application/json",
        referer=referer,
        origin=OPENAI_AUTH,
        content_type="application/json",
        extra_headers={
            "sec-fetch-site": "same-origin",
            "oai-device-id": device_id,
            **_generate_datadog_trace_headers(),
        },
    )

    def _request_openai_resend():
        # 浏览器模式下只通过页面 UI 点击 Resend 按钮
        resend_clicked = _click_first(page, [
            'button:has-text("Resend")',
            'button:has-text("resend")',
            'button:has-text("Resend code")',
            'button:has-text("重新发送")',
            'a:has-text("Resend")',
            'a:has-text("resend")',
            'a:has-text("Resend code")',
        ], timeout=3)
        if resend_clicked:
            log(f"  phone-otp/resend -> 已点击页面 Resend 按钮: {resend_clicked}")
        else:
            log("  phone-otp/resend -> 页面未找到 Resend 按钮，跳过（浏览器模式不走 HTTP）")

    if hasattr(phone_callback, "set_resend_callback"):
        phone_callback.set_resend_callback(_request_openai_resend)

    # ---- 第1步: 获取手机号 ----
    log("注册流程已进入 add_phone，开始准备租号并接收短信验证码...")
    phone_number = str(phone_callback() or "").strip()
    if not phone_number:
        raise RuntimeError("未获取到手机号")
    log(f"检测到 add_phone，提交手机号(UI): {_mask_phone_number(phone_number)}")

    # 解析国家拨号码和本地号码
    dial_code, local_number, country_name = _parse_phone_country_and_local(phone_number)
    log(f"  解析号码: 国家={country_name or '未知'} 拨号码=+{dial_code} 本地号={local_number[:4]}...")

    # 确保在 add-phone 页面
    current_url = str(page.url or "")
    if "add-phone" not in current_url:
        page.goto(f"{OPENAI_AUTH}/add-phone", wait_until="domcontentloaded", timeout=30000)
    time.sleep(1)

    # ---- 第2步: 选择国家 ----
    country_selected = _select_phone_country_ui(page, dial_code, country_name, log)
    _browser_pause(page)

    # ---- 第3步: 填写手机号 ----
    phone_input_sel = _wait_for_any_selector(page, PHONE_INPUT_SELECTORS, timeout=10)
    if phone_input_sel:
        # 如果成功选了国家，输入本地号码；否则输入完整号码
        fill_value = local_number if country_selected else phone_number
        filled = _fill_input_like_user(page, phone_input_sel, fill_value)
        # _fill_input_like_user 用严格相等验证，但 add-phone 页面可能在 input 中自动加了国家前缀
        # 所以额外检查 input.value 是否包含我们填入的号码
        if not filled:
            try:
                actual_val = str(page.evaluate(
                    "(sel) => { const el = document.querySelector(sel); return el ? el.value : ''; }",
                    phone_input_sel,
                ) or "")
                # 如果 input 值包含我们的号码（可能前面有 +56 之类的前缀），认为成功
                if fill_value and fill_value in actual_val.replace(" ", "").replace("-", ""):
                    filled = True
                    log(f"  手机号已填写(含前缀): {actual_val[:12]}...")
            except Exception:
                pass
        if not filled:
            # fallback: 尝试先清空再用 keyboard.type 输入
            log(f"  _fill_input_like_user 失败，尝试 keyboard fallback...")
            try:
                page.click(phone_input_sel)
                time.sleep(0.3)
                # 三次全选删除确保清空
                for _ in range(3):
                    page.keyboard.press("Meta+a")
                    time.sleep(0.1)
                    page.keyboard.press("Backspace")
                    time.sleep(0.1)
                page.keyboard.type(fill_value, delay=random.randint(30, 70))
                time.sleep(0.3)
                # 验证输入值
                actual = page.evaluate(
                    "(sel) => { const el = document.querySelector(sel); return el ? el.value : ''; }",
                    phone_input_sel,
                )
                actual_clean = str(actual or "").replace(" ", "").replace("-", "")
                if fill_value in actual_clean:
                    filled = True
                    log(f"  keyboard fallback 成功: {str(actual or '')[:12]}...")
            except Exception as e:
                log(f"  keyboard fallback 失败: {e}")
        if not filled:
            # 最终 fallback: 直接用 JS 设置值
            try:
                js_ok = page.evaluate(
                    """
                    ({ selector, value }) => {
                      const input = document.querySelector(selector);
                      if (!input) return false;
                      input.focus();
                      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                      if (setter) setter.call(input, value);
                      else input.value = value;
                      input.dispatchEvent(new Event('input', { bubbles: true }));
                      input.dispatchEvent(new Event('change', { bubbles: true }));
                      // 也触发 React 合成事件
                      const nativeEvent = new Event('input', { bubbles: true });
                      Object.defineProperty(nativeEvent, 'target', { writable: false, value: input });
                      input.dispatchEvent(nativeEvent);
                      return input.value.includes(value);
                    }
                    """,
                    {"selector": phone_input_sel, "value": fill_value},
                )
                if js_ok:
                    filled = True
                    log(f"  JS setValue fallback 成功")
            except Exception as e:
                log(f"  JS setValue fallback 失败: {e}")
        if not filled:
            raise RuntimeError(f"手机号输入框填写失败: {phone_input_sel}")
        log(f"  手机号输入框已填写: {phone_input_sel} value={fill_value[:4]}...")
    else:
        raise RuntimeError("未找到手机号输入框")
    _browser_pause(page)

    # ---- 第4步: 点击发送按钮 ----
    send_sel = _click_first(page, PHONE_SEND_SELECTORS, timeout=8)
    if send_sel:
        log(f"  已点击发送按钮: {send_sel}")
    elif _submit_form_with_fallback(page, phone_input_sel):
        log("  未找到发送按钮，已使用表单 fallback 提交")
    else:
        raise RuntimeError("未找到发送验证码按钮")

    # 等待页面响应（可能显示 OTP 输入框或错误）
    time.sleep(2)

    # 检查发送是否成功（页面应出现 OTP 输入框或 URL 变化）
    error_text = _extract_auth_error_text(page)
    if error_text:
        if hasattr(phone_callback, "mark_send_failed"):
            phone_callback.mark_send_failed(error_text)
        raise RuntimeError(f"手机号提交失败: {error_text[:200]}")

    if hasattr(phone_callback, "mark_send_succeeded"):
        phone_callback.mark_send_succeeded()
    log("手机号提交成功(UI)，开始等待短信验证码...")

    # ---- 第5步: 等待 SMS 验证码并在页面 OTP 输入框中填写 ----
    for code_attempt in range(3):
        sms_code = str(phone_callback() or "").strip()
        if not sms_code:
            raise RuntimeError("未获取到短信验证码")

        # 等待 OTP 输入框出现
        otp_sel = _wait_for_any_selector(page, OTP_INPUT_SELECTORS, timeout=10)
        if not otp_sel:
            # 尝试用 phone input selectors 作为 OTP（某些版本页面复用同一 input）
            otp_sel = _find_first_selector(page, PHONE_INPUT_SELECTORS)
        if not otp_sel:
            raise RuntimeError("未找到短信验证码输入框")

        # 使用与邮箱 OTP 相同的填写逻辑
        otp_resp = _submit_otp_via_page(
            page,
            sms_code,
            log,
            allow_api_fallback=False,
        )
        otp_status = int(otp_resp.get("status") or 0)
        log(f"  phone-otp 页面提交状态: {otp_status}")

        if otp_resp.get("ok") or otp_status in (200, 201, 204):
            if hasattr(phone_callback, "report_success"):
                phone_callback.report_success()
            # 等待页面跳转
            time.sleep(1.5)
            state = _extract_flow_state(
                otp_resp.get("data"),
                otp_resp.get("url", page.url),
            )
            if not state.get("page_type"):
                state = _derive_registration_state_from_page(page)
            next_url = _normalize_url(resume_url, OPENAI_AUTH) if resume_url else ""
            if next_url:
                page.goto(next_url, wait_until="domcontentloaded", timeout=30000)
                return _extract_flow_state(None, page.url)
            return state

        # 检查是否是无效验证码
        page_error = _extract_auth_error_text(page)
        if page_error and any(kw in page_error.lower() for kw in ("invalid", "incorrect", "wrong", "expired")):
            log(f"短信验证码被判定无效: {page_error[:100]}，继续等待下一条...")
            if hasattr(phone_callback, "mark_code_failed"):
                phone_callback.mark_code_failed(page_error or "invalid otp code")
            continue

        if hasattr(phone_callback, "mark_code_failed"):
            phone_callback.mark_code_failed(page_error or f"status {otp_status}")
        raise RuntimeError(f"短信验证码校验失败: {page_error[:200] if page_error else f'status {otp_status}'}")

    raise RuntimeError("短信验证码校验失败: 多次验证码均无效或未通过")


def _is_oauth_browser_callback_url(url: str) -> bool:
    """True for real browser OAuth/next-auth callback destinations.

    These paths often contain ``/api/`` (e.g. ChatGPT next-auth
    ``/api/auth/callback/openai``) but must still be followed with page.goto.
    Do not confuse them with auth.openai.com JSON continue APIs such as
    ``/api/accounts/email-otp/send``.
    """
    text = str(url or "").strip().lower()
    if not text:
        return False
    if "/api/auth/callback" in text:
        return True
    if "chatgpt.com" in text and "/api/auth/" in text:
        return True
    if "platform.openai.com" in text and "/auth/callback" in text:
        return True
    if "/api/oauth/oauth2/auth" in text:
        return True
    return False


def _is_internal_auth_api_continue_url(url: str) -> bool:
    """True for auth state-machine API continues that must not be page.goto'd."""
    text = str(url or "").strip()
    if not text:
        return False
    if _is_oauth_browser_callback_url(text):
        return False
    lowered = text.lower()
    if lowered.rstrip("/").endswith("/send"):
        return True
    if "/api/accounts/" in lowered:
        return True
    # Remaining auth.openai.com /api/* continues are protocol steps, not pages.
    if "auth.openai.com" in lowered and "/api/" in lowered:
        return True
    return False


def _requires_registration_navigation(state: dict) -> bool:
    if str(state.get("method") or "GET").upper() != "GET":
        return False
    page_type = str(state.get("page_type") or "").strip().lower()
    # API continue 不是页面 URL，禁止 page.goto 撞 /api/*
    if page_type in {
        "email_otp_send",
        "email_otp_verification",
        "create_account_password",
        "login_password",
        "about_you",
        "add_phone",
    }:
        return False
    if page_type == "external_url" and state.get("continue_url"):
        continue_url = str(state.get("continue_url") or "")
        # next-auth / OAuth browser callbacks must be navigated even when path has /api/
        if _is_oauth_browser_callback_url(continue_url):
            return True
        if _is_internal_auth_api_continue_url(continue_url):
            return False
        return bool(continue_url)
    continue_url = str(state.get("continue_url") or "")
    current_url = str(state.get("current_url") or "")
    if not continue_url or continue_url == current_url:
        return False
    # 禁止把 JSON 状态机里的 API 路径当页面导航；OAuth 浏览器回调除外
    if _is_oauth_browser_callback_url(continue_url):
        return True
    if _is_internal_auth_api_continue_url(continue_url) or "/api/" in continue_url:
        return False
    return True


def _browser_add_cookies(page, cookies: list[dict]) -> None:
    try:
        page.context.add_cookies(cookies)
    except Exception:
        pass


def _seed_browser_device_id(page, device_id: str) -> None:
    _browser_add_cookies(
        page,
        [
            {"name": "oai-did", "value": device_id, "domain": "chatgpt.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": ".chatgpt.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": "openai.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": "auth.openai.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": ".auth.openai.com", "path": "/"},
        ],
    )


def _csrf_token_from_cookies(page) -> str:
    """next-auth stores csrf as cookie value ``token|hash``; API expects the token half."""
    try:
        cookies = list(page.context.cookies() or [])
    except Exception:
        return ""
    for item in cookies:
        name = str(item.get("name") or "").lower()
        if "csrf" not in name:
            continue
        raw = str(item.get("value") or "").strip()
        if not raw:
            continue
        # next-auth serializes the separator as ``%7C`` in some browser jars.
        # Playwright may expose either the encoded or decoded representation.
        token = unquote(raw).split("|", 1)[0].strip()
        if token:
            return token
    return ""


def _get_browser_csrf_token(page, *, log=None) -> str:
    logger = log or (lambda _message: None)
    result = _browser_fetch(
        page,
        f"{CHATGPT_APP}/api/auth/csrf",
        method="GET",
        headers={
            "accept": "application/json",
            "referer": f"{CHATGPT_APP}/",
            "sec-fetch-site": "same-origin",
        },
        redirect="follow",
    )
    if result.get("ok") and isinstance(result.get("data"), dict):
        token = str((result.get("data") or {}).get("csrfToken") or "").strip()
        if token:
            return token

    # Prefer the browser cookie before Playwright APIRequestContext. Authenticated
    # SOCKS5 proxies work for the browser process but APIRequestContext can reject
    # their handshake, adding 20 seconds before returning the same cookie token.
    cookie_token = _csrf_token_from_cookies(page)
    if cookie_token:
        logger("Web Session CSRF 回退使用 next-auth csrf cookie")
        return cookie_token

    # APIRequestContext remains a final fallback for direct/HTTP proxy contexts
    # where page-world fetch returned an empty or HTML response.
    try:
        request = getattr(getattr(page, "context", None), "request", None)
        if request is not None:
            response = request.get(
                f"{CHATGPT_APP}/api/auth/csrf",
                headers={
                    "accept": "application/json",
                    "referer": f"{CHATGPT_APP}/",
                },
                timeout=12000,
            )
            try:
                data = response.json()
            except Exception:
                data = {}
            token = str((data or {}).get("csrfToken") or "").strip()
            if token:
                logger(
                    f"Web Session CSRF via context.request status={getattr(response, 'status', '-')}"
                )
                return token
    except Exception as exc:
        logger(f"Web Session CSRF context.request 失败: {exc}")

    logger(
        "Web Session CSRF 获取失败: "
        f"status={result.get('status')} ok={result.get('ok')} "
        f"text={str(result.get('text') or '')[:120]} cookies={_cookie_names_summary(page)}"
    )
    return ""


def _usable_next_auth_signin_url(value: str) -> bool:
    """Reject next-auth error/login self-routes masquerading as authorize URLs."""
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    path = str(parsed.path or "/").rstrip("/").lower() or "/"
    host = str(parsed.hostname or "").lower()
    chatgpt_host = str(urlparse(CHATGPT_APP).hostname or "chatgpt.com").lower()
    if host == chatgpt_host and (
        path.startswith("/api/auth/signin")
        or path.startswith("/api/auth/error")
        or path in {"/auth/login", "/login"}
    ):
        return False
    return host == chatgpt_host or host == "openai.com" or host.endswith(".openai.com")


def _start_browser_signin(
    page,
    email: str,
    device_id: str,
    csrf_token: str,
    *,
    screen_hint: str = "login_or_signup",
    log=None,
) -> str:
    from urllib.parse import urlencode

    logger = log or (lambda _message: None)
    query = urlencode(
        {
            "prompt": "login",
            "ext-oai-did": device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "screen_hint": str(screen_hint or "login_or_signup"),
            "login_hint": email,
        }
    )
    body = urlencode(
        {
            "callbackUrl": f"{CHATGPT_APP}/",
            "csrfToken": csrf_token,
            "json": "true",
        }
    )
    result = _browser_fetch(
        page,
        f"{CHATGPT_APP}/api/auth/signin/openai?{query}",
        method="POST",
        headers={
            "accept": "application/json",
            "referer": f"{CHATGPT_APP}/",
            "origin": CHATGPT_APP,
            "content-type": "application/x-www-form-urlencoded",
            "sec-fetch-site": "same-origin",
        },
        body=body,
        redirect="follow",
    )
    browser_url = ""
    if result.get("ok") and isinstance(result.get("data"), dict):
        browser_url = str((result.get("data") or {}).get("url") or "").strip()
        if _usable_next_auth_signin_url(browser_url):
            return browser_url
        if browser_url:
            parsed = urlparse(browser_url)
            logger(
                "Web Session signin 拒绝 next-auth 非 authorize 路径: "
                f"{parsed.hostname or '-'}{parsed.path or '/'}"
            )
            return ""

    # Same fallback as CSRF: shared cookie jar via context.request.
    try:
        request = getattr(getattr(page, "context", None), "request", None)
        if request is not None:
            response = request.post(
                f"{CHATGPT_APP}/api/auth/signin/openai?{query}",
                headers={
                    "accept": "application/json",
                    "referer": f"{CHATGPT_APP}/",
                    "origin": CHATGPT_APP,
                    "content-type": "application/x-www-form-urlencoded",
                },
                data=body,
                timeout=15000,
            )
            try:
                data = response.json()
            except Exception:
                data = {}
            url = str((data or {}).get("url") or "").strip()
            if _usable_next_auth_signin_url(url):
                logger(
                    f"Web Session signin via context.request status={getattr(response, 'status', '-')}"
                )
                return url
            if url:
                parsed = urlparse(url)
                logger(
                    "Web Session signin context.request 拒绝非 authorize 路径: "
                    f"{parsed.hostname or '-'}{parsed.path or '/'}"
                )
            logger(
                "Web Session signin context.request 无 url: "
                f"status={getattr(response, 'status', '-')} body={str(data)[:160]}"
            )
    except Exception as exc:
        logger(f"Web Session signin context.request 失败: {exc}")

    logger(
        "Web Session signin 未返回 authorize URL: "
        f"status={result.get('status')} text={str(result.get('text') or '')[:160]}"
    )
    return ""

def _browser_authorize(page, auth_url: str, log) -> str:
    if not auth_url:
        return ""
    try:
        page.goto(auth_url, wait_until="domcontentloaded", timeout=30000)
        final_url = page.url
        log(f"Authorize -> {final_url[:120]}")
        return final_url
    except Exception as exc:
        log(f"Authorize 失败: {exc}")
        return ""


def _validate_browser_email_otp(
    page,
    code: str,
    device_id: str = "",
    user_agent: str = "",
    referer: str = "",
) -> dict:
    headers = _build_browser_headers(
        user_agent=user_agent,
        accept="application/json",
        referer=referer or f"{OPENAI_AUTH}/email-verification",
        origin=OPENAI_AUTH,
        content_type="application/json",
        extra_headers={
            "sec-fetch-site": "same-origin",
            "oai-device-id": device_id,
            **_generate_datadog_trace_headers(),
        },
    )
    try:
        sentinel = _build_browser_sentinel_token(page, device_id, "email_otp_validate", user_agent)
    except Exception:
        sentinel = ""
    if sentinel:
        headers["openai-sentinel-token"] = sentinel
    _browser_pause(page)
    return _browser_fetch(
        page,
        f"{OPENAI_AUTH}/api/accounts/email-otp/validate",
        method="POST",
        headers=headers,
        body=json.dumps({"code": code}),
        redirect="follow",
    )


def _send_browser_oauth_email_otp(
    page,
    *,
    device_id: str,
    user_agent: str,
    referer: str,
    log,
) -> tuple[bool, float | None]:
    """Request a fresh OAuth OTP and return a mailbox timestamp cutoff.

    The passwordless login page can display an OTP form while the only message
    in the forwarding mailbox is the code consumed by the preceding signup
    phase.  The HTTP OAuth client explicitly resends in this situation; the
    browser recovery path must do the same and pass the cutoff to its mailbox
    reader so an old code cannot be submitted to a new auth session.
    """
    effective_referer = referer or f"{OPENAI_AUTH}/email-verification"
    common_extra = {
        "sec-fetch-site": "same-origin",
        "oai-device-id": device_id,
        **_generate_datadog_trace_headers(),
    }
    attempts = (
        (
            "POST",
            f"{OPENAI_AUTH}/api/accounts/passwordless/send-otp",
            "passwordless/send-otp",
            "application/json",
        ),
        (
            "GET",
            f"{OPENAI_AUTH}/api/accounts/email-otp/send",
            "email-otp/send",
            "",
        ),
    )
    for method, url, label, content_type in attempts:
        sent_at = time.time() - 8
        headers = _build_browser_headers(
            user_agent=user_agent,
            accept="application/json, text/plain, */*",
            referer=effective_referer,
            origin=OPENAI_AUTH,
            content_type=content_type,
            extra_headers=common_extra,
        )
        try:
            sentinel = _build_browser_sentinel_token(
                page,
                device_id,
                "email_otp_send",
                user_agent,
            )
        except Exception as exc:
            sentinel = ""
            log(f"  OAuth {label} Sentinel 生成失败，继续请求: {exc}")
        if sentinel:
            headers["openai-sentinel-token"] = sentinel
        try:
            result = _browser_fetch(
                page,
                url,
                method=method,
                headers=headers,
                redirect="follow",
            )
        except Exception as exc:
            log(f"  OAuth {label} 异常: {exc}")
            continue
        status = int(result.get("status") or 0)
        log(f"  OAuth {label} -> {status}")
        if 200 <= status < 300 or result.get("ok"):
            return True, sent_at
    return False, None


def _submit_browser_about_you(
    page,
    device_id: str,
    user_agent: str,
    referer: str,
    *,
    name: str = "",
    birthdate: str = "",
) -> dict:
    from .constants import generate_random_user_info

    headers = _build_browser_headers(
        user_agent=user_agent,
        accept="application/json",
        referer=referer or f"{OPENAI_AUTH}/about-you",
        origin=OPENAI_AUTH,
        content_type="application/json",
        extra_headers={
            "sec-fetch-site": "same-origin",
            "oai-device-id": device_id,
            "x-access-flow-invocation-id": str(uuid.uuid4()),
            **_generate_datadog_trace_headers(),
        },
    )
    try:
        sentinel = _build_browser_sentinel_token(page, device_id, "oauth_create_account", user_agent)
    except Exception:
        sentinel = ""
    if sentinel:
        headers["openai-sentinel-token"] = sentinel
    user_info = generate_random_user_info()
    if str(name or "").strip():
        user_info["name"] = str(name).strip()
    if str(birthdate or "").strip():
        user_info["birthdate"] = str(birthdate).strip()
    _browser_pause(page)
    return _browser_fetch(
        page,
        f"{OPENAI_AUTH}/api/accounts/create_account",
        method="POST",
        headers=headers,
        body=json.dumps(user_info),
        redirect="follow",
    )


def _complete_oauth_in_browser(page, oauth_start, proxy, log) -> dict | None:
    """在浏览器里完成 OAuth consent 流程，多策略重试点击 Continue。

    参考 Chrome 扩展项目的 step9 实现:
    - consent 页面是一个 <form action="/sign-in-with-chatgpt/.../consent">
    - 首选 form.requestSubmit(button) 而非 button.click()
    - 多轮重试: requestSubmit → click → dispatchEvent → 刷新重试
    """
    from .oauth import submit_callback_url

    CONSENT_FORM_SEL = OAUTH_CONSENT_FORM_SELECTOR
    MAX_ROUNDS = 4
    CLICK_EFFECT_TIMEOUT = 30

    def _try_extract_callback(url: str) -> dict | None:
        if not url or "code=" not in url:
            return None
        try:
            return json.loads(submit_callback_url(
                callback_url=url,
                expected_state=oauth_start.state,
                code_verifier=oauth_start.code_verifier,
                redirect_uri=oauth_start.redirect_uri,
                client_id=oauth_start.client_id,
                proxy_url=proxy,
            ))
        except ValueError as ve:
            # state 缺失或不匹配时，如果 URL 确实是我们的 callback，跳过 state 验证直接换 token
            if "state" in str(ve) and "localhost" in url and "code=" in url:
                try:
                    # 手动提取 code，跳过 state 验证
                    from urllib.parse import urlparse, parse_qs
                    parsed = urlparse(url)
                    params = parse_qs(parsed.query)
                    code = (params.get("code") or [""])[0]
                    if code:
                        from .oauth import _post_form, _jwt_claims_no_verify, OAUTH_TOKEN_URL
                        import time as _time
                        token_resp = _post_form(
                            OAUTH_TOKEN_URL,
                            {
                                "grant_type": "authorization_code",
                                "client_id": oauth_start.client_id,
                                "code": code,
                                "redirect_uri": oauth_start.redirect_uri,
                                "code_verifier": oauth_start.code_verifier,
                            },
                            proxy_url=proxy,
                        )
                        access_token = (token_resp.get("access_token") or "").strip()
                        refresh_token = (token_resp.get("refresh_token") or "").strip()
                        id_token = (token_resp.get("id_token") or "").strip()
                        if access_token:
                            claims = _jwt_claims_no_verify(id_token)
                            auth_claims = claims.get("https://api.openai.com/auth") or {}
                            now = int(_time.time())
                            expires_in = int(token_resp.get("expires_in") or 0)
                            return {
                                "id_token": id_token,
                                "access_token": access_token,
                                "refresh_token": refresh_token,
                                "account_id": str(auth_claims.get("chatgpt_account_id") or ""),
                                "email": str(claims.get("email") or ""),
                                "expired": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now + max(expires_in, 0))),
                                "last_refresh": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now)),
                            }
                except Exception:
                    pass
            return None
        except Exception:
            return None

    def _check_current_url() -> dict | None:
        url = str(page.url or "")
        result = _try_extract_callback(url)
        if result:
            return result
        cb = _extract_callback_url_from_exception(Exception(url))
        return _try_extract_callback(cb) if cb else None

    def _wait_for_callback(timeout_sec: int) -> dict | None:
        deadline = time.time() + timeout_sec
        checked_urls = set()
        while time.time() < deadline:
            try:
                url = str(page.url or "")
            except Exception:
                url = ""
            if url and url not in checked_urls:
                checked_urls.add(url)
                if "code=" in url or "localhost" in url:
                    log(f"  [callback_wait] 检测到 URL 变化: {url[:150]}")
            result = _check_current_url()
            if result:
                return result
            # 也检查是否有导航到 localhost 的请求（即使页面加载失败）
            if "localhost" in url and "code=" in url:
                result = _try_extract_callback(url)
                if result:
                    return result
            time.sleep(0.8)
        # 最后再检查一次
        try:
            final_url = str(page.url or "")
            if "code=" in final_url:
                log(f"  [callback_wait] 超时后最终 URL: {final_url[:150]}")
                result = _try_extract_callback(final_url)
                if result:
                    return result
        except Exception:
            pass
        return None

    def _find_consent_button():
        """按优先级查找 consent 页面的 Continue 按钮"""
        # 策略 1: 在 consent form 内找 submit 按钮
        _sel = CONSENT_FORM_SEL
        btn = page.evaluate("""(sel) => {
            const form = document.querySelector(sel);
            if (!form) return null;
            const buttons = form.querySelectorAll('button[type="submit"], input[type="submit"], [role="button"]');
            for (const el of buttons) {
                if (el.offsetParent === null) continue;
                const text = (el.textContent || '').trim().toLowerCase();
                const ddName = el.getAttribute('data-dd-action-name') || '';
                if (ddName === 'Continue' || /continue|继续|continuar|fortfahren|continuer|続ける/i.test(text)) return 'form-continue';
            }
            const first = Array.from(buttons).find(el => el.offsetParent !== null);
            if (first) return 'form-submit';
            return null;
        }""", _sel)
        if btn:
            return btn
        # 策略 2: 全局查找 Continue 按钮
        for sel in [
            'button[type="submit"][data-dd-action-name="Continue"]',
            'button:has-text("Continue")',
            'button:has-text("继续")',
            'button:has-text("Continuar")',
            'button:has-text("Fortfahren")',
            'button:has-text("Continuer")',
            'button:has-text("Allow")',
            'button:has-text("Authorize")',
            'button[type="submit"]',
        ]:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=500):
                    return sel
            except Exception:
                continue
        return None

    def _click_strategy_request_submit(log_round: int) -> bool:
        """策略 1: form.requestSubmit(button) — 最可靠的表单提交方式"""
        try:
            result = page.evaluate("""(sel) => {
                const form = document.querySelector(sel);
                if (!form) return 'no-form';
                const buttons = form.querySelectorAll('button[type="submit"], input[type="submit"]');
                let target = null;
                for (const el of buttons) {
                    if (el.offsetParent === null) continue;
                    const text = (el.textContent || '').trim().toLowerCase();
                    const ddName = el.getAttribute('data-dd-action-name') || '';
                    if (ddName === 'Continue' || /continue|继续|continuar|fortfahren|continuer/i.test(text)) { target = el; break; }
                }
                if (!target) target = Array.from(buttons).find(el => el.offsetParent !== null);
                if (!target) return 'no-button';
                if (typeof form.requestSubmit === 'function') {
                    form.requestSubmit(target);
                    return 'requestSubmit';
                }
                target.click();
                return 'click-fallback';
            }""", CONSENT_FORM_SEL)
            log(f"  consent 第{log_round}轮 requestSubmit: {result}")
            return result not in ("no-form", "no-button")
        except Exception as e:
            log(f"  consent requestSubmit 异常: {e}")
            return False

    def _click_strategy_playwright(log_round: int) -> bool:
        """策略 2: Playwright locator.click()"""
        for sel in [
            'button:has-text("Continue")',
            'button:has-text("继续")',
            'button:has-text("Continuar")',
            'button:has-text("Fortfahren")',
            'button:has-text("Continuer")',
            'button[type="submit"]',
        ]:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1500):
                    loc.click()
                    log(f"  consent 第{log_round}轮 playwright click: {sel}")
                    return True
            except Exception:
                continue
        return False

    def _click_strategy_js_dispatch(log_round: int) -> bool:
        """策略 3: JS dispatchEvent 模拟点击"""
        try:
            result = page.evaluate("""() => {
                const buttons = document.querySelectorAll('button, [role="button"]');
                for (const el of buttons) {
                    if (el.offsetParent === null) continue;
                    const text = (el.textContent || '').trim().toLowerCase();
                    const ddName = el.getAttribute('data-dd-action-name') || '';
                    if (ddName === 'Continue' || /continue|继续|continuar|fortfahren|continuer/i.test(text)) {
                        el.focus();
                        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                        return text || 'dispatched';
                    }
                }
                return null;
            }
            """)
            if result:
                log(f"  consent 第{log_round}轮 JS dispatch: {result}")
                return True
            return False
        except Exception:
            return False

    strategies = [
        _click_strategy_request_submit,
        _click_strategy_playwright,
        _click_strategy_js_dispatch,
        _click_strategy_request_submit,
    ]

    try:
        current_url = str(page.url or "")
        log(f"  浏览器 consent 处理: {current_url[:100]}")

        # 先检查当前 URL 是否已经有 code
        result = _check_current_url()
        if result:
            log("  ✓ 页面已在 callback URL")
            return result

        # 等待页面加载
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        time.sleep(1)

        # 检查 "Try again" 按钮
        try:
            try_again = page.query_selector('button:has-text("Try again")')
            if try_again and try_again.is_visible():
                log("  consent 页面报错，点击 Try again...")
                try_again.click()
                time.sleep(3)
        except Exception:
            pass

        # 多轮策略重试
        for round_idx in range(MAX_ROUNDS):
            result = _check_current_url()
            if result:
                log("  ✓ 浏览器 OAuth consent 完成")
                return result

            strategy_fn = strategies[min(round_idx, len(strategies) - 1)]
            clicked = strategy_fn(round_idx + 1)

            if clicked:
                # consent 提交后会跳转到 localhost:1455/auth/callback
                # 由于没有本地服务监听，浏览器可能报连接错误，但 URL 已经更新
                try:
                    page.wait_for_url("**/auth/callback*", timeout=15000)
                except Exception:
                    pass  # 超时或导航错误都忽略，下面会检查 URL
                time.sleep(1)
                result = _wait_for_callback(CLICK_EFFECT_TIMEOUT)
                if result:
                    log("  ✓ 浏览器 OAuth consent 完成")
                    return result
                log(f"  consent 第{round_idx + 1}轮点击后页面未跳转")
            else:
                log(f"  consent 第{round_idx + 1}轮未找到按钮")

            # 最后一轮前刷新页面重试
            if round_idx < MAX_ROUNDS - 1:
                log(f"  consent 刷新页面准备第{round_idx + 2}轮...")
                try:
                    page.reload(wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
                time.sleep(2)

        log(f"  consent {MAX_ROUNDS}轮尝试后仍未完成，当前: {str(page.url or '')[:100]}")
        return None
    except Exception as exc:
        cb = _extract_callback_url_from_exception(exc)
        if cb:
            result = _try_extract_callback(cb)
            if result:
                log("  ✓ 从异常中提取 callback 完成 OAuth")
                return result
        log(f"  浏览器 OAuth consent 异常: {exc}")
        return None


def _submit_oauth_password_direct(page, password: str, log) -> dict:
    """OAuth 流程专用：直接填密码登录，不尝试恢复到注册态。"""
    input_selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=15)
    if not input_selector:
        # 密码输入框没出现，可能页面还在加载或跳转了
        # 等一下再试
        time.sleep(2)
        input_selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=10)
    if not input_selector:
        raise RuntimeError("OAuth 密码页未找到输入框")
    if not _fill_input_like_user(page, input_selector, password):
        raise RuntimeError("OAuth 密码页填写失败")
    log(f"  OAuth 密码页输入框: {input_selector}")
    _browser_pause(page)

    submission = _PasswordFormSubmission(
        page,
        input_selector,
        log,
        context="OAuth 密码页",
        business_markers=("/api/accounts/password/verify",),
    )
    try:
        submission.start()
        deadline = time.time() + 60
        processed_responses = 0
        verified_result: dict | None = None
        while time.time() < deadline:
            current_url = str(page.url or "")
            while processed_responses < len(submission.observer.business_responses):
                response = submission.observer.business_responses[processed_responses]
                processed_responses += 1
                status, response_url, data, response_text = _browser_response_details(response)
                if 200 <= status < 300:
                    next_state = _extract_flow_state(data, response_url or current_url)
                    verified_result = {
                        "ok": True,
                        "status": status,
                        "url": response_url or current_url,
                        "data": data or None,
                        "text": "",
                        "password_verified": True,
                        "next_state": next_state,
                    }
                    if str(next_state.get("page_type") or "") not in {
                        "",
                        "login_password",
                        "create_account_password",
                    }:
                        return verified_result
                    continue
                if status >= 400:
                    return {
                        "ok": False,
                        "status": status,
                        "url": response_url or current_url,
                        "data": data or None,
                        "text": _browser_response_error(data, response_text)
                        or f"password verify HTTP {status}",
                    }
            if submission.observer.business_failures and verified_result is None:
                return {
                    "ok": False,
                    "status": 0,
                    "url": current_url,
                    "data": None,
                    "text": f"OAuth 密码请求失败: {submission.observer.business_failures[-1]}",
                }
            state = _derive_registration_state_from_page(page)
            page_type = str(state.get("page_type") or "")
            if page_type in {
                "email_otp_verification",
                "about_you",
                "consent",
                "workspace_selection",
                "organization_selection",
                "add_phone",
                "oauth_callback",
                "chatgpt_home",
                "external_url",
            } or "code=" in current_url:
                return {
                    "ok": True,
                    "status": int((verified_result or {}).get("status") or 200),
                    "url": current_url,
                    "data": (verified_result or {}).get("data"),
                    "text": "",
                    "password_verified": verified_result is not None,
                    "next_state": state,
                }
            error_text = _extract_auth_error_text(page) if verified_result is None else ""
            if error_text:
                return {
                    "ok": False,
                    "status": 400,
                    "url": current_url,
                    "data": None,
                    "text": error_text,
                }
            validation_error = (
                _extract_input_validation_message(page, input_selector)
                if verified_result is None
                else ""
            )
            if validation_error:
                return {
                    "ok": False,
                    "status": 400,
                    "url": current_url,
                    "data": None,
                    "text": validation_error,
                }
            submission.advance_if_idle()
            time.sleep(0.5)
        if verified_result is not None:
            verified_result["transition_pending"] = True
            return verified_result
        return {
            "ok": False,
            "status": 0,
            "url": str(page.url or ""),
            "data": None,
            "text": _password_submission_timeout_text(
                submission,
                "OAuth 密码请求未产生响应且页面未跳转",
            ),
        }
    finally:
        submission.close()


def _submit_password_via_page(page, password: str, log) -> dict:
    input_selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=15)
    if not input_selector:
        raise RuntimeError("密码页未找到输入框")
    if not _fill_input_like_user(page, input_selector, password):
        raise RuntimeError("密码页填写失败")
    log(f"密码页输入框: {input_selector}")
    _browser_pause(page)

    submission = _PasswordFormSubmission(
        page,
        input_selector,
        log,
        context="密码页",
        business_markers=("/api/accounts/user/register",),
    )
    try:
        submission.start()
        start_url = str(page.url or "")
        deadline = time.time() + 60
        last_url = start_url
        processed_responses = 0
        committed_result: dict | None = None
        while time.time() < deadline:
            current_url = str(page.url or "")
            last_url = current_url or last_url
            while processed_responses < len(submission.observer.business_responses):
                response = submission.observer.business_responses[processed_responses]
                processed_responses += 1
                status, response_url, data, response_text = _browser_response_details(response)
                if 200 <= status < 300:
                    response_state = _extract_flow_state(
                        data or None,
                        response_url or current_url,
                    )
                    response_page_type = str(response_state.get("page_type") or "")
                    if response_page_type and response_page_type not in {
                        "create_account_password",
                        "login_password",
                    }:
                        return {
                            "ok": True,
                            "status": status,
                            "url": response_url or current_url,
                            "data": data or None,
                            "text": "",
                            "otp_triggered": response_page_type
                            in {"email_otp_verification", "email_otp_send"},
                            "otp_sent_at": submission.started_at - 8,
                            "register_committed": True,
                        }
                    committed_result = {
                        "ok": True,
                        "status": status,
                        "url": response_url or current_url,
                        "data": data or None,
                        "text": "",
                        "register_committed": True,
                        "transition_pending": True,
                    }
                    log("密码注册请求已返回成功，等待 SPA 离开旧密码页面")
                    break
                if status >= 400:
                    return {
                        "ok": False,
                        "status": status,
                        "url": response_url or current_url,
                        "data": data or None,
                        "text": _browser_response_error(data, response_text)
                        or f"user register HTTP {status}",
                    }
            state = _derive_registration_state_from_page(page)
            page_type = str(state.get("page_type") or "")
            if page_type in {
                "email_otp_verification",
                "about_you",
                "add_phone",
                "oauth_callback",
                "chatgpt_home",
            }:
                state_url = str(
                    state.get("continue_url")
                    or state.get("current_url")
                    or current_url
                )
                return {
                    "ok": True,
                    "status": int((committed_result or {}).get("status") or 200),
                    "url": current_url,
                    "data": {
                        "page": {
                            "type": page_type,
                            "payload": {"url": state_url},
                        }
                    },
                    "text": "",
                    "otp_triggered": page_type in {"email_otp_verification", "email_otp_send"},
                    "otp_sent_at": submission.started_at - 8,
                    "register_committed": committed_result is not None,
                }
            if current_url != start_url and page_type and page_type not in {
                "create_account_password",
                "login_password",
            }:
                return {
                    "ok": True,
                    "status": 200,
                    "url": current_url,
                    "data": None,
                    "text": "",
                    # Preserve password-submit time even on generic SPA navigation so
                    # mailbox otp_sent_at does not fall back to a too-late cutoff.
                    "otp_triggered": page_type in {"email_otp_verification", "email_otp_send"},
                    "otp_sent_at": submission.started_at - 8,
                    "register_committed": committed_result is not None,
                }
            if committed_result is None and page_type == "login_password":
                return {
                    "ok": False,
                    "status": 409,
                    "url": current_url,
                    "data": None,
                    "text": (
                        "user_already_exists: browser password submission "
                        "reached login_password"
                    ),
                    "existing_account_signal": "login_password",
                    "existing_account_stage": "password_submit",
                }
            error_text = (
                _extract_auth_error_text(page)
                if committed_result is None
                else ""
            )
            if error_text:
                _dump_debug(page, "chatgpt_password_fail")
                return {
                    "ok": False,
                    "status": 400,
                    "url": current_url,
                    "data": None,
                    "text": error_text,
                }
            validation_error = (
                _extract_input_validation_message(page, input_selector)
                if committed_result is None
                else ""
            )
            if validation_error:
                _dump_debug(page, "chatgpt_password_fail")
                return {
                    "ok": False,
                    "status": 400,
                    "url": current_url,
                    "data": None,
                    "text": validation_error,
                }
            if committed_result is None:
                submission.advance_if_idle()
            time.sleep(0.5)
        _dump_debug(page, "chatgpt_password_fail")
        validation_error = (
            _extract_input_validation_message(page, input_selector)
            if committed_result is None
            else ""
        )
        error_text = (
            _extract_auth_error_text(page)
            if committed_result is None
            else ""
        )
        return {
            "ok": False,
            "status": int((committed_result or {}).get("status") or 0),
            "url": last_url,
            "data": (committed_result or {}).get("data"),
            "text": error_text
            or validation_error
            or (
                "密码注册请求已成功提交，但页面在等待期限内未离开旧密码页面"
                if committed_result is not None
                else ""
            )
            or (
                f"密码注册请求结果不确定: {submission.observer.business_failures[-1]}"
                if submission.observer.business_failures
                else ""
            )
            or _password_submission_timeout_text(
                submission,
                "密码注册请求未产生响应且页面未跳转",
            ),
            "register_committed": committed_result is not None,
        }
    finally:
        submission.close()


def _submit_otp_via_page(
    page,
    code: str,
    log,
    *,
    device_id: str = "",
    user_agent: str = "",
    referer: str = "",
    allow_api_fallback: bool = True,
    assume_success_without_state: bool = True,
) -> dict:
    otp = str(code or "").strip()
    if not otp:
        return {"ok": False, "status": 400, "url": page.url, "data": None, "text": "验证码为空"}

    # Six-box OTP forms may auto-submit as soon as the last digit is typed.
    # Install the observer before filling so that transaction cannot race past
    # the listener and trigger a duplicate API fallback.
    otp_observer = _NetworkActivityObserver(
        page,
        ("/api/accounts/email-otp/validate",),
    )

    # 等待页面加载完成，确保 OTP 输入框已渲染
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    time.sleep(1)

    filled = False

    # 先尝试 6 格 OTP 输入框
    try:
        digit_inputs = page.locator(
            "input[inputmode='numeric'], input[autocomplete='one-time-code'], input[type='tel'], input[type='number']"
        )
        count = digit_inputs.count()
        if count >= len(otp):
            done = 0
            for i in range(min(count, len(otp))):
                box = digit_inputs.nth(i)
                try:
                    box.wait_for(state="visible", timeout=800)
                    box.fill("")
                    box.type(otp[i], delay=random.randint(20, 60))
                    done += 1
                except Exception:
                    break
            if done >= len(otp):
                filled = True
                log(f"验证码页已填写 {done} 位分格输入框")
    except Exception:
        pass

    # 再尝试单输入框
    if not filled:
        otp_candidates = [
            page.get_by_label(re.compile(r"verification code|code|otp", re.IGNORECASE)),
            page.get_by_role("textbox", name=re.compile(r"verification code|code|otp", re.IGNORECASE)),
            page.locator("input[autocomplete='one-time-code']"),
            page.locator("input[name*='code' i]"),
            page.locator("input[id*='code' i]"),
            page.locator("input[type='text']"),
            page.locator("input"),
        ]
        for candidate in otp_candidates:
            try:
                target = candidate.first
                target.wait_for(state="visible", timeout=1200)
                target.click(timeout=1200)
                target.fill("")
                target.type(otp, delay=random.randint(18, 45))
                final_value = str(target.input_value() or "").strip()
                if final_value:
                    filled = True
                    log("验证码页已填写单输入框")
                    break
            except Exception:
                continue

    if not filled:
        # 再等 3 秒重试一次（页面可能还在渲染）
        time.sleep(3)
        otp_retry_selectors = [
            "input[inputmode='numeric']",
            "input[autocomplete='one-time-code']",
            "input[name*='code' i]",
            "input[type='text']",
        ]
        for sel in otp_retry_selectors:
            try:
                target = page.locator(sel).first
                if target.is_visible(timeout=2000):
                    target.click(timeout=1500)
                    target.fill("")
                    target.type(otp, delay=random.randint(18, 45))
                    if str(target.input_value() or "").strip():
                        filled = True
                        log("验证码页已填写单输入框(重试)")
                        break
            except Exception:
                continue

    if not filled:
        otp_observer.close()
        return {"ok": False, "status": 0, "url": page.url, "data": None, "text": "验证码页未找到可填写输入框"}

    _browser_pause(page)

    try:
        state_after_fill = _derive_registration_state_from_page(page)
        auto_submitted = bool(
            otp_observer.has_business_request
            or str(state_after_fill.get("page_type") or "")
            not in {"", "email_otp_verification"}
        )
        if auto_submitted:
            log("验证码输入完成后页面已自动提交，继续等待现有请求")
        else:
            submit_selector = _click_first(
                page,
                [
                    'button[type="submit"]',
                    'button[data-testid="continue-button"]',
                    'button:has-text("Continue")',
                    'button:has-text("continue")',
                    'button:has-text("Verify")',
                    'button:has-text("verify")',
                    'button:has-text("Next")',
                    'button:has-text("next")',
                ],
                timeout=8,
            )
            if not submit_selector:
                return {"ok": False, "status": 0, "url": page.url, "data": None, "text": "验证码页未找到 Continue 按钮"}
            log(f"验证码页已点击继续按钮: {submit_selector}")

        start_time = time.time()
        deadline = start_time + 60
        last_url = str(page.url or "")
        processed_responses = 0
        api_fallback_attempted = False
        committed_result: dict | None = None

        def _response_details(response) -> tuple[int, str, dict, str]:
            status = int(getattr(response, "status", 0) or 0)
            response_url = str(getattr(response, "url", "") or "")
            text = ""
            data = {}
            try:
                text = str(response.text() or "")
            except Exception:
                pass
            try:
                parsed = response.json()
                if isinstance(parsed, dict):
                    data = parsed
            except Exception:
                if text:
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            data = parsed
                    except (TypeError, ValueError):
                        pass
            return status, response_url, data, text

        def _response_error(data: dict, text: str) -> str:
            error = data.get("error") if isinstance(data, dict) else None
            if isinstance(error, dict):
                message = str(error.get("message") or error.get("detail") or "").strip()
                if message:
                    return message
            for key in ("message", "detail", "error"):
                value = data.get(key) if isinstance(data, dict) else None
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return str(text or "").strip()[:500]

        def _success_result(
            status: int,
            response_url: str,
            data: dict | None = None,
            current_state: dict | None = None,
        ) -> dict:
            effective_url = str(response_url or page.url or "")
            payload = data if isinstance(data, dict) else None
            state = _extract_flow_state(payload, effective_url)
            # Validate API URLs contain "email-otp" and wrongly infer OTP state;
            # also treat email_otp_send as still-on-OTP for success handling.
            still_otp_from_payload = _is_email_otp(state) or not state.get("page_type")
            if still_otp_from_payload:
                derived = (
                    current_state
                    if isinstance(current_state, dict)
                    else _derive_registration_state_from_page(page)
                )
                if derived.get("page_type") and not _is_email_otp(derived):
                    return {
                        "ok": True,
                        "status": status or 200,
                        "url": effective_url,
                        "data": {
                            "page": {
                                "type": str(derived.get("page_type")),
                                "payload": {
                                    "url": str(
                                        derived.get("continue_url")
                                        or derived.get("current_url")
                                        or page.url
                                        or ""
                                    )
                                },
                            }
                        },
                        "text": "",
                    }
                if not assume_success_without_state:
                    return {
                        "ok": False,
                        "status": status or 200,
                        "url": effective_url,
                        "data": payload,
                        "text": "验证码校验已成功提交，等待页面离开邮箱验证码页",
                        "otp_committed": True,
                        "transition_pending": True,
                    }
                # The validate endpoint can return an empty 204 while the SPA
                # keeps the same URL. Its successful contract is the next
                # about_you step, so expose that state to the outer machine.
                payload = {
                    "page": {
                        "type": "about_you",
                        "payload": {"url": f"{OPENAI_AUTH}/about-you"},
                    }
                }
            return {"ok": True, "status": status or 200, "url": effective_url, "data": payload, "text": ""}

        while time.time() < deadline:
            current_url = str(page.url or "")
            last_url = current_url or last_url
            state = _derive_registration_state_from_page(page)
            page_type = str(state.get("page_type") or "")
            if page_type in {
                "about_you",
                "add_phone",
                "consent",
                "workspace_selection",
                "organization_selection",
                "oauth_callback",
                "chatgpt_home",
                "external_url",
            } or "code=" in current_url:
                return {
                    "ok": True,
                    "status": int((committed_result or {}).get("status") or 200),
                    "url": current_url,
                    "data": (committed_result or {}).get("data"),
                    "text": "",
                    "otp_committed": committed_result is not None,
                }

            while processed_responses < len(otp_observer.business_responses):
                response = otp_observer.business_responses[processed_responses]
                processed_responses += 1
                status, response_url, data, response_text = _response_details(response)
                if 200 <= status < 300:
                    success_result = _success_result(
                        status,
                        response_url,
                        data,
                        current_state=state,
                    )
                    if success_result.get("otp_committed") and not success_result.get("ok"):
                        committed_result = success_result
                        break
                    return success_result
                if status >= 400:
                    error_text = _response_error(data, response_text)
                    return {
                        "ok": False,
                        "status": status,
                        "url": response_url or current_url,
                        "data": data or None,
                        "text": error_text or f"email OTP validate HTTP {status}",
                    }

            ui_request_in_flight = bool(otp_observer.business_requests) and not (
                otp_observer.business_responses or otp_observer.business_failures
            )
            if (
                allow_api_fallback
                and not api_fallback_attempted
                and time.time() - start_time >= 10
                and not ui_request_in_flight
                and not otp_observer.has_business_request
            ):
                api_fallback_attempted = True
                log("验证码页 URL 未变化，改用浏览器上下文 API 校验兜底")
                api_result = _validate_browser_email_otp(
                    page,
                    otp,
                    device_id=device_id,
                    user_agent=user_agent,
                    referer=referer or current_url,
                )
                api_status = int(api_result.get("status") or 0)
                if 200 <= api_status < 300 or api_result.get("ok"):
                    success_result = _success_result(
                        api_status,
                        str(api_result.get("url") or current_url),
                        api_result.get("data") if isinstance(api_result.get("data"), dict) else None,
                        current_state=state,
                    )
                    if success_result.get("otp_committed") and not success_result.get("ok"):
                        committed_result = success_result
                    else:
                        return success_result
                if api_status >= 400:
                    post_api_state = _derive_registration_state_from_page(page)
                    if str(post_api_state.get("page_type") or "") in {
                        "about_you",
                        "add_phone",
                        "consent",
                        "workspace_selection",
                        "organization_selection",
                        "oauth_callback",
                        "chatgpt_home",
                        "external_url",
                    }:
                        return {
                            "ok": True,
                            "status": 200,
                            "url": str(page.url or current_url),
                            "data": None,
                            "text": "",
                        }
                    return {
                        "ok": False,
                        "status": api_status,
                        "url": str(api_result.get("url") or current_url),
                        "data": api_result.get("data"),
                        "text": _response_error(
                            api_result.get("data") if isinstance(api_result.get("data"), dict) else {},
                            str(api_result.get("text") or ""),
                        ) or f"email OTP validate HTTP {api_status}",
                    }

            error_text = "" if committed_result is not None else _extract_auth_error_text(page)
            if error_text:
                return {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
            time.sleep(0.4)
        if committed_result is not None:
            committed_result["text"] = (
                "验证码校验已成功提交，但页面在等待期限内未离开邮箱验证码页"
            )
            return committed_result
        return {
            "ok": False,
            "status": 0,
            "url": last_url,
            "data": None,
            "text": (
                f"验证码请求结果不确定且页面未进入下一状态: url={last_url[:160]}"
                if otp_observer.business_failures
                else f"验证码页提交后未进入下一状态: url={last_url[:160]}"
            ),
        }
    finally:
        otp_observer.close()


def _submit_about_you_via_page(
    page,
    log,
    *,
    device_id: str = "",
    user_agent: str = "",
    profile_name: str = "",
    profile_birthdate: str = "",
) -> dict:
    from .constants import generate_random_user_info
    from .utils import generate_random_name

    user_info = generate_random_user_info()
    name = str(profile_name or user_info.get("name") or "").strip()
    birthdate = str(profile_birthdate or user_info.get("birthdate") or "").strip()
    if len(name.split()) < 2:
        _first, generated_last = generate_random_name()
        name = f"{name} {generated_last}".strip()
    if not name or not birthdate:
        raise RuntimeError("about_you 数据生成失败")
    date_parts = birthdate.split("-")
    if len(date_parts) == 3:
        yyyy, mm, dd = date_parts
        us_birthdate = f"{mm}/{dd}/{yyyy}"
        cn_birthdate = f"{yyyy}/{mm}/{dd}"
    else:
        us_birthdate = birthdate
        cn_birthdate = birthdate.replace("-", "/")
    log(f"about_you 表单: name={name}, birthdate={birthdate}, ui_birthdate={us_birthdate}, cn_birthdate={cn_birthdate}")

    def _fill_locator(locator, value: str) -> bool:
        try:
            target = locator.first
            target.wait_for(state="visible", timeout=1500)
            target.click(timeout=1500)
            _browser_pause(page, headed=False)
            try:
                applied = bool(
                    target.evaluate(
                        """
                        (input, nextValue) => {
                          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                          if (!setter) return false;
                          setter.call(input, nextValue);
                          input.dispatchEvent(new Event('input', { bubbles: true }));
                          input.dispatchEvent(new Event('change', { bubbles: true }));
                          return String(input.value || '') === String(nextValue || '');
                        }
                        """,
                        value,
                    )
                )
            except Exception:
                applied = False
            if not applied:
                target.fill("")
                target.type(value, delay=random.randint(25, 70))
            try:
                target.dispatch_event("blur")
            except Exception:
                pass
            final_val = str(target.input_value() or "").strip()
            return final_val == str(value).strip()
        except Exception:
            return False

    def _locator_from_visible_input_entry(entry: dict):
        try:
            visible_index = int(entry.get("visibleIndex"))
        except Exception:
            return None
        return page.locator("input:visible:not([type='hidden']):not([disabled]):not([readonly])").nth(visible_index)

    def _fill_visible_input_entry(entry: dict | None, value: str) -> bool:
        if not entry:
            return False
        locator = _locator_from_visible_input_entry(entry)
        if locator is None:
            return False
        return _fill_locator(locator, value)

    def _resolve_visible_input_selector(selectors: list[str]) -> str | None:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=500)
                return selector
            except Exception:
                continue
        return None

    def _fill_second_visible_input(values: list[str], excluded_visible_indices: set[int] | None = None) -> bool:
        """兜底：about_you 卡片一般是 Full name + Birthday/Age 两个输入框。"""
        try:
            locator = page.locator(
                "input:visible:not([type='hidden']):not([disabled]):not([readonly])"
            )
            count = locator.count()
            if count < 2:
                return False
            excluded = {int(value) for value in (excluded_visible_indices or set())}
            target_index = None
            for idx in range(count):
                if idx not in excluded:
                    target_index = idx
                    if idx > 0:
                        break
            if target_index is None:
                return False
            target = locator.nth(target_index)
            target.click(timeout=1200)
            _browser_pause(page, headed=False)
            for value in values:
                try:
                    target.fill("")
                except Exception:
                    pass
                try:
                    target.type(str(value), delay=random.randint(18, 45))
                except Exception:
                    continue
                final_val = str(target.input_value() or "").strip()
                if final_val:
                    return True
            return False
        except Exception:
            return False

    def _has_visible(locator) -> bool:
        try:
            locator.first.wait_for(state="visible", timeout=700)
            return True
        except Exception:
            return False

    def _fill_birthday_selects(yyyy: str, mm: str, dd: str) -> bool:
        """处理 Month/Day/Year 下拉样式的生日控件。"""
        try:
            select_locator = page.locator("select:visible")
            count = select_locator.count()
            if count < 2:
                return False

            month_num = int(mm)
            day_num = int(dd)
            year_num = int(yyyy)
            month_short = time.strftime("%b", time.strptime(str(month_num), "%m"))
            month_full = time.strftime("%B", time.strptime(str(month_num), "%m"))

            assigned = {"month": False, "day": False, "year": False}

            for i in range(count):
                sel = select_locator.nth(i)
                try:
                    options = sel.locator("option")
                    option_count = options.count()
                except Exception:
                    option_count = 0
                if option_count <= 0:
                    continue

                texts: list[str] = []
                for idx in range(min(option_count, 80)):
                    try:
                        texts.append(str(options.nth(idx).inner_text(timeout=300) or "").strip())
                    except Exception:
                        continue
                joined = " ".join(texts).lower()

                try:
                    if (not assigned["month"]) and (
                        "january" in joined or "february" in joined or "march" in joined or "april" in joined
                    ):
                        for candidate in (month_full, month_short, str(month_num), f"{month_num:02d}"):
                            try:
                                sel.select_option(label=candidate, timeout=800)
                                assigned["month"] = True
                                break
                            except Exception:
                                try:
                                    sel.select_option(value=candidate, timeout=800)
                                    assigned["month"] = True
                                    break
                                except Exception:
                                    continue
                        continue

                    if (not assigned["year"]) and any(str(y) in joined for y in (year_num, year_num - 1, year_num + 1, 2026, 2025)):
                        for candidate in (str(year_num),):
                            try:
                                sel.select_option(label=candidate, timeout=800)
                                assigned["year"] = True
                                break
                            except Exception:
                                try:
                                    sel.select_option(value=candidate, timeout=800)
                                    assigned["year"] = True
                                    break
                                except Exception:
                                    continue
                        continue

                    if (not assigned["day"]) and any(str(x) in joined for x in (" 1 ", "2", "30", "31")):
                        for candidate in (str(day_num), f"{day_num:02d}"):
                            try:
                                sel.select_option(label=candidate, timeout=800)
                                assigned["day"] = True
                                break
                            except Exception:
                                try:
                                    sel.select_option(value=candidate, timeout=800)
                                    assigned["day"] = True
                                    break
                                except Exception:
                                    continue
                except Exception:
                    continue

            # 下拉顺序兜底：month/day/year
            if count >= 3:
                try:
                    if not assigned["month"]:
                        select_locator.nth(0).select_option(label=month_short, timeout=800)
                        assigned["month"] = True
                except Exception:
                    pass
                try:
                    if not assigned["day"]:
                        select_locator.nth(1).select_option(label=str(day_num), timeout=800)
                        assigned["day"] = True
                except Exception:
                    pass
                try:
                    if not assigned["year"]:
                        select_locator.nth(2).select_option(label=str(year_num), timeout=800)
                        assigned["year"] = True
                except Exception:
                    pass

            return assigned["month"] and assigned["day"] and assigned["year"]
        except Exception:
            return False

    visible_inputs = _collect_visible_text_inputs(page)
    if visible_inputs:
        log(
            "about_you 可见输入框: "
            + " | ".join(
                f"#{int(item.get('visibleIndex', 0))} {(_about_you_input_hints(item) or '-')[:80]}"
                for item in visible_inputs[:4]
            )
        )
    ordered_visible_entries = sorted(
        [item for item in visible_inputs if str(item.get("visibleIndex", "")).isdigit()],
        key=lambda item: int(item.get("visibleIndex", 0)),
    )
    name_entry = _pick_best_about_you_input(visible_inputs, "name")
    age_entry = _pick_best_about_you_input(
        visible_inputs,
        "age",
        exclude_visible_indices={int(name_entry.get("visibleIndex"))} if name_entry and str(name_entry.get("visibleIndex", "")).isdigit() else set(),
    )

    name_candidates = [
        page.get_by_label(re.compile(r"full\s*name", re.IGNORECASE)),
        page.get_by_label(re.compile(r"全名|姓名|氏名", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"full\s*name|name", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"全名|姓名|氏名", re.IGNORECASE)),
        page.locator("input[autocomplete='name']"),
        page.locator("input[name*='name' i]"),
        page.locator("input[id*='name' i]"),
        page.locator("input[name*='姓名']"),
        page.locator("input[id*='姓名']"),
        page.locator(
            "xpath=//*[contains(translate(normalize-space(string(.)),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'full name')]/following::input[1]"
        ),
        page.locator("xpath=//*[contains(normalize-space(string(.)),'全名') or contains(normalize-space(string(.)),'姓名')]/following::input[1]"),
    ]
    birthday_candidates = [
        page.get_by_label(re.compile(r"birthday|date of birth|birth", re.IGNORECASE)),
        page.get_by_label(re.compile(r"生日|出生|生年月日", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"birthday|date of birth|birth", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"生日|出生|生年月日", re.IGNORECASE)),
        page.get_by_placeholder(re.compile(r"mm.?dd.?yyyy|yyyy.?mm.?dd|birthday|生日|生年月日", re.IGNORECASE)),
        page.locator("input[name*='birth' i]"),
        page.locator("input[id*='birth' i]"),
        page.locator("input[placeholder*='MM' i]"),
        page.locator("input[placeholder*='DD' i]"),
        page.locator("input[placeholder*='YYYY' i]"),
        page.locator("input[placeholder*='年']"),
        page.locator("input[placeholder*='月']"),
        page.locator("input[placeholder*='日']"),
        page.locator(
            "xpath=//*[contains(translate(normalize-space(string(.)),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'birthday')]/following::input[1]"
        ),
        page.locator("xpath=//*[contains(normalize-space(string(.)),'生日') or contains(normalize-space(string(.)),'出生') or contains(normalize-space(string(.)),'生年月日')]/following::input[1]"),
        page.locator("input[type='date']"),
    ]

    age_years = None
    try:
        birth_year = int(str(birthdate).split("-")[0])
        current_year = int(time.strftime("%Y"))
        age_years = max(25, min(40, current_year - birth_year))
    except Exception:
        age_years = random.randint(25, 35)

    age_candidates = [
        page.get_by_label(re.compile(r"age|umur|usia", re.IGNORECASE)),
        page.get_by_label(re.compile(r"年龄|年齢", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"age|umur|usia", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"年龄|年齢", re.IGNORECASE)),
        page.locator("input[name*='age' i]"),
        page.locator("input[id*='age' i]"),
        page.locator("input[placeholder*='Age' i]"),
        page.locator("input[placeholder*='年龄'], input[placeholder*='年齢']"),
        page.locator(
            "xpath=//*[contains(translate(normalize-space(string(.)),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'age')]/following::input[1]"
        ),
        page.locator("xpath=//*[contains(normalize-space(string(.)),'年龄') or contains(normalize-space(string(.)),'年齢')]/following::input[1]"),
    ]

    fill_result = {"name": False, "birthdate": False, "age": False, "month": False, "day": False, "year": False}
    if _fill_visible_input_entry(name_entry, name):
        fill_result["name"] = True
    if not fill_result.get("name"):
        for candidate in name_candidates:
            if _fill_locator(candidate, name):
                fill_result["name"] = True
                break
    mode_probe = {}
    try:
        mode_probe = page.evaluate(
            """
            () => {
              const labels = Array.from(document.querySelectorAll('label'))
                .map((n) => String(n.textContent || '').trim().toLowerCase())
                .filter(Boolean);
              const groupedLabels = Array.from(document.querySelectorAll('[role="group"][aria-labelledby]'))
                .flatMap((group) => String(group.getAttribute('aria-labelledby') || '')
                  .split(/\\s+/)
                  .map((id) => String(document.getElementById(id)?.textContent || '').trim().toLowerCase()))
                .filter(Boolean);
              const placeholders = Array.from(document.querySelectorAll('input'))
                .map((n) => String(n.placeholder || '').trim().toLowerCase())
                .filter(Boolean);
              const headings = Array.from(document.querySelectorAll('h1,h2,h3'))
                .map((n) => String(n.textContent || '').trim().toLowerCase())
                .filter(Boolean);
              const allText = labels.concat(groupedLabels).concat(placeholders).concat(headings);
              const hasAge = allText.some((t) => t === 'age' || t === 'edad' || t === 'âge' || t === 'alter' || t === 'idade' || t === 'umur' || t === 'usia' || t.includes('how old') || t.includes('年龄') || t.includes('年齢') || t.includes('나이'));
              const hasBirthday = allText.some((t) =>
                t.includes('birthday') || t.includes('date of birth') || t.includes('birth') || t.includes('生日') || t.includes('出生') || t.includes('生年月日') || t.includes('fecha de nacimiento') || t.includes('nascimento') || t.includes('geburtstag') || t.includes('naissance')
              );
              return { labels: labels.concat(groupedLabels), placeholders, headings, hasAge, hasBirthday };
            }
            """
        ) or {}
    except Exception:
        mode_probe = {}

    has_age_label = bool(mode_probe.get("hasAge"))
    has_birthday_label = bool(mode_probe.get("hasBirthday"))
    has_age_field = any(_has_visible(candidate) for candidate in age_candidates)
    has_birthday_field = any(_has_visible(candidate) for candidate in birthday_candidates)
    has_birthday_select = False
    has_segmented_birthday = False
    try:
        has_birthday_select = page.locator("select:visible").count() >= 2
    except Exception:
        has_birthday_select = False
    try:
        has_segmented_birthday = all(
            page.locator(
                f'div[data-type="{part}"], input[data-type="{part}"]'
            ).count() > 0
            for part in ("year", "month", "day")
        )
    except Exception:
        has_segmented_birthday = False
    if has_birthday_select:
        about_mode = "birthday_select"
    elif has_segmented_birthday:
        about_mode = "birthday"
    elif (has_age_label and not has_birthday_label) or (has_age_field and not has_birthday_field):
        about_mode = "age"
    else:
        about_mode = "birthday"
    log(
        f"about_you 页面模式: {about_mode} "
        f"segmented_birthday={has_segmented_birthday} "
        f"labels={mode_probe.get('labels', [])[:4]}"
    )
    direct_name_selector = _resolve_visible_input_selector(
        [
            'input[name="name"]',
            'input[name="full_name"]',
            'input[autocomplete="name"]',
            'input[placeholder*="全名"]',
            'input[placeholder*="氏名"]',
            'input[placeholder*="name" i]',
            'input[id*="name" i]:not([type="hidden"])',
        ]
    )
    direct_age_selector = _resolve_visible_input_selector(
        [
            'input[name="age"]',
            'input[placeholder="Age"]',
            'input[placeholder="age"]',
            'input[placeholder*="年龄"]',
            'input[placeholder*="年齢"]',
            'input[placeholder*="umur" i]',
            'input[placeholder*="usia" i]',
            'input[id*="age" i]',
        ]
    )
    if about_mode == "age" and len(ordered_visible_entries) >= 2:
        name_entry = ordered_visible_entries[0]
        age_entry = ordered_visible_entries[1]
        log(
            f"about_you age 输入框映射: name=#{int(name_entry.get('visibleIndex', 0))}, "
            f"age=#{int(age_entry.get('visibleIndex', 0))}"
        )
    if about_mode == "age":
        log(
            "about_you age 直接定位: "
            f"name={direct_name_selector or '-'}, age={direct_age_selector or '-'}"
        )

    def _fill_segmented_date(mm: str, dd: str, yyyy: str) -> bool:
        """处理 MM / DD / YYYY 分段日期输入框（React DateField 样式）。
        特征：一个 Birthday label 下有多个小 input 或 div[data-type] 段。"""
        try:
            # 方式1: div[data-type] 段 (React Aria DateField)
            month_seg = page.locator('div[data-type="month"], input[data-type="month"]')
            day_seg = page.locator('div[data-type="day"], input[data-type="day"]')
            year_seg = page.locator('div[data-type="year"], input[data-type="year"]')
            if month_seg.count() > 0 and day_seg.count() > 0 and year_seg.count() > 0:
                def replace_segment(locator, value: str) -> None:
                    target = locator.first
                    target.click(force=True)
                    target.press("Control+A")
                    target.type(value, delay=50)
                    time.sleep(0.15)

                # Year first prevents a month/day update from being normalized
                # against the DateField's default current year.
                replace_segment(year_seg, yyyy)
                replace_segment(month_seg, mm)
                replace_segment(day_seg, dd)

                expected = f"{yyyy}-{mm}-{dd}"
                hidden = page.locator('input[name="birthday"]')
                if hidden.count() <= 0:
                    return False
                for _ in range(5):
                    if str(hidden.first.input_value() or "").strip() == expected:
                        return True
                    time.sleep(0.1)
                return False

            # 方式2: 单个 date input 里有 MM/DD/YYYY 占位符
            # 点击输入框，然后按顺序输入 MM DD YYYY（Tab 切换段）
            date_input = page.locator("input[placeholder*='MM'], input[placeholder*='mm'], input[type='date']")
            if date_input.count() > 0:
                date_input.first.click(force=True)
                time.sleep(0.2)
                page.keyboard.type(mm, delay=50)
                page.keyboard.type(dd, delay=50)
                page.keyboard.type(yyyy, delay=50)
                return True

            # 方式3: Birthday label 下的第二个可见 input，直接点击后按数字键输入
            birthday_input = page.get_by_label(
                re.compile(r"birthday|birth|生日|出生|生年月日", re.IGNORECASE)
            )
            if birthday_input.count() > 0:
                birthday_input.first.click(force=True)
                time.sleep(0.2)
                page.keyboard.type(mm, delay=50)
                page.keyboard.type(dd, delay=50)
                page.keyboard.type(yyyy, delay=50)
                return True

            # 方式4: 第二个可见 input（name 是第一个）
            inputs = page.locator("input:visible:not([type='hidden']):not([disabled])")
            if inputs.count() >= 2:
                target = inputs.nth(1)
                target.click(force=True)
                time.sleep(0.3)
                # 先清空
                page.keyboard.press("Control+a")
                page.keyboard.press("Backspace")
                time.sleep(0.1)
                # 输入 MM，Tab 到 DD，Tab 到 YYYY
                page.keyboard.type(mm, delay=80)
                time.sleep(0.3)
                page.keyboard.type(dd, delay=80)
                time.sleep(0.3)
                page.keyboard.type(yyyy, delay=80)
                time.sleep(0.3)
                # 验证是否填入了正确的值
                val = str(target.input_value() or "").strip()
                if val and val != target.get_attribute("placeholder"):
                    return True
                # 如果直接输入不行，试 Tab 切换
                target.click(force=True)
                time.sleep(0.2)
                page.keyboard.press("Control+a")
                page.keyboard.press("Backspace")
                for i, part in enumerate([mm, dd, yyyy]):
                    page.keyboard.type(part, delay=80)
                    if i < 2:
                        page.keyboard.press("Tab")
                        time.sleep(0.2)
                return True
        except Exception:
            pass
        return False

    if about_mode == "birthday_select":
        if len(date_parts) == 3 and _fill_birthday_selects(yyyy, mm, dd):
            fill_result["month"] = True
            fill_result["day"] = True
            fill_result["year"] = True
            fill_result["birthdate"] = True
    elif about_mode == "age":
        if direct_name_selector and _fill_input_like_user(page, direct_name_selector, name):
            fill_result["name"] = True
        elif _fill_visible_input_entry(name_entry, name):
            fill_result["name"] = True
        if age_years is not None:
            if direct_age_selector and _fill_input_like_user(page, direct_age_selector, str(age_years)):
                fill_result["age"] = True
            elif _fill_visible_input_entry(age_entry, str(age_years)):
                fill_result["age"] = True
            if not fill_result.get("age") and len(ordered_visible_entries) < 2:
                for candidate in age_candidates:
                    if _fill_locator(candidate, str(age_years)):
                        fill_result["age"] = True
                        break
        # fallback: 直接找 placeholder="Age" 的输入框
        if not fill_result.get("age") and age_years is not None and len(ordered_visible_entries) < 2:
            try:
                age_input = page.locator("input[placeholder='Age'], input[placeholder='age']")
                if age_input.count() > 0:
                    age_input.first.click(force=True)
                    time.sleep(0.2)
                    age_input.first.fill("")
                    age_input.first.type(str(age_years), delay=random.randint(30, 60))
                    fill_result["age"] = True
            except Exception:
                pass
        if not fill_result.get("age") and age_years is not None:
            excluded_indices = set()
            if name_entry and str(name_entry.get("visibleIndex", "")).isdigit():
                excluded_indices.add(int(name_entry.get("visibleIndex")))
            if _fill_second_visible_input([str(age_years)], excluded_visible_indices=excluded_indices):
                fill_result["age"] = True
        if len(date_parts) == 3 and _sync_hidden_birthday_input(page, f"{yyyy}-{mm}-{dd}", log):
            fill_result["birthdate"] = True
    elif about_mode == "birthday" or about_mode == "birthday_text":
        # 先尝试分段日期输入（MM / DD / YYYY 格式的 DateField）
        if len(date_parts) == 3 and _fill_segmented_date(mm, dd, yyyy):
            fill_result["birthdate"] = True
            log("about_you 使用分段日期输入成功")
        # 再尝试普通文本输入
        if not fill_result.get("birthdate"):
            for candidate in birthday_candidates:
                if _fill_locator(candidate, cn_birthdate):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, us_birthdate):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, birthdate):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, cn_birthdate.replace("/", "")):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, us_birthdate.replace("/", "")):
                    fill_result["birthdate"] = True
                    break
        if not fill_result.get("birthdate"):
            fallback_values = [cn_birthdate, cn_birthdate.replace("/", " / "), cn_birthdate.replace("/", ""), us_birthdate, us_birthdate.replace("/", " / "), us_birthdate.replace("/", ""), birthdate]
            if _fill_second_visible_input(fallback_values):
                fill_result["birthdate"] = True

    log(f"about_you 填写结果: {fill_result}")
    if not fill_result.get("name"):
        raise RuntimeError("about_you 未成功填写 Full name")
    if not (
        fill_result.get("birthdate")
        or fill_result.get("age")
        or (fill_result.get("month") and fill_result.get("day") and fill_result.get("year"))
    ):
        raise RuntimeError("about_you 未成功填写 Birthday/Age")
    _browser_pause(page)

    about_observer = _NetworkActivityObserver(
        page,
        ("/api/accounts/create_account",),
    )
    create_account_requests = about_observer.business_requests
    create_account_responses = about_observer.business_responses
    create_account_failures = about_observer.business_failures

    def _finish_about(result: dict) -> dict:
        about_observer.close()
        return result

    try:
        submit_selector = _click_first(
            page,
            [
                'button:has-text("Finish creating account")',
                'button:has-text("finish creating account")',
                'button[type="submit"]',
                'button[data-testid="continue-button"]',
                'button:has-text("Continue")',
                'button:has-text("continue")',
                'button:has-text("Next")',
                'button:has-text("next")',
            ],
            timeout=8,
        )
    except Exception:
        about_observer.close()
        raise
    if not submit_selector:
        about_observer.close()
        raise RuntimeError("about_you 未找到提交按钮")
    log(f"about_you 已点击继续按钮: {submit_selector}")

    submit_started_at = time.time()
    deadline = submit_started_at + 60
    about_api_attempted = False
    about_api_error = ""
    retried_generic_validation = False
    last_url = page.url
    committed_result: dict | None = None
    committed_at: float | None = None
    while time.time() < deadline:
        current_url = page.url
        last_url = current_url or last_url
        # The Auth SPA may keep /about-you in the URL while the create_account
        # request has already advanced its internal flow state.
        if create_account_responses:
            # Multiple React handlers can race and submit the same about-you
            # form twice.  Prefer an observed 2xx even when a later 409 was
            # queued before this polling loop inspected the responses.
            response_index = 0
            if committed_result is None:
                for index, candidate in enumerate(create_account_responses):
                    candidate_status = int(getattr(candidate, "status", 0) or 0)
                    if 200 <= candidate_status < 300:
                        response_index = index
                        break
            response = create_account_responses.pop(response_index)
            response_status = int(getattr(response, "status", 0) or 0)
            response_url = str(getattr(response, "url", "") or current_url)
            response_text = ""
            response_data = None
            try:
                response_text = str(response.text() or "")
            except Exception:
                pass
            try:
                parsed_response = response.json()
                if isinstance(parsed_response, dict):
                    response_data = parsed_response
            except Exception:
                if response_text:
                    try:
                        parsed_response = json.loads(response_text)
                        if isinstance(parsed_response, dict):
                            response_data = parsed_response
                    except (TypeError, ValueError):
                        pass
            if 200 <= response_status < 300:
                if committed_result is not None:
                    log(
                        "about_you 开户 2xx 已确认；忽略随后重复的成功响应: "
                        f"status={response_status}"
                    )
                    continue
                committed_result = {
                    "ok": True,
                    "status": response_status,
                    "url": response_url,
                    "data": response_data,
                    "text": "",
                    "signup_committed": True,
                }
                committed_at = time.time()
                response_state = _extract_flow_state(response_data, response_url)
                if str(response_state.get("page_type") or "") not in {"", "about_you"}:
                    return _finish_about(committed_result)
                continue
            if response_status >= 400:
                response_error = ""
                response_code = ""
                if isinstance(response_data, dict):
                    error = response_data.get("error")
                    if isinstance(error, dict):
                        response_error = str(error.get("message") or error.get("detail") or "").strip()
                        response_code = str(
                            error.get("code") or error.get("error_code") or ""
                        ).strip()
                    response_error = response_error or str(response_data.get("message") or "").strip()
                    response_code = response_code or str(
                        response_data.get("code") or response_data.get("error_code") or ""
                    ).strip()
                if committed_result is not None:
                    # create_account is an irreversible boundary.  A duplicate
                    # invalid_auth_step/invalid_state response cannot roll back
                    # an earlier 2xx from the same page invocation.
                    committed_result["post_commit_response_status"] = response_status
                    if response_code:
                        committed_result["post_commit_response_code"] = response_code
                    log(
                        "about_you 开户 2xx 已确认；忽略随后重复提交响应: "
                        f"status={response_status} code={response_code or '-'}"
                    )
                    continue
                return _finish_about({
                    "ok": False,
                    "status": response_status,
                    "url": response_url,
                    "data": response_data,
                    "text": response_error or response_text[:500] or f"create_account HTTP {response_status}",
                })
        state_after_submit = _derive_registration_state_from_page(page)
        state_page_type = str(state_after_submit.get("page_type") or "")
        ui_request_in_flight = bool(create_account_requests) and not (
            create_account_responses or create_account_failures
        )
        if state_page_type in {
            "add_phone",
            "consent",
            "workspace_selection",
            "organization_selection",
            "oauth_callback",
            "chatgpt_home",
            "external_url",
        }:
            if committed_result is not None:
                committed_result["url"] = current_url
                committed_result["data"] = {
                    "continue_url": str(
                        state_after_submit.get("continue_url")
                        or state_after_submit.get("current_url")
                        or current_url
                    ),
                    "method": "GET",
                }
                return _finish_about(committed_result)
            if ui_request_in_flight or state_page_type == "add_phone":
                time.sleep(0.2)
                continue
            return _finish_about(
                {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
            )
        if committed_result is not None:
            if committed_at is not None and time.time() - committed_at >= 5:
                committed_result["data"] = {
                    "continue_url": f"{CHATGPT_APP}/",
                    "method": "GET",
                }
                committed_result["transition_pending"] = True
                return _finish_about(committed_result)
            time.sleep(0.2)
            continue
        if (
            not about_api_attempted
            and time.time() - submit_started_at >= 10
            and not ui_request_in_flight
            and not about_observer.has_business_request
        ):
            about_api_attempted = True
            log("about_you URL 未变化，改用浏览器上下文 create_account API 兜底")
            try:
                api_result = _submit_browser_about_you(
                    page,
                    device_id,
                    user_agent,
                    referer=current_url,
                    name=name,
                    birthdate=birthdate,
                )
            except Exception as exc:
                api_result = {
                    "ok": False,
                    "status": 0,
                    "url": current_url,
                    "data": None,
                    "text": str(exc),
                }
            api_status = int(api_result.get("status") or 0)
            api_url = str(api_result.get("url") or current_url)
            api_data = api_result.get("data") if isinstance(api_result.get("data"), dict) else None
            if 200 <= api_status < 300 or api_result.get("ok"):
                if not api_data:
                    # A successful 204/empty response still means the account
                    # was created; advance through the normal ChatGPT landing
                    # route so the existing session extraction can continue.
                    api_data = {
                        "continue_url": f"{CHATGPT_APP}/",
                        "method": "GET",
                    }
                return _finish_about({
                    "ok": True,
                    "status": api_status or 200,
                    "url": api_url,
                    "data": api_data,
                    "text": "",
                    "signup_committed": True,
                })
            if api_status >= 400:
                error_payload = api_result.get("data") if isinstance(api_result.get("data"), dict) else {}
                error_obj = error_payload.get("error") if isinstance(error_payload, dict) else {}
                about_api_error = (
                    str(error_obj.get("message") or error_obj.get("detail") or "").strip()
                    if isinstance(error_obj, dict)
                    else ""
                ) or str(error_payload.get("message") or "").strip() or str(api_result.get("text") or "").strip()[:500]
                log(f"about_you API 兜底返回失败: status={api_status} text={about_api_error[:180]}")
        if "code=" in current_url or "chatgpt.com" in current_url or "sign-in-with-chatgpt" in current_url:
            return _finish_about(
                {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
            )
        if "add-phone" in current_url:
            if ui_request_in_flight:
                time.sleep(0.2)
                continue
            return _finish_about(
                {"ok": False, "status": 0, "url": current_url, "data": None, "text": "add_phone 已出现但未观察到 create_account 成功响应"}
            )
        try:
            error_text = page.locator("text=Sorry, we cannot create your account").first.text_content(timeout=500)
        except Exception:
            error_text = ""
        if not error_text:
            try:
                error_text = page.locator("text=Enter a valid age to continue").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if not error_text:
            try:
                error_text = page.locator("text=doesn't look right").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if not error_text:
            try:
                error_text = page.locator("[role='alert']").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if not error_text:
            try:
                error_text = page.locator(".error, [class*='error'], [class*='Error']").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if error_text and "oai_log" not in error_text and "SSR_HTML" not in error_text:
            normalized_error = str(error_text).strip().lower()
            if (
                about_mode == "age"
                and not retried_generic_validation
                and not about_observer.has_business_request
                and ("doesn't look right" in normalized_error or "try again" in normalized_error)
            ):
                retried_generic_validation = True
                log("about_you age 模式提交被拒，重新同步 Full name/Age/hidden birthday 后重试一次...")
                if direct_name_selector and _fill_input_like_user(page, direct_name_selector, name):
                    fill_result["name"] = True
                elif _fill_visible_input_entry(name_entry, name):
                    fill_result["name"] = True
                elif len(ordered_visible_entries) < 2:
                    for candidate in name_candidates:
                        if _fill_locator(candidate, name):
                            fill_result["name"] = True
                            break
                if age_years is not None:
                    if direct_age_selector and _fill_input_like_user(page, direct_age_selector, str(age_years)):
                        fill_result["age"] = True
                    elif _fill_visible_input_entry(age_entry, str(age_years)):
                        fill_result["age"] = True
                    elif len(ordered_visible_entries) < 2:
                        for candidate in age_candidates:
                            if _fill_locator(candidate, str(age_years)):
                                fill_result["age"] = True
                                break
                if len(date_parts) == 3 and _sync_hidden_birthday_input(page, f"{yyyy}-{mm}-{dd}", log):
                    fill_result["birthdate"] = True
                _browser_pause(page)
                retry_submit_selector = _click_first(
                    page,
                    [
                        'button:has-text("Finish creating account")',
                        'button:has-text("finish creating account")',
                        'button[type="submit"]',
                        'button[data-testid="continue-button"]',
                        'button:has-text("Continue")',
                        'button:has-text("continue")',
                        'button:has-text("Next")',
                        'button:has-text("next")',
                    ],
                    timeout=5,
                )
                if retry_submit_selector:
                    log(f"about_you 重试提交按钮: {retry_submit_selector}")
                    time.sleep(0.5)
                    continue
            return _finish_about(
                {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
            )
        time.sleep(0.5)
    _dump_debug(page, "chatgpt_about_you_fail")
    return _finish_about({
        "ok": False,
        "status": 0,
        "url": last_url,
        "data": None,
        "text": about_api_error
        or (create_account_failures[-1] if create_account_failures else "")
        or "about_you 提交后未跳转",
    })


def _browser_route_action(page_type: str) -> str:
    normalized = str(page_type or "").strip().lower()
    if normalized == "login_password":
        return "existing_account"
    if normalized in {"create_account_password", "password"}:
        return "signup"
    if normalized in {"email_otp_verification", "email_otp_send"}:
        return "otp_verify"
    if normalized == "about_you":
        return "complete_profile"
    if normalized in {"oauth_callback", "chatgpt_home"}:
        return "complete"
    return "observe"


def _raise_existing_account_detected(
    email: str,
    state: dict | None,
    *,
    stage: str,
    reason: str,
    signal: str = "",
) -> None:
    route_state = dict(state or {})
    page_type = str(route_state.get("page_type") or "").strip()
    normalized_signal = str(signal or "").strip() or (
        "login_password" if page_type == "login_password" else "account_already_exists"
    )
    raise ExistingAccountDetected(
        email,
        reason,
        stage=stage,
        signal=normalized_signal,
        page_type=page_type,
        source="browser_registration",
        event={
            "current_url": str(route_state.get("current_url") or "")[:500],
            "continue_url": str(route_state.get("continue_url") or "")[:500],
            "route_source": str(route_state.get("_route_source") or "page"),
        },
    )


def _browser_registration_flow(
    page,
    email: str,
    password: str,
    otp_callback,
    phone_callback,
    log,
    *,
    device_id: str = "",
    initial_state: dict | None = None,
) -> dict:
    device_id = str(device_id or uuid.uuid4())
    try:
        user_agent = str(page.evaluate("() => navigator.userAgent") or "").strip() or _random_chrome_ua()
    except Exception:
        user_agent = _random_chrome_ua()

    _seed_browser_device_id(page, device_id)
    requested_state = dict(initial_state or {})
    profile_data = requested_state.get("profile")
    if not isinstance(profile_data, dict):
        profile_data = {}
    profile_name = str(profile_data.get("name") or "").strip()
    profile_birthdate = str(profile_data.get("birthdate") or "").strip()
    try:
        state = _start_browser_signup_via_page(page, email, log)
    except _BrowserSignupEntryUnavailable as exc:
        log(f"OpenAI 页面注册入口失败，尝试 ChatGPT authorize 入口: {exc}")
        state = _start_browser_signup_via_authorize(page, email, device_id, log)
    route_source = str(state.get("_route_source") or "page")
    route_response_status = state.get("_route_response_status")
    page_otp_triggered = bool(state.pop("_page_otp_triggered", False))
    otp_sent_at_hint = state.pop("_otp_sent_at", None)
    auth_cookies = _get_cookies(page)
    log(
        "授权态 cookies: "
        f"login_session={'yes' if auth_cookies.get('login_session') else 'no'}, "
        f"oai-did={'yes' if auth_cookies.get('oai-did') else 'no'}"
    )
    initial_page_type = str(state.get("page_type") or "")
    route_status_suffix = (
        f" status={route_response_status}" if route_response_status is not None else ""
    )
    log(
        "[路由] stage=after_email "
        f"source={route_source} page={initial_page_type or '-'} "
        f"action={_browser_route_action(initial_page_type)}{route_status_suffix}"
    )
    log(
        f"注册状态起点: page={initial_page_type or '-'} "
        f"url={(state.get('current_url') or '')[:100]}"
    )
    if initial_page_type == "login_password":
        _raise_existing_account_detected(
            email,
            state,
            stage="after_email",
            reason="browser registration reached login_password after email continue",
            signal="login_password",
        )
    register_submitted = False
    browser_otp_sent = False
    signup_committed = False
    seen_states: dict[str, int] = {}

    for step in range(12):
        signature = "|".join(
            [
                str(state.get("page_type") or ""),
                str(state.get("method") or ""),
                str(state.get("continue_url") or ""),
                str(state.get("current_url") or ""),
            ]
        )
        seen_states[signature] = seen_states.get(signature, 0) + 1
        log(
            f"注册状态推进: step={step+1} page={state.get('page_type') or '-'} "
            f"next={str(state.get('continue_url') or '')[:60]} seen={seen_states[signature]}"
        )
        if seen_states[signature] > 2:
            raise RuntimeError(f"注册状态卡住: page={state.get('page_type') or '-'}")

        if _is_registration_complete(state):
            _handle_post_signup_onboarding(page, log)
            return _extract_flow_state(None, page.url)

        if _is_password_registration(state):
            if register_submitted:
                raise RuntimeError("重复进入密码注册阶段")
            log("提交注册密码...")
            pre_cookies = _get_cookies(page)
            log(
                "密码阶段 cookies: "
                f"login_session={'yes' if pre_cookies.get('login_session') else 'no'}, "
                f"oai-client-auth-session={'yes' if pre_cookies.get('oai-client-auth-session') else 'no'}"
            )
            reg_resp = _submit_password_via_page(page, password, log)
            password_status = int(reg_resp.get("status", 0) or 0)
            log(
                "[阶段] stage=password "
                f"result={'ok' if reg_resp.get('ok') else 'failed'} status={password_status}"
            )
            log(f"密码页提交状态: {password_status}")
            if not reg_resp.get("ok"):
                password_error = str(reg_resp.get("text") or "")[:300]
                if is_existing_account_detected_message(password_error):
                    _raise_existing_account_detected(
                        email,
                        {
                            **state,
                            "page_type": "login_password",
                            "current_url": reg_resp.get("url") or state.get("current_url"),
                        },
                        stage=str(
                            reg_resp.get("existing_account_stage") or "password_submit"
                        ),
                        reason=password_error,
                        signal=str(
                            reg_resp.get("existing_account_signal") or "login_password"
                        ),
                    )
                raise RuntimeError(f"密码页提交失败: {password_error}")
            register_submitted = True
            # Always keep the password-submit timestamp as the OTP cutoff, even when
            # the API page type is email_otp_send (otp_triggered used to be false and
            # the early sent_at was dropped). OpenAI often delivers the first code
            # while Camoufox is still settling the password SPA.
            raw_sent_at = reg_resp.get("otp_sent_at")
            if raw_sent_at is not None:
                try:
                    otp_sent_at_hint = float(raw_sent_at)
                except (TypeError, ValueError):
                    pass
            if reg_resp.get("otp_triggered"):
                page_otp_triggered = True
            # 密码提交后 SPA 常立即导航到 email-verification；先 settle 再读状态，避免 evaluate 撞导航
            _wait_for_auth_page_settle(page, timeout=10.0, log=log)
            state = _extract_flow_state(reg_resp.get("data"), reg_resp.get("url", page.url))
            if str(state.get("page_type") or "") == "email_otp_send":
                state["page_type"] = "email_otp_verification"
                # API 返回 send 表示应触发/已触发发码，不把 continue_url 当地址栏导航
                page_otp_triggered = page_otp_triggered or True
                if otp_sent_at_hint is None:
                    otp_sent_at_hint = time.time() - OTP_SENT_AT_FALLBACK_GRACE_SECONDS
            try:
                live_state = _derive_registration_state_from_page(page)
            except Exception as exc:
                if not _is_navigation_context_error(exc):
                    raise
                _wait_for_auth_page_settle(page, timeout=8.0, log=log)
                live_state = _derive_registration_state_from_page(page)
            if live_state.get("page_type") in {
                "email_otp_verification",
                "about_you",
                "add_phone",
                "chatgpt_home",
                "oauth_callback",
            }:
                state = live_state
            elif not state.get("page_type") or _is_password_registration(state):
                state = live_state
            continue

        if str(state.get("page_type") or "") == "login_password":
            log(
                "[路由] stage=state_transition page=login_password "
                "action=existing_account"
            )
            _raise_existing_account_detected(
                email,
                state,
                stage="state_transition",
                reason="browser registration reached login_password",
                signal="login_password",
            )

        if _is_email_otp(state):
            if not otp_callback:
                raise RuntimeError("ChatGPT 注册需要邮箱验证码但未提供 otp_callback")
            otp_sent_at = otp_sent_at_hint
            try:
                first_wait = int((requested_state or {}).get("otp_wait_timeout") or 120)
            except (TypeError, ValueError):
                first_wait = 120
            try:
                resend_wait = int((requested_state or {}).get("otp_resend_wait_timeout") or 90)
            except (TypeError, ValueError):
                resend_wait = 90
            first_wait = max(first_wait, 30)
            resend_wait = max(resend_wait, 30)
            referer = str(
                state.get("current_url")
                or state.get("continue_url")
                or f"{OPENAI_AUTH}/email-verification"
            )
            _wait_for_auth_page_settle(page, timeout=8.0, log=log)
            try:
                live_before_send = _derive_registration_state_from_page(page)
            except Exception as exc:
                if _is_navigation_context_error(exc):
                    _wait_for_auth_page_settle(page, timeout=6.0, log=log)
                    live_before_send = _derive_registration_state_from_page(page)
                else:
                    raise
            referer = str(
                live_before_send.get("current_url")
                or referer
            )
            if _find_first_visible_selector(page, OTP_INPUT_SELECTORS):
                page_otp_triggered = True
                if not otp_sent_at:
                    # Wider grace than the historical 8s: password SPA can hang
                    # 30-60s after register while the OTP mail already lands in
                    # TempMail; a tight cutoff silently drops that mail.
                    otp_sent_at = time.time() - OTP_SENT_AT_FALLBACK_GRACE_SECONDS
                log(
                    "浏览器已出现 OTP 输入框，首次优先等待页面自动发码（超时后再重发）"
                    f" otp_sent_at_age={max(0, int(time.time() - float(otp_sent_at or time.time())))}s"
                )
            if not browser_otp_sent and not page_otp_triggered:
                request_started_at = time.time() - OTP_SENT_AT_FALLBACK_GRACE_SECONDS
                try:
                    send_result = _send_browser_email_otp(
                        page,
                        device_id=device_id,
                        user_agent=user_agent,
                        referer=referer,
                    )
                except Exception as exc:
                    if _is_navigation_context_error(exc):
                        log(f"email-otp/send 期间页面导航，按已发码继续: {exc}")
                        send_result = {"ok": True, "status": 200, "text": "navigation_during_send"}
                        page_otp_triggered = True
                    else:
                        raise
                send_status = int(send_result.get("status") or 0)
                if 200 <= send_status < 300 or send_result.get("ok"):
                    otp_sent_at = request_started_at
                    log(f"浏览器注册验证码已触发: status={send_status or 200}")
                else:
                    log(
                        "浏览器注册验证码触发返回非成功，继续按页面已有验证码等待: "
                        f"status={send_status} text={str(send_result.get('text') or '')[:160]}"
                    )
                browser_otp_sent = True
                _wait_for_auth_page_settle(page, timeout=8.0, log=log)

            def _request_browser_email_otp(wait_timeout: int) -> str:
                log(f"等待 ChatGPT 验证码 timeout={wait_timeout}s")
                callback_payload = {
                    "otp_sent_at": otp_sent_at,
                    "timeout": wait_timeout,
                    "phase": "browser_register_email_otp",
                    "phase_label": "浏览器注册邮箱验证码",
                    "page_type": str(state.get("page_type") or "email_otp_verification"),
                }
                try:
                    callback_value = otp_callback(callback_payload)
                except TypeError:
                    callback_value = otp_callback()
                if isinstance(callback_value, dict):
                    return str(
                        callback_value.get("code")
                        or callback_value.get("otp")
                        or callback_value.get("value")
                        or ""
                    ).strip()
                return str(callback_value or "").strip()

            def _resend_browser_email_otp() -> float:
                """Resend registration OTP via UI first, then email-otp/send API."""
                nonlocal browser_otp_sent
                clicked = _click_first(page, EMAIL_OTP_RESEND_SELECTORS, timeout=4)
                if clicked:
                    log(f"验证码页已点击重发: {clicked}")
                    browser_otp_sent = True
                    _wait_for_auth_page_settle(page, timeout=6.0, log=log)
                    return time.time() - 8
                request_started_at = time.time() - 8
                try:
                    send_result = _send_browser_email_otp(
                        page,
                        device_id=device_id,
                        user_agent=user_agent,
                        referer=referer,
                    )
                except Exception as exc:
                    if _is_navigation_context_error(exc):
                        log(f"email-otp/send 重发期间页面导航，按已发码继续: {exc}")
                        browser_otp_sent = True
                        return request_started_at
                    raise
                send_status = int(send_result.get("status") or 0)
                browser_otp_sent = True
                if 200 <= send_status < 300 or send_result.get("ok"):
                    log(f"浏览器注册验证码已重发: status={send_status or 200}")
                else:
                    log(
                        "浏览器注册验证码重发返回非成功，继续等待新邮件: "
                        f"status={send_status} text={str(send_result.get('text') or '')[:160]}"
                    )
                _wait_for_auth_page_settle(page, timeout=6.0, log=log)
                return request_started_at

            # Align with protocol: first wait auto-sent OTP, then one resend + second wait.
            code = _request_browser_email_otp(first_wait)
            if not code:
                log(
                    f"首次等待未收到验证码，尝试重发后等待 {resend_wait}s "
                    f"(budget-aware via mailbox callback)"
                )
                otp_sent_at = _resend_browser_email_otp()
                code = _request_browser_email_otp(resend_wait)
            if not code:
                raise RuntimeError("未获取到验证码")
            page_otp_triggered = False
            otp_sent_at_hint = None
            otp_resp = _submit_otp_via_page(
                page,
                code,
                log,
                device_id=device_id,
                user_agent=user_agent,
                referer=str(state.get("current_url") or state.get("continue_url") or ""),
            )
            otp_status = int(otp_resp.get("status", 0) or 0)
            log(
                "[阶段] stage=email_otp "
                f"result={'ok' if otp_resp.get('ok') else 'failed'} status={otp_status}"
            )
            log(f"验证码页提交状态: {otp_status}")
            if not otp_resp.get("ok"):
                # Validate rejected or never left OTP: allow the same digits to
                # be fetched again after resend (OpenAI often reuses the code).
                _invoke_otp_release(otp_callback, code, log)
                raise RuntimeError(f"验证码校验失败: {(otp_resp.get('text') or '')[:300]}")
            state = _extract_flow_state(otp_resp.get("data"), otp_resp.get("url", page.url))
            if not state.get("page_type") or _is_email_otp(state):
                # Prefer live DOM: API/URL can still say email-verification while
                # about_you inputs are already painted.
                try:
                    live_after_otp = _derive_registration_state_from_page(page)
                except Exception as exc:
                    if not _is_navigation_context_error(exc):
                        raise
                    _wait_for_auth_page_settle(page, timeout=8.0, log=log)
                    live_after_otp = _derive_registration_state_from_page(page)
                if live_after_otp.get("page_type") and not _is_email_otp(live_after_otp):
                    state = live_after_otp
                elif _is_email_otp(state) or _is_email_otp(live_after_otp or {}):
                    # HTTP 2xx but SPA still on OTP — wait for transition before
                    # burning another mailbox poll that excludes this code.
                    log("验证码 API 已成功，等待 SPA 离开验证码页...")
                    _wait_for_auth_page_settle(page, timeout=12.0, log=log)
                    settle_deadline = time.time() + 20.0
                    advanced = None
                    while time.time() < settle_deadline:
                        try:
                            advanced = _derive_registration_state_from_page(page)
                        except Exception as exc:
                            if not _is_navigation_context_error(exc):
                                raise
                            _wait_for_auth_page_settle(page, timeout=4.0, log=log)
                            continue
                        if advanced.get("page_type") and not _is_email_otp(advanced):
                            state = advanced
                            break
                        time.sleep(0.5)
                    if _is_email_otp(state) and (not advanced or _is_email_otp(advanced)):
                        # Still stuck: release the code so resend/reuse can work.
                        _invoke_otp_release(otp_callback, code, log)
                        log(
                            "验证码提交后页面仍停留在 OTP，已释放该码以便重试/重发"
                        )
                        # Keep state as OTP; next loop iteration will resend path
                        # only if we re-enter OTP with empty code budget — force
                        # one controlled resend instead of a silent second wait.
                        otp_sent_at = _resend_browser_email_otp()
                        page_otp_triggered = True
                        code_retry = _request_browser_email_otp(resend_wait)
                        if not code_retry:
                            raise RuntimeError(
                                "验证码提交后页面未推进，且重发后仍未获取到新验证码"
                            )
                        otp_resp = _submit_otp_via_page(
                            page,
                            code_retry,
                            log,
                            device_id=device_id,
                            user_agent=user_agent,
                            referer=str(
                                state.get("current_url")
                                or state.get("continue_url")
                                or ""
                            ),
                        )
                        log(f"验证码页重试提交状态: {otp_resp.get('status', 0)}")
                        if not otp_resp.get("ok"):
                            _invoke_otp_release(otp_callback, code_retry, log)
                            raise RuntimeError(
                                f"验证码校验失败: {(otp_resp.get('text') or '')[:300]}"
                            )
                        state = _extract_flow_state(
                            otp_resp.get("data"), otp_resp.get("url", page.url)
                        )
                        if not state.get("page_type") or _is_email_otp(state):
                            live_retry = _derive_registration_state_from_page(page)
                            if live_retry.get("page_type"):
                                state = live_retry
            continue

        if _is_about_you(state):
            log("提交 about_you 信息...")
            target_url = _normalize_url(
                str(state.get("current_url") or state.get("continue_url") or f"{OPENAI_AUTH}/about-you"),
                OPENAI_AUTH,
            )
            _ensure_about_you_page(page, target_url, log)
            about_resp = _submit_about_you_via_page(
                page,
                log,
                device_id=device_id,
                user_agent=user_agent,
                profile_name=profile_name,
                profile_birthdate=profile_birthdate,
            )
            about_status = int(about_resp.get("status", 0) or 0)
            about_error = str(about_resp.get("text") or "")[:300]
            about_existing = bool(
                not about_resp.get("ok")
                and is_existing_account_detected_message(about_error)
            )
            log(
                "[阶段] stage=about_you "
                f"result={'existing_account' if about_existing else 'ok' if about_resp.get('ok') else 'failed'} "
                f"status={about_status}"
            )
            log(f"about_you 提交状态: {about_status}")
            if not about_resp.get("ok"):
                if about_existing:
                    _raise_existing_account_detected(
                        email,
                        {
                            **state,
                            "page_type": "about_you",
                            "current_url": about_resp.get("url") or state.get("current_url"),
                        },
                        stage="about_you",
                        reason=about_error,
                        signal="account_already_exists",
                    )
                raise RuntimeError(f"about_you 提交失败: {about_error}")
            signup_committed = bool(about_resp.get("signup_committed"))
            _wait_for_auth_page_settle(page, timeout=12.0, log=log)
            state = _extract_flow_state(about_resp.get("data"), about_resp.get("url", page.url))
            try:
                if not state.get("page_type") or str(state.get("page_type") or "") == "about_you":
                    live_after_about = _derive_registration_state_from_page(page)
                    if live_after_about.get("page_type"):
                        state = live_after_about
            except Exception as exc:
                if _is_navigation_context_error(exc):
                    _wait_for_auth_page_settle(page, timeout=8.0, log=log)
                    state = _derive_registration_state_from_page(page)
                else:
                    raise
            if _is_add_phone(state):
                if not phone_callback:
                    if signup_committed:
                        state["signup_committed"] = True
                        state["signup_commit_source"] = "about_you_create_account_2xx"
                    return state
                log("about_you 后进入 add_phone，尝试短信验证...")
                state = _handle_add_phone_challenge(
                    page,
                    phone_callback,
                    device_id=device_id,
                    user_agent=user_agent,
                    log=log,
                    resume_url=f"{CHATGPT_APP}/",
                )
            continue

        if _is_add_phone(state):
            if not phone_callback:
                if signup_committed:
                    state["signup_committed"] = True
                    state["signup_commit_source"] = "about_you_create_account_2xx"
                return state
            log("注册流程进入 add_phone，尝试短信验证...")
            state = _handle_add_phone_challenge(
                page,
                phone_callback,
                device_id=device_id,
                user_agent=user_agent,
                log=log,
                resume_url=f"{CHATGPT_APP}/",
            )
            continue

        if _requires_registration_navigation(state):
            target_url = _normalize_url(str(state.get("continue_url") or state.get("current_url") or ""), OPENAI_AUTH)
            if not target_url:
                raise RuntimeError("缺少可跟随的 continue_url")
            # 仅跳过 auth 状态机内部 API；ChatGPT next-auth /api/auth/callback 必须 goto
            if _is_internal_auth_api_continue_url(target_url) or (
                "/api/" in target_url and not _is_oauth_browser_callback_url(target_url)
            ):
                log(f"跳过 API continue_url 页面导航: {target_url[:120]}")
                # API continue 交给对应阶段（OTP/about_you）处理
                if "email-otp" in target_url:
                    state = {
                        **state,
                        "page_type": "email_otp_verification",
                        "continue_url": f"{OPENAI_AUTH}/email-verification",
                        "current_url": str(page.url or f"{OPENAI_AUTH}/email-verification"),
                    }
                    continue
                state = _extract_flow_state(None, page.url)
                continue
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                # OAuth callbacks commonly end in a refused localhost/redirect
                # navigation after the URL has already been committed. Keep the
                # actual browser URL and let the next iteration classify it.
                log(f"跟随注册回调导航出现可忽略异常: {exc}")
            _wait_for_auth_page_settle(page, timeout=8.0, log=log)
            try:
                state = _derive_registration_state_from_page(page)
            except Exception as exc:
                if _is_navigation_context_error(exc):
                    _wait_for_auth_page_settle(page, timeout=6.0, log=log)
                    state = _extract_flow_state(None, page.url)
                else:
                    raise
            if not state.get("page_type") and "code=" in str(page.url or ""):
                state = _build_manual_flow_state("oauth_callback", str(page.url or ""))
            continue

        raise RuntimeError(f"未支持的注册状态: page={state.get('page_type') or '-'}")

    raise RuntimeError("注册状态机超出最大步数")


class ChatGPTBrowserRegister:
    def __init__(
        self,
        *,
        headless: bool,
        proxy: Optional[str] = None,
        otp_callback: Optional[Callable[[], str]] = None,
        phone_callback: Optional[Callable[[], str]] = None,
        log_fn: Callable[[str], None] = print,
    ):
        self.headless = headless
        self.proxy = proxy
        self.otp_callback = otp_callback
        self.phone_callback = phone_callback
        self.log = log_fn

    def run(self, email: str, password: str) -> dict:
        launch_opts = _camoufox_launch_opts(headless=self.headless, proxy=self.proxy)

        with ExitStack() as stack:
            browser = _enter_camoufox_with_geoip_fallback(stack, launch_opts, self.log)
            page = browser.new_page()
            self.log("启动浏览器上下文注册状态机")
            final_state = _browser_registration_flow(
                page,
                email,
                password,
                self.otp_callback,
                self.phone_callback,
                self.log,
            )
            self.log(f"注册流程完成: page={final_state.get('page_type') or '-'}")

            # 获取 session token 和 cookies
            cookies_dict = _get_cookies(page)

            # ═══ 通过 Codex CLI OAuth 获取正确的 token ═══
            # 注册完成后的浏览器上下文 session 状态不稳定（NS_BINDING_ABORTED），
            # 直接用全新浏览器做 OAuth 更可靠
            self.log("执行 Codex CLI OAuth 流程获取 token...")

        # 直接用全新浏览器做 OAuth（注册后的浏览器上下文不可靠）
        codex_result = self._retry_oauth_fresh_browser(email, password)
        if codex_result:
            self.log(f"全新浏览器 OAuth 成功: account_id={codex_result.get('account_id','')}")
            return {
                "email": email, "password": password,
                "account_id": codex_result.get("account_id", ""),
                "access_token": codex_result.get("access_token", ""),
                "refresh_token": codex_result.get("refresh_token", ""),
                "id_token": codex_result.get("id_token", ""),
                "session_token": "", "workspace_id": "",
                "cookies": "", "profile": {},
            }

        raise RuntimeError("ChatGPT 注册未完成完整 OAuth callback，已拒绝回退到 session/access_token 半成品结果")

    def _retry_oauth_fresh_browser(self, email, password):
        """在全新浏览器 context 里做 Codex OAuth（绕过 add_phone session）。"""
        launch_opts = _camoufox_launch_opts(headless=self.headless, proxy=self.proxy)
        try:
            with ExitStack() as stack:
                browser = _enter_camoufox_with_geoip_fallback(stack, launch_opts, self.log)
                page = browser.new_page()
                self.log("  全新浏览器 OAuth 开始...")
                result = _do_codex_oauth(
                    page, {}, email, password,
                    self.otp_callback, self.phone_callback, self.proxy, self.log,
                    strict_browser=True,
                )
                return result
        except Exception as e:
            self.log(f"  全新浏览器 OAuth 异常: {e}")
            return None


def run_browser_oauth_token_recovery_sync(
    *,
    email: str,
    password: str,
    proxy: Optional[str],
    otp_callback: Callable[[], str],
    device_id: str = "",
    headless: bool = True,
    log_fn: Callable[[str], None] = print,
) -> dict:
    """Run the any-auto-register-style Codex OAuth flow in an isolated browser.

    A completed signup can still leave the HTTP OAuth client at ``add_phone``
    without a ChatGPT session cookie. The reference implementation solves this
    by opening a fresh browser and revisiting the original OAuth authorization
    URL; OpenAI can then issue the callback without forcing a phone bind. Keep
    this transaction separate from the signup browser so it receives the same
    killable process and proxy/GeoIP setup as the registration stage.
    """
    logger = log_fn or (lambda _message: None)
    effective_headless, headless_reason = resolve_browser_headless(
        headless,
        override_env_names=(),
    )
    ensure_browser_display_available(effective_headless)
    logger(
        "浏览器 OAuth Token recovery 启动: "
        f"mode={'headless' if effective_headless else 'headed'} ({headless_reason})"
    )

    with ExitStack() as stack:
        proxy_config = stack.enter_context(
            playwright_proxy_context(proxy, logger=logger)
        )
        raw_proxy = str(proxy or "").strip() or None
        launch_opts = _camoufox_launch_opts(headless=effective_headless, proxy=raw_proxy)
        if proxy_config:
            launch_opts["proxy"] = proxy_config

        browser = _enter_camoufox_with_geoip_fallback(stack, launch_opts, logger)
        page = browser.new_page()
        page.set_default_timeout(30000)
        page.set_default_navigation_timeout(45000)
        effective_device_id = str(device_id or "").strip()
        if effective_device_id:
            _seed_browser_device_id(page, effective_device_id)

        logger("浏览器 OAuth Token recovery 已进入独立 Codex OAuth 状态机")
        result = _do_codex_oauth(
            page,
            {"oai-did": effective_device_id} if effective_device_id else {},
            str(email or ""),
            str(password or ""),
            otp_callback,
            None,
            proxy,
            logger,
            strict_browser=True,
        )
        if not isinstance(result, dict):
            raise RuntimeError("browser_oauth_token_recovery_empty_result")
        if not str(result.get("access_token") or "").strip():
            raise RuntimeError("browser_oauth_token_recovery_missing_access_token")
        if not str(result.get("refresh_token") or "").strip():
            raise RuntimeError("browser_oauth_token_recovery_missing_refresh_token")
        logger(
            "浏览器 OAuth Token recovery 完成: "
            f"account_id={str(result.get('account_id') or '')[:24]}"
        )
        return dict(result)


def run_browser_registration_stage_sync(
    *,
    email: str,
    password: str,
    proxy: Optional[str],
    otp_callback: Callable[[], str],
    device_id: str,
    headless: bool = True,
    cookies: Optional[list[dict]] = None,
    initial_state: Optional[dict] = None,
    log_fn: Callable[[str], None] = print,
) -> dict:
    """Complete only signup in one Camoufox context and return scoped cookies."""

    logger = log_fn or (lambda _message: None)
    effective_headless, headless_reason = resolve_browser_headless(
        headless,
        override_env_names=(),
    )
    ensure_browser_display_available(effective_headless)
    logger(
        "浏览器注册链路启动: "
        f"mode={'headless' if effective_headless else 'headed'} ({headless_reason})"
    )

    with ExitStack() as stack:
        proxy_config = stack.enter_context(
            playwright_proxy_context(proxy, logger=logger)
        )
        raw_proxy = str(proxy or "").strip() or None
        launch_opts = _camoufox_launch_opts(headless=effective_headless, proxy=raw_proxy)
        if proxy_config:
            launch_opts["proxy"] = proxy_config

        browser = _enter_camoufox_with_geoip_fallback(stack, launch_opts, logger)
        page = browser.new_page()
        page.set_default_timeout(30000)
        page.set_default_navigation_timeout(45000)
        imported_cookies = _import_browser_context_cookies(page, cookies, logger)
        if cookies:
            logger(f"浏览器注册链路导入显式 Cookie: {imported_cookies}/{len(cookies)}")
        logger("浏览器注册链路已进入独立上下文状态机")
        final_state = _browser_registration_flow(
            page,
            email,
            password,
            otp_callback,
            None,
            logger,
            device_id=device_id,
            initial_state=initial_state,
        )
        if not _is_registration_complete(final_state):
            page_type = str(final_state.get("page_type") or "unknown")
            raise RuntimeError(
                f"browser_registration_incomplete: page={page_type}"
            )

        # CRITICAL: OpenAI account is already committed after about_you.
        # Notify parent immediately so HME finalize_success runs before the
        # long Web Session bridge. Otherwise a process kill / deploy mid-bridge
        # leaves the lease reusable and the next attempt hits user_already_exists.
        try:
            _invoke_otp_callback(
                otp_callback,
                {
                    "action": "signup_committed",
                    "email": str(email or ""),
                    "page_type": str(final_state.get("page_type") or ""),
                    "page_url": str(page.url or ""),
                },
            )
            logger(
                "OpenAI 开户已提交（about_you/callback 完成），已通知父任务 finalize HME success"
            )
        except Exception as exc:
            logger(f"通知 signup_committed 失败（继续抓 Web Session）: {exc}")

        try:
            user_agent = str(page.evaluate("() => navigator.userAgent") or "")
        except Exception:
            user_agent = ""

        # about_you 常落到 platform.openai.com callback；必须在同一 Camoufox 上下文
        # 把 OpenAI 登录态桥成 ChatGPT next-auth，再读 AT/session_token。
        # Keep this bounded: parent can OAuth-recover / auth_pending after.
        session_data = _wait_for_web_session(
            page,
            timeout=55,
            log=logger,
            email=str(email or ""),
            device_id=str(device_id or ""),
        )
        cookies = list(page.context.cookies())
        web_session = _normalize_browser_web_session(session_data, cookies)
        cookie_names = sorted(
            {
                str(cookie.get("name") or "")
                for cookie in cookies
                if cookie.get("name")
            }
        )
        has_at = bool(str(web_session.get("access_token") or "").strip())
        has_st = bool(str(web_session.get("session_token") or "").strip())
        logger(
            "浏览器注册链路完成: "
            f"page={final_state.get('page_type') or '-'} "
            f"cookies={','.join(cookie_names)} "
            f"web_at={'yes' if has_at else 'no'} "
            f"web_session_token={'yes' if has_st else 'no'} "
            f"account_id={str(web_session.get('account_id') or '-') }"
        )
        payload = {
            "final_state": final_state,
            "page_url": str(page.url or ""),
            "cookies": cookies,
            "cookie_names": cookie_names,
            "device_id": str(device_id or ""),
            "user_agent": user_agent,
            "web_session": web_session,
            "requested_executor": "headless" if headless else "headed",
            "effective_executor": "headless" if effective_headless else "headed",
            "headless_reason": headless_reason,
        }
        if not has_at:
            # 保留 cookies 供上层协议 Session 补抓；error 使 StageResult.ok=False
            payload["error"] = (
                "browser_registration_missing_web_session: "
                "signup finished but ChatGPT /api/auth/session returned no accessToken; "
                f"page={final_state.get('page_type') or '-'} "
                f"url={(str(page.url or '')[:120])} "
                f"cookies={','.join(cookie_names[:20])}"
            )
            logger(payload["error"])
        return payload
