"""Passive notification mirror + custom/webhook notification store.

Opens a private monitor connection to the session bus and watches
org.freedesktop.Notifications Notify calls without ever answering them —
KDE's (or any other) notification daemon keeps working untouched; the island
just peeks what flies by. Supports DND, blocklists, regex/keyword rules,
categories, call recognition, and persisted history.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import ClassVar

from gi.repository import Gio, GLib, GObject


class NotifyService(GObject.Object):
    __gsignals__: ClassVar[dict] = {
        # app_name, summary, body, app_icon (for peeks)
        "notified": (GObject.SignalFlags.RUN_FIRST, None, (str, str, str, str)),
        # emitted whenever history changes (including while DND is enabled)
        "history-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # incoming call detected: app_name, summary, body, icon
        "call": (GObject.SignalFlags.RUN_FIRST, None, (str, str, str, str)),
    }

    def __init__(
        self,
        blocked_apps: list[str] | None = None,
        do_not_disturb: bool = False,
        history_path: str | None = None,
        rules: list[str] | None = None,
    ):
        super().__init__()
        data_home = os.environ.get(
            "XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share")
        )
        self.history_path = history_path or os.path.join(
            data_home, "lost-island", "notifications.json"
        )
        self.history = self._load_history()
        self.blocked_apps = {
            str(name).strip().casefold()
            for name in (blocked_apps or [])
            if str(name).strip()
        }
        self._rules = self._compile_rules(rules or [])
        self.do_not_disturb = bool(do_not_disturb)
        self._conn: Gio.DBusConnection | None = None
        try:
            addr = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
            self._conn = Gio.DBusConnection.new_for_address_sync(
                addr,
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None,
                None,
            )
            self._conn.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus.Monitoring",
                "BecomeMonitor",
                GLib.Variant(
                    "(asu)",
                    (
                        [
                            (
                                "type='method_call',"
                                "interface='org.freedesktop.Notifications',"
                                "member='Notify'"
                            )
                        ],
                        0,
                    ),
                ),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except GLib.Error:
            self._conn = None
            return
        self._conn.add_filter(self._on_message)

    @staticmethod
    def _compile_rules(rules: list[str]) -> list[re.Pattern]:
        compiled = []
        for raw in rules:
            if not isinstance(raw, str):
                continue
            pat = raw.strip()
            if not pat:
                continue
            try:
                if len(pat) >= 2 and pat.startswith("/") and pat.endswith("/"):
                    compiled.append(re.compile(pat[1:-1], re.IGNORECASE))
                elif pat.startswith("regex:") and len(pat) > 6:
                    compiled.append(re.compile(pat[6:], re.IGNORECASE))
                else:
                    compiled.append(re.compile(re.escape(pat), re.IGNORECASE))
            except re.error:
                continue
        return compiled

    def set_dnd(self, enabled: bool) -> None:
        self.do_not_disturb = bool(enabled)

    def stop(self):
        if self._conn is not None and not self._conn.is_closed():
            self._conn.close(None, None)
        self._conn = None

    def clear(self):
        if self.history:
            self.history.clear()
            self._save_history()
            self.emit("history-changed")

    def dismiss(self, index: int) -> None:
        if 0 <= index < len(self.history):
            del self.history[index]
            self._save_history()
            self.emit("history-changed")

    def push_custom(
        self,
        source: str,
        title: str,
        body: str,
        category: str = "custom",
        icon: str = "",
    ) -> None:
        self._emit(source, title, body, icon, category=category)

    def _matches_filter(self, app_s: str, sum_s: str, body_s: str) -> bool:
        if app_s.casefold() in self.blocked_apps:
            return True
        if not self._rules:
            return False
        haystack = f"{app_s}\n{sum_s}\n{body_s}"
        return any(r.search(haystack) for r in self._rules)

    def _on_message(self, conn, message, incoming):
        if not incoming:
            return message
        try:
            if (
                message.get_interface() == "org.freedesktop.Notifications"
                and message.get_member() == "Notify"
            ):
                body = message.get_body()
                if body is not None:
                    unpacked = body.unpack()
                    if len(unpacked) >= 5:
                        app, _rid, icon, summary, text = unpacked[:5]
                        actions = unpacked[5] if len(unpacked) > 5 else []
                        hints = unpacked[6] if len(unpacked) > 6 else {}
                        category = ""
                        image_path = ""
                        desktop_entry = ""
                        if isinstance(hints, dict):
                            category = str(hints.get("category", "") or "")
                            image_path = str(hints.get("image-path", "") or "")
                            desktop_entry = str(hints.get("desktop-entry", "") or "")
                        eff_icon = icon or image_path or desktop_entry
                        act_list = (
                            [str(a) for a in actions]
                            if isinstance(actions, (list, tuple))
                            else []
                        )
                        GLib.idle_add(
                            self._emit,
                            app,
                            summary,
                            text,
                            eff_icon,
                            category,
                            act_list,
                        )
        except (GLib.Error, ValueError, TypeError):
            pass
        return message

    def _emit(
        self,
        app,
        summary,
        text,
        icon,
        category: str = "",
        actions: list[str] | None = None,
    ):
        app_s = str(app or "")[:256]
        sum_s = str(summary or "")[:1000]
        body_s = str(text or "")[:5000]
        icon_s = str(icon or "")[:2048]
        cat_s = str(category or "").strip()[:80]
        if self._matches_filter(app_s, sum_s, body_s):
            return False
        is_call = "call" in cat_s.casefold() or (
            "incoming call" in f"{sum_s} {body_s}".casefold()
        )
        if not cat_s:
            cat_s = "call" if is_call else "general"
        entry = {
            "app": app_s,
            "summary": sum_s,
            "body": body_s,
            "icon": icon_s,
            "category": cat_s,
            "actions": actions or [],
            "is_call": is_call,
            "time": time.time(),
        }
        self.history.insert(0, entry)
        del self.history[50:]
        self._save_history()
        self.emit("history-changed")
        if is_call:
            self.emit("call", app_s, sum_s, body_s, icon_s)
        elif not self.do_not_disturb:
            self.emit("notified", app_s, sum_s, body_s, icon_s)
        return False

    def _load_history(self) -> list[dict]:
        try:
            with open(self.history_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [row for row in data[:50] if isinstance(row, dict)]
        except (OSError, ValueError):
            pass
        return []

    def _save_history(self):
        try:
            parent = os.path.dirname(self.history_path)
            os.makedirs(parent or ".", mode=0o700, exist_ok=True)
            tmp = self.history_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.history[:50], f, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.history_path)
        except OSError:
            pass
