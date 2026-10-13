"""Durable output preferences and the last local workspace, shared by both hosts."""

from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path
from typing import Any

MAX_WORKSPACE_BYTES = 512 * 1024
_store_locks: dict[Path, Any] = {}
_store_locks_guard = threading.Lock()


def _workspace_defaults() -> dict[str, Any]:
    return {
        "source_dir": "", "output_dir": "", "output_follows_source": True,
        "output_name": "", "output_name_touched": False, "settings": {},
        "recent_sources": [], "assignments": {}, "image_paths": [],
    }


def _normalize_workspace(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("工作区设置必须是一个对象。")
    result = _workspace_defaults()
    for name in ("source_dir", "output_dir", "output_name"):
        value = data.get(name, result[name])
        if not isinstance(value, str) or len(value) > 32767 or "\x00" in value:
            raise ValueError(f"无效的工作区字段：{name}")
        result[name] = value
    for name in ("output_follows_source", "output_name_touched"):
        value = data.get(name, result[name])
        if not isinstance(value, bool):
            raise ValueError(f"无效的工作区字段：{name}")
        result[name] = value
    settings = data.get("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("输出设置必须是一个对象。")
    for key, value in settings.items():
        if not isinstance(key, str) or not isinstance(value, (str, bool, int, float)):
            raise ValueError("输出设置包含无效值。")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("输出设置包含无效数值。")
    result["settings"] = settings
    for name in ("recent_sources", "image_paths"):
        values = data.get(name, [])
        if not isinstance(values, list) or not all(
            isinstance(item, str) and len(item) <= 32767 and "\x00" not in item
            for item in values
        ):
            raise ValueError(f"{name} 必须是路径列表。")
        result[name] = list(dict.fromkeys(item for item in values if item))
    result["recent_sources"] = result["recent_sources"][:12]
    assignments = data.get("assignments", {})
    if not isinstance(assignments, dict) or not all(
        isinstance(key, str) and (value is None or isinstance(value, str))
        for key, value in assignments.items()
    ):
        raise ValueError("图片分配必须是文件名映射。")
    result["assignments"] = assignments
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_WORKSPACE_BYTES:
        raise ValueError("工作区设置过大。")
    return result


class WorkspaceStore:
    """Keep common settings even when the last project is missing or reset."""

    def __init__(self, root: Path) -> None:
        self.path = root / "workspace.json"
        self.settings_path = root / "settings.json"
        with _store_locks_guard:
            self._lock = _store_locks.setdefault(root.resolve(), threading.RLock())

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        if path.stat().st_size > MAX_WORKSPACE_BYTES:
            raise ValueError("设置文件过大。")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("设置文件必须是一个对象。")
        return value

    @staticmethod
    def _write(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                current = _normalize_workspace(self._read(self.path))
            except (OSError, ValueError, UnicodeError):
                current = _workspace_defaults()
            try:
                settings = self._read(self.settings_path)
                current["settings"] = _normalize_workspace({"settings": settings})["settings"]
            except FileNotFoundError:
                # One-time migration from the first desktop build. Keep the old
                # workspace file intact, including when migration cannot write.
                if current["settings"]:
                    self._write(self.settings_path, current["settings"])
            except (OSError, ValueError, UnicodeError):
                pass
            return current

    def save(self, data: dict[str, Any]) -> bool:
        with self._lock:
            if not isinstance(data, dict):
                raise ValueError("工作区设置必须是一个对象。")
            current = self.load()
            current.update(data)
            normalized = _normalize_workspace(current)
            settings = normalized.pop("settings")
            self._write(self.settings_path, settings)
            self._write(self.path, normalized)
        return True
