"""Lightweight persisted Focus checklist paired with the Pomodoro timer."""

from __future__ import annotations

import json
import os
from typing import ClassVar

from gi.repository import GObject

MAX_TODOS = 20
MAX_TODO_CHARS = 140


class TodoService(GObject.Object):
    """Persisted short list of focus tasks shown in the Focus Timer popover."""

    __gsignals__: ClassVar[dict] = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, storage_path: str | None = None):
        super().__init__()
        data_home = os.environ.get(
            "XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share")
        )
        self.storage_path = storage_path or os.path.join(
            data_home, "ledge-pill", "todos.json"
        )
        self.items: list[dict] = []  # [{"text": str, "done": bool}]
        self._load()

    def add(self, text: str) -> bool:
        clean = " ".join(str(text or "").split())[:MAX_TODO_CHARS]
        if not clean:
            return False
        self.items.insert(0, {"text": clean, "done": False})
        del self.items[MAX_TODOS:]
        self._save()
        self.emit("changed")
        return True

    def toggle(self, index: int) -> bool:
        if not (0 <= index < len(self.items)):
            return False
        self.items[index]["done"] = not bool(self.items[index].get("done"))
        self._save()
        self.emit("changed")
        return bool(self.items[index]["done"])

    def remove(self, index: int) -> None:
        if 0 <= index < len(self.items):
            self.items.pop(index)
            self._save()
            self.emit("changed")

    def clear_done(self) -> int:
        before = len(self.items)
        self.items = [it for it in self.items if not it.get("done")]
        removed = before - len(self.items)
        if removed:
            self._save()
            self.emit("changed")
        return removed

    def active_task(self) -> str:
        for it in self.items:
            if not it.get("done") and it.get("text"):
                return str(it["text"])
        return ""

    def pending_count(self) -> int:
        return sum(1 for it in self.items if not it.get("done"))

    def _load(self):
        try:
            with open(self.storage_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.items = [
                    {
                        "text": str(row["text"])[:MAX_TODO_CHARS],
                        "done": bool(row.get("done")),
                    }
                    for row in data[:MAX_TODOS]
                    if isinstance(row, dict)
                    and isinstance(row.get("text"), str)
                    and row["text"].strip()
                ]
        except (OSError, ValueError):
            self.items = []

    def _save(self):
        try:
            parent = os.path.dirname(self.storage_path)
            os.makedirs(parent or ".", mode=0o700, exist_ok=True)
            tmp = self.storage_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.items[:MAX_TODOS], f, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.storage_path)
        except OSError:
            pass
