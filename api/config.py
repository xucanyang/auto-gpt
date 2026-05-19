from fastapi import APIRouter
from pydantic import BaseModel
from core.config_store import config_store

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
    "icloud_hme_mode",
    "icloud_cookie",
    "icloud_domain_base",
    "icloud_forward_to",
    "icloud_forward_mailbox_id",
    "icloud_hme_auto_create_enabled",
    "icloud_hme_auto_create_stock_limit",
    "icloud_hme_auto_create_interval_min_minutes",
    "icloud_hme_auto_create_interval_max_minutes",
    "icloud_hme_auto_create_rate_limit_backoff_minutes",
    "tempmail_api_url",
    "tempmail_api_key",
    "tempmail_api_key_header",
    "tempmail_mode",
    "tempmail_primary_domain",
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
    "smstome_cookie",
    "smstome_country_slugs",
    "smstome_phone_attempts",
    "smstome_otp_timeout_seconds",
    "smstome_poll_interval_seconds",
    "smstome_sync_max_pages_per_country",
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
    "sub2api_api_key",
    "sub2api_group_ids",
    "chatgpt_enable_team_invite",
    "chatgpt_team_invite_deferred_activation",
    "chatgpt_capture_free_workspace",
    "chatgpt_capture_business_workspace",
    "chatgpt_existing_account_login_password",
    "chatgpt_gopay_defaults",
    "chatgpt_payment_link_defaults",
    "chatgpt_access_token_only_checkout_amount_check_enabled",
    "chatgpt_access_token_only_checkout_country",
    "chatgpt_access_token_only_checkout_currency",
    "chatgpt_access_token_only_zero_amount_stop_enabled",
    "chatgpt_access_token_only_zero_amount_stop_threshold",
    "external_subscription_api_enabled",
    "external_subscription_api_token",
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


class AppleMailImportRequest(BaseModel):
    content: str
    filename: str = ""
    pool_dir: str = ""
    bind_to_config: bool = True


@router.get("")
def get_config():
    all_cfg = config_store.get_all()
    if not all_cfg.get("mail_provider"):
        all_cfg["mail_provider"] = "luckmail"
    if not all_cfg.get("proxy_pool_cooldown_enabled"):
        all_cfg["proxy_pool_cooldown_enabled"] = "true"
    if not all_cfg.get("icloud_hme_mode"):
        all_cfg["icloud_hme_mode"] = "live"
    if not all_cfg.get("icloud_domain_base"):
        all_cfg["icloud_domain_base"] = "icloud.com"
    if not all_cfg.get("icloud_forward_to"):
        all_cfg["icloud_forward_to"] = "b@cccy.me"
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
    if not all_cfg.get("external_subscription_api_enabled"):
        all_cfg["external_subscription_api_enabled"] = "false"
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
    # 只返回已知 key，未设置的返回空字符串
    return {k: all_cfg.get(k, "") for k in CONFIG_KEYS}


@router.put("")
def update_config(body: ConfigUpdate):
    # 只允许更新已知 key
    safe = {k: v for k, v in body.data.items() if k in CONFIG_KEYS}
    config_store.set_many(safe)
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
