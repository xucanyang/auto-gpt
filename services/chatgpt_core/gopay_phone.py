from __future__ import annotations

import re
from typing import Any

DEFAULT_GOPAY_PHONE_COUNTRY_CODE = "62"
GOPAY_RECOGNIZED_COUNTRY_CODES_KEY = "chatgpt_gopay_recognized_country_codes"


def digits_only(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def normalize_gopay_recognized_country_codes(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw_items = [digits_only(item) for item in value]
    else:
        raw_items = re.findall(r"\d+", str(value or ""))

    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        code = digits_only(item)
        if not code or code in seen:
            continue
        seen.add(code)
        normalized.append(code)

    return normalized or [DEFAULT_GOPAY_PHONE_COUNTRY_CODE]


def split_gopay_phone_input(
    phone_country_code: Any,
    phone_number: Any,
    recognized_country_codes: Any,
    *,
    default_country_code: str = DEFAULT_GOPAY_PHONE_COUNTRY_CODE,
) -> dict[str, str]:
    country_code = digits_only(phone_country_code) or digits_only(default_country_code)
    number = digits_only(phone_number)
    codes = normalize_gopay_recognized_country_codes(recognized_country_codes)

    for code in sorted(codes, key=lambda item: (-len(item), codes.index(item))):
        if number.startswith(code) and len(number) > len(code):
            return {
                "phone_country_code": code,
                "phone_number": number[len(code):],
            }

    return {
        "phone_country_code": country_code or DEFAULT_GOPAY_PHONE_COUNTRY_CODE,
        "phone_number": number,
    }
