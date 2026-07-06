import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from core.config_store import config_store
from core.shared_config import SharedConfigConflict, filter_shareable_config, shared_config_store

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
    "chatgpt_enable_team_invite",
    "chatgpt_team_invite_deferred_activation",
    "chatgpt_capture_free_workspace",
    "chatgpt_capture_business_workspace",
    "chatgpt_save_registration_access_token_account",
    "chatgpt_register_otp_wait_seconds",
    "chatgpt_register_otp_resend_wait_seconds",
    "chatgpt_register_otp_account_budget_seconds",
    "chatgpt_k12_enabled",
    "chatgpt_k12_workspace_ids",
    "chatgpt_k12_save_all_spaces",
    "chatgpt_k12_strict_join",
    "chatgpt_k12_join_timeout_seconds",
    "chatgpt_k12_join_retry_count",
    "chatgpt_k12_post_join_poll_seconds",
    "chatgpt_k12_capture_refresh_tokens",
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
    "chatgpt_gopay_defaults",
    "chatgpt_payment_link_defaults",
    "chatgpt_access_token_only_checkout_amount_check_enabled",
    "chatgpt_access_token_only_checkout_country",
    "chatgpt_access_token_only_checkout_currency",
    "chatgpt_access_token_only_zero_amount_stop_enabled",
    "chatgpt_access_token_only_zero_amount_stop_threshold",
    "chatgpt_access_token_only_gopay_provider_link_enabled",
    "external_subscription_api_enabled",
    "external_subscription_api_token",
    "external_subscription_verify_after_seconds",
    "external_access_token_api_enabled",
    "external_access_token_api_token",
    "external_access_token_allow_refresh",
    "external_access_token_default_lease_seconds",
    "external_access_token_max_limit",
    "external_access_token_precheck_cooldown_seconds",
    "chatgpt_gopay_billing_llm_enabled",
    "chatgpt_gopay_billing_llm_base_url",
    "chatgpt_gopay_billing_llm_api_key",
    "chatgpt_gopay_billing_llm_model",
    "chatgpt_gopay_billing_llm_wire_api",
    "chatgpt_gopay_billing_llm_country_strategy",
    "chatgpt_gopay_billing_llm_fixed_country",
    "chatgpt_gopay_billing_llm_reasoning_effort",
    "chatgpt_gopay_billing_llm_timeout_seconds",
    "chatgpt_gopay_billing_llm_prompt",
    "chatgpt_llm_api_base_url",
    "chatgpt_llm_api_key",
    "chatgpt_llm_model",
    "chatgpt_llm_timeout_seconds",
    "chatgpt_llm_billing_address_prompt",
    "chatgpt_phone_verification_enabled",
    "chatgpt_gopay_otp_auto_resend_delay_seconds",
    "chatgpt_gopay_phone_candidates",
    "chatgpt_gopay_uid_bindings",
    "chatgpt_gopay_uid_sessions",
    "chatgpt_gopay_smsforwarder_secret",
    "chatgpt_gopay_smsforwarder_recent_events",
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
        all_cfg["task_proxy_mode"] = "pool"
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
    if not all_cfg.get("chatgpt_gopay_billing_llm_enabled"):
        all_cfg["chatgpt_gopay_billing_llm_enabled"] = "true"
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
    if not all_cfg.get("chatgpt_access_token_only_gopay_provider_link_enabled"):
        all_cfg["chatgpt_access_token_only_gopay_provider_link_enabled"] = "false"
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
    if not all_cfg.get("chatgpt_gopay_billing_llm_base_url"):
        all_cfg["chatgpt_gopay_billing_llm_base_url"] = "https://api.666800.xyz"
    if not all_cfg.get("chatgpt_gopay_billing_llm_model"):
        all_cfg["chatgpt_gopay_billing_llm_model"] = "gpt-5.4"
    if not all_cfg.get("chatgpt_gopay_billing_llm_wire_api"):
        all_cfg["chatgpt_gopay_billing_llm_wire_api"] = "responses"
    if not all_cfg.get("chatgpt_gopay_billing_llm_country_strategy"):
        all_cfg["chatgpt_gopay_billing_llm_country_strategy"] = "billing_country"
    if not all_cfg.get("chatgpt_gopay_billing_llm_fixed_country"):
        all_cfg["chatgpt_gopay_billing_llm_fixed_country"] = "US"
    if not all_cfg.get("chatgpt_gopay_billing_llm_reasoning_effort"):
        all_cfg["chatgpt_gopay_billing_llm_reasoning_effort"] = "xhigh"
    if not all_cfg.get("chatgpt_gopay_billing_llm_timeout_seconds"):
        all_cfg["chatgpt_gopay_billing_llm_timeout_seconds"] = "45"
    if not all_cfg.get("chatgpt_gopay_billing_llm_prompt"):
        all_cfg["chatgpt_gopay_billing_llm_prompt"] = "生成一个真实可用的账单地址，地址在谷歌地图中能找到对应的位置。"
    if not all_cfg.get("chatgpt_gopay_billing_llm_api_key") and all_cfg.get("chatgpt_llm_api_key"):
        all_cfg["chatgpt_gopay_billing_llm_api_key"] = all_cfg.get("chatgpt_llm_api_key") or ""
    if not all_cfg.get("chatgpt_llm_api_base_url"):
        all_cfg["chatgpt_llm_api_base_url"] = all_cfg.get("chatgpt_gopay_billing_llm_base_url") or "https://api.666800.xyz/"
    if not all_cfg.get("chatgpt_llm_model"):
        all_cfg["chatgpt_llm_model"] = all_cfg.get("chatgpt_gopay_billing_llm_model") or "gpt-5.4"
    if not all_cfg.get("chatgpt_llm_timeout_seconds"):
        all_cfg["chatgpt_llm_timeout_seconds"] = all_cfg.get("chatgpt_gopay_billing_llm_timeout_seconds") or "45"
    if not all_cfg.get("chatgpt_llm_billing_address_prompt"):
        all_cfg["chatgpt_llm_billing_address_prompt"] = all_cfg.get("chatgpt_gopay_billing_llm_prompt") or ""
    if not all_cfg.get("chatgpt_phone_verification_enabled"):
        all_cfg["chatgpt_phone_verification_enabled"] = "true"
    if not all_cfg.get("chatgpt_gopay_otp_auto_resend_delay_seconds"):
        all_cfg["chatgpt_gopay_otp_auto_resend_delay_seconds"] = "10"
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
    return {**result, "state": config_store.get_share_state()}


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
    dynamic_template = str(safe.get("dynamic_proxy_template") or "").strip()
    dynamic_country = str(safe.get("dynamic_proxy_default_country") or "").strip().upper()
    if "dynamic_proxy_ip_retention_minutes" in safe:
        try:
            from core.dynamic_proxy import normalize_retention_minutes

            safe["dynamic_proxy_ip_retention_minutes"] = str(
                normalize_retention_minutes(safe.get("dynamic_proxy_ip_retention_minutes"), default=5)
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if dynamic_template:
        safe.setdefault("task_proxy_mode", "dynamic")
        safe.setdefault("task_proxy_url", dynamic_template)
    if dynamic_country:
        safe.setdefault("task_proxy_country_code", dynamic_country)
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
