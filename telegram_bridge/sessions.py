"""JSON-backed per-user state storage with dotted-path access.

Per-user values are stored under dotted paths and saved to a JSON file.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from pathlib import Path
from typing import Any


log = logging.getLogger("owui-telegram")


class SessionStore:
    """Store arbitrary JSON-compatible values under each Telegram user.

    Paths are dot-separated and scoped to a user, for example::

        store.set(user_id, "settings.model", "qwen3.6-35b-a3b")
        store.get(user_id, "outbound_messages.685.user_id")

    ``set`` creates missing dictionaries and saves the file immediately.
    Path names are not checked against a schema; callers own their spelling
    and the shape of the values they write.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"users": {}}

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not load sessions from %s: %s", self.path, exc)
            return {"users": {}}

        if not isinstance(data, dict) or not isinstance(data.get("users", {}), dict):
            log.warning("Ignoring sessions file with an invalid root structure: %s", self.path)
            return {"users": {}}

        data.setdefault("users", {})
        return data

    def get(
        self,
        user_id: int | str,
        path: str,
        default_value: Any = None,
    ) -> Any:
        """Return the value at a path, or ``default_value`` if absent or None."""
        parts = self._split_path(path)
        with self._lock:
            value: Any = self._data["users"].get(str(user_id), {})
            for part in parts:
                if not isinstance(value, dict) or part not in value:
                    return copy.deepcopy(default_value)
                value = value[part]
            if value is None:
                return copy.deepcopy(default_value)
            return copy.deepcopy(value)

    def set(self, user_id: int | str, path: str, value: Any) -> None:
        """Set a value at a dotted path and persist the updated JSON file."""
        parts = self._split_path(path)
        with self._lock:
            user = self._data["users"].setdefault(str(user_id), {})
            if not isinstance(user, dict):
                raise TypeError(f"Session data for user {user_id} is not an object")

            target = user
            for part in parts[:-1]:
                child = target.setdefault(part, {})
                if not isinstance(child, dict):
                    raise TypeError(f"Session path component {part!r} is not an object")
                target = child
            target[parts[-1]] = copy.deepcopy(value)
            self._save()

    @staticmethod
    def _split_path(path: str) -> list[str]:
        parts = path.split(".")
        if not path or any(not part for part in parts):
            raise ValueError("Session path must contain non-empty dot-separated parts")
        return parts

    def _save(self) -> None:
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp_path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp_path.replace(self.path)
        except (OSError, TypeError, ValueError) as exc:
            log.warning("Could not save sessions to %s: %s", self.path, exc)
