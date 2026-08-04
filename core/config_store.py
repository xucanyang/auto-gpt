"""全局配置持久化 - 存储在 SQLite，并在缺省时回退到环境变量/.env。"""
import os
import re
import time
from pathlib import Path
from typing import Any, Optional
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlmodel import Field, SQLModel, Session, select
from .db import engine
from .shared_config import (
    CONFIG_SHARE_BASELINE_REVISION_KEY,
    CONFIG_SHARE_DETACHED_AT_KEY,
    CONFIG_SHARE_ENABLED_KEY,
    CONFIG_SHARE_LAST_PULL_AT_KEY,
    LOCAL_ONLY_KEYS,
    filter_shareable_config,
    instance_id,
    is_shareable_key,
    shared_config_store,
)


_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _normalize_config_value(value) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _canonical_config_key(key: str) -> str:
    value = str(key or "").strip()
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _config_key_candidates(key: str) -> list[str]:
    raw = str(key or "").strip()
    if not raw:
        return []

    normalized = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_")
    candidates: list[str] = []
    seen = set()
    for item in (
        raw,
        raw.lower(),
        raw.upper(),
        normalized,
        normalized.lower(),
        normalized.upper(),
    ):
        value = str(item or "").strip()
        if value and value not in seen:
            seen.add(value)
            candidates.append(value)
    return candidates


def _load_env_file(path: Path | str | None = None) -> dict[str, str]:
    env_path = Path(path or _ENV_FILE)
    if not env_path.exists():
        return {}

    try:
        lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return {}

    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = _normalize_config_value(value)
    return values


def _runtime_env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in _load_env_file().items():
        text = _normalize_config_value(value)
        if text:
            values[key] = text
    for key, value in os.environ.items():
        text = _normalize_config_value(value)
        if text:
            values[key] = text
    return values


def _get_env_fallback_value(key: str, env_values: Optional[dict[str, str]] = None) -> str:
    values = env_values if env_values is not None else _runtime_env_values()
    for candidate in _config_key_candidates(key):
        text = str(values.get(candidate, "") or "").strip()
        if text:
            return text
    return ""


def _merge_env_fallback(values: dict[str, str], env_values: Optional[dict[str, str]] = None) -> dict[str, str]:
    merged = dict(values or {})
    runtime_values = env_values if env_values is not None else _runtime_env_values()
    for env_key, env_value in runtime_values.items():
        text = str(env_value or "").strip()
        if not text:
            continue
        canonical_key = _canonical_config_key(env_key)
        for target_key in (env_key, canonical_key):
            if not target_key:
                continue
            if str(merged.get(target_key, "") or "").strip():
                continue
            merged[target_key] = text
    return merged


class ConfigItem(SQLModel, table=True):
    __tablename__ = "configs"
    key: str = Field(primary_key=True)
    value: str = ""


class ConfigStore:
    """简单 key-value 配置存储"""

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}
        self._warm_cache()

    def _warm_cache(self) -> None:
        try:
            with Session(engine) as s:
                items = s.exec(select(ConfigItem)).all()
                for item in items:
                    text = str(item.value or "").strip()
                    if text:
                        self._cache[item.key] = text
        except SQLAlchemyError:
            pass

    def _get_local(self, key: str, default: str = "", *, env_values: Optional[dict[str, str]] = None) -> str:
        env_values = env_values if env_values is not None else _runtime_env_values()
        last_error: SQLAlchemyError | None = None
        for attempt in range(3):
            try:
                with Session(engine) as s:
                    item = s.get(ConfigItem, key)
                    value = str(item.value if item else "" or "").strip()
                    if value:
                        self._cache[key] = value
                        return value
                    break
            except SQLAlchemyError as exc:
                last_error = exc
                retry_locked = (
                    isinstance(exc, OperationalError)
                    and "database is locked" in str(exc).lower()
                    and attempt < 2
                )
                if not retry_locked:
                    break
                time.sleep(0.05 * (attempt + 1))

        if last_error is not None:
            cached = str(self._cache.get(key, "") or "").strip()
            if cached:
                return cached
        fallback = _get_env_fallback_value(key, env_values=env_values)
        value = fallback or default
        if value:
            self._cache[key] = value
        return value

    def _set_local(self, key: str, value: str) -> None:
        with Session(engine) as s:
            item = s.get(ConfigItem, key)
            if item:
                item.value = value
            else:
                item = ConfigItem(key=key, value=value)
            s.add(item)
            s.commit()
        text = str(value or "").strip()
        if text:
            self._cache[key] = text
        else:
            self._cache.pop(key, None)

    def _get_all_local(self) -> dict:
        env_values = _runtime_env_values()
        try:
            with Session(engine) as s:
                items = s.exec(select(ConfigItem)).all()
                values = {i.key: i.value for i in items}
                for key, value in values.items():
                    text = str(value or "").strip()
                    if text:
                        self._cache[key] = text
        except SQLAlchemyError:
            values = dict(self._cache)
        return _merge_env_fallback(values, env_values=env_values)

    def _set_many_local(self, data: dict) -> None:
        with Session(engine) as s:
            for key, value in data.items():
                item = s.get(ConfigItem, key)
                if item:
                    item.value = value
                else:
                    item = ConfigItem(key=key, value=value)
                s.add(item)
            s.commit()
        for key, value in data.items():
            text = str(value or "").strip()
            if text:
                self._cache[key] = text
            else:
                self._cache.pop(key, None)

    def get_local_all(self) -> dict:
        """只读取本实例本地配置，不叠加共享模板；保留环境变量兜底。"""
        return self._get_all_local()

    def get_saved_local_all(self) -> dict:
        """只读取本实例 configs 表中已保存的值，不叠加环境变量兜底。"""
        try:
            with Session(engine) as s:
                items = s.exec(select(ConfigItem)).all()
                values = {i.key: i.value for i in items}
                for key, value in values.items():
                    text = str(value or "").strip()
                    if text:
                        self._cache[key] = text
                    else:
                        self._cache.pop(key, None)
                return values
        except SQLAlchemyError:
            return dict(self._cache)

    def shared_enabled(self) -> bool:
        raw = self._get_local(CONFIG_SHARE_ENABLED_KEY, os.getenv("CONFIG_SHARE_ENABLED", "false"))
        return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}

    def get_share_state(self) -> dict[str, Any]:
        meta = shared_config_store.meta()
        return {
            "instance_id": instance_id(),
            "enabled": self.shared_enabled(),
            "mode": "shared" if self.shared_enabled() else "local",
            "baseline_revision": self._get_local(CONFIG_SHARE_BASELINE_REVISION_KEY, ""),
            "detached_at": self._get_local(CONFIG_SHARE_DETACHED_AT_KEY, ""),
            "last_pull_at": self._get_local(CONFIG_SHARE_LAST_PULL_AT_KEY, ""),
            "shared": meta,
            "local_only_keys": sorted(LOCAL_ONLY_KEYS),
        }

    def enable_shared(self, *, pull: bool = True) -> dict[str, Any]:
        if pull:
            self.pull_shared_to_local()
        revision = shared_config_store.revision()
        self._set_many_local({
            CONFIG_SHARE_ENABLED_KEY: "true",
            CONFIG_SHARE_BASELINE_REVISION_KEY: str(revision),
            CONFIG_SHARE_DETACHED_AT_KEY: "",
        })
        return self.get_share_state()

    def disable_shared(self) -> dict[str, Any]:
        # 脱离共享前先把当前共享模板落成本地基线，避免页面突然回退到旧本地配置。
        if self.shared_enabled():
            self.pull_shared_to_local()
        revision = shared_config_store.revision()
        self._set_many_local({
            CONFIG_SHARE_ENABLED_KEY: "false",
            CONFIG_SHARE_BASELINE_REVISION_KEY: str(revision),
            CONFIG_SHARE_DETACHED_AT_KEY: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        return self.get_share_state()

    def pull_shared_to_local(self) -> dict[str, Any]:
        data = shared_config_store.get_all()
        if data:
            self._set_many_local(data)
        revision = shared_config_store.revision()
        self._set_many_local({
            CONFIG_SHARE_BASELINE_REVISION_KEY: str(revision),
            CONFIG_SHARE_LAST_PULL_AT_KEY: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        return {"ok": True, "revision": revision, "updated": len(data)}

    def push_to_shared(
        self,
        data: dict[str, Any],
        *,
        replace: bool = True,
        base_revision: int | None = None,
        action: str = "push",
        note: str = "",
    ) -> dict[str, Any]:
        safe = filter_shareable_config(data)
        result = shared_config_store.write(
            safe,
            replace=replace,
            base_revision=base_revision,
            updated_by=instance_id(),
            action=action,
            note=note,
        )
        # 当前实例保留一份本地镜像，便于脱离共享或共享源临时不可用时兜底。
        if safe:
            self._set_many_local(safe)
        self._set_many_local({
            CONFIG_SHARE_BASELINE_REVISION_KEY: str(result.get("revision") or shared_config_store.revision()),
            CONFIG_SHARE_LAST_PULL_AT_KEY: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        return result

    def get(self, key: str, default: str = "") -> str:
        # Local-only keys (notably auth_*) must not spend an extra connection
        # checking the shared-config mode on every request.
        if is_shareable_key(key) and self.shared_enabled():
            try:
                found, value = shared_config_store.get_entry(key)
                if found:
                    text = str(value or "")
                    if text.strip():
                        self._cache[key] = text
                    else:
                        self._cache.pop(key, None)
                    return text
            except Exception:
                pass
        return self._get_local(key, default)

    def set(self, key: str, value: str) -> None:
        self.set_many({key: value})

    def get_all(self) -> dict:
        values = self._get_all_local()
        if self.shared_enabled():
            try:
                shared_values = shared_config_store.get_all()
                for key, value in shared_values.items():
                    if is_shareable_key(key):
                        values[key] = value
                        text = str(value or "").strip()
                        if text:
                            self._cache[key] = text
                        else:
                            self._cache.pop(key, None)
            except Exception:
                # 共享 DB 短暂不可用时保留本地镜像/环境变量兜底。
                pass
        return values

    def set_many(self, data: dict, *, base_revision: int | None = None) -> None:
        if not self.shared_enabled():
            self._set_many_local(data)
            return

        shared_updates = {k: v for k, v in (data or {}).items() if is_shareable_key(k)}
        local_updates = {k: v for k, v in (data or {}).items() if not is_shareable_key(k)}
        if shared_updates:
            result = shared_config_store.write(
                shared_updates,
                replace=False,
                base_revision=base_revision,
                updated_by=instance_id(),
                action="update",
                note="settings-save",
            )
            # 本地镜像同步保存，便于关闭共享时无缝切换。
            self._set_many_local(shared_updates)
            self._set_many_local({
                CONFIG_SHARE_BASELINE_REVISION_KEY: str(result.get("revision") or shared_config_store.revision()),
                CONFIG_SHARE_LAST_PULL_AT_KEY: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
        if local_updates:
            self._set_many_local(local_updates)


config_store = ConfigStore()
