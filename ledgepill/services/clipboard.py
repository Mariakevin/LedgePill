"""Clipboard history service.

Monitors the default GTK4 clipboard, persists recent/pinned text entries with
owner-only atomic writes, prevents self-copy bounce loops, and optionally syncs
Wayland clipboard via wl-paste when the overlay window is unfocused.
"""

from __future__ import annotations

import json
import os
import time
from typing import ClassVar

import gi

gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gio, GLib, GObject

MAX_HISTORY = 40
MAX_PINNED = 15
MAX_ENTRY_CHARS = 8192


class ClipboardService(GObject.Object):
    __gsignals__: ClassVar[dict] = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, storage_path: str | None = None):
        super().__init__()
        data_home = os.environ.get(
            "XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share")
        )
        self.storage_path = storage_path or os.path.join(
            data_home, "ledge-pill", "clipboard.json"
        )
        self.history: list[str] = []
        self.pinned: list[str] = []
        self._suppressed: set[str] = set()
        self._suppress_until = 0.0
        self._sync_gen = 0
        self._dirty = False
        self._save_timer = 0
        self._wl_busy = False
        self._last_sync = 0.0
        self._ignore_text = ""
        self._ignore_until = 0.0
        self._stopped = False
        self._load()

        self._clipboard: Gdk.Clipboard | None = None
        self._handler = 0
        self._has_wl_paste = bool(GLib.find_program_in_path("wl-paste"))
        self._has_wl_copy = bool(GLib.find_program_in_path("wl-copy"))
        display = Gdk.Display.get_default()
        if display is not None:
            self._clipboard = display.get_clipboard()
            if self._clipboard is not None:
                self._handler = self._clipboard.connect("changed", self._on_changed)

    def stop(self):
        self._stopped = True
        if self._clipboard is not None and self._handler:
            self._clipboard.disconnect(self._handler)
            self._handler = 0
        if self._save_timer:
            GLib.source_remove(self._save_timer)
            self._save_timer = 0
        if self._dirty:
            self._save_now()

    def _clear_os_clipboard(self):
        cb = self._clipboard
        if cb is None:
            display = Gdk.Display.get_default()
            cb = display.get_clipboard() if display is not None else None
        if cb is not None and hasattr(cb, "set_content"):
            try:
                cb.set_content(None)
            except (GLib.Error, TypeError):
                pass
        if self._has_wl_copy:
            try:
                Gio.Subprocess.new(
                    ["wl-copy", "--clear"],
                    Gio.SubprocessFlags.STDOUT_SILENCE
                    | Gio.SubprocessFlags.STDERR_SILENCE,
                )
            except GLib.Error:
                pass

    def clear(self, include_pinned: bool = False):
        changed = False
        prev_top = self.history[0] if self.history else ""
        if include_pinned:
            removed = list(self.history)
            if self.history or self.pinned:
                self.history.clear()
                self.pinned.clear()
                changed = True
        else:
            removed = [t for t in self.history if t not in self.pinned]
            if len(self.history) > len(self.pinned) or (
                not self.pinned and self.history
            ):
                self.history = list(self.pinned)
                changed = True
        if changed:
            self._sync_gen += 1
            self._suppressed.update(removed)
            self._suppress_until = time.monotonic() + 2.5
            if not self.history or prev_top not in self.history:
                self._clear_os_clipboard()
            self._save_now()
            self.emit("changed", "")

    def remove(self, index: int) -> None:
        if 0 <= index < len(self.history):
            text = self.history.pop(index)
            if text in self.pinned:
                self.pinned.remove(text)
            self._sync_gen += 1
            self._suppressed.add(text)
            self._suppress_until = time.monotonic() + 2.5
            if index == 0 or not self.history:
                self._clear_os_clipboard()
            self._save_now()
            self.emit("changed", "")

    def toggle_pin(self, index: int) -> bool:
        if not (0 <= index < len(self.history)):
            return False
        text = self.history[index]
        if text in self.pinned:
            self.pinned.remove(text)
            pinned_now = False
        else:
            self.pinned.insert(0, text)
            del self.pinned[MAX_PINNED:]
            pinned_now = True
        self._rebuild_order()
        self._save_now()
        self.emit("changed", "")
        return pinned_now

    def copy_text(self, text: str) -> bool:
        if not text:
            return False
        self._suppressed.discard(text)
        self._ignore_text = text
        self._ignore_until = time.monotonic() + 1.5
        copied = False
        cb = self._clipboard
        if cb is None:
            display = Gdk.Display.get_default()
            cb = display.get_clipboard() if display is not None else None
        if cb is not None:
            try:
                cb.set(text)
                copied = True
            except (GLib.Error, TypeError):
                pass
        if self._has_wl_copy:
            try:
                proc = Gio.Subprocess.new(
                    ["wl-copy", "--type", "text/plain;charset=utf-8"],
                    Gio.SubprocessFlags.STDIN_PIPE
                    | Gio.SubprocessFlags.STDOUT_SILENCE
                    | Gio.SubprocessFlags.STDERR_SILENCE,
                )
                proc.communicate_utf8_async(text, None, None)
                copied = True
            except GLib.Error:
                pass
        return copied

    def copy_item(self, index: int) -> bool:
        if 0 <= index < len(self.history):
            return self.copy_text(self.history[index])
        return False

    def search(self, query: str) -> list[tuple[int, str]]:
        q = (query or "").strip().casefold()
        if not q:
            return list(enumerate(self.history))
        return [(i, t) for i, t in enumerate(self.history) if q in t.casefold()]

    def sync_external(self):
        """Pull current Wayland text clipboard if wl-paste is available."""
        if self._stopped or not self._has_wl_paste or self._wl_busy:
            return
        now = time.monotonic()
        if now - self._last_sync < 1.0:
            return
        self._last_sync = now
        self._wl_busy = True
        gen = self._sync_gen
        try:
            proc = Gio.Subprocess.new(
                ["wl-paste", "--no-newline", "--type", "text/plain;charset=utf-8"],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE,
            )
        except GLib.Error:
            self._wl_busy = False
            return
        proc.communicate_utf8_async(
            None, None, lambda p, r, g=gen: self._on_wl_paste(p, r, g)
        )

    def _on_wl_paste(self, proc, res, gen: int | None = None):
        self._wl_busy = False
        if self._stopped or (gen is not None and gen != self._sync_gen):
            return
        try:
            _ok, text, _err = proc.communicate_utf8_finish(res)
        except GLib.Error:
            return
        if proc.get_successful() and text:
            if text in self._suppressed:
                return
            if self._suppressed:
                self._suppressed.clear()
            self._record(text)

    def _on_changed(self, clipboard):
        if not self._stopped:
            clipboard.read_text_async(None, self._on_text)

    def _on_text(self, clipboard, res):
        if self._stopped:
            return
        try:
            text = clipboard.read_text_finish(res)
        except (GLib.Error, TypeError):
            text = None
        if text:
            if text in self._suppressed and time.monotonic() < self._suppress_until:
                return
            if self._suppressed and text not in self._suppressed:
                self._suppressed.clear()
            self._record(text)

    def _record(self, text: str):
        if not isinstance(text, str):
            return
        if len(text) > MAX_ENTRY_CHARS:
            text = text[:MAX_ENTRY_CHARS]
        if not text.strip():
            return
        if text == self._ignore_text and time.monotonic() < self._ignore_until:
            return
        if self.history and self.history[0] == text:
            return
        if text in self.pinned:
            # Keep pinned position stable unless explicitly reordered
            return
        unpinned = [t for t in self.history if t not in self.pinned and t != text]
        unpinned.insert(0, text)
        max_unpinned = max(5, MAX_HISTORY - len(self.pinned))
        del unpinned[max_unpinned:]
        self.history = list(self.pinned) + unpinned
        self._schedule_save()
        self.emit("changed", text)

    def _rebuild_order(self):
        unpinned = [t for t in self.history if t not in self.pinned]
        max_unpinned = max(5, MAX_HISTORY - len(self.pinned))
        del unpinned[max_unpinned:]
        self.history = list(self.pinned) + unpinned

    def _schedule_save(self):
        self._dirty = True
        if not self._save_timer:
            self._save_timer = GLib.timeout_add(300, self._flush_timer)

    def _flush_timer(self) -> bool:
        self._save_timer = 0
        self._save_now()
        return False

    def _load(self):
        try:
            with open(self.storage_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                pinned = [
                    str(x)[:MAX_ENTRY_CHARS]
                    for x in data.get("pinned", [])[:MAX_PINNED]
                    if isinstance(x, str) and x.strip()
                ]
                hist = [
                    str(x)[:MAX_ENTRY_CHARS]
                    for x in data.get("history", [])[:MAX_HISTORY]
                    if isinstance(x, str) and x.strip()
                ]
                self.pinned = list(dict.fromkeys(pinned))
                unpinned = [t for t in dict.fromkeys(hist) if t not in self.pinned]
                self.history = list(self.pinned) + unpinned[:MAX_HISTORY]
                supp = [
                    str(x)[:MAX_ENTRY_CHARS]
                    for x in data.get("suppressed", [])[:MAX_HISTORY]
                    if isinstance(x, str) and x.strip()
                ]
                self._suppressed = set(supp)
        except (OSError, ValueError):
            pass

    def _save_now(self):
        self._dirty = False
        try:
            parent = os.path.dirname(self.storage_path)
            os.makedirs(parent or ".", mode=0o700, exist_ok=True)
            tmp = self.storage_path + ".tmp"
            payload = {
                "pinned": self.pinned[:MAX_PINNED],
                "history": self.history[:MAX_HISTORY],
            }
            if self._suppressed:
                payload["suppressed"] = list(self._suppressed)[:MAX_HISTORY]
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.storage_path)
        except OSError:
            pass
