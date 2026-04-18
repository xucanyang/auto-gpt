"""插件拉取 / 启停管理（仅保留 ChatGPT 相关 CLIProxyAPI）"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import requests

_ROOT = Path(__file__).resolve().parents[2]
_EXT_ROOT = _ROOT / "_ext_targets"
_LOG_ROOT = Path(__file__).resolve().parent / "external_logs"
_LOG_ROOT.mkdir(parents=True, exist_ok=True)

_REMOTE_URLS = {
    "cliproxyapi": "https://github.com/router-for-me/CLIProxyAPI.git",
}

_SERVICE_META = {
    "cliproxyapi": {
        "label": "CLIProxyAPI",
        "repo_name": "CLIProxyAPI",
        "url": "http://127.0.0.1:8317",
        "health": "http://127.0.0.1:8317/",
        "management_url": "http://127.0.0.1:8317/management.html",
        "port": 8317,
        "kind": "web",
    },
}

_PROCS: dict[str, subprocess.Popen] = {}
_LOG_FILES: dict[str, Any] = {}
_LAST_ERROR: dict[str, str] = {}
_LOCK = threading.Lock()


def _get_setting(key: str, default: str = "") -> str:
    try:
        from core.config_store import config_store

        value = str(config_store.get(key, "") or "").strip()
        return value or default
    except Exception:
        return default


def _creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _repo_path(name: str) -> Path:
    return _EXT_ROOT / _SERVICE_META[name]["repo_name"]


def _log_path(name: str) -> Path:
    return _LOG_ROOT / f"{name}.log"


def _close_log(name: str):
    f = _LOG_FILES.pop(name, None)
    if f:
        try:
            f.close()
        except Exception:
            pass


def _open_log(name: str):
    _close_log(name)
    f = open(_log_path(name), "a", encoding="utf-8")
    _LOG_FILES[name] = f
    return f


def _clone_repo_if_missing(name: str):
    repo = _repo_path(name)
    if repo.exists():
        return
    repo.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", _REMOTE_URLS[name], str(repo)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_creationflags(),
    )


def install(name: str) -> dict[str, Any]:
    if name not in _SERVICE_META:
        raise KeyError(name)
    _clone_repo_if_missing(name)
    return _status_one(name)


def _health_ok(name: str) -> bool:
    url = _SERVICE_META[name].get("health") or ""
    if not url:
        return False
    try:
        resp = requests.get(url, timeout=5)
        return resp.ok
    except Exception:
        return False


def _find_pid_by_port(port: int) -> int | None:
    if port <= 0:
        return None
    try:
        result = subprocess.run(
            ["bash", "-lc", f"lsof -ti tcp:{int(port)}"],
            capture_output=True,
            text=True,
            creationflags=_creationflags(),
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
    except Exception:
        return None
    return None


def _proc_running(name: str) -> bool:
    proc = _PROCS.get(name)
    return bool(proc and proc.poll() is None)


def _status_one(name: str) -> dict[str, Any]:
    meta = _SERVICE_META[name]
    repo = _repo_path(name)
    proc = _PROCS.get(name)
    running = _health_ok(name)
    pid = proc.pid if proc and proc.poll() is None else None
    if running:
        pid = _find_pid_by_port(int(meta.get("port") or 0)) or pid
    return {
        "name": name,
        "label": meta["label"],
        "repo_path": str(repo),
        "repo_exists": repo.exists(),
        "url": meta.get("url", ""),
        "management_url": meta.get("management_url", ""),
        "management_key": _get_setting("cliproxyapi_management_key", "cliproxyapi"),
        "running": running,
        "pid": pid,
        "log_path": str(_log_path(name)),
        "last_error": _LAST_ERROR.get(name, ""),
        "kind": meta["kind"],
    }


def list_status() -> list[dict[str, Any]]:
    return [_status_one(name) for name in _SERVICE_META]


def _find_go() -> str | None:
    for candidate in ("go", str(Path.home() / "go" / "bin" / "go")):
        result = subprocess.run(
            ["bash", "-lc", f"command -v {candidate!s} 2>/dev/null || true"],
            capture_output=True,
            text=True,
            creationflags=_creationflags(),
        )
        path = result.stdout.strip()
        if path:
            return path
        candidate_path = Path(candidate)
        if candidate_path.exists():
            return str(candidate_path)
    return None


def _ensure_cliproxyapi_runtime_config(repo: Path):
    config_path = repo / "config.local.yaml"
    if not config_path.exists():
        shutil_source = repo / "config.example.yaml"
        if shutil_source.exists():
            config_path.write_text(shutil_source.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            config_path.write_text("server:\n", encoding="utf-8")
    secret = _get_setting("cliproxyapi_management_key", "cliproxyapi")
    lines = config_path.read_text(encoding="utf-8").splitlines()
    updated_lines = []
    replaced = False
    for line in lines:
        if line.lstrip().startswith("secret-key:"):
            indent = line[: len(line) - len(line.lstrip())]
            updated_lines.append(f'{indent}secret-key: "{secret}"')
            replaced = True
        else:
            updated_lines.append(line)
    if not replaced:
        updated_lines.append(f'  secret-key: "{secret}"')
    config_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")


def _build_command(name: str) -> tuple[list[str], Path]:
    if name != "cliproxyapi":
        raise KeyError(name)
    repo = _repo_path(name)
    go_exe = _find_go()
    if not go_exe:
        raise RuntimeError("未找到 go，可在设置中先安装 Go 或将 go 加入 PATH")
    _ensure_cliproxyapi_runtime_config(repo)
    config_path = repo / "config.local.yaml"
    return [go_exe, "run", "./cmd/server", "-config", str(config_path)], repo


def start(name: str) -> dict[str, Any]:
    with _LOCK:
        if name not in _SERVICE_META:
            raise KeyError(name)
        repo = _repo_path(name)
        if not repo.exists():
            raise RuntimeError(f"{_SERVICE_META[name]['label']} 未安装，请先在插件页点击“安装”")
        if _status_one(name)["running"]:
            return _status_one(name)

        log_file = _open_log(name)
        try:
            command, cwd = _build_command(name)
            proc = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=_creationflags(),
            )
            _PROCS[name] = proc
            _LAST_ERROR[name] = ""
        except Exception as e:
            _LAST_ERROR[name] = str(e)
            _close_log(name)
            raise

    for _ in range(90):
        time.sleep(1)
        if _health_ok(name):
            return _status_one(name)
        proc = _PROCS.get(name)
        if proc and proc.poll() is not None:
            _LAST_ERROR[name] = f"启动失败，退出码={proc.returncode}"
            return _status_one(name)
    _LAST_ERROR[name] = "启动超时"
    return _status_one(name)


def stop(name: str) -> dict[str, Any]:
    with _LOCK:
        proc = _PROCS.get(name)
        port_pid = _find_pid_by_port(int(_SERVICE_META[name].get("port") or 0))
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:
                proc.kill()
        if port_pid and (not proc or port_pid != proc.pid):
            subprocess.run(
                ["kill", "-9", str(port_pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_creationflags(),
            )
        _PROCS.pop(name, None)
        _close_log(name)

    for _ in range(10):
        if not _health_ok(name):
            break
        time.sleep(1)
    return _status_one(name)


def start_all() -> list[dict[str, Any]]:
    results = []
    for name in _SERVICE_META:
        try:
            if not _repo_path(name).exists():
                item = _status_one(name)
                item["last_error"] = "未安装；如需使用请先手动安装"
                results.append(item)
            else:
                results.append(start(name))
        except Exception:
            results.append(_status_one(name))
    return results


def stop_all() -> list[dict[str, Any]]:
    return [stop(name) for name in _SERVICE_META]
