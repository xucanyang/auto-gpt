"""ChatGPT 浏览器注册流程（Camoufox）。"""
import base64
import json
import os
import random
import re
import secrets
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse

from core.task_runtime import TaskInterruption

from .constants import (
    OPENAI_AUTH,
    CHATGPT_APP,
    PLATFORM_LOGIN_ENTRY,
    SENTINEL_SDK_URL,
    SENTINEL_REQ_URL,
    SENTINEL_FRAME_URL,
    SENTINEL_BASE,
    OAUTH_CONSENT_FORM_SELECTOR,
)
from ..sentinel_browser import (
    run_with_browser_capacity,
    run_with_persistent_browser_capacity,
)
from ..shared_camoufox import (
    shared_camoufox_registration_session,
)
from ..browser_identity import (
    LATEST_CURL_IMPERSONATE,
    infer_browser_family,
    merge_observed_browser_fingerprint,
)
from ..task_logging import format_http_trace_log
from ..web_session_lease import WebSessionLeaseReleaseRequested


_DIAGNOSTIC_VIDEO_CAPABILITY_LOCK = threading.Lock()
_DIAGNOSTIC_VIDEO_UNSUPPORTED = False

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
    'input[autocomplete="current-password"]',
    'input[aria-label*="password" i]',
    'input[placeholder*="password" i]',
    'input[data-testid*="password" i]',
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
    "input[name*='otp' i]",
    "input[id*='otp' i]",
    "input[aria-label*='code' i]",
    "input[aria-label*='verification' i]",
    "input[placeholder*='code' i]",
    "input[placeholder*='verification' i]",
    "input[data-testid*='code' i]",
    "input[data-testid*='otp' i]",
    "input[data-testid*='verification' i]",
    "input[aria-label*='one-time' i]",
    "input[placeholder*='one-time' i]",
    "input[maxlength='1']",
    "input[type='text'][maxlength='6']",
]

SIGNUP_RECOVERY_SELECTORS = [
    'a:has-text("Sign up")',
    'button:has-text("Sign up")',
    'a:has-text("sign up")',
    'button:has-text("sign up")',
    'a:has-text("Register")',
    'button:has-text("Register")',
    'a:has-text("Create account")',
    'button:has-text("Create account")',
    'a:has-text("创建账号")',
    'button:has-text("创建账号")',
    'a:has-text("注册")',
    'button:has-text("注册")',
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


def _registration_transition_timeout_seconds(default: int = 40) -> int:
    try:
        from core.config_store import config_store

        raw = config_store.get(
            "chatgpt_runtime_registration_transition_timeout_seconds",
            str(default),
        )
        parsed = int(float(str(raw).strip()))
    except Exception:
        parsed = default
    return max(20, min(120, parsed))


class _BrowserSignupEntryUnavailable(RuntimeError):
    """No signup form was reached, so authorize fallback is still idempotent."""


def _shared_browser_registration():
    """Load the project-owned, regression-tested browser transaction helpers."""
    from services.chatgpt_core import browser_registration

    return browser_registration


def _browser_failure_detail(result: dict | None, fallback: str = "上游未返回错误摘要") -> str:
    return _shared_browser_registration()._browser_failure_detail(
        result,
        fallback=fallback,
    )


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


def _find_first_visible_selector(page, selectors: list[str]) -> str | None:
    for selector in selectors:
        if _first_visible_locator(page, selector) is not None:
            return selector
    return None


def _wait_for_any_selector(page, selectors: list[str], timeout: int = 30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = _find_first_visible_selector(page, selectors)
        if found:
            return found
        time.sleep(0.5)
    return None


def _click_first(page, selectors: list[str], *, timeout: int = 10) -> str | None:
    found = _wait_for_any_selector(page, selectors, timeout=timeout)
    if not found:
        return None
    try:
        locator = _first_visible_locator(page, found)
        if locator is not None:
            locator.click(timeout=max(1000, int(timeout * 1000)))
            return found
    except Exception:
        return None
    try:
        page.click(found)
        return found
    except Exception:
        return None


def _is_login_password_url(url: str) -> bool:
    return bool(re.search(r"(?:auth|accounts)\.openai\.com/.*log-?in/password", str(url or ""), flags=re.I))


def _build_manual_flow_state(page_type: str, current_url: str) -> dict:
    state = _extract_flow_state(None, current_url)
    state["page_type"] = page_type
    state["current_url"] = current_url
    return state


def _get_visible_page_text(page) -> str:
    try:
        return str(page.evaluate("() => document.body?.innerText || ''") or "")
    except Exception:
        return ""


def _has_signup_registration_choice(page) -> bool:
    if not _is_login_password_url(str(page.url or "")):
        return False
    if _find_first_selector(page, SIGNUP_RECOVERY_SELECTORS):
        return True
    text = _get_visible_page_text(page)
    return bool(re.search(r"sign\s*up|register|create\s*account|还没有帐户|还没有账户|請註冊|请注册|去注册|注册", text, flags=re.I))


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


def _switch_login_password_to_otp(
    page,
    log,
    *,
    context: str,
    timeout: float = 75.0,
) -> dict | None:
    """Use the passwordless action on an existing-account password page."""
    return _shared_browser_registration()._switch_login_password_to_otp(
        page,
        log,
        context=context,
        timeout=timeout,
    )


def _is_login_password_rejection(result: dict | None) -> bool:
    return _shared_browser_registration()._is_login_password_rejection(result)


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


def _fill_input_like_user(page, selector: str, value: str) -> bool:
    try:
        locator = page.locator(selector).first
        locator.wait_for(state="visible", timeout=2000)
        current = str(locator.input_value() or "").strip()
        if current == str(value).strip():
            return True
        locator.click(timeout=1500)
        _browser_pause(page)
        try:
            locator.fill("")
        except Exception:
            pass
        _browser_pause(page, headed=False)
        try:
            locator.type(value, delay=random.randint(35, 85))
        except Exception:
            try:
                page.fill(selector, value)
            except Exception:
                return False
        final_value = str(locator.input_value() or "").strip()
        if final_value == str(value):
            return True
    except Exception:
        pass

    try:
        ok = page.evaluate(
            """
            ({ selector, value }) => {
              const input = document.querySelector(selector);
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
        )
        return bool(ok)
    except Exception:
        return False


def _submit_form_with_fallback(page, input_selector: str) -> bool:
    try:
        return bool(
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
                  if (form?.submit) {
                    form.submit();
                    return true;
                  }
                  input.focus?.();
                  for (const type of ['keydown', 'keypress', 'keyup']) {
                    input.dispatchEvent(new KeyboardEvent(type, {
                      key: 'Enter',
                      code: 'Enter',
                      bubbles: true,
                      cancelable: true,
                    }));
                  }
                  return true;
                }
                """,
                input_selector,
            )
        )
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
            if any(token in hints for token in ("full name", "fullname", "全名", "姓名", "nombre completo", "nom complet", "vollständiger name", "nome completo")):
                score += 10
            if any(token in hints for token in (" name ", "name", "autocomplete=name", "nombre", "nom", "nome")):
                score += 3
            if any(token in hints for token in ("age", "年龄", "edad", "âge", "alter", "idade", "birthday", "birth", "date of birth", "出生", "生日")):
                score -= 8
        elif field == "age":
            if any(token in hints for token in ("age", "年龄", "how old", "edad", "âge", "alter", "idade", "나이")):
                score += 10
            if any(token in hints for token in ("full name", "fullname", "全名", "姓名", "nombre completo", "nom complet")):
                score -= 10
            if "name" in hints and "age" not in hints and "年龄" not in hints and "edad" not in hints:
                score -= 6
            if any(token in hints for token in ("birthday", "birth", "date of birth", "出生", "生日", "fecha de nacimiento", "nascimento")):
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
    # The auth SPA often keeps the previous URL while replacing its form. The
    # visible controls are the authoritative state during that transition.
    otp_selector = _find_first_visible_selector(page, OTP_INPUT_SELECTORS)
    if otp_selector:
        return _build_manual_flow_state("email_otp_verification", current_url)

    try:
        about_visible = bool(
            page.evaluate(
                """
                () => {
                  const inputs = Array.from(document.querySelectorAll("input:not([type='hidden'])"));
                  const text = String(document.body?.innerText || '').toLowerCase();
                  const hasName = inputs.some((el) => {
                    const hint = `${el.name || ''} ${el.id || ''} ${el.placeholder || ''}`.toLowerCase();
                    return hint.includes('name') || hint.includes('姓名') || hint.includes('全名');
                  });
                  const hasAgeOrBirth = inputs.some((el) => {
                    const hint = `${el.name || ''} ${el.id || ''} ${el.placeholder || ''}`.toLowerCase();
                    return hint.includes('age') || hint.includes('birth') || hint.includes('birthday') || hint.includes('年龄') || hint.includes('生日');
                  });
                  return (hasName && hasAgeOrBirth) || text.includes('about you');
                }
                """
            )
        )
    except Exception:
        about_visible = False
    if about_visible:
        return _build_manual_flow_state("about_you", current_url)

    if _find_first_visible_selector(page, PASSWORD_INPUT_SELECTORS):
        page_type = "login_password" if _is_login_password_url(current_url) else "create_account_password"
        return _build_manual_flow_state(page_type, current_url)

    if state.get("page_type"):
        return state

    return state


def _recover_signup_password_page(page, log) -> bool:
    if not _is_login_password_url(str(page.url or "")):
        return False
    if not _has_signup_registration_choice(page):
        return False
    selector = _click_first(page, SIGNUP_RECOVERY_SELECTORS, timeout=2)
    if not selector:
        return False
    log(f"密码页落到登录态，尝试点击注册入口恢复: {selector}")
    time.sleep(1.2)
    return True


def _wait_for_signup_entry_transition(
    page,
    log,
    timeout: int | None = None,
    *,
    response_observer=None,
    input_selector: str = "",
) -> dict:
    base_timeout = float(
        _registration_transition_timeout_seconds() if timeout is None else timeout
    )
    started_at = time.monotonic()
    activity_deadline = started_at + base_timeout
    hard_deadline = started_at + max(base_timeout, 75.0)
    processed_responses = 0
    passwordless_clicked = False
    request_submit_at: float | None = None
    enter_at: float | None = None
    last_business_status = 0
    last_business_url = ""
    last_page_type = ""

    def _with_diagnostics(state: dict, *, source: str) -> dict:
        result = dict(state or {})
        result["_transition_diagnostics"] = {
            "source": source,
            "submit_business_request_seen": bool(
                response_observer is not None
                and response_observer.has_business_request
            ),
            "last_business_status": last_business_status,
            "last_business_url": last_business_url,
            "last_page_type": str(result.get("page_type") or last_page_type),
            "transition_elapsed_ms": round(
                (time.monotonic() - started_at) * 1000
            ),
        }
        return result

    while time.monotonic() < min(activity_deadline, hard_deadline):
        if response_observer is not None:
            responses = response_observer.business_responses
            while processed_responses < len(responses):
                response = responses[processed_responses]
                processed_responses += 1
                status, response_url, data, response_text = (
                    _shared_browser_registration()._browser_response_details(response)
                )
                last_business_status = status
                last_business_url = response_url
                activity_deadline = min(
                    hard_deadline,
                    time.monotonic() + base_timeout,
                )
                response_state = _extract_flow_state(data or None, response_url)
                response_page_type = str(response_state.get("page_type") or "")
                if response_page_type == "email_otp_send":
                    response_state["page_type"] = "email_otp_verification"
                    response_page_type = "email_otp_verification"
                if 200 <= status < 300 and response_page_type in {
                    "create_account_password",
                    "login_password",
                    "email_otp_verification",
                    "about_you",
                    "add_phone",
                    "chatgpt_home",
                    "oauth_callback",
                }:
                    log(
                        "邮箱页业务响应已确认状态推进: "
                        f"HTTP={status} page={response_page_type}"
                    )
                    return _with_diagnostics(
                        response_state,
                        source="business_response",
                    )
                if status >= 400:
                    error_text = (
                        _shared_browser_registration()._browser_response_error(
                            data,
                            response_text,
                        )
                    )
                    raise RuntimeError(
                        "邮箱页业务请求失败: "
                        f"HTTP={status}｜响应={error_text or response_url[:160]}"
                    )

        state = _derive_registration_state_from_page(page)
        last_page_type = str(state.get("page_type") or last_page_type)
        if state.get("page_type") in {
            "create_account_password",
            "login_password",
            "email_otp_verification",
            "about_you",
            "add_phone",
            "chatgpt_home",
            "oauth_callback",
        }:
            if (
                state.get("page_type") == "login_password"
                and _recover_signup_password_page(page, log)
            ):
                return _with_diagnostics(
                    _derive_registration_state_from_page(page),
                    source="signup_recovery",
                )
            return _with_diagnostics(state, source="page_state")

        if not passwordless_clicked and _click_passwordless_login_if_available(
            page,
            log,
            context="邮箱页提交后",
        ):
            passwordless_clicked = True
            time.sleep(0.5)
            continue

        error_text = _extract_auth_error_text(page)
        if error_text:
            raise RuntimeError(f"邮箱页提交失败: {error_text[:300]}")

        if (
            response_observer is not None
            and input_selector
            and not response_observer.has_business_request
        ):
            now = time.monotonic()
            elapsed = now - started_at
            if request_submit_at is None and elapsed >= 8:
                request_submit_at = now
                if _submit_form_with_fallback(page, input_selector):
                    log("邮箱页点击后无业务请求，已执行一次同表单 requestSubmit")
                else:
                    log("邮箱页点击后无业务请求，同表单 requestSubmit 不可用")
            elif (
                request_submit_at is not None
                and enter_at is None
                and now - request_submit_at >= 10
            ):
                enter_at = now
                try:
                    page.locator(input_selector).first.press("Enter", timeout=5000)
                    log("邮箱页 requestSubmit 后仍无业务请求，已执行一次可信 Enter")
                except Exception as exc:
                    log(f"邮箱页 Enter 兜底失败: {str(exc)[:180]}")
        time.sleep(0.25)

    failure = ""
    if response_observer is not None and response_observer.business_failures:
        failure = str(response_observer.business_failures[-1] or "")[:180]
    diagnostic = (
        f"business_request={'yes' if response_observer is not None and response_observer.has_business_request else 'no'} "
        f"last_http={last_business_status or '-'} "
        f"last_page={last_page_type or '-'}"
    )
    if failure:
        diagnostic += f" failure={failure}"
    raise RuntimeError(f"邮箱页提交后未进入密码/验证码页面 ({diagnostic})")


def _start_browser_signup_via_page(page, email: str, log) -> dict:
    entry_errors: list[str] = []
    ready_page_types = {
        "create_account_password",
        "login_password",
        "email_otp_verification",
        "about_you",
        "add_phone",
    }
    for entry_url in (PLATFORM_LOGIN_ENTRY, f"{OPENAI_AUTH}/log-in"):
        try:
            log(f"打开 OpenAI 注册入口: {entry_url}")
            # A committed registration document is enough. Some proxy exits
            # never fire DOMContentLoaded even though the form is interactive.
            page.goto(entry_url, wait_until="commit", timeout=30000)
        except Exception as exc:
            current_url = str(getattr(page, "url", "") or "")
            try:
                recovered_state = _derive_registration_state_from_page(page)
            except Exception:
                recovered_state = {}
            recovered_selector = None
            if recovered_state.get("page_type") not in ready_page_types:
                try:
                    recovered_selector = _wait_for_any_selector(
                        page, EMAIL_INPUT_SELECTORS, timeout=2
                    )
                except Exception:
                    recovered_selector = None
            if recovered_state.get("page_type") in ready_page_types or recovered_selector:
                log(
                    "注册入口导航超时但页面已提交并可继续: "
                    f"url={current_url[:120] or '-'}"
                )
            else:
                log(
                    f"注册入口访问失败: {entry_url} -> {exc} "
                    f"current={current_url[:120] or '-'}"
                )
                entry_errors.append(f"{entry_url}: {exc}")
                continue

        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception as exc:
            log(
                "注册入口 DOMContentLoaded 未完成，继续检查表单状态: "
                f"{str(exc)[:160]}"
            )

        try:
            initial_state = _derive_registration_state_from_page(page)
        except Exception as exc:
            entry_errors.append(f"{entry_url}: 页面状态读取失败: {exc}")
            log(f"注册入口页面状态读取失败: {entry_url} -> {str(exc)[:180]}")
            continue
        if initial_state.get("page_type") in ready_page_types:
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
            if inline_state.get("page_type") == "login_password" and _recover_signup_password_page(page, log):
                return _derive_registration_state_from_page(page)
            return inline_state

        email_observer = _shared_browser_registration()._NetworkActivityObserver(
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

            return _wait_for_signup_entry_transition(
                page,
                log,
                response_observer=email_observer,
                input_selector=email_selector,
            )
        finally:
            email_observer.close()

    detail = "; ".join(entry_errors[-2:])
    raise _BrowserSignupEntryUnavailable(
        f"未找到可用 OpenAI 注册入口邮箱输入框{f': {detail}' if detail else ''}"
    )


def _start_browser_signup_via_authorize(
    page,
    email: str,
    device_id: str,
    log,
    *,
    screen_hint: str = "login_or_signup",
) -> dict:
    login_only = str(screen_hint or "").strip().lower() == "login"
    log("访问 ChatGPT 登录入口..." if login_only else "访问 ChatGPT 首页...")
    try:
        page.goto(f"{CHATGPT_APP}/", wait_until="commit", timeout=30000)
    except Exception as exc:
        current_url = str(getattr(page, "url", "") or "")
        current_host = str(urlparse(current_url).hostname or "").lower()
        target_host = str(urlparse(CHATGPT_APP).hostname or "chatgpt.com").lower()
        if current_host != target_host:
            raise
        log(
            "ChatGPT 首页导航超时但主文档已提交，继续 CSRF 探测: "
            f"url={current_url[:120]} error={str(exc)[:120]}"
        )
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception as exc:
        log(f"ChatGPT 首页 DOMContentLoaded 未完成，继续浏览器内探测: {str(exc)[:160]}")

    # Reuse the hardened project bridge: it decodes next-auth CSRF cookies and
    # avoids APIRequestContext on authenticated SOCKS5 proxy paths.
    from services.chatgpt_core.browser_registration import (
        _get_browser_csrf_token as _get_hardened_csrf_token,
        _start_browser_signin as _start_hardened_browser_signin,
    )

    log("获取 CSRF token...")
    csrf_token = _get_hardened_csrf_token(page, log=log)
    if not csrf_token:
        raise RuntimeError("获取 CSRF token 失败")

    log(f"提交邮箱: {email}")
    authorize_url = _start_hardened_browser_signin(
        page,
        email,
        device_id,
        csrf_token,
        screen_hint="login" if login_only else "login_or_signup",
        log=log,
    )
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
    output_dir = tempfile.mkdtemp(prefix="auto-gpt-browser-debug-")
    page.screenshot(path=os.path.join(output_dir, f"{prefix}.png"))
    with open(os.path.join(output_dir, f"{prefix}.html"), "x", encoding="utf-8") as f:
        f.write(page.content())


def _get_cookies(page) -> dict:
    return {c["name"]: c["value"] for c in page.context.cookies()}


def _fallback_browser_ua() -> str:
    return (
        "Mozilla/5.0 (X11; Linux x86_64; rv:147.0) "
        "Gecko/20100101 Firefox/147.0"
    )


def _infer_sec_ch_ua(user_agent: str) -> str:
    match = re.search(r"Chrome/(\d+)", str(user_agent or ""))
    major = str(match.group(1) if match else "146")
    return f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not.A/Brand";v="99"'


def _capture_browser_fingerprint(
    page,
    cookie_items: list[dict],
    planned_fingerprint: Any = None,
) -> dict:
    """Capture the browser identity that owns the returned Web Session cookies."""
    try:
        browser_state = page.evaluate(
            """
            async () => {
              let webglVendor = '';
              let webglRenderer = '';
              let geolocation = {};
              try {
                const canvas = document.createElement('canvas');
                const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
                const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
                if (gl && ext) {
                  webglVendor = gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) || '';
                  webglRenderer = gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) || '';
                }
              } catch (_) {}
              try {
                geolocation = await Promise.race([
                  new Promise(resolve => navigator.geolocation.getCurrentPosition(
                    position => resolve({
                      latitude: position.coords.latitude,
                      longitude: position.coords.longitude,
                      accuracy: position.coords.accuracy,
                    }),
                    () => resolve({}),
                    { timeout: 500, maximumAge: 60000 }
                  )),
                  new Promise(resolve => setTimeout(() => resolve({}), 600)),
                ]);
              } catch (_) {}
              return {
                user_agent: navigator.userAgent || '',
                locale: navigator.language || '',
                languages: Array.isArray(navigator.languages) ? navigator.languages : [],
                navigator_platform: navigator.platform || '',
                navigator_oscpu: navigator.oscpu || '',
                hardware_concurrency: navigator.hardwareConcurrency || 0,
                max_touch_points: navigator.maxTouchPoints || 0,
                platform_version: navigator.userAgentData?.platformVersion || '',
                viewport_width: window.innerWidth || 0,
                viewport_height: window.innerHeight || 0,
                outer_width: window.outerWidth || 0,
                outer_height: window.outerHeight || 0,
                device_scale_factor: window.devicePixelRatio || 1,
                screen_width: screen.width || 0,
                screen_height: screen.height || 0,
                screen_avail_width: screen.availWidth || 0,
                screen_avail_height: screen.availHeight || 0,
                color_depth: screen.colorDepth || 0,
                pixel_depth: screen.pixelDepth || 0,
                timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
                webgl_vendor: webglVendor,
                webgl_renderer: webglRenderer,
                geolocation,
              };
            }
            """
        ) or {}
    except Exception:
        browser_state = {}
    if not isinstance(browser_state, dict):
        browser_state = {}

    def _positive_int(value) -> int:
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError):
            return 0
        return max(parsed, 0)

    user_agent = str(browser_state.get("user_agent") or "").strip()
    family = infer_browser_family(user_agent)
    device_id = next(
        (
            str(item.get("value") or "").strip()
            for item in list(cookie_items or [])
            if str(item.get("name") or "").strip() == "oai-did"
        ),
        "",
    )
    observed = {
        "device_id": device_id,
        "impersonate": LATEST_CURL_IMPERSONATE[family],
        "user_agent": user_agent,
        "browser_family": family,
        "platform_version": str(browser_state.get("platform_version") or "").strip(),
        "locale": str(browser_state.get("locale") or "").strip(),
        "languages": list(browser_state.get("languages") or []),
        "navigator_platform": str(browser_state.get("navigator_platform") or "").strip(),
        "navigator_oscpu": str(browser_state.get("navigator_oscpu") or "").strip(),
        "timezone": str(browser_state.get("timezone") or "").strip(),
        "webgl_vendor": str(browser_state.get("webgl_vendor") or "").strip(),
        "webgl_renderer": str(browser_state.get("webgl_renderer") or "").strip(),
        "geolocation": dict(browser_state.get("geolocation") or {}),
    }
    for key in (
        "viewport_width",
        "viewport_height",
        "outer_width",
        "outer_height",
        "screen_width",
        "screen_height",
        "screen_avail_width",
        "screen_avail_height",
        "color_depth",
        "pixel_depth",
        "hardware_concurrency",
        "max_touch_points",
    ):
        observed[key] = _positive_int(browser_state.get(key))
    try:
        observed["device_scale_factor"] = float(
            browser_state.get("device_scale_factor") or 1.0
        )
    except (TypeError, ValueError):
        observed["device_scale_factor"] = 1.0
    if not planned_fingerprint:
        languages = list(observed.get("languages") or [])
        observed["accept_language"] = ",".join(languages)
    return merge_observed_browser_fingerprint(planned_fingerprint, observed)


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
        "user-agent": user_agent or _fallback_browser_ua(),
        "accept-language": "en-US,en;q=0.9",
        "accept": accept,
    }
    if infer_browser_family(user_agent) == "chrome":
        headers.update(
            {
                "sec-ch-ua": _infer_sec_ch_ua(user_agent),
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": (
                    '"macOS"' if "Macintosh" in str(user_agent or "") else '"Windows"'
                ),
            }
        )
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
    def __init__(self, device_id: str, user_agent: str, browser_fingerprint: Any = None):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent or _fallback_browser_ua()
        self.sid = str(uuid.uuid4())
        self.browser_fingerprint = dict(browser_fingerprint or {})

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
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        perf_now = 1000 + random.random() * 49000
        profile = self.browser_fingerprint
        screen = (
            f"{int(profile.get('screen_width') or 1920)}x"
            f"{int(profile.get('screen_height') or 1080)}"
        )
        languages = list(profile.get("languages") or ["en-US", "en"])
        family = str(profile.get("browser_family") or "firefox")
        try:
            zone = ZoneInfo(str(profile.get("timezone") or "UTC"))
        except Exception:
            zone = timezone.utc
        now = datetime.now(zone)
        date_string = now.strftime("%a %b %d %Y %H:%M:%S GMT%z") + (
            f" ({now.tzname() or 'Coordinated Universal Time'})"
        )
        return [
            screen,
            date_string,
            4294705152 if family == "chrome" else None,
            random.random(),
            self.user_agent,
            SENTINEL_SDK_URL,
            None,
            None,
            str(profile.get("locale") or "en-US"),
            ",".join(str(item) for item in languages),
            random.random(),
            "webkitTemporaryStorage−undefined",
            "location",
            "Object",
            perf_now,
            self.sid,
            "",
            int(profile.get("hardware_concurrency") or 8),
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
    return page.evaluate(
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
    )


def _build_browser_sentinel_token(page, device_id: str, flow: str, user_agent: str) -> str:
    generator = _SentinelTokenGenerator(
        device_id,
        user_agent,
        _capture_browser_fingerprint(page, []),
    )
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
    resend: bool = False,
) -> dict:
    return _shared_browser_registration()._send_browser_email_otp(
        page,
        device_id=device_id,
        user_agent=user_agent,
        referer=referer,
        resend=resend,
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

    s = cffi_requests.Session(impersonate="firefox147")
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
                "user-agent": "Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 Firefox/147.0",
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
                    "user-agent": "Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 Firefox/147.0",
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
    return _shared_browser_registration()._submit_login_email_via_page(
        page,
        email,
        log,
    )

def _do_codex_oauth(page, cookies_dict: dict, email: str, password: str, otp_callback, phone_callback, proxy: str | None, log) -> dict | None:
    """在真实浏览器会话内完成 Codex OAuth，返回完整 token 包。"""
    from .oauth import generate_oauth_url
    from .constants import CODEX_CLIENT_ID, CODEX_REDIRECT_URI, CODEX_SCOPE

    oauth_start = generate_oauth_url(
        redirect_uri=CODEX_REDIRECT_URI,
        scope=CODEX_SCOPE,
        client_id=CODEX_CLIENT_ID,
    )
    try:
        user_agent = str(page.evaluate("() => navigator.userAgent") or "").strip() or _fallback_browser_ua()
    except Exception:
        user_agent = _fallback_browser_ua()
    device_id = str(cookies_dict.get("oai-did") or uuid.uuid4())
    log(f"  OAuth state={oauth_start.state[:20]}...")

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
                    raise RuntimeError(
                        "OAuth 邮箱页提交失败: "
                        f"{_browser_failure_detail(email_resp)}"
                    )
                continue

            if state["page_type"] in {"login_password", "create_account_password"}:
                log("  OAuth 页面需要密码登录，提交密码...")
                # OAuth 流程中直接填密码登录，不尝试恢复到注册态
                password_resp = _submit_oauth_password_direct(page, password, log)
                log(f"  OAuth 密码页提交状态: {password_resp.get('status', 0)}")
                if not password_resp.get("ok"):
                    raise RuntimeError(
                        "OAuth 密码页提交失败: "
                        f"{_browser_failure_detail(password_resp)}"
                    )
                continue

            if state["page_type"] == "email_otp_verification":
                if not otp_callback:
                    log("  ⚠️ OAuth 需要邮箱 OTP 但没有 otp_callback")
                    return None
                log("  OAuth 等待邮箱验证码...")
                code = otp_callback()
                if not code:
                    log("  ⚠️ OAuth OTP 获取失败")
                    return None
                otp_resp = _submit_otp_via_page(
                    page,
                    code,
                    log,
                    device_id=device_id,
                    user_agent=user_agent,
                    referer=current_url,
                    assume_success_without_state=False,
                )
                log(f"  OAuth 验证码页提交状态: {otp_resp.get('status', 0)}")
                if not otp_resp.get("ok"):
                    raise RuntimeError(
                        "OAuth 验证码校验失败: "
                        f"{_browser_failure_detail(otp_resp)}"
                    )
                continue

            if state["page_type"] == "about_you":
                log("  OAuth 页面出现 about_you，继续页面填写...")
                about_resp = _submit_about_you_via_page(
                    page,
                    log,
                    device_id=device_id,
                    user_agent=user_agent,
                )
                log(f"  OAuth about_you 提交状态: {about_resp.get('status', 0)}")
                if not about_resp.get("ok"):
                    raise RuntimeError(
                        "OAuth about_you 提交失败: "
                        f"{_browser_failure_detail(about_resp)}"
                    )
                continue

            if state["page_type"] in {"consent", "workspace_selection", "organization_selection", "external_url"}:
                browser_result = _complete_oauth_in_browser(page, oauth_start, proxy, log)
                if browser_result:
                    return browser_result
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
                        # 回退到 curl session 方式
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
                # 检查 cookie 里是否有 session
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
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = page.evaluate("""
            async () => {
                const r = await fetch('https://chatgpt.com/api/auth/session', {
                    credentials: 'include',
                    headers: { 'accept': 'application/json' },
                });
                const j = await r.json();
                return j.accessToken || '';
            }
            """)
            if r:
                return r
        except Exception:
            pass
        time.sleep(2)
    return ""


def _is_registration_complete(state: dict) -> bool:
    page_type = str(state.get("page_type") or "")
    url = str(state.get("current_url") or state.get("continue_url") or "").lower()
    return page_type in {"callback", "oauth_callback", "chatgpt_home"} or (
        "chatgpt.com" in url and "redirect_uri" not in url and "about-you" not in url
    )


def _signup_callback_url(page, state: dict) -> str:
    """Return the post-signup callback URL without treating a login URL as one."""
    candidates = [
        str((state or {}).get("continue_url") or "").strip(),
        str((state or {}).get("current_url") or "").strip(),
        str(getattr(page, "url", "") or "").strip(),
    ]
    for raw in candidates:
        candidate = _normalize_url(raw, OPENAI_AUTH)
        if not candidate:
            continue
        parsed = urlparse(candidate)
        path = str(parsed.path or "").lower()
        query = str(parsed.query or "").lower()
        if "code=" in query or "callback" in path or "/oauth/" in path:
            return candidate
    return ""


def _follow_signup_callback(page, state: dict, log) -> str:
    """Follow OpenAI's create-account callback in the existing browser context."""
    callback_url = _signup_callback_url(page, state)
    if not callback_url:
        return str(getattr(page, "url", "") or "").strip()
    current_url = str(getattr(page, "url", "") or "").strip()
    if callback_url == current_url and "chatgpt.com" in current_url:
        return current_url
    log(f"跟随 signup callback 建立 ChatGPT 会话: {callback_url[:160]}")
    try:
        page.goto(callback_url, wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        # Redirects to ChatGPT can abort the original document navigation; the
        # waiter below will inspect the final URL and session cookies.
        log(f"signup callback 导航异常（可继续检查最终 URL）: {exc}")
    return str(getattr(page, "url", "") or callback_url).strip()


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
    target = f"{state.get('continue_url') or ''} {state.get('current_url') or ''}".lower()
    return str(state.get("page_type") or "") == "email_otp_verification" or "email-verification" in target or "email-otp" in target


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

        # 手机 OTP 只走当前 add-phone 表单，禁止误用邮箱 validate API。
        otp_resp = _submit_ui_otp_via_page(page, sms_code, log)
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


def _requires_registration_navigation(state: dict) -> bool:
    if str(state.get("method") or "GET").upper() != "GET":
        return False
    if str(state.get("page_type") or "") == "external_url" and state.get("continue_url"):
        return True
    continue_url = str(state.get("continue_url") or "")
    current_url = str(state.get("current_url") or "")
    return bool(continue_url and continue_url != current_url)


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


def _get_browser_csrf_token(page) -> str:
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
        return str((result.get("data") or {}).get("csrfToken") or "").strip()
    return ""


def _start_browser_signin(page, email: str, device_id: str, csrf_token: str) -> str:
    from urllib.parse import urlencode

    query = urlencode(
        {
            "prompt": "login",
            "ext-oai-did": device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "screen_hint": "login_or_signup",
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
    if result.get("ok") and isinstance(result.get("data"), dict):
        return str((result.get("data") or {}).get("url") or "").strip()
    return ""


def _browser_authorize(page, auth_url: str, log) -> str:
    return _shared_browser_registration()._browser_authorize(page, auth_url, log)


def _validate_browser_email_otp(page, code: str, device_id: str, user_agent: str, referer: str) -> dict:
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
    sentinel = _build_browser_sentinel_token(page, device_id, "email_otp_validate", user_agent)
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


def _submit_browser_about_you(page, device_id: str, user_agent: str, referer: str) -> dict:
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
            **_generate_datadog_trace_headers(),
        },
    )
    sentinel = _build_browser_sentinel_token(page, device_id, "oauth_create_account", user_agent)
    if sentinel:
        headers["openai-sentinel-token"] = sentinel
    user_info = generate_random_user_info()
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
    return _shared_browser_registration()._submit_oauth_password_direct(
        page,
        password,
        log,
    )


def _submit_password_via_page(page, password: str, log) -> dict:
    return _shared_browser_registration()._submit_password_via_page(
        page,
        password,
        log,
    )


def _submit_otp_via_page(
    page,
    code: str,
    log,
    *,
    device_id: str = "",
    user_agent: str = "",
    referer: str = "",
    assume_success_without_state: bool = True,
) -> dict:
    result = _shared_browser_registration()._submit_otp_via_page(
        page,
        code,
        log,
        device_id=device_id,
        user_agent=user_agent,
        referer=referer,
        allow_api_fallback=True,
        assume_success_without_state=assume_success_without_state,
    )
    if result.get("ok") and 200 <= int(result.get("status") or 0) < 300:
        result.setdefault("otp_committed", True)
    return result


def _invoke_otp_callback(otp_callback, payload: dict[str, Any]) -> Any:
    """Bridge contextual OTP leases while retaining no-argument callbacks."""

    if not callable(otp_callback):
        return None
    request = dict(payload or {})
    try:
        return otp_callback(request)
    except TypeError:
        try:
            return otp_callback(**request)
        except TypeError:
            return otp_callback()


def _normalize_otp_callback_result(
    value: Any,
    *,
    challenge_id: str,
    generation: int,
) -> tuple[str, dict[str, Any]]:
    if isinstance(value, dict):
        result = dict(value)
        returned_challenge = str(result.get("challenge_id") or "").strip()
        if returned_challenge and returned_challenge != challenge_id:
            raise RuntimeError(
                "otp_challenge_mismatch: mailbox result belongs to another challenge"
            )
        try:
            returned_generation = int(result.get("generation") or 0)
        except (TypeError, ValueError):
            returned_generation = 0
        if returned_generation and returned_generation != generation:
            raise RuntimeError(
                "otp_generation_mismatch: mailbox result belongs to an old generation"
            )
        code = str(
            result.get("code")
            or result.get("otp")
            or result.get("value")
            or ""
        ).strip()
        return code, result
    return str(value or "").strip(), {}


def _terminal_registration_business_error(value: Any) -> bool:
    text = str(value or "").lower()
    return any(
        marker in text
        for marker in (
            "registration_disallowed",
            "identity_provider_mismatch",
            "account_deactivated",
            "account_deleted",
        )
    )


def _submit_ui_otp_via_page(page, code: str, log) -> dict:
    """Submit non-email OTP forms without calling the email validation API."""
    otp = str(code or "").strip()
    if not otp:
        return {
            "ok": False,
            "status": 400,
            "url": page.url,
            "data": None,
            "text": "验证码为空",
        }

    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    time.sleep(1)

    filled = False
    try:
        digit_inputs = page.locator(
            "input[inputmode='numeric'], input[autocomplete='one-time-code'], "
            "input[type='tel'], input[type='number']"
        )
        count = digit_inputs.count()
        if count >= len(otp):
            done = 0
            for index in range(min(count, len(otp))):
                box = digit_inputs.nth(index)
                try:
                    box.wait_for(state="visible", timeout=800)
                    box.fill("")
                    box.type(otp[index], delay=random.randint(20, 60))
                    done += 1
                except Exception:
                    break
            if done >= len(otp):
                filled = True
                log(f"验证码页已填写 {done} 位分格输入框")
    except Exception:
        pass

    if not filled:
        otp_candidates = [
            page.get_by_label(
                re.compile(r"verification code|code|otp", re.IGNORECASE)
            ),
            page.get_by_role(
                "textbox",
                name=re.compile(r"verification code|code|otp", re.IGNORECASE),
            ),
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
                if str(target.input_value() or "").strip():
                    filled = True
                    log("验证码页已填写单输入框")
                    break
            except Exception:
                continue

    if not filled:
        time.sleep(3)
        for selector in (
            "input[inputmode='numeric']",
            "input[autocomplete='one-time-code']",
            "input[name*='code' i]",
            "input[type='text']",
        ):
            try:
                target = page.locator(selector).first
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
        return {
            "ok": False,
            "status": 0,
            "url": page.url,
            "data": None,
            "text": "验证码页未找到可填写输入框",
        }

    _browser_pause(page)
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
        return {
            "ok": False,
            "status": 0,
            "url": page.url,
            "data": None,
            "text": "验证码页未找到 Continue 按钮",
        }
    log(f"验证码页已点击继续按钮: {submit_selector}")

    deadline = time.time() + _registration_transition_timeout_seconds()
    last_url = str(page.url or "")
    while time.time() < deadline:
        current_url = str(page.url or "")
        last_url = current_url or last_url
        if any(
            marker in current_url
            for marker in (
                "about-you",
                "add-phone",
                "chatgpt.com",
                "code=",
                "consent",
                "sign-in-with-chatgpt",
                "workspace",
                "organization",
            )
        ):
            return {
                "ok": True,
                "status": 200,
                "url": current_url,
                "data": None,
                "text": "",
            }
        try:
            error_text = page.locator("text=Invalid code").first.text_content(
                timeout=400
            )
        except Exception:
            error_text = ""
        if error_text:
            return {
                "ok": False,
                "status": 400,
                "url": current_url,
                "data": None,
                "text": error_text,
            }
        time.sleep(0.5)
    return {
        "ok": False,
        "status": 0,
        "url": last_url,
        "data": None,
        "text": "验证码页提交后未跳转",
    }


def _submit_about_you_via_page(
    page,
    log,
    *,
    device_id: str = "",
    user_agent: str = "",
    profile_name: str = "",
    profile_birthdate: str = "",
) -> dict:
    result = _shared_browser_registration()._submit_about_you_via_page(
        page,
        log,
        device_id=device_id,
        user_agent=user_agent,
        profile_name=profile_name,
        profile_birthdate=profile_birthdate,
    )
    if result.get("ok") and 200 <= int(result.get("status") or 0) < 300:
        result.setdefault("signup_committed", True)
    return result


def _ensure_about_you_page(page, target_url: str, log) -> None:
    _shared_browser_registration()._ensure_about_you_page(page, target_url, log)


def _committed_signup_partial_state(
    page,
    state: dict,
    *,
    otp_committed: bool,
    reason: str,
) -> dict:
    current_url = str(getattr(page, "url", "") or state.get("current_url") or "")
    return {
        **dict(state or {}),
        "page_type": "post_signup_partial",
        "current_url": current_url,
        "otp_committed": bool(otp_committed),
        "signup_committed": True,
        "signup_commit_source": "about_you_create_account_2xx",
        "session_capture_pending": True,
        "post_signup_failure_code": str(reason or "post_signup_state_unresolved"),
    }


def _browser_registration_flow(
    page,
    email: str,
    password: str,
    otp_callback,
    phone_callback,
    log,
    *,
    profile_name: str = "",
    profile_birthdate: str = "",
    stop_check: Callable[[], None] | None = None,
    login_only: bool = False,
    otp_wait_timeout: int = 120,
    otp_resend_wait_timeout: int = 90,
) -> dict:
    device_id = str(uuid.uuid4())
    try:
        user_agent = str(page.evaluate("() => navigator.userAgent") or "").strip() or _fallback_browser_ua()
    except Exception:
        user_agent = _fallback_browser_ua()

    _seed_browser_device_id(page, device_id)
    if login_only:
        state = _start_browser_signup_via_authorize(
            page,
            email,
            device_id,
            log,
            screen_hint="login",
        )
    else:
        try:
            state = _start_browser_signup_via_page(page, email, log)
        except _BrowserSignupEntryUnavailable as exc:
            log(f"页面驱动注册入口失败，回退 ChatGPT authorize 入口: {exc}")
            state = _start_browser_signup_via_authorize(page, email, device_id, log)
    auth_cookies = _get_cookies(page)
    log(
        "授权态 cookies: "
        f"login_session={'yes' if auth_cookies.get('login_session') else 'no'}, "
        f"oai-did={'yes' if auth_cookies.get('oai-did') else 'no'}"
    )
    flow_label = "登录测活" if login_only else "注册"
    log(
        f"{flow_label}状态起点: page={state.get('page_type') or '-'} "
        f"url={(state.get('current_url') or '')[:100]}"
    )
    transition_diagnostics = state.get("_transition_diagnostics")
    if isinstance(transition_diagnostics, dict):
        log(
            "[状态推进] stage=email "
            f"source={transition_diagnostics.get('source') or '-'} "
            f"business_request={'yes' if transition_diagnostics.get('submit_business_request_seen') else 'no'} "
            f"last_http={transition_diagnostics.get('last_business_status') or '-'} "
            f"elapsed_ms={transition_diagnostics.get('transition_elapsed_ms') or 0}"
        )
    otp_sent_at_hint = state.pop("_otp_sent_at", None)
    register_submitted = False
    otp_committed = False
    signup_committed = False
    otp_challenge_id = uuid.uuid4().hex
    otp_generation = 0
    submitted_otp_codes: set[str] = set()
    otp_resend_attempted = False
    authorize_reentry_attempted = False
    passwordless_attempts = 0
    seen_states: dict[str, int] = {}

    for step in range(12):
        if callable(stop_check):
            stop_check()
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
            f"{flow_label}状态推进: step={step+1} page={state.get('page_type') or '-'} "
            f"next={str(state.get('continue_url') or '')[:60]} seen={seen_states[signature]}"
        )
        if seen_states[signature] > 2:
            raise RuntimeError(f"{flow_label}状态卡住: page={state.get('page_type') or '-'}")

        if _is_registration_complete(state):
            if not login_only:
                _handle_post_signup_onboarding(page, log)
            completed_state = dict(state or {})
            completed_state["page_url"] = str(page.url or "")
            completed_state["current_url"] = str(page.url or "") or str(
                state.get("current_url") or state.get("continue_url") or ""
            )
            completed_state["otp_committed"] = bool(otp_committed)
            completed_state["signup_committed"] = bool(signup_committed)
            return completed_state

        if signup_committed and (
            _is_password_registration(state)
            or _is_email_otp(state)
            or _is_about_you(state)
        ):
            log(
                "开户业务请求已确认但页面回落到注册阶段；"
                "停止重复提交并转入已有账号登录恢复"
            )
            return _committed_signup_partial_state(
                page,
                state,
                otp_committed=otp_committed,
                reason="post_signup_state_regressed",
            )

        if _is_password_registration(state):
            if login_only:
                raise RuntimeError("失效测活拒绝进入新账号注册密码阶段")
            if register_submitted:
                raise RuntimeError("重复进入密码注册阶段")
            log("提交注册密码...")
            pre_cookies = _get_cookies(page)
            log(
                "密码阶段 cookies: "
                f"login_session={'yes' if pre_cookies.get('login_session') else 'no'}, "
                f"oai-client-auth-session={'yes' if pre_cookies.get('oai-client-auth-session') else 'no'}"
            )
            try:
                reg_resp = _submit_password_via_page(page, password, log)
            except RuntimeError:
                # The SPA can replace the password form with OTP while keeping
                # /create-account/password in the address bar. Re-read the live
                # controls before surfacing a password lookup failure.
                live_state = _derive_registration_state_from_page(page)
                if _is_email_otp(live_state):
                    log("密码提交期间页面已切换到一次性验证码，跳过重复密码提交")
                    state = live_state
                    continue
                raise
            if reg_resp.get("register_committed") and not reg_resp.get("ok"):
                log(
                    "密码注册业务请求已成功，SPA 未离开旧页面；"
                    "按已提交状态进入邮箱验证码阶段，不重复提交密码"
                )
                reg_resp = {
                    **reg_resp,
                    "ok": True,
                    "data": {
                        "page": {
                            "type": "email_otp_verification",
                            "payload": {
                                "url": f"{OPENAI_AUTH}/email-verification"
                            },
                        }
                    },
                }
            if not reg_resp.get("ok"):
                live_state = _derive_registration_state_from_page(page)
                if _is_email_otp(live_state):
                    log(
                        "密码请求返回失败但页面已进入一次性验证码，"
                        f"HTTP={reg_resp.get('status', 0) or '-'}；直接进入 OTP"
                    )
                    state = live_state
                    continue
                raise RuntimeError(
                    "密码页提交失败: "
                    f"{_browser_failure_detail(reg_resp)}"
                )
            register_submitted = True
            raw_otp_sent_at = reg_resp.get("otp_sent_at")
            if raw_otp_sent_at is not None:
                try:
                    otp_sent_at_hint = float(raw_otp_sent_at)
                except (TypeError, ValueError):
                    pass
            state = _extract_flow_state(reg_resp.get("data"), reg_resp.get("url", page.url))
            if not state.get("page_type") or _is_password_registration(state):
                state = _derive_registration_state_from_page(page)
            log(
                f"[注册] 注册密码已提交｜HTTP={reg_resp.get('status', 0) or '-'} "
                f"｜业务提交={'是' if reg_resp.get('register_committed') else '否'}"
                f"｜下一页={state.get('page_type') or '-'}"
            )
            continue

        if str(state.get("page_type") or "") == "login_password":
            if not login_only and _recover_signup_password_page(page, log):
                state = _derive_registration_state_from_page(page)
                continue
            if login_only and passwordless_attempts == 0:
                passwordless_attempts += 1
                passwordless_state = _switch_login_password_to_otp(
                    page,
                    log,
                    context="登录测活密码页",
                )
                if passwordless_state is not None:
                    state = passwordless_state
                    continue
                log("登录测活未切换到一次性验证码状态，继续使用库存密码")
            log(
                "提交已有账号登录密码..."
                if login_only
                else "注册流程落到已有账号登录密码页，按登录流程继续认证..."
            )
            login_resp = _submit_oauth_password_direct(page, password, log)
            log(f"登录密码页提交状态: {login_resp.get('status', 0)}")
            if not login_resp.get("ok"):
                if (
                    login_only
                    and passwordless_attempts < 2
                    and _is_login_password_rejection(login_resp)
                ):
                    passwordless_attempts += 1
                    log("库存密码被 OpenAI 拒绝，尝试一次性验证码登录兜底")
                    passwordless_state = _switch_login_password_to_otp(
                        page,
                        log,
                        context="登录测活密码失败兜底",
                    )
                    if passwordless_state is not None:
                        state = passwordless_state
                        continue
                raise RuntimeError(
                    "登录密码页提交失败: "
                    f"{_browser_failure_detail(login_resp)}"
                )
            state = dict(login_resp.get("next_state") or {})
            if not state.get("page_type"):
                state = _extract_flow_state(
                    login_resp.get("data"),
                    login_resp.get("url", page.url),
                )
            if not state.get("page_type"):
                state = _derive_registration_state_from_page(page)
            if (
                login_resp.get("password_verified")
                and login_resp.get("transition_pending")
                and str(state.get("page_type") or "") == "login_password"
            ):
                if authorize_reentry_attempted:
                    raise RuntimeError("登录密码已验证，但 authorize 受控重入后仍未推进")
                authorize_reentry_attempted = True
                log("登录密码已验证但 SPA 未推进，执行一次 authorize 受控重入")
                state = _start_browser_signup_via_authorize(
                    page,
                    email,
                    device_id,
                    log,
                    screen_hint="login",
                )
            continue

        if _is_email_otp(state):
            if otp_committed:
                if authorize_reentry_attempted:
                    raise RuntimeError(
                        "验证码已验证，但 authorize 受控重入后仍停留在验证码页"
                    )
                authorize_reentry_attempted = True
                log("验证码已验证但页面仍停留在 OTP，执行一次 authorize 受控重入")
                state = _start_browser_signup_via_authorize(
                    page,
                    email,
                    device_id,
                    log,
                    screen_hint="login" if login_only else "login_or_signup",
                )
                continue
            if not otp_callback:
                raise RuntimeError(f"ChatGPT {flow_label}需要邮箱验证码但未提供 otp_callback")
            referer = str(
                state.get("current_url")
                or state.get("continue_url")
                or page.url
                or ""
            )

            def _acquire_challenge_code(generation: int, timeout: int) -> str:
                cutoff = otp_sent_at_hint
                if cutoff is None:
                    cutoff = time.time() - 60
                payload = {
                    "action": "acquire",
                    "challenge_id": otp_challenge_id,
                    "generation": generation,
                    "otp_sent_at": cutoff,
                    "timeout": timeout,
                    "phase": "browser_register_email_otp",
                    "phase_label": "any-auto 浏览器注册邮箱验证码",
                    "exclude_codes": sorted(submitted_otp_codes),
                }
                log(
                    f"等待 ChatGPT {flow_label}验证码｜challenge={otp_challenge_id[:8]} "
                    f"generation={generation}"
                )
                callback_value = _invoke_otp_callback(otp_callback, payload)
                code_value, _metadata = _normalize_otp_callback_result(
                    callback_value,
                    challenge_id=otp_challenge_id,
                    generation=generation,
                )
                return code_value

            otp_generation += 1
            code = _acquire_challenge_code(
                otp_generation,
                max(int(otp_wait_timeout or 120), 30),
            )
            if not code:
                raise RuntimeError("未获取到验证码")
            if code in submitted_otp_codes:
                raise RuntimeError("otp_duplicate_code: 同一验证码禁止重复提交")

            while True:
                submitted_otp_codes.add(code)
                otp_resp = _submit_otp_via_page(
                    page,
                    code,
                    log,
                    device_id=device_id,
                    user_agent=user_agent,
                    referer=referer,
                    assume_success_without_state=not login_only,
                )
                otp_status = int(otp_resp.get("status") or 0)
                otp_committed = otp_committed or bool(otp_resp.get("otp_committed"))
                if otp_resp.get("otp_committed") and not otp_resp.get("ok"):
                    if authorize_reentry_attempted:
                        raise RuntimeError("验证码已验证，但 authorize 受控重入后仍未推进")
                    authorize_reentry_attempted = True
                    log("验证码业务请求已成功但 SPA 未推进，执行一次 authorize 受控重入")
                    state = _start_browser_signup_via_authorize(
                        page,
                        email,
                        device_id,
                        log,
                        screen_hint="login" if login_only else "login_or_signup",
                    )
                    break
                if otp_resp.get("ok"):
                    break

                failure_detail = _browser_failure_detail(otp_resp)
                if _terminal_registration_business_error(failure_detail):
                    raise RuntimeError(f"验证码校验失败: {failure_detail}")
                if not (400 <= otp_status < 500) or otp_resend_attempted:
                    raise RuntimeError(f"验证码校验失败: {failure_detail}")

                otp_resend_attempted = True
                resend_started_at = time.time()
                resend_result = _send_browser_email_otp(
                    page,
                    device_id=device_id,
                    user_agent=user_agent,
                    referer=referer,
                    resend=True,
                )
                resend_status = int(resend_result.get("status") or 0)
                if not (resend_result.get("ok") or 200 <= resend_status < 300):
                    raise RuntimeError(
                        "验证码校验失败且重发未成功: "
                        f"validate={failure_detail} resend={_browser_failure_detail(resend_result)}"
                    )
                otp_sent_at_hint = resend_started_at
                otp_generation += 1
                log(
                    "验证码被上游拒绝，已在当前 Context 重发一次并切换新代次｜"
                    f"generation={otp_generation}"
                )
                code = _acquire_challenge_code(
                    otp_generation,
                    max(int(otp_resend_wait_timeout or 90), 30),
                )
                if not code:
                    raise RuntimeError("验证码重发后未获取到新验证码")
                if code in submitted_otp_codes:
                    raise RuntimeError("otp_duplicate_code: 重发后邮箱仍返回已提交验证码")

            if otp_committed and not otp_resp.get("ok"):
                continue
            state = _extract_flow_state(otp_resp.get("data"), otp_resp.get("url", page.url))
            if not state.get("page_type"):
                state = _derive_registration_state_from_page(page)
            log(
                f"[验证码] 验证码已提交｜长度={len(str(code or '').strip())} "
                f"｜HTTP={otp_status or '-'}"
                f"｜业务提交={'是' if otp_resp.get('otp_committed') else '否'}"
                f"｜下一页={state.get('page_type') or '-'}"
            )
            continue

        if _is_about_you(state):
            if login_only:
                raise RuntimeError("失效测活拒绝进入新账号 about_you 阶段")
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
            signup_committed = signup_committed or bool(
                about_resp.get("signup_committed")
            )
            if not about_resp.get("ok"):
                if signup_committed:
                    error_text = str(about_resp.get("text") or "").lower()
                    reason = (
                        "post_signup_duplicate_submission"
                        if "invalid_auth_step" in error_text or "invalid_state" in error_text
                        else "post_signup_auth_api_failure"
                    )
                    log(
                        "开户 2xx 已确认，忽略后续 about_you 失败并转入登录恢复: "
                        f"reason={reason}"
                    )
                    return _committed_signup_partial_state(
                        page,
                        state,
                        otp_committed=otp_committed,
                        reason=reason,
                    )
                raise RuntimeError(
                    "about_you 提交失败: "
                    f"{_browser_failure_detail(about_resp)}"
                )
            state = _extract_flow_state(about_resp.get("data"), about_resp.get("url", page.url))
            if not state.get("page_type"):
                state = _derive_registration_state_from_page(page)
            log(
                f"[注册] about_you 资料已提交｜HTTP={about_resp.get('status', 0) or '-'} "
                f"｜开户提交={'是' if about_resp.get('signup_committed') else '否'}"
                f"｜下一页={state.get('page_type') or '-'}"
            )
            if signup_committed and int(
                about_resp.get("post_commit_response_status") or 0
            ) >= 400:
                log(
                    "开户 2xx 后观察到重复 create_account 失败响应；"
                    "不再信任旧 SPA 路由，直接转入已有账号登录恢复"
                )
                return _committed_signup_partial_state(
                    page,
                    state,
                    otp_committed=otp_committed,
                    reason="post_signup_duplicate_submission",
                )
            if _is_add_phone(state):
                if not phone_callback:
                    state["otp_committed"] = bool(otp_committed)
                    state["signup_committed"] = bool(signup_committed)
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
            if login_only:
                log("已有账号登录进入 add_phone；跳过手机号绑定，直接尝试捕获 Web Session")
                return state
            if not phone_callback:
                state["otp_committed"] = bool(otp_committed)
                state["signup_committed"] = bool(signup_committed)
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
            target_url = _normalize_url(
                str(state.get("continue_url") or state.get("current_url") or ""),
                OPENAI_AUTH,
            )
            if not target_url:
                raise RuntimeError("缺少可跟随的 continue_url")
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                if not signup_committed:
                    raise
                log(
                    "开户 2xx 已确认，后续导航异常不再回滚注册结果；"
                    "转入已有账号登录恢复: "
                    f"{type(exc).__name__}: {str(exc)[:180]}"
                )
                return _committed_signup_partial_state(
                    page,
                    state,
                    otp_committed=otp_committed,
                    reason="post_signup_navigation_failed",
                )
            state = _extract_flow_state(None, page.url)
            continue

        if signup_committed:
            current_url = str(getattr(page, "url", "") or "").lower()
            reason = (
                "post_signup_auth_api_failure"
                if "/error" in current_url
                else "post_signup_state_unresolved"
            )
            log(
                "开户 2xx 已确认但后续页面不可继续；"
                f"转入已有账号登录恢复: reason={reason}"
            )
            return _committed_signup_partial_state(
                page,
                state,
                otp_committed=otp_committed,
                reason=reason,
            )

        raise RuntimeError(f"未支持的注册状态: page={state.get('page_type') or '-'}")

    if signup_committed:
        return _committed_signup_partial_state(
            page,
            state,
            otp_committed=otp_committed,
            reason="post_signup_state_unresolved",
        )
    raise RuntimeError(f"{flow_label}状态机超出最大步数")


class ChatGPTBrowserRegister:
    def __init__(
        self,
        *,
        headless: bool,
        proxy: Optional[str] = None,
        otp_callback: Optional[Callable[..., Any]] = None,
        phone_callback: Optional[Callable[[], str]] = None,
        profile_name: str = "",
        profile_birthdate: str = "",
        stop_check: Optional[Callable[[], None]] = None,
        login_only: bool = False,
        log_fn: Callable[[str], None] = print,
        session_lease: Any = None,
        session_ready_callback: Optional[
            Callable[[dict[str, Any], str], Any]
        ] = None,
        browser_fingerprint: Any = None,
        capacity_managed_externally: bool = False,
        otp_wait_timeout: int = 120,
        otp_resend_wait_timeout: int = 90,
    ):
        self.headless = headless
        self.proxy = proxy
        self.otp_callback = otp_callback
        self.phone_callback = phone_callback
        self.profile_name = str(profile_name or "").strip()
        self.profile_birthdate = str(profile_birthdate or "").strip()
        self.stop_check = stop_check
        self.login_only = bool(login_only)
        self.log = log_fn
        self.session_lease = session_lease
        self.session_ready_callback = session_ready_callback
        self.browser_fingerprint = browser_fingerprint
        self.capacity_managed_externally = bool(capacity_managed_externally)
        self.otp_wait_timeout = max(int(otp_wait_timeout or 120), 30)
        self.otp_resend_wait_timeout = max(
            int(otp_resend_wait_timeout or 90), 30
        )
        # Once Auth has accepted create_account, a later browser/navigation
        # fault must be persisted as a pending result rather than replaying
        # signup in a fresh context.
        self._signup_committed_in_attempt = False
        self._signup_submit_started_in_attempt = False
        self._commit_journal: dict[str, dict[str, Any]] = {}
        self._browser_stop_check = (
            self._checkpoint if self.session_lease is not None else self.stop_check
        )

    def _checkpoint(self) -> None:
        if self.session_lease is not None:
            self.session_lease.check_release_requested()
        if callable(self.stop_check):
            self.stop_check()

    @staticmethod
    def _is_transient_browser_retry_error(exc: BaseException | str) -> bool:
        text = str(exc or "").lower()
        if any(
            marker in text
            for marker in (
                "account_deactivated",
                "account_deleted",
                "incorrect email address or password",
                "密码不正确",
                "密码错误",
                "existing_account",
                "already exists",
                "signup_committed",
                "post_signup",
                "registration_disallowed",
                "identity_provider_mismatch",
            )
        ):
            return False
        return any(
            marker in text
            for marker in (
                "about_you 未找到提交按钮",
                "about_you 未成功填写",
                "密码页未找到输入框",
                "密码页填写失败",
                "targetclosed",
                "target closed",
                "frame was detached",
                "execution context was destroyed",
                "ns_binding_aborted",
                "browser worker 启动或通信失败",
            )
        )

    def run(self, email: str, password: str) -> dict:
        """Run the browser flow with one fresh-context retry for transient UI faults."""

        self._commit_journal = {}
        for attempt in range(2):
            self._signup_committed_in_attempt = False
            self._signup_submit_started_in_attempt = False
            try:
                return self._run_once(email, password)
            except TaskInterruption:
                raise
            except Exception as exc:
                if (
                    attempt
                    or self.session_lease is not None
                    or self._signup_committed_in_attempt
                    or self._signup_submit_started_in_attempt
                    or bool(self._commit_journal)
                    or not self._is_transient_browser_retry_error(exc)
                ):
                    raise
                self._checkpoint()
                self.log(
                    "浏览器页面状态属于临时竞态，释放当前 Context 后重新尝试一次: "
                    f"{type(exc).__name__}: {str(exc)[:180]}"
                )
                time.sleep(0.5)
        raise RuntimeError("浏览器流程重试未返回结果")

    def _run_once(self, email: str, password: str) -> dict:
        """Complete signup or existing-account login and capture one Web Session.

        The browser transport owns only the OpenAI auth flow and the subsequent
        ``chatgpt.com/api/auth/session`` capture. Refresh-token/Codex OAuth is a
        separate mode-owned stage and must not run here.
        """
        if self.session_lease is None and self.capacity_managed_externally:
            self._checkpoint()
            return self._run_browser_session(email, password)

        if self.session_lease is None:
            return run_with_browser_capacity(
                "any_auto_browser_registration",
                lambda: self._run_browser_session(email, password),
                logger=self.log,
                stop_check=self._browser_stop_check,
                priority="normal" if self.login_only else "registration",
                shared_camoufox_headless=self.headless,
            )

        self.session_lease.transition("waiting_capacity")
        try:
            result = run_with_persistent_browser_capacity(
                "web_session_login_hold",
                lambda: self._run_browser_session(email, password),
                logger=self.log,
                stop_check=self._browser_stop_check,
                shared_camoufox_headless=self.headless,
            )
        except TaskInterruption as exc:
            if isinstance(exc, WebSessionLeaseReleaseRequested):
                self.session_lease.transition("released")
                raise
            if str(getattr(self.session_lease, "status", "")) not in {
                "stopped",
                "released",
                "failed",
                "interrupted",
            }:
                self.session_lease.transition("stopped")
            raise
        except Exception as exc:
            if (
                getattr(self.session_lease, "ready_at", "")
                and str(getattr(self.session_lease, "status", ""))
                not in {"stopped", "released", "failed", "interrupted"}
            ):
                self.session_lease.transition("interrupted", error=exc)
            raise
        if str(getattr(self.session_lease, "status", "")) == "releasing":
            self.session_lease.transition("released")
        return result

    def _run_browser_session(self, email: str, password: str) -> dict:
        global _DIAGNOSTIC_VIDEO_UNSUPPORTED

        self._signup_committed_in_attempt = False
        self._signup_submit_started_in_attempt = False

        if self.session_lease is not None:
            self.session_lease.transition("authenticating")
            self._checkpoint()
        with ExitStack() as browser_cleanup, ExitStack() as trace_cleanup:
            from services.chatgpt_core.registration_diagnostics import (
                current_registration_diagnostic_session,
            )

            diagnostic_session = current_registration_diagnostic_session()
            lease_context_options: dict[str, Any] = {}
            if self.session_lease is not None:
                lease_context_options.update(
                    self.session_lease.browser_context_options()
                )
            diagnostic_enabled = bool(
                diagnostic_session is not None and diagnostic_session.enabled
            )

            def _record_diagnostic_failure(event: str, exc: Exception) -> None:
                if diagnostic_session is None:
                    return
                error_text = f"{type(exc).__name__}: {exc}"
                try:
                    diagnostic_session.note_warning(f"{event}:{error_text}"[:1000])
                except Exception:
                    pass
                try:
                    diagnostic_session.record_event(
                        "diagnostic",
                        event,
                        {"error": error_text},
                    )
                except Exception:
                    pass

            diagnostic_context_active = False
            diagnostic_options: dict[str, Any] = {}
            if diagnostic_enabled:
                try:
                    diagnostic_options.update(
                        diagnostic_session.browser_context_options() or {}
                    )
                except Exception as exc:
                    _record_diagnostic_failure(
                        "browser_diagnostic_context_options_failed",
                        exc,
                    )
                    diagnostic_enabled = False

            with _DIAGNOSTIC_VIDEO_CAPABILITY_LOCK:
                video_known_unsupported = _DIAGNOSTIC_VIDEO_UNSUPPORTED
            if "record_video_dir" in diagnostic_options and video_known_unsupported:
                diagnostic_options.pop("record_video_dir", None)
                try:
                    diagnostic_session.mark_video_capture_unavailable(
                        "Camoufox runtime does not support Browser.setScreencastOptions"
                    )
                except Exception:
                    pass

            def _enter_registration_context(extra_options: dict[str, Any]):
                return browser_cleanup.enter_context(
                    shared_camoufox_registration_session(
                        headless=self.headless,
                        proxy=self.proxy,
                        extra_context_options=extra_options,
                        browser_fingerprint=self.browser_fingerprint,
                        logger=self.log,
                    )
                )

            session = None
            if diagnostic_enabled:
                combined_options = dict(diagnostic_options)
                combined_options.update(lease_context_options)
                video_attempted = "record_video_dir" in combined_options
                try:
                    session = _enter_registration_context(dict(combined_options))
                    diagnostic_context_active = True
                except Exception as exc:
                    error_text = f"{type(exc).__name__}: {exc}".lower()
                    video_capability_unsupported = bool(
                        video_attempted
                        and "setscreencastoptions" in error_text
                        and "not supported" in error_text
                    )
                    if video_capability_unsupported:
                        with _DIAGNOSTIC_VIDEO_CAPABILITY_LOCK:
                            _DIAGNOSTIC_VIDEO_UNSUPPORTED = True
                        try:
                            diagnostic_session.mark_video_capture_unavailable(
                                f"{type(exc).__name__}: {exc}"
                            )
                        except Exception:
                            pass
                        retry_options = dict(combined_options)
                        retry_options.pop("record_video_dir", None)
                        try:
                            session = _enter_registration_context(retry_options)
                            diagnostic_context_active = True
                        except Exception as retry_exc:
                            _record_diagnostic_failure(
                                "browser_diagnostic_context_setup_failed",
                                retry_exc,
                            )
                    else:
                        _record_diagnostic_failure(
                            "browser_diagnostic_context_setup_failed",
                            exc,
                        )

            if session is None:
                session = _enter_registration_context(lease_context_options)

            session_fingerprint = getattr(session, "browser_fingerprint", None)
            if not self.browser_fingerprint and session_fingerprint:
                self.browser_fingerprint = dict(session_fingerprint)

            browser = session.browser
            context = session.context
            page = session.page
            if self.session_lease is not None:
                self.session_lease.seed_browser_context(context)

            if diagnostic_context_active:
                # ExitStack is unwound in reverse order: capture the final DOM
                # and stop Trace first, then close to flush HAR/video.
                def _close_diagnostic_context() -> None:
                    try:
                        context.close()
                    except Exception as exc:
                        _record_diagnostic_failure(
                            "browser_context_close_failed",
                            exc,
                        )

                trace_cleanup.callback(_close_diagnostic_context)
                try:
                    diagnostic_session.start_browser_capture(context, page)
                except Exception as exc:
                    _record_diagnostic_failure(
                        "browser_capture_start_failed",
                        exc,
                    )

                def _stop_diagnostic_capture() -> None:
                    try:
                        diagnostic_session.stop_browser_capture(page, context)
                    except Exception as exc:
                        _record_diagnostic_failure(
                            "browser_capture_stop_failed",
                            exc,
                        )

                trace_cleanup.callback(_stop_diagnostic_capture)
            else:
                trace_cleanup.callback(context.close)
            if self.session_lease is not None:
                trace_cleanup.callback(
                    lambda: (
                        self.session_lease.checkpoint_profile(context)
                        if bool(getattr(self.session_lease, "release_requested", False))
                        else None
                    )
                )
            pending_requests: dict[int, tuple[float, str, str, int]] = {}

            def _trace_allowed(url: str, resource_type: str = "") -> bool:
                try:
                    host = str(urlparse(str(url or "")).hostname or "").lower()
                except Exception:
                    host = ""
                if host not in {
                    "auth.openai.com",
                    "chatgpt.com",
                    "platform.openai.com",
                    "sentinel.openai.com",
                    "cloudflare.com",
                } and not host.endswith(".openai.com"):
                    return False
                kind = str(resource_type or "").lower()
                lowered_url = str(url or "").lower()
                return kind in {"document", "xhr", "fetch", "websocket"} or "/api/" in lowered_url

            def _page_hint(url: str) -> str:
                lowered = str(url or "").lower()
                if "email-otp" in lowered or "email-verification" in lowered:
                    return "email_otp"
                if "create-account/password" in lowered or "user/register" in lowered:
                    return "password"
                if "about-you" in lowered:
                    return "about_you"
                if "/api/auth/session" in lowered:
                    return "chatgpt_session"
                if "authorize" in lowered or "signin/openai" in lowered:
                    return "authorize"
                return ""

            def _request_size(request) -> int:
                try:
                    payload = request.post_data
                    if payload:
                        return len(str(payload).encode("utf-8", errors="replace"))
                except Exception:
                    pass
                return 0

            def _on_request(request) -> None:
                url = str(getattr(request, "url", "") or "")
                resource_type = str(getattr(request, "resource_type", "") or "")
                if "/api/accounts/create_account" in url.lower():
                    # This is the irreversible Auth create-account boundary.
                    # A later TargetClosed/navigation error must not replay it.
                    self._signup_submit_started_in_attempt = True
                if not _trace_allowed(url, resource_type):
                    return
                if len(pending_requests) >= 2048:
                    pending_requests.pop(next(iter(pending_requests)), None)
                pending_requests[id(request)] = (
                    time.monotonic(),
                    str(getattr(request, "method", "GET") or "GET"),
                    resource_type,
                    _request_size(request),
                )

            def _on_response(response) -> None:
                try:
                    request = response.request
                    url = str(getattr(response, "url", "") or getattr(request, "url", "") or "")
                    status = int(getattr(response, "status", 0) or 0)
                    lowered_url = url.lower()
                    commit_stage = ""
                    if "/api/accounts/user/register" in lowered_url:
                        commit_stage = "register"
                    elif "/api/accounts/email-otp/validate" in lowered_url:
                        commit_stage = "email_otp"
                    elif "/api/accounts/create_account" in lowered_url:
                        commit_stage = "create_account"
                    if commit_stage and 200 <= status < 300:
                        self._commit_journal.setdefault(
                            commit_stage,
                            {
                                "status": status,
                                "url": url[:500],
                                "committed_at": time.time(),
                            },
                        )
                        if commit_stage == "create_account":
                            self._signup_committed_in_attempt = True
                    resource_type = str(getattr(request, "resource_type", "") or "")
                    if not _trace_allowed(url, resource_type):
                        return
                    started, method, stored_type, request_bytes = pending_requests.pop(
                        id(request),
                        (time.monotonic(), str(getattr(request, "method", "GET") or "GET"), resource_type, _request_size(request)),
                    )
                    headers = getattr(response, "headers", {}) or {}
                    try:
                        response_bytes = int(headers.get("content-length") or 0)
                    except (TypeError, ValueError):
                        response_bytes = 0
                    self.log(
                        format_http_trace_log(
                            method,
                            url,
                            status=status,
                            duration_ms=round((time.monotonic() - started) * 1000),
                            page=_page_hint(url),
                            resource_type=stored_type or resource_type,
                            request_bytes=request_bytes,
                            response_bytes=response_bytes,
                        )
                    )
                except Exception:
                    return

            def _on_request_failed(request) -> None:
                try:
                    url = str(getattr(request, "url", "") or "")
                    resource_type = str(getattr(request, "resource_type", "") or "")
                    if not _trace_allowed(url, resource_type):
                        return
                    started, method, stored_type, request_bytes = pending_requests.pop(
                        id(request),
                        (time.monotonic(), str(getattr(request, "method", "GET") or "GET"), resource_type, _request_size(request)),
                    )
                    self.log(
                        format_http_trace_log(
                            method,
                            url,
                            status="FAILED",
                            duration_ms=round((time.monotonic() - started) * 1000),
                            page=_page_hint(url),
                            resource_type=stored_type or resource_type,
                            request_bytes=request_bytes,
                            error=str(getattr(request, "failure", "") or "network error"),
                        )
                    )
                except Exception:
                    return

            installed_listeners: list[tuple[str, object]] = []
            for event, listener in (
                ("request", _on_request),
                ("response", _on_response),
                ("requestfailed", _on_request_failed),
            ):
                try:
                    page.on(event, listener)
                    installed_listeners.append((event, listener))
                except Exception:
                    continue

            def _cleanup_page_trace() -> None:
                for event, listener in installed_listeners:
                    try:
                        page.remove_listener(event, listener)
                    except Exception:
                        pass
                installed_listeners.clear()
                pending_requests.clear()

            trace_cleanup.callback(_cleanup_page_trace)
            self.log(
                "[登录态] 登录浏览器上下文已启动"
                if self.login_only
                else "[注册] 浏览器上下文已启动"
            )
            from services.chatgpt_core.browser_registration import (
                _normalize_browser_web_session,
                _wait_for_web_session,
            )

            restored_session = False
            if self.session_lease is not None and bool(
                getattr(self.session_lease, "restored_profile", False)
            ):
                self.log("[执行登录态] 正在注入已保存浏览器状态并验证现有 Session")
                try:
                    page.goto(
                        f"{CHATGPT_APP}/",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    restored_cookies = list(page.context.cookies() or [])
                    restored_device_id = next(
                        (
                            str(item.get("value") or "").strip()
                            for item in restored_cookies
                            if str(item.get("name") or "").strip() == "oai-did"
                        ),
                        "",
                    )
                    restored_data = _wait_for_web_session(
                        page,
                        timeout=12,
                        log=self.log,
                        email=email,
                        device_id=restored_device_id,
                        stop_check=self._browser_stop_check,
                    )
                    restored_payload = _normalize_browser_web_session(
                        restored_data,
                        list(page.context.cookies() or []),
                    )
                    restored_session = bool(
                        str(restored_payload.get("access_token") or "").strip()
                        and str(restored_payload.get("session_token") or "").strip()
                        and str(
                            restored_payload.get("cookie_header")
                            or restored_payload.get("cookies")
                            or ""
                        ).strip()
                    )
                    self.log(
                        "[执行登录态] 已保存浏览器状态仍有效，跳过密码与 OTP 登录"
                        if restored_session
                        else "[执行登录态] 已保存浏览器状态无效，继续正常密码/OTP 登录"
                    )
                except TaskInterruption:
                    raise
                except Exception as exc:
                    self.log(
                        "[执行登录态] 已保存浏览器状态验证失败，继续正常登录: "
                        f"{type(exc).__name__}: {str(exc)[:180]}"
                    )

            if restored_session:
                final_state = {
                    "page_type": "chatgpt_home",
                    "current_url": str(page.url or CHATGPT_APP),
                    "restored_profile": True,
                    "session_capture_pending": False,
                }
            else:
                final_state = _browser_registration_flow(
                    page,
                    email,
                    password,
                    self.otp_callback,
                    self.phone_callback,
                    self.log,
                    profile_name=self.profile_name,
                    profile_birthdate=self.profile_birthdate,
                    stop_check=self._browser_stop_check,
                    login_only=self.login_only,
                    otp_wait_timeout=self.otp_wait_timeout,
                    otp_resend_wait_timeout=self.otp_resend_wait_timeout,
                )
            self._signup_committed_in_attempt = bool(
                final_state.get("signup_committed")
            )
            self.log(
                f"{'登录态' if self.login_only else '注册'}流程完成: "
                f"page={final_state.get('page_type') or '-'}"
            )

            # The OpenAI auth callback may still be on platform.openai.com.
            # Reuse the project-owned bridge to establish ChatGPT next-auth.
            _follow_signup_callback(page, final_state, self.log)

            def _capture_web_session(timeout: int) -> tuple[list[dict], dict]:
                cookie_snapshot = list(page.context.cookies() or [])
                current_device_id = next(
                    (
                        str(item.get("value") or "").strip()
                        for item in cookie_snapshot
                        if str(item.get("name") or "").strip() == "oai-did"
                    ),
                    "",
                )
                session_data = _wait_for_web_session(
                    page,
                    timeout=timeout,
                    log=self.log,
                    email=email,
                    device_id=current_device_id,
                    stop_check=self._browser_stop_check,
                )
                cookie_snapshot = list(page.context.cookies() or [])
                return cookie_snapshot, _normalize_browser_web_session(
                    session_data,
                    cookie_snapshot,
                )

            self.log("开始抓取 ChatGPT Web Session: https://chatgpt.com/api/auth/session")
            initial_capture_timeout = (
                10 if final_state.get("session_capture_pending") else 55
            )
            try:
                cookie_items, web_session = _capture_web_session(
                    initial_capture_timeout
                )
            except TaskInterruption:
                raise
            except Exception as exc:
                if self.login_only or not bool(final_state.get("signup_committed")):
                    raise
                self.log(
                    "开户已确认但首次 Web Session 抓取异常，继续已有账号登录恢复: "
                    f"{type(exc).__name__}: {str(exc)[:180]}"
                )
                try:
                    cookie_items = list(page.context.cookies() or [])
                except Exception:
                    cookie_items = []
                web_session = {}
                final_state = {
                    **final_state,
                    "session_capture_pending": True,
                    "session_capture_pending_reason": (
                        "post_signup_session_capture_failed"
                    ),
                }

            def _web_session_complete(payload: dict) -> bool:
                return bool(
                    str(payload.get("access_token") or "").strip()
                    and str(payload.get("session_token") or "").strip()
                    and str(
                        payload.get("cookie_header") or payload.get("cookies") or ""
                    ).strip()
                )

            if (
                not self.login_only
                and bool(final_state.get("signup_committed"))
                and not _web_session_complete(web_session)
            ):
                self.log(
                    "开户业务请求已确认但 Web Session 尚未就绪，"
                    "在同一浏览器上下文执行一次已有账号登录恢复"
                )
                try:
                    recovered_state = _browser_registration_flow(
                        page,
                        email,
                        password,
                        self.otp_callback,
                        None,
                        self.log,
                        profile_name=self.profile_name,
                        profile_birthdate=self.profile_birthdate,
                        stop_check=self._browser_stop_check,
                        login_only=True,
                    )
                    _follow_signup_callback(page, recovered_state, self.log)
                    final_state = {
                        **recovered_state,
                        "otp_committed": bool(final_state.get("otp_committed")),
                        "signup_committed": True,
                        "signup_recovery": "existing_account_login",
                        "session_capture_pending": False,
                        "post_signup_failure_code": str(
                            final_state.get("post_signup_failure_code") or ""
                        ),
                    }
                    self._signup_committed_in_attempt = True
                    cookie_items, web_session = _capture_web_session(55)
                except TaskInterruption:
                    raise
                except Exception as exc:
                    self.log(f"开户后已有账号登录恢复失败: {str(exc)[:300]}")
                    final_state = {
                        **final_state,
                        "signup_committed": True,
                        "signup_recovery": "existing_account_login_failed",
                        "session_capture_pending": True,
                        "session_capture_pending_reason": (
                            "post_signup_existing_account_login_failed"
                        ),
                    }

            access_token = str(web_session.get("access_token") or "").strip()
            session_token = str(web_session.get("session_token") or "").strip()
            cookie_header = str(
                web_session.get("cookie_header") or web_session.get("cookies") or ""
            ).strip()
            account_id = str(
                web_session.get("account_id")
                or next(
                    (
                        str(item.get("value") or "").strip()
                        for item in cookie_items
                        if str(item.get("name") or "").strip() == "_account"
                    ),
                    "",
                )
                or (final_state or {}).get("account_id")
                or ""
            ).strip()
            browser_fingerprint = _capture_browser_fingerprint(
                page,
                cookie_items,
                self.browser_fingerprint,
            )
            if not access_token or not session_token or not cookie_header:
                if not self.login_only and bool(final_state.get("signup_committed")):
                    pending_reason = str(
                        final_state.get("session_capture_pending_reason")
                        or (
                            "post_signup_session_capture_incomplete"
                            if final_state.get("signup_recovery")
                            == "existing_account_login"
                            else ""
                        )
                        or final_state.get("post_signup_failure_code")
                        or "post_signup_session_capture_incomplete"
                    ).strip()
                    self.log(
                        "开户已确认但 Web Session 仍不完整；"
                        "保存 session_capture_pending 账号，禁止重复 signup: "
                        f"reason={pending_reason}"
                    )
                    return {
                        "success": True,
                        "email": email,
                        "password": password,
                        "account_id": account_id,
                        "access_token": access_token,
                        "refresh_token": "",
                        "id_token": access_token,
                        "session_token": session_token,
                        "workspace_id": str(web_session.get("workspace_id") or account_id),
                        "cookies": cookie_items,
                        "cookie_header": cookie_header,
                        "metadata": {
                            "registration_stage_complete": True,
                            "registration_session_capture": "pending",
                            "registration_page_type": str(final_state.get("page_type") or ""),
                            "registration_otp_committed": bool(
                                final_state.get("otp_committed")
                            ),
                            "registration_signup_committed": True,
                            "registration_signup_recovery": str(
                                final_state.get("signup_recovery") or ""
                            ),
                            "registration_post_signup_failure_code": str(
                                final_state.get("post_signup_failure_code") or ""
                            ),
                            "registered_auth_pending": True,
                            "session_capture_pending": True,
                            "session_capture_pending_reason": pending_reason,
                            "login_only": False,
                            "web_session_capture_mode": "pending_existing_account_recovery",
                            "web_session_browser_fingerprint": browser_fingerprint,
                            "web_session_expires_at": str(web_session.get("expires") or ""),
                            "web_session_expiry_source": "web_session_expires",
                        },
                        "source": "registered_auth_pending",
                    }
                raise RuntimeError(
                    "ChatGPT Web Session 材料不完整: "
                    f"AT状态={'存在' if access_token else '缺失'}｜"
                    f"Session状态={'存在' if session_token else '缺失'}｜"
                    f"Cookie状态={'存在' if cookie_header else '缺失'}"
                )
            self.log(
                "ChatGPT Web Session 获取成功: "
                f"access_token=yes session_token=yes cookies=yes account_id={account_id or '-'}"
            )
            result_payload = {
                "success": True,
                "email": email,
                "password": password,
                "account_id": account_id,
                "access_token": access_token,
                "refresh_token": "",
                "id_token": access_token,
                "session_token": session_token,
                "workspace_id": str(web_session.get("workspace_id") or account_id),
                "cookies": cookie_items,
                "cookie_header": cookie_header,
                "metadata": {
                    "registration_stage_complete": not self.login_only,
                    "registration_session_capture": "chatgpt_api_auth_session",
                    "registration_page_type": str(final_state.get("page_type") or ""),
                    "registration_page_url": str(page.url or ""),
                    "registration_otp_committed": bool(
                        final_state.get("otp_committed")
                    ),
                    "registration_signup_committed": bool(
                        final_state.get("signup_committed")
                    ),
                    "registration_signup_recovery": str(
                        final_state.get("signup_recovery") or ""
                    ),
                    "registration_post_signup_failure_code": str(
                        final_state.get("post_signup_failure_code") or ""
                    ),
                    "session_capture_pending": False,
                    "login_only": self.login_only,
                    "web_session_capture_mode": (
                        "existing_account_login" if self.login_only else "signup"
                    ),
                    "web_session_browser_fingerprint": browser_fingerprint,
                    "web_session_expires_at": str(web_session.get("expires") or ""),
                    "web_session_expiry_source": "web_session_expires",
                },
                "source": "any_auto_browser_web_session",
            }
            if self.session_lease is not None:
                def _publish_session_material(
                    payload: dict[str, Any],
                    reason: str,
                ) -> dict[str, Any]:
                    if not callable(self.session_ready_callback):
                        return {}
                    published = self.session_ready_callback(payload, reason)
                    return dict(published or {}) if isinstance(published, dict) else {}

                def _refresh_held_session_payload() -> dict[str, Any]:
                    refreshed_cookies, refreshed_session = _capture_web_session(20)
                    refreshed_access_token = str(
                        refreshed_session.get("access_token") or ""
                    ).strip()
                    refreshed_session_token = str(
                        refreshed_session.get("session_token") or ""
                    ).strip()
                    refreshed_cookie_header = str(
                        refreshed_session.get("cookie_header")
                        or refreshed_session.get("cookies")
                        or ""
                    ).strip()
                    if not (
                        refreshed_access_token
                        and refreshed_session_token
                        and refreshed_cookie_header
                    ):
                        raise RuntimeError("保持中的浏览器未返回完整 ChatGPT Web Session")
                    refreshed_account_id = str(
                        refreshed_session.get("account_id") or account_id
                    ).strip()
                    refreshed_fingerprint = _capture_browser_fingerprint(
                        page,
                        refreshed_cookies,
                        self.browser_fingerprint,
                    )
                    return {
                        **result_payload,
                        "account_id": refreshed_account_id,
                        "workspace_id": str(
                            refreshed_session.get("workspace_id")
                            or refreshed_account_id
                        ),
                        "access_token": refreshed_access_token,
                        "id_token": refreshed_access_token,
                        "session_token": refreshed_session_token,
                        "cookies": refreshed_cookies,
                        "cookie_header": refreshed_cookie_header,
                        "metadata": {
                            **dict(result_payload.get("metadata") or {}),
                            "registration_page_url": str(page.url or ""),
                            "web_session_capture_mode": "held_session_refresh",
                            "web_session_browser_fingerprint": refreshed_fingerprint,
                            "web_session_expires_at": str(refreshed_session.get("expires") or ""),
                            "web_session_expiry_source": "web_session_expires",
                        },
                    }

                self.session_lease.hold_browser(
                    page=page,
                    context=page.context,
                    initial_payload=result_payload,
                    on_session_material=_publish_session_material,
                    refresh_payload=_refresh_held_session_payload,
                    log=self.log,
                    stop_check=self._browser_stop_check,
                )
            return result_payload

    def _retry_oauth_fresh_browser(self, email, password):
        """Run Codex OAuth in a fresh incognito context after add-phone."""
        try:
            with ExitStack() as stack:
                session = stack.enter_context(
                    shared_camoufox_registration_session(
                        headless=self.headless,
                        proxy=self.proxy,
                        browser_fingerprint=self.browser_fingerprint,
                        logger=self.log,
                    )
                )
                page = session.page
                self.log("  全新无痕上下文 OAuth 开始...")
                result = _do_codex_oauth(
                    page, {}, email, password,
                    self.otp_callback, self.phone_callback, self.proxy, self.log,
                )
                return result
        except Exception as e:
            self.log(f"  全新无痕上下文 OAuth 异常: {e}")
            return None
