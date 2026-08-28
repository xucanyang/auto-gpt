PINNED_CHROMIUM_VERSION = "151.0.0.0"
PINNED_CHROMIUM_MAJOR = 151
PINNED_CURL_IMPERSONATE = "chrome150"
PINNED_CHROMIUM_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{PINNED_CHROMIUM_VERSION} Safari/537.36"
)
DEFAULT_SENTINEL_SDK_VERSION = "20260219f9f6"
DEFAULT_SENTINEL_FRAME_URL = (
    "https://sentinel.openai.com/backend-api/sentinel/frame.html"
    f"?sv={DEFAULT_SENTINEL_SDK_VERSION}"
)
DEFAULT_SENTINEL_SDK_URL = (
    f"https://sentinel.openai.com/sentinel/{DEFAULT_SENTINEL_SDK_VERSION}/sdk.js"
)
