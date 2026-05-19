from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from core.base_platform import Account, AccountStatus
from core.config_store import config_store
from core.db import AccountModel, engine
from services.external_apps import install, list_status, start, start_all, stop, stop_all
from services.chatgpt_sync import backfill_chatgpt_account_to_cpa, get_cliproxy_sync_state
from services.sub2api_sync import backfill_chatgpt_account_to_sub2api, get_sub2api_sync_state

router = APIRouter(prefix="/integrations", tags=["integrations"])


class BackfillRequest(BaseModel):
    platforms: list[str] = Field(default_factory=lambda: ["chatgpt"])
    account_ids: list[int] = Field(default_factory=list)
    destination: str = "cliproxyapi"
    pending_only: bool = False
    status: Optional[str] = None
    email: Optional[str] = None


class GoPayOtpUidBinding(BaseModel):
    uid: str
    phone_country_code: str = "86"
    phone_number: str
    package_name: str = ""
    title: str = ""
    label: str = ""
    device: str = ""
    source: str = "manual"
    updated_at: str = ""
    enabled: bool = True


class GoPayOtpBindingsRequest(BaseModel):
    bindings: list[GoPayOtpUidBinding] = Field(default_factory=list)


class GoPayOtpPhonePoolItem(BaseModel):
    phone_country_code: str = "86"
    phone_number: str
    uid: str = ""
    status: str = "ready"
    source: str = "manual"
    package_name: str = ""
    title: str = ""
    label: str = ""
    device: str = ""
    pin: str = ""
    note: str = ""
    enabled: bool = True
    last_otp: str = ""
    last_otp_at: str = ""
    last_error: str = ""
    last_seen_at: str = ""
    last_used_account_id: Optional[int] = None
    gopay_session_id: str = ""
    updated_at: str = ""


class GoPayOtpPhonePoolRequest(BaseModel):
    items: list[GoPayOtpPhonePoolItem] = Field(default_factory=list)


class GoPayOtpSettingsRequest(BaseModel):
    smsforwarder_secret: Optional[str] = None
    clear_secret: bool = False
    otp_auto_resend_delay_seconds: Optional[int] = None


class GoPayOtpStartByUidRequest(BaseModel):
    account_id: int
    uid: str
    pin: Optional[str] = None
    plan: str = "plus"
    country: str = "ID"
    currency: str = "IDR"
    proxy: Optional[str] = None
    checkout_url: Optional[str] = None
    force: bool = False
    save_defaults: bool = True


class GoPayOtpParseRequest(BaseModel):
    raw: str


GOPAY_UID_BINDINGS_KEY = "chatgpt_gopay_uid_bindings"
GOPAY_UID_SESSIONS_KEY = "chatgpt_gopay_uid_sessions"
GOPAY_PHONE_POOL_KEY = "chatgpt_gopay_phone_pool"
GOPAY_SMSFORWARDER_SECRET_KEY = "chatgpt_gopay_smsforwarder_secret"
GOPAY_SMSFORWARDER_RECENT_EVENTS_KEY = "chatgpt_gopay_smsforwarder_recent_events"
GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS_KEY = "chatgpt_gopay_otp_auto_resend_delay_seconds"
GOPAY_WEBHOOK_PATH = "/api/integrations/gopay-otp/smsforwarder"
GOPAY_TERMINAL_PHASES = {"succeeded", "failed", "cancelled"}
GOPAY_WAITING_OTP_PHASE = "waiting_otp"
GOPAY_PHONE_POOL_STATUSES = {"ready", "reserved", "used", "invalid"}
GOPAY_RECENT_EVENT_LIMIT = 80
DEFAULT_GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS = 120
GOPAY_STATUS_TEXT = {
    "started": "支付会话已启动",
    "bound": "手机号已绑定",
    "duplicate": "重复提交已忽略",
    "ignored": "已忽略",
    "conflict": "绑定冲突",
    "missing_uid": "格式错误",
    "missing_phone": "未提取手机号",
    "missing_otp": "未提取验证码",
    "unmatched_phone": "未匹配手机号",
    "unmatched_session": "未匹配会话",
    "format_error": "格式错误",
    "submit_failed": "验证码提交失败",
    "submitted": "验证码已提交",
    "resend_requested": "重发验证码已请求",
    "resend_failed": "重发验证码失败",
    "ready": "可用",
    "reserved": "等待验证码",
    "used": "已使用",
    "invalid": "无效",
    "missing": "会话丢失",
    "waiting_otp": "等待验证码",
    "waiting_link_pin": "等待绑定PIN",
    "waiting_payment_pin": "等待支付PIN",
    "verifying": "扣款确认中",
    "succeeded": "支付成功",
    "failed": "支付失败",
    "cancelled": "已取消",
}
GOPAY_SMSFORWARDER_BIND_TEMPLATE = """TYPE=GOPAY_BIND
UID={{UID}}
PKG={{PACKAGE_NAME}}
TITLE={{TITLE}}
MSG_BEGIN
{{MSG}}
MSG_END
DEVICE={{DEVICE_NAME}}"""
GOPAY_SMSFORWARDER_TEMPLATE = """TYPE=GOPAY_OTP
UID={{UID}}
PKG={{PACKAGE_NAME}}
TITLE={{TITLE}}
MSG_BEGIN
{{MSG}}
MSG_END
DEVICE={{DEVICE_NAME}}"""
GOPAY_SMSFORWARDER_WEB_PARAMS_FORM = "raw=[msg]&timestamp=[timestamp]&sign=[sign]"
GOPAY_SMSFORWARDER_WEB_PARAMS_JSON = '{"raw":"[msg]","timestamp":"[timestamp]","sign":"[sign]"}'
GOPAY_OTP_ADMIN_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>GoPay OTP Webhook</title>
  <style>
    :root { color-scheme: light; --bg:#f5f7fb; --panel:#fff; --line:#d9e0ec; --text:#1f2937; --muted:#667085; --blue:#2563eb; --red:#dc2626; --green:#15803d; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:var(--text); background:linear-gradient(180deg,#eef4ff 0%,var(--bg) 280px); }
    main { max-width:1180px; margin:0 auto; padding:28px 18px 48px; }
    header { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; margin-bottom:18px; }
    h1 { margin:0 0 6px; font-size:28px; }
    h2 { margin:0 0 14px; font-size:18px; }
    p { color:var(--muted); margin:0; }
    .grid { display:grid; grid-template-columns:1.15fr .85fr; gap:16px; align-items:start; }
    .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px; box-shadow:0 16px 40px rgba(31,41,55,.08); }
    .stack { display:flex; flex-direction:column; gap:14px; }
    .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
    label { display:block; font-size:13px; color:var(--muted); margin-bottom:5px; }
    input, textarea { width:100%; border:1px solid var(--line); border-radius:8px; padding:10px 11px; font-size:14px; background:#fff; color:var(--text); }
    textarea { min-height:116px; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; resize:vertical; }
    button { border:0; border-radius:8px; padding:10px 13px; font-weight:650; cursor:pointer; background:var(--blue); color:#fff; }
    button.secondary { background:#e8eef8; color:#1f2937; }
    button.danger { background:var(--red); }
    button:disabled { opacity:.55; cursor:not-allowed; }
    table { width:100%; border-collapse:collapse; }
    th, td { border-bottom:1px solid var(--line); padding:10px 8px; text-align:left; vertical-align:top; font-size:13px; }
    th { color:var(--muted); font-weight:700; background:#f8fafc; }
    code, pre { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
    pre { white-space:pre-wrap; background:#f8fafc; border:1px solid var(--line); border-radius:8px; padding:12px; margin:8px 0 0; }
    .pill { display:inline-flex; align-items:center; gap:5px; padding:4px 8px; border-radius:999px; background:#eef2ff; color:#3730a3; font-size:12px; font-weight:700; }
    .ok { color:var(--green); }
    .bad { color:var(--red); }
    .muted { color:var(--muted); }
    .cols { display:grid; grid-template-columns:repeat(6, minmax(0, 1fr)); gap:10px; }
    .cols .wide { grid-column:span 2; }
    .cols .xwide { grid-column:span 3; }
    @media (max-width: 900px) { .grid, .cols { grid-template-columns:1fr; } .cols .wide, .cols .xwide { grid-column:auto; } header { flex-direction:column; } }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>GoPay OTP Webhook</h1>
        <p>SmsForwarder 直接推送通知，服务端按 UID 查手机号并提交 GoPay OTP。</p>
      </div>
      <div class="row">
        <a href="/" class="muted">返回后台</a>
        <button class="secondary" id="reloadBtn">刷新</button>
      </div>
    </header>
    <div id="authTip" class="card" style="display:none;margin-bottom:16px">
      未检测到登录 token。请先打开后台登录，再回到本页。
    </div>
    <section class="grid">
      <div class="card stack">
        <h2>SmsForwarder 配置</h2>
        <div id="secretState" class="pill">Secret 状态读取中</div>
        <div>
          <label>Webhook Server</label>
          <input id="webhookUrl" readonly />
        </div>
        <div>
          <label>消息模板</label>
          <textarea id="messageTemplate" readonly></textarea>
        </div>
        <div>
          <label>webParams 表单模式</label>
          <input id="webParamsForm" readonly />
        </div>
        <div>
          <label>webParams JSON 模式</label>
          <input id="webParamsJson" readonly />
        </div>
        <div class="row">
          <input id="secretInput" placeholder="SmsForwarder webhook secret" style="flex:1;min-width:240px" />
          <button id="saveSecretBtn">保存 Secret</button>
          <button class="danger" id="clearSecretBtn">清空</button>
        </div>
        <div class="row">
          <div style="flex:1;min-width:220px">
            <label>自动重发延迟秒数（0 表示关闭）</label>
            <input id="autoResendDelay" type="number" min="0" max="3600" />
          </div>
          <button id="saveDelayBtn">保存延迟</button>
        </div>
      </div>
      <div class="card stack">
        <h2>按 UID 启动 GoPay</h2>
        <div>
          <label>ChatGPT Account ID</label>
          <input id="startAccountId" placeholder="17" />
        </div>
        <div>
          <label>UID</label>
          <input id="startUid" placeholder="99910283" />
        </div>
        <div>
          <label>GoPay PIN</label>
          <input id="startPin" placeholder="可选，留空用默认值" />
        </div>
        <div class="row">
          <label><input id="startForce" type="checkbox" style="width:auto" /> 覆盖未结束会话</label>
          <button id="startBtn">启动支付</button>
        </div>
        <pre id="startResult">等待操作</pre>
      </div>
    </section>
    <section class="card stack" style="margin-top:16px">
      <h2>UID 与手机号绑定</h2>
      <div class="cols">
        <div><label>UID</label><input id="uid" placeholder="99910283" /></div>
        <div><label>区号</label><input id="cc" value="86" /></div>
        <div class="wide"><label>手机号</label><input id="phone" placeholder="15335521131" /></div>
        <div class="wide"><label>包名</label><input id="pkg" value="com.whatsapp" /></div>
        <div><label>标题</label><input id="title" value="GoPay" /></div>
        <div><label>标签</label><input id="label" placeholder="小米11pro" /></div>
      </div>
      <div class="row">
        <button id="addBindingBtn">添加绑定</button>
      </div>
      <div style="overflow:auto">
        <table>
          <thead><tr><th>UID</th><th>手机号</th><th>包名</th><th>标题</th><th>标签</th><th>启用</th><th>操作</th></tr></thead>
          <tbody id="bindingsBody"></tbody>
        </table>
      </div>
    </section>
    <section class="card stack" style="margin-top:16px">
      <h2>Active GoPay 会话</h2>
      <div style="overflow:auto">
        <table>
          <thead><tr><th>UID</th><th>Account</th><th>Session</th><th>手机号</th><th>阶段</th><th>错误</th><th>操作</th></tr></thead>
          <tbody id="sessionsBody"></tbody>
        </table>
      </div>
    </section>
    <section class="card stack" style="margin-top:16px">
      <h2>最近 webhook 消息</h2>
      <div style="overflow:auto">
        <table>
          <thead><tr><th>时间</th><th>UID</th><th>OTP</th><th>状态</th><th>包名</th><th>标题</th><th>阶段</th><th>详情</th><th>内容</th></tr></thead>
          <tbody id="eventsBody"></tbody>
        </table>
      </div>
    </section>
  </main>
  <script>
    const token = localStorage.getItem('auth_token') || '';
    const headers = () => ({ 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) });
    let state = null;
    function esc(value) {
      return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    async function api(path, options = {}) {
      const res = await fetch('/api' + path, { ...options, headers: { ...headers(), ...(options.headers || {}) } });
      if (!res.ok) throw new Error(await res.text());
      return res.json();
    }
    async function load() {
      document.getElementById('authTip').style.display = token ? 'none' : 'block';
      state = await api('/integrations/gopay-otp');
      document.getElementById('webhookUrl').value = location.origin + state.webhook_path;
      document.getElementById('messageTemplate').value = state.message_template || '';
      document.getElementById('webParamsForm').value = state.web_params_form || '';
      document.getElementById('webParamsJson').value = state.web_params_json || '';
      document.getElementById('secretState').textContent = state.secret_enabled ? 'Secret 已启用' : 'Secret 未设置';
      document.getElementById('secretState').className = state.secret_enabled ? 'pill ok' : 'pill bad';
      document.getElementById('autoResendDelay').value = String(state.otp_auto_resend_delay_seconds ?? 120);
      renderBindings();
      renderSessions();
      renderEvents();
    }
    function renderBindings() {
      document.getElementById('bindingsBody').innerHTML = (state.bindings || []).map(item => `
        <tr>
          <td>${esc(item.uid)}</td>
          <td>+${esc(item.phone_country_code)} ${esc(item.phone_number)}</td>
          <td>${esc(item.package_name)}</td>
          <td>${esc(item.title)}</td>
          <td>${esc(item.label)}</td>
          <td>${item.enabled ? '是' : '否'}</td>
          <td class="row">
            <button class="secondary" onclick="toggleBinding('${esc(item.uid)}')">${item.enabled ? '禁用' : '启用'}</button>
            <button class="danger" onclick="deleteBinding('${esc(item.uid)}')">删除</button>
          </td>
        </tr>`).join('');
    }
    function renderSessions() {
      document.getElementById('sessionsBody').innerHTML = (state.sessions || []).map(item => `
        <tr>
          <td>${esc(item.uid)}</td>
          <td>${esc(item.account_id)}</td>
          <td><code>${esc(item.gopay_session_id)}</code></td>
          <td>+${esc(item.phone_country_code)} ${esc(item.phone_number)}</td>
          <td><span class="pill">${esc(item.phase)}</span></td>
          <td>${esc(item.last_error)}</td>
          <td class="row">
            <button class="secondary" ${item.phase === 'waiting_otp' ? '' : 'disabled'} onclick="resendOtp('${esc(item.uid)}')">重发 OTP</button>
            <button class="secondary" onclick="clearSession('${esc(item.uid)}')">清除记录</button>
          </td>
        </tr>`).join('');
    }
    function renderEvents() {
      document.getElementById('eventsBody').innerHTML = (state.recent_events || []).map(item => `
        <tr>
          <td>${esc(item.received_at)}</td>
          <td>${esc(item.uid)}</td>
          <td>${esc(item.otp)}</td>
          <td><span class="pill">${esc(item.status)}</span></td>
          <td>${esc(item.package_name)}</td>
          <td>${esc(item.title)}</td>
          <td>${esc(item.phase || '')}</td>
          <td>${esc(item.detail || '')}</td>
          <td>${esc(item.message || '')}</td>
        </tr>`).join('');
    }
    async function saveBindings(bindings) {
      state = await api('/integrations/gopay-otp/bindings', { method: 'PUT', body: JSON.stringify({ bindings }) });
      renderBindings();
    }
    document.getElementById('addBindingBtn').onclick = async () => {
      const next = {
        uid: document.getElementById('uid').value.trim(),
        phone_country_code: document.getElementById('cc').value.replace(/\D/g, '') || '86',
        phone_number: document.getElementById('phone').value.replace(/\D/g, ''),
        package_name: document.getElementById('pkg').value.trim(),
        title: document.getElementById('title').value.trim(),
        label: document.getElementById('label').value.trim(),
        enabled: true,
      };
      if (!next.uid || !next.phone_number) return alert('UID 和手机号不能为空');
      if ((state.bindings || []).some(item => item.uid === next.uid)) return alert('UID 已存在');
      await saveBindings([...(state.bindings || []), next]);
      document.getElementById('uid').value = '';
      document.getElementById('phone').value = '';
    };
    window.toggleBinding = async uid => {
      await saveBindings((state.bindings || []).map(item => item.uid === uid ? { ...item, enabled: !item.enabled } : item));
    };
    window.deleteBinding = async uid => {
      if (!confirm('删除这条 UID 绑定？')) return;
      await saveBindings((state.bindings || []).filter(item => item.uid !== uid));
    };
    window.clearSession = async uid => {
      state = await api('/integrations/gopay-otp/sessions/' + encodeURIComponent(uid) + '/clear', { method: 'POST' });
      renderSessions();
    };
    document.getElementById('saveSecretBtn').onclick = async () => {
      state = await api('/integrations/gopay-otp/settings', { method: 'PUT', body: JSON.stringify({ smsforwarder_secret: document.getElementById('secretInput').value }) });
      document.getElementById('secretInput').value = '';
      await load();
    };
    document.getElementById('clearSecretBtn').onclick = async () => {
      state = await api('/integrations/gopay-otp/settings', { method: 'PUT', body: JSON.stringify({ clear_secret: true }) });
      await load();
    };
    document.getElementById('saveDelayBtn').onclick = async () => {
      state = await api('/integrations/gopay-otp/settings', {
        method: 'PUT',
        body: JSON.stringify({ otp_auto_resend_delay_seconds: Number(document.getElementById('autoResendDelay').value || 0) }),
      });
      await load();
    };
    window.resendOtp = async uid => {
      try {
        state = await api('/integrations/gopay-otp/sessions/' + encodeURIComponent(uid) + '/resend-otp', { method: 'POST' });
        await load();
      } catch (err) {
        alert(err.message || err);
      }
    };
    document.getElementById('startBtn').onclick = async () => {
      const body = {
        account_id: Number(document.getElementById('startAccountId').value),
        uid: document.getElementById('startUid').value.trim(),
        pin: document.getElementById('startPin').value,
        plan: 'plus',
        force: document.getElementById('startForce').checked,
      };
      try {
        const result = await api('/integrations/gopay-otp/start-by-uid', { method: 'POST', body: JSON.stringify(body) });
        document.getElementById('startResult').textContent = JSON.stringify(result.session || result, null, 2);
        await load();
      } catch (err) {
        document.getElementById('startResult').textContent = String(err.message || err);
      }
    };
    document.getElementById('reloadBtn').onclick = () => load().catch(err => alert(err.message || err));
    load().catch(err => alert(err.message || err));
  </script>
</body>
</html>"""


def _to_account(model: AccountModel) -> Account:
    return Account(
        platform=model.platform,
        email=model.email,
        password=model.password,
        user_id=model.user_id,
        region=model.region,
        token=model.token,
        status=AccountStatus(model.status),
        extra=model.get_extra(),
    )


def _model_to_dict(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_config(key: str, default: Any) -> Any:
    raw = str(config_store.get(key, "") or "").strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _set_json_config(key: str, value: Any) -> None:
    config_store.set(key, json.dumps(value, ensure_ascii=False))


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _normalize_auto_resend_delay(value: Any) -> int:
    try:
        delay = int(value)
    except Exception:
        delay = DEFAULT_GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS
    return max(0, min(delay, 3600))


def _load_auto_resend_delay_seconds() -> int:
    raw = str(config_store.get(GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS_KEY, "") or "").strip()
    if not raw:
        return DEFAULT_GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS
    return _normalize_auto_resend_delay(raw)


def _save_auto_resend_delay_seconds(value: Any) -> None:
    config_store.set(GOPAY_OTP_AUTO_RESEND_DELAY_SECONDS_KEY, str(_normalize_auto_resend_delay(value)))


def _status_text(status: Any, detail: Any = "") -> str:
    key = str(status or "").strip()
    if key in GOPAY_STATUS_TEXT:
        return GOPAY_STATUS_TEXT[key]
    lowered = key.lower()
    if "conflict" in lowered:
        return "绑定冲突"
    if "failed" in lowered:
        return "失败"
    if "ignored" in lowered:
        return "已忽略"
    detail_text = str(detail or "")
    if "reference" in detail_text.lower():
        return "缺少reference"
    return key or "未知状态"


def _with_status_text(item: dict[str, Any]) -> dict[str, Any]:
    return {
        **item,
        "status_text": str(item.get("status_text") or _status_text(item.get("status"), item.get("detail") or item.get("last_error"))),
        "phase_text": str(item.get("phase_text") or _status_text(item.get("phase"))),
    }


def _normalize_pin(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _global_gopay_pin_configured() -> bool:
    raw = str(config_store.get("chatgpt_gopay_defaults", "") or "").strip()
    if not raw:
        return False
    try:
        parsed = json.loads(raw)
    except Exception:
        return False
    return bool(_normalize_pin(parsed.get("pin") if isinstance(parsed, dict) else ""))


def _normalize_binding(value: Any) -> Optional[dict[str, Any]]:
    data = value if isinstance(value, dict) else {}
    uid = str(data.get("uid") or "").strip()
    phone_number = _digits(data.get("phone_number"))
    if not uid or not phone_number:
        return None
    return {
        "uid": uid,
        "phone_country_code": _digits(data.get("phone_country_code") or "86") or "86",
        "phone_number": phone_number,
        "package_name": str(data.get("package_name") or "").strip(),
        "title": str(data.get("title") or "").strip(),
        "label": str(data.get("label") or "").strip(),
        "device": str(data.get("device") or "").strip(),
        "source": str(data.get("source") or "manual").strip() or "manual",
        "updated_at": str(data.get("updated_at") or "").strip(),
        "enabled": bool(data.get("enabled", True)),
    }


def _load_uid_bindings() -> list[dict[str, Any]]:
    raw = _json_config(GOPAY_UID_BINDINGS_KEY, [])
    items = raw.values() if isinstance(raw, dict) else raw
    if not isinstance(items, list) and not isinstance(items, type({}.values())):
        return []
    bindings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        binding = _normalize_binding(item)
        if not binding or binding["uid"] in seen:
            continue
        seen.add(binding["uid"])
        bindings.append(binding)
    return bindings


def _save_uid_bindings(bindings: list[dict[str, Any]]) -> None:
    _set_json_config(GOPAY_UID_BINDINGS_KEY, bindings)


def _validate_binding_uniqueness(bindings: list[dict[str, Any]]) -> None:
    seen_uid: set[str] = set()
    seen_phone: dict[str, str] = {}
    for binding in bindings:
        uid = str(binding.get("uid") or "").strip()
        phone_key = _binding_phone_key(binding)
        if uid in seen_uid:
            raise HTTPException(400, f"UID 重复: {uid}")
        seen_uid.add(uid)
        if phone_key and phone_key in seen_phone and seen_phone[phone_key] != uid:
            raise HTTPException(400, f"手机号重复绑定: {binding.get('phone_country_code')} {binding.get('phone_number')}")
        if phone_key:
            seen_phone[phone_key] = uid


def _find_uid_binding(uid: Any, *, enabled_only: bool = True) -> Optional[dict[str, Any]]:
    target = str(uid or "").strip()
    if not target:
        return None
    for binding in _load_uid_bindings():
        if binding.get("uid") == target and (not enabled_only or binding.get("enabled") is not False):
            return binding
    return None


def _normalize_phone_pool_item(value: Any) -> Optional[dict[str, Any]]:
    data = value if isinstance(value, dict) else {}
    phone_number = _digits(data.get("phone_number"))
    if not phone_number:
        return None
    status = str(data.get("status") or "ready").strip().lower()
    if status not in GOPAY_PHONE_POOL_STATUSES:
        status = "ready"
    account_id = data.get("last_used_account_id")
    try:
        normalized_account_id = int(account_id) if account_id not in (None, "") else None
    except Exception:
        normalized_account_id = None
    return {
        "phone_country_code": _digits(data.get("phone_country_code") or "86") or "86",
        "phone_number": phone_number,
        "uid": str(data.get("uid") or "").strip(),
        "status": status,
        "source": str(data.get("source") or "manual").strip() or "manual",
        "package_name": str(data.get("package_name") or "").strip(),
        "title": str(data.get("title") or "").strip(),
        "label": str(data.get("label") or "").strip(),
        "device": str(data.get("device") or "").strip(),
        "pin": _normalize_pin(data.get("pin")),
        "note": str(data.get("note") or "").strip(),
        "enabled": bool(data.get("enabled", True)),
        "last_otp": _digits(data.get("last_otp")),
        "last_otp_at": str(data.get("last_otp_at") or "").strip(),
        "last_error": str(data.get("last_error") or "").strip(),
        "last_seen_at": str(data.get("last_seen_at") or "").strip(),
        "last_used_account_id": normalized_account_id,
        "gopay_session_id": str(data.get("gopay_session_id") or "").strip(),
        "updated_at": str(data.get("updated_at") or "").strip(),
    }


def _load_phone_pool() -> list[dict[str, Any]]:
    raw = _json_config(GOPAY_PHONE_POOL_KEY, [])
    items = raw.values() if isinstance(raw, dict) else raw
    if not isinstance(items, list) and not isinstance(items, type({}.values())):
        return []
    pool: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        normalized = _normalize_phone_pool_item(item)
        if not normalized:
            continue
        key = _phone_key(normalized.get("phone_country_code"), normalized.get("phone_number"))
        if key in seen:
            continue
        seen.add(key)
        pool.append(normalized)
    return pool


def _save_phone_pool(pool: list[dict[str, Any]]) -> None:
    _set_json_config(GOPAY_PHONE_POOL_KEY, pool)


def _find_phone_pool_item(country_code: Any, phone_number: Any) -> Optional[dict[str, Any]]:
    key = _phone_key(country_code, phone_number)
    if not key:
        return None
    for item in _load_phone_pool():
        if _phone_key(item.get("phone_country_code"), item.get("phone_number")) == key:
            return item
    return None


def _upsert_phone_pool_from_binding(
    binding: dict[str, Any],
    *,
    status: Optional[str] = None,
    source: Optional[str] = None,
    account_id: Optional[int] = None,
    session_id: str = "",
) -> dict[str, Any]:
    key = _binding_phone_key(binding)
    if not key:
        raise HTTPException(400, "手机号不能为空")
    now = _utcnow_iso()
    pool = _load_phone_pool()
    existing_index = -1
    for index, item in enumerate(pool):
        if _phone_key(item.get("phone_country_code"), item.get("phone_number")) == key:
            existing_index = index
            break
    existing = dict(pool[existing_index]) if existing_index >= 0 else {}
    next_item = {
        **existing,
        "phone_country_code": binding.get("phone_country_code", "86"),
        "phone_number": binding.get("phone_number", ""),
        "uid": binding.get("uid", ""),
        "status": status or existing.get("status") or "ready",
        "source": source or binding.get("source") or existing.get("source") or "manual",
        "package_name": binding.get("package_name", ""),
        "title": binding.get("title", ""),
        "label": binding.get("label", ""),
        "device": binding.get("device", ""),
        "pin": existing.get("pin", ""),
        "note": existing.get("note", ""),
        "enabled": existing.get("enabled", True),
        "last_otp": existing.get("last_otp", ""),
        "last_otp_at": existing.get("last_otp_at", ""),
        "last_error": existing.get("last_error", ""),
        "last_seen_at": now,
        "last_used_account_id": account_id if account_id is not None else existing.get("last_used_account_id"),
        "gopay_session_id": session_id or existing.get("gopay_session_id", ""),
        "updated_at": now,
    }
    normalized = _normalize_phone_pool_item(next_item)
    if not normalized:
        raise HTTPException(400, "手机号池条目无效")
    if existing_index >= 0:
        pool[existing_index] = normalized
    else:
        pool.insert(0, normalized)
    _save_phone_pool(pool)
    return normalized


def _update_phone_pool_runtime(binding: dict[str, Any], **patch: Any) -> Optional[dict[str, Any]]:
    key = _binding_phone_key(binding)
    if not key:
        return None
    pool = _load_phone_pool()
    for index, item in enumerate(pool):
        if _phone_key(item.get("phone_country_code"), item.get("phone_number")) != key:
            continue
        next_item = {
            **item,
            **{k: v for k, v in patch.items() if v is not None},
            "updated_at": _utcnow_iso(),
        }
        normalized = _normalize_phone_pool_item(next_item)
        if not normalized:
            return None
        pool[index] = normalized
        _save_phone_pool(pool)
        return normalized
    return None


def _sync_phone_pool_with_bindings(bindings: list[dict[str, Any]]) -> None:
    for binding in bindings:
        _upsert_phone_pool_from_binding(binding, source=binding.get("source") or "manual")


def _ensure_phone_pool_from_bindings(bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pool = _load_phone_pool()
    existing_keys = {_phone_key(item.get("phone_country_code"), item.get("phone_number")) for item in pool}
    changed = False
    now = _utcnow_iso()
    for binding in bindings:
        key = _binding_phone_key(binding)
        if not key or key in existing_keys:
            continue
        normalized = _normalize_phone_pool_item({
            "phone_country_code": binding.get("phone_country_code", "86"),
            "phone_number": binding.get("phone_number", ""),
            "uid": binding.get("uid", ""),
            "status": "ready",
            "source": binding.get("source") or "uid_binding_migrate",
            "package_name": binding.get("package_name", ""),
            "title": binding.get("title", ""),
            "label": binding.get("label", ""),
            "device": binding.get("device", ""),
            "enabled": binding.get("enabled", True),
            "last_seen_at": binding.get("updated_at") or now,
            "updated_at": binding.get("updated_at") or now,
        })
        if normalized:
            pool.append(normalized)
            existing_keys.add(key)
            changed = True
    if changed:
        _save_phone_pool(pool)
    return pool


def _load_uid_sessions() -> dict[str, dict[str, Any]]:
    raw = _json_config(GOPAY_UID_SESSIONS_KEY, {})
    if isinstance(raw, list):
        return {
            str(item.get("uid")): item
            for item in raw
            if isinstance(item, dict) and str(item.get("uid") or "").strip()
        }
    if not isinstance(raw, dict):
        return {}
    return {
        str(uid): item
        for uid, item in raw.items()
        if isinstance(item, dict) and str(uid or "").strip()
    }


def _save_uid_sessions(sessions: dict[str, dict[str, Any]]) -> None:
    _set_json_config(GOPAY_UID_SESSIONS_KEY, sessions)


def _event_received_timestamp(event: dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(str(event.get("received_at") or "")).timestamp()
    except Exception:
        return 0.0


def _load_recent_events() -> list[dict[str, Any]]:
    raw = _json_config(GOPAY_SMSFORWARDER_RECENT_EVENTS_KEY, [])
    if not isinstance(raw, list):
        return []
    return sorted(
        [_with_status_text(item) for item in raw if isinstance(item, dict)],
        key=_event_received_timestamp,
        reverse=True,
    )


def _append_recent_event(event: dict[str, Any]) -> dict[str, Any]:
    events = _load_recent_events()
    saved = {
        "event_id": hashlib.sha256(
            f"{time.time_ns()}:{event.get('uid', '')}:{event.get('otp', '')}".encode("utf-8")
        ).hexdigest()[:16],
        "received_at": _utcnow_iso(),
        **event,
    }
    saved = _with_status_text(saved)
    events.insert(0, saved)
    _set_json_config(GOPAY_SMSFORWARDER_RECENT_EVENTS_KEY, events[:GOPAY_RECENT_EVENT_LIMIT])
    return saved


def _refresh_gopay_snapshot(session_id: str) -> Optional[dict[str, Any]]:
    try:
        from services.chatgpt_core.gopay_flow import get_gopay_session

        return get_gopay_session(session_id)
    except KeyError:
        return None
    except Exception as exc:
        return {"phase": "unknown", "last_error": str(exc)}


def _session_payload_from_snapshot(uid: str, binding: dict[str, Any], account_id: int, snapshot: dict[str, Any]) -> dict[str, Any]:
    return _with_status_text({
        "uid": uid,
        "account_id": account_id,
        "gopay_session_id": str(snapshot.get("session_id") or ""),
        "phone_country_code": binding.get("phone_country_code", ""),
        "phone_number": binding.get("phone_number", ""),
        "package_name": binding.get("package_name", ""),
        "title": binding.get("title", ""),
        "phase": str(snapshot.get("phase") or ""),
        "status": str(snapshot.get("status") or ""),
        "last_error": str(snapshot.get("last_error") or ""),
        "otp_resend_count": int(snapshot.get("otp_resend_count") or 0),
        "otp_auto_resend_done": bool(snapshot.get("otp_auto_resend_done")),
        "last_otp_resend_at": str(snapshot.get("last_otp_resend_at") or ""),
        "pin_source": str(snapshot.get("pin_source") or ""),
        "active_action": str(snapshot.get("active_action") or ""),
        "started_at": _utcnow_iso(),
        "updated_at": _utcnow_iso(),
    })


def _refresh_saved_sessions() -> dict[str, dict[str, Any]]:
    sessions = _load_uid_sessions()
    changed = False
    for uid, item in list(sessions.items()):
        session_id = str(item.get("gopay_session_id") or "").strip()
        if not session_id:
            continue
        snapshot = _refresh_gopay_snapshot(session_id)
        if snapshot is None:
            if item.get("phase") != "missing":
                item["phase"] = "missing"
                item["last_error"] = "GoPay 内存会话不存在，可能是服务重启或任务已丢失"
                item["updated_at"] = _utcnow_iso()
                changed = True
            continue
        phase = str(snapshot.get("phase") or item.get("phase") or "")
        status = str(snapshot.get("status") or item.get("status") or "")
        last_error = str(snapshot.get("last_error") or "")
        otp_resend_count = int(snapshot.get("otp_resend_count") or 0)
        otp_auto_resend_done = bool(snapshot.get("otp_auto_resend_done"))
        last_otp_resend_at = str(snapshot.get("last_otp_resend_at") or "")
        if (
            item.get("phase") != phase
            or item.get("status") != status
            or item.get("last_error") != last_error
            or int(item.get("otp_resend_count") or 0) != otp_resend_count
            or bool(item.get("otp_auto_resend_done")) != otp_auto_resend_done
            or str(item.get("last_otp_resend_at") or "") != last_otp_resend_at
        ):
            item["phase"] = phase
            item["status"] = status
            item["phase_text"] = _status_text(phase)
            item["status_text"] = _status_text(status, last_error)
            item["last_error"] = last_error
            item["otp_resend_count"] = otp_resend_count
            item["otp_auto_resend_done"] = otp_auto_resend_done
            item["last_otp_resend_at"] = last_otp_resend_at
            item["updated_at"] = _utcnow_iso()
            changed = True
    if changed:
        _save_uid_sessions(sessions)
    return {uid: _with_status_text(item) for uid, item in sessions.items()}


def _phone_key(country_code: Any, phone_number: Any) -> str:
    cc = _digits(country_code)
    number = _digits(phone_number)
    return f"{cc}:{number}" if cc and number else ""


def _binding_phone_key(binding: dict[str, Any]) -> str:
    return _phone_key(binding.get("phone_country_code"), binding.get("phone_number"))


def _snapshot_phone_key(snapshot: dict[str, Any]) -> str:
    return _phone_key(snapshot.get("phone_country_code"), snapshot.get("phone_number"))


def _snapshot_matches_binding(snapshot: dict[str, Any], binding: dict[str, Any]) -> bool:
    return bool(_binding_phone_key(binding) and _binding_phone_key(binding) == _snapshot_phone_key(snapshot))


def _list_live_gopay_snapshots() -> list[dict[str, Any]]:
    try:
        from services.chatgpt_core.gopay_flow import list_gopay_sessions

        items = list_gopay_sessions()
        return [item for item in items if isinstance(item, dict)]
    except Exception:
        return []


def _iter_saved_account_gopay_snapshots() -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    try:
        with Session(engine) as db:
            rows = db.exec(select(AccountModel).where(AccountModel.platform == "chatgpt")).all()
            for row in rows:
                extra = row.get_extra()
                snapshot = extra.get("chatgpt_gopay") if isinstance(extra.get("chatgpt_gopay"), dict) else None
                if snapshot:
                    snapshots.append(dict(snapshot))
    except Exception:
        return []
    return snapshots


def _candidate_from_snapshot(uid: str, binding: dict[str, Any], snapshot: dict[str, Any]) -> Optional[dict[str, Any]]:
    if str(snapshot.get("phase") or "") != GOPAY_WAITING_OTP_PHASE:
        return None
    if not _snapshot_matches_binding(snapshot, binding):
        return None
    account_id = int(snapshot.get("account_id") or 0)
    session_id = str(snapshot.get("session_id") or "").strip()
    if not account_id or not session_id:
        return None
    return _session_payload_from_snapshot(uid, binding, account_id, snapshot)


def _resolve_gopay_otp_target(uid: str, binding: dict[str, Any]) -> dict[str, Any]:
    sessions = _refresh_saved_sessions()
    existing = sessions.get(uid)
    if existing:
        session_id = str(existing.get("gopay_session_id") or "").strip()
        snapshot = _refresh_gopay_snapshot(session_id) if session_id else None
        if snapshot is None:
            existing["phase"] = "missing"
            existing["last_error"] = "GoPay 内存会话不存在，可能是服务重启或任务已丢失"
            existing["updated_at"] = _utcnow_iso()
            sessions[uid] = existing
            _save_uid_sessions(sessions)
        elif str(snapshot.get("phase") or "") == GOPAY_WAITING_OTP_PHASE:
            payload = _session_payload_from_snapshot(uid, binding, int(existing.get("account_id") or snapshot.get("account_id") or 0), snapshot)
            sessions[uid] = payload
            _save_uid_sessions(sessions)
            return {"ok": True, "active": payload, "snapshot": snapshot}
        elif str(snapshot.get("phase") or "") not in GOPAY_TERMINAL_PHASES:
            existing["phase"] = str(snapshot.get("phase") or "")
            existing["status"] = str(snapshot.get("status") or existing.get("status") or "")
            existing["last_error"] = str(snapshot.get("last_error") or "")
            existing["updated_at"] = _utcnow_iso()
            sessions[uid] = existing
            _save_uid_sessions(sessions)

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for snapshot in [*_list_live_gopay_snapshots(), *_iter_saved_account_gopay_snapshots()]:
        session_id = str(snapshot.get("session_id") or "").strip()
        if not session_id or session_id in seen:
            continue
        live_snapshot = _refresh_gopay_snapshot(session_id)
        if live_snapshot is None:
            continue
        payload = _candidate_from_snapshot(uid, binding, live_snapshot)
        if payload:
            seen.add(session_id)
            candidates.append({"active": payload, "snapshot": live_snapshot})

    if not candidates:
        return {
            "ok": False,
            "detail": "没有找到使用该手机号且正在等待 OTP 的 GoPay 会话",
        }
    if len(candidates) > 1:
        return {
            "ok": False,
            "detail": f"该手机号同时匹配 {len(candidates)} 个等待 OTP 的 GoPay 会话，请先保留一个 active 会话",
            "candidates": [item["active"] for item in candidates],
        }
    active = candidates[0]["active"]
    sessions[uid] = active
    _save_uid_sessions(sessions)
    return {"ok": True, **candidates[0]}


def _decode_nested_urlencoded_text(value: Any) -> str:
    text = str(value or "")
    for _ in range(2):
        if not re.search(r"%(?:0[AdD]|20|2[0-9A-Fa-f]|3[ADad]|5[BbDd]|E[0-9A-Fa-f])", text):
            break
        decoded = urllib.parse.unquote_plus(text)
        if decoded == text:
            break
        text = decoded
    return text


def _first_text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        text = _decode_nested_urlencoded_text(value).strip()
        if text:
            return text
    return ""


def _extract_inline_field(text: str, labels: tuple[str, ...], stops: tuple[str, ...]) -> str:
    source = str(text or "")
    label_pattern = "|".join(re.escape(label) for label in labels)
    stop_pattern = "|".join(re.escape(stop) for stop in stops)
    match = re.search(
        rf"(?:^|[\s,，;；])(?:{label_pattern})\s*[:=：]\s*(.*?)(?=(?:[\s,，;；]+(?:{stop_pattern})\s*[:=：])|(?:\s+来自\S*)|$)",
        source,
        flags=re.I | re.S,
    )
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip(" ,，;；")


def _parse_labeled_lines(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    key_map = {
        "type": "type",
        "uid": "uid",
        "cc": "phone_country_code",
        "country_code": "phone_country_code",
        "phone_country_code": "phone_country_code",
        "区号": "phone_country_code",
        "phone": "phone_number",
        "phone_number": "phone_number",
        "mobile": "phone_number",
        "手机号": "phone_number",
        "pkg": "package_name",
        "package_name": "package_name",
        "package": "package_name",
        "包名": "package_name",
        "title": "title",
        "标题": "title",
        "app": "app_name",
        "app_name": "app_name",
        "应用名": "app_name",
        "device": "device",
        "设备": "device",
    }
    for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        from_match = re.match(r"^来自(.+)$", line)
        if from_match:
            fields.setdefault("device", from_match.group(1).strip())
            continue
        match = re.match(r"^([^:=：]+)\s*[:=：]\s*(.*)$", line)
        if not match:
            continue
        raw_key = re.sub(r"\s+", "_", match.group(1).strip().lower())
        target = key_map.get(raw_key)
        if target:
            fields[target] = match.group(2).strip()
    return fields


def _extract_message_text(text: str, data: dict[str, Any]) -> str:
    for key in ("content", "text", "body", "notification_content", "notificationContent"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    raw_text = str(text or "")
    block = re.search(r"MSG_BEGIN\s*(.*?)\s*MSG_END", raw_text, flags=re.I | re.S)
    if block:
        return block.group(1).strip()
    labeled = re.search(r"(?ims)^\s*(?:内容|MSG)\s*[:：=]\s*(.*)$", raw_text)
    if labeled:
        body = labeled.group(1).strip()
        body = re.split(
            r"(?im)(?:^\s*|\s+)(?:来自|DEVICE\s*=|APP\s*=|APP_NAME\s*=|PKG\s*=|PACKAGE_NAME\s*=|TITLE\s*=|UID\s*=|TYPE\s*=)",
            body,
            maxsplit=1,
        )[0].strip()
        if body:
            return body
    inline = _extract_inline_field(
        raw_text,
        ("内容", "MSG", "MESSAGE"),
        ("来自", "DEVICE", "APP", "APP_NAME", "PKG", "PACKAGE_NAME", "TITLE", "UID", "TYPE", "包名", "标题", "应用名"),
    )
    if inline:
        return inline
    for key in ("msg", "message"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return str(data.get("content") or data.get("raw") or raw_text or "").strip()


def _extract_otp(text: str) -> str:
    source = str(text or "")
    patterns = (
        r"\b(\d{4,8})\b\s+(?:is\s+your\s+(?:verification|security)\s+code|is\s+your\s+code)",
        r"(?:验证码|校验码|验证代码|verification code|security code|one-time code|otp|code)[^\d]{0,30}(\d{4,8})",
        r"\b(\d{4,8})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, source, flags=re.I)
        if match:
            return match.group(1)
    return ""


def _extract_phone_number(text: str) -> str:
    source = str(text or "")
    candidates = re.findall(r"(?:\+?\d[\d\s().-]{8,}\d)", source)
    for candidate in candidates:
        digits = _digits(candidate)
        if 10 <= len(digits) <= 15:
            if len(digits) == 13 and digits.startswith("86"):
                digits = digits[2:]
            return digits
    return ""


def _parse_smsforwarder_content(data: dict[str, Any], body_text: str) -> dict[str, Any]:
    raw = _decode_nested_urlencoded_text(
        data.get("raw")
        or data.get("payload")
        or data.get("content")
        or data.get("msg")
        or data.get("message")
        or body_text
        or ""
    )
    fields = _parse_labeled_lines(raw)
    message = _extract_message_text(raw, data)
    uid = str(
        _first_text(data, "uid", "UID", "android_uid", "androidUid")
        or fields.get("uid")
        or _extract_inline_field(raw, ("UID",), ("PKG", "PACKAGE_NAME", "PACKAGE", "TITLE", "MSG", "MESSAGE", "DEVICE", "包名", "标题", "内容", "应用名", "来自"))
        or ""
    ).strip()
    package_name = str(
        _first_text(data, "package_name", "packageName", "pkg", "package", "app_package", "appPackage")
        or fields.get("package_name")
        or _extract_inline_field(raw, ("PKG", "PACKAGE_NAME", "PACKAGE", "包名"), ("TITLE", "MSG", "MESSAGE", "DEVICE", "UID", "标题", "内容", "应用名", "来自"))
        or _first_text(data, "from")
        or ""
    ).strip()
    title = str(
        _first_text(data, "title", "notification_title", "notificationTitle")
        or fields.get("title")
        or _extract_inline_field(raw, ("TITLE", "标题"), ("MSG", "MESSAGE", "DEVICE", "UID", "PKG", "PACKAGE_NAME", "PACKAGE", "内容", "包名", "应用名", "来自"))
        or ""
    ).strip()
    phone_country_code = str(
        _first_text(data, "phone_country_code", "country_code", "cc", "CC")
        or fields.get("phone_country_code")
        or _extract_inline_field(raw, ("CC", "COUNTRY_CODE", "PHONE_COUNTRY_CODE", "区号"), ("PHONE", "PHONE_NUMBER", "MOBILE", "PKG", "PACKAGE_NAME", "TITLE", "MSG", "MESSAGE", "DEVICE", "UID", "手机号", "包名", "标题", "内容", "应用名", "来自"))
        or "86"
    ).strip()
    phone_number = (
        _first_text(data, "phone_number", "phoneNumber", "phone", "mobile")
        or fields.get("phone_number")
        or _extract_inline_field(raw, ("PHONE", "PHONE_NUMBER", "MOBILE", "手机号"), ("PKG", "PACKAGE_NAME", "PACKAGE", "TITLE", "MSG", "MESSAGE", "DEVICE", "UID", "CC", "COUNTRY_CODE", "包名", "标题", "内容", "应用名", "来自"))
        or _extract_phone_number(message)
    )
    event_type = str(
        _first_text(data, "type", "TYPE", "event_type", "eventType")
        or fields.get("type")
        or _extract_inline_field(raw, ("TYPE",), ("UID", "PKG", "PACKAGE_NAME", "PACKAGE", "TITLE", "MSG", "MESSAGE", "DEVICE", "包名", "标题", "内容", "应用名", "来自"))
        or ""
    ).strip().splitlines()[0].strip().split()[0].upper()
    otp = _extract_otp(message)
    phone_number = _digits(phone_number)
    if not event_type:
        if phone_number and not otp:
            event_type = "GOPAY_BIND"
        elif otp:
            event_type = "GOPAY_OTP"
    return {
        "type": event_type,
        "uid": uid,
        "phone_country_code": _digits(phone_country_code) or "86",
        "phone_number": phone_number,
        "package_name": package_name,
        "title": title,
        "app_name": str(_first_text(data, "app_name", "appName") or fields.get("app_name") or "").strip(),
        "device": str(_first_text(data, "device", "device_name", "deviceName") or fields.get("device") or "").strip(),
        "message": message,
        "otp": otp,
        "raw": raw,
        "raw_hash": hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:16],
    }


async def _read_smsforwarder_request(request: Request) -> tuple[dict[str, Any], str]:
    body = await request.body()
    body_text = body.decode("utf-8", errors="replace")
    content_type = request.headers.get("content-type", "").lower()
    data: dict[str, Any] = {}
    if "application/json" in content_type:
        try:
            parsed = json.loads(body_text or "{}")
            if isinstance(parsed, dict):
                data.update(parsed)
            else:
                data["raw"] = body_text
        except Exception:
            data["raw"] = body_text
    elif "application/x-www-form-urlencoded" in content_type:
        parsed = urllib.parse.parse_qs(body_text, keep_blank_values=True)
        data.update({key: values[-1] if values else "" for key, values in parsed.items()})
    else:
        data["raw"] = body_text
    for key, value in request.query_params.items():
        data.setdefault(key, value)
    return data, body_text


def _verify_smsforwarder_signature(data: dict[str, Any]) -> None:
    secret = str(config_store.get(GOPAY_SMSFORWARDER_SECRET_KEY, "") or "").strip()
    if not secret:
        return
    timestamp = str(data.get("timestamp") or "").strip()
    provided = str(data.get("sign") or "").strip()
    if not timestamp or not provided:
        raise HTTPException(401, "SmsForwarder 签名缺少 timestamp/sign")
    try:
        ts_ms = int(float(timestamp))
    except Exception as exc:
        raise HTTPException(401, "SmsForwarder timestamp 无效") from exc
    if abs(int(time.time() * 1000) - ts_ms) > 60 * 60 * 1000:
        raise HTTPException(401, "SmsForwarder timestamp 已过期")
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    normalized = urllib.parse.unquote_plus(provided)
    if not hmac.compare_digest(expected, normalized):
        raise HTTPException(401, "SmsForwarder 签名校验失败")


def _is_duplicate_smsforwarder_event(uid: str, otp: str, raw_hash: str) -> bool:
    now = time.time()
    for event in _load_recent_events()[:30]:
        if str(event.get("uid") or "") != uid:
            continue
        if str(event.get("otp") or "") != otp:
            continue
        if str(event.get("raw_hash") or "") != raw_hash:
            continue
        try:
            received = datetime.fromisoformat(str(event.get("received_at") or "")).timestamp()
        except Exception:
            received = 0
        if now - received <= 300:
            return True
    return False


def _phone_pool_summary(pool: list[dict[str, Any]]) -> dict[str, int]:
    summary = {status: 0 for status in GOPAY_PHONE_POOL_STATUSES}
    for item in pool:
        status = str(item.get("status") or "ready").strip().lower()
        if status not in summary:
            summary[status] = 0
        summary[status] += 1
    return summary


def _recent_event_summary(events: list[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    cutoff = time.time() - 3600
    for event in events:
        try:
            received = datetime.fromisoformat(str(event.get("received_at") or "")).timestamp()
        except Exception:
            received = 0
        if received and received < cutoff:
            continue
        status = str(event.get("status") or "unknown").strip() or "unknown"
        summary[status] = summary.get(status, 0) + 1
    return summary


def _adapter_state() -> dict[str, Any]:
    sessions = _refresh_saved_sessions()
    bindings = _load_uid_bindings()
    raw_phone_pool = _ensure_phone_pool_from_bindings(bindings)
    phone_pool = []
    has_global_pin = _global_gopay_pin_configured()
    for item in raw_phone_pool:
        enriched = _with_status_text(item)
        pin = str(enriched.get("pin") or "")
        enriched["has_pin"] = bool(pin)
        enriched["pin_source"] = "手机号PIN" if pin else ("全局默认PIN" if has_global_pin else "未配置")
        enriched.pop("pin", None)
        phone_pool.append(enriched)
    recent_events = _load_recent_events()
    return {
        "bindings": bindings,
        "phone_pool": phone_pool,
        "sessions": [_with_status_text(item) for item in sessions.values()],
        "recent_events": recent_events,
        "summary": {
            "bindings_total": len(bindings),
            "bindings_enabled": len([item for item in bindings if item.get("enabled") is not False]),
            "bindings_disabled": len([item for item in bindings if item.get("enabled") is False]),
            "sessions_total": len(sessions),
            "sessions_waiting_otp": len([item for item in sessions.values() if str(item.get("phase") or "") == GOPAY_WAITING_OTP_PHASE]),
            "sessions_missing": len([item for item in sessions.values() if str(item.get("phase") or "") == "missing"]),
            "phone_pool": _phone_pool_summary(phone_pool),
            "recent_events": _recent_event_summary(recent_events),
        },
        "secret_enabled": bool(str(config_store.get(GOPAY_SMSFORWARDER_SECRET_KEY, "") or "").strip()),
        "otp_auto_resend_delay_seconds": _load_auto_resend_delay_seconds(),
        "webhook_path": GOPAY_WEBHOOK_PATH,
        "message_template": GOPAY_SMSFORWARDER_TEMPLATE,
        "bind_message_template": GOPAY_SMSFORWARDER_BIND_TEMPLATE,
        "web_params_form": GOPAY_SMSFORWARDER_WEB_PARAMS_FORM,
        "web_params_json": GOPAY_SMSFORWARDER_WEB_PARAMS_JSON,
        "sample_payload": {
            "type": "GOPAY_OTP",
            "uid": "99910283",
            "package_name": "com.whatsapp",
            "title": "GoPay",
            "message": "306953 is your verification code. For your security, do not share this code.",
        },
    }


@router.get("/services")
def get_services():
    return {"items": list_status()}


@router.post("/services/start-all")
def start_all_services():
    return {"items": start_all()}


@router.post("/services/stop-all")
def stop_all_services():
    return {"items": stop_all()}


@router.post("/services/{name}/start")
def start_service(name: str):
    return start(name)


@router.post("/services/{name}/install")
def install_service(name: str):
    return install(name)


@router.post("/services/{name}/stop")
def stop_service(name: str):
    return stop(name)


@router.post("/backfill")
def backfill_integrations(body: BackfillRequest):
    summary = {"total": 0, "success": 0, "failed": 0, "skipped": 0, "items": []}
    targets = set(body.platforms or [])
    destination = str(body.destination or "cliproxyapi").strip().lower() or "cliproxyapi"

    with Session(engine) as s:
        q = select(AccountModel)
        if body.account_ids:
            q = q.where(AccountModel.id.in_(body.account_ids))
            if targets:
                q = q.where(AccountModel.platform.in_(targets))
        elif targets:
            q = q.where(AccountModel.platform.in_(targets))
        else:
            return summary

        if body.status:
            q = q.where(AccountModel.status == body.status)
        if body.email:
            q = q.where(AccountModel.email.contains(body.email))

        rows = s.exec(q).all()
        if body.pending_only:
            def _is_pending_target(row: AccountModel) -> bool:
                if row.platform != "chatgpt":
                    return False
                if destination == "sub2api":
                    state = get_sub2api_sync_state(row)
                    if not state:
                        return True
                    remote_state = str(state.get("remote_state") or "").strip().lower()
                    uploaded = bool(state.get("uploaded"))
                    return (
                        remote_state in {"", "not_found", "cross_workspace_only", "deleted_exact_match"}
                        or (not remote_state and not uploaded)
                    )
                else:
                    state = get_cliproxy_sync_state(row)
                if not state:
                    return True
                return str(state.get("remote_state") or "").strip().lower() in {"not_found", "cross_workspace_only"}

            rows = [row for row in rows if _is_pending_target(row)]

        for row in rows:
            item = {"platform": row.platform, "email": row.email, "results": []}
            try:
                results = []
                if row.platform == "chatgpt":
                    if destination == "sub2api":
                        outcome = backfill_chatgpt_account_to_sub2api(row, session=s, commit=True)
                        default_name = "Sub2API"
                    else:
                        outcome = backfill_chatgpt_account_to_cpa(row, session=s, commit=True)
                        default_name = "CLIProxyAPI"

                    ok = bool(outcome.get("ok"))
                    skipped = bool(outcome.get("skipped"))
                    results.extend(outcome.get("results") or [])
                    if not results:
                        results.append({"name": default_name, "ok": ok, "msg": outcome.get("message", "")})
                    if skipped:
                        summary["skipped"] += 1
                    elif ok:
                        summary["success"] += 1
                    else:
                        summary["failed"] += 1

                if not results:
                    item["results"].append({"name": "skip", "ok": False, "msg": "未配置对应导入目标"})
                    summary["failed"] += 1
                else:
                    item["results"] = results
            except Exception as e:
                s.rollback()
                item["results"].append({"name": "error", "ok": False, "msg": str(e)})
                summary["failed"] += 1
            summary["items"].append(item)
            summary["total"] += 1

    return summary


@router.get("/gopay-otp")
def get_gopay_otp_adapter_state():
    return _adapter_state()


@router.get("/gopay-otp/admin")
def get_gopay_otp_adapter_admin_page():
    return RedirectResponse(url="/gopay-otp", status_code=302)


@router.put("/gopay-otp/bindings")
def update_gopay_otp_uid_bindings(body: GoPayOtpBindingsRequest):
    bindings: list[dict[str, Any]] = []
    for item in body.bindings:
        binding = _normalize_binding(_model_to_dict(item))
        if not binding:
            raise HTTPException(400, "UID 和手机号不能为空")
        if not binding.get("updated_at"):
            binding["updated_at"] = _utcnow_iso()
        bindings.append(binding)
    _validate_binding_uniqueness(bindings)
    _save_uid_bindings(bindings)
    _sync_phone_pool_with_bindings(bindings)
    return _adapter_state()


@router.put("/gopay-otp/phone-pool")
def update_gopay_otp_phone_pool(body: GoPayOtpPhonePoolRequest):
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    bindings: list[dict[str, Any]] = []
    existing_by_key = {
        _phone_key(item.get("phone_country_code"), item.get("phone_number")): item
        for item in _load_phone_pool()
    }
    for item in body.items:
        normalized = _normalize_phone_pool_item(_model_to_dict(item))
        if not normalized:
            raise HTTPException(400, "手机号不能为空")
        key = _phone_key(normalized.get("phone_country_code"), normalized.get("phone_number"))
        if key in seen:
            raise HTTPException(400, f"手机号重复: {normalized.get('phone_country_code')} {normalized.get('phone_number')}")
        seen.add(key)
        existing = existing_by_key.get(key) or {}
        if not normalized.get("pin") and existing.get("pin"):
            normalized["pin"] = existing.get("pin")
        if not normalized.get("updated_at"):
            normalized["updated_at"] = _utcnow_iso()
        items.append(normalized)
        if normalized.get("uid") and normalized.get("enabled") is not False:
            binding = _normalize_binding({
                "uid": normalized.get("uid"),
                "phone_country_code": normalized.get("phone_country_code"),
                "phone_number": normalized.get("phone_number"),
                "package_name": normalized.get("package_name"),
                "title": normalized.get("title"),
                "label": normalized.get("label") or normalized.get("device"),
                "device": normalized.get("device"),
                "source": normalized.get("source") or "phone_pool",
                "updated_at": normalized.get("updated_at"),
                "enabled": True,
            })
            if binding:
                bindings.append(binding)
    _validate_binding_uniqueness(bindings)
    _save_phone_pool(items)
    _save_uid_bindings(bindings)
    return _adapter_state()


@router.put("/gopay-otp/settings")
def update_gopay_otp_adapter_settings(body: GoPayOtpSettingsRequest):
    if body.clear_secret:
        config_store.set(GOPAY_SMSFORWARDER_SECRET_KEY, "")
    elif body.smsforwarder_secret is not None:
        config_store.set(GOPAY_SMSFORWARDER_SECRET_KEY, str(body.smsforwarder_secret or "").strip())
    if body.otp_auto_resend_delay_seconds is not None:
        _save_auto_resend_delay_seconds(body.otp_auto_resend_delay_seconds)
    return _adapter_state()


@router.post("/gopay-otp/start-by-uid")
def start_gopay_payment_by_uid(body: GoPayOtpStartByUidRequest):
    uid = str(body.uid or "").strip()
    binding = _find_uid_binding(uid)
    if not binding:
        raise HTTPException(404, "UID 未绑定或已禁用")

    sessions = _refresh_saved_sessions()
    existing = sessions.get(uid)
    if existing and not body.force:
        phase = str(existing.get("phase") or "")
        if phase and phase not in GOPAY_TERMINAL_PHASES and phase != "missing":
            raise HTTPException(409, f"UID {uid} 已存在未结束的 GoPay 会话: {phase}")

    from api.chatgpt import GoPayStartReq, start_gopay_payment

    pool_item = _find_phone_pool_item(binding.get("phone_country_code"), binding.get("phone_number")) or {}
    pool_pin = _normalize_pin(pool_item.get("pin"))
    requested_pin = _normalize_pin(body.pin)
    effective_pin = requested_pin or pool_pin
    pin_source = "本次输入PIN" if requested_pin else ("手机号PIN" if pool_pin else "全局默认PIN")
    req = GoPayStartReq(
        phone_country_code=binding["phone_country_code"],
        phone_number=binding["phone_number"],
        plan=body.plan,
        country=body.country,
        currency=body.currency,
        proxy=body.proxy,
        checkout_url=body.checkout_url,
        pin=effective_pin,
        pin_source=pin_source,
        save_defaults=body.save_defaults if requested_pin or not pool_pin else False,
    )
    with Session(engine) as db:
        snapshot = start_gopay_payment(body.account_id, req, session=db)

    session_payload = _session_payload_from_snapshot(uid, binding, body.account_id, snapshot)
    sessions[uid] = session_payload
    _save_uid_sessions(sessions)
    _upsert_phone_pool_from_binding(
        binding,
        status="reserved",
        source=binding.get("source") or "manual",
        account_id=body.account_id,
        session_id=session_payload.get("gopay_session_id", ""),
    )
    _append_recent_event({
        "status": "started",
        "type": "GOPAY_START",
        "uid": uid,
        "otp": "",
        "phone_country_code": binding.get("phone_country_code", ""),
        "phone_number": binding.get("phone_number", ""),
        "raw_hash": "",
        "package_name": binding.get("package_name", ""),
        "title": binding.get("title", ""),
        "message": f"GoPay payment started for UID {uid}",
        "account_id": body.account_id,
        "gopay_session_id": session_payload.get("gopay_session_id", ""),
        "phase": session_payload.get("phase", ""),
        "pin_source": pin_source,
    })
    return {"ok": True, "binding": binding, "session": session_payload, "snapshot": snapshot}


@router.post("/gopay-otp/sessions/{uid}/clear")
def clear_gopay_otp_uid_session(uid: str):
    sessions = _load_uid_sessions()
    removed = sessions.pop(str(uid or "").strip(), None)
    _save_uid_sessions(sessions)
    return {"ok": True, "removed": removed, **_adapter_state()}


@router.post("/gopay-otp/sessions/{uid}/resend-otp")
def resend_gopay_otp_for_uid(uid: str):
    uid = str(uid or "").strip()
    sessions = _refresh_saved_sessions()
    active = sessions.get(uid)
    if not active:
        saved = _append_recent_event({"status": "ignored", "uid": uid, "otp": "", "detail": "UID 没有 active GoPay 会话"})
        raise HTTPException(404, saved["detail"])

    session_id = str(active.get("gopay_session_id") or "").strip()
    account_id = int(active.get("account_id") or 0)
    snapshot = _refresh_gopay_snapshot(session_id)
    if snapshot is None:
        active["phase"] = "missing"
        active["last_error"] = "GoPay 内存会话不存在，可能是服务重启或任务已丢失"
        active["updated_at"] = _utcnow_iso()
        sessions[uid] = active
        _save_uid_sessions(sessions)
        _append_recent_event({"status": "ignored", "uid": uid, "otp": "", "detail": active["last_error"]})
        raise HTTPException(404, active["last_error"])

    phase = str(snapshot.get("phase") or "")
    if phase != GOPAY_WAITING_OTP_PHASE:
        detail = f"当前阶段不需要重发 OTP: {phase}"
        active["phase"] = phase
        active["status"] = str(snapshot.get("status") or active.get("status") or "")
        active["last_error"] = str(snapshot.get("last_error") or "")
        active["updated_at"] = _utcnow_iso()
        sessions[uid] = active
        _save_uid_sessions(sessions)
        _append_recent_event({"status": "ignored", "uid": uid, "otp": "", "detail": detail, "phase": phase})
        raise HTTPException(400, detail)

    try:
        from api.chatgpt import resend_gopay_payment_otp

        with Session(engine) as db:
            result = resend_gopay_payment_otp(account_id, session_id, session=db)
    except HTTPException as exc:
        detail = str(exc.detail)
        _append_recent_event({"status": "resend_failed", "uid": uid, "otp": "", "detail": detail, "phase": phase})
        raise
    except Exception as exc:
        detail = str(exc)
        _append_recent_event({"status": "resend_failed", "uid": uid, "otp": "", "detail": detail, "phase": phase})
        raise HTTPException(500, detail) from exc

    active["phase"] = str(result.get("phase") or "")
    active["status"] = str(result.get("status") or "")
    active["last_error"] = str(result.get("last_error") or "")
    active["otp_resend_count"] = int(result.get("otp_resend_count") or 0)
    active["otp_auto_resend_done"] = bool(result.get("otp_auto_resend_done"))
    active["last_otp_resend_at"] = str(result.get("last_otp_resend_at") or "")
    active["updated_at"] = _utcnow_iso()
    sessions[uid] = active
    _save_uid_sessions(sessions)
    saved = _append_recent_event({
        "status": "resend_requested",
        "uid": uid,
        "otp": "",
        "detail": "GoPay OTP 重发请求已提交",
        "account_id": account_id,
        "gopay_session_id": session_id,
        "phase": active["phase"],
    })
    return {"ok": True, "status": "resend_requested", "event": saved, "snapshot": result, **_adapter_state()}


@router.post("/gopay-otp/test-parse")
def test_parse_gopay_otp_message(body: GoPayOtpParseRequest):
    parsed = _parse_smsforwarder_content({"raw": body.raw}, body.raw)
    binding = _find_uid_binding(parsed.get("uid"))
    return {"parsed": parsed, "binding": binding}


def _handle_smsforwarder_bind(parsed: dict[str, Any], event_base: dict[str, Any]) -> dict[str, Any]:
    uid = str(parsed.get("uid") or "").strip()
    phone_number = _digits(parsed.get("phone_number"))
    if not uid:
        saved = _append_recent_event({**event_base, "status": "missing_uid", "detail": "绑定消息缺少 UID"})
        return {"ok": False, "status": "missing_uid", "status_text": saved["status_text"], "detail": saved["detail"], "event": saved}
    if not phone_number:
        saved = _append_recent_event({**event_base, "status": "missing_phone", "detail": "绑定消息未提取到手机号"})
        return {"ok": False, "status": "missing_phone", "status_text": saved["status_text"], "detail": saved["detail"], "event": saved}

    next_binding = _normalize_binding({
        "uid": uid,
        "phone_country_code": parsed.get("phone_country_code") or "86",
        "phone_number": phone_number,
        "package_name": parsed.get("package_name") or "",
        "title": parsed.get("title") or "",
        "label": parsed.get("device") or "",
        "device": parsed.get("device") or "",
        "source": "smsforwarder_bind",
        "updated_at": _utcnow_iso(),
        "enabled": True,
    })
    if not next_binding:
        saved = _append_recent_event({**event_base, "status": "missing_phone", "detail": "绑定消息手机号无效"})
        return {"ok": False, "status": "missing_phone", "status_text": saved["status_text"], "detail": saved["detail"], "event": saved}

    bindings = _load_uid_bindings()
    existing_uid = next((item for item in bindings if item.get("uid") == uid), None)
    existing_phone = next(
        (
            item
            for item in bindings
            if _binding_phone_key(item) == _binding_phone_key(next_binding)
            and item.get("uid") != uid
        ),
        None,
    )
    if existing_uid and _binding_phone_key(existing_uid) != _binding_phone_key(next_binding):
        detail = (
            "UID 已绑定不同手机号: "
            f"old=+{existing_uid.get('phone_country_code')} {existing_uid.get('phone_number')} "
            f"new=+{next_binding.get('phone_country_code')} {next_binding.get('phone_number')}"
        )
        saved = _append_recent_event({**event_base, "status": "conflict", "detail": detail})
        return {"ok": False, "status": "conflict", "detail": detail, "event": saved, "binding": existing_uid}
    if existing_phone:
        detail = f"手机号已绑定到其他 UID: {existing_phone.get('uid')}"
        saved = _append_recent_event({**event_base, "status": "conflict", "detail": detail})
        return {"ok": False, "status": "conflict", "detail": detail, "event": saved, "binding": existing_phone}

    if existing_uid:
        bindings = [
            {
                **item,
                **next_binding,
                "source": item.get("source") or next_binding["source"],
                "enabled": item.get("enabled", True),
            }
            if item.get("uid") == uid
            else item
            for item in bindings
        ]
    else:
        bindings.insert(0, next_binding)
    _validate_binding_uniqueness(bindings)
    _save_uid_bindings(bindings)
    pool_item = _upsert_phone_pool_from_binding(next_binding, status="ready", source="smsforwarder_bind")
    saved = _append_recent_event({
        **event_base,
        "status": "bound",
        "detail": "UID 与手机号已绑定，并已同步到 GoPay 手机号池",
    })
    return {"ok": True, "status": "bound", "event": saved, "binding": next_binding, "phone_pool_item": pool_item}


@router.post("/gopay-otp/smsforwarder")
async def receive_smsforwarder_gopay_otp(request: Request):
    data, body_text = await _read_smsforwarder_request(request)
    _verify_smsforwarder_signature(data)
    parsed = _parse_smsforwarder_content(data, body_text)
    event_base = {
        "type": parsed.get("type", ""),
        "uid": parsed.get("uid", ""),
        "phone_country_code": parsed.get("phone_country_code", ""),
        "phone_number": parsed.get("phone_number", ""),
        "otp": parsed.get("otp", ""),
        "raw_hash": parsed.get("raw_hash", ""),
        "package_name": parsed.get("package_name", ""),
        "title": parsed.get("title", ""),
        "device": parsed.get("device", ""),
        "message": str(parsed.get("message") or "")[:500],
        "raw_excerpt": str(parsed.get("raw") or "")[:700],
    }

    event_type = str(parsed.get("type") or "").strip().upper()
    if event_type == "GOPAY_BIND":
        return _handle_smsforwarder_bind(parsed, event_base)

    uid = str(parsed.get("uid") or "").strip()
    otp = str(parsed.get("otp") or "").strip()
    if not uid:
        saved = _append_recent_event({**event_base, "status": "missing_uid", "detail": "消息缺少 UID"})
        return {"ok": False, "status": "missing_uid", "status_text": saved["status_text"], "detail": saved["detail"], "event": saved}
    if not otp:
        saved = _append_recent_event({**event_base, "status": "missing_otp", "detail": "未提取到 OTP"})
        return {"ok": False, "status": "missing_otp", "status_text": saved["status_text"], "detail": saved["detail"], "event": saved}

    binding = _find_uid_binding(uid)
    if not binding:
        saved = _append_recent_event({**event_base, "status": "unmatched_phone", "detail": "UID 未绑定或已禁用"})
        return {"ok": False, "status": "unmatched_phone", "status_text": saved["status_text"], "detail": saved["detail"], "event": saved}
    event_base["phone_country_code"] = binding.get("phone_country_code", "")
    event_base["phone_number"] = binding.get("phone_number", "")

    expected_package = str(binding.get("package_name") or "").strip()
    actual_package = str(parsed.get("package_name") or "").strip()
    if expected_package and actual_package and expected_package != actual_package:
        event_base["package_warning"] = f"包名不一致: expected={expected_package} actual={actual_package}"

    expected_title = str(binding.get("title") or "").strip()
    actual_title = str(parsed.get("title") or "").strip()
    if expected_title and actual_title and expected_title.lower() != actual_title.lower():
        event_base["title_warning"] = f"标题不一致: expected={expected_title} actual={actual_title}"

    if _is_duplicate_smsforwarder_event(uid, otp, str(parsed.get("raw_hash") or "")):
        saved = _append_recent_event({**event_base, "status": "duplicate", "detail": "5 分钟内重复 OTP 消息"})
        return {"ok": True, "status": "duplicate", "event": saved}

    target = _resolve_gopay_otp_target(uid, binding)
    if not target.get("ok"):
        detail = str(target.get("detail") or "UID 没有 active GoPay 会话")
        saved = _append_recent_event({**event_base, "status": "unmatched_session", "detail": detail})
        return {"ok": False, "status": "unmatched_session", "status_text": saved["status_text"], "detail": detail, "event": saved}
    active = dict(target["active"])
    snapshot = dict(target["snapshot"])
    session_id = str(active.get("gopay_session_id") or "").strip()
    account_id = int(active.get("account_id") or 0)
    phase = str(snapshot.get("phase") or "")

    try:
        from api.chatgpt import GoPayOtpReq, submit_gopay_payment_otp

        with Session(engine) as db:
            result = submit_gopay_payment_otp(account_id, session_id, GoPayOtpReq(otp=otp), session=db)
    except HTTPException as exc:
        detail = str(exc.detail)
        _upsert_phone_pool_from_binding(binding, status="reserved", account_id=account_id, session_id=session_id)
        _update_phone_pool_runtime(binding, last_otp=otp, last_otp_at=_utcnow_iso(), last_error=detail)
        saved = _append_recent_event({**event_base, "status": "submit_failed", "detail": detail, "phase": phase})
        return {"ok": False, "status": "submit_failed", "detail": detail, "event": saved}
    except Exception as exc:
        detail = str(exc)
        _upsert_phone_pool_from_binding(binding, status="reserved", account_id=account_id, session_id=session_id)
        _update_phone_pool_runtime(binding, last_otp=otp, last_otp_at=_utcnow_iso(), last_error=detail)
        saved = _append_recent_event({**event_base, "status": "submit_failed", "detail": detail, "phase": phase})
        return {"ok": False, "status": "submit_failed", "detail": detail, "event": saved}

    active["phase"] = str(result.get("phase") or "")
    active["status"] = str(result.get("status") or "")
    active["last_error"] = str(result.get("last_error") or "")
    active["updated_at"] = _utcnow_iso()
    sessions[uid] = active
    _save_uid_sessions(sessions)
    pool_status = "used" if active["phase"] in GOPAY_TERMINAL_PHASES or active["phase"] in {"waiting_link_pin", "waiting_payment_pin", "verifying"} else "reserved"
    _upsert_phone_pool_from_binding(binding, status=pool_status, account_id=account_id, session_id=session_id)
    _update_phone_pool_runtime(binding, last_otp=otp, last_otp_at=_utcnow_iso(), last_error=str(result.get("last_error") or ""))
    saved = _append_recent_event({
        **event_base,
        "status": "submitted",
        "detail": "OTP 已提交到 GoPay 会话",
        "account_id": account_id,
        "gopay_session_id": session_id,
        "phase": active["phase"],
    })
    return {"ok": True, "status": "submitted", "event": saved, "snapshot": result}
