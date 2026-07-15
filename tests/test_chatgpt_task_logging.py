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


def test_format_phone_binding_timeline_log_is_plain_aligned_and_masks_phone_otp():
    line = format_task_timeline_log(
        "手机绑定",
        "状态：已获取｜验证码=123456，手机号=+12269023179",
        stage_index=4,
        stage_total=12,
        phase_label="邮箱验证",
    )
    assert line.startswith("[手机号绑定][步骤04/12 邮箱验证]")
    assert "[手机绑定]" not in line
    assert "已获取" in line
    assert "验证码=[REDACTED_OTP]" in line
    assert "123456" not in line
    assert "+12269023179" not in line
    assert "+1226***3179" in line


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
