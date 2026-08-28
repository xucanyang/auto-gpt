"""Turnstile Solver 进程管理 - 后端启动时自动拉起"""
import subprocess
from subprocess import TimeoutExpired
import sys
import os
import time
import threading
import requests

_proc: subprocess.Popen = None
_log_file = None
_lock = threading.Lock()
_restart_lock = threading.Lock()
SOLVER_MAX_BROWSERS_LIMIT = 15


def _solver_enabled() -> bool:
    return os.getenv("APP_ENABLE_SOLVER", "1").lower() not in {"0", "false", "no"}


def _solver_port() -> int:
    return int(os.getenv("SOLVER_PORT", "8889"))


def _solver_url() -> str:
    return (os.getenv("LOCAL_SOLVER_URL") or f"http://127.0.0.1:{_solver_port()}").rstrip("/")


def _solver_bind_host() -> str:
    return os.getenv("SOLVER_BIND_HOST", "0.0.0.0")


def _solver_browser_type() -> str:
    return str(os.getenv("SOLVER_BROWSER_TYPE", "chromium") or "chromium").strip().lower()


def _runtime_solver_value(key: str, env_name: str, default: str) -> str:
    try:
        from core.config_store import config_store

        stored = str(config_store.get(key, "") or "").strip()
    except Exception:
        stored = ""
    return stored or str(os.getenv(env_name, default) or default).strip()


def _solver_max_browsers() -> int:
    raw = _runtime_solver_value(
        "chatgpt_runtime_solver_max_browsers",
        "SOLVER_MAX_BROWSERS",
        "4",
    )
    try:
        return max(1, min(int(float(raw)), SOLVER_MAX_BROWSERS_LIMIT))
    except Exception:
        return 4


def _solver_warm_browsers(max_browsers: int) -> int:
    raw = _runtime_solver_value(
        "chatgpt_runtime_solver_warm_browsers",
        "SOLVER_WARM_BROWSERS",
        "0",
    )
    try:
        return max(0, min(int(float(raw)), max_browsers))
    except Exception:
        return 0


def _solver_idle_timeout_seconds() -> int:
    raw = _runtime_solver_value(
        "chatgpt_runtime_solver_idle_timeout_seconds",
        "SOLVER_IDLE_TIMEOUT_SECONDS",
        "300",
    )
    try:
        return max(30, min(int(float(raw)), 86400))
    except Exception:
        return 300


def _solver_pool_mode() -> str:
    mode = _runtime_solver_value(
        "chatgpt_runtime_solver_mode",
        "SOLVER_POOL_MODE",
        "auto",
    ).lower()
    return mode if mode in {"auto", "fixed"} else "auto"


def is_running() -> bool:
    try:
        r = requests.get(f"{_solver_url()}/health", timeout=2)
        return r.status_code < 500
    except Exception:
        try:
            r = requests.get(f"{_solver_url()}/", timeout=2)
            return r.status_code < 500
        except Exception:
            return False


def get_status() -> dict:
    try:
        response = requests.get(f"{_solver_url()}/health", timeout=2)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            return {"running": True, **payload}
    except Exception:
        pass
    return {
        "running": is_running(),
        "mode": _solver_pool_mode(),
        "max_browsers": _solver_max_browsers(),
        "warm_browsers": _solver_warm_browsers(_solver_max_browsers()),
        "idle_timeout_seconds": _solver_idle_timeout_seconds(),
    }


def start():
    global _proc, _log_file
    with _lock:
        if not _solver_enabled():
            print("[Solver] 已禁用，跳过自动启动")
            return
        if is_running():
            print("[Solver] 已在运行")
            return
        solver_script = os.path.join(
            os.path.dirname(__file__), "turnstile_solver", "start.py"
        )
        log_path = os.path.join(
            os.path.dirname(__file__), "turnstile_solver", "solver.log"
        )
        _log_file = open(log_path, "a", encoding="utf-8")
        max_browsers = _solver_max_browsers()
        child_env = dict(os.environ)
        child_env.update(
            {
                "SOLVER_POOL_MODE": _solver_pool_mode(),
                "SOLVER_MAX_BROWSERS": str(max_browsers),
                "SOLVER_WARM_BROWSERS": str(
                    _solver_warm_browsers(max_browsers)
                ),
                "SOLVER_IDLE_TIMEOUT_SECONDS": str(
                    _solver_idle_timeout_seconds()
                ),
            }
        )
        command = [
            sys.executable,
            "-u",
            solver_script,
            "--browser_type",
            _solver_browser_type(),
            "--thread",
            str(max_browsers),
            "--host",
            _solver_bind_host(),
            "--port",
            str(_solver_port()),
        ]
        if (
            _solver_browser_type() in {"chromium", "chrome", "msedge"}
            and str(child_env.get("AUTO_GPT_XVFB") or "").strip() == "1"
            and str(child_env.get("DISPLAY") or "").strip()
        ):
            command.append("--no-headless")
        _proc = subprocess.Popen(
            command,
            stdout=_log_file,
            stderr=subprocess.STDOUT,
            env=child_env,
        )
        # 等待服务就绪（最多30s）
        for _ in range(30):
            time.sleep(1)
            if is_running():
                print(f"[Solver] 已启动 PID={_proc.pid}")
                return
            if _proc.poll() is not None:
                print(f"[Solver] 启动失败，退出码={_proc.returncode}，日志: {log_path}")
                _proc = None
                if _log_file:
                    _log_file.close()
                    _log_file = None
                return
        print(f"[Solver] 启动超时，日志: {log_path}")


def stop():
    global _proc, _log_file
    with _lock:
        if _proc and _proc.poll() is None:
            try:
                _proc.terminate()
                _proc.wait(timeout=5)
            except TimeoutExpired:
                print("[Solver] 停止超时，强制结束进程")
                _proc.kill()
                _proc.wait(timeout=5)
            print("[Solver] 已停止")
        _proc = None
        if _log_file:
            _log_file.close()
            _log_file = None


def start_async():
    """在后台线程启动，不阻塞主进程"""
    t = threading.Thread(target=start, daemon=True)
    t.start()


def restart_async() -> None:
    """Apply persisted pool settings without blocking the config request."""

    def _restart() -> None:
        with _restart_lock:
            stop()
            start()

    threading.Thread(target=_restart, name="solver-restart", daemon=True).start()
