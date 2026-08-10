from __future__ import annotations

from datetime import datetime, timezone, timedelta
import re
from typing import Any, Callable
from urllib.parse import urlparse

from sqlmodel import Session, select

from core.base_mailbox import ManualEmailOtpMailbox, TempMailLocalMailbox
from core.base_platform import Account, AccountStatus
from core.config_store import config_store
from core.db import AccountModel, engine, save_account
from core.task_runtime import RegisterTaskControl, SkipCurrentAttemptRequested, TaskInterruption
from services.chatgpt_core.task_logging import redact_log_text, sanitize_error_message, sanitize_task_detail
from services.chatgpt_account_state import (
    apply_auth_capture_status,
    classify_chatgpt_capabilities,
    has_payment_pending_marker,
)
from services.chatgpt_core.invalid_account_recheck import (
    AT_ONLY_CLEAR_EXTRA_KEYS,
    _account_id_from_access_token,
    _append_revival_marker,
    _build_revival_marker,
    _classify_recheck_error,
    _message_for_status,
)
from services.chatgpt_core.account_fingerprint import inject_account_browser_fingerprint, persist_account_browser_fingerprint
from services.chatgpt_core.mailbox_state import (
    build_mailbox_state,
    mailbox_state_summary,
    normalize_mailbox_provider,
    sanitize_mailbox_state,
)
from services.chatgpt_core.restored_email_service import RestoredEmailService, mailbox_state_from_account
from services.chatgpt_core.local_status_refresh import (
    prepare_chatgpt_account_for_local_status_refresh,
    schedule_chatgpt_local_status_refresh_for_account_id,
)
from services.chatgpt_core.refresh_token_registration_engine import (
    EmailServiceAdapter,
    RefreshTokenRegistrationEngine,
)
from services.chatgpt_core.utils import describe_flow_state, generate_random_birthday, generate_random_name


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _log_to(log_fn: Callable[[str], None] | None, message: str) -> None:
    text = redact_log_text(str(message or '').strip())
    if not text:
        return
    if callable(log_fn):
        log_fn(text)


def _timeline_log(
    log_fn: Callable[[str], None] | None,
    message: str,
    *,
    level: str = "info",
) -> None:
    if not callable(log_fn):
        return
    try:
        log_fn(message)
    except TypeError:
        try:
            log_fn(message, level)
        except TypeError:
            log_fn(message)


def _read_bool(value: Any, *, default: bool = False) -> bool:
    if value in (None, ''):
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {'1', 'true', 'yes', 'on', 'y', '是', '开启', '允许', '启用'}:
        return True
    if text in {'0', 'false', 'no', 'off', 'n', '否', '关闭', '禁止', '禁用'}:
        return False
    return default


def _config_bool(config: dict[str, Any], key: str, *, default: bool = False) -> bool:
    if key not in config or config.get(key) in (None, ''):
        return default
    return _read_bool(config.get(key), default=default)


def _classify_custom_recheck_error(error_text: str) -> tuple[str, bool, bool | None]:
    text = str(error_text or '').strip()
    lowered = text.lower()
    if any(marker in lowered for marker in (
        'otp_rate_limited',
        'too many tries',
        'too many attempts',
        'please wait a few minutes',
    )):
        return 'otp_rate_limited', True, True
    if any(marker in lowered for marker in ('验证码', 'verification code', 'email otp', 'one-time code')) and any(
        marker in lowered for marker in ('timeout', 'timed out', '超时')
    ):
        return 'email_otp_timeout', True, None
    return _classify_recheck_error(text)


def _message_for_custom_status(status: str, raw_error: str = '', *, saved: bool = False) -> str:
    if status == 'login_alive':
        return '账号可登录，已保存到账号池' if saved else '账号可登录，未保存账号'
    if status == 'email_otp_timeout':
        return '未收到邮箱验证码，请确认邮箱可收信后重试'
    if status == 'otp_rate_limited':
        return 'OTP 校验次数过多，当前邮箱已进入冷却，稍后重试'
    return _message_for_status(status, raw_error)




def _capture_access_token_without_refresh_token(
    *,
    email: str,
    password: str,
    email_service: Any,
    exported_mailbox_state: dict[str, Any],
    merged_config: dict[str, Any],
    browser_mode: str,
    log_fn: Callable[[str], None] | None,
    proxy_url: str | None,
    task_id: str = '',
    allow_add_phone_verification: bool = False,
    allow_existing_phone_verification: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], RefreshTokenRegistrationEngine]:
    """Run the ChatGPT web registration/login path and stop after session AT capture."""

    engine_instance = RefreshTokenRegistrationEngine(
        email_service=email_service,
        proxy_url=proxy_url or None,
        callback_logger=lambda msg, level='info', *_: _log_to(log_fn, str(msg)),
        browser_mode=browser_mode,
        extra_config=merged_config,
    )
    engine_instance.email = email
    engine_instance.password = password

    email_service.create_email()
    register_client = engine_instance._build_chatgpt_client()
    oauth_client = engine_instance._build_oauth_client()
    engine_instance._reuse_register_browser_context(register_client, oauth_client)
    email_adapter = EmailServiceAdapter(email_service, email, lambda msg, level='info': _log_to(log_fn, str(msg)))
    first_name, last_name = generate_random_name()
    birthdate = generate_random_birthday()
    register_otp_wait_seconds = engine_instance._read_int_config(
        'chatgpt_register_otp_wait_seconds',
        fallback_keys=('chatgpt_otp_wait_seconds',),
        default=600,
        minimum=30,
        maximum=3600,
    )

    _log_to(log_fn, '[邮箱测活] 第一阶段：走 ChatGPT 注册/登录链路，只读取 Session access_token')
    if not register_client.visit_homepage():
        probe = dict(getattr(register_client, 'last_homepage_probe', {}) or {})
        raise RuntimeError(str(probe.get('detail') or probe.get('reason') or '访问 ChatGPT 首页失败'))
    csrf_token = register_client.get_csrf_token()
    if not csrf_token:
        raise RuntimeError('获取 ChatGPT CSRF token 失败')
    auth_url = register_client.signin(email, csrf_token)
    if not auth_url:
        raise RuntimeError('提交邮箱失败，未获取 ChatGPT authorize URL')
    final_url = register_client.authorize(auth_url)
    if not final_url:
        raise RuntimeError('ChatGPT authorize 失败')

    final_path = urlparse(final_url).path
    if 'api/accounts/authorize' in final_path or final_path == '/error':
        authorize_probe = dict(getattr(register_client, 'last_authorize_probe', {}) or {})
        authorize_status = int(authorize_probe.get('status_code') or 0)
        if final_path == '/error' or authorize_status in {403, 429}:
            raise RuntimeError(f'预授权被拦截: status={authorize_status or "-"} path={final_path}')
        _log_to(log_fn, f'[邮箱测活] 第一阶段：authorize 停在 {final_path} status={authorize_status or "-"}，继续按注册状态机推进')

    state = register_client._state_from_url(final_url)
    _log_to(log_fn, f'[邮箱测活] 第一阶段状态起点: {describe_flow_state(state)}')
    seen_states: dict[tuple[str, str, str, str], int] = {}
    register_submitted = False
    account_created = False
    made_progress = False
    referer = final_url if final_url.startswith(register_client.AUTH) else register_client.BASE

    for step in range(20):
        state_key = register_client._state_signature(state)
        seen_states[state_key] = seen_states.get(state_key, 0) + 1
        _log_to(log_fn, f'[邮箱测活] 第一阶段状态推进[{step + 1}/20]: {describe_flow_state(state)}')
        if seen_states[state_key] > 2:
            raise RuntimeError(f'注册登录状态卡住: {describe_flow_state(state)}')

        if register_client._is_registration_complete_state(state):
            register_client.last_registration_state = state
            _log_to(log_fn, '[邮箱测活] 第一阶段：ChatGPT 登录态已落地，开始读取 Session access_token')
            session_ok, session_result = register_client.reuse_session_and_get_tokens()
            exported_state = email_service.export_state()
            if not session_ok:
                raise RuntimeError(f'注册登录链路已完成，但读取 access_token 失败: {session_result}')
            tokens = dict(session_result or {})
            access_token = str(tokens.get('access_token') or '').strip()
            if not access_token:
                raise RuntimeError('注册登录链路已完成，但 ChatGPT Session 未返回 access_token')
            tokens.setdefault('source', 'registration_session')
            tokens.setdefault('mailbox_state', exported_state)
            tokens.setdefault('auth_level', 'access_token_only')
            tokens.setdefault('partial_auth', True)
            _log_to(log_fn, '[邮箱测活] 第一阶段：已从 ChatGPT Session 读取 access_token，停止第一阶段')
            return tokens, exported_state, engine_instance

        if register_client._state_is_password_registration(state):
            if register_submitted:
                raise RuntimeError('注册密码阶段重复进入')
            success, message = register_client.register_user(email, password)
            if not success:
                raise RuntimeError(f'注册失败: {message}')
            register_submitted = True
            made_progress = True
            if not register_client.send_email_otp(
                referer=state.current_url or state.continue_url or f'{register_client.AUTH}/create-account/password'
            ):
                _log_to(log_fn, '[邮箱测活] 第一阶段：发送注册验证码接口返回失败，继续等待邮箱验证码')
            state = register_client._state_from_url(f'{register_client.AUTH}/email-verification')
            referer = f'{register_client.AUTH}/create-account/password'
            continue

        if oauth_client._state_is_login_password(state):
            if password:
                next_state = oauth_client._submit_password_verify(
                    email,
                    password,
                    getattr(register_client, 'device_id', '') or '',
                    user_agent=getattr(register_client, 'ua', None),
                    sec_ch_ua=getattr(register_client, 'sec_ch_ua', None),
                    impersonate=getattr(register_client, 'impersonate', None),
                    referer=state.current_url or state.continue_url or referer,
                )
            else:
                next_state = oauth_client._send_passwordless_login_otp(
                    email,
                    getattr(register_client, 'device_id', '') or '',
                    user_agent=getattr(register_client, 'ua', None),
                    sec_ch_ua=getattr(register_client, 'sec_ch_ua', None),
                    impersonate=getattr(register_client, 'impersonate', None),
                    referer=state.current_url or state.continue_url or referer,
                )
            if not next_state:
                raise RuntimeError(oauth_client.last_error or '登录密码/验证码入口推进失败')
            made_progress = True
            referer = state.current_url or referer
            state = next_state
            continue

        if register_client._state_is_email_otp(state):
            next_state = oauth_client._handle_otp_verification(
                email,
                getattr(register_client, 'device_id', '') or '',
                getattr(register_client, 'ua', None),
                getattr(register_client, 'sec_ch_ua', None),
                getattr(register_client, 'impersonate', None),
                email_adapter,
                state,
            )
            if not next_state:
                last_error = str(oauth_client.last_error or '').strip()
                if last_error:
                    raise RuntimeError(last_error)
                raise RuntimeError('邮箱 OTP 验证后未进入下一步状态')
            made_progress = True
            referer = state.current_url or referer
            state = next_state
            register_client.last_registration_state = state
            continue

        if register_client._state_is_about_you(state):
            if account_created:
                raise RuntimeError('填写信息阶段重复进入')
            success, next_state = register_client.create_account(
                first_name,
                last_name,
                birthdate,
                return_state=True,
            )
            if not success:
                raise RuntimeError(f'创建账号失败: {next_state}')
            account_created = True
            made_progress = True
            state = next_state
            register_client.last_registration_state = state
            continue

        if oauth_client._state_is_existing_phone_otp(state):
            next_state = oauth_client._handle_existing_phone_otp_verification(
                getattr(register_client, 'device_id', '') or '',
                getattr(register_client, 'ua', None),
                getattr(register_client, 'sec_ch_ua', None),
                getattr(register_client, 'impersonate', None),
                state,
                allow_existing_phone_verification=allow_existing_phone_verification,
            )
            if not next_state:
                raise RuntimeError(oauth_client.last_error or '已绑定手机号二次验证失败')
            made_progress = True
            referer = state.current_url or referer
            state = next_state
            continue

        if oauth_client._state_is_add_phone(state):
            if not allow_add_phone_verification:
                raise RuntimeError('登录后要求 add_phone 新绑，但第一阶段不允许新增手机号')
            next_state = oauth_client._handle_add_phone_verification(
                getattr(register_client, 'device_id', '') or '',
                getattr(register_client, 'ua', None),
                getattr(register_client, 'sec_ch_ua', None),
                getattr(register_client, 'impersonate', None),
                state,
                email=email,
                sms_probe_only=False,
            )
            if not next_state:
                raise RuntimeError(oauth_client.last_error or 'add_phone 验证失败')
            made_progress = True
            referer = state.current_url or referer
            state = next_state
            continue

        if register_client._state_requires_navigation(state):
            success, next_state = register_client._follow_flow_state(
                state,
                referer=state.current_url or referer,
            )
            if not success:
                raise RuntimeError(f'注册登录跳转失败: {next_state}')
            made_progress = True
            referer = state.current_url or referer
            state = next_state
            register_client.last_registration_state = state
            continue

        if not made_progress:
            _log_to(log_fn, f'[邮箱测活] 第一阶段：未知起始状态，回退为注册密码阶段: {describe_flow_state(state)}')
            state = register_client._state_from_url(f'{register_client.AUTH}/create-account/password')
            continue

        raise RuntimeError(f'未支持的注册登录状态: {describe_flow_state(state)}')

    raise RuntimeError('注册登录状态机超出最大步数')

def _normalize_email(value: Any) -> str:
    return str(value or '').strip().lower()


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _find_chatgpt_account_in_session(
    session: Session,
    *,
    email: str = '',
    preferred_account_id: int = 0,
) -> AccountModel | None:
    preferred_account_id = int(preferred_account_id or 0)
    if preferred_account_id > 0:
        row = session.get(AccountModel, preferred_account_id)
        if row is not None and row.platform == 'chatgpt':
            return row

    target = _normalize_email(email)
    if not target:
        return None
    rows = session.exec(
        select(AccountModel)
        .where(AccountModel.platform == 'chatgpt')
        .where(AccountModel.email == email)
        .order_by(AccountModel.updated_at.desc())
    ).all()
    if not rows:
        rows = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == 'chatgpt')
            .order_by(AccountModel.updated_at.desc())
        ).all()
        rows = [row for row in rows if _normalize_email(row.email) == target]
    return rows[0] if rows else None


def _find_existing_chatgpt_account(email: str, preferred_account_id: int = 0) -> AccountModel | None:
    with Session(engine) as session:
        return _find_chatgpt_account_in_session(
            session,
            email=email,
            preferred_account_id=preferred_account_id,
        )



def _email_domain(email: str) -> str:
    normalized = _normalize_email(email)
    if '@' not in normalized:
        return ''
    return normalized.rsplit('@', 1)[-1].strip().lower().lstrip('@.')


def _normalize_tempmail_domain(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get('domain') or value.get('name') or value.get('value') or ''
    return str(value or '').strip().lower().lstrip('@.')


def _tempmail_domain_available(item: Any) -> bool:
    if isinstance(item, str):
        return bool(_normalize_tempmail_domain(item))
    if not isinstance(item, dict):
        return False
    is_active = item.get('is_active')
    if is_active is None:
        is_active = item.get('active')
    status = str(item.get('status') or ('active' if is_active is not False else 'disabled')).strip().lower()
    dns_status = str(item.get('dns_status') or '').strip().lower()
    return (
        is_active is not False
        and status in {'', 'active', 'ready', 'enabled'}
        and dns_status not in {'missing', 'error', 'failed', 'invalid'}
    )


def _extract_tempmail_domain_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ('domains', 'data', 'items'):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    nested = payload.get('data')
    if isinstance(nested, dict):
        for key in ('domains', 'items'):
            value = nested.get(key)
            if isinstance(value, list):
                return value
    return []


def _tempmail_api_proxy_from_config(global_config: dict[str, Any], task_proxy: str | None = None) -> str | None:
    # TempMail Ready 是邮箱控制面；默认不继承 ChatGPT 登录出口，避免任务代理触发 Cloudflare/403。
    # 只有显式配置邮箱 API 代理，或显式允许使用任务代理时，才走代理。
    for key in ('tempmail_proxy', 'tempmail_api_proxy', 'mailbox_proxy', 'email_proxy', 'mail_api_proxy'):
        value = str((global_config or {}).get(key) or '').strip()
        if value:
            return value
    if _read_bool((global_config or {}).get('mailbox_use_task_proxy'), default=False):
        return task_proxy
    if _read_bool((global_config or {}).get('tempmail_use_task_proxy'), default=False):
        return task_proxy
    return None


def _summarize_http_error_body(body: str) -> str:
    text = str(body or '').strip()
    if not text:
        return ''
    lowered = text.lower()
    if '<html' in lowered or '<!doctype html' in lowered:
        if 'just a moment' in lowered or 'cf-browser-verification' in lowered or 'cloudflare' in lowered:
            return 'Cloudflare 挑战页/风控拦截'
        title_match = re.search(r'<title[^>]*>(.*?)</title>', text, flags=re.I | re.S)
        if title_match:
            title = re.sub(r'\s+', ' ', title_match.group(1)).strip()
            if title:
                return f'HTML 页面: {title[:80]}'
        return 'HTML 页面响应'
    text = re.sub(r'\s+', ' ', text)
    return text[:160]


def _list_available_tempmail_domains(global_config: dict[str, Any], proxy_url: str | None = None) -> set[str]:
    api_url = str(global_config.get('tempmail_api_url') or '').strip()
    api_key = str(global_config.get('tempmail_api_key') or '').strip()
    if not api_url or not api_key:
        return set()
    mailbox = TempMailLocalMailbox(
        api_url=api_url,
        api_key=api_key,
        api_key_header=global_config.get('tempmail_api_key_header', 'Authorization'),
        mode='fixed_domain',
        proxy=_tempmail_api_proxy_from_config(global_config, proxy_url),
    )
    response = mailbox._request(
        'GET',
        '/api/domains',
        headers=mailbox._headers(),
        timeout=15,
    )
    if response.status_code != 200:
        detail = _summarize_http_error_body(response.text)
        suffix = f'，{detail}' if detail else ''
        raise RuntimeError(f'HTTP {response.status_code}{suffix}')
    payload = response.json()
    domains: set[str] = set()
    for item in _extract_tempmail_domain_items(payload):
        domain = _normalize_tempmail_domain(item)
        if domain and _tempmail_domain_available(item):
            domains.add(domain)
    return domains


def _mailbox_state_from_tempmail_domain_match(
    email: str,
    global_config: dict[str, Any],
    proxy_url: str | None,
    log_fn: Callable[[str], None] | None,
) -> dict[str, Any]:
    domain = _email_domain(email)
    if not domain:
        raise SkipCurrentAttemptRequested('邮箱格式异常，无法匹配 TempMail 域名')
    if not str(global_config.get('tempmail_api_url') or '').strip() or not str(global_config.get('tempmail_api_key') or '').strip():
        raise SkipCurrentAttemptRequested('TempMail Ready API 未配置，无法自动匹配邮箱域名')

    try:
        available_domains = _list_available_tempmail_domains(global_config, proxy_url=proxy_url)
    except Exception as exc:
        raise SkipCurrentAttemptRequested(f'TempMail 域名列表读取失败，已跳过: {exc}') from exc

    if domain not in available_domains:
        raise SkipCurrentAttemptRequested(f'TempMail 未配置可用域名 {domain}，已跳过')

    config = {}
    for key in (
        'tempmail_api_url',
        'tempmail_api_key',
        'tempmail_api_key_header',
        'tempmail_wait_timeout_seconds',
        'tempmail_ttl_minutes',
        'tempmail_reuse_window_minutes',
        'tempmail_permanent',
        'tempmail_platform',
        'tempmail_proxy',
        'tempmail_api_proxy',
        'mailbox_proxy',
        'email_proxy',
        'mail_api_proxy',
        'mailbox_use_task_proxy',
        'tempmail_use_task_proxy',
    ):
        value = global_config.get(key)
        if _has_value(value):
            config[key] = value
    config['tempmail_mode'] = 'fixed_domain'
    config['tempmail_primary_domain'] = domain
    config['tempmail_fixed_domains'] = [domain]
    config.setdefault('tempmail_api_key_header', 'Authorization')
    config.setdefault('tempmail_wait_timeout_seconds', 180)
    config.setdefault('tempmail_ttl_minutes', 30)
    config.setdefault('tempmail_reuse_window_minutes', 20)
    config.setdefault('tempmail_platform', 'chatgpt')

    if callable(log_fn):
        log_fn(f'[邮箱测活] 命中 TempMail Ready 域名: {domain}')
    return {
        'provider': 'tempmail_local',
        'email': email,
        'account': {
            'email': email,
            'account_id': '',
            'extra': {'provider': 'tempmail_local'},
        },
        'before_ids': [],
        'config': config,
        'proxy': proxy_url or '',
        'recovered_from_domain_match': True,
    }


def _mailbox_state_from_icloud_hme_alias(email: str, global_config: dict[str, Any]) -> dict[str, Any]:
    """Recover a historical HME row into the Ready + TempMail contract.

    The legacy SQLite table remains useful for audit/backfill, but this path
    never reads Apple cookies or invokes the removed direct HME client.
    ``anonymous_id`` is retained as historical metadata and is not guessed to
    be a current Helper lease.
    """
    target = _normalize_email(email)
    if not target:
        return {}
    try:
        from core.db import IcloudHmeAliasModel

        with Session(engine) as session:
            alias = session.exec(
                select(IcloudHmeAliasModel)
                .where(IcloudHmeAliasModel.hme == email)
                .where(IcloudHmeAliasModel.bound_service == 'chatgpt')
            ).first()
            if alias is None:
                # Keep this ChatGPT-only association lookup bounded.  The
                # Helper ledger, not this SQLite cache, owns cross-platform
                # logical-address availability.
                aliases = session.exec(
                    select(IcloudHmeAliasModel).where(
                        IcloudHmeAliasModel.bound_service == 'chatgpt'
                    )
                ).all()
                alias = next((row for row in aliases if _normalize_email(getattr(row, 'hme', '')) == target), None)
            if alias is None:
                return {}
            anonymous_id = str(getattr(alias, 'anonymous_id', '') or '').strip()
            forward_to = str(getattr(alias, 'forward_to', '') or '').strip()
            forward_mailbox_id = str(getattr(alias, 'forward_mailbox_id', '') or '').strip()
    except Exception:
        return {}

    config = {}
    for key in (
        'icloud_forward_to',
        'icloud_hme_helper_api_url',
        'icloud_hme_helper_internal_key',
        'icloud_hme_helper_api_key_header',
        'icloud_hme_helper_consumer',
        'icloud_hme_helper_checkout_ttl_seconds',
        'icloud_hme_helper_wait_timeout_seconds',
        'icloud_hme_helper_max_cache_age_seconds',
        'tempmail_api_url',
        'tempmail_api_key',
        'tempmail_api_key_header',
        'tempmail_wait_timeout_seconds',
    ):
        value = global_config.get(key)
        if _has_value(value):
            config[key] = value
    config['icloud_hme_mode'] = 'helper_ready_api'
    if forward_to:
        config['icloud_forward_to'] = forward_to
    else:
        config.setdefault('icloud_forward_to', str(global_config.get('icloud_forward_to') or 'b@cccy.me'))
    if not config.get('icloud_hme_helper_api_url') or not (
        config.get('icloud_hme_helper_internal_key') or config.get('icloud_hme_helper_api_key')
    ):
        return {}
    if not config.get('tempmail_api_url') or not config.get('tempmail_api_key'):
        return {}
    return {
        'provider': 'hme_ready_api',
        'email': email,
        'account': {
            'email': email,
            'account_id': anonymous_id,
            'extra': {
                'provider': 'hme_ready_api',
                'platform': 'chatgpt',
                'registration_platform': 'chatgpt',
                'source': 'legacy-icloud-hme',
                'anonymous_id': anonymous_id,
                'forward_to': str(config.get('icloud_forward_to') or '').strip(),
                'forward_mailbox_id': forward_mailbox_id,
            },
        },
        'before_ids': [],
        'config': config,
        'proxy': '',
        'recovered_from_alias': True,
    }


def _mailbox_state_from_applemail_pool(email: str, global_config: dict[str, Any]) -> dict[str, Any]:
    target = _normalize_email(email)
    if not target:
        return {}
    try:
        from core.applemail_pool import load_applemail_pool_records

        pool_file = str(global_config.get('applemail_pool_file') or '').strip()
        pool_dir = str(global_config.get('applemail_pool_dir') or 'mail').strip() or 'mail'
        pool_path, records = load_applemail_pool_records(pool_file=pool_file, pool_dir=pool_dir)
        for record in records:
            if _normalize_email(record.get('email')) != target:
                continue
            config = {
                'applemail_base_url': str(global_config.get('applemail_base_url') or 'https://www.appleemail.top'),
                'applemail_pool_file': str(pool_path.name),
                'applemail_pool_dir': pool_dir,
                'applemail_mailboxes': str(global_config.get('applemail_mailboxes') or 'INBOX,Junk'),
            }
            return {
                'provider': 'applemail',
                'email': email,
                'account': {
                    'email': str(record.get('email') or email).strip(),
                    'account_id': str(record.get('email') or email).strip(),
                    'extra': {
                        'provider': 'applemail',
                        'client_id': str(record.get('client_id') or '').strip(),
                        'refresh_token': str(record.get('refresh_token') or '').strip(),
                        'mailbox': str(record.get('mailbox') or 'INBOX').strip() or 'INBOX',
                        'pool_file': str(pool_path.name),
                    },
                },
                'before_ids': [],
                'config': config,
                'proxy': '',
                'recovered_from_pool': True,
            }
    except Exception:
        return {}
    return {}


def _resolve_custom_email_service(
    *,
    email: str,
    merged_config: dict[str, Any],
    proxy_url: str | None,
    preferred_account_id: int = 0,
    task_control: RegisterTaskControl | None,
    attempt_id: int | None,
    log_fn: Callable[[str], None] | None,
):
    existing = _find_existing_chatgpt_account(email, preferred_account_id=preferred_account_id)
    existing_state: dict[str, Any] = {}
    if existing is not None:
        existing_state = mailbox_state_from_account(existing)
        if existing_state and str(existing_state.get('provider') or '').strip() not in {'', 'manual_email_otp'}:
            if callable(log_fn):
                log_fn(f"[邮箱测活] 复用账号池邮箱通道: account_id={existing.id} provider={existing_state.get('provider')}")
            service = RestoredEmailService(
                state=existing_state,
                proxy=proxy_url,
                log_fn=lambda message, level='info': log_fn(message) if callable(log_fn) else None,
                task_control=task_control,
                attempt_id=attempt_id,
            )
            return service, existing_state

    state = _mailbox_state_from_icloud_hme_alias(email, merged_config)
    if state:
        if callable(log_fn):
            if existing_state and str(existing_state.get('provider') or '').strip() == 'manual_email_otp':
                log_fn('[邮箱测活] 账号池邮箱通道为 manual_email_otp，但当前邮箱命中 HME Ready，优先切换自动收码通道')
            log_fn(
                f"[邮箱测活] 命中 HME Ready 邮箱通道: "
                f"provider={normalize_mailbox_provider(state.get('provider'))}"
            )
        service = RestoredEmailService(
            state=state,
            proxy=proxy_url,
            log_fn=lambda message, level='info': log_fn(message) if callable(log_fn) else None,
            task_control=task_control,
            attempt_id=attempt_id,
        )
        return service, state

    state = _mailbox_state_from_applemail_pool(email, merged_config)
    if state:
        if callable(log_fn):
            if existing_state and str(existing_state.get('provider') or '').strip() == 'manual_email_otp':
                log_fn('[邮箱测活] 账号池邮箱通道为 manual_email_otp，但当前邮箱命中 AppleMail 邮箱池，优先切换自动收码通道')
            log_fn(f"[邮箱测活] 命中 AppleMail 邮箱池通道: provider={state.get('provider')}")
        service = RestoredEmailService(
            state=state,
            proxy=proxy_url,
            log_fn=lambda message, level='info': log_fn(message) if callable(log_fn) else None,
            task_control=task_control,
            attempt_id=attempt_id,
        )
        return service, state

    try:
        state = _mailbox_state_from_tempmail_domain_match(email, merged_config, proxy_url, log_fn)
    except SkipCurrentAttemptRequested:
        state = {}
    if state:
        if callable(log_fn) and existing_state and str(existing_state.get('provider') or '').strip() == 'manual_email_otp':
            log_fn('[邮箱测活] 账号池邮箱通道为 manual_email_otp，但当前邮箱命中 TempMail Ready 域名，优先切换自动收码通道')
        service = RestoredEmailService(
            state=state,
            proxy=proxy_url,
            log_fn=lambda message, level='info': log_fn(message) if callable(log_fn) else None,
            task_control=task_control,
            attempt_id=attempt_id,
        )
        return service, state

    if existing_state:
        if callable(log_fn):
            log_fn(f"[邮箱测活] 复用账号池邮箱通道: account_id={existing.id} provider={existing_state.get('provider')}")
        service = RestoredEmailService(
            state=existing_state,
            proxy=proxy_url,
            log_fn=lambda message, level='info': log_fn(message) if callable(log_fn) else None,
            task_control=task_control,
            attempt_id=attempt_id,
        )
        return service, existing_state

    state = _mailbox_state_from_tempmail_domain_match(email, merged_config, proxy_url, log_fn)
    service = RestoredEmailService(
        state=state,
        proxy=proxy_url,
        log_fn=lambda message, level='info': log_fn(message) if callable(log_fn) else None,
        task_control=task_control,
        attempt_id=attempt_id,
    )
    return service, state


class ManualTaskEmailService:
    """给 OAuth 登录探测使用的手动邮箱服务适配器。"""

    service_type = type('ST', (), {'value': 'manual_email_otp'})()

    def __init__(
        self,
        *,
        email: str,
        extra: dict[str, Any],
        proxy: str | None = None,
        task_control: RegisterTaskControl | None = None,
        attempt_id: int | None = None,
        log_fn: Callable[[str], None] | None = None,
    ):
        self.email = str(email or '').strip()
        self._acct = None
        self._before_ids: set[str] = set()
        self._mailbox = ManualEmailOtpMailbox(email=self.email, extra=extra, proxy=proxy)
        self._mailbox._task_control = task_control
        self._mailbox._task_attempt_token = attempt_id
        self._mailbox._log_fn = log_fn
        self._last_verification_result: dict[str, Any] = {}
        self._extra = dict(extra or {})
        self._proxy = proxy or ''

    def create_email(self, config=None):
        self._acct = self._mailbox.get_email()
        get_current_ids = getattr(self._mailbox, 'get_current_ids', None)
        if callable(get_current_ids):
            try:
                self._before_ids = set(get_current_ids(self._acct) or set())
            except Exception as exc:
                self._mailbox._log(f'[邮箱测活] 邮箱基线读取失败，继续等待新验证码: {exc}')
                self._before_ids = set()
        return {
            'email': self.email,
            'service_id': str(getattr(self._acct, 'account_id', '') or self.email),
            'token': '',
            'mailbox_action': str((getattr(self._acct, 'extra', None) or {}).get('mailbox_action') or 'manual'),
        }

    def get_verification_code(
        self,
        email=None,
        email_id=None,
        timeout=120,
        pattern=None,
        otp_sent_at=None,
        exclude_codes=None,
        phase=None,
        phase_label=None,
    ):
        if self._acct is None:
            self.create_email()
        code = self._mailbox.wait_for_code(
            self._acct,
            keyword='',
            timeout=int(timeout or 120),
            before_ids=self._before_ids,
            code_pattern=pattern,
            otp_sent_at=otp_sent_at,
            exclude_codes=exclude_codes,
            phase=phase,
            phase_label=phase_label,
        )
        self._last_verification_result = dict(getattr(self._mailbox, '_last_verification_result', None) or {})
        return code

    def export_state(self) -> dict[str, Any]:
        return build_mailbox_state(
            provider='manual_email_otp',
            email=self.email,
            account_email=str(getattr(self._acct, 'email', '') or self.email),
            account_id=str(getattr(self._acct, 'account_id', '') or self.email),
            account_extra=getattr(self._acct, 'extra', None) or {},
            before_ids=self._before_ids,
            config=self._extra,
            proxy=self._proxy,
        )

    def finalize_success(self, account_email: str = '', task_id: str = ''):
        finalize = getattr(self._mailbox, 'finalize_success', None)
        if callable(finalize) and self._acct is not None:
            finalize(self._acct, registered_email=str(account_email or self.email), task_id=str(task_id or ''))

    def finalize_failure(self, error_message: str = '', task_id: str = ''):
        finalize = getattr(self._mailbox, 'finalize_failure', None)
        if callable(finalize) and self._acct is not None:
            finalize(self._acct, error_message=str(error_message or ''), task_id=str(task_id or ''))

    def update_status(self, success, error=None):
        return None

    @property
    def status(self):
        return None


def _build_success_payload(
    *,
    email: str,
    task_id: str,
    saved: bool,
    saved_account_id: int = 0,
    token_account_id: str = '',
    has_access_token: bool = False,
    has_refresh_token: bool = False,
    mailbox_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        'status': 'login_alive',
        'recoverable': True,
        'retryable': False,
        'email': str(email or ''),
        'message': _message_for_custom_status('login_alive', saved=saved),
        'source': 'custom_email_recheck',
        'task_id': str(task_id or ''),
        'checked_at': _utcnow().isoformat(),
        'account_id': str(token_account_id or ''),
        'saved_account_id': int(saved_account_id or 0),
        'saved': bool(saved),
        'has_access_token': bool(has_access_token),
        'has_refresh_token': bool(has_refresh_token),
    }
    if mailbox_state:
        payload['mailbox_state'] = mailbox_state_summary(mailbox_state, account_email=email)
    return payload


def _build_failure_payload(
    *,
    status: str,
    email: str,
    task_id: str,
    raw_error: str,
    retryable: bool,
    recoverable: bool | None,
    mailbox_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        'status': str(status or 'unknown_error'),
        'recoverable': recoverable,
        'retryable': bool(retryable),
        'email': str(email or ''),
        'message': _message_for_custom_status(status, raw_error),
        'raw_error': str(raw_error or ''),
        'source': 'custom_email_recheck',
        'task_id': str(task_id or ''),
        'checked_at': _utcnow().isoformat(),
        'saved': False,
        'has_access_token': False,
        'has_refresh_token': False,
    }
    if status == 'otp_rate_limited':
        cooldown_seconds = 600
        payload['cooldown_seconds'] = cooldown_seconds
        payload['cooldown_until'] = (_utcnow() + timedelta(seconds=cooldown_seconds)).isoformat()
    if mailbox_state:
        payload['mailbox_state'] = mailbox_state_summary(mailbox_state, account_email=email)
    return payload


def _upsert_custom_email_recheck_account(
    *,
    email: str,
    password: str,
    tokens: dict[str, Any],
    oauth_client: Any,
    engine_instance: RefreshTokenRegistrationEngine,
    task_id: str,
    recheck_payload: dict[str, Any],
    mailbox_state: dict[str, Any] | None = None,
    preferred_account_id: int = 0,
    proxy_url: str | None = None,
) -> tuple[AccountModel, bool]:
    access_token = str(tokens.get('access_token') or '').strip()
    refresh_token = str(tokens.get('refresh_token') or '').strip()
    id_token = str(tokens.get('id_token') or '').strip()
    token_account_id = str(recheck_payload.get('account_id') or _account_id_from_access_token(access_token) or '').strip()
    workspace_id = str(tokens.get('workspace_id') or '').strip()
    if not workspace_id:
        try:
            workspace_id = str(engine_instance._extract_workspace_id(oauth_client) or '').strip()
        except Exception:
            workspace_id = ''
    session_token = str(tokens.get('session_token') or '').strip()
    if not session_token:
        try:
            session_token = str(engine_instance._extract_session_token(oauth_client) or '').strip()
        except Exception:
            session_token = ''
    matched_existing = False

    with Session(engine) as session:
        existing = _find_chatgpt_account_in_session(
            session,
            email=email,
            preferred_account_id=preferred_account_id,
        )
        if existing is not None:
            matched_existing = True
            extra = existing.get_extra()
            for key in AT_ONLY_CLEAR_EXTRA_KEYS:
                extra.pop(key, None)
            extra.pop('chatgpt_local', None)
            extra['access_token'] = access_token
            extra['auth_level'] = 'access_token_only'
            extra['partial_auth'] = True
            extra['chatgpt_registration_mode'] = 'access_token_only'
            extra['chatgpt_token_source'] = 'custom_email_recheck'
            extra['mail_provider'] = str((mailbox_state or {}).get('provider') or extra.get('mail_provider') or 'manual_email_otp').strip() or 'manual_email_otp'
            if refresh_token:
                extra['refresh_token'] = refresh_token
                extra['auth_level'] = 'refresh_token'
                extra['partial_auth'] = False
                extra['chatgpt_registration_mode'] = 'refresh_token'
            if id_token:
                extra['id_token'] = id_token
            if session_token:
                extra['session_token'] = session_token
            if workspace_id:
                extra['workspace_id'] = workspace_id
            if mailbox_state:
                cleaned_mailbox_state = sanitize_mailbox_state(mailbox_state, account_email=email)
                if cleaned_mailbox_state:
                    extra['chatgpt_mailbox_state'] = cleaned_mailbox_state
            revival_marker = _build_revival_marker(
                source='custom_email_recheck',
                mode='revive_existing',
                email=email,
                task_id=task_id,
                account_row_id=int(existing.id or 0),
                has_access_token=True,
                has_refresh_token=bool(refresh_token),
                auth_level='refresh_token' if refresh_token else 'access_token_only',
            )
            recheck_payload['revival_marker'] = dict(revival_marker)
            extra['chatgpt_custom_email_recheck'] = dict(recheck_payload)
            _append_revival_marker(extra, revival_marker)
            extra = persist_account_browser_fingerprint(extra, source='custom_email_recheck', overwrite=False)
            existing.email = email
            if password:
                existing.password = password
            if token_account_id:
                existing.user_id = token_account_id
            existing.token = access_token
            existing.set_extra(extra)
            apply_auth_capture_status(
                existing,
                'pending_payment' if has_payment_pending_marker(existing) else 'registered',
            )
            prepare_chatgpt_account_for_local_status_refresh(
                existing,
                reason='custom_email_recheck:update',
            )
            existing.updated_at = _utcnow()
            session.add(existing)
            session.commit()
            session.refresh(existing)
            schedule_chatgpt_local_status_refresh_for_account_id(
                existing.id,
                proxy=proxy_url or None,
                use_default_proxy=False if proxy_url else True,
                reason='custom_email_recheck:update',
                delay_seconds=2.0,
            )
            return existing, matched_existing

    extra = {
        'access_token': access_token,
        'auth_level': 'access_token_only',
        'partial_auth': True,
        'chatgpt_registration_mode': 'access_token_only',
        'chatgpt_token_source': 'custom_email_recheck',
        'chatgpt_custom_email_recheck': dict(recheck_payload),
        'mail_provider': str((mailbox_state or {}).get('provider') or 'manual_email_otp').strip() or 'manual_email_otp',
    }
    if refresh_token:
        extra['refresh_token'] = refresh_token
        extra['auth_level'] = 'refresh_token'
        extra['partial_auth'] = False
        extra['chatgpt_registration_mode'] = 'refresh_token'
    if id_token:
        extra['id_token'] = id_token
    if session_token:
        extra['session_token'] = session_token
    if workspace_id:
        extra['workspace_id'] = workspace_id
    if mailbox_state:
        cleaned_mailbox_state = sanitize_mailbox_state(mailbox_state, account_email=email)
        if cleaned_mailbox_state:
            extra['chatgpt_mailbox_state'] = cleaned_mailbox_state

    account = Account(
        platform='chatgpt',
        email=email,
        password=password,
        user_id=token_account_id,
        token=access_token,
        status=AccountStatus.REGISTERED,
        extra=extra,
    )
    extra['chatgpt_capabilities'] = classify_chatgpt_capabilities(account)
    account.extra = extra

    saved = save_account(account)
    with Session(engine) as session:
        refreshed = session.get(AccountModel, int(saved.id or 0)) if saved is not None else None
        if refreshed is None:
            raise RuntimeError('账号保存后读取失败')
        current_extra = refreshed.get_extra()
        revival_marker = _build_revival_marker(
            source='custom_email_recheck',
            mode='create_new',
            email=email,
            task_id=task_id,
            account_row_id=int(refreshed.id or 0),
            has_access_token=True,
            has_refresh_token=bool(refresh_token),
            auth_level='refresh_token' if refresh_token else 'access_token_only',
        )
        recheck_payload['revival_marker'] = dict(revival_marker)
        current_extra['chatgpt_custom_email_recheck'] = dict(recheck_payload)
        _append_revival_marker(current_extra, revival_marker)
        current_extra = persist_account_browser_fingerprint(current_extra, source='custom_email_recheck', overwrite=False)
        refreshed.set_extra(current_extra)
        apply_auth_capture_status(
            refreshed,
            'pending_payment' if has_payment_pending_marker(refreshed) else 'registered',
        )
        current_extra = refreshed.get_extra()
        refreshed.set_extra(current_extra)
        prepare_chatgpt_account_for_local_status_refresh(
            refreshed,
            reason='custom_email_recheck:create',
        )
        refreshed.updated_at = _utcnow()
        session.add(refreshed)
        session.commit()
        session.refresh(refreshed)
        schedule_chatgpt_local_status_refresh_for_account_id(
            refreshed.id,
            proxy=proxy_url or None,
            use_default_proxy=False if proxy_url else True,
            reason='custom_email_recheck:create',
            delay_seconds=2.0,
        )
        return refreshed, matched_existing


def recheck_custom_chatgpt_email(
    *,
    email: str,
    password: str = '',
    save_on_success: bool = True,
    task_id: str = '',
    log_fn: Callable[[str], None] | None = None,
    stop_checker: Callable[[], None] | None = None,
    task_control: RegisterTaskControl | None = None,
    attempt_id: int | None = None,
    proxy_url: str | None = None,
    preferred_account_id: int = 0,
    skip_access_token_probe: bool = False,
    finalize_mailbox: bool = True,
) -> dict[str, Any]:
    action_logs: list[str] = []
    saved_account_id = 0

    def _check_stop() -> None:
        if callable(stop_checker):
            stop_checker()

    def _log(message: str, level: str = 'info') -> None:
        _check_stop()
        text = redact_log_text(str(message or '').strip())
        if not text:
            return
        action_logs.append(text)
        _log_to(log_fn, text)

    def _persist_post_finalize_state(state: dict[str, Any], *account_ids: int) -> None:
        """Persist the post-finalize lease projection for saved ChatGPT rows."""

        if not save_on_success or not isinstance(state, dict) or not state:
            return
        candidates = []
        for value in account_ids:
            try:
                account_id = int(value or 0)
            except (TypeError, ValueError):
                account_id = 0
            if account_id > 0 and account_id not in candidates:
                candidates.append(account_id)
        if not candidates:
            return
        try:
            with Session(engine) as session:
                for account_id in candidates:
                    row = session.get(AccountModel, account_id)
                    if row is None:
                        continue
                    extra = row.get_extra()
                    extra['chatgpt_mailbox_state'] = dict(state)
                    row.set_extra(extra)
                    row.updated_at = _utcnow()
                    session.add(row)
                session.commit()
        except Exception as exc:
            _log(f'[邮箱测活] finalize 后 mailbox_state 写回失败: {exc}', 'warning')

    normalized_email = str(email or '').strip()
    normalized_password = str(password or '')
    if not normalized_email:
        return {
            'ok': False,
            'error': '邮箱地址不能为空',
            'data': {
                'message': '邮箱地址不能为空',
                'error_code': 'missing_email',
                'retryable': False,
                'logs': list(action_logs),
            },
        }

    merged_config = config_store.get_all().copy()
    merged_config.update({
        'manual_email_address': normalized_email,
        'chatgpt_existing_account_capture': True,
        'chatgpt_save_registration_access_token_account': True,
        'chatgpt_registration_mode': 'refresh_token',
        '_current_account_email': normalized_email,
        '_current_task_id': task_id,
    })
    if int(preferred_account_id or 0) > 0:
        merged_config['_current_account_id'] = int(preferred_account_id or 0)
    resolved_account_id = int(merged_config.get('_current_account_id') or 0)
    if resolved_account_id <= 0 and normalized_email:
        try:
            existing_for_challenge = _find_existing_chatgpt_account(normalized_email)
            if existing_for_challenge is not None and int(existing_for_challenge.id or 0) > 0:
                resolved_account_id = int(existing_for_challenge.id or 0)
                merged_config['_current_account_id'] = resolved_account_id
        except Exception:
            pass
    account_fingerprint_extra: dict[str, Any] = {}
    if resolved_account_id > 0:
        try:
            with Session(engine) as session:
                existing_account = session.get(AccountModel, resolved_account_id)
                if existing_account is not None:
                    account_fingerprint_extra = existing_account.get_extra()
        except Exception:
            account_fingerprint_extra = {}
    if account_fingerprint_extra:
        merged_config = inject_account_browser_fingerprint(merged_config, account_fingerprint_extra, overwrite=False)
    if task_control is not None:
        merged_config['_task_control'] = task_control
        merged_config['_task_attempt_id'] = attempt_id
        merged_config['_manual_phone_otp_enabled'] = True
        merged_config['_manual_phone_otp_timeout_seconds'] = 60
    allow_add_phone_verification = False
    allow_existing_phone_verification = _config_bool(
        merged_config,
        'chatgpt_recheck_allow_existing_phone_verification',
        default=True,
    )
    allow_phone_verification = bool(allow_add_phone_verification or allow_existing_phone_verification)
    browser_mode = str(
        merged_config.get('browser_mode')
        or merged_config.get('default_executor')
        or 'protocol'
    ).strip().lower() or 'protocol'

    _log(f'[邮箱测活] 开始处理：{normalized_email}')
    _log(
        '[邮箱测活] 手机验证策略：'
        f"add_phone新绑={'允许' if allow_add_phone_verification else '不允许'}，"
        f"已绑手机号二次验证={'允许' if allow_existing_phone_verification else '不允许'}"
    )
    _log('[邮箱测活] 使用 ChatGPT 登录探测；需要验证码时会在任务面板等待人工提交')

    email_service, initial_mailbox_state = _resolve_custom_email_service(
        email=normalized_email,
        merged_config=merged_config,
        proxy_url=proxy_url,
        preferred_account_id=preferred_account_id,
        task_control=task_control,
        attempt_id=attempt_id,
        log_fn=_log,
    )
    exported_mailbox_state: dict[str, Any] = dict(initial_mailbox_state or {})
    stage1_payload: dict[str, Any] | None = None
    stage1_saved_account_id = 0
    stage1_revived_existing_account = False
    followup_preferred_account_id = int(preferred_account_id or 0)
    try:
        if not skip_access_token_probe:
            _timeline_log(log_fn, '[邮箱测活] 阶段 1/2：登录测活并抓取 AccessToken')
            stage1_tokens, exported_mailbox_state, stage1_engine_instance = _capture_access_token_without_refresh_token(
                email=normalized_email,
                password=normalized_password,
                email_service=email_service,
                exported_mailbox_state=exported_mailbox_state,
                merged_config=merged_config,
                browser_mode=browser_mode,
                log_fn=_log,
                proxy_url=proxy_url,
                task_id=task_id,
                allow_add_phone_verification=allow_add_phone_verification,
                allow_existing_phone_verification=allow_existing_phone_verification,
            )
            stage1_access_token = str((stage1_tokens or {}).get('access_token') or '').strip()
            if not stage1_access_token:
                raise RuntimeError('第一阶段登录成功但未获取 access_token')

            stage1_token_account_id = _account_id_from_access_token(stage1_access_token)
            if not stage1_token_account_id:
                extract_account_info = getattr(stage1_engine_instance, '_extract_account_info', None)
                if callable(extract_account_info):
                    try:
                        stage1_token_account_id = str((extract_account_info(stage1_tokens) or {}).get('account_id') or '').strip()
                    except Exception:
                        stage1_token_account_id = ''

            stage1_payload = _build_success_payload(
                email=normalized_email,
                task_id=task_id,
                saved=False,
                token_account_id=stage1_token_account_id,
                has_access_token=True,
                has_refresh_token=bool(str((stage1_tokens or {}).get('refresh_token') or '').strip()),
                mailbox_state=exported_mailbox_state,
            )
            stage1_payload.update({
                'allow_add_phone_verification': False,
                'allow_existing_phone_verification': False,
                'followup_auth_ok': False,
            })

            if save_on_success:
                saved_account, stage1_revived_existing_account = _upsert_custom_email_recheck_account(
                    email=normalized_email,
                    password=normalized_password,
                    tokens=stage1_tokens,
                    oauth_client=None,
                    engine_instance=stage1_engine_instance,
                    task_id=task_id,
                    recheck_payload=stage1_payload,
                    mailbox_state=exported_mailbox_state,
                    preferred_account_id=preferred_account_id,
                )
                stage1_saved_account_id = int(saved_account.id or 0)
                followup_preferred_account_id = stage1_saved_account_id or followup_preferred_account_id
                if followup_preferred_account_id > 0:
                    merged_config['_current_account_id'] = followup_preferred_account_id
                stage1_payload['saved'] = True
                stage1_payload['saved_account_id'] = stage1_saved_account_id
                stage1_payload['revived_existing_account'] = bool(stage1_revived_existing_account)
                stage1_payload['created_new_account'] = not bool(stage1_revived_existing_account)
                stage1_payload['message'] = '账号可登录，已保存 access_token'
                with Session(engine) as session:
                    row = session.get(AccountModel, stage1_saved_account_id)
                    if row is not None:
                        extra = row.get_extra()
                        extra['chatgpt_custom_email_recheck'] = dict(stage1_payload)
                        extra = persist_account_browser_fingerprint(extra, source='custom_email_recheck', overwrite=False)
                        row.set_extra(extra)
                        row.updated_at = _utcnow()
                        session.add(row)
                        session.commit()
                _timeline_log(
                    log_fn,
                    f"[邮箱测活] 阶段 1/2 成功：AccessToken 已保存，account_id={stage1_saved_account_id}",
                )
            else:
                _timeline_log(log_fn, '[邮箱测活] 阶段 1/2 成功：AccessToken 已获取；按本次设置不保存账号')

            _timeline_log(log_fn, '[邮箱测活] 阶段 2/2：补抓完整 Auth/RT')

        _check_stop()
        email_service.create_email()
        email_adapter = EmailServiceAdapter(email_service, normalized_email, _log)
        engine_instance = RefreshTokenRegistrationEngine(
            email_service=email_service,
            proxy_url=proxy_url,
            callback_logger=lambda msg, level='info', *_: _log(str(msg), str(level or 'info')),
            browser_mode=browser_mode,
            extra_config=merged_config,
        )
        engine_instance.email = normalized_email
        engine_instance.password = normalized_password
        register_client = engine_instance._build_chatgpt_client()
        oauth_client = engine_instance._build_oauth_client()
        first_name, last_name = generate_random_name()
        birthdate = generate_random_birthday()

        tokens = oauth_client.login_and_get_tokens(
            normalized_email,
            normalized_password,
            device_id=getattr(register_client, 'device_id', '') or '',
            user_agent=getattr(register_client, 'ua', None),
            sec_ch_ua=getattr(register_client, 'sec_ch_ua', None),
            impersonate=getattr(register_client, 'impersonate', None),
            browser_fingerprint=getattr(register_client, 'fingerprint', None),
            skymail_client=email_adapter,
            prefer_passwordless_login=True,
            allow_phone_verification=allow_phone_verification,
            allow_add_phone_verification=allow_add_phone_verification,
            allow_existing_phone_verification=allow_existing_phone_verification,
            force_new_browser=True,
            force_chatgpt_entry=False,
            screen_hint='login',
            force_password_login=bool(normalized_password),
            complete_about_you_if_needed=True,
            first_name=first_name,
            last_name=last_name,
            birthdate=birthdate,
            login_source='custom_email_recheck',
            stop_after_login=False,
            workspace_scope_preference='free',
            allow_add_phone_session_recovery=False,
        )
        exported_mailbox_state = email_service.export_state()
        access_token = str((tokens or {}).get('access_token') or '').strip()
        if not tokens or not access_token:
            raise RuntimeError(oauth_client.last_error or 'OAuth 登录成功但未获取 access_token')

        token_account_id = _account_id_from_access_token(access_token)
        if not token_account_id:
            extract_account_info = getattr(engine_instance, '_extract_account_info', None)
            if callable(extract_account_info):
                try:
                    token_account_id = str((extract_account_info(tokens) or {}).get('account_id') or '').strip()
                except Exception:
                    token_account_id = ''

        payload = _build_success_payload(
            email=normalized_email,
            task_id=task_id,
            saved=False,
            token_account_id=token_account_id,
            has_access_token=True,
            has_refresh_token=bool(str((tokens or {}).get('refresh_token') or '').strip()),
            mailbox_state=exported_mailbox_state,
        )
        payload.update({
            'allow_add_phone_verification': allow_add_phone_verification,
            'allow_existing_phone_verification': allow_existing_phone_verification,
            'followup_auth_ok': bool(str((tokens or {}).get('refresh_token') or '').strip()),
        })
        saved_account_id = 0
        revived_existing_account = False
        if save_on_success:
            saved_account, revived_existing_account = _upsert_custom_email_recheck_account(
                email=normalized_email,
                password=normalized_password,
                tokens=tokens,
                oauth_client=oauth_client,
                engine_instance=engine_instance,
                task_id=task_id,
                recheck_payload=payload,
                mailbox_state=exported_mailbox_state,
                preferred_account_id=followup_preferred_account_id,
                proxy_url=proxy_url,
            )
            saved_account_id = int(saved_account.id or 0)
            payload['saved'] = True
            payload['saved_account_id'] = saved_account_id
            payload['revived_existing_account'] = bool(revived_existing_account)
            payload['created_new_account'] = not bool(revived_existing_account)
            payload['message'] = _message_for_custom_status('login_alive', saved=True)
            # 保存时 recheck_payload 里还没有 saved_account_id，补一遍来源结果。
            with Session(engine) as session:
                row = session.get(AccountModel, saved_account_id)
                if row is not None:
                    extra = row.get_extra()
                    extra['chatgpt_custom_email_recheck'] = dict(payload)
                    extra = persist_account_browser_fingerprint(extra, source='custom_email_recheck', overwrite=False)
                    row.set_extra(extra)
                    row.updated_at = _utcnow()
                    session.add(row)
                    session.commit()
            _timeline_log(
                log_fn,
                (
                    f"[邮箱测活] 结果：{'存活，已保存完整 Auth/RT' if bool(str((tokens or {}).get('refresh_token') or '').strip()) else '存活，已保存账号'}"
                    f"，account_id={saved_account_id}"
                ),
            )
        else:
            _timeline_log(
                log_fn,
                f"[邮箱测活] 结果：{'存活，已获取完整 Auth/RT' if bool(str((tokens or {}).get('refresh_token') or '').strip()) else '存活，未保存账号'}",
            )

        if finalize_mailbox:
            try:
                email_service.finalize_success(account_email=normalized_email, task_id=task_id)
            except Exception:
                pass
            try:
                # Finalize may replace the lease state/registration projection;
                # return the post-commit snapshot to callers instead of the
                # pre-finalize mailbox state captured above.
                exported_mailbox_state = email_service.export_state()
                payload["mailbox_state"] = exported_mailbox_state
                _persist_post_finalize_state(exported_mailbox_state, saved_account_id, stage1_saved_account_id)
            except Exception:
                pass
        return {
            'ok': True,
            'data': {
                'message': payload['message'],
                'status': payload['status'],
                'custom_email_recheck': payload,
                'token_saved': bool(save_on_success and saved_account_id),
                'saved_account_id': saved_account_id,
                'revived_existing_account': bool(save_on_success and revived_existing_account),
                'logs': list(action_logs),
            },
            'error': '',
        }
    except TaskInterruption:
        raise
    except Exception as exc:
        raw_error = sanitize_error_message(exc or '邮箱测活失败')
        if stage1_payload is not None and not skip_access_token_probe:
            payload = dict(stage1_payload)
            payload['followup_auth_ok'] = False
            payload['followup_error'] = sanitize_error_message(raw_error)
            payload['message'] = (
                '账号可登录，已保存 access_token；完整 Auth 未补全'
                if bool(payload.get('saved'))
                else '账号可登录，未保存账号；完整 Auth 未补全'
            )
            if save_on_success and stage1_saved_account_id > 0:
                with Session(engine) as session:
                    row = session.get(AccountModel, stage1_saved_account_id)
                    if row is not None:
                        extra = row.get_extra()
                        extra['chatgpt_custom_email_recheck'] = dict(payload)
                        extra = persist_account_browser_fingerprint(extra, source='custom_email_recheck', overwrite=False)
                        row.set_extra(extra)
                        row.updated_at = _utcnow()
                        session.add(row)
                        session.commit()
            if finalize_mailbox:
                try:
                    email_service.finalize_success(account_email=normalized_email, task_id=task_id)
                except Exception:
                    pass
                try:
                    exported_mailbox_state = email_service.export_state()
                    payload["mailbox_state"] = exported_mailbox_state
                    _persist_post_finalize_state(exported_mailbox_state, stage1_saved_account_id)
                except Exception:
                    pass
            _timeline_log(log_fn, f'[邮箱测活] 阶段 2/2 失败：{raw_error}；保留第一阶段结果')
            return {
                'ok': True,
                'data': {
                    'message': payload['message'],
                    'status': payload['status'],
                    'custom_email_recheck': payload,
                    'token_saved': bool(save_on_success and stage1_saved_account_id),
                    'saved_account_id': stage1_saved_account_id,
                    'revived_existing_account': bool(save_on_success and stage1_revived_existing_account),
                    'logs': list(action_logs),
                },
                'error': '',
            }
        status, retryable, recoverable = _classify_custom_recheck_error(raw_error)
        try:
            exported_mailbox_state = email_service.export_state()
        except Exception:
            exported_mailbox_state = exported_mailbox_state or {}
        payload = _build_failure_payload(
            status=status,
            email=normalized_email,
            task_id=task_id,
            raw_error=sanitize_error_message(raw_error),
            retryable=retryable,
            recoverable=recoverable,
            mailbox_state=exported_mailbox_state,
        )
        if finalize_mailbox:
            try:
                email_service.finalize_failure(error_message=sanitize_error_message(raw_error), task_id=task_id)
            except Exception:
                pass
            try:
                exported_mailbox_state = email_service.export_state()
                payload['mailbox_state'] = exported_mailbox_state
                _persist_post_finalize_state(exported_mailbox_state, saved_account_id, stage1_saved_account_id)
            except Exception:
                pass
        _timeline_log(log_fn, f'[邮箱测活] 结果：失败，{payload["message"]}')
        return {
            'ok': False,
            'error': payload['message'],
            'data': {
                'message': payload['message'],
                'error_code': status,
                'retryable': bool(retryable),
                'custom_email_recheck': payload,
                'logs': list(action_logs),
            },
        }
