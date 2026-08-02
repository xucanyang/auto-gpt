import asyncio
import json

from sqlmodel import SQLModel, Session, create_engine, select

from core.db import TaskLog
from services.chatgpt_core.task_logging import (
    REDACTION_VERSION,
    REDACTED,
    REDACTED_OTP,
    REDACTED_TOKEN,
    build_task_current_state,
    classify_task_log_level,
    format_task_timeline_log,
    format_http_trace_log,
    infer_registration_timeline_stage,
    mask_email_for_log,
    mask_phone_for_log,
    redact_log_text,
    redact_proxy_url,
    redact_raw_email_api_line,
    redact_raw_phone_line,
    redact_url,
    sanitize_error_message,
    sanitize_phone_item,
    sanitize_phone_result,
    sanitize_task_detail,
)


def test_redact_proxy_url_keeps_endpoint_but_removes_credentials():
    assert redact_proxy_url("http://user:pass@1.2.3.4:8000") == "http://***:***@1.2.3.4:8000"
    assert redact_proxy_url("socks5://1.2.3.4:1080") == "socks5://1.2.3.4:1080"


def test_redact_url_removes_query_and_fragment():
    safe = redact_url("https://api.example.com/sms/get?token=abc&key=xyz&id=1#frag")
    assert safe == "https://api.example.com/sms/get"
    assert "token" not in safe and "key" not in safe and "abc" not in safe


def test_redact_log_text_covers_otp_tokens_password_cookie_proxy_and_sms_url():
    raw = "\n".join(
        [
            "收到验证码 123456",
            "尝试 OTP: 654321",
            "验证 OTP 码: 333222",
            "准备提交手机号验证码: 111222",
            "authorization code: abcdef1234567890...",
            "CSRF token: deadbeefcafebabe1234...",
            "Authorization: Bearer eyJabcdefghijk.abcdefghijklmnop.abcdefghijklmnop",
            "accessToken: at-secret sessionToken=st-secret refresh_token=rt-secret id_token=it-secret",
            "Cookie: oai-client-auth-session=secret; other=x",
            "password=super-secret",
            "邮箱: demo@example.com, 密码: cn-secret",
            "proxy http://user:pass@1.2.3.4:8000",
            "+15551234567----https://sms.example.com/get?token=secret&key=abc",
        ]
    )
    safe = redact_log_text(raw)
    assert "123456" not in safe
    assert "654321" not in safe
    assert "333222" not in safe
    assert "111222" not in safe
    assert "abcdef1234567890" not in safe
    assert "deadbeefcafebabe1234" not in safe
    assert "eyJabcdefghijk" not in safe
    assert "at-secret" not in safe
    assert "st-secret" not in safe
    assert "rt-secret" not in safe
    assert "it-secret" not in safe
    assert "super-secret" not in safe
    assert "cn-secret" not in safe
    assert "user:pass@" not in safe
    assert "token=secret" not in safe and "key=abc" not in safe
    assert REDACTED_OTP in safe
    assert REDACTED_TOKEN in safe
    assert REDACTED in safe
    assert "http://***:***@1.2.3.4:8000" in safe


def test_classify_task_log_level_sends_low_level_register_noise_to_debug():
    debug_lines = [
        "访问 ChatGPT 首页...",
        "获取 CSRF token...",
        "CSRF token: deadbeefcafebabe1234...",
        "提交邮箱: demo@example.com",
        "获取到 authorize URL",
        "访问 authorize URL...",
        "重定向到: https://auth.openai.com/email-verification",
        "Sentinel Browser 阶段: launch chromium",
        "follow -> 200 https://chatgpt.com/",
        "注册状态推进: step=1 state=page=email_otp_verification method=GET next=https://auth.openai.com/email-verification",
        "验证码发送状态: 200",
        "验证 OTP 码: 123456",
        "create_account: 已生成 sentinel token",
    ]
    for line in debug_lines:
        assert classify_task_log_level(line, flow="access_token_register") == "debug"

    assert classify_task_log_level("[账号] -------- 尝试 1 / 目标成功 1 / 当前成功数 0 --------", flow="access_token_register") == "info"
    assert classify_task_log_level("[验证码] 等待邮箱验证码：注册阶段邮箱验证码 timeout=600s", flow="access_token_register") == "info"
    assert classify_task_log_level("[路由] stage=after_email page=login_password action=existing_account", flow="access_token_register") == "info"
    assert classify_task_log_level("[已有账号] stage=about_you action=skip slot=0", flow="access_token_register") == "info"
    assert classify_task_log_level("[FAIL] 注册失败: 验证码失败: HTTP 403", flow="access_token_register") == "info"
    assert classify_task_log_level("显式 debug", "debug", flow="access_token_register") == "debug"
    assert classify_task_log_level("显式 warning", "warning", flow="access_token_register") == "warning"


def test_classify_task_log_level_keeps_phone_binding_summary_visible():
    assert classify_task_log_level("[手机号绑定] 同号连续绑定继续: +1555***0123", flow="phone_binding") == "info"
    assert classify_task_log_level("[手机号池] 已回写号码状态：绑定成功", flow="phone_binding") == "info"
    assert classify_task_log_level("[SUMMARY] OpenAI 手机号绑定完成：成功 2/3", flow="phone_binding") == "info"
    assert classify_task_log_level("[号码测试] OpenAI 已接受并发送验证码: +15555550123", flow="phone_binding") == "debug"
    assert classify_task_log_level("phone-otp/send -> 200 response={}", flow="phone_binding") == "debug"
    assert classify_task_log_level("callback -> /api/auth/callback", flow="phone_binding") == "debug"


def test_raw_line_and_phone_helpers_do_not_expose_sms_api_secret():
    safe = redact_raw_phone_line("+15551234567----https://sms.example.com/get?token=secret")
    assert safe.startswith("+1555")
    assert "***" in safe
    assert "token=secret" not in safe
    assert safe.endswith("https://sms.example.com/get")
    assert mask_phone_for_log("+15551234567") != "+15551234567"


def test_mask_email_for_log_preserves_domain_and_compacts_local_part():
    masked = mask_email_for_log("ahem.oafs.55+gpt1@icloud.com")
    assert masked == "ahe***1@icloud.com"


def test_raw_email_api_line_keeps_email_and_endpoint_but_removes_url_secrets():
    raw = "demo@example.com----https://mail-api.example.com/path?token=secret&id=1#frag"

    safe = redact_raw_email_api_line(raw)

    assert safe == "demo@example.com----https://mail-api.example.com/path"
    assert "demo@example.com" in safe
    assert "https://mail-api.example.com/path" in safe
    assert "token=secret" not in safe
    assert "secret" not in safe
    assert "id=1" not in safe
    assert "?" not in safe
    assert "#frag" not in safe


def test_redact_log_text_masks_phone_after_verification_context_without_otp_misfire():
    safe = redact_log_text("OpenAI 已接受并发送验证码: +13434832954")
    assert "+1343***2954" in safe
    assert "+13434832954" not in safe
    assert REDACTED_OTP not in safe


def test_redact_log_text_can_expose_phone_without_treating_it_as_otp():
    phone = "+13434832954"
    safe = redact_log_text(
        f"OpenAI 已接受并发送验证码: {phone} otp=123456",
        expose_phone=True,
    )

    assert phone in safe
    assert "123456" not in safe
    assert REDACTED_OTP in safe


def test_redact_log_text_covers_free_text_code_and_api_secret_snippets():
    raw = "\n".join(
        [
            "code=123456",
            '{"code":"654321"}',
            "收码 API 响应不是 JSON: 111222",
            '收码 API 响应不是 JSON: {"otp":"333444"}',
            "api_key=abc123xyz999",
            "apikey: abc123xyz999",
            "secret=abc123xyz999",
            "api_secret=abc123xyz999",
            "client_secret=abc123xyz999",
        ]
    )
    safe = redact_log_text(raw)
    for leaked in ("123456", "654321", "111222", "333444", "abc123xyz999"):
        assert leaked not in safe
    assert REDACTED_OTP in safe
    assert REDACTED_TOKEN in safe


def test_sanitize_task_detail_recurses_without_breaking_shape():
    detail = {
        "access_token": "at-secret",
        "has_access_token": True,
        "phone_count": 1,
        "prefix4": "1555",
        "settings": {"proxy": "http://user:pass@1.2.3.4:8000"},
        "runtime_results": [
            {
                "phone": "+15551234567",
                "api_url": "https://sms.example.com/get?token=secret",
                "raw_line": "+15551234567----https://sms.example.com/get?token=secret",
                "reason": "Authorization: Bearer eyJabcdefghijk.abcdefghijklmnop.abcdefghijklmnop",
                "code_received": True,
            }
        ],
        "logs": ["收到验证码 654321"],
        "mailbox_state": {
            "email": "demo@example.com",
            "provider": "icloud_hme",
            "before_ids": [1, 2, 3],
            "raw_message": "验证码 654321",
            "token": "mailbox-secret",
        },
    }
    safe = sanitize_task_detail(detail)
    assert safe["access_token"] == REDACTED_TOKEN
    assert safe["has_access_token"] is True
    assert safe["phone_count"] == 1
    assert safe["prefix4"] == "1555"
    assert isinstance(safe["runtime_results"], list)
    first = safe["runtime_results"][0]
    assert first["phone"] == "+15551234567"
    assert first["api_url"] == "https://sms.example.com/get"
    assert "token=secret" not in first["raw_line"]
    assert REDACTED_TOKEN in first["reason"]
    assert safe["mailbox_state"]["has_state"] is True
    assert safe["mailbox_state"]["before_count"] == 3
    dumped = json.dumps(safe, ensure_ascii=False)
    assert "654321" not in dumped
    assert "mailbox-secret" not in dumped


def test_sanitize_task_detail_preserves_proxy_mapping_shape():
    safe = sanitize_task_detail(
        {
            "proxy": {
                "mode": "specified",
                "specified": "http://user:pass@1.2.3.4:8000",
                "nested": {"proxy_url": "socks5://user:pass@5.6.7.8:1080"},
            }
        }
    )
    assert isinstance(safe["proxy"], dict)
    assert safe["proxy"]["specified"] == "http://***:***@1.2.3.4:8000"
    assert safe["proxy"]["nested"]["proxy_url"] == "socks5://***:***@5.6.7.8:1080"


def test_sanitize_task_detail_covers_structured_api_secret_keys():
    safe = sanitize_task_detail(
        {
            "api_key": "abc123xyz999",
            "apikey": "abc123xyz999",
            "x_api_key": "abc123xyz999",
            "x-api-key": "abc123xyz999",
            "api_secret": "abc123xyz999",
            "secret": "abc123xyz999",
            "client_secret": "abc123xyz999",
            "params": {
                "api_secret": "abc123xyz999",
                "x_api_key": "abc123xyz999",
            },
        }
    )
    dumped = json.dumps(safe, ensure_ascii=False)
    assert "abc123xyz999" not in dumped
    assert dumped.count(REDACTED_TOKEN) >= 8


def test_sanitize_task_detail_redacts_email_api_line_fields_without_losing_identity():
    raw_line = "demo@example.com----https://mail-api.example.com/path?token=secret&id=1#frag"
    raw_lines = "\n".join(
        [
            raw_line,
            "second@example.com----https://mail-api.example.com/second?token=other&id=2#tail",
        ]
    )

    safe = sanitize_task_detail(
        {
            "email_api_line": raw_line,
            "nested": {
                "email_api_lines": raw_lines,
            },
        }
    )

    assert safe["email_api_line"] == "demo@example.com----https://mail-api.example.com/path"
    assert safe["nested"]["email_api_lines"].splitlines() == [
        "demo@example.com----https://mail-api.example.com/path",
        "second@example.com----https://mail-api.example.com/second",
    ]

    dumped = json.dumps(safe, ensure_ascii=False)
    assert "demo@example.com" in dumped
    assert "second@example.com" in dumped
    assert "https://mail-api.example.com/path" in dumped
    assert "https://mail-api.example.com/second" in dumped
    for leaked in ("token=secret", "token=other", "secret", "other", "id=1", "id=2", "#frag", "#tail"):
        assert leaked not in dumped
    assert "?" not in dumped
    assert "#" not in dumped


def test_sanitize_phone_item_and_result_return_display_copies():
    item = {
        "phone": "+15551234567",
        "api_url": "https://sms.example.com/get?token=secret",
        "raw_line": "+15551234567----https://sms.example.com/get?token=secret",
        "proxy": "http://user:pass@1.2.3.4:8000",
        "reason": "验证码 123456",
    }
    safe_item = sanitize_phone_item(item)
    safe_result = sanitize_phone_result(item)
    assert item["api_url"].endswith("token=secret")  # original untouched
    for safe in (safe_item, safe_result):
        assert safe["phone"] == "+15551234567"
        assert safe["phone_masked"] != "+15551234567"
        assert safe["api_url"] == "https://sms.example.com/get"
        assert "token=secret" not in safe["raw_line"]
        assert safe["proxy"] == "http://***:***@1.2.3.4:8000"
        assert "123456" not in safe["reason"]


def test_format_task_timeline_log_builds_stable_human_timeline():
    line = format_task_timeline_log(
        "邮箱测活",
        "开始执行",
        item_index=5,
        item_total=74,
        email="demo@example.com",
        stage_index=1,
        stage_total=2,
        phase_label="登录测活并抓取 AccessToken",
    )
    assert line == "[邮箱测活][5/74][demo@example.com] 阶段 1/2：登录测活并抓取 AccessToken；开始执行"


def test_format_phone_binding_info_shows_full_identity_but_masks_otp():
    email = "operator.account+gpt1@icloud.com"
    phone = "+12269023179"
    line = format_task_timeline_log(
        "手机绑定",
        (
            "状态：已获取｜验证码=123456，"
            f"手机号={mask_phone_for_log(phone)}，邮箱={mask_email_for_log(email)}"
        ),
        email=email,
        phone=phone,
        stage_index=4,
        stage_total=12,
        phase_label="邮箱验证",
    )
    assert line.startswith("[手机号绑定][步骤04/12 邮箱验证]")
    assert "[手机绑定]" not in line
    assert "已获取" in line
    assert "验证码=[REDACTED_OTP]" in line
    assert "123456" not in line
    assert phone in line
    assert email in line
    assert mask_phone_for_log(phone) not in line
    assert mask_email_for_log(email) not in line


def test_format_phone_binding_debug_masks_identity_and_strong_secrets():
    email = "operator.account+gpt1@icloud.com"
    phone = "+12269023179"
    line = format_task_timeline_log(
        "手机绑定",
        (
            f"邮箱={email}，手机号={phone}，验证码=123456，"
            "token=token-secret password=password-secret"
        ),
        email=email,
        phone=phone,
        stage_index=4,
        stage_total=12,
        phase_label="邮箱验证",
        debug=True,
    )

    assert email not in line
    assert phone not in line
    assert mask_email_for_log(email) in line
    assert mask_phone_for_log(phone) in line
    assert "123456" not in line
    assert "token-secret" not in line
    assert "password-secret" not in line


def test_format_registration_timeline_log_keeps_detailed_stable_fields():
    message = (
        "[代理] candidate=1/2 source=specified "
        "proxy=http://proxy.local:8080 (出口IP: 198.51.100.10)"
    )
    stage_index, phase_label = infer_registration_timeline_stage(message)

    line = format_task_timeline_log(
        "ChatGPT注册",
        message,
        item_index=1,
        item_total=3,
        email="detailed.account+gpt1@example.com",
        stage_index=stage_index,
        stage_total=9,
        phase_label=phase_label,
    )

    assert line.startswith("[1/3][步骤02/09 选择代理] 已选择")
    assert "[ChatGPT注册]" not in line
    assert "[尝试" not in line
    assert "det***1@example.com" not in line
    assert "候选=1/2" in line
    assert "来源=指定代理" in line
    assert "代理=http://proxy.local:8080" in line
    assert "出口IP=198.51.100.10" in line

    dynamic_line = format_task_timeline_log(
        "ChatGPT注册",
        "[代理] candidate=1/2 source=dynamic country=JP actual=unverified "
        "provider=dynamic sid=refreshed retention=t-120 probe=disabled",
        item_index=1,
        item_total=3,
        email="detailed.account+gpt1@example.com",
        stage_index=2,
        stage_total=9,
        phase_label="选择代理",
    )
    assert "目标国家=JP" in dynamic_line
    assert "实际国家=未验证" in dynamic_line
    assert "SID=已刷新" in dynamic_line
    assert "IP保留=120分钟" in dynamic_line

    safe_line = redact_log_text(
        "代理=http://proxy.local:8080｜出口IP=198.51.100.10"
    )
    assert safe_line == "代理=http://proxy.local:8080｜出口IP=198.51.100.10"


def test_format_registration_timeline_masks_mailbox_details_without_dropping_them():
    message = (
        "[iCloudHME] Helper 已领取别名: detailed.account+gpt1@icloud.com "
        "lease=ck_registration_123，监听转发箱 forward@example.net mailbox_id=mailbox-1"
    )
    stage_index, phase_label = infer_registration_timeline_stage(message)

    line = format_task_timeline_log(
        "ChatGPT注册",
        message,
        item_index=1,
        item_total=3,
        email="detailed.account+gpt1@icloud.com",
        stage_index=stage_index,
        stage_total=9,
        phase_label=phase_label,
    )

    assert "[步骤03/09 领取邮箱] 已领取" in line
    assert "别名=det***1@icloud.com" in line
    assert "租约=ck_registration_123" in line
    assert "监听转发箱=for***d@example.net" in line
    assert "邮箱ID=mailbox-1" in line
    assert "detailed.account+gpt1@icloud.com" not in line
    assert "forward@example.net" not in line


def test_format_registration_result_and_summary_preserve_operator_semantics():
    result_line = format_task_timeline_log(
        "ChatGPT注册",
        "[结果] outcome=FAILED code=registration_failed reason=proxy timeout "
        "mailbox=finalized slot=1 backfill=no certainty=unknown progress=0/1",
        item_index=1,
        item_total=3,
        stage_index=9,
        stage_total=9,
        phase_label="完成",
    )
    summary_line = format_task_timeline_log(
        "ChatGPT注册",
        "完成: 成功 1 个, 跳过 0 个, 失败 1 个; "
        "Plus checkout amount=0: 0 个, amount!=0: 1 个",
        stage_index=9,
        stage_total=9,
        phase_label="完成",
    )

    assert "[步骤09/09 完成] 失败" in result_line
    assert "原因码=registration_failed" in result_line
    assert "原因=proxy timeout" in result_line
    assert "占用目标=是" in result_line
    assert "补位=否" in result_line
    assert "确定性=未知" in result_line
    assert "成功=1｜跳过=0｜失败=1" in summary_line
    assert "amount!=0: 1" in summary_line


def test_registration_timeline_formats_each_info_event_without_collapsing_density():
    events = [
        "[账号] target=1 current_success=0 executor=headless status=started",
        "[邮箱] mail_provider=hme_ready_api",
        "[验证码] 等待邮箱验证码：浏览器邮箱验证码 timeout=120s",
        "[注册] about_you 资料已提交｜HTTP=200",
        "[登录] ChatGPT Web Session 获取成功｜AT=是｜Session=是｜Cookie状态=已获取",
        "[结果] outcome=SUCCESS code=success mailbox=success slot=0 backfill=no certainty=known progress=1/1",
    ]
    lines = []
    for event in events:
        stage_index, phase_label = infer_registration_timeline_stage(event)
        lines.append(
            format_task_timeline_log(
                "ChatGPT注册",
                event,
                item_index=1,
                item_total=3,
                email="density@example.com",
                stage_index=stage_index,
                stage_total=9,
                phase_label=phase_label,
            )
        )

    assert len(lines) == len(events)
    assert all(line.startswith("[1/3]") for line in lines)
    assert all("[ChatGPT注册]" not in line and "[尝试" not in line for line in lines)
    assert all("｜" in line for line in lines)


def test_registration_prefix_uses_success_slot_and_debug_is_network_only():
    info = format_task_timeline_log(
        "ChatGPT注册",
        "[邮箱] 邮箱已获取｜邮箱=sta1231@icloud.com｜渠道=HME Helper API",
        success_slot=2,
        success_total=3,
        stage_index=3,
        stage_total=9,
        phase_label="领取邮箱",
    )
    debug = format_task_timeline_log(
        "ChatGPT注册",
        format_http_trace_log(
            "POST",
            "https://auth.openai.com/api/accounts/email-otp/validate?token=secret",
            status=200,
            duration_ms=842,
            page="about_you",
            resource_type="xhr",
        ),
        success_slot=2,
        success_total=3,
        stage_index=5,
        stage_total=9,
        phase_label="邮箱验证",
        debug=True,
    )
    assert info.startswith("[2/3][步骤03/09 领取邮箱]")
    assert "邮箱=sta***1@icloud.com" in info
    assert "[ChatGPT注册]" not in info and "[尝试" not in info
    assert debug.startswith("[DEBUG][2/3][步骤05/09 邮箱验证] [HTTP] POST auth.openai.com/api/accounts/email-otp/validate")
    assert "token=secret" not in debug
    assert "any-auto" not in debug and "headless" not in debug


def test_registration_otp_summary_never_contains_plain_code():
    line = format_task_timeline_log(
        "ChatGPT注册",
        "[验证码] 验证码已收到｜邮箱=sta1231@icloud.com｜长度=6｜等待=18秒｜来源=注册邮箱｜重发次数=1",
        success_slot=1,
        success_total=3,
        stage_index=5,
        stage_total=9,
        phase_label="邮箱验证",
    )
    submitted = format_task_timeline_log(
        "ChatGPT注册",
        "[验证码] 验证码已提交｜长度=6｜HTTP=200｜下一页=about_you",
        success_slot=1,
        success_total=3,
        stage_index=5,
        stage_total=9,
        phase_label="邮箱验证",
    )
    assert "长度=6" in line and "等待=18秒" in line and "来源=注册邮箱" in line
    assert "长度=6" in submitted and "HTTP=200" in submitted and "下一页=about_you" in submitted
    assert "123456" not in line and "123456" not in submitted


def test_http_trace_and_timeline_formatter_strip_transport_secrets_without_outer_logger():
    trace = format_http_trace_log(
        "POST",
        "https://user:password@auth.openai.com/api/accounts/email-otp/validate?token=secret#fragment",
        status=200,
        duration_ms=12,
    )
    line = format_task_timeline_log(
        "ChatGPT注册",
        "[验证码] code=123456",
        success_slot=1,
        success_total=1,
        stage_index=5,
        stage_total=9,
        phase_label="邮箱验证",
    )
    assert trace == "[HTTP] POST auth.openai.com/api/accounts/email-otp/validate -> 200 12ms"
    assert "user:password" not in trace
    assert "token=secret" not in trace
    assert "123456" not in line


def test_registration_auto_upload_gate_uses_skip_not_fail(monkeypatch):
    from api import tasks
    from services import external_sync

    monkeypatch.setattr(
        external_sync,
        "sync_account",
        lambda _account: [
            {
                "name": "Upload Gate",
                "ok": False,
                "msg": "跳过上传：待支付/仅 AT 账号缺少 refresh_token",
            }
        ],
    )
    emitted = []

    tasks._auto_upload_integrations(
        "task-registration-upload-skip",
        type("Account", (), {"id": 5694})(),
        log_fn=lambda message, level="info": emitted.append((level, message)),
    )

    assert emitted == [
        ("info", "[Auto Upload] 开始自动同步外部系统，inventory_id=5694"),
        (
            "info",
            "[Upload Gate] [SKIP] 跳过上传：待支付/仅 AT 账号缺少 refresh_token",
        ),
    ]
    assert all("[FAIL]" not in message for _level, message in emitted)


def test_build_task_current_state_masks_phone_and_keeps_stage_fields():
    current = build_task_current_state(
        task="phone_binding_test",
        task_label="手机绑定",
        item_index=2,
        item_total=10,
        email="",
        account_id=123,
        phone="+15551234567",
        phase="phone_sms_wait",
        phase_label="等待短信验证码",
        stage_index=3,
        stage_total=4,
        last_message="OpenAI 已发短信",
        next_step="提交验证码",
        resource_touched=True,
        started_at="2026-06-21T12:00:00Z",
    )
    assert current["task"] == "phone_binding_test"
    assert current["task_label"] == "手机绑定"
    assert current["item_index"] == 2
    assert current["item_total"] == 10
    assert current["account_id"] == 123
    assert current["phone"] == "+1555***4567"
    assert current["phase"] == "phone_sms_wait"
    assert current["phase_label"] == "等待短信验证码"
    assert current["stage_index"] == 3
    assert current["stage_total"] == 4
    assert current["resource_touched"] is True


def test_enqueue_phone_binding_keeps_runtime_raw_but_meta_safe(monkeypatch):
    import api.tasks as tasks

    class _BackgroundTasks:
        def __init__(self):
            self.calls = []

        def add_task(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    class _FakeRepo:
        def get(self, phone):
            return None

        def add(self, *, phone, api_url, label='', max_accounts=3, api_expired_date=''):
            return type('Record', (), {'id': 42})()

    captured = {}

    def _fake_create_task_record(_task_id, *, platform, source, total, meta):
        captured['meta'] = meta
        tasks._task_store.create(_task_id, platform=platform, total=total, source=source, meta=meta)

    monkeypatch.setattr(
        tasks,
        '_resolve_phone_binding_test_accounts',
        lambda _req: ([{'account_id': 1, 'email': 'demo@example.com', 'status': 'pending_payment'}], [], [], []),
    )
    monkeypatch.setattr(tasks, '_create_standalone_task_record', _fake_create_task_record)
    monkeypatch.setattr(tasks, '_save_task_log', lambda *args, **kwargs: None)

    import services.chatgpt_core.phone_pool_repository as phone_pool_module

    monkeypatch.setattr(phone_pool_module, 'PhonePoolRepository', lambda: _FakeRepo())

    background_tasks = _BackgroundTasks()
    req = tasks.PhoneBindingTestTaskRequest(
        account_ids=[1],
        phone_lines='+15551234567----https://sms.example.com/get?token=secret&key=abc',
        proxy='http://user:pass@1.2.3.4:8000',
        proxy_mode='specified',
        proxy_failover=False,
    )
    result = tasks.enqueue_phone_binding_test_task(req, background_tasks=background_tasks)

    assert result['task_id']
    assert len(background_tasks.calls) == 1
    runtime_phone_items = background_tasks.calls[0][0][3]
    runtime_settings = background_tasks.calls[0][0][4]
    assert runtime_phone_items[0]['api_url'].endswith('token=secret&key=abc')
    assert runtime_phone_items[0]['raw_line'].endswith('token=secret&key=abc')
    assert runtime_settings['proxy'] == 'http://user:pass@1.2.3.4:8000'

    meta = captured['meta']
    assert meta['settings']['proxy'] == 'http://***:***@1.2.3.4:8000'
    assert meta['proxy']['specified'] == 'http://***:***@1.2.3.4:8000'
    assert meta['phone_items'][0]['api_url'] == 'https://sms.example.com/get'
    assert 'token=secret' not in meta['phone_items'][0]['raw_line']


def test_api_tasks_log_and_save_task_log_are_redaction_backstops(monkeypatch, tmp_path):
    import api.tasks as tasks

    task_id = "task_logging_redaction_unit"
    tasks._task_store.create(task_id, platform="chatgpt", total=1, source="unit", meta={})
    tasks._log(
        task_id,
        "收到验证码 123456 proxy=http://user:pass@1.2.3.4:8000 raw=+15551234567----https://sms.example.com/get?token=secret",
    )
    snapshot = tasks._task_store.snapshot(task_id)
    joined_logs = "\n".join(snapshot.get("logs") or [])
    assert "123456" not in joined_logs
    assert "user:pass@" not in joined_logs
    assert "token=secret" not in joined_logs
    assert "http://***:***@1.2.3.4:8000" in joined_logs

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'task_logs.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr(tasks, "engine", test_engine)

    raw_detail = {
        "task_id": task_id,
        "source": "unit",
        "meta": {
            "access_token": "at-secret",
            "settings": {"proxy": "http://user:pass@1.2.3.4:8000"},
            "phone_items": [
                {
                    "phone": "+15551234567",
                    "api_url": "https://sms.example.com/get?token=secret",
                    "raw_line": "+15551234567----https://sms.example.com/get?token=secret",
                }
            ],
        },
        "logs": ["尝试 OTP: 654321"],
    }
    tasks._save_task_log(
        "chatgpt",
        "demo@example.com",
        "failed",
        error="Authorization: Bearer eyJabcdefghijk.abcdefghijklmnop.abcdefghijklmnop password=secret",
        detail=raw_detail,
    )

    with Session(test_engine) as session:
        row = session.exec(select(TaskLog).where(TaskLog.task_id == task_id)).one()
        saved_detail = json.loads(row.detail_json)

    assert saved_detail["redaction_version"] == REDACTION_VERSION
    dumped = json.dumps(saved_detail, ensure_ascii=False)
    assert "at-secret" not in dumped
    assert "user:pass@" not in dumped
    assert "token=secret" not in dumped
    assert "654321" not in dumped
    assert "eyJabcdefghijk" not in row.error
    assert "secret" not in row.error


def test_phone_binding_info_keeps_identity_while_debug_and_secrets_stay_masked():
    import api.tasks as tasks

    task_id = "task_phone_binding_identity_visibility"
    email = "operator.account+gpt1@icloud.com"
    phone = "+15551234567"
    tasks._task_store.create(
        task_id,
        platform="chatgpt",
        total=1,
        source="phone_binding_test",
        meta={},
    )

    sensitive_suffix = (
        "验证码=876543 token=token-secret password=password-secret "
        "proxy=http://proxy-user:proxy-pass@1.2.3.4:8000"
    )
    tasks._log(
        task_id,
        f"[手机号绑定] 邮箱={email} 手机号={phone} {sensitive_suffix}",
        level="info",
    )
    tasks._log(
        task_id,
        f"[手机号绑定] 邮箱={email} 手机号={phone} {sensitive_suffix}",
        level="debug",
    )

    raw_snapshot = tasks._task_store.snapshot(task_id)
    response_snapshot = tasks._sanitize_task_snapshot_for_response(raw_snapshot)
    for snapshot in (raw_snapshot, response_snapshot):
        info_line, debug_line = snapshot["logs"][-2:]
        assert email in info_line
        assert phone in info_line
        assert email not in debug_line
        assert phone not in debug_line
        assert mask_email_for_log(email) in debug_line
        assert mask_phone_for_log(phone) in debug_line
        for leaked in (
            "876543",
            "token-secret",
            "password-secret",
            "proxy-user:proxy-pass@",
        ):
            assert leaked not in info_line
            assert leaked not in debug_line


def test_stop_task_persists_click_and_terminal_snapshots(monkeypatch, tmp_path):
    """The stop response is a durable boundary, not only an SSE event."""
    from api import tasks
    from core.db import TaskLog, SQLModel

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'stop_task_logs.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr(tasks, "engine", test_engine)

    task_id = "task_stop_snapshot_unit"
    tasks._task_store.create(
        task_id,
        platform="chatgpt",
        total=2,
        source="unit",
        supports_after_current=True,
    )
    tasks._task_store.mark_running(task_id)
    tasks._log(task_id, "before-stop log line")

    response = tasks.stop_task(task_id)
    assert response["mode"] == "immediate"
    assert response["changed"] is True

    with Session(test_engine) as session:
        row = session.exec(select(TaskLog).where(TaskLog.task_id == task_id)).one()
        detail = json.loads(row.detail_json)
    assert row.status == "running"
    assert detail["attempt_outcome"] == "immediate_stop_requested"
    assert any("before-stop log line" in line for line in detail["logs"])
    assert any("已请求立即停止" in line for line in detail["logs"])

    tasks._task_store.append_log(task_id, "[00:00:00] terminal drain log")
    tasks._task_store.finish(
        task_id,
        status="stopped",
        success=0,
        skipped=0,
        errors=[],
        error="任务已手动停止",
    )
    with Session(test_engine) as session:
        row = session.exec(select(TaskLog).where(TaskLog.task_id == task_id)).one()
        detail = json.loads(row.detail_json)
    assert row.status == "stopped"
    assert detail["control"]["stop_mode"] == "immediate"
    assert any("terminal drain log" in line for line in detail["logs"])


def test_account_result_does_not_close_active_task_and_late_callback_keeps_terminal_snapshot(monkeypatch, tmp_path):
    """Per-account success writes must not erase a later interrupted batch."""
    from api import tasks
    from core.task_runtime import RegisterTaskStore

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'account_result_race.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr(tasks, "engine", test_engine)
    store = RegisterTaskStore()
    store.set_terminal_callback(tasks._persist_terminal_task_snapshot)
    monkeypatch.setattr(tasks, "_task_store", store)

    task_id = "task_account_result_race"
    store.create(task_id, platform="chatgpt", total=3, source="manual")
    store.mark_running(task_id)
    tasks._log(task_id, "[OK] 第一个账号完成")
    tasks._save_task_log(
        "chatgpt",
        "first@example.com",
        "success",
        detail=tasks._build_task_log_detail(task_id, {"attempt_outcome": "success"}),
    )
    with Session(test_engine) as session:
        row = session.exec(select(TaskLog).where(TaskLog.task_id == task_id)).one()
        assert row.status == "running"

    tasks._log(task_id, "[INTERRUPTED] 远端结果未知")
    store.finish(
        task_id,
        status="interrupted",
        success=1,
        skipped=0,
        errors=["远端结果未知"],
        error="远端结果未知",
    )
    # Simulate a worker callback that captured the old running snapshot.
    tasks._save_task_log(
        "chatgpt",
        "first@example.com",
        "success",
        error="迟到回调错误",
        detail={
            "task_id": task_id,
            "status_snapshot": "running",
            "success": 0,
            "skipped": 0,
            "errors": [],
            "logs": ["旧快照"],
            "source": "manual",
            "attempt_outcome": "success",
        },
    )

    with Session(test_engine) as session:
        row = session.exec(select(TaskLog).where(TaskLog.task_id == task_id)).one()
        detail = json.loads(row.detail_json)
    assert row.status == "interrupted"
    assert row.error == "远端结果未知"
    assert detail["status_snapshot"] == "interrupted"
    assert detail["attempt_outcome"] == "task_interrupted"
    assert any("远端结果未知" in line for line in detail["logs"])
    history = tasks.get_logs(platform="chatgpt")
    assert history["items"][0]["status"] == "interrupted"
    assert history["items"][0]["interrupted"] == 1


def test_history_stats_are_recovered_from_legacy_registration_meta_and_summary():
    from api import tasks

    detail = {
        "status_snapshot": "running",
        "progress": "2/10",
        "meta": {"registered_accounts": [{"account_id": 1}, {"account_id": 2}]},
        "logs": ["[SUMMARY] 完成: 成功 2 个, 跳过 1 个, 失败 3 个"],
    }
    stats = tasks._task_log_stats(detail, status="stopped")
    assert stats["success"] == 2
    assert stats["skipped"] == 1
    assert stats["failed"] == 3
    assert stats["total"] == 10
    assert stats["stats_available"] is True


def test_history_stats_do_not_present_unknown_stale_running_counts_as_known_zero():
    from api import tasks

    stale = tasks._task_log_stats(
        {
            "status_snapshot": "running",
            "progress": "0/10",
            "success": 0,
            "skipped": 0,
            "errors": [],
            "logs": [],
        },
        status="stopped",
    )
    assert stale["stats_available"] is False

    terminal = tasks._task_log_stats(
        {
            "status_snapshot": "stopped",
            "progress": "0/10",
            "success": 0,
            "skipped": 0,
            "errors": [],
            "logs": [],
        },
        status="stopped",
    )
    assert terminal["stats_available"] is True
    assert terminal["total"] == 10


def test_api_tasks_preserves_blank_log_lines_in_sse(monkeypatch):
    from api import tasks
    from core.task_runtime import RegisterTaskStore

    task_id = "task_blank_log_sse"
    store = RegisterTaskStore()
    store.create(task_id, platform="chatgpt", total=1, source="unit")
    monkeypatch.setattr(tasks, "_task_store", store)
    tasks._log(task_id, "")
    store.finish(task_id, status="done", success=1, skipped=0, errors=[])

    async def collect_events():
        response = await tasks.stream_logs(task_id)
        events = []
        async for chunk in response.body_iterator:
            text = chunk.decode() if isinstance(chunk, bytes) else chunk
            for line in text.splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        return events

    events = asyncio.run(collect_events())

    assert events[0] == {"line": ""}
    assert events[-1] == {"done": True, "status": "done"}


def test_api_task_sse_uses_monotonic_cursor_after_log_window_trim(monkeypatch):
    from api import tasks
    from core.task_runtime import RegisterTaskStore

    task_id = "task_trimmed_log_sse"
    store = RegisterTaskStore(
        active_max_log_entries=3,
        active_max_log_bytes=1024,
        finished_max_log_entries=3,
        finished_max_log_bytes=1024,
    )
    store.create(task_id, platform="chatgpt", total=1, source="unit")
    for index in range(5):
        store.append_log(task_id, f"line-{index}")
    store.finish(task_id, status="done", success=1, skipped=0, errors=[])
    monkeypatch.setattr(tasks, "_task_store", store)

    snapshot = store.snapshot(task_id)
    assert snapshot["log_start_index"] == 2
    assert snapshot["log_next_index"] == 5

    async def collect_events():
        response = await tasks.stream_logs(task_id, since=0)
        events = []
        async for chunk in response.body_iterator:
            text = chunk.decode() if isinstance(chunk, bytes) else chunk
            for line in text.splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        return events

    events = asyncio.run(collect_events())

    assert events[:-1] == [
        {"line": "line-2"},
        {"line": "line-3"},
        {"line": "line-4"},
    ]
    assert events[-1] == {"done": True, "status": "done"}


def test_terminal_history_keeps_persisted_active_window_after_memory_compaction(
    monkeypatch,
    tmp_path,
):
    from api import tasks
    from core.task_runtime import RegisterTaskStore

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'terminal_window.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr(tasks, "engine", test_engine)

    store = RegisterTaskStore(
        active_max_log_entries=10,
        active_max_log_bytes=1024,
        finished_max_log_entries=2,
        finished_max_log_bytes=1024,
    )
    store.set_terminal_callback(tasks._persist_terminal_task_snapshot)
    monkeypatch.setattr(tasks, "_task_store", store)
    task_id = "task_terminal_window"
    store.create(task_id, platform="chatgpt", total=1, source="unit")
    for entry in ("line-1", "line-2", "line-3", "line-4"):
        store.append_log(task_id, entry)

    store.finish(task_id, status="done", success=1, skipped=0, errors=[])
    assert store.snapshot(task_id)["logs"] == ["line-3", "line-4"]

    # Historical runners may issue a second terminal persistence call after
    # finish(). It must not replace the larger durable window with the compact
    # in-memory copy.
    tasks._persist_task_snapshot(
        task_id,
        attempt_outcome="runner_terminal_followup",
    )

    with Session(test_engine) as session:
        row = session.exec(select(TaskLog).where(TaskLog.task_id == task_id)).one()
        detail = json.loads(row.detail_json)
    assert detail["logs"] == ["line-1", "line-2", "line-3", "line-4"]
    assert detail["log_start_index"] == 0
    assert detail["log_next_index"] == 4


def test_batch_oaipay_upload_task_logging_saves_attempt_outcome(tmp_path, monkeypatch):
    from sqlmodel import create_engine
    from api import tasks
    from core.db import TaskLog, SQLModel

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'oaipay_task_logs.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr(tasks, "engine", test_engine)

    task_id = "task_oaipay_test_123"
    raw_detail = tasks._build_task_log_detail(
        task_id,
        {
            "email": "demo_oaipay@example.com",
            "attempt_outcome": "batch_oaipay_upload_success",
            "source": "batch_oaipay_upload",
            "meta": {
                "runtime_success": 5,
                "runtime_skipped": 2,
                "runtime_errors": [],
                "category_mode": "auto",
                "fallback_category_id": 2,
                "runtime_category_counts": {"#2 PLUS--已接美国长效 [自动]": 5},
            },
        },
    )
    tasks._save_task_log(
        "chatgpt",
        "demo_oaipay@example.com",
        "success",
        error="",
        detail=raw_detail,
    )

    with Session(test_engine) as session:
        row = session.exec(select(TaskLog).where(TaskLog.task_id == task_id)).one()
        saved_detail = json.loads(row.detail_json)

    assert row.status == "success"
    assert row.email == "demo_oaipay@example.com"
    assert saved_detail["attempt_outcome"] == "batch_oaipay_upload_success"
    assert saved_detail["source"] == "batch_oaipay_upload"
    assert saved_detail["meta"]["runtime_success"] == 5
    assert saved_detail["meta"]["category_mode"] == "auto"
    assert saved_detail["meta"]["runtime_category_counts"]["#2 PLUS--已接美国长效 [自动]"] == 5


def test_enqueue_batch_oaipay_upload_task_empty(tmp_path, monkeypatch):
    from sqlmodel import create_engine
    from api import tasks
    from core.db import TaskLog, SQLModel

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'oaipay_empty_task.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr(tasks, "engine", test_engine)

    req = tasks.BatchOaipayUploadTaskRequest(account_ids=[999999])
    res = tasks.enqueue_batch_oaipay_upload_task(req)
    assert res["eligible"] == 0
    assert res["missing"] == 1
    assert 999999 in res["missing_ids"]

    with Session(test_engine) as session:
        row = session.exec(select(TaskLog).where(TaskLog.task_id == res["task_id"])).one()
        saved_detail = json.loads(row.detail_json)

    assert saved_detail["attempt_outcome"] == "batch_oaipay_upload_empty"
