"""任务默认代理配置的归一化规则。

动态代理曾同时使用 ``task_proxy_*`` 和 ``dynamic_proxy_*`` 两组字段。
前者现在只服务指定代理/代理池；后者是动态代理的唯一 canonical 配置。
本模块不触碰数据库，供 API 保存路径和受控迁移共用，避免两处漂移。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping


TASK_PROXY_MODES = {"direct", "pool", "specified", "dynamic"}
DYNAMIC_PROXY_PROVIDERS = {"cliproxy", "miyaip"}
DYNAMIC_PROXY_CONFIG_KEYS = {
    "task_proxy_mode",
    "task_proxy_url",
    "task_proxy_country_code",
    "dynamic_proxy_template",
    "dynamic_proxy_default_country",
    "dynamic_proxy_provider",
    "miyaip_crc",
    "miyaip_key_name",
    "miyaip_pool",
    "miyaip_gateway_server",
    "miyaip_protocol",
    "miyaip_request_timeout_seconds",
}
_DYNAMIC_PROXY_LEGACY_NORMALIZATION_KEYS = {
    "task_proxy_mode",
    "task_proxy_url",
    "task_proxy_country_code",
    "dynamic_proxy_template",
    "dynamic_proxy_default_country",
}


def normalize_task_proxy_mode(value: Any, default: str = "dynamic") -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in TASK_PROXY_MODES else default


def normalize_dynamic_proxy_provider(value: Any, default: str = "cliproxy") -> str:
    provider = str(value or "").strip().lower()
    if not provider:
        return default
    if provider not in DYNAMIC_PROXY_PROVIDERS:
        raise ValueError("动态代理渠道必须是 cliproxy / miyaip")
    return provider


def _text(value: Any) -> str:
    return str(value or "").strip()


def _country(value: Any) -> str:
    return _text(value).upper()


def _summary(value: Any) -> dict[str, Any]:
    text = _text(value)
    return {
        "present": bool(text),
        "length": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "",
    }


@dataclass(frozen=True)
class DynamicProxyNormalization:
    """一次动态代理快照归一化的无敏感报告。"""

    updates: dict[str, str]
    mode: str
    template_source: str
    country_source: str
    template_conflict: bool
    country_conflict: bool
    template: dict[str, Any]
    country: dict[str, Any]

    @property
    def changed(self) -> bool:
        return bool(self.updates)

    def report(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "changed_keys": sorted(self.updates),
            "template_source": self.template_source,
            "country_source": self.country_source,
            "template_conflict": self.template_conflict,
            "country_conflict": self.country_conflict,
            "template": self.template,
            "country": self.country,
        }


def _select_dynamic_value(
    canonical: str,
    legacy: str,
    *,
    prefer_legacy_on_conflict: bool,
) -> tuple[str, str, bool]:
    if canonical and legacy:
        if canonical == legacy:
            return canonical, "canonical", False
        if prefer_legacy_on_conflict:
            return legacy, "legacy_runtime_conflict", True
        return canonical, "canonical", True
    if canonical:
        return canonical, "canonical", False
    if legacy:
        return legacy, "legacy_fallback", False
    return "", "empty", False


def normalize_dynamic_proxy_snapshot(
    config: Mapping[str, Any] | None,
    *,
    prefer_legacy_on_conflict: bool = True,
) -> DynamicProxyNormalization:
    """生成动态模式的 canonical 更新，不直接写库。

    历史运行时曾让 legacy 字段优先。因此受控迁移默认在两份非空且
    不同时把 legacy 提升成 canonical，确保升级不会悄悄切换出口。
    """

    values = config or {}
    mode = normalize_task_proxy_mode(values.get("task_proxy_mode"), "dynamic")
    if mode != "dynamic":
        return DynamicProxyNormalization(
            updates={},
            mode=mode,
            template_source="not_dynamic",
            country_source="not_dynamic",
            template_conflict=False,
            country_conflict=False,
            template=_summary(values.get("dynamic_proxy_template")),
            country=_summary(values.get("dynamic_proxy_default_country")),
        )

    raw_canonical_template = values.get("dynamic_proxy_template", "")
    raw_legacy_template = values.get("task_proxy_url", "")
    canonical_template = _text(raw_canonical_template)
    legacy_template = _text(raw_legacy_template)
    template, template_source, template_conflict = _select_dynamic_value(
        canonical_template,
        legacy_template,
        prefer_legacy_on_conflict=prefer_legacy_on_conflict,
    )

    raw_canonical_country = values.get("dynamic_proxy_default_country", "")
    raw_legacy_country = values.get("task_proxy_country_code", "")
    canonical_country = _country(raw_canonical_country)
    legacy_country = _country(raw_legacy_country)
    country, country_source, country_conflict = _select_dynamic_value(
        canonical_country,
        legacy_country,
        prefer_legacy_on_conflict=prefer_legacy_on_conflict,
    )

    updates: dict[str, str] = {}
    if template and template != canonical_template:
        updates["dynamic_proxy_template"] = template
    if _text(raw_legacy_template):
        # 只有 canonical 已经存在或刚被安全提升时，才清历史字段。
        if template:
            updates["task_proxy_url"] = ""

    if country and country != canonical_country:
        updates["dynamic_proxy_default_country"] = country
    if _text(raw_legacy_country):
        if country:
            updates["task_proxy_country_code"] = ""

    return DynamicProxyNormalization(
        updates=updates,
        mode=mode,
        template_source=template_source,
        country_source=country_source,
        template_conflict=template_conflict,
        country_conflict=country_conflict,
        template=_summary(template),
        country=_summary(country),
    )


def normalize_dynamic_proxy_update(
    update: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """归一化一次 ``PUT /config`` 的动态代理字段。

    这是保存路径而不是全量迁移：只有请求触及代理默认项时才动作。
    新 UI 明确提交 canonical 字段时 canonical 胜出；旧客户端仅提交
    legacy ``task_proxy_*`` 时仍会被提升，避免失去兼容。
    """

    incoming = dict(update or {})
    if not (set(incoming) & DYNAMIC_PROXY_CONFIG_KEYS):
        return incoming

    existing = dict(current or {})
    mode_value = incoming.get("task_proxy_mode", existing.get("task_proxy_mode", "dynamic"))
    mode = normalize_task_proxy_mode(mode_value, "dynamic")
    if "task_proxy_mode" in incoming:
        incoming["task_proxy_mode"] = mode

    explicit_provider = "dynamic_proxy_provider" in incoming
    if explicit_provider:
        incoming["dynamic_proxy_provider"] = normalize_dynamic_proxy_provider(
            incoming.get("dynamic_proxy_provider")
        )

    # 兼容旧客户端：历史上只提交 dynamic template 也会切到 dynamic。
    if "task_proxy_mode" not in incoming and _text(incoming.get("dynamic_proxy_template")):
        mode = "dynamic"
        incoming["task_proxy_mode"] = mode

    if mode != "dynamic":
        return incoming

    explicit_legacy_template = bool(
        _text(incoming.get("dynamic_proxy_template"))
        or _text(incoming.get("task_proxy_url"))
    )
    if explicit_provider:
        provider = incoming["dynamic_proxy_provider"]
    elif explicit_legacy_template:
        # Older clients know only the region/sid template contract.  Treat an
        # explicit template update as Cliproxy even if the current global
        # selection is MiyaIP, otherwise the old request would silently mutate
        # credentials for a provider it cannot represent.
        provider = "cliproxy"
        incoming["dynamic_proxy_provider"] = provider
    else:
        provider = normalize_dynamic_proxy_provider(existing.get("dynamic_proxy_provider"))
    if explicit_provider:
        incoming["dynamic_proxy_provider"] = provider

    # Provider credentials and selection are independent patches. Switching
    # channels preserves both saved channel configurations and must not
    # opportunistically rewrite legacy template fields.
    if not (set(incoming) & _DYNAMIC_PROXY_LEGACY_NORMALIZATION_KEYS):
        return incoming

    has_canonical_template = "dynamic_proxy_template" in incoming
    has_legacy_template = "task_proxy_url" in incoming
    has_canonical_country = "dynamic_proxy_default_country" in incoming
    has_legacy_country = "task_proxy_country_code" in incoming

    current_canonical_template = _text(existing.get("dynamic_proxy_template"))
    current_legacy_template = _text(existing.get("task_proxy_url"))
    current_canonical_country = _country(existing.get("dynamic_proxy_default_country"))
    current_legacy_country = _country(existing.get("task_proxy_country_code"))
    incoming_legacy_template = _text(incoming.get("task_proxy_url"))
    incoming_legacy_country = _country(incoming.get("task_proxy_country_code"))

    # 明确 canonical 值代表新 UI 的用户意图；legacy 非空代表旧客户端
    # 的用户意图。legacy 空值通常是新 UI 的清理动作，不能吞掉旧模板。
    #
    # 关键边界：当本次请求没有提交某个 canonical 字段时，绝不能因为
    # 旧字段仍残留且与 canonical 冲突，就把旧值重新提升覆盖当前值。
    # 例如只改 IP 保留分钟或默认国家时，动态节点必须保持不变。旧字段
    # 只有在 canonical 为空时才作为兼容回退。
    if has_canonical_template:
        template = _text(incoming.get("dynamic_proxy_template"))
    elif has_legacy_template and incoming_legacy_template:
        template = incoming_legacy_template
    elif current_canonical_template:
        template = current_canonical_template
    else:
        template = current_legacy_template

    if has_canonical_country:
        country = _country(incoming.get("dynamic_proxy_default_country"))
    elif has_legacy_country and incoming_legacy_country:
        country = incoming_legacy_country
    elif current_canonical_country:
        country = current_canonical_country
    else:
        country = current_legacy_country

    if template:
        incoming["dynamic_proxy_template"] = template
        incoming["task_proxy_url"] = ""
    if country:
        incoming["dynamic_proxy_default_country"] = country
        incoming["task_proxy_country_code"] = ""

    return incoming
