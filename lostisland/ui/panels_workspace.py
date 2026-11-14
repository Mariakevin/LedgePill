"""Workspace panels: Desktop apps, AI assistant, and Camera Mirror."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import threading

from gi.repository import Gdk, GLib, Gtk, Pango

from lostisland import config
from lostisland.ui.draw import pick_icon
from lostisland.ui.panel_shared import (
    _badge,
    _icon_btn,
    _scroll_box,
    _section_card,
    _section_title,
    build_dock_tile,
)


class DesktopPanel(Gtk.Box):
    """Taskbar panel for app launch, keyboard layouts, and background apps."""

    def __init__(
        self,
        cfg: dict,
        desktop,
        bluetooth=None,
        kdeconnect=None,
        embedded: bool = False,
        on_toggle=None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        if not embedded:
            self.add_css_class("section-card")
        self.cfg = cfg
        self.desktop = desktop
        self._embedded = embedded
        self._on_toggle = on_toggle
        self._apps_open = False
        self._armed_end = ""
        self._disarm_timer = 0
        self._last_apps_snap = None
        self._last_windows_snap = None
        self._keyboard_syncing = False
        self._keyboard_model: tuple[str, ...] = ()

        self.connect("destroy", lambda *_: self._cleanup())

        self.apps_badge = _badge("0")
        self.kb_lbl = _badge("")
        self.kb_lbl.set_visible(False)
        self.apps_chev = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        self.apps_summary = Gtk.Label(label="No apps", xalign=0, hexpand=True)
        self.apps_summary.add_css_class("tile-sub")

        head = Gtk.Button()
        if embedded:
            self._head_btn = build_dock_tile(
                pick_icon(
                    self,
                    "view-grid-symbolic",
                    "application-x-executable-symbolic",
                    "system-run-symbolic",
                ),
                "Taskbar",
                self.apps_badge,
                self.apps_summary,
                chevron=self.apps_chev,
                extra_badge=self.kb_lbl,
                tooltip="Apps, keyboard layouts, tray items, and background tasks",
            )
            self._head_btn.connect("clicked", lambda *_: self.toggle_open())
            self.head_tile = self._head_btn
        else:
            head.add_css_class("clip-toggle-btn")
            head.connect("clicked", lambda *_: self.toggle_open())
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            title = _section_title("Taskbar")
            title.set_hexpand(True)
            hbox.append(title)
            hbox.append(self.apps_badge)
            hbox.append(self.kb_lbl)
            hbox.append(self.apps_chev)
            head.set_child(hbox)
            self._head_btn = head
            self.head_tile = head
            self.append(head)

        self.apps_revealer = Gtk.Revealer()
        self.apps_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.apps_revealer.set_reveal_child(False)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        if not embedded:
            body.set_margin_top(6)
        if self.desktop is not None:
            self.workspaces_title = _section_title("Workspaces")
            self.workspaces_box = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=5
            )
            self.workspaces_title.set_visible(False)
            self.workspaces_box.set_visible(False)
            body.append(self.workspaces_title)
            body.append(self.workspaces_box)
            self._build_keyboard_options(body)
        if self.desktop is not None and self.cfg.get("modules", {}).get(
            "launcher", True
        ):
            self.app_search = Gtk.SearchEntry(
                placeholder_text="Search and launch apps…"
            )
            self.app_search.add_css_class("island-search")
            self.app_search.connect("search-changed", self._on_app_search_changed)
            body.append(self.app_search)
            self.favorites_title = _section_title("Pinned apps")
            body.append(self.favorites_title)
            self.favorites_box = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=4
            )
            body.append(self.favorites_box)
            self.launcher_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            self.launcher_box.set_visible(False)
            body.append(self.launcher_box)
        if self.desktop is not None:
            self.windows_title = _section_title("Open windows")
            self.windows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            self.windows_title.set_visible(False)
            body.append(self.windows_title)
            body.append(self.windows_box)
            apps_title = _section_title("Tray and background apps")
            body.append(apps_title)
            self.apps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            body.append(self.apps_box)
        self.apps_revealer.set_child(body)
        if not embedded:
            self.append(self.apps_revealer)

        if self.desktop is not None:
            self.desktop.connect("changed", lambda *_: self.refresh_dynamic())
        if hasattr(self, "favorites_box"):
            self.refresh_favorites()
            self.refresh_launcher()
        self.refresh_dynamic()

    def _build_keyboard_options(self, body: Gtk.Box):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.add_css_class("taskbar-keyboard-row")
        icon = Gtk.Image.new_from_icon_name("input-keyboard-symbolic")
        icon.set_pixel_size(14)
        row.append(icon)
        title = Gtk.Label(label="Keyboard", xalign=0)
        title.add_css_class("tile-sub")
        row.append(title)

        self.kb_picker = Gtk.DropDown()
        self.kb_picker.set_hexpand(True)
        self.kb_picker.set_visible(False)
        self.kb_picker.connect("notify::selected", self._on_keyboard_selected)
        row.append(self.kb_picker)

        self.kb_layout_text = Gtk.Label(label="—", xalign=0, hexpand=True)
        self.kb_layout_text.add_css_class("list-title")
        row.append(self.kb_layout_text)

        self.kb_prev_btn = _icon_btn("go-previous-symbolic", "Previous keyboard layout")
        self.kb_prev_btn.connect("clicked", lambda *_: self._cycle_keyboard(-1))
        row.append(self.kb_prev_btn)
        self.kb_next_btn = _icon_btn("go-next-symbolic", "Next keyboard layout")
        self.kb_next_btn.connect("clicked", lambda *_: self._cycle_keyboard(1))
        row.append(self.kb_next_btn)
        self.kb_settings_btn = _icon_btn(
            "preferences-system-symbolic", "Open keyboard settings"
        )
        self.kb_settings_btn.connect(
            "clicked", lambda *_: self.desktop.open_keyboard_settings()
        )
        row.append(self.kb_settings_btn)
        row.set_visible(
            bool(self.desktop.keyboard_layout)
            or bool(getattr(self.desktop, "keyboard_switch_available", False))
        )
        self.keyboard_row = row
        body.append(row)
        self._sync_keyboard_options()

    def _sync_keyboard_options(self):
        if not hasattr(self, "keyboard_row"):
            return
        options = tuple(getattr(self.desktop, "keyboard_layout_options", []) or [])
        index = int(getattr(self.desktop, "keyboard_layout_index", -1))
        current = str(getattr(self.desktop, "keyboard_layout", "") or "")
        can_switch = bool(getattr(self.desktop, "keyboard_switch_available", False))
        can_open_settings = bool(
            getattr(self.desktop, "keyboard_settings_available", False)
        )
        backend = getattr(self.desktop, "_keyboard_backend", "")
        self._keyboard_syncing = True
        if options != self._keyboard_model:
            self.kb_picker.set_model(Gtk.StringList.new(list(options)))
            self._keyboard_model = options
        has_choices = len(options) > 1
        can_cycle = can_switch and (
            has_choices or (not options and backend in {"sway", "hyprland", "x11"})
        )
        self.kb_picker.set_visible(has_choices)
        self.kb_layout_text.set_visible(not has_choices)
        self.kb_layout_text.set_label(current or "Unavailable")
        self.kb_prev_btn.set_visible(not has_choices and can_cycle)
        self.kb_next_btn.set_visible(not has_choices and can_cycle)
        self.kb_settings_btn.set_sensitive(can_open_settings)
        if has_choices:
            self.kb_picker.set_selected(index if 0 <= index < len(options) else 0)
        self._keyboard_syncing = False
        self.keyboard_row.set_visible(bool(current) or can_switch or can_open_settings)

    def _on_keyboard_selected(self, picker, _pspec):
        if self._keyboard_syncing:
            return
        if self.desktop is not None:
            self.desktop.set_keyboard_layout(picker.get_selected())

    def _cycle_keyboard(self, step: int):
        if self.desktop is not None:
            self.desktop.cycle_keyboard_layout(step)

    def _on_app_search_changed(self, *_):
        self.refresh_launcher()
        self.refresh_apps()

    def _launch_app(self, app_id: str):
        if self.desktop is not None and self.desktop.launch_app(app_id):
            if hasattr(self, "app_search"):
                self.app_search.set_text("")
            self.set_open(False)

    def _toggle_favorite(self, app_id: str):
        favorites = list(self.cfg.get("favorite_apps", []) or [])
        if app_id in favorites:
            favorites.remove(app_id)
        else:
            favorites.append(app_id)
        self.cfg["favorite_apps"] = favorites[:24]
        config.save(self.cfg)
        self.refresh_favorites()
        self.refresh_launcher()

    def _app_launch_row(self, app: dict, pinned: bool = False) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        launch = Gtk.Button()
        launch.add_css_class("panel-row-btn")
        launch.set_hexpand(True)
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = Gtk.Image.new_from_icon_name("application-x-executable-symbolic")
        icon.set_pixel_size(14)
        content.append(icon)
        label = Gtk.Label(label=app.get("name", "Application"), xalign=0, hexpand=True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        content.append(label)
        launch.set_child(content)
        launch.set_tooltip_text(app.get("desc") or f"Launch {app.get('name', 'app')}")
        launch.connect("clicked", lambda *_: self._launch_app(app.get("id", "")))
        pin = _icon_btn(
            "starred-symbolic" if pinned else "bookmark-new-symbolic",
            "Unpin app" if pinned else "Pin app to taskbar",
        )
        pin.connect("clicked", lambda *_: self._toggle_favorite(app.get("id", "")))
        row.append(launch)
        row.append(pin)
        return row

    def refresh_favorites(self):
        if not hasattr(self, "favorites_box") or self.desktop is None:
            return
        while (child := self.favorites_box.get_first_child()) is not None:
            self.favorites_box.remove(child)
        favorites = list(self.cfg.get("favorite_apps", []) or [])
        apps = self.desktop.list_apps(limit=500)
        app_by_id = {app["id"]: app for app in apps}
        shown = 0
        for app_id in favorites:
            app = app_by_id.get(app_id)
            if app is None:
                continue
            self.favorites_box.append(self._app_launch_row(app, pinned=True))
            shown += 1
        if not shown:
            hint = Gtk.Label(label="Pin apps from search for quick launch", xalign=0)
            hint.add_css_class("empty-hint")
            self.favorites_box.append(hint)

    def refresh_launcher(self):
        if not hasattr(self, "launcher_box") or self.desktop is None:
            return
        query = self.app_search.get_text().strip()
        while (child := self.launcher_box.get_first_child()) is not None:
            self.launcher_box.remove(child)
        if not query:
            self.launcher_box.set_visible(False)
            return
        self.launcher_box.set_visible(True)
        apps = self.desktop.list_apps(query, limit=8)
        if not apps:
            hint = Gtk.Label(label="No matching apps", xalign=0)
            hint.add_css_class("empty-hint")
            self.launcher_box.append(hint)
            return
        pinned = set(self.cfg.get("favorite_apps", []) or [])
        for app in apps:
            self.launcher_box.append(
                self._app_launch_row(app, pinned=app.get("id") in pinned)
            )

    def set_open(self, opened: bool):
        self._apps_open = bool(opened)
        self.apps_revealer.set_reveal_child(self._apps_open)
        self.apps_chev.set_from_icon_name(
            "pan-up-symbolic" if self._apps_open else "pan-down-symbolic"
        )
        if self._embedded:
            if self._apps_open:
                self._head_btn.add_css_class("dock-tile--active")
            else:
                self._head_btn.remove_css_class("dock-tile--active")
        if self._on_toggle is not None:
            self._on_toggle("apps", self._apps_open)
        if self._apps_open:
            self.refresh_apps(force=True)

    def toggle_open(self):
        self.set_open(not self._apps_open)

    def refresh_apps(self, force: bool = True):
        if not hasattr(self, "apps_box") or self.desktop is None:
            return
        query = self.app_search.get_text() if hasattr(self, "app_search") else ""
        apps = self.desktop.list_running_apps(query, limit=16)
        snap = tuple(
            (
                a["key"],
                len(a["pids"]),
                a["window_id"],
                a["tray_item"],
                round(a["rss_mb"] / 16),
            )
            for a in apps
        )
        if hasattr(self, "apps_badge"):
            self.apps_badge.set_label(str(len(apps) + len(self.desktop.windows)))
        if hasattr(self, "apps_summary"):
            active = next(
                (w for w in self.desktop.windows if w.get("active") and w.get("title")),
                None,
            )
            if active:
                self.apps_summary.set_label(active["title"])
            elif apps:
                self.apps_summary.set_label(
                    apps[0]["name"] if len(apps) == 1 else f"{len(apps)} background"
                )
            else:
                self.apps_summary.set_label("Find an app")
        if not force and snap == self._last_apps_snap:
            return
        self._last_apps_snap = snap
        self._armed_end = ""
        while (c := self.apps_box.get_first_child()) is not None:
            self.apps_box.remove(c)
        if not apps:
            empty = Gtk.Label(
                label="No background apps running",
                xalign=0,
            )
            empty.add_css_class("empty-hint")
            self.apps_box.append(empty)
            return
        for app in apps:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            btn = Gtk.Button()
            btn.add_css_class("panel-row-btn")
            btn.set_hexpand(True)
            role = "Tray" if app["tray_item"] else "Background"
            proc_txt = f"{len(app['pids'])}p" if len(app["pids"]) > 1 else ""
            mem_txt = f"{app['rss_mb']:.0f}M" if app["rss_mb"] >= 1 else ""
            meta_txt = " · ".join(p for p in (proc_txt, mem_txt) if p)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            name_lbl = Gtk.Label(label=app["name"], xalign=0, hexpand=True)
            name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            role_pill = Gtk.Label(label=role)
            role_pill.add_css_class("role-pill")
            box.append(name_lbl)
            box.append(role_pill)
            if meta_txt:
                meta_lbl = Gtk.Label(label=meta_txt, xalign=1)
                meta_lbl.add_css_class("tile-sub")
                box.append(meta_lbl)
            btn.set_child(box)
            btn.set_tooltip_text(
                "Activate from system tray"
                if app["tray_item"]
                else "Running in background"
            )
            btn.connect(
                "clicked",
                lambda _b, tray=app["tray_item"]: (
                    self.desktop.activate_tray_item(tray) if tray else None
                ),
            )
            end_btn = Gtk.Button(label="End")
            end_btn.add_css_class("list-clear-btn")
            end_btn.add_css_class("danger-btn")
            end_btn.connect(
                "clicked",
                lambda b, k=app["key"], pids=app["pids"]: self._on_end_app(b, k, pids),
            )
            row.append(btn)
            row.append(end_btn)
            self.apps_box.append(row)

    def refresh_windows(self):
        if not hasattr(self, "windows_box") or self.desktop is None:
            return
        windows = [
            w
            for w in self.desktop.windows
            if str(w.get("id", "")).strip() and str(w.get("title", "")).strip()
        ]
        snap = tuple(
            (w["id"], w["title"], w.get("app", ""), bool(w.get("active")))
            for w in windows
        )
        if snap == self._last_windows_snap:
            return
        self._last_windows_snap = snap
        while (child := self.windows_box.get_first_child()) is not None:
            self.windows_box.remove(child)
        self.windows_title.set_visible(bool(windows))
        backend = getattr(self.desktop, "_backend", "none")
        can_focus = backend in {"hyprland", "sway", "x11"} or (
            backend == "kde"
            and bool(os.environ.get("DISPLAY"))
            and shutil.which("wmctrl") is not None
        )
        for win in windows[:12]:
            button = Gtk.Button()
            button.add_css_class("panel-row-btn")
            if win.get("active"):
                button.add_css_class("panel-row-btn--active")
            button.set_hexpand(True)
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            icon = Gtk.Image.new_from_icon_name("application-x-executable-symbolic")
            icon.set_pixel_size(14)
            title = Gtk.Label(label=win["title"], xalign=0, hexpand=True)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            content.append(icon)
            content.append(title)
            if win.get("active"):
                active = _badge("Active", accent=True)
                content.append(active)
            button.set_child(content)
            button.set_sensitive(can_focus)
            button.set_tooltip_text(
                "Focus window" if can_focus else "Window switching unavailable"
            )
            button.connect(
                "clicked", lambda _b, wid=win["id"]: self.desktop.focus_window(wid)
            )
            self.windows_box.append(button)

    def refresh_workspaces(self):
        if not hasattr(self, "workspaces_box") or self.desktop is None:
            return
        workspaces = list(self.desktop.workspaces or [])
        snap = tuple(
            (w.get("id"), w.get("name"), bool(w.get("active"))) for w in workspaces
        )
        if snap == getattr(self, "_last_workspaces_snap", None):
            return
        self._last_workspaces_snap = snap
        while (child := self.workspaces_box.get_first_child()) is not None:
            self.workspaces_box.remove(child)
        has_workspaces = bool(workspaces)
        self.workspaces_title.set_visible(has_workspaces)
        self.workspaces_box.set_visible(has_workspaces)
        can_switch = getattr(self.desktop, "_backend", "none") in {
            "hyprland",
            "sway",
        }
        for workspace in workspaces[:12]:
            name = str(workspace.get("name") or workspace.get("id") or "")
            button = Gtk.Button(label=name)
            button.add_css_class("panel-row-btn")
            if workspace.get("active"):
                button.add_css_class("panel-row-btn--active")
            button.set_sensitive(can_switch)
            button.set_tooltip_text(f"Switch to workspace {name}")
            workspace_id = str(workspace.get("id", ""))
            button.connect(
                "clicked",
                lambda _b, wid=workspace_id: self.desktop.switch_workspace(wid),
            )
            self.workspaces_box.append(button)

    def _cleanup(self):
        if self._disarm_timer:
            GLib.source_remove(self._disarm_timer)
            self._disarm_timer = 0

    def _schedule_disarm(self):
        if self._disarm_timer:
            GLib.source_remove(self._disarm_timer)
        self._disarm_timer = GLib.timeout_add_seconds(4, self._disarm_pending)

    def _disarm_pending(self) -> bool:
        self._disarm_timer = 0
        self._armed_end = ""
        if hasattr(self, "apps_box"):
            self._last_apps_snap = None
            self.refresh_apps(force=True)
        return False

    def _on_end_app(self, btn: Gtk.Button, key: str, pids: list[int]):
        if self._armed_end == key:
            self._armed_end = ""
            if self._disarm_timer:
                GLib.source_remove(self._disarm_timer)
                self._disarm_timer = 0
            if self.desktop is not None:
                self.desktop.terminate_app(pids)
        else:
            self._armed_end = key
            btn.set_label("End?")
            self._schedule_disarm()

    def refresh_dynamic(self):
        self.refresh_apps(force=False)
        self.refresh_workspaces()
        self.refresh_windows()
        self._sync_keyboard_options()
        if self.desktop is not None:
            layout = self.desktop.keyboard_layout
            self.kb_lbl.set_visible(bool(layout))
            if layout:
                self.kb_lbl.set_label(
                    layout.upper() if self._embedded else f"KB: {layout}"
                )


_AI_PROVIDER_LABELS = (
    ("ollama", "Ollama"),
    ("lmstudio", "LM Studio"),
    ("claude", "Claude"),
    ("gemini", "Gemini"),
    ("openrouter", "OpenRouter"),
)


class AiDevPanel(Gtk.Box):
    """Unified AI Assistant & local LLM telemetry card."""

    def __init__(
        self,
        cfg: dict,
        ollama=None,
        productivity=None,
        desktop=None,
        clipboard=None,
        embedded: bool = False,
        on_toggle=None,
    ):
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL, spacing=0 if embedded else 8
        )
        self.cfg = cfg
        self.ollama = ollama
        self.prod = productivity
        self.desktop = desktop
        self.clipboard = clipboard
        self._embedded = embedded
        self._on_toggle = on_toggle
        self._open = False
        self._pending_prompt = ""
        self._stream_status = ""
        self._stream_text = ""
        self._stream_lbl: Gtk.Label | None = None
        self._error_text = ""
        self._copy_timer = 0
        self._scroll_idle = 0
        self._focus_idle = 0

        self.connect("destroy", lambda *_: self._cleanup())

        mods = self.cfg.get("modules", {})
        if mods.get("assistant", True):
            self._build_assistant()
        else:
            self._build_telemetry()

        if self.ollama is not None:
            self.ollama.connect("changed", lambda *_: self.refresh())
        if self.desktop is not None:
            self.desktop.connect("activity-changed", lambda *_: self.refresh())
        if self.prod is not None:
            self.prod.connect("ai-reply", self._on_ai_reply)
            self.prod.connect("ai-progress", self._on_ai_progress)
            self.prod.connect("action-confirm", self._on_action_confirm)

        self.refresh()

    def _build_telemetry(self):
        l_box = _section_card(4)
        l_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        l_title = _section_title("Local LLM & Dev")
        l_title.set_hexpand(True)
        self.llm_badge = _badge("Offline")
        l_ref = _icon_btn("view-refresh-symbolic", "Refresh local LLM status")
        l_ref.connect("clicked", lambda *_: self.ollama and self.ollama.refresh_now())
        l_head.append(l_title)
        l_head.append(self.llm_badge)
        l_head.append(l_ref)
        self.llm_val = Gtk.Label(label="Offline", xalign=0)
        self.llm_val.add_css_class("tile-big")
        self.llm_val.set_ellipsize(Pango.EllipsizeMode.END)
        self.llm_sub = Gtk.Label(label="No active builds", xalign=0)
        self.llm_sub.add_css_class("tile-sub")
        self.llm_sub.set_ellipsize(Pango.EllipsizeMode.END)
        l_box.append(l_head)
        l_box.append(self.llm_val)
        l_box.append(self.llm_sub)
        self.append(l_box)

    def _build_assistant(self):
        container = (
            Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            if self._embedded
            else _section_card(8)
        )
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        ai_ico = Gtk.Image.new_from_icon_name(
            pick_icon(
                self,
                "starred-symbolic",
                "applications-science-symbolic",
                "emblem-system-symbolic",
            )
        )
        ai_ico.set_pixel_size(14)
        ai_ico.add_css_class("dock-tile-icon")
        head.append(ai_ico)
        title = _section_title("AI Assistant")
        head.append(title)

        self.llm_badge = _badge("Offline")
        self.llm_badge.set_halign(Gtk.Align.START)
        if not self._embedded:
            head.append(self.llm_badge)
        spacer = Gtk.Box(hexpand=True)
        head.append(spacer)

        self.prov_btn = Gtk.Button(label="Ollama")
        self.prov_btn.add_css_class("panel-row-btn")
        self.prov_btn.set_tooltip_text("Click to switch AI provider")
        self.prov_btn.connect("clicked", self._cycle_provider)

        self.model_btn = Gtk.Button(label="")
        self.model_btn.add_css_class("list-clear-btn")
        self.model_btn.set_tooltip_text("Click to cycle pulled local models")
        self.model_btn.connect("clicked", self._cycle_model)

        self.copy_btn = _icon_btn("edit-copy-symbolic", "Copy latest AI reply")
        self.copy_btn.connect("clicked", self._copy_last_reply)
        self.copy_btn.set_visible(False)

        self.clr_btn = Gtk.Button(label="Clear")
        self.clr_btn.add_css_class("list-clear-btn")
        self.clr_btn.connect("clicked", self._clear_chat)
        self.clr_btn.set_visible(False)

        self.agent_btn = Gtk.ToggleButton(label="Sys")
        self.agent_btn.add_css_class("panel-row-btn")
        self.agent_btn.set_tooltip_text(
            "Allow AI to read live system details and control devices. "
            "Online providers may receive this context."
        )
        self.agent_btn.set_active(bool(self.cfg.get("ai_system_control", False)))
        self.agent_btn.connect("toggled", self._on_toggle_agent)

        self.hook_btn = _icon_btn(
            "utilities-terminal-symbolic",
            "Copy coding-agent hook command (Claude Code / CLI)",
        )
        self.hook_btn.connect("clicked", self._copy_agent_hook)
        self.hook_btn.set_visible(self.clipboard is not None)

        for w in (
            self.prov_btn,
            self.model_btn,
            self.copy_btn,
            self.clr_btn,
            self.hook_btn,
            self.agent_btn,
        ):
            head.append(w)
        container.append(head)

        self.chat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.chat_scroll = _scroll_box(self.chat_box, 145)
        self.chat_scroll.set_visible(False)
        container.append(self.chat_scroll)

        self.confirm_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.confirm_box.add_css_class("ai-confirm-bar")
        self.confirm_lbl = Gtk.Label(xalign=0, hexpand=True)
        self.confirm_lbl.add_css_class("list-title")
        self.confirm_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        c_ok = Gtk.Button(label="Confirm")
        c_ok.add_css_class("panel-row-btn")
        c_ok.add_css_class("panel-row-btn--active")
        c_ok.connect("clicked", lambda *_: self._resolve_action(True))
        c_no = Gtk.Button(label="Cancel")
        c_no.add_css_class("panel-row-btn")
        c_no.add_css_class("danger-btn")
        c_no.connect("clicked", lambda *_: self._resolve_action(False))
        self.confirm_box.append(self.confirm_lbl)
        self.confirm_box.append(c_ok)
        self.confirm_box.append(c_no)
        self.confirm_box.set_visible(False)
        container.append(self.confirm_box)

        prompt_shell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        prompt_shell.add_css_class("ai-prompt-shell")
        if self.clipboard is not None:
            paste_btn = _icon_btn(
                "edit-paste-symbolic", "Insert latest clipboard text into prompt"
            )
            paste_btn.connect("clicked", self._insert_clipboard)
            prompt_shell.append(paste_btn)
        self.ai_entry = Gtk.Entry(
            placeholder_text="Ask AI or control system…", hexpand=True
        )
        self.ai_entry.add_css_class("ai-entry")
        self.ai_entry.connect("activate", self._on_ask)

        self.ask_btn = Gtk.Button(label="Send")
        self.ask_btn.add_css_class("ai-send-btn")
        self.ask_btn.connect("clicked", self._on_ask)

        for w in (self.ai_entry, self.ask_btn):
            prompt_shell.append(w)
        container.append(prompt_shell)

        self.quick_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        quick_specs = (
            (
                "System health",
                "Give a concise overview of my CPU, RAM, GPU, battery, network, and any alerts.",
            ),
            (
                "Diagnose",
                "Check systemd failed units, top CPU/RAM processes, and disk free space.",
            ),
            (
                "VRAM status",
                "Check local LLM VRAM status and active model telemetry.",
            ),
        )
        for chip_lbl, chip_prompt in quick_specs:
            qb = Gtk.Button(label=chip_lbl)
            qb.add_css_class("ai-quick-chip")
            qb.connect("clicked", lambda _b, p=chip_prompt: self._submit_prompt(p))
            self.quick_row.append(qb)
        container.append(self.quick_row)

        if self._embedded:
            self.ai_summary = Gtk.Label(label="Click to ask", xalign=0, hexpand=True)
            self.ai_summary.add_css_class("tile-sub")
            self.ai_chev = Gtk.Image.new_from_icon_name("pan-down-symbolic")
            self.head_tile = build_dock_tile(
                pick_icon(
                    self,
                    "starred-symbolic",
                    "applications-science-symbolic",
                    "emblem-system-symbolic",
                ),
                "AI",
                self.llm_badge,
                self.ai_summary,
                chevron=self.ai_chev,
                tooltip="Click to open AI Assistant",
            )
            self.head_tile.connect("clicked", lambda *_: self.toggle_open())
            self.revealer = Gtk.Revealer()
            self.revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
            self.revealer.set_transition_duration(180)
            self.revealer.set_reveal_child(False)
            self.revealer.set_child(container)
        else:
            self.append(container)

    def set_open(self, opened: bool):
        self._open = bool(opened)
        if hasattr(self, "revealer"):
            self.revealer.set_reveal_child(self._open)
        if hasattr(self, "ai_chev"):
            self.ai_chev.set_from_icon_name(
                "pan-up-symbolic" if self._open else "pan-down-symbolic"
            )
        if self._embedded and hasattr(self, "head_tile"):
            if self._open:
                self.head_tile.add_css_class("dock-tile--active")
            else:
                self.head_tile.remove_css_class("dock-tile--active")
        if self._on_toggle is not None:
            self._on_toggle("ai", self._open)
        if self._open and hasattr(self, "ai_entry") and self.ai_entry.get_mapped():
            if self._focus_idle:
                GLib.source_remove(self._focus_idle)
            self._focus_idle = GLib.idle_add(self._focus_entry)

    def _focus_entry(self) -> bool:
        self._focus_idle = 0
        if hasattr(self, "ai_entry") and self._open:
            self.ai_entry.grab_focus()
        return False

    def toggle_open(self):
        self.set_open(not self._open)

    def _on_toggle_agent(self, btn: Gtk.ToggleButton):
        self.cfg["ai_system_control"] = bool(btn.get_active())
        config.save(self.cfg)
        self._sync_ai_header()

    def _on_action_confirm(self, _svc, label: str, _cb):
        if hasattr(self, "confirm_box"):
            self.confirm_lbl.set_label(label)
            self.confirm_box.set_visible(True)
            if self._embedded and not self._open:
                self.set_open(True)

    def _resolve_action(self, approved: bool):
        if hasattr(self, "confirm_box"):
            self.confirm_box.set_visible(False)
        if self.prod is not None and hasattr(self.prod, "resolve_pending_action"):
            self.prod.resolve_pending_action(approved)

    def _cycle_provider(self, *_):
        ids = [k for k, _ in _AI_PROVIDER_LABELS]
        current = str(self.cfg.get("ai_provider", "ollama"))
        idx = ids.index(current) if current in ids else 0
        provider = ids[(idx + 1) % len(ids)]
        model = str(self.cfg.get("ai_model", "") or "").strip()
        current_pool = self._local_model_pool(current)
        next_pool = self._local_model_pool(provider)
        if model and (
            (current in ("ollama", "lmstudio") and model in current_pool)
            or (
                provider in ("ollama", "lmstudio")
                and next_pool
                and model not in next_pool
            )
        ):
            self.cfg["ai_model"] = ""
        self.cfg["ai_provider"] = provider
        config.save(self.cfg)
        self._sync_ai_header()

    def _local_model_pool(self, provider: str) -> list[str]:
        if self.ollama is None:
            return []
        if provider == "ollama":
            return list(self.ollama.models)
        if provider == "lmstudio":
            return list(self.ollama.lmstudio_models)
        return []

    def _cycle_model(self, *_):
        prov = str(self.cfg.get("ai_provider", "ollama"))
        pool = self._local_model_pool(prov)
        if len(pool) < 2:
            return
        cur = (
            self.prod.resolved_ai_model(prov)
            if self.prod is not None
            else str(self.cfg.get("ai_model", "") or pool[0])
        )
        idx = pool.index(cur) if cur in pool else 0
        self.cfg["ai_model"] = pool[(idx + 1) % len(pool)]
        config.save(self.cfg)
        self._sync_ai_header()

    def _insert_clipboard(self, *_):
        if self.clipboard is None or not getattr(self.clipboard, "history", None):
            return
        snippet = self.clipboard.history[0][:1500].strip()
        if not snippet:
            return
        cur = self.ai_entry.get_text().strip()
        text = f"{cur}\n\n{snippet}" if cur else f"Explain or summarize:\n\n{snippet}"
        self.ai_entry.set_text(text)
        self.ai_entry.grab_focus()

    def _cleanup(self):
        if self._copy_timer:
            GLib.source_remove(self._copy_timer)
            self._copy_timer = 0
        if self._scroll_idle:
            GLib.source_remove(self._scroll_idle)
            self._scroll_idle = 0
        if self._focus_idle:
            GLib.source_remove(self._focus_idle)
            self._focus_idle = 0

    def _copy_last_reply(self, *_):
        reply = self.prod.last_ai_reply if self.prod is not None else ""
        if not reply:
            return
        copied = False
        if self.clipboard is not None and hasattr(self.clipboard, "copy_text"):
            copied = self.clipboard.copy_text(reply)
        else:
            disp = self.get_display()
            if disp is not None:
                disp.get_clipboard().set(reply)
                copied = True
        if copied:
            self.copy_btn.set_tooltip_text("Copied ✓")
            if self._copy_timer:
                GLib.source_remove(self._copy_timer)
            self._copy_timer = GLib.timeout_add(
                1000,
                lambda: (
                    setattr(self, "_copy_timer", 0),
                    self.copy_btn.set_tooltip_text("Copy latest AI reply"),
                    False,
                )[2],
            )

    def _copy_agent_hook(self, *_):
        if self.clipboard is None or not hasattr(self.clipboard, "copy_text"):
            return
        cmd = 'lost-island --agent "Claude Code:attention:Waiting for approval"'
        if self.clipboard.copy_text(cmd):
            self.hook_btn.set_tooltip_text("Hook command copied ✓")
            GLib.timeout_add(
                1200,
                lambda: (
                    self.hook_btn.set_tooltip_text(
                        "Copy coding-agent hook command (Claude Code / CLI)"
                    ),
                    False,
                )[1],
            )

    def _sync_ai_summary(self, online: bool | None = None):
        if not hasattr(self, "ai_summary"):
            return
        if online is None:
            online = bool(
                self.ollama is not None
                and (self.ollama.available or self.ollama.lmstudio_available)
            )
        prov = str(self.cfg.get("ai_provider", "ollama"))
        mod = (
            self.prod.resolved_ai_model(prov)
            if self.prod is not None and hasattr(self.prod, "resolved_ai_model")
            else ""
        )
        self.ai_summary.set_label(mod or ("Click to ask" if online else "Offline"))

    def _clear_chat(self, *_):
        self._pending_prompt = ""
        self._stream_status = ""
        self._stream_text = ""
        self._error_text = ""
        if self.prod is not None and hasattr(self.prod, "clear_ai_history"):
            self.prod.clear_ai_history()
        self._sync_ai_summary()
        self._refresh_chat()

    def _submit_prompt(self, prompt: str):
        text = (prompt or "").strip()
        if not text or self.prod is None or getattr(self.prod, "_ai_busy", False):
            return
        if self._embedded and not self._open:
            self.set_open(True)
        self._pending_prompt = text
        self._stream_status = ""
        self._stream_text = ""
        self._error_text = ""
        if hasattr(self, "ask_btn"):
            self.ask_btn.set_label("Stop")
            self.ask_btn.set_tooltip_text("Stop generation")
        self._sync_ai_summary()
        self._refresh_chat()
        self.prod.ask_ai(text)

    def _on_ask(self, *_):
        if self.prod is None:
            return
        if getattr(self.prod, "_ai_busy", False):
            if hasattr(self.prod, "cancel_ai"):
                self.prod.cancel_ai()
            return
        prompt = self.ai_entry.get_text().strip()
        if not prompt:
            return
        self.ai_entry.set_text("")
        self._submit_prompt(prompt)

    def _on_ai_progress(self, _svc, status: str, partial: str):
        if not self._pending_prompt:
            return
        if status:
            self._stream_status = status
        if partial:
            self._stream_text = partial
        if self._stream_lbl is not None:
            self._stream_lbl.set_label(self._live_bubble_text())
            vadj = self.chat_scroll.get_vadjustment()
            if self._scroll_idle:
                GLib.source_remove(self._scroll_idle)
            self._scroll_idle = GLib.idle_add(self._scroll_chat_bottom, vadj)

    def _live_bubble_text(self) -> str:
        if self._stream_text:
            return self._stream_text
        if self._stream_status:
            return f"⚙ {self._stream_status}"
        return "Thinking…"

    def _on_ai_reply(self, _svc, reply: str, ok: bool):
        self._pending_prompt = ""
        self._stream_status = ""
        self._stream_text = ""
        self._error_text = "" if ok else reply
        if hasattr(self, "ask_btn"):
            self.ask_btn.set_label("Send")
            self.ask_btn.set_tooltip_text("Send prompt")
            self.ask_btn.set_sensitive(True)
        self._sync_ai_summary()
        self._refresh_chat()

    def _sync_ai_header(self):
        if not hasattr(self, "prov_btn"):
            return
        if hasattr(self, "agent_btn"):
            active = bool(self.cfg.get("ai_system_control", False))
            if self.agent_btn.get_active() != active:
                self.agent_btn.handler_block_by_func(self._on_toggle_agent)
                self.agent_btn.set_active(active)
                self.agent_btn.handler_unblock_by_func(self._on_toggle_agent)
            if active:
                self.agent_btn.add_css_class("panel-row-btn--active")
            else:
                self.agent_btn.remove_css_class("panel-row-btn--active")
        prov = str(self.cfg.get("ai_provider", "ollama"))
        label_map = dict(_AI_PROVIDER_LABELS)
        self.prov_btn.set_label(label_map.get(prov, prov.title()))
        model_name = (
            self.prod.resolved_ai_model(prov)
            if self.prod is not None and hasattr(self.prod, "resolved_ai_model")
            else str(self.cfg.get("ai_model", "") or "")
        )
        self.model_btn.set_label(model_name[:14] if model_name else "default")
        self.model_btn.set_visible(prov in ("ollama", "lmstudio") and bool(model_name))
        history = list(getattr(self.prod, "ai_history", [])) if self.prod else []
        has_reply = bool(self.prod and getattr(self.prod, "last_ai_reply", ""))
        self.copy_btn.set_visible(has_reply)
        self.clr_btn.set_visible(
            bool(history or self._pending_prompt or self._error_text)
        )

    def _refresh_chat(self):
        if not hasattr(self, "chat_box"):
            return
        self._sync_ai_header()
        self._stream_lbl = None
        while (c := self.chat_box.get_first_child()) is not None:
            self.chat_box.remove(c)
        history = list(getattr(self.prod, "ai_history", [])) if self.prod else []
        show_chat = bool(history or self._pending_prompt or self._error_text)
        self.chat_scroll.set_visible(show_chat)
        if hasattr(self, "quick_row"):
            self.quick_row.set_visible(not show_chat)
        if not show_chat:
            return

        for msg in history[-6:]:
            is_user = msg.get("role") == "user"
            bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            bubble.add_css_class("chat-bubble-user" if is_user else "chat-bubble-ai")
            role_lbl = Gtk.Label(label="You" if is_user else "AI", xalign=0)
            role_lbl.add_css_class("tile-sub")
            lbl = Gtk.Label(label=msg.get("content", ""), xalign=0)
            lbl.add_css_class("list-title" if is_user else "list-sub")
            lbl.set_wrap(True)
            lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            lbl.set_max_width_chars(46)
            lbl.set_selectable(True)
            bubble.append(role_lbl)
            bubble.append(lbl)
            self.chat_box.append(bubble)

        if self._pending_prompt:
            q_bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            q_bubble.add_css_class("chat-bubble-user")
            q_role = Gtk.Label(label="You", xalign=0)
            q_role.add_css_class("tile-sub")
            q_lbl = Gtk.Label(label=self._pending_prompt, xalign=0)
            q_lbl.add_css_class("list-title")
            q_lbl.set_wrap(True)
            q_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            q_lbl.set_max_width_chars(46)
            q_bubble.append(q_role)
            q_bubble.append(q_lbl)
            self.chat_box.append(q_bubble)

            t_bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            t_bubble.add_css_class("chat-bubble-ai")
            t_role = Gtk.Label(label="AI", xalign=0)
            t_role.add_css_class("tile-sub")
            t_lbl = Gtk.Label(label=self._live_bubble_text(), xalign=0)
            t_lbl.add_css_class("list-sub")
            t_lbl.set_wrap(True)
            t_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            t_lbl.set_max_width_chars(46)
            self._stream_lbl = t_lbl
            t_bubble.append(t_role)
            t_bubble.append(t_lbl)
            self.chat_box.append(t_bubble)
        elif self._error_text:
            e_bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            e_bubble.add_css_class("chat-bubble-ai")
            e_lbl = Gtk.Label(label=self._error_text, xalign=0)
            e_lbl.add_css_class("list-sub")
            e_lbl.set_wrap(True)
            e_lbl.set_selectable(True)
            e_bubble.append(e_lbl)
            self.chat_box.append(e_bubble)

        vadj = self.chat_scroll.get_vadjustment()
        if self._scroll_idle:
            GLib.source_remove(self._scroll_idle)
        self._scroll_idle = GLib.idle_add(self._scroll_chat_bottom, vadj)

    def _scroll_chat_bottom(self, vadj) -> bool:
        self._scroll_idle = 0
        vadj.set_value(max(0.0, vadj.get_upper() - vadj.get_page_size()))
        return False

    def refresh(self):
        online = bool(
            self.ollama is not None
            and (self.ollama.available or self.ollama.lmstudio_available)
        )
        run_det = (
            list(getattr(self.ollama, "running_details", []) or [])
            if self.ollama is not None
            else []
        )
        lms_loaded = (
            list(getattr(self.ollama, "lmstudio_loaded", []) or [])
            if self.ollama is not None
            else []
        )
        dev_parts = []
        if run_det:
            d0 = run_det[0]
            if d0.get("vram_gb", 0) > 0:
                dev_parts.append(f"{d0['vram_gb']:.1f}G VRAM ({d0['vram_pct']}% GPU)")
        if self.ollama is not None and self.ollama.tokens_per_sec > 0:
            dev_parts.append(f"{self.ollama.tokens_per_sec:.1f} tok/s")
        elif not dev_parts and self.ollama is not None and self.ollama.models:
            dev_parts.append(self.ollama.models[0])
        if self.desktop is not None:
            if getattr(self.desktop, "agent_activity", ""):
                dev_parts.append(self.desktop.agent_activity)
            if self.desktop.package_activity:
                dev_parts.append(self.desktop.package_activity)
            if self.desktop.build_activity:
                dev_parts.append(self.desktop.build_activity)

        if hasattr(self, "llm_badge"):
            if online:
                self.llm_badge.add_css_class("section-badge--accent")
                if run_det:
                    d0 = run_det[0]
                    lbl_txt = (
                        f"GPU {d0['vram_pct']}%" if d0.get("vram_gb", 0) > 0 else "CPU"
                    )
                elif self.ollama.available:
                    lbl_txt = "Ready"
                elif lms_loaded:
                    lbl_txt = "LM Loaded"
                else:
                    lbl_txt = "LM Studio"
                self.llm_badge.set_label(lbl_txt)
            else:
                self.llm_badge.remove_css_class("section-badge--accent")
                self.llm_badge.set_label("Offline")
            tip = []
            if run_det:
                d0 = run_det[0]
                meta = " · ".join(
                    p
                    for p in (
                        d0.get("parameter_size", ""),
                        d0.get("quantization", ""),
                        f"ctx {d0['context_length']}"
                        if d0.get("context_length")
                        else "",
                    )
                    if p
                )
                tip.append(f"Loaded: {d0['name']}" + (f" ({meta})" if meta else ""))
            elif self.ollama is not None and self.ollama.running_models:
                tip.append(f"Loaded: {self.ollama.running_models[0]}")
            elif lms_loaded:
                tip.append(f"LM Studio loaded: {lms_loaded[0]}")
            tip.extend(dev_parts)
            self.llm_badge.set_tooltip_text(
                "\n".join(tip) if tip else "Local LLM status"
            )

        if hasattr(self, "llm_val"):
            if online:
                if self.ollama.running_models:
                    self.llm_val.set_label(self.ollama.running_models[0])
                elif self.ollama.models:
                    self.llm_val.set_label(f"{len(self.ollama.models)} models")
                elif self.ollama.lmstudio_available:
                    self.llm_val.set_label(
                        self.ollama.lmstudio_models[0]
                        if self.ollama.lmstudio_models
                        else "LM Studio"
                    )
                else:
                    self.llm_val.set_label("Ready")
            else:
                self.llm_val.set_label("Offline")
            self.llm_sub.set_label(
                " · ".join(dev_parts) if dev_parts else "No active builds"
            )

        self._sync_ai_summary(online)
        self._refresh_chat()


class CameraMirror(Gtk.Popover):
    """Nusa/Bloom pre-call camera mirror popover opened strictly on demand."""

    def __init__(self):
        super().__init__()
        self._proc: subprocess.Popen | None = None
        self._active = False
        self._gen = 0
        self._flip = True

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl = _section_title("Camera Mirror")
        lbl.set_hexpand(True)
        flip_btn = Gtk.ToggleButton(label="Flip")
        flip_btn.set_active(True)
        flip_btn.add_css_class("panel-row-btn")
        flip_btn.connect("toggled", self._on_flip)
        head.append(lbl)
        head.append(flip_btn)

        self.picture = Gtk.Picture()
        self.picture.set_size_request(256, 192)
        self.picture.set_content_fit(Gtk.ContentFit.COVER)
        self.picture.add_css_class("mirror-preview")

        self.status_lbl = Gtk.Label(label="Ready", xalign=0.5)
        self.status_lbl.add_css_class("tile-sub")

        box.append(head)
        box.append(self.picture)
        box.append(self.status_lbl)
        self.set_child(box)

        self.connect("map", lambda *_: self.start_preview())
        self.connect("unmap", lambda *_: self.stop_preview())
        self.connect("destroy", lambda *_: self.stop_preview())

    def _on_flip(self, btn: Gtk.ToggleButton):
        self._flip = bool(btn.get_active())
        if self.get_mapped():
            self.stop_preview()
            self.start_preview()

    def start_preview(self):
        self.stop_preview()
        devs = sorted(glob.glob("/dev/video*"))
        ffmpeg = shutil.which("ffmpeg")
        if not devs:
            self.status_lbl.set_label("No /dev/video* camera found")
            return
        if not ffmpeg:
            self.status_lbl.set_label("Install ffmpeg for live mirror")
            return
        self._active = True
        self._gen += 1
        gen = self._gen
        self.status_lbl.set_label(os.path.basename(devs[0]))
        vf = ["-vf", "hflip"] if self._flip else []
        cmd = [
            ffmpeg,
            "-loglevel",
            "error",
            "-f",
            "v4l2",
            "-video_size",
            "320x240",
            "-i",
            devs[0],
            *vf,
            "-f",
            "image2pipe",
            "-vcodec",
            "bmp",
            "-r",
            "15",
            "-",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except OSError:
            self.status_lbl.set_label("Could not open camera")
            self._active = False
            return
        threading.Thread(
            target=self._read_frames, args=(self._proc, gen), daemon=True
        ).start()

    def _read_frames(self, proc: subprocess.Popen, gen: int):
        stdout = proc.stdout
        if stdout is None:
            return
        while self._active and gen == self._gen and proc.poll() is None:
            hdr = stdout.read(6)
            if len(hdr) < 6 or hdr[:2] != b"BM":
                break
            total = int.from_bytes(hdr[2:6], "little")
            if total <= 54 or total > 4 * 1024 * 1024:
                break
            rest = stdout.read(total - 6)
            if len(rest) < total - 6:
                break
            frame = hdr + rest
            GLib.idle_add(self._apply_frame, frame, gen)
        GLib.idle_add(self._on_stream_end, gen)

    def _apply_frame(self, frame: bytes, gen: int) -> bool:
        if not self._active or gen != self._gen:
            return False
        try:
            tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(frame))
            self.picture.set_paintable(tex)
        except GLib.Error:
            pass
        return False

    def _on_stream_end(self, gen: int) -> bool:
        if self._active and gen == self._gen and self.picture.get_paintable() is None:
            self.status_lbl.set_label("Camera busy or permission denied")
        return False

    def stop_preview(self):
        self._active = False
        self._gen += 1
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                proc.kill()
