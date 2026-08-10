"""account_manager - ChatGPT 账号管理后台"""
import logging
import os
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from core.db import init_db
from api.accounts import router as accounts_router
from api.chatgpt import router as chatgpt_router
from api.tasks import router as tasks_router
from api.registration_diagnostics import router as registration_diagnostics_router
from api.proxies import router as proxies_router
from api.config import router as config_router
from api.actions import router as actions_router
from api.auth import router as auth_router
from api.outlook import router as outlook_router
from api.contribution import router as contribution_router
from api.tempmail_archive import router as tempmail_archive_router
from api.external_subscription import (
    router as external_subscription_router,
    start_subscription_verification_scheduler,
    stop_subscription_verification_scheduler,
)
from api.external_access_tokens import router as external_access_tokens_router
from api.phone_pool import router as phone_pool_router
from api.baxigpt_cdk_pool import router as baxigpt_cdk_pool_router
from api.delivery_cards import public_router as delivery_cards_public_router
from api.delivery_cards import router as delivery_cards_router
from api.oaipay import router as oaipay_router
from api.system import router as system_router
from services.chatgpt_core import ChatGPTPlatform

logger = logging.getLogger(__name__)

EXPECTED_CONDA_ENV = os.getenv("APP_CONDA_ENV", "auto-chatgpt")
PUBLIC_API_PATHS = {
    "/api/health",
    "/api/chatgpt/export-sub2api-download",
}

SENSITIVE_SPA_FALLBACK_ROOTS = {
    "actuator",
    "adminer",
    "ansible",
    "backup",
    "backups",
    "config",
    "configs",
    "credentials",
    "database",
    "db",
    "debug",
    "deploy",
    "deployment",
    "dump",
    "dumps",
    "env",
    "iac",
    "infra",
    "infrastructure",
    "ops",
    "phpinfo",
    "server-status",
    "secrets",
    "tf",
}

SENSITIVE_SPA_FALLBACK_FILENAMES = {
    "app.py",
    "application.properties",
    "application.yaml",
    "application.yml",
    "compose.yaml",
    "compose.yml",
    "config.json",
    "config.local.json",
    "config.prod.json",
    "config.production.json",
    "config.yaml",
    "config.yml",
    "credentials.json",
    "database.json",
    "database.yaml",
    "database.yml",
    "docker-compose.override.yml",
    "docker-compose.prod.yml",
    "docker-compose.production.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
    "info.php",
    "local_settings.py",
    "main.py",
    "manage.py",
    "package-lock.json",
    "package.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "pulumi.prod.yaml",
    "pulumi.yaml",
    "pyproject.toml",
    "requirements.txt",
    "service-account.json",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "settings.json",
    "settings.local.json",
    "settings.prod.json",
    "settings.production.json",
    "settings.py",
    "terraform.tfstate",
    "terraform.tfvars",
    "yarn.lock",
}

SENSITIVE_SPA_FALLBACK_FILENAME_PREFIXES = (
    "application.",
    "config.",
    "credentials.",
    "database.",
    "docker-compose.",
    "secret.",
    "secrets.",
    "service-account.",
    "settings.",
)

SENSITIVE_SPA_FALLBACK_SUFFIXES = (
    ".7z",
    ".bak",
    ".backup",
    ".conf",
    ".db",
    ".dump",
    ".gz",
    ".ini",
    ".log",
    ".old",
    ".orig",
    ".pem",
    ".php",
    ".py",
    ".rar",
    ".sql",
    ".sqlite",
    ".sqlite3",
    ".swp",
    ".tar",
    ".tgz",
    ".tfstate",
    ".tfvars",
    ".toml",
    ".zip",
)


def _detect_conda_env() -> str:
    conda_env = os.getenv("CONDA_DEFAULT_ENV")
    if conda_env:
        return conda_env

    prefix_parts = os.path.normpath(sys.prefix).split(os.sep)
    if "envs" in prefix_parts:
        idx = prefix_parts.index("envs")
        if idx + 1 < len(prefix_parts):
            return prefix_parts[idx + 1]
    return ""


def _should_block_spa_fallback(full_path: str) -> bool:
    """Reject scanner-style requests before the SPA catch-all returns index.html.

    The frontend needs history-mode routes such as /settings to fall back to
    index.html, but paths like /.git/config, /.env, /main.py and
    /docker-compose.yml should be a real 404 instead of a misleading 200.
    """
    normalized = (full_path or "").replace("\\", "/").strip("/")
    if not normalized:
        return False

    lowered = normalized.lower()
    segments = [segment for segment in lowered.split("/") if segment]
    if not segments:
        return False

    if any(segment.startswith(".") for segment in segments):
        return True

    root = segments[0]
    if root in SENSITIVE_SPA_FALLBACK_ROOTS:
        return True

    filename = segments[-1]
    if filename in SENSITIVE_SPA_FALLBACK_FILENAMES:
        return True
    if filename.startswith(SENSITIVE_SPA_FALLBACK_FILENAME_PREFIXES):
        return True

    return filename.endswith(SENSITIVE_SPA_FALLBACK_SUFFIXES)


def _print_runtime_info() -> None:
    current_env = _detect_conda_env()
    print(f"[Runtime] Python: {sys.executable}")
    print(f"[Runtime] Conda Env: {current_env or '未检测到'}")
    if EXPECTED_CONDA_ENV == "docker":
        return
    if current_env and current_env != EXPECTED_CONDA_ENV:
        print(
            f"[WARN] 当前环境为 '{current_env}'，推荐使用 '{EXPECTED_CONDA_ENV}' 启动，"
            "否则 Turnstile Solver 可能因依赖缺失而无法启动。"
        )
    elif not current_env:
        print(
            f"[WARN] 未检测到 conda 环境，推荐使用 '{EXPECTED_CONDA_ENV}' 启动，"
            "否则 Turnstile Solver 可能因依赖缺失而无法启动。"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _print_runtime_info()
    init_db()
    print("[OK] 数据库初始化完成")
    try:
        from services.chatgpt_core.phone_api_forwarding import (
            relay_is_configured,
            sync_phone_pool_inventory,
        )
        if relay_is_configured():
            from api.phone_pool import _repo as phone_pool_repo
            relay_sync = sync_phone_pool_inventory(
                phone_pool_repo.list(),
                trigger="startup",
            )
            if str(relay_sync.get("status") or "") == "synced":
                print(
                    "[PhonePool] API Relay 启动库存同步完成: "
                    f"inventory={int(relay_sync.get('inventory_count') or 0)} "
                    f"routes={int(relay_sync.get('route_count') or 0)}"
                )
            else:
                print(
                    "[WARN] 手机号池 API Relay 启动库存同步失败: "
                    f"{relay_sync.get('last_error') or relay_sync.get('status') or 'unknown'}"
                )
    except Exception as exc:
        print(f"[WARN] 手机号池 API Relay 启动库存同步失败: {exc}")
    print(f"[OK] 已加载核心模块: {[ChatGPTPlatform.name]}")
    from core.scheduler import scheduler
    scheduler.start()
    from services.tempmail_archive_cleanup import start as start_tempmail_archive_cleanup
    start_tempmail_archive_cleanup()
    from services.account_rate_limit_recovery import start as start_account_rate_limit_recovery
    start_account_rate_limit_recovery()
    from services.chatgpt_core.local_status_refresh import start_chatgpt_local_status_refresh_recovery
    start_chatgpt_local_status_refresh_recovery()
    try:
        from services.chatgpt_core.phone_pool_repository import (
            start_phone_pool_api_expiry_autofill,
            start_phone_pool_maintenance,
        )

        start_phone_pool_maintenance()
        start_phone_pool_api_expiry_autofill(delay_seconds=30, limit=50)
    except Exception as exc:
        print(f"[WARN] 手机号池后台维护启动失败: {exc}")
    from services.proxy_scan_scheduler import start as start_proxy_scan_scheduler
    start_proxy_scan_scheduler()
    # Idea 任务的订单轮询只在任务进程存活期间执行。服务重启后不自动恢复
    # 旧订单，避免本地任务已中断但后台继续请求上游。
    start_subscription_verification_scheduler()
    from services.solver_manager import start_async
    start_async()
    yield
    from core.scheduler import scheduler as _scheduler
    _scheduler.stop()
    from services.tempmail_archive_cleanup import stop as stop_tempmail_archive_cleanup
    stop_tempmail_archive_cleanup()
    from services.account_rate_limit_recovery import stop as stop_account_rate_limit_recovery
    stop_account_rate_limit_recovery()
    from services.chatgpt_core.local_status_refresh import stop_chatgpt_local_status_refresh_recovery
    stop_chatgpt_local_status_refresh_recovery()
    from services.chatgpt_core.phone_pool_repository import stop_phone_pool_maintenance
    stop_phone_pool_maintenance()
    from services.proxy_scan_scheduler import stop as stop_proxy_scan_scheduler
    stop_proxy_scan_scheduler()
    from services.chatgpt_core.baxigpt_status_poller import stop as stop_baxigpt_status_poller
    stop_baxigpt_status_poller()
    stop_subscription_verification_scheduler()
    from services.solver_manager import stop
    stop()


app = FastAPI(title="Account Manager", version="1.0.0", lifespan=lifespan)


@app.get("/api/health", include_in_schema=False)
def api_health():
    return {"ok": True, "service": "auto-gpt"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_API_PATHS:
        return await call_next(request)
    if path.startswith("/api/external/subscription-links"):
        return await call_next(request)
    if path.startswith("/api/external/access-tokens"):
        return await call_next(request)
    if path.startswith("/api/public/delivery-cards"):
        return await call_next(request)
    if path.startswith("/api/auth/") or not path.startswith("/api/"):
        return await call_next(request)
    from core.config_store import config_store as _cs
    try:
        password_hash = await run_in_threadpool(_cs.get, "auth_password_hash", "")
    except Exception as exc:
        logger.warning("Admin auth configuration read failed: %s", exc)
        return JSONResponse(
            {"detail": "认证存储暂时不可用，请稍后重试"},
            status_code=503,
        )
    if not password_hash:
        return JSONResponse(
            {"detail": "管理员认证尚未初始化，请先设置管理员密码"},
            status_code=503,
        )
    auth_header = request.headers.get("Authorization", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    if not token:
        return JSONResponse({"detail": "未认证，请先登录"}, status_code=401)
    try:
        from api.auth import verify_token
        await run_in_threadpool(verify_token, token)
    except HTTPException as e:
        return JSONResponse({"detail": e.detail}, status_code=e.status_code)
    except Exception as exc:
        logger.warning("Admin token verification storage failure: %s", exc)
        return JSONResponse(
            {"detail": "认证存储暂时不可用，请稍后重试"},
            status_code=503,
        )
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(accounts_router, prefix="/api")
app.include_router(chatgpt_router, prefix="/api")
app.include_router(tasks_router, prefix="/api")
app.include_router(registration_diagnostics_router, prefix="/api")
app.include_router(proxies_router, prefix="/api")
app.include_router(config_router, prefix="/api")
app.include_router(actions_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(outlook_router, prefix="/api")
app.include_router(contribution_router, prefix="/api")
app.include_router(tempmail_archive_router, prefix="/api")
app.include_router(external_subscription_router, prefix="/api")
app.include_router(external_access_tokens_router, prefix="/api")
app.include_router(phone_pool_router, prefix="/api")
app.include_router(baxigpt_cdk_pool_router, prefix="/api")
app.include_router(oaipay_router, prefix="/api")
app.include_router(delivery_cards_router, prefix="/api/admin")
app.include_router(delivery_cards_public_router, prefix="/api")
app.include_router(system_router, prefix="/api")


@app.get("/api/solver/status")
def solver_status():
    from services.solver_manager import is_running
    return {"running": is_running()}


@app.post("/api/solver/restart")
def solver_restart():
    from services.solver_manager import stop, start_async
    stop()
    start_async()
    return {"message": "重启中"}


_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/assets", StaticFiles(directory=os.path.join(_static_dir, "assets")), name="assets")

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def spa_fallback(full_path: str):
        if _should_block_spa_fallback(full_path):
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(
            os.path.join(_static_dir, "index.html"),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    reload_enabled = os.getenv("APP_RELOAD", "0").lower() in {"1", "true", "yes"}
    uvicorn.run("main:app", host=host, port=port, reload=reload_enabled)
