"""
Sentinel Token 生成器模块（纯 Python 方案）。

协议路径对齐 any-auto-register：
- PoW 用纯 Python FNV
- turnstile ``t`` 用 sentinel_vm 解 dx（不再固定空串）
"""

import base64
import json
import random
import time
import uuid

from .sentinel_constants import (
    DEFAULT_SENTINEL_SDK_URL,
    PINNED_CHROMIUM_USER_AGENT,
)
from .browser_identity import browser_fingerprint_to_dict, infer_browser_family
from .utils import coerce_browser_fingerprint


SENTINEL_REQ_URL = "https://sentinel.openai.com/backend-api/sentinel/req"
SENTINEL_REFERER = "https://sentinel.openai.com/backend-api/sentinel/frame.html"


class SentinelTokenGenerator:
    """
    Sentinel Token 纯 Python 生成器。

    说明：
    - 该实现不依赖 Node / JS。
    - t 字段按当前纯 Python 方案固定空串，由上游接口判定可用性。
    """

    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id=None, user_agent=None, browser_fingerprint=None):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent or PINNED_CHROMIUM_USER_AGENT
        self.requirements_seed = str(random.random())
        self.sid = str(uuid.uuid4())
        self.browser_fingerprint = (
            coerce_browser_fingerprint(browser_fingerprint)
            if browser_fingerprint is not None
            else None
        )

    @staticmethod
    def _fnv1a_32(text):
        """
        FNV-1a 32位哈希算法（从 SDK JS 逆向还原）

        逆向来源：SDK 中的匿名函数，特征码：
          e = 2166136261  (FNV offset basis)
          e ^= t.charCodeAt(r)
          e = Math.imul(e, 16777619) >>> 0  (FNV prime)

        最后做 xorshift 混合（murmurhash3 风格的 finalizer）：
          e ^= e >>> 16
          e = Math.imul(e, 2246822507) >>> 0
          e ^= e >>> 13
          e = Math.imul(e, 3266489909) >>> 0
          e ^= e >>> 16
        """
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self):
        """
        构造浏览器环境数据数组（完整仿真 PoW 参数）。

        SDK 中的元素对应关系（按索引）：
          [0]  screen.width + screen.height
          [1]  new Date().toString()
          [2]  performance.memory.jsHeapSizeLimit
          [3]  Math.random()（后被 nonce 覆盖）
          [4]  navigator.userAgent
          [5]  随机 script src
          [6]  脚本版本匹配
          [7]  document.documentElement.data-build
          [8]  navigator.language
          [9]  navigator.languages.join(',')（后被耗时覆盖）
          [10] Math.random()
          [11] 随机 navigator 属性
          [12] Object.keys(document) 随机一个
          [13] Object.keys(window) 随机一个
          [14] performance.now()
          [15] self.sid
          [16] URLSearchParams 参数
          [17] navigator.hardwareConcurrency
          [18] performance.timeOrigin
        """
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        fingerprint = self.browser_fingerprint
        screen_info = (
            f"{fingerprint.screen_width}x{fingerprint.screen_height}"
            if fingerprint is not None
            else "1920x1080"
        )
        try:
            zone = ZoneInfo(
                str(getattr(fingerprint, "timezone", "") or "UTC")
            )
        except Exception:
            zone = timezone.utc
        now = datetime.now(zone)
        zone_label = now.tzname() or "Coordinated Universal Time"
        date_str = now.strftime("%a %b %d %Y %H:%M:%S GMT%z") + f" ({zone_label})"
        js_heap_limit = (
            4294705152
            if fingerprint is None or fingerprint.browser_family == "chrome"
            else None
        )
        nav_random1 = random.random()
        ua = self.user_agent
        script_src = DEFAULT_SENTINEL_SDK_URL
        script_version = None
        data_build = None
        language = fingerprint.locale if fingerprint is not None else "en-US"
        languages = (
            ",".join(fingerprint.languages)
            if fingerprint is not None and fingerprint.languages
            else "en-US,en"
        )
        nav_random2 = random.random()
        nav_props = [
            "vendorSub",
            "productSub",
            "vendor",
            "maxTouchPoints",
            "scheduling",
            "userActivation",
            "doNotTrack",
            "geolocation",
            "connection",
            "plugins",
            "mimeTypes",
            "pdfViewerEnabled",
            "webkitTemporaryStorage",
            "webkitPersistentStorage",
            "hardwareConcurrency",
            "cookieEnabled",
            "credentials",
            "mediaDevices",
            "permissions",
            "locks",
            "ink",
        ]
        nav_prop = random.choice(nav_props)
        nav_val = f"{nav_prop}−undefined"
        doc_key = random.choice(
            ["location", "implementation", "URL", "documentURI", "compatMode"]
        )
        win_key = random.choice(
            ["Object", "Function", "Array", "Number", "parseFloat", "undefined"]
        )
        perf_now = random.uniform(1000, 50000)
        hardware_concurrency = (
            int(fingerprint.hardware_concurrency)
            if fingerprint is not None
            else random.choice([4, 8, 12, 16])
        )
        time_origin = time.time() * 1000 - perf_now

        return [
            screen_info,
            date_str,
            js_heap_limit,
            nav_random1,
            ua,
            script_src,
            script_version,
            data_build,
            language,
            languages,
            nav_random2,
            nav_val,
            doc_key,
            win_key,
            perf_now,
            self.sid,
            "",
            hardware_concurrency,
            time_origin,
        ]

    @staticmethod
    def _base64_encode(data):
        """
        模拟 SDK 的 E() 函数：JSON.stringify → TextEncoder.encode → btoa
        """
        json_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        encoded = json_str.encode("utf-8")
        return base64.b64encode(encoded).decode("ascii")

    def _run_check(self, start_time, seed, difficulty, config, nonce):
        """
        单次 PoW 检查（_runCheck 方法逆向还原）

        参数:
            start_time: 起始时间（秒）
            seed: PoW 种子字符串
            difficulty: 难度字符串（hex 前缀阈值）
            config: 环境配置数组
            nonce: 当前尝试序号

        返回:
            成功时返回 base64(config) + "~S"
            失败时返回 None
        """
        config[3] = nonce
        config[9] = round((time.time() - start_time) * 1000)
        encoded = self._base64_encode(config)
        digest = self._fnv1a_32(seed + encoded)
        if digest[: len(difficulty)] <= difficulty:
            return encoded + "~S"
        return None

    def generate_token(self, seed=None, difficulty=None):
        """
        生成 sentinel token（完整 PoW 流程）

        参数:
            seed: PoW 种子（来自服务端的 proofofwork.seed）
            difficulty: 难度值（来自服务端的 proofofwork.difficulty）

        返回:
            格式为 "gAAAAAB..." 的 sentinel token 字符串
        """
        if seed is None:
            seed = self.requirements_seed
            difficulty = difficulty or "0"
        if difficulty is None or difficulty == "":
            difficulty = "0"
        difficulty = str(difficulty)
        start_time = time.time()
        config = self._get_config()
        for nonce in range(self.MAX_ATTEMPTS):
            value = self._run_check(start_time, seed, difficulty, config, nonce)
            if value:
                return "gAAAAAB" + value
        return "gAAAAAB" + self.ERROR_PREFIX + self._base64_encode(str(None))

    def generate_requirements_token(self):
        config = self._get_config()
        config[3] = 1
        config[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._base64_encode(config)


def fetch_sentinel_challenge(
    session,
    device_id,
    flow="authorize_continue",
    user_agent=None,
    sec_ch_ua=None,
    impersonate=None,
    request_p=None,
    browser_fingerprint=None,
):
    """请求 sentinel/req。返回 (challenge_dict, request_p) 或 (None, request_p)。"""
    generator = SentinelTokenGenerator(
        device_id=device_id,
        user_agent=user_agent,
        browser_fingerprint=browser_fingerprint,
    )
    sent_p = str(request_p or "").strip() or generator.generate_requirements_token()
    req_body = {
        "p": sent_p,
        "id": device_id,
        "flow": flow,
    }
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Referer": SENTINEL_REFERER,
        "Origin": "https://sentinel.openai.com",
        "User-Agent": user_agent or "Mozilla/5.0",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if infer_browser_family(user_agent, impersonate) == "chrome":
        headers.update(
            {
                "sec-ch-ua": sec_ch_ua
                or '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": (
                    '"macOS"' if "Macintosh" in str(user_agent or "") else '"Windows"'
                ),
            }
        )
    kwargs = {"data": json.dumps(req_body), "headers": headers, "timeout": 20}
    if impersonate:
        kwargs["impersonate"] = impersonate
    try:
        response = session.post(SENTINEL_REQ_URL, **kwargs)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict):
                return data, sent_p
    except Exception:
        return None, sent_p
    return None, sent_p


def _solve_turnstile_t(
    *,
    challenge: dict,
    request_p: str,
    user_agent: str | None,
    browser_fingerprint=None,
) -> str:
    """对齐 any-auto：用请求时的 requirements p 作为 dx 的 XOR key。"""
    turnstile = challenge.get("turnstile") or {}
    dx_b64 = str(turnstile.get("dx") or "").strip()
    if not dx_b64 or not str(request_p or "").strip():
        return ""
    try:
        from .sentinel_vm import solve_turnstile_dx

        return str(
            solve_turnstile_dx(
                dx_b64,
                str(request_p),
                user_agent=user_agent or "",
                sdk_url=DEFAULT_SENTINEL_SDK_URL,
                browser_fingerprint=browser_fingerprint_to_dict(browser_fingerprint),
            )
            or ""
        )
    except Exception:
        return ""


def _build_sentinel_token_python(
    session,
    device_id,
    *,
    flow="authorize_continue",
    user_agent=None,
    sec_ch_ua=None,
    impersonate=None,
    browser_fingerprint=None,
):
    challenge, request_p = fetch_sentinel_challenge(
        session,
        device_id,
        flow=flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        impersonate=impersonate,
        browser_fingerprint=browser_fingerprint,
    )
    if not challenge:
        return None

    c_value = str(challenge.get("token") or "").strip()
    if not c_value:
        return None

    generator = SentinelTokenGenerator(
        device_id=device_id,
        user_agent=user_agent,
        browser_fingerprint=browser_fingerprint,
    )
    pow_data = challenge.get("proofofwork") or {}
    # any-auto: turnstile 用初始 requirements p；PoW 成功后 p 换成 gAAAAAB...
    if pow_data.get("required") and pow_data.get("seed"):
        p_value = generator.generate_token(
            seed=pow_data.get("seed"),
            difficulty=pow_data.get("difficulty", "0"),
        )
    else:
        p_value = request_p or generator.generate_requirements_token()

    t_value = _solve_turnstile_t(
        challenge=challenge,
        request_p=request_p,
        user_agent=user_agent,
        browser_fingerprint=browser_fingerprint,
    )

    return json.dumps(
        {
            "p": p_value,
            "t": t_value,
            "c": c_value,
            "id": device_id,
            "flow": flow,
        },
        separators=(",", ":"),
    )


def build_sentinel_token(
    session,
    device_id,
    flow="authorize_continue",
    user_agent=None,
    sec_ch_ua=None,
    impersonate=None,
    browser_fingerprint=None,
):
    """默认 Sentinel token 构造：PoW + 可选 turnstile VM（any-auto 对齐）。"""
    return _build_sentinel_token_python(
        session,
        device_id,
        flow=flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        impersonate=impersonate,
        browser_fingerprint=browser_fingerprint,
    )


def build_sentinel_token_vm_only(
    session,
    device_id,
    flow="authorize_continue",
    user_agent=None,
    sec_ch_ua=None,
    impersonate=None,
    browser_fingerprint=None,
):
    """
    与 build_sentinel_token 相同：协议路径统一走 PoW + VM turnstile。
    """
    return _build_sentinel_token_python(
        session,
        device_id,
        flow=flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        impersonate=impersonate,
        browser_fingerprint=browser_fingerprint,
    )
