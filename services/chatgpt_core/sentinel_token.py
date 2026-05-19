"""
Sentinel Token 生成器模块（纯 Python 方案）。
"""

import base64
import json
import random
import time
import uuid


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

    def __init__(self, device_id=None, user_agent=None):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        )
        self.requirements_seed = str(random.random())
        self.sid = str(uuid.uuid4())

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

        screen_info = "1920x1080"
        now = datetime.now(timezone.utc)
        date_str = now.strftime(
            "%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)"
        )
        js_heap_limit = 4294705152
        nav_random1 = random.random()
        ua = self.user_agent
        script_src = "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js"
        script_version = None
        data_build = None
        language = "en-US"
        languages = "en-US,en"
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
        hardware_concurrency = random.choice([4, 8, 12, 16])
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
):
    generator = SentinelTokenGenerator(device_id=device_id, user_agent=user_agent)
    req_body = {
        "p": str(request_p or "").strip() or generator.generate_requirements_token(),
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
        "sec-ch-ua": sec_ch_ua
        or '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    kwargs = {"data": json.dumps(req_body), "headers": headers, "timeout": 20}
    if impersonate:
        kwargs["impersonate"] = impersonate
    try:
        response = session.post(SENTINEL_REQ_URL, **kwargs)
        if response.status_code == 200:
            return response.json()
    except Exception:
        return None
    return None


def _build_sentinel_token_python(
    session,
    device_id,
    *,
    flow="authorize_continue",
    user_agent=None,
    sec_ch_ua=None,
    impersonate=None,
):
    challenge = fetch_sentinel_challenge(
        session,
        device_id,
        flow=flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        impersonate=impersonate,
    )
    if not challenge:
        return None

    c_value = str(challenge.get("token") or "").strip()
    if not c_value:
        return None

    generator = SentinelTokenGenerator(device_id=device_id, user_agent=user_agent)
    pow_data = challenge.get("proofofwork") or {}
    if pow_data.get("required") and pow_data.get("seed"):
        p_value = generator.generate_token(
            seed=pow_data.get("seed"),
            difficulty=pow_data.get("difficulty", "0"),
        )
    else:
        p_value = generator.generate_requirements_token()

    return json.dumps(
        {
            "p": p_value,
            "t": "",
            "c": c_value,
            "id": device_id,
            "flow": flow,
        }
    )


def build_sentinel_token(
    session,
    device_id,
    flow="authorize_continue",
    user_agent=None,
    sec_ch_ua=None,
    impersonate=None,
):
    """默认 Sentinel token 构造：纯 Python。"""
    return _build_sentinel_token_python(
        session,
        device_id,
        flow=flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        impersonate=impersonate,
    )


def build_sentinel_token_vm_only(
    session,
    device_id,
    flow="authorize_continue",
    user_agent=None,
    sec_ch_ua=None,
    impersonate=None,
):
    """
    VM 分支专用构造器（命名保持不变，内部使用纯 Python）。
    """
    return _build_sentinel_token_python(
        session,
        device_id,
        flow=flow,
        user_agent=user_agent,
        sec_ch_ua=sec_ch_ua,
        impersonate=impersonate,
    )

