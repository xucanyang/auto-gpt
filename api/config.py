import os
import math
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from core.config_store import config_store
from core.shared_config import SharedConfigConflict, filter_shareable_config, shared_config_store
from core.task_proxy_config import normalize_dynamic_proxy_update

router = APIRouter(prefix="/config", tags=["config"])

CONFIG_KEYS = [
    "laoudo_auth",
    "laoudo_email",
    "laoudo_account_id",
    "yescaptcha_key",
    "twocaptcha_key",
    "default_executor",
    "default_captcha_solver",
    "proxy_pool_cooldown_enabled",
    "proxy_scan_enabled",
    "proxy_scan_interval_minutes",
    "proxy_scan_concurrency",
    "proxy_scan_timeout_seconds",
    "proxy_scan_targets",
    "proxy_scan_only_active",
    "proxy_scan_min_score",
    "proxy_pool_max_candidates",
    "task_proxy_mode",
    "task_proxy_url",
    "task_proxy_country_code",
    "task_proxy_failover",
    "task_proxy_max_candidates",
    "task_proxy_min_score",
    "dynamic_proxy_template",
    "dynamic_proxy_default_country",
    "dynamic_proxy_ip_retention_minutes",
    "dynamic_proxy_require_country_match",
    "dynamic_proxy_probe_timeout_seconds",
    "dynamic_proxy_probe_enabled",
    "dynamic_proxy_max_attempts",
    "duckmail_api_url",
    "duckmail_provider_url",
    "duckmail_bearer",
    "duckmail_domain",
    "duckmail_api_key",
    "freemail_api_url",
    "freemail_admin_token",
    "freemail_username",
    "freemail_password",
    "freemail_domain",
    "moemail_api_url",
    "moemail_api_key",
    "skymail_api_base",
    "skymail_token",
    "skymail_domain",
    "cloudmail_api_base",
    "cloudmail_admin_email",
    "cloudmail_admin_password",
    "cloudmail_domain",
    "cloudmail_subdomain",
    "cloudmail_timeout",
    "mail_provider",
    "mailbox_otp_timeout_seconds",
    "email_api_lines",
    "email_api_poll_interval_seconds",
    "email_api_request_timeout_seconds",
    "email_api_gmail_dot_variant_enabled",
    "email_api_gmail_variant_count",
    "email_api_gmail_variant_rules",
    "email_api_gmail_plus_tag_template",
    "email_api_default_scheme",
    "icloud_hme_mode",
    "icloud_cookie",
    "icloud_domain_base",
    "icloud_forward_to",
    "icloud_forward_mailbox_id",
    "icloud_hme_helper_api_url",
    "icloud_hme_helper_internal_key",
    "icloud_hme_helper_api_key_header",
    "icloud_hme_helper_consumer",
    "icloud_hme_helper_checkout_ttl_seconds",
    "icloud_hme_helper_wait_timeout_seconds",
    "icloud_hme_helper_max_cache_age_seconds",
    "icloud_hme_auto_create_enabled",
    "icloud_hme_auto_create_stock_limit",
    "icloud_hme_auto_create_interval_min_minutes",
    "icloud_hme_auto_create_interval_max_minutes",
    "icloud_hme_auto_create_rate_limit_backoff_minutes",
    "icloud_hme_auto_create_error_backoff_minutes",
    "icloud_hme_auto_delete_enabled",
    "icloud_hme_auto_delete_account_interval_min_minutes",
    "icloud_hme_auto_delete_account_interval_max_minutes",
    "icloud_hme_auto_delete_interval_min_minutes",
    "icloud_hme_auto_delete_interval_max_minutes",
    "icloud_hme_auto_delete_max_per_run",
    "icloud_hme_auto_delete_per_item_delay_min_seconds",
    "icloud_hme_auto_delete_per_item_delay_max_seconds",
    "icloud_hme_auto_delete_rate_limit_backoff_minutes",
    "icloud_hme_auto_delete_error_backoff_minutes",
    "icloud_hme_auto_delete_recheck_before_delete",
    "icloud_hme_auto_delete_pause_active_tasks",
    "icloud_hme_auto_delete_dead_statuses",
    "tempmail_archive_cleanup_enabled",
    "tempmail_archive_cleanup_interval_minutes",
    "tempmail_archive_cleanup_keep_recent_minutes",
    "tempmail_archive_cleanup_threshold",
    "tempmail_archive_cleanup_pause_active_tasks",
    "tempmail_archive_cleanup_mailbox",
    "tempmail_archive_cleanup_backup_path",
    "tempmail_api_url",
    "tempmail_api_key",
    "tempmail_api_key_header",
    "tempmail_mode",
    "tempmail_primary_domain",
    "tempmail_fixed_domains",
    "tempmail_wait_timeout_seconds",
    "tempmail_ttl_minutes",
    "tempmail_reuse_window_minutes",
    "tempmail_permanent",
    "tempmail_platform",
    "maliapi_base_url",
    "maliapi_api_key",
    "maliapi_domain",
    "maliapi_auto_domain_strategy",
    "applemail_base_url",
    "applemail_pool_dir",
    "applemail_pool_file",
    "applemail_mailboxes",
    "gptmail_base_url",
    "gptmail_api_key",
    "gptmail_domain",
    "opentrashmail_api_url",
    "opentrashmail_domain",
    "opentrashmail_password",
    "cfworker_api_url",
    "cfworker_admin_token",
    "cfworker_custom_auth",
    "cfworker_domain",
    "cfworker_domains",
    "cfworker_enabled_domains",
    "cfworker_subdomain",
    "cfworker_random_subdomain",
    "cfworker_fingerprint",
    "chatgpt_phone_verification_provider",
    "local_phone_gateway_url",
    "local_phone_gateway_token",
    "local_phone_gateway_service_alias",
    "local_phone_gateway_auto_acquire_enabled",
    "local_phone_gateway_timeout_seconds",
    "local_phone_gateway_poll_interval_seconds",
    "local_phone_gateway_max_attempts",
    "local_phone_gateway_max_resend_attempts",
    "local_phone_gateway_resend_interval_seconds",
    "local_phone_gateway_queue_timeout_seconds",
    "smstome_cookie",
    "smstome_country_slugs",
    "smstome_phone_attempts",
    "smstome_otp_timeout_seconds",
    "smstome_poll_interval_seconds",
    "smstome_sync_max_pages_per_country",
    "chatgpt_phone_signup_use_pool",
    "chatgpt_phone_signup_timeout_seconds",
    "chatgpt_phone_signup_poll_interval_seconds",
    "chatgpt_phone_signup_max_resend_attempts",
    "chatgpt_phone_signup_resend_interval_seconds",
    "luckmail_base_url",
    "luckmail_api_key",
    "luckmail_email_type",
    "luckmail_domain",
    "cpa_api_url",
    "cpa_api_key",
    "cpa_cleanup_enabled",
    "cpa_cleanup_interval_minutes",
    "cpa_cleanup_threshold",
    "cpa_cleanup_concurrency",
    "cpa_cleanup_register_delay_seconds",
    "sub2api_api_url",
    "oaipay_api_url",
    "sub2api_api_key",
    "oaipay_api_key",
    "sub2api_group_ids",
    "oaipay_group",
    "chatgpt_save_registration_access_token_account",
    "chatgpt_existing_account_login_route_enabled",
    "chatgpt_register_unique_exit_ip_enabled",
    "chatgpt_local_status_probe_concurrency",
    "chatgpt_local_status_probe_unique_exit_ip_enabled",
    "chatgpt_local_status_probe_delay_seconds",
    "chatgpt_local_status_probe_delay_max_seconds",
    "chatgpt_register_otp_wait_seconds",
    "chatgpt_register_otp_resend_wait_seconds",
    "chatgpt_register_otp_account_budget_seconds",
    "chatgpt_phone_signup_password",
    "chatgpt_existing_account_login_password",
    "chatgpt_resume_auth_allow_phone_verification",
    "chatgpt_resume_auth_allow_add_phone_verification",
    "chatgpt_resume_auth_allow_existing_phone_verification",
    "chatgpt_recheck_allow_add_phone_verification",
    "chatgpt_recheck_allow_existing_phone_verification",
    "existing_phone_otp_timeout_seconds",
    "existing_phone_otp_poll_interval_seconds",
    "existing_phone_otp_max_resend_attempts",
    "existing_phone_otp_resend_interval_seconds",
    "chatgpt_subscription_auth_capture_retry_delays_seconds",
    "chatgpt_workspace_select_no_org_retry_delays_seconds",
    "chatgpt_payment_link_defaults",
    "openai_pay_long_link_base_url",
    "openai_pay_long_link_api_key",
    "chatgpt_access_token_only_checkout_amount_check_enabled",
    "chatgpt_access_token_only_checkout_country",
    "chatgpt_access_token_only_checkout_currency",
    "chatgpt_access_token_only_zero_amount_stop_enabled",
    "chatgpt_access_token_only_zero_amount_stop_threshold",
    "external_subscription_api_enabled",
    "external_subscription_api_token",
    "external_subscription_verify_after_seconds",
    "external_access_token_api_enabled",
    "external_access_token_api_token",
    "external_access_token_allow_refresh",
    "external_access_token_default_lease_seconds",
    "external_access_token_max_limit",
    "external_access_token_precheck_cooldown_seconds",
    "chatgpt_llm_api_base_url",
    "chatgpt_llm_api_key",
    "chatgpt_llm_model",
    "chatgpt_llm_timeout_seconds",
    "chatgpt_llm_billing_address_prompt",
    "chatgpt_phone_verification_enabled",
    "codex_proxy_url",
    "codex_proxy_key",
    "codex_proxy_upload_type",
    "cliproxyapi_base_url",
    "cliproxyapi_management_key",
    "contribution_enabled",
    "contribution_server_url",
    "contribution_key",
]


class ConfigUpdate(BaseModel):
    data: dict
    base_revision: int | None = None


class ShareStateUpdate(BaseModel):
    enabled: bool
    pull: bool = True


class SharePushRequest(BaseModel):
    base_revision: int | None = None
    confirm: bool = False
    note: str = ""
    # Backward-compatible default: callers that only need to replace the
    # template can keep the current instance in local mode.
    enable_shared: bool = False


class TempMailDomainsRequest(BaseModel):
    api_url: str = ""
    api_key: str = ""
    api_key_header: str = ""
    include_inactive: bool = False


class AppleMailImportRequest(BaseModel):
    content: str
    filename: str = ""
    pool_dir: str = ""
    bind_to_config: bool = True


class PaymentLinkConnectionTestRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""


_LOCAL_STATUS_PROBE_MAX_CONCURRENCY = 10
_LOCAL_STATUS_PROBE_MAX_DELAY_SECONDS = 3600.0


def _config_bool(value: Any, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y", "是", "开启", "启用"}:
        return True
    if text in {"0", "false", "no", "off", "n", "否", "关闭", "禁用"}:
        return False
    return default


def _normalize_probe_delay_config(value: Any, label: str) -> str:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"{label}必须是 0 到 3600 之间的有限数字") from exc
    if not math.isfinite(parsed) or parsed < 0 or parsed > _LOCAL_STATUS_PROBE_MAX_DELAY_SECONDS:
        raise HTTPException(400, f"{label}必须是 0 到 3600 之间的有限数字")
    return str(int(parsed)) if parsed.is_integer() else str(parsed)


def _normalize_local_status_probe_update(safe: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Normalize and validate the global local-status probe contract atomically."""
    probe_keys = {
        "chatgpt_local_status_probe_concurrency",
        "chatgpt_local_status_probe_unique_exit_ip_enabled",
        "chatgpt_local_status_probe_delay_seconds",
        "chatgpt_local_status_probe_delay_max_seconds",
        "task_proxy_mode",
        "task_proxy_failover",
    }
    if not probe_keys.intersection(safe):
        return safe

    merged = dict(current or {})
    merged.update(safe)

    if "chatgpt_local_status_probe_concurrency" in safe:
        raw = safe["chatgpt_local_status_probe_concurrency"]
        try:
            parsed = float(raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "本地状态同步并发必须是 1 到 10 的整数") from exc
        if not math.isfinite(parsed) or not parsed.is_integer() or not 1 <= parsed <= _LOCAL_STATUS_PROBE_MAX_CONCURRENCY:
            raise HTTPException(400, "本地状态同步并发必须是 1 到 10 的整数")
        safe["chatgpt_local_status_probe_concurrency"] = str(int(parsed))

    if "chatgpt_local_status_probe_unique_exit_ip_enabled" in safe:
        safe["chatgpt_local_status_probe_unique_exit_ip_enabled"] = (
            "true" if _config_bool(safe["chatgpt_local_status_probe_unique_exit_ip_enabled"], default=True) else "false"
        )

    for key, label in (
        ("chatgpt_local_status_probe_delay_seconds", "本地状态同步最小延时"),
        ("chatgpt_local_status_probe_delay_max_seconds", "本地状态同步最大延时"),
    ):
        if key in safe:
            safe[key] = _normalize_probe_delay_config(safe[key], label)

    merged.update(safe)

    def _effective_delay(value: Any) -> float:
        try:
            parsed = float(value or 0)
        except (TypeError, ValueError):
            return 0.0
        return parsed if math.isfinite(parsed) and parsed >= 0 else 0.0

    min_delay = _effective_delay(merged.get("chatgpt_local_status_probe_delay_seconds"))
    max_delay = _effective_delay(merged.get("chatgpt_local_status_probe_delay_max_seconds"))
    if max_delay < min_delay:
        raise HTTPException(400, "本地状态同步最大延时不能小于最小延时")

    mode = str(merged.get("task_proxy_mode") or "dynamic").strip().lower()
    unique_exit_ip = _config_bool(
        merged.get("chatgpt_local_status_probe_unique_exit_ip_enabled"),
        default=True,
    )
    failover = _config_bool(merged.get("task_proxy_failover"), default=False)
    if unique_exit_ip and mode in {"direct", "none", "no_proxy", "直连"}:
        raise HTTPException(400, "直连模式不能满足本地状态同步的独立出口 IP 要求，请关闭该开关或改用代理模式")
    if unique_exit_ip and mode in {"specified", "manual", "explicit"} and not failover:
        raise HTTPException(400, "指定代理模式开启独立出口 IP 时必须开启失败切换，或关闭独立出口要求")
    return safe


def _normalize_payment_link_service_update(safe: dict[str, Any]) -> dict[str, Any]:
    base_url_key = "openai_pay_long_link_base_url"
    api_key_key = "openai_pay_long_link_api_key"
    if base_url_key in safe:
        raw_base_url = str(safe.get(base_url_key) or "").strip()
        if raw_base_url:
            from services.chatgpt_core.long_link_payment_client import (
                LongLinkPaymentError,
                normalize_long_link_base_url,
            )

            try:
                raw_base_url = normalize_long_link_base_url(raw_base_url)
            except LongLinkPaymentError as exc:
                raise HTTPException(400, str(exc)) from exc
        safe[base_url_key] = raw_base_url
    if api_key_key in safe:
        api_key = str(safe.get(api_key_key) or "").strip()
        if len(api_key) > 512 or any(ord(character) < 32 or ord(character) == 127 for character in api_key):
            raise HTTPException(400, "支付链接生成服务密钥格式无效")
        safe[api_key_key] = api_key
    return safe


def _default_tempmail_archive_backup_path() -> str:
    runtime_dir = str(os.getenv("APP_RUNTIME_DIR") or "").strip()
    if runtime_dir:
        return str(Path(runtime_dir) / "tempmail_email_backups.db")
    return "data/tempmail_email_backups.db"


def _normalize_tempmail_domain_item(item):
    if isinstance(item, str):
        domain = item.strip().lower().lstrip("@.")
        if not domain:
            return None
        return {
            "domain": domain,
            "is_active": True,
            "status": "active",
            "dns_status": "",
            "mailbox_count": None,
        }
    if not isinstance(item, dict):
        return None
    domain = str(
        item.get("domain")
        or item.get("name")
        or item.get("value")
        or ""
    ).strip().lower().lstrip("@.")
    if not domain:
        return None
    is_active = item.get("is_active")
    if is_active is None:
        is_active = item.get("active")
    status = str(item.get("status") or ("active" if is_active is not False else "disabled")).strip().lower()
    dns_status = str(item.get("dns_status") or "").strip().lower()
    available = (is_active is not False) and status in {"", "active", "ready", "enabled"} and dns_status not in {"missing", "error", "failed", "invalid"}
    return {
        "domain": domain,
        "is_active": bool(is_active is not False),
        "status": status or "active",
        "dns_status": dns_status,
        "available": available,
        "mailbox_count": item.get("mailbox_count"),
        "dns_record_count": item.get("dns_record_count"),
        "is_protected": item.get("is_protected"),
    }


def _extract_tempmail_domain_items(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("domains", "data", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    nested = payload.get("data")
    if isinstance(nested, dict):
        for key in ("domains", "items"):
            value = nested.get(key)
            if isinstance(value, list):
                return value
    return []


@router.post("/tempmail/domains")
def list_tempmail_domains(body: TempMailDomainsRequest | None = None):
    from core.base_mailbox import TempMailLocalMailbox

    body = body or TempMailDomainsRequest()
    api_url = str(body.api_url or config_store.get("tempmail_api_url", "") or "").strip()
    api_key = str(body.api_key or config_store.get("tempmail_api_key", "") or "").strip()
    api_key_header = str(body.api_key_header or config_store.get("tempmail_api_key_header", "Authorization") or "Authorization").strip() or "Authorization"
    if not api_url or not api_key:
        raise HTTPException(400, "TempMail API URL / API Key 未配置")

    mailbox = TempMailLocalMailbox(
        api_url=api_url,
        api_key=api_key,
        api_key_header=api_key_header,
        mode="fixed_domain",
    )
    try:
        response = mailbox._request(
            "GET",
            "/api/domains",
            headers=mailbox._headers(),
            timeout=15,
        )
    except Exception as exc:
        raise HTTPException(502, f"读取 TempMail 域名失败: {exc}") from exc
    if response.status_code != 200:
        raise HTTPException(502, f"读取 TempMail 域名失败: HTTP {response.status_code} {response.text[:200]}")
    try:
        payload = response.json()
    except Exception as exc:
        raise HTTPException(502, "读取 TempMail 域名失败: 返回不是 JSON") from exc

    seen = set()
    domains = []
    for item in _extract_tempmail_domain_items(payload):
        normalized = _normalize_tempmail_domain_item(item)
        if not normalized:
            continue
        domain = normalized["domain"]
        if domain in seen:
            continue
        seen.add(domain)
        if body.include_inactive or normalized.get("available"):
            domains.append(normalized)

    return {
        "ok": True,
        "domains": domains,
        "available_domains": [item["domain"] for item in domains if item.get("available")],
    }


def _build_config_response(*, local_only: bool = False) -> dict[str, Any]:
    all_cfg = config_store.get_local_all() if local_only else config_store.get_all()
    if not all_cfg.get("mail_provider"):
        all_cfg["mail_provider"] = "luckmail"
    if not all_cfg.get("proxy_pool_cooldown_enabled"):
        all_cfg["proxy_pool_cooldown_enabled"] = "true"
    if not all_cfg.get("proxy_scan_enabled"):
        all_cfg["proxy_scan_enabled"] = "false"
    if not all_cfg.get("proxy_scan_interval_minutes"):
        all_cfg["proxy_scan_interval_minutes"] = "30"
    if not all_cfg.get("proxy_scan_concurrency"):
        all_cfg["proxy_scan_concurrency"] = "8"
    if not all_cfg.get("proxy_scan_timeout_seconds"):
        all_cfg["proxy_scan_timeout_seconds"] = "8"
    if not all_cfg.get("proxy_scan_targets"):
        all_cfg["proxy_scan_targets"] = "basic,geo,chatgpt"
    if not all_cfg.get("proxy_scan_only_active"):
        all_cfg["proxy_scan_only_active"] = "true"
    if not all_cfg.get("proxy_scan_min_score"):
        all_cfg["proxy_scan_min_score"] = "50"
    if not all_cfg.get("proxy_pool_max_candidates"):
        all_cfg["proxy_pool_max_candidates"] = "5"
    if not all_cfg.get("task_proxy_mode"):
        all_cfg["task_proxy_mode"] = "dynamic"
    if not all_cfg.get("task_proxy_url"):
        all_cfg["task_proxy_url"] = ""
    if not all_cfg.get("task_proxy_country_code"):
        all_cfg["task_proxy_country_code"] = ""
    if not all_cfg.get("task_proxy_failover"):
        all_cfg["task_proxy_failover"] = "false"
    if not all_cfg.get("task_proxy_max_candidates"):
        all_cfg["task_proxy_max_candidates"] = all_cfg.get("proxy_pool_max_candidates") or "5"
    if not all_cfg.get("task_proxy_min_score"):
        all_cfg["task_proxy_min_score"] = all_cfg.get("proxy_scan_min_score") or "50"
    # GET 只做内存兼容展示，绝不偷偷写库。旧 dynamic 配置仍可能只有
    # task_proxy_*；下一次受控保存/迁移会把它们归一化成 canonical 字段。
    if str(all_cfg.get("task_proxy_mode") or "dynamic").strip().lower() == "dynamic":
        if not all_cfg.get("dynamic_proxy_template") and all_cfg.get("task_proxy_url"):
            all_cfg["dynamic_proxy_template"] = all_cfg.get("task_proxy_url")
        if not all_cfg.get("dynamic_proxy_default_country") and all_cfg.get("task_proxy_country_code"):
            all_cfg["dynamic_proxy_default_country"] = all_cfg.get("task_proxy_country_code")
    if not all_cfg.get("dynamic_proxy_template"):
        all_cfg["dynamic_proxy_template"] = ""
    if not all_cfg.get("dynamic_proxy_default_country"):
        all_cfg["dynamic_proxy_default_country"] = "JP"
    if not all_cfg.get("dynamic_proxy_ip_retention_minutes"):
        all_cfg["dynamic_proxy_ip_retention_minutes"] = "5"
    if not all_cfg.get("dynamic_proxy_require_country_match"):
        all_cfg["dynamic_proxy_require_country_match"] = "true"
    if not all_cfg.get("dynamic_proxy_probe_timeout_seconds"):
        all_cfg["dynamic_proxy_probe_timeout_seconds"] = "8"
    if not all_cfg.get("dynamic_proxy_probe_enabled"):
        all_cfg["dynamic_proxy_probe_enabled"] = "true"
    if not all_cfg.get("dynamic_proxy_max_attempts"):
        all_cfg["dynamic_proxy_max_attempts"] = "5"
    if not all_cfg.get("chatgpt_local_status_probe_concurrency"):
        all_cfg["chatgpt_local_status_probe_concurrency"] = "1"
    if not all_cfg.get("chatgpt_local_status_probe_unique_exit_ip_enabled"):
        all_cfg["chatgpt_local_status_probe_unique_exit_ip_enabled"] = "true"
    if not all_cfg.get("chatgpt_local_status_probe_delay_seconds"):
        all_cfg["chatgpt_local_status_probe_delay_seconds"] = "0"
    if not all_cfg.get("chatgpt_local_status_probe_delay_max_seconds"):
        all_cfg["chatgpt_local_status_probe_delay_max_seconds"] = "0"
    if "chatgpt_save_registration_access_token_account" not in all_cfg:
        all_cfg["chatgpt_save_registration_access_token_account"] = "true"
    if not all_cfg.get("chatgpt_register_otp_wait_seconds"):
        all_cfg["chatgpt_register_otp_wait_seconds"] = "120"
    if not all_cfg.get("chatgpt_register_otp_resend_wait_seconds"):
        all_cfg["chatgpt_register_otp_resend_wait_seconds"] = "90"
    if not all_cfg.get("chatgpt_register_otp_account_budget_seconds"):
        all_cfg["chatgpt_register_otp_account_budget_seconds"] = "210"
    if not all_cfg.get("email_api_poll_interval_seconds"):
        all_cfg["email_api_poll_interval_seconds"] = "3"
    if not all_cfg.get("email_api_request_timeout_seconds"):
        all_cfg["email_api_request_timeout_seconds"] = "15"
    if not all_cfg.get("email_api_gmail_dot_variant_enabled"):
        all_cfg["email_api_gmail_dot_variant_enabled"] = "true"
    if not all_cfg.get("email_api_gmail_variant_count"):
        all_cfg["email_api_gmail_variant_count"] = "2"
    if not all_cfg.get("email_api_gmail_variant_rules"):
        all_cfg["email_api_gmail_variant_rules"] = "all"
    if not all_cfg.get("email_api_gmail_plus_tag_template"):
        all_cfg["email_api_gmail_plus_tag_template"] = "r{rand}"
    if not all_cfg.get("email_api_default_scheme"):
        all_cfg["email_api_default_scheme"] = "https"
    if not all_cfg.get("icloud_hme_mode"):
        all_cfg["icloud_hme_mode"] = "live"
    if not all_cfg.get("icloud_domain_base"):
        all_cfg["icloud_domain_base"] = "icloud.com"
    if not all_cfg.get("icloud_forward_to"):
        all_cfg["icloud_forward_to"] = "b@cccy.me"
    if not all_cfg.get("icloud_hme_helper_api_key_header"):
        all_cfg["icloud_hme_helper_api_key_header"] = "X-Internal-Key"
    if not all_cfg.get("icloud_hme_helper_consumer"):
        all_cfg["icloud_hme_helper_consumer"] = "auto-gpt/chatgpt_register"
    if not all_cfg.get("icloud_hme_helper_checkout_ttl_seconds"):
        all_cfg["icloud_hme_helper_checkout_ttl_seconds"] = "10800"
    if not all_cfg.get("icloud_hme_helper_wait_timeout_seconds"):
        all_cfg["icloud_hme_helper_wait_timeout_seconds"] = "300"
    if not all_cfg.get("icloud_hme_helper_max_cache_age_seconds"):
        all_cfg["icloud_hme_helper_max_cache_age_seconds"] = "86400"
    if not all_cfg.get("icloud_hme_auto_create_enabled"):
        all_cfg["icloud_hme_auto_create_enabled"] = "false"
    if not all_cfg.get("icloud_hme_auto_create_stock_limit"):
        all_cfg["icloud_hme_auto_create_stock_limit"] = "10"
    if not all_cfg.get("icloud_hme_auto_create_interval_min_minutes"):
        all_cfg["icloud_hme_auto_create_interval_min_minutes"] = "60"
    if not all_cfg.get("icloud_hme_auto_create_interval_max_minutes"):
        all_cfg["icloud_hme_auto_create_interval_max_minutes"] = "120"
    if not all_cfg.get("icloud_hme_auto_create_rate_limit_backoff_minutes"):
        all_cfg["icloud_hme_auto_create_rate_limit_backoff_minutes"] = "360"
    if not all_cfg.get("icloud_hme_auto_create_error_backoff_minutes"):
        all_cfg["icloud_hme_auto_create_error_backoff_minutes"] = "3"
    if not all_cfg.get("icloud_hme_auto_delete_enabled"):
        all_cfg["icloud_hme_auto_delete_enabled"] = "false"
    if not all_cfg.get("icloud_hme_auto_delete_account_interval_min_minutes"):
        all_cfg["icloud_hme_auto_delete_account_interval_min_minutes"] = "10"
    if not all_cfg.get("icloud_hme_auto_delete_account_interval_max_minutes"):
        all_cfg["icloud_hme_auto_delete_account_interval_max_minutes"] = "30"
    if not all_cfg.get("icloud_hme_auto_delete_interval_min_minutes"):
        all_cfg["icloud_hme_auto_delete_interval_min_minutes"] = "60"
    if not all_cfg.get("icloud_hme_auto_delete_interval_max_minutes"):
        all_cfg["icloud_hme_auto_delete_interval_max_minutes"] = "120"
    if not all_cfg.get("icloud_hme_auto_delete_max_per_run"):
        all_cfg["icloud_hme_auto_delete_max_per_run"] = "20"
    if not all_cfg.get("icloud_hme_auto_delete_per_item_delay_min_seconds"):
        all_cfg["icloud_hme_auto_delete_per_item_delay_min_seconds"] = "30"
    if not all_cfg.get("icloud_hme_auto_delete_per_item_delay_max_seconds"):
        all_cfg["icloud_hme_auto_delete_per_item_delay_max_seconds"] = "90"
    if not all_cfg.get("icloud_hme_auto_delete_rate_limit_backoff_minutes"):
        all_cfg["icloud_hme_auto_delete_rate_limit_backoff_minutes"] = "60"
    if not all_cfg.get("icloud_hme_auto_delete_error_backoff_minutes"):
        all_cfg["icloud_hme_auto_delete_error_backoff_minutes"] = "3"
    if not all_cfg.get("icloud_hme_auto_delete_recheck_before_delete"):
        all_cfg["icloud_hme_auto_delete_recheck_before_delete"] = "true"
    if not all_cfg.get("icloud_hme_auto_delete_pause_active_tasks"):
        all_cfg["icloud_hme_auto_delete_pause_active_tasks"] = "true"
    if not all_cfg.get("icloud_hme_auto_delete_dead_statuses"):
        all_cfg["icloud_hme_auto_delete_dead_statuses"] = "account_deactivated,password_invalid"
    if not all_cfg.get("tempmail_archive_cleanup_enabled"):
        all_cfg["tempmail_archive_cleanup_enabled"] = "false"
    if not all_cfg.get("tempmail_archive_cleanup_interval_minutes"):
        all_cfg["tempmail_archive_cleanup_interval_minutes"] = "30"
    if not all_cfg.get("tempmail_archive_cleanup_keep_recent_minutes"):
        all_cfg["tempmail_archive_cleanup_keep_recent_minutes"] = "60"
    if not all_cfg.get("tempmail_archive_cleanup_threshold"):
        all_cfg["tempmail_archive_cleanup_threshold"] = "100"
    if not all_cfg.get("tempmail_archive_cleanup_pause_active_tasks"):
        all_cfg["tempmail_archive_cleanup_pause_active_tasks"] = "true"
    if not all_cfg.get("tempmail_archive_cleanup_mailbox"):
        all_cfg["tempmail_archive_cleanup_mailbox"] = all_cfg.get("icloud_forward_to") or "b@cccy.me"
    if not all_cfg.get("tempmail_archive_cleanup_backup_path"):
        all_cfg["tempmail_archive_cleanup_backup_path"] = _default_tempmail_archive_backup_path()
    if not all_cfg.get("applemail_base_url"):
        all_cfg["applemail_base_url"] = "https://www.appleemail.top"
    if not all_cfg.get("applemail_pool_dir"):
        all_cfg["applemail_pool_dir"] = "mail"
    if not all_cfg.get("applemail_mailboxes"):
        all_cfg["applemail_mailboxes"] = "INBOX,Junk"
    if not all_cfg.get("gptmail_base_url"):
        all_cfg["gptmail_base_url"] = "https://mail.chatgpt.org.uk"
    if not all_cfg.get("luckmail_base_url"):
        all_cfg["luckmail_base_url"] = "https://mails.luckyous.com/"
    if not all_cfg.get("contribution_server_url"):
        all_cfg["contribution_server_url"] = "http://new.xem8k5.top:7317/"
    if not all_cfg.get("openai_pay_long_link_base_url"):
        all_cfg["openai_pay_long_link_base_url"] = (
            os.getenv("OPENAI_PAY_LONG_LINK_BASE_URL") or "http://openai-pay-long-link:8788"
        ).strip()
    if not all_cfg.get("chatgpt_access_token_only_zero_amount_stop_enabled"):
        all_cfg["chatgpt_access_token_only_zero_amount_stop_enabled"] = "false"
    if not all_cfg.get("chatgpt_access_token_only_checkout_amount_check_enabled"):
        all_cfg["chatgpt_access_token_only_checkout_amount_check_enabled"] = "true"
    if not all_cfg.get("chatgpt_access_token_only_checkout_country"):
        all_cfg["chatgpt_access_token_only_checkout_country"] = "US"
    if not all_cfg.get("chatgpt_access_token_only_checkout_currency"):
        all_cfg["chatgpt_access_token_only_checkout_currency"] = "USD"
    if not all_cfg.get("chatgpt_access_token_only_zero_amount_stop_threshold"):
        all_cfg["chatgpt_access_token_only_zero_amount_stop_threshold"] = "1"
    if not all_cfg.get("external_subscription_api_enabled"):
        all_cfg["external_subscription_api_enabled"] = "false"
    if not all_cfg.get("external_subscription_verify_after_seconds"):
        all_cfg["external_subscription_verify_after_seconds"] = "300"
    if not all_cfg.get("external_access_token_api_enabled"):
        all_cfg["external_access_token_api_enabled"] = "false"
    if not all_cfg.get("external_access_token_allow_refresh"):
        all_cfg["external_access_token_allow_refresh"] = "true"
    if not all_cfg.get("external_access_token_default_lease_seconds"):
        all_cfg["external_access_token_default_lease_seconds"] = "86400"
    if not all_cfg.get("external_access_token_max_limit"):
        all_cfg["external_access_token_max_limit"] = "50"
    if not all_cfg.get("external_access_token_precheck_cooldown_seconds"):
        all_cfg["external_access_token_precheck_cooldown_seconds"] = "600"
    if not all_cfg.get("chatgpt_resume_auth_allow_phone_verification"):
        all_cfg["chatgpt_resume_auth_allow_phone_verification"] = "false"
    if not all_cfg.get("chatgpt_resume_auth_allow_add_phone_verification"):
        all_cfg["chatgpt_resume_auth_allow_add_phone_verification"] = all_cfg.get("chatgpt_resume_auth_allow_phone_verification") or "false"
    if not all_cfg.get("chatgpt_resume_auth_allow_existing_phone_verification"):
        all_cfg["chatgpt_resume_auth_allow_existing_phone_verification"] = "true"
    if not all_cfg.get("chatgpt_recheck_allow_add_phone_verification"):
        all_cfg["chatgpt_recheck_allow_add_phone_verification"] = "false"
    if not all_cfg.get("chatgpt_recheck_allow_existing_phone_verification"):
        all_cfg["chatgpt_recheck_allow_existing_phone_verification"] = "true"
    if not all_cfg.get("existing_phone_otp_timeout_seconds"):
        all_cfg["existing_phone_otp_timeout_seconds"] = "180"
    if not all_cfg.get("existing_phone_otp_poll_interval_seconds"):
        all_cfg["existing_phone_otp_poll_interval_seconds"] = "5"
    if not all_cfg.get("existing_phone_otp_max_resend_attempts"):
        all_cfg["existing_phone_otp_max_resend_attempts"] = "1"
    if not all_cfg.get("existing_phone_otp_resend_interval_seconds"):
        all_cfg["existing_phone_otp_resend_interval_seconds"] = "30"
    if not all_cfg.get("chatgpt_subscription_auth_capture_retry_delays_seconds"):
        all_cfg["chatgpt_subscription_auth_capture_retry_delays_seconds"] = "5,10"
    if not all_cfg.get("chatgpt_workspace_select_no_org_retry_delays_seconds"):
        all_cfg["chatgpt_workspace_select_no_org_retry_delays_seconds"] = "5,10,20"
    if not all_cfg.get("chatgpt_phone_verification_provider"):
        all_cfg["chatgpt_phone_verification_provider"] = "smstome"
    if not all_cfg.get("local_phone_gateway_url"):
        all_cfg["local_phone_gateway_url"] = "http://sms-gateway:8720"
    if not all_cfg.get("local_phone_gateway_service_alias"):
        all_cfg["local_phone_gateway_service_alias"] = "chatgpt"
    if not all_cfg.get("local_phone_gateway_auto_acquire_enabled"):
        all_cfg["local_phone_gateway_auto_acquire_enabled"] = "true"
    if not all_cfg.get("local_phone_gateway_timeout_seconds"):
        all_cfg["local_phone_gateway_timeout_seconds"] = "180"
    if not all_cfg.get("local_phone_gateway_poll_interval_seconds"):
        all_cfg["local_phone_gateway_poll_interval_seconds"] = "5"
    if not all_cfg.get("local_phone_gateway_max_attempts"):
        all_cfg["local_phone_gateway_max_attempts"] = "3"
    if not all_cfg.get("local_phone_gateway_max_resend_attempts"):
        all_cfg["local_phone_gateway_max_resend_attempts"] = "20"
    if not all_cfg.get("local_phone_gateway_resend_interval_seconds"):
        all_cfg["local_phone_gateway_resend_interval_seconds"] = "30"
    if not all_cfg.get("local_phone_gateway_queue_timeout_seconds"):
        all_cfg["local_phone_gateway_queue_timeout_seconds"] = "3600"
    if not all_cfg.get("chatgpt_phone_signup_use_pool"):
        all_cfg["chatgpt_phone_signup_use_pool"] = "false"
    if not all_cfg.get("chatgpt_phone_signup_timeout_seconds"):
        all_cfg["chatgpt_phone_signup_timeout_seconds"] = "180"
    if not all_cfg.get("chatgpt_phone_signup_poll_interval_seconds"):
        all_cfg["chatgpt_phone_signup_poll_interval_seconds"] = "5"
    if not all_cfg.get("chatgpt_phone_signup_max_resend_attempts"):
        all_cfg["chatgpt_phone_signup_max_resend_attempts"] = "1"
    if not all_cfg.get("chatgpt_phone_signup_resend_interval_seconds"):
        all_cfg["chatgpt_phone_signup_resend_interval_seconds"] = "60"
    if not all_cfg.get("chatgpt_phone_verification_enabled"):
        all_cfg["chatgpt_phone_verification_enabled"] = "true"
    # 只返回已知 key，未设置的返回空字符串
    return {k: all_cfg.get(k, "") for k in CONFIG_KEYS}


def _build_shareable_local_snapshot() -> dict[str, str]:
    """构造“本实例已保存本地配置”视角的共享模板候选。

    Settings 页只维护 CONFIG_KEYS，但历史/集成模块可能也通过 config_store 写入
    其他全局 key。推送或对比共享模板时必须基于本地 configs 全量快照，
    避免丢掉非页面字段，也避免把默认值/布尔展示值当成真实差异。
    """
    return filter_shareable_config(config_store.get_saved_local_all())


@router.get("")
def get_config():
    # 保持旧接口返回纯配置对象，避免破坏已有前端/任务入口。
    return _build_config_response()


@router.post("/payment-link/test")
def test_payment_link_connection(body: PaymentLinkConnectionTestRequest | None = None):
    from services.chatgpt_core.long_link_payment_client import LongLinkPaymentClient, LongLinkPaymentError

    try:
        if body is None:
            client = LongLinkPaymentClient.from_runtime_config()
        else:
            client = LongLinkPaymentClient(
                base_url=body.base_url,
                api_key=body.api_key,
                request_timeout=15,
                profile_cache_seconds=0,
            )
        profile = client.get_profile(force_refresh=True)
    except LongLinkPaymentError as exc:
        detail = str(exc)
        invalid_config = detail.startswith("未配置支付链接生成服务") or detail.startswith(
            "支付链接生成服务地址"
        ) or detail.startswith("支付链接生成服务密钥格式")
        raise HTTPException(400 if invalid_config else 502, detail) from exc

    return {
        "ok": True,
        "base_url": client.base_url,
        "api_version": client.api_version,
        "link_type": str(profile.get("link_type") or "hosted"),
        "country": str(profile.get("country") or ""),
        "currency": str(profile.get("currency") or ""),
        "effective_concurrency": int(profile.get("effective_concurrency") or 0),
        "profile_hash_prefix": str(profile.get("profile_hash") or "")[:12],
    }


@router.get("/share-state")
def get_config_share_state():
    return config_store.get_share_state()


@router.put("/share-state")
def update_config_share_state(body: ShareStateUpdate):
    return config_store.enable_shared(pull=body.pull) if body.enabled else config_store.disable_shared()


@router.post("/share/pull")
def pull_shared_config_to_instance():
    result = config_store.pull_shared_to_local()
    return {**result, "state": config_store.get_share_state()}


@router.post("/share/push")
def push_instance_config_to_shared(body: SharePushRequest):
    if not body.confirm:
        raise HTTPException(400, "需要 confirm=true 才能用当前实例配置覆盖共享模板")
    data = _build_shareable_local_snapshot()
    try:
        result = config_store.push_to_shared(
            data,
            replace=True,
            base_revision=body.base_revision,
            action="push",
            note=body.note or "instance-push",
        )
    except SharedConfigConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    # Publishing from local mode is a two-part operator action: first commit
    # the local snapshot as the new shared revision, then attach this instance
    # to that exact revision without pulling it back over the local database.
    # The opt-in flag keeps the existing push-only API behavior intact.
    if body.enable_shared:
        try:
            state = config_store.enable_shared(pull=False)
        except Exception as exc:
            raise HTTPException(
                503,
                "共享模板已更新，但当前实例切换共享失败，请刷新状态后重试",
            ) from exc
    else:
        state = config_store.get_share_state()
    return {**result, "state": state}


@router.get("/share/diff")
def diff_instance_config_with_shared():
    local = _build_shareable_local_snapshot()
    shared = shared_config_store.get_all()
    keys = sorted(set(local) | set(shared))
    diffs = []
    for key in keys:
        local_value = str(local.get(key, "") or "")
        shared_value = str(shared.get(key, "") or "")
        if local_value != shared_value:
            diffs.append({
                "key": key,
                "local_present": bool(local_value),
                "shared_present": bool(shared_value),
                "local_length": len(local_value),
                "shared_length": len(shared_value),
            })
    return {
        "ok": True,
        "state": config_store.get_share_state(),
        "diff_count": len(diffs),
        "diffs": diffs[:500],
    }


@router.get("/share/audit")
def get_shared_config_audit(limit: int = 50):
    return {"ok": True, "items": shared_config_store.audit(limit=limit)}


@router.put("")
def update_config(body: ConfigUpdate):
    # 只允许更新已知 key
    safe = {k: v for k, v in body.data.items() if k in CONFIG_KEYS}
    current_config = config_store.get_all()
    safe = normalize_dynamic_proxy_update(safe, current_config)
    safe = _normalize_local_status_probe_update(safe, current_config)
    safe = _normalize_payment_link_service_update(safe)
    if "dynamic_proxy_ip_retention_minutes" in safe:
        try:
            from core.dynamic_proxy import normalize_retention_minutes

            safe["dynamic_proxy_ip_retention_minutes"] = str(
                normalize_retention_minutes(safe.get("dynamic_proxy_ip_retention_minutes"), default=5)
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    try:
        config_store.set_many(safe, base_revision=body.base_revision)
    except SharedConfigConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "updated": list(safe.keys())}


@router.post("/applemail/import")
def import_applemail_pool(body: AppleMailImportRequest):
    from core.applemail_pool import load_applemail_pool_snapshot, save_applemail_pool_json

    pool_dir = str(body.pool_dir or config_store.get("applemail_pool_dir", "mail")).strip() or "mail"
    result = save_applemail_pool_json(
        body.content,
        pool_dir=pool_dir,
        filename=body.filename,
    )

    if body.bind_to_config:
        config_store.set_many(
            {
                "applemail_pool_dir": pool_dir,
                "applemail_pool_file": result["filename"],
            }
        )

    snapshot = load_applemail_pool_snapshot(
        pool_file=result["filename"],
        pool_dir=pool_dir,
    )

    return {
        **result,
        "pool_dir": pool_dir,
        "bound_to_config": body.bind_to_config,
        "items": snapshot["items"],
        "truncated": snapshot["truncated"],
    }


@router.get("/applemail/pool")
def get_applemail_pool_snapshot(
    pool_dir: str = "",
    pool_file: str = "",
):
    from core.applemail_pool import load_applemail_pool_snapshot

    resolved_pool_dir = str(pool_dir or config_store.get("applemail_pool_dir", "mail")).strip() or "mail"
    resolved_pool_file = str(pool_file or config_store.get("applemail_pool_file", "")).strip()
    try:
        snapshot = load_applemail_pool_snapshot(
            pool_file=resolved_pool_file,
            pool_dir=resolved_pool_dir,
        )
    except Exception:
        snapshot = {
            "filename": resolved_pool_file,
            "path": "",
            "count": 0,
            "items": [],
            "truncated": False,
        }
    return {
        **snapshot,
        "pool_dir": resolved_pool_dir,
    }
