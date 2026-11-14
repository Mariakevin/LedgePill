"""Desktop integrations: app launcher, workspaces, windows, fullscreen,
keyboard layout, PackageKit & build monitoring, system tray, and power controls.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
from typing import ClassVar

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib, GObject

try:
    gi.require_version("GioUnix", "2.0")
    from gi.repository import GioUnix
except (ValueError, ImportError):
    GioUnix = None

from lostisland.services.desktop_backends import (
    _AGENT_PROCS,
    _BUILD_PROCS,
    _CMDLINE_INTERPRETERS,
    _IGNORE_PREFIXES,
    _IGNORE_PROCS,
    _KWIN_WINDOWS_SCRIPT,
    _PKG_PROCS,
    _WINDOWS_DBUS_PATH,
    _WINDOWS_IFACE_XML,
    _WRAPPER_PROCS,
    _app_id_aliases,
    _build_kwin_windows_js,
    _coding_agent_for_process,
    _friendly_tray_name,
    _is_user_tray_item,
    _KdeScriptBridge,
    _match_open_window,
    _match_tray_item,
    _read_ppid,
    _run_json,
    _spawn,
    _sway_walk,
    _tray_pid_from_service,
    _wmctrl_windows,
)

try:
    _PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
except (OSError, ValueError):
    _PAGE_SIZE = 4096


class DesktopService(GObject.Object):
    __gsignals__: ClassVar[dict] = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "layout-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "fullscreen-changed": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "activity-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self.keyboard_layout = ""
        self.keyboard_layout_options: list[str] = []
        self.keyboard_layout_index = -1
        self.workspaces: list[dict] = []  # [{id, name, active}]
        self.windows: list[dict] = []  # [{id, title, app, active}]
        # [{name, key, pids, rss_mb, window_id, tray_item, background}]
        self.running_apps: list[dict] = []
        self.fullscreen = False
        self.tray_items: list[str] = []
        self.tray_meta: dict[str, dict] = {}
        self.package_activity = ""
        self.build_activity = ""
        self.agent_activity = ""
        self._pending_agent_labels: set[str] = set()
        self._desktop_map: dict[str, str] | None = None
        self._kwin_windows: list[dict] = []
        self._kde_layouts: list[tuple[str, str, str]] = []
        self._kwin_bridge: _KdeScriptBridge | None = None
        self._bus: Gio.DBusConnection | None = None
        self._subs: list[int] = []
        self._timer = 0
        self._busy = False
        self._stopped = False
        self._poll_gen = 0
        self._backend = self._detect_wm_backend()
        self._keyboard_backend = self._detect_keyboard_backend()
        self._init_dbus()

    @staticmethod
    def _detect_wm_backend() -> str:
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):
            return "hyprland"
        if os.environ.get("SWAYSOCK") and shutil.which("swaymsg"):
            return "sway"
        if (
            os.environ.get("KDE_FULL_SESSION")
            or "kde" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        ):
            return "kde"
        if os.environ.get("DISPLAY") and shutil.which("wmctrl"):
            return "x11"
        return "none"

    @staticmethod
    def _detect_keyboard_backend() -> str:
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):
            return "hyprland"
        if os.environ.get("SWAYSOCK") and shutil.which("swaymsg"):
            return "sway"
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        if os.environ.get("KDE_FULL_SESSION") or "kde" in desktop:
            return "kde"
        if "gnome" in desktop:
            return "gnome"
        if os.environ.get("DISPLAY"):
            return "x11"
        return "none"

    @staticmethod
    def _gnome_keyboard_state() -> tuple[str, list[str]]:
        try:
            source = Gio.SettingsSchemaSource.get_default()
            if (
                source is None
                or source.lookup("org.gnome.desktop.input-sources", True) is None
            ):
                return "", []
            settings = Gio.Settings.new("org.gnome.desktop.input-sources")
            raw_sources = settings.get_value("sources").unpack()
            sources = []
            for item in raw_sources:
                if isinstance(item, (tuple, list)) and len(item) >= 2:
                    sources.append((str(item[0]), str(item[1])))
            current = settings.get_uint("current")
            active = sources[current][1] if current < len(sources) else ""
            options = [ident for kind, ident in sources if kind == "xkb"]
            return active, options
        except (GLib.Error, TypeError, ValueError):
            return "", []

    @staticmethod
    def _x11_keyboard_state() -> tuple[str, list[str]]:
        try:
            layouts = subprocess.run(
                ["xkb-switch", "-l"],
                capture_output=True,
                text=True,
                check=True,
                timeout=0.5,
            ).stdout.splitlines()
            active = subprocess.run(
                ["xkb-switch", "-p"],
                capture_output=True,
                text=True,
                check=True,
                timeout=0.5,
            ).stdout.strip()
            return active, [layout.strip() for layout in layouts if layout.strip()]
        except (OSError, subprocess.SubprocessError):
            return "", []

    @property
    def keyboard_switch_available(self) -> bool:
        if self._keyboard_backend == "x11":
            return shutil.which("xkb-switch") is not None
        return self._keyboard_backend in {"kde", "gnome", "sway", "hyprland"}

    @property
    def keyboard_settings_available(self) -> bool:
        candidates = {
            "kde": [["systemsettings", "kcm_keyboard"]],
            "gnome": [["gnome-control-center", "keyboard"]],
            "x11": [
                ["xfce4-keyboard-settings"],
                ["mate-keyboard-properties"],
                ["gnome-control-center", "keyboard"],
                ["systemsettings", "kcm_keyboard"],
            ],
            "sway": [
                ["gnome-control-center", "keyboard"],
                ["systemsettings", "kcm_keyboard"],
                ["xfce4-keyboard-settings"],
            ],
            "hyprland": [
                ["gnome-control-center", "keyboard"],
                ["systemsettings", "kcm_keyboard"],
                ["xfce4-keyboard-settings"],
            ],
        }.get(self._keyboard_backend, [])
        return any(shutil.which(cmd[0]) for cmd in candidates)

    def set_keyboard_layout(self, index: int) -> bool:
        """Switch to a configured keyboard layout when the desktop supports it."""
        try:
            index = int(index)
        except (TypeError, ValueError):
            return False
        if index < 0 or index >= len(self.keyboard_layout_options):
            return False

        if self._keyboard_backend == "kde" and self._bus is not None:
            self._bus.call(
                "org.kde.keyboard",
                "/Layouts",
                "org.kde.KeyboardLayouts",
                "setLayout",
                GLib.Variant("(u)", (index,)),
                None,
                Gio.DBusCallFlags.NONE,
                1000,
                None,
                lambda bus, res: self._on_kde_layout_set(bus, res),
            )
            return True
        if self._keyboard_backend == "gnome":
            try:
                settings = Gio.Settings.new("org.gnome.desktop.input-sources")
                raw_sources = settings.get_value("sources").unpack()
                source_indices = [
                    i
                    for i, item in enumerate(raw_sources)
                    if isinstance(item, (tuple, list))
                    and len(item) >= 2
                    and str(item[0]) == "xkb"
                ]
                return bool(
                    index < len(source_indices)
                    and settings.set_uint("current", source_indices[index])
                )
            except (GLib.Error, TypeError, ValueError):
                return False
        if self._keyboard_backend == "sway":
            return _spawn(
                ["swaymsg", "input", "type:keyboard", "xkb_switch_layout", str(index)]
            )
        if self._keyboard_backend == "hyprland":
            return _spawn(["hyprctl", "switchxkblayout", "all", str(index)])
        if self._keyboard_backend == "x11":
            return _spawn(["xkb-switch", "-s", self.keyboard_layout_options[index]])
        return False

    def cycle_keyboard_layout(self, step: int = 1) -> bool:
        """Cycle the active layout, wrapping around the configured layout list."""
        if len(self.keyboard_layout_options) > 1 and self.keyboard_layout_index >= 0:
            nxt = (self.keyboard_layout_index + (1 if step >= 0 else -1)) % len(
                self.keyboard_layout_options
            )
            return self.set_keyboard_layout(nxt)
        if self._keyboard_backend == "kde" and self._bus is not None:
            count = len(self.keyboard_layout_options)
            if count > 1:
                if self.keyboard_layout_index < 0:
                    current = count - 1 if step >= 0 else 0
                else:
                    current = self.keyboard_layout_index
                return self.set_keyboard_layout(
                    (current + (1 if step >= 0 else -1)) % count
                )
        if self._keyboard_backend == "sway":
            direction = "next" if step >= 0 else "prev"
            return _spawn(
                ["swaymsg", "input", "type:keyboard", "xkb_switch_layout", direction]
            )
        if self._keyboard_backend == "hyprland":
            direction = "next" if step >= 0 else "prev"
            return _spawn(["hyprctl", "switchxkblayout", "all", direction])
        if self._keyboard_backend == "x11":
            return _spawn(["xkb-switch", "-n"])
        return False

    def open_keyboard_settings(self) -> bool:
        candidates = {
            "kde": [["systemsettings", "kcm_keyboard"]],
            "gnome": [["gnome-control-center", "keyboard"]],
            "x11": [
                ["xfce4-keyboard-settings"],
                ["mate-keyboard-properties"],
                ["gnome-control-center", "keyboard"],
                ["systemsettings", "kcm_keyboard"],
            ],
            "sway": [
                ["gnome-control-center", "keyboard"],
                ["systemsettings", "kcm_keyboard"],
                ["xfce4-keyboard-settings"],
            ],
            "hyprland": [
                ["gnome-control-center", "keyboard"],
                ["systemsettings", "kcm_keyboard"],
                ["xfce4-keyboard-settings"],
            ],
        }.get(self._keyboard_backend, [])
        for cmd in candidates:
            if shutil.which(cmd[0]) and _spawn(cmd):
                return True
        return False

    def _on_kde_layout_set(self, bus, res):
        try:
            bus.call_finish(res)
        except GLib.Error:
            logging.getLogger(__name__).debug("KDE layout switch failed", exc_info=True)

    def _init_dbus(self):
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            self._bus = None
            return
        self._subs.append(
            self._bus.signal_subscribe(
                "org.kde.keyboard",
                "org.kde.KeyboardLayouts",
                "layoutChanged",
                "/Layouts",
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_kde_layout_changed,
            )
        )
        for sig in (
            "StatusNotifierItemRegistered",
            "StatusNotifierItemUnregistered",
        ):
            self._subs.append(
                self._bus.signal_subscribe(
                    "org.kde.StatusNotifierWatcher",
                    "org.kde.StatusNotifierWatcher",
                    sig,
                    "/StatusNotifierWatcher",
                    None,
                    Gio.DBusSignalFlags.NONE,
                    lambda *_args: self._fetch_tray(),
                )
            )
        self._fetch_kde_layout()

    def _on_kde_layout_changed(self, bus, sender, path, iface, signal, params):
        self._fetch_kde_layout()

    def _fetch_kde_layout(self):
        if self._bus is None:
            return
        self._bus.call(
            "org.kde.keyboard",
            "/Layouts",
            "org.kde.KeyboardLayouts",
            "getLayoutsList",
            None,
            GLib.VariantType("(a(sss))"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_kde_layouts_list,
        )

    def _on_kde_layouts_list(self, bus, res):
        if self._stopped:
            return
        try:
            layouts = list(bus.call_finish(res).unpack()[0])
        except GLib.Error:
            return
        if not layouts or self._bus is None:
            return
        clean = [
            (str(item[0] or ""), str(item[1] or ""), str(item[2] or ""))
            for item in layouts
            if isinstance(item, (tuple, list)) and len(item) >= 3
        ]
        names = [name or layout for layout, _variant, name in clean]
        if clean != self._kde_layouts:
            self._kde_layouts = clean
            self.keyboard_layout_options = names
            self.emit("changed")
        self._bus.call(
            "org.kde.keyboard",
            "/Layouts",
            "org.kde.KeyboardLayouts",
            "getLayout",
            None,
            GLib.VariantType("(u)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            lambda b, r: self._on_kde_layout_idx(b, r, clean),
        )

    def _on_kde_layout_idx(self, bus, res, layouts: list):
        if self._stopped:
            return
        try:
            idx = int(bus.call_finish(res).unpack()[0])
        except GLib.Error:
            return
        if 0 <= idx < len(layouts):
            short = str(layouts[idx][0] or layouts[idx][2] or "")
            old_index = self.keyboard_layout_index
            self.keyboard_layout_index = idx
            if short and (short != self.keyboard_layout or idx != old_index):
                old = self.keyboard_layout
                self.keyboard_layout = short
                self.emit("changed")
                if old and old != short:
                    self.emit("layout-changed", short)

    def start(self):
        self._stopped = False
        if (
            self._backend == "kde"
            and self._bus is not None
            and self._kwin_bridge is None
        ):
            self._start_kwin_window_bridge()
        if not self._timer:
            self.refresh_now()
            self._timer = GLib.timeout_add_seconds(3, self._tick)

    def stop(self):
        self._stopped = True
        self._busy = False
        self._poll_gen += 1
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if self._kwin_bridge is not None:
            self._kwin_bridge.stop()
            self._kwin_bridge = None
        if self._bus is not None:
            for sub in self._subs:
                try:
                    self._bus.signal_unsubscribe(sub)
                except (GLib.Error, TypeError) as exc:
                    logging.getLogger(__name__).debug(
                        "Desktop signal_unsubscribe failed: %s", exc
                    )
        self._subs.clear()

    def _start_kwin_window_bridge(self):
        if self._bus is None:
            return
        self._kwin_bridge = _KdeScriptBridge(
            dbus_path=_WINDOWS_DBUS_PATH,
            iface_xml=_WINDOWS_IFACE_XML,
            method_name="SyncWindows",
            script_name=_KWIN_WINDOWS_SCRIPT,
            js_builder=_build_kwin_windows_js,
            on_call=self._on_kwin_sync_windows,
        )
        self._kwin_bridge.start(self._bus)

    @staticmethod
    def _parse_kwin_windows(raw: str) -> list[dict]:
        try:
            items = json.loads(raw or "[]")
        except ValueError:
            return []
        if not isinstance(items, list):
            return []
        out: list[dict] = []
        for w in items[:50]:
            if not isinstance(w, dict):
                continue
            try:
                wpid = int(w.get("pid") or 0)
            except (TypeError, ValueError):
                wpid = 0
            out.append(
                {
                    "id": str(w.get("id", "")),
                    "pid": wpid,
                    "app": str(w.get("app", "")),
                    "desktop": str(w.get("desktop", "")),
                    "title": str(w.get("title", "")),
                    "active": bool(w.get("active", False)),
                }
            )
        return out

    @staticmethod
    def _sway_workspaces(tree: dict) -> list[dict]:
        workspaces: list[dict] = []

        def visit(node):
            if not isinstance(node, dict):
                return
            if node.get("type") == "workspace":
                name = str(node.get("name") or "")
                if name and not name.startswith("__i3"):
                    num = node.get("num")
                    workspaces.append(
                        {
                            "id": str(
                                num if isinstance(num, int) and num >= 0 else name
                            ),
                            "name": name,
                            "active": bool(node.get("focused")),
                        }
                    )
            for child in (*node.get("nodes", []), *node.get("floating_nodes", [])):
                visit(child)

        visit(tree)
        return workspaces

    def _on_kwin_sync_windows(self, payload):
        if self._stopped:
            return
        wins = self._parse_kwin_windows(str(payload or ""))
        if wins != self._kwin_windows:
            self._kwin_windows = wins
            self.refresh_now()

    def _tick(self) -> bool:
        self.refresh_now()
        return True

    def refresh_now(self):
        if self._busy or self._stopped:
            return
        self._busy = True
        gen = self._poll_gen
        self._fetch_tray()
        threading.Thread(target=self._poll_worker, args=(gen,), daemon=True).start()

    def _fetch_tray(self):
        if self._bus is None:
            return
        self._bus.call(
            "org.kde.StatusNotifierWatcher",
            "/StatusNotifierWatcher",
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant(
                "(ss)",
                ("org.kde.StatusNotifierWatcher", "RegisteredStatusNotifierItems"),
            ),
            GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_tray_items,
        )

    def _on_tray_items(self, bus, res):
        if self._stopped:
            return
        try:
            items = [str(i) for i in list(bus.call_finish(res).unpack()[0])[:30]]
            for stale in list(self.tray_meta):
                if stale not in items:
                    del self.tray_meta[stale]
            for it in items:
                if it not in self.tray_meta:
                    self.tray_meta[it] = {
                        "pid": _tray_pid_from_service(it),
                        "id": "",
                        "title": "",
                        "category": "",
                        "status": "",
                        "tooltip": "",
                    }
                    self._resolve_tray_meta(it)
            if items != self.tray_items:
                self.tray_items = items
                self.refresh_now()
                self.emit("changed")
        except GLib.Error:
            pass

    def _resolve_tray_meta(self, item: str):
        if self._bus is None:
            return
        service, path = (
            item.split("/", 1) if "/" in item else (item, "StatusNotifierItem")
        )
        if service:
            self._bus.call(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "GetConnectionUnixProcessID",
                GLib.Variant("(s)", (service,)),
                GLib.VariantType("(u)"),
                Gio.DBusCallFlags.NONE,
                1000,
                None,
                lambda b, r, it=item: self._on_tray_pid(b, r, it),
            )
            self._bus.call(
                service,
                "/" + path.lstrip("/"),
                "org.freedesktop.DBus.Properties",
                "GetAll",
                GLib.Variant("(s)", ("org.kde.StatusNotifierItem",)),
                GLib.VariantType("(a{sv})"),
                Gio.DBusCallFlags.NONE,
                1000,
                None,
                lambda b, r, it=item: self._on_tray_props(b, r, it),
            )

    def _on_tray_pid(self, bus, res, item: str):
        if self._stopped or item not in self.tray_meta:
            return
        try:
            pid = int(bus.call_finish(res).unpack()[0])
        except (GLib.Error, TypeError, ValueError, IndexError):
            return
        if pid > 0 and self.tray_meta[item].get("pid") != pid:
            self.tray_meta[item]["pid"] = pid
            self.refresh_now()

    def _on_tray_props(self, bus, res, item: str):
        if self._stopped or item not in self.tray_meta:
            return
        try:
            props = bus.call_finish(res).unpack()[0]
        except (GLib.Error, TypeError, ValueError, IndexError):
            return
        if not isinstance(props, dict):
            return
        meta = self.tray_meta[item]
        tid = str(props.get("Id") or "").strip()
        title = str(props.get("Title") or "").strip()
        category = str(props.get("Category") or "").strip()
        status = str(props.get("Status") or "").strip()
        raw_tip = props.get("ToolTip")
        tooltip = (
            str(raw_tip[2]).strip()
            if isinstance(raw_tip, (tuple, list)) and len(raw_tip) >= 3 and raw_tip[2]
            else ""
        )
        if (
            tid != meta.get("id")
            or title != meta.get("title")
            or category != meta.get("category")
            or status != meta.get("status")
            or tooltip != meta.get("tooltip")
        ):
            meta["id"] = tid
            meta["title"] = title
            meta["category"] = category
            meta["status"] = status
            meta["tooltip"] = tooltip
            self.refresh_now()

    def activate_tray_item(self, item: str):
        if self._bus is None or "/" not in item:
            return
        service, path = item.split("/", 1)
        self._bus.call(
            service,
            "/" + path,
            "org.kde.StatusNotifierItem",
            "Activate",
            GLib.Variant("(ii)", (0, 0)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
        )

    def _poll_worker(self, gen: int | None = None):
        workspaces: list[dict] = []
        windows: list[dict] = []
        fullscreen = False
        layout = self.keyboard_layout
        keyboard_options: list[str] | None = None

        try:
            if self._keyboard_backend == "gnome":
                layout, keyboard_options = self._gnome_keyboard_state()
            elif self._keyboard_backend == "x11":
                layout, keyboard_options = self._x11_keyboard_state()
            if self._backend == "hyprland":
                cli_raw = _run_json(["hyprctl", "clients", "-j"])
                aw_raw = _run_json(["hyprctl", "activewindow", "-j"])
                active_ws = _run_json(["hyprctl", "activeworkspace", "-j"])
                active_ws_id = str(
                    active_ws.get("id", "") if isinstance(active_ws, dict) else ""
                )
                aw_addr = aw_raw.get("address") if isinstance(aw_raw, dict) else ""
                fullscreen = bool(
                    isinstance(aw_raw, dict) and aw_raw.get("fullscreen", 0)
                )
                if isinstance(cli_raw, list):
                    for c in cli_raw[:25]:
                        if isinstance(c, dict) and c.get("mapped", True):
                            windows.append(
                                {
                                    "id": str(c.get("address", "")),
                                    "title": str(c.get("title", "")),
                                    "app": str(c.get("class", "")),
                                    "active": c.get("address") == aw_addr,
                                }
                            )
                ws_raw = _run_json(["hyprctl", "workspaces", "-j"])
                if isinstance(ws_raw, list):
                    workspaces = [
                        {
                            "id": str(ws.get("id", "")),
                            "name": str(ws.get("name") or ws.get("id", "")),
                            "active": str(ws.get("id", "")) == active_ws_id,
                        }
                        for ws in ws_raw
                        if isinstance(ws, dict) and not ws.get("is_special")
                    ]
                    workspaces.sort(
                        key=lambda ws: (
                            not ws["id"].lstrip("-").isdigit(),
                            int(ws["id"]) if ws["id"].lstrip("-").isdigit() else 0,
                            ws["name"].casefold(),
                        )
                    )
                dev_raw = _run_json(["hyprctl", "devices", "-j"])
                if isinstance(dev_raw, dict):
                    for kb in dev_raw.get("keyboards", []):
                        if isinstance(kb, dict) and kb.get("main"):
                            layout = str(kb.get("active_keymap", layout))[:24]
                            break
            elif self._backend == "sway":
                tree = _run_json(["swaymsg", "-t", "get_tree"])
                if isinstance(tree, dict):
                    fullscreen = _sway_walk(tree, windows)
                    workspaces = self._sway_workspaces(tree)
                inp = _run_json(["swaymsg", "-t", "get_inputs"])
                if isinstance(inp, list):
                    for item in inp:
                        if isinstance(item, dict) and item.get("type") == "keyboard":
                            layout = str(item.get("xkb_active_layout_name", layout))[
                                :24
                            ]
                            raw_options = item.get("xkb_layout_names")
                            if isinstance(raw_options, list):
                                keyboard_options = [
                                    str(name) for name in raw_options if str(name)
                                ]
                            break
            elif self._backend == "kde":
                windows = list(self._kwin_windows)
                if os.environ.get("DISPLAY") and shutil.which("wmctrl"):
                    seen_ids = {w.get("id") for w in windows if w.get("id")}
                    for xw in _wmctrl_windows():
                        if xw.get("id") not in seen_ids:
                            windows.append(xw)
            elif self._backend == "x11" or (
                os.environ.get("DISPLAY") and shutil.which("wmctrl")
            ):
                windows = _wmctrl_windows()
        except (OSError, subprocess.SubprocessError, ValueError):
            pass

        pkg_act = ""
        build_act = ""
        agent_act = ""
        running_apps: list[dict] | None = None
        try:
            self._pending_agent_labels.clear()
            pkg_act, build_act, running_apps = self._scan_proc_and_activity(windows)
            agent_act = " + ".join(
                label
                for label in _AGENT_PROCS.values()
                if label in self._pending_agent_labels
            )
        except (OSError, ValueError, TypeError, GLib.Error):
            logging.getLogger(__name__).debug("desktop poll failed", exc_info=True)
        finally:
            GLib.idle_add(
                self._apply_poll,
                workspaces,
                windows,
                fullscreen,
                layout,
                pkg_act,
                build_act,
                running_apps,
                gen,
                agent_act,
                keyboard_options,
            )

    def _get_desktop_map(self) -> dict[str, str]:
        if self._desktop_map is not None:
            return self._desktop_map
        mapping: dict[str, str] = {}
        cls = GioUnix.DesktopAppInfo if GioUnix is not None else Gio.DesktopAppInfo
        for info in cls.get_all():
            try:
                if info.get_nodisplay() or info.get_is_hidden():
                    continue
                if hasattr(info, "should_show") and not info.should_show():
                    continue
                name = info.get_display_name() or info.get_name() or ""
                if not name:
                    continue
                exe = info.get_executable() or ""
                exe_base = os.path.basename(exe).lower() if exe else ""
                if exe_base and exe_base not in _WRAPPER_PROCS:
                    mapping[exe_base] = name
                app_id = (info.get_id() or "").removesuffix(".desktop").lower()
                if app_id:
                    mapping.setdefault(app_id, name)
                    for alias in _app_id_aliases(app_id):
                        mapping.setdefault(alias, name)
                if hasattr(info, "get_startup_wm_class"):
                    wm = (info.get_startup_wm_class() or "").strip().lower()
                    if wm and wm not in _WRAPPER_PROCS:
                        mapping.setdefault(wm, name)
                        for alias in _app_id_aliases(wm):
                            mapping.setdefault(alias, name)
            except (GLib.Error, TypeError, AttributeError) as exc:
                logging.getLogger(__name__).debug("Skipping desktop map entry: %s", exc)
                continue
        self._desktop_map = mapping
        return mapping

    def _resolve_proc_name(
        self, pid: str, comm: str, dmap: dict[str, str]
    ) -> tuple[str, bool]:
        low = comm.lower()
        for cand in (low, low.removesuffix("-bin")):
            if cand in dmap:
                return dmap[cand], True
        # Inspect cmdline for wrappers (flatpak/electron/python/java) or 15-char truncated comm
        if low in _CMDLINE_INTERPRETERS or len(low) >= 15:
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    parts = [
                        p.decode("utf-8", "ignore")
                        for p in f.read().split(b"\x00")
                        if p
                    ]
                if parts:
                    arg0 = os.path.basename(parts[0].rstrip("/")).lower()
                    for cand in (arg0, arg0.removesuffix("-bin")):
                        if cand and cand not in _WRAPPER_PROCS and cand in dmap:
                            return dmap[cand], True
                if low in _CMDLINE_INTERPRETERS:
                    for arg in parts[1:]:
                        if arg.startswith("-") or arg == "run":
                            continue
                        base = os.path.basename(arg.rstrip("/"))
                        if base in ("app.asar", "__main__.py", "resources"):
                            base = os.path.basename(os.path.dirname(arg.rstrip("/")))
                        if base:
                            clean = base.removesuffix(".py").removesuffix(".js").lower()
                            for cand in (clean, *_app_id_aliases(clean)):
                                if cand in dmap:
                                    return dmap[cand], True
                            break
            except OSError:
                pass
        return comm, False

    def _scan_proc_and_activity(
        self, windows: list[dict] | None = None
    ) -> tuple[str, str, list[dict]]:
        uid = os.getuid()
        self_pid = os.getpid()
        dmap = self._get_desktop_map()
        win_list = windows if windows is not None else self.windows
        tray_meta = {k: dict(v) for k, v in self.tray_meta.items()}
        eligible_trays = [
            t for t in self.tray_items if _is_user_tray_item(t, tray_meta.get(t, {}))
        ]
        groups: dict[str, dict] = {}
        foreground_keys: set[str] = set()
        foreground_trays: set[str] = set()
        pkg_msg = (
            "pacman transaction active"
            if os.path.exists("/var/lib/pacman/db.lck")
            else ""
        )
        build_msg = ""
        try:
            pids = os.listdir("/proc")
        except OSError:
            return pkg_msg, build_msg, []

        proc_rows: list[tuple[int, int, str, str, float]] = []
        parent_map: dict[int, int] = {}
        rss_by_pid: dict[int, float] = {}
        for pid in pids:
            if not pid.isdigit():
                continue
            try:
                st_uid = os.stat(f"/proc/{pid}").st_uid
                with open(f"/proc/{pid}/comm") as f:
                    comm = f.read().strip()
                if not comm or st_uid != uid:
                    continue
                low = comm.lower()
                if low in _PKG_PROCS and not pkg_msg:
                    pkg_msg = f"{low} running"
                elif low in _BUILD_PROCS and not build_msg:
                    build_msg = f"{_BUILD_PROCS[low]} build running"
                elif low == "java" and not build_msg:
                    try:
                        with open(f"/proc/{pid}/cmdline", "rb") as f:
                            cmd = f.read().decode("utf-8", "ignore").lower()
                        if "gradle" in cmd:
                            build_msg = "Gradle build running"
                        elif "maven" in cmd or "plexus.classworlds" in cmd:
                            build_msg = "Maven build running"
                    except OSError:
                        pass

                agent = _coding_agent_for_process(pid, low)
                if agent:
                    self._pending_agent_labels.add(agent)

                pid_i = int(pid)
                ppid = _read_ppid(pid)
                parent_map[pid_i] = ppid
                if pid_i == self_pid or st_uid != uid:
                    continue
                if low in _IGNORE_PROCS or low.startswith(_IGNORE_PREFIXES):
                    continue
                with open(f"/proc/{pid}/statm") as f:
                    parts = f.read().split()
                rss_pages = int(parts[1]) if len(parts) > 1 else 0
                rss_mb = (rss_pages * _PAGE_SIZE) / (1024 * 1024)
                rss_by_pid[pid_i] = rss_mb
                proc_rows.append((pid_i, ppid, comm, low, rss_mb))
            except (OSError, ValueError):
                continue

        for pid_i, ppid, comm, low, rss_mb in proc_rows:
            gppid = parent_map.get(ppid, 0) if ppid > 1 else 0
            label, is_desktop_app = self._resolve_proc_name(str(pid_i), comm, dmap)
            key = label.casefold()
            tray_id, tray_label = _match_tray_item(
                eligible_trays, tray_meta, pid_i, ppid, key, low, gppid=gppid
            )
            # Only taskbar tray-resident apps belong in Background apps.
            if not tray_id:
                continue

            t_meta = tray_meta.get(tray_id, {})
            label = _friendly_tray_name(
                tray_id,
                t_meta,
                dmap,
                fallback=label if is_desktop_app else (tray_label or label),
            )
            if not label:
                continue
            key = label.casefold()
            if key in foreground_keys:
                continue

            alt_tok = str(t_meta.get("id") or "").lower() or low
            win_id = _match_open_window(
                win_list, pid_i, ppid, key, alt_tok, label, dmap
            )
            if win_id:
                foreground_keys.add(key)
                foreground_trays.add(tray_id)
                groups.pop(key, None)
                continue

            entry = groups.get(key)
            if entry is None:
                entry = {
                    "name": label,
                    "key": key,
                    "pids": [],
                    "rss_mb": 0.0,
                    "window_id": "",
                    "tray_item": tray_id,
                    "background": True,
                }
                groups[key] = entry
            elif not entry["tray_item"]:
                entry["tray_item"] = tray_id
            entry["pids"].append(pid_i)
            entry["rss_mb"] += rss_mb

        matched_trays = {g["tray_item"] for g in groups.values() if g["tray_item"]}
        for tray_id in eligible_trays:
            if tray_id in foreground_trays or tray_id in matched_trays:
                continue
            t_meta = tray_meta.get(tray_id, {})
            name = _friendly_tray_name(tray_id, t_meta, dmap)
            if not name:
                continue
            key = name.casefold()
            tpid = int(t_meta.get("pid") or 0)
            alt_tok = str(t_meta.get("id") or key).lower()
            if key in foreground_keys or _match_open_window(
                win_list, tpid, 0, key, alt_tok, name, dmap
            ):
                foreground_keys.add(key)
                foreground_trays.add(tray_id)
                continue
            groups[key] = {
                "name": name,
                "key": key,
                "pids": [tpid] if tpid > 1 else [],
                "rss_mb": rss_by_pid.get(tpid, 0.0),
                "window_id": "",
                "tray_item": tray_id,
                "background": True,
            }

        apps = list(groups.values())
        apps.sort(
            key=lambda a: (
                not bool(a["tray_item"]),
                -a["rss_mb"],
                a["name"].casefold(),
            )
        )
        return pkg_msg, build_msg, apps[:40]

    def _scan_running_apps(self, windows: list[dict] | None = None) -> list[dict]:
        return self._scan_proc_and_activity(windows)[2]

    def _apply_poll(
        self,
        workspaces: list[dict],
        windows: list[dict],
        fullscreen: bool,
        layout: str,
        pkg_act: str,
        build_act: str,
        running_apps: list[dict] | None = None,
        gen: int | None = None,
        agent_act: str = "",
        keyboard_options: list[str] | None = None,
    ):
        if self._stopped or (gen is not None and gen != self._poll_gen):
            return False
        self._busy = False
        old_ws = self.workspaces
        old_win = self.windows
        old_fs = self.fullscreen
        old_layout = self.keyboard_layout
        old_keyboard_options = tuple(self.keyboard_layout_options)
        old_act = (self.package_activity, self.build_activity, self.agent_activity)
        old_run_sig = [
            (a["key"], len(a["pids"]), a["window_id"], round(a["rss_mb"] / 16))
            for a in self.running_apps
        ]
        self.workspaces = workspaces
        self.windows = windows
        if running_apps is not None:
            self.running_apps = running_apps
        new_run_sig = [
            (a["key"], len(a["pids"]), a["window_id"], round(a["rss_mb"] / 16))
            for a in self.running_apps
        ]
        self.fullscreen = fullscreen
        if layout:
            self.keyboard_layout = layout
        if keyboard_options is not None:
            self.keyboard_layout_options = keyboard_options
            self.keyboard_layout_index = next(
                (
                    i
                    for i, name in enumerate(self.keyboard_layout_options)
                    if name.casefold() == self.keyboard_layout.casefold()
                ),
                -1,
            )
        self.package_activity = pkg_act
        self.build_activity = build_act
        self.agent_activity = agent_act
        if fullscreen != old_fs:
            self.emit("fullscreen-changed", fullscreen)
        if layout and old_layout and layout != old_layout:
            self.emit("layout-changed", layout)
        if (pkg_act, build_act, agent_act) != old_act:
            self.emit("activity-changed")
        if (
            workspaces != old_ws
            or windows != old_win
            or fullscreen != old_fs
            or (layout and layout != old_layout)
            or tuple(self.keyboard_layout_options) != old_keyboard_options
            or new_run_sig != old_run_sig
        ):
            self.emit("changed")
        return False

    def list_running_apps(self, query: str = "", limit: int = 25) -> list[dict]:
        if not self.running_apps:
            self.running_apps = self._scan_running_apps()
        q = (query or "").strip().casefold()
        if not q:
            return list(self.running_apps[:limit])
        return [
            a for a in self.running_apps if q in a["name"].casefold() or q in a["key"]
        ][:limit]

    def terminate_app(self, pids: list[int]) -> bool:
        uid = os.getuid()
        self_pid = os.getpid()
        sent = False
        for pid in pids:
            if not isinstance(pid, int) or pid <= 1 or pid == self_pid:
                continue
            try:
                if os.stat(f"/proc/{pid}").st_uid == uid:
                    os.kill(pid, signal.SIGTERM)
                    sent = True
            except OSError:
                continue
        if sent:
            GLib.timeout_add(
                250, lambda: (not self._stopped and self.refresh_now(), False)[1]
            )
        return sent

    # -- window / app / power actions --------------------------------------

    def focus_window(self, win_id: str):
        if self._backend == "hyprland":
            _spawn(["hyprctl", "dispatch", "focuswindow", f"address:{win_id}"])
        elif self._backend == "sway":
            _spawn(["swaymsg", f'[con_id="{win_id}"]', "focus"])
        elif self._backend == "x11" or (
            self._backend == "kde"
            and os.environ.get("DISPLAY")
            and shutil.which("wmctrl")
        ):
            _spawn(["wmctrl", "-i", "-a", win_id])

    def switch_workspace(self, workspace_id: str) -> bool:
        workspace = str(workspace_id or "").strip()
        if not workspace:
            return False
        if self._backend == "hyprland":
            return _spawn(["hyprctl", "dispatch", "workspace", workspace])
        if self._backend == "sway":
            return _spawn(["swaymsg", "workspace", workspace])
        return False

    @staticmethod
    def list_apps(query: str = "", limit: int = 30) -> list[dict]:
        cls = GioUnix.DesktopAppInfo if GioUnix is not None else Gio.DesktopAppInfo
        q = (query or "").strip().casefold()
        out = []
        for info in cls.get_all():
            try:
                if info.get_nodisplay() or info.get_is_hidden():
                    continue
                name = info.get_display_name() or info.get_name() or ""
                desc = info.get_description() or ""
                app_id = info.get_id() or ""
                if q and q not in name.casefold() and q not in desc.casefold():
                    continue
                icon = info.get_icon()
                icon_name = (
                    icon.to_string() if icon is not None else "application-x-executable"
                )
                out.append(
                    {"id": app_id, "name": name, "desc": desc, "icon": icon_name}
                )
            except (GLib.Error, TypeError, AttributeError) as exc:
                logging.getLogger(__name__).debug("Skipping desktop app entry: %s", exc)
                continue
        out.sort(key=lambda a: a["name"].casefold())
        return out[:limit]

    @staticmethod
    def launch_app(app_id: str) -> bool:
        cls = GioUnix.DesktopAppInfo if GioUnix is not None else Gio.DesktopAppInfo
        try:
            info = cls.new(app_id)
            if info is not None:
                info.launch([], None)
                return True
        except GLib.Error:
            pass
        return False

    @staticmethod
    def power_action(action: str) -> bool:
        if action == "lock":
            try:
                bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                bus.call(
                    "org.freedesktop.ScreenSaver",
                    "/ScreenSaver",
                    "org.freedesktop.ScreenSaver",
                    "Lock",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    1500,
                    None,
                    lambda b, r: DesktopService._on_lock_dbus_done(b, r),
                )
                return True
            except GLib.Error:
                return _spawn(["loginctl", "lock-session"])

        login_methods = {
            "suspend": "Suspend",
            "reboot": "Reboot",
            "poweroff": "PowerOff",
        }
        cmds = {
            "suspend": ["systemctl", "suspend"],
            "reboot": ["systemctl", "reboot"],
            "poweroff": ["systemctl", "poweroff"],
        }
        method = login_methods.get(action)
        cmd = cmds.get(action)
        if not method or not cmd:
            return False
        try:
            sys_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            sys_bus.call(
                "org.freedesktop.login1",
                "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager",
                method,
                GLib.Variant("(b)", (True,)),
                None,
                Gio.DBusCallFlags.NONE,
                2000,
                None,
                lambda b, r, fallback=cmd: DesktopService._on_power_dbus_done(
                    b, r, fallback
                ),
            )
            return True
        except GLib.Error:
            return _spawn(cmd)

    @staticmethod
    def _on_lock_dbus_done(bus, res):
        try:
            bus.call_finish(res)
        except GLib.Error:
            _spawn(["loginctl", "lock-session"])

    @staticmethod
    def _on_power_dbus_done(bus, res, fallback: list[str]):
        try:
            bus.call_finish(res)
        except GLib.Error:
            _spawn(fallback)


_CURSOR_IFACE_XML = """
<node>
  <interface name="it.zorn.LostIsland.Cursor">
    <method name="Move">
      <arg type="s" name="pos" direction="in"/>
    </method>
  </interface>
</node>
"""
_CURSOR_DBUS_PATH = "/it/zorn/LostIsland/Cursor"
_CURSOR_DBUS_IFACE = "it.zorn.LostIsland.Cursor"
_KWIN_CURSOR_SCRIPT = "lostisland-cursor"


class CursorWatcher(GObject.Object):
    """Global cursor coordinate tracker for expressive MascotEyes outside the pill."""

    __gsignals__: ClassVar[dict] = {
        "moved": (GObject.SignalFlags.RUN_FIRST, None, (float, float)),
        "active-changed": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
    }

    def __init__(self, backend: str | None = None):
        super().__init__()
        self._backend = backend or self.detect_backend()
        self._active = False
        self.global_active = False
        self._gen = 0
        self._last_pos: tuple[float, float] | None = None
        self._timer = 0
        self._poll_busy = False
        self._bus: Gio.DBusConnection | None = None
        self._kde_bridge: _KdeScriptBridge | None = None
        self._x11 = None
        self._x11_dpy = None
        self._x11_root = 0
        self._xdotool = shutil.which("xdotool") or ""

    @staticmethod
    def detect_backend() -> str:
        wm = DesktopService._detect_wm_backend()
        if wm != "none":
            return wm
        if os.environ.get("DISPLAY"):
            return "x11"
        return "none"

    @staticmethod
    def parse_pair(raw: str) -> tuple[float, float] | None:
        parts = [p.strip() for p in (raw or "").split(",")]
        if len(parts) != 2:
            return None
        try:
            return float(parts[0]), float(parts[1])
        except ValueError:
            return None

    @staticmethod
    def parse_hypr_cursor(raw: str) -> tuple[float, float] | None:
        text = (raw or "").strip()
        if not text:
            return None
        if text.startswith("{"):
            try:
                data = json.loads(text)
                if isinstance(data, dict) and "x" in data and "y" in data:
                    return float(data["x"]), float(data["y"])
            except (ValueError, TypeError):
                return None
        return CursorWatcher.parse_pair(text)

    @staticmethod
    def parse_sway_cursor(raw: object) -> tuple[float, float] | None:
        data = raw
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except ValueError:
                return None
        if not isinstance(data, list):
            return None
        for seat in data:
            if not isinstance(seat, dict):
                continue
            cur = seat.get("cursor")
            if isinstance(cur, dict) and "x" in cur and "y" in cur:
                try:
                    return float(cur["x"]), float(cur["y"])
                except (ValueError, TypeError):
                    continue
        return None

    @staticmethod
    def parse_xdotool_shell(raw: str) -> tuple[float, float] | None:
        vals: dict[str, float] = {}
        for line in (raw or "").splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().upper()
            if k in ("X", "Y"):
                try:
                    vals[k] = float(v.strip())
                except ValueError:
                    return None
        if "X" in vals and "Y" in vals:
            return vals["X"], vals["Y"]
        return None

    @staticmethod
    def normalize_cursor(
        gx: float,
        gy: float,
        mon_x: float,
        mon_y: float,
        mon_w: float,
        mon_h: float,
        margin_top: int = 0,
    ) -> tuple[float, float]:
        cx = float(mon_x) + float(mon_w) / 2.0
        cy = float(mon_y) + float(margin_top) + 18.0
        dx = max(240.0, float(mon_w) * 0.42)
        nx = max(-1.0, min(1.0, (float(gx) - cx) / dx))
        if gy < cy:
            dy_up = max(18.0, float(margin_top) + 24.0)
            ny = max(-1.0, (float(gy) - cy) / dy_up)
        else:
            dy_down = max(240.0, (float(mon_h) - float(margin_top)) * 0.65)
            ny = min(1.0, (float(gy) - cy) / dy_down)
        return nx, ny

    def start(self):
        if self._active or self._backend == "none":
            return
        self._active = True
        self._gen += 1
        gen = self._gen
        self._last_pos = None
        if self._backend == "kde":
            self._start_kde(gen)
        elif self._backend in ("hyprland", "sway"):
            self._set_global_active(True)
            self._timer = GLib.timeout_add(45, self._poll_tick)
        elif self._backend == "x11":
            if self._init_x11():
                self._set_global_active(True)
                self._timer = GLib.timeout_add(45, self._x11_tick)
            elif self._xdotool:
                self._set_global_active(True)
                self._timer = GLib.timeout_add(65, self._poll_tick)
            else:
                self._active = False

    def stop(self):
        self._active = False
        self._gen += 1
        self._poll_busy = False
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self._cleanup_kde()
        self._cleanup_x11()
        self._set_global_active(False)

    def _set_global_active(self, active: bool):
        if active != self.global_active:
            self.global_active = active
            self.emit("active-changed", active)

    def _emit_if_changed(self, x: float, y: float):
        if not self._active:
            return
        if (
            self._last_pos is not None
            and abs(x - self._last_pos[0]) < 1.5
            and abs(y - self._last_pos[1]) < 1.5
        ):
            return
        self._last_pos = (x, y)
        self.emit("moved", x, y)

    # -- KDE Plasma KWin scripting D-Bus cursor push ------------------------

    @staticmethod
    def _build_cursor_js(unique: str) -> str:
        return (
            "(function () {\n"
            f"    var SVC = {json.dumps(unique)};\n"
            f"    var PATH = {json.dumps(_CURSOR_DBUS_PATH)};\n"
            f"    var IFACE = {json.dumps(_CURSOR_DBUS_IFACE)};\n"
            "    var lastTs = 0;\n"
            "    var lastX = -999999;\n"
            "    var lastY = -999999;\n"
            "    function sendPos() {\n"
            '        if (typeof workspace === "undefined") { return; }\n'
            "        var p = workspace.cursorPos;\n"
            "        if (!p) { return; }\n"
            "        var x = Math.round(p.x);\n"
            "        var y = Math.round(p.y);\n"
            "        var now = Date.now();\n"
            "        if (now - lastTs < 25) { return; }\n"
            "        if (Math.abs(x - lastX) < 3 && Math.abs(y - lastY) < 3) { return; }\n"
            "        lastTs = now;\n"
            "        lastX = x;\n"
            "        lastY = y;\n"
            '        callDBus(SVC, PATH, IFACE, "Move", String(x) + "," + String(y));\n'
            "    }\n"
            "    if (\n"
            '        typeof workspace !== "undefined" &&\n'
            "        workspace.cursorPosChanged &&\n"
            '        typeof workspace.cursorPosChanged.connect === "function"\n'
            "    ) {\n"
            "        workspace.cursorPosChanged.connect(sendPos);\n"
            "    }\n"
            "    sendPos();\n"
            "})();\n"
        )

    def _start_kde(self, gen: int):
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            self._bus = None
            return
        self._kde_bridge = _KdeScriptBridge(
            dbus_path=_CURSOR_DBUS_PATH,
            iface_xml=_CURSOR_IFACE_XML,
            method_name="Move",
            script_name=_KWIN_CURSOR_SCRIPT,
            js_builder=self._build_cursor_js,
            on_call=lambda raw: (
                (parsed := self.parse_pair(str(raw or "")))
                and self._emit_if_changed(*parsed)
            ),
            on_ready=lambda: (
                self._active and gen == self._gen and self._set_global_active(True)
            ),
        )
        if not self._kde_bridge.start(self._bus):
            self._kde_bridge = None

    def _cleanup_kde(self):
        if self._kde_bridge is not None:
            self._kde_bridge.stop()
            self._kde_bridge = None

    # -- Hyprland / Sway / xdotool poll worker ------------------------------

    def _poll_tick(self) -> bool:
        if not self._active:
            return False
        if not self._poll_busy:
            self._poll_busy = True
            threading.Thread(
                target=self._poll_cursor_worker, args=(self._gen,), daemon=True
            ).start()
        return True

    def _poll_cursor_worker(self, gen: int):
        pos: tuple[float, float] | None = None
        try:
            if self._backend == "hyprland":
                out = subprocess.run(
                    ["hyprctl", "cursorpos", "-j"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=0.5,
                )
                if out.returncode == 0:
                    pos = self.parse_hypr_cursor(out.stdout)
            elif self._backend == "sway":
                data = _run_json(["swaymsg", "-t", "get_seats"])
                pos = self.parse_sway_cursor(data)
            elif self._backend == "x11" and self._xdotool:
                out = subprocess.run(
                    [self._xdotool, "getmouselocation", "--shell"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=0.5,
                )
                if out.returncode == 0:
                    pos = self.parse_xdotool_shell(out.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            pos = None
        finally:
            GLib.idle_add(self._apply_poll_pos, pos, gen)

    def _apply_poll_pos(self, pos: tuple[float, float] | None, gen: int) -> bool:
        self._poll_busy = False
        if self._active and gen == self._gen and pos is not None:
            self._emit_if_changed(*pos)
        return False

    # -- X11 libX11 fast query ---------------------------------------------

    def _init_x11(self) -> bool:
        try:
            x11 = ctypes.CDLL("libX11.so.6")
            x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            x11.XDefaultRootWindow.restype = ctypes.c_ulong
            x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            x11.XCloseDisplay.restype = ctypes.c_int
            x11.XQueryPointer.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_uint),
            ]
            x11.XQueryPointer.restype = ctypes.c_int
            dpy = x11.XOpenDisplay(None)
            if not dpy:
                return False
            self._x11 = x11
            self._x11_dpy = dpy
            self._x11_root = x11.XDefaultRootWindow(dpy)
            return True
        except OSError:
            self._x11 = None
            self._x11_dpy = None
            return False

    def _x11_tick(self) -> bool:
        if not self._active or self._x11 is None or not self._x11_dpy:
            return False
        root_r = ctypes.c_ulong()
        child_r = ctypes.c_ulong()
        rx = ctypes.c_int()
        ry = ctypes.c_int()
        wx = ctypes.c_int()
        wy = ctypes.c_int()
        mask = ctypes.c_uint()
        ok = self._x11.XQueryPointer(
            self._x11_dpy,
            self._x11_root,
            ctypes.byref(root_r),
            ctypes.byref(child_r),
            ctypes.byref(rx),
            ctypes.byref(ry),
            ctypes.byref(wx),
            ctypes.byref(wy),
            ctypes.byref(mask),
        )
        if ok:
            self._emit_if_changed(float(rx.value), float(ry.value))
        return True

    def _cleanup_x11(self):
        if self._x11 is not None and self._x11_dpy:
            try:
                self._x11.XCloseDisplay(self._x11_dpy)
            except (OSError, ctypes.ArgumentError):
                pass
        self._x11_dpy = None
        self._x11 = None
