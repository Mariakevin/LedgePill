"""Application entry point: layer-shell window, service wiring, CLI."""

from __future__ import annotations

import ctypes
import os
import signal
import sys

# gtk4-layer-shell must be resolved before libwayland-client, which GTK pulls
# in on display init — preload it globally or the surface won't anchor.
try:
    ctypes.CDLL("libgtk4-layer-shell.so.0", mode=ctypes.RTLD_GLOBAL)
except OSError:
    pass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from ledgepill import APP_ID, __version__, config
from ledgepill.services.audio import AudioService
from ledgepill.services.bluetooth import BluetoothService
from ledgepill.services.brightness import BrightnessService
from ledgepill.services.calendar import CalendarService
from ledgepill.services.cava import CavaService
from ledgepill.services.clipboard import ClipboardService
from ledgepill.services.desktop import CursorWatcher, DesktopService
from ledgepill.services.feeds import FeedService
from ledgepill.services.kdeconnect import KdeConnectService
from ledgepill.services.lyrics import LyricsService
from ledgepill.services.mpris import (
    MprisService,
    detect_desktop_accent,
    extract_accent_color,
)
from ledgepill.services.network import NetworkService
from ledgepill.services.notify import NotifyService
from ledgepill.services.ollama import OllamaService
from ledgepill.services.power import PowerService
from ledgepill.services.privacy import PrivacyService
from ledgepill.services.productivity import ProductivityService
from ledgepill.services.system import SystemService
from ledgepill.services.system_agent import SystemAgent
from ledgepill.services.todos import TodoService
from ledgepill.services.weather import WeatherService
from ledgepill.services.webhook import WebhookService
from ledgepill.ui.expanded import _hex_rgb
from ledgepill.ui.island import Island

CSS_PATH = os.path.join(os.path.dirname(__file__), "ui", "style.css")
_GDK_LOG_FILTER_INSTALLED = False


def _read_log_field(field) -> str:
    val = getattr(field, "value", None)
    if isinstance(val, bytes):
        return val.decode("utf-8", "replace")
    if isinstance(val, str):
        return val
    if isinstance(val, int) and val:
        length = getattr(field, "length", -1)
        if length == 0:
            return ""
        return ctypes.string_at(val, length if length > 0 else -1).decode(
            "utf-8", "replace"
        )
    return ""


_SUPPRESSED_GDK_MESSAGES = (
    "Duplicate call to gdk_frame_timings_presented()",
    "gdk_frame_timings_presented() called on discarded frame",
)


def _should_suppress_gdk_log(domain: str, message: str) -> bool:
    return domain == "Gdk" and any(s in message for s in _SUPPRESSED_GDK_MESSAGES)


def _gdk_log_writer(*args):
    level = args[0]
    fields = args[1]
    domain = ""
    msg = ""
    try:
        for f in fields:
            if f.key == "GLIB_DOMAIN":
                domain = _read_log_field(f)
            elif f.key == "MESSAGE":
                msg = _read_log_field(f)
        if _should_suppress_gdk_log(domain, msg):
            return GLib.LogWriterOutput.HANDLED
    except (AttributeError, TypeError, ValueError, ctypes.ArgumentError, UnicodeError):
        return GLib.log_writer_default(level, fields)
    return GLib.log_writer_default(level, fields)


def _install_gdk_log_filter():
    global _GDK_LOG_FILTER_INSTALLED
    if _GDK_LOG_FILTER_INSTALLED or not hasattr(GLib, "log_set_writer_func"):
        return
    _GDK_LOG_FILTER_INSTALLED = True
    try:
        GLib.log_set_writer_func(_gdk_log_writer, None)
    except (GLib.Error, RuntimeError, TypeError):
        pass


def layer_shell_hint() -> str:
    return (
        "ledge-pill: gtk4-layer-shell not found — install the package for "
        "your distro (Ubuntu/Debian: libgtk4-layer-shell0, Arch: "
        "gtk4-layer-shell, Fedora: gtk4-layer-shell) and re-run the app."
    )


def _needs_audio_service(modules: dict, ai_system_control: bool) -> bool:
    if any(modules.get(name, True) for name in ("volume_osd", "privacy", "toggles")):
        return True
    return ai_system_control and any(
        modules.get(name, True) for name in ("assistant", "ollama")
    )


class LedgePill(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.win: Gtk.Window | None = None
        self.island: Island | None = None
        self.settings_win = None
        self._css_provider: Gtk.CssProvider | None = None
        self._layer_shell = None
        self._dynamic_accent: str | None = None
        self._fullscreen_active = False
        self._fs_overridden = False
        self._rebuilding = False
        self._signal_sources: list[int] = []
        self._active_monitor = None
        self._monitor_name = ""
        self.cursor_watcher: CursorWatcher | None = None
        self.media = None
        self.cava = None
        self.lyrics = None
        self.power = None
        self.audio = None
        self.brightness = None
        self.notifier = None
        self.network = None
        self.bluetooth = None
        self.weather = None
        self.system = None
        self.clipboard = None
        self.ollama = None
        self.feeds = None
        self.privacy = None
        self.desktop = None
        self.kdeconnect = None
        self.system_agent = None
        self.productivity = None
        self.todos = None
        self.calendar = None
        self.webhook = None
        for opt, short, desc, arg in (
            ("toggle", "t", "Expand/collapse the island", GLib.OptionArg.NONE),
            ("ai", "a", "Expand straight to AI Assistant", GLib.OptionArg.NONE),
            ("clipboard", "\0", "Expand straight to Clipboard", GLib.OptionArg.NONE),
            (
                "agent",
                "\0",
                "Push coding-agent event (Agent:state:message)",
                GLib.OptionArg.STRING,
            ),
            ("cycle", "c", "Cycle pill faces", GLib.OptionArg.NONE),
            ("dnd", "d", "Toggle Do Not Disturb", GLib.OptionArg.NONE),
            ("lock", "l", "Lock the active desktop session", GLib.OptionArg.NONE),
            ("notify", "n", "Push a notification peek", GLib.OptionArg.STRING),
            ("settings", "s", "Open the settings window", GLib.OptionArg.NONE),
            ("smoke-test", "\0", "Start the UI briefly and exit", GLib.OptionArg.NONE),
            ("quit", "q", "Quit a running instance", GLib.OptionArg.NONE),
            ("version", "v", "Print version", GLib.OptionArg.NONE),
        ):
            self.add_main_option(
                opt,
                ord(short) if short != "\0" else 0,
                GLib.OptionFlags.NONE,
                arg,
                desc,
                "TEXT" if arg == GLib.OptionArg.STRING else None,
            )

    def do_command_line(self, cmdline):
        opts = cmdline.get_options_dict()
        if opts.contains("version"):
            cmdline.print_literal(f"ledge-pill {__version__}\n")
            return 0
        if opts.contains("quit"):
            self.quit()
            return 0
        self.do_activate()
        if opts.contains("smoke-test"):
            GLib.timeout_add(1500, self._quit_smoke_test)
            return 0
        if opts.contains("toggle") and self.island:
            self.island.toggle()
        if opts.contains("ai") and self.island:
            self.island.expand_to_dock("ai")
        if opts.contains("clipboard") and self.island:
            self.island.expand_to_dock("clipboard")
        if opts.contains("agent"):
            val = opts.lookup_value("agent", GLib.VariantType.new("s"))
            raw = val.get_string() if val is not None else ""
            if raw:
                parts = raw.split(":", 2)
                agent = parts[0].strip() or "Agent"
                state = parts[1].strip().lower() if len(parts) > 1 else "attention"
                msg = parts[2].strip() if len(parts) > 2 else ""
                self._on_agent_event(agent, state, msg)
        if opts.contains("cycle") and self.island:
            self.island.pill.cycle(+1)
        if opts.contains("dnd"):
            self._set_dnd(not bool(self.cfg.get("do_not_disturb", False)))
        if opts.contains("lock") and self.desktop:
            self.desktop.power_action("lock")
        if opts.contains("notify") and self.island:
            val = opts.lookup_value("notify", GLib.VariantType.new("s"))
            msg = val.get_string() if val is not None else ""
            if msg:
                if self.notifier is not None:
                    self.notifier.push_custom("CLI", msg, "")
                else:
                    self.island.show_peek(
                        self.island.peek.show_notification,
                        "CLI",
                        msg,
                        "",
                        "",
                        priority=60,
                        key="cli",
                    )
        if opts.contains("settings"):
            self.open_settings()
        return 0

    def do_activate(self):
        if self.win:
            return
        _install_gdk_log_filter()
        self._install_signal_handlers()
        self.cfg = config.load()
        config.write_default()
        self._build_services()
        self._sync_dynamic_accent(reload_css=False)
        self._load_css()
        self._build_window()

    def _install_signal_handlers(self):
        if self._signal_sources or not hasattr(GLib, "unix_signal_add"):
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                src = GLib.unix_signal_add(
                    GLib.PRIORITY_HIGH,
                    sig,
                    lambda *_: (self.quit(), GLib.SOURCE_REMOVE)[1],
                )
                self._signal_sources.append(src)
            except (GLib.Error, TypeError, ValueError):
                pass

    def do_shutdown(self):
        ctx = GLib.MainContext.default()
        for src in self._signal_sources:
            if ctx.find_source_by_id(src) is not None:
                GLib.source_remove(src)
        self._signal_sources.clear()
        if self.settings_win is not None:
            self.settings_win.destroy()
            self.settings_win = None
        self._stop_services()
        Adw.Application.do_shutdown(self)

    def _quit_smoke_test(self):
        self.quit()
        return GLib.SOURCE_REMOVE

    # -- window ------------------------------------------------------------

    def _build_window(self):
        self.win = Gtk.Window(application=self)
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        # only THIS window is transparent (so the rounded island floats);
        # other app windows (settings) keep their solid theme background
        self.win.add_css_class("island-window")

        display = Gdk.Display.get_default()
        is_wayland = display is not None and "Wayland" in type(display).__name__
        if is_wayland:
            try:
                gi.require_version("Gtk4LayerShell", "1.0")
                from gi.repository import Gtk4LayerShell as LayerShell

                if LayerShell.is_supported():
                    self._layer_shell = LayerShell
                else:
                    print(
                        "ledge-pill: layer-shell unsupported on this compositor — "
                        "falling back to a normal window",
                        file=sys.stderr,
                    )
                    self._layer_shell = None
            except (ValueError, ImportError):
                print(layer_shell_hint(), file=sys.stderr)
                print("ledge-pill: falling back to a normal window", file=sys.stderr)
                self._layer_shell = None
        else:
            self._layer_shell = None

        if self._layer_shell:
            self._layer_shell.init_for_window(self.win)
            self._layer_shell.set_namespace(self.win, "ledge-pill")
            self._apply_layer()

        self.island = Island(
            self.cfg,
            self.media,
            self.power,
            weather=self.weather,
            system=self.system,
            on_settings=self.open_settings,
            cava=self.cava,
            lyrics=self.lyrics,
            brightness=self.brightness,
            notifier=self.notifier,
            clipboard=self.clipboard,
            audio=self.audio,
            network=self.network,
            ollama=self.ollama,
            feeds=self.feeds,
            privacy=self.privacy,
            desktop=self.desktop,
            kdeconnect=self.kdeconnect,
            productivity=self.productivity,
            todos=self.todos,
            calendar=self.calendar,
            on_dnd_change=self._set_dnd,
            on_expand_change=self._on_expand_change,
            on_mascot_active=self._sync_cursor_watcher,
        )
        rgb = _hex_rgb(self._active_accent())
        self.island.expanded.ring.accent = rgb
        self.island.pill.mascot.accent = rgb
        self.island.pill.halo.accent = rgb
        self._wire_services()
        self.win.set_child(self.island)
        self.win.connect("close-request", self._on_win_close_request)
        self.win.present()
        self._sync_cursor_watcher(
            bool(
                self.island.pill.mascot.get_mapped()
                and self.island.pill.mascot.get_visible()
            )
        )

    def _on_win_close_request(self, *_):
        if not self._rebuilding:
            self.quit()
        return False

    def _on_expand_change(self, expanded: bool):
        if (
            self._layer_shell
            and self.win
            and hasattr(self._layer_shell, "set_keyboard_mode")
        ):
            mode = (
                self._layer_shell.KeyboardMode.ON_DEMAND
                if expanded
                else self._layer_shell.KeyboardMode.NONE
            )
            self._layer_shell.set_keyboard_mode(self.win, mode)

    def _apply_fullscreen_state(self):
        behavior = self.cfg.get("fullscreen_behavior", "ignore")
        if self.win is not None:
            want_visible = not (self._fullscreen_active and behavior == "hide")
            if self.win.get_visible() != want_visible:
                self.win.set_visible(want_visible)
        if self.island is not None:
            if self._fullscreen_active and behavior == "compact":
                self._fs_overridden = True
                self.island.collapse()
                self.island.pill.set_orientation_style("vertical")
            elif self._fs_overridden:
                self._fs_overridden = False
                self.island.refresh_style()

    def _set_dnd(self, enabled: bool):
        self.cfg["do_not_disturb"] = bool(enabled)
        config.save(self.cfg)
        if self.notifier is not None:
            self.notifier.set_dnd(bool(enabled))
        if self.island is not None:
            self.island.pill.show_dnd(bool(enabled))
            self.island.expanded.set_dnd_state(bool(enabled))

    def _apply_layer(self):
        ls, win = self._layer_shell, self.win
        mon_name = self._pick_monitor()
        ov = (self.cfg.get("monitor_layouts", {}) or {}).get(mon_name, {})
        layer_val = ov.get("layer", self.cfg.get("layer", "top"))
        style_val = ov.get("style", self.cfg.get("island_style", "pill"))
        margin_val = (
            0
            if style_val == "waterdrop"
            else ov.get("margin_top", self.cfg.get("margin_top", 0))
        )
        overlap_val = ov.get("overlap_panel", self.cfg.get("overlap_panel", True))
        layer = ls.Layer.OVERLAY if layer_val == "overlay" else ls.Layer.TOP
        ls.set_layer(win, layer)
        ls.set_anchor(win, ls.Edge.TOP, True)
        ls.set_margin(win, ls.Edge.TOP, int(margin_val))
        # -1 ignores other surfaces' exclusive zones, so the island sits on
        # the panel instead of being pushed below it
        ls.set_exclusive_zone(win, -1 if overlap_val else 0)

    def _pick_monitor(self) -> str:
        want = self.cfg.get("monitor", "")
        display = Gdk.Display.get_default()
        if display is None:
            return want or ""
        monitors = display.get_monitors()
        if monitors is None:
            return want or ""
        if hasattr(monitors, "get_n_items"):
            n = monitors.get_n_items() or 0
            get_item = monitors.get_item
        elif isinstance(monitors, (list, tuple)):
            n = len(monitors)
            get_item = lambda i: monitors[i]
        else:
            return want or ""
        if n <= 0:
            return want or ""
        chosen = None
        if want:
            for i in range(n):
                mon = get_item(i)
                if (
                    mon is not None
                    and getattr(mon, "get_connector", lambda: None)() == want
                ):
                    chosen = mon
                    break
        if chosen is None:
            chosen = get_item(0)
        if chosen is None:
            return want or ""
        self._layer_shell.set_monitor(self.win, chosen)
        self._active_monitor = chosen
        connector = chosen.get_connector() if hasattr(chosen, "get_connector") else None
        self._monitor_name = connector or want or ""
        return self._monitor_name

    def _monitor_geometry(self) -> tuple[float, float, float, float, int]:
        mon = self._active_monitor
        if mon is None:
            display = Gdk.Display.get_default()
            if display is not None:
                monitors = display.get_monitors()
                if (
                    monitors is not None
                    and hasattr(monitors, "get_n_items")
                    and (monitors.get_n_items() or 0) > 0
                ):
                    mon = monitors.get_item(0)
        mx, my, mw, mh = 0.0, 0.0, 1920.0, 1080.0
        if mon is not None and hasattr(mon, "get_geometry"):
            geo = mon.get_geometry()
            mx = float(getattr(geo, "x", 0) or 0)
            my = float(getattr(geo, "y", 0) or 0)
            mw = float(getattr(geo, "width", 1920) or 1920)
            mh = float(getattr(geo, "height", 1080) or 1080)
        ov = (self.cfg.get("monitor_layouts", {}) or {}).get(self._monitor_name, {})
        margin_val = int(ov.get("margin_top", self.cfg.get("margin_top", 0)) or 0)
        return mx, my, mw, mh, margin_val

    def _sync_cursor_watcher(self, active: bool):
        if not active or self.island is None:
            if self.cursor_watcher is not None:
                self.cursor_watcher.stop()
            if self.island is not None:
                self.island.set_global_cursor_active(False)
            return
        if self.cursor_watcher is None:
            self.cursor_watcher = CursorWatcher()
            self.cursor_watcher.connect("moved", self._on_global_cursor_moved)
            self.cursor_watcher.connect(
                "active-changed",
                lambda _w, ok: self.island and self.island.set_global_cursor_active(ok),
            )
        self.cursor_watcher.start()

    def _on_global_cursor_moved(self, _watcher, gx: float, gy: float):
        if self.island is None:
            return
        mx, my, mw, mh, margin_top = self._monitor_geometry()
        nx, ny = CursorWatcher.normalize_cursor(gx, gy, mx, my, mw, mh, margin_top)
        self.island.set_global_cursor(nx, ny)

    def _effective_accent_mode(self) -> str:
        mode = str(self.cfg.get("accent_mode", "manual"))
        if mode in ("auto", "system", "artwork"):
            return mode
        return "artwork" if self.cfg.get("dynamic_artwork_colors") else "manual"

    def _active_accent(self) -> str:
        raw = (
            self._dynamic_accent
            if self._effective_accent_mode() != "manual" and self._dynamic_accent
            else self.cfg.get("accent", "#ff9f0a")
        )
        accent = str(raw)
        probe = Gdk.RGBA()
        return accent if probe.parse(accent) else "#ff9f0a"

    def _load_css(self):
        display = Gdk.Display.get_default()
        if display is None:
            return
        if self._css_provider:
            Gtk.StyleContext.remove_provider_for_display(display, self._css_provider)
        provider = Gtk.CssProvider()
        accent = self._active_accent()
        try:
            opacity = min(1.0, max(0.3, float(self.cfg.get("opacity", 1.0))))
        except (TypeError, ValueError):
            opacity = 1.0
        glass_top = min(1.0, round(opacity * 0.96, 3))
        theme_css = (
            f".island.theme-obsidian {{ background-color: rgba(12, 12, 14, {opacity}); }}\n"
            f".island.theme-liquid-glass {{ background-color: rgba(14, 16, 22, {opacity}); "
            f"background-image: linear-gradient(180deg, rgba(28, 32, 42, {glass_top}), "
            f"rgba(12, 14, 20, {opacity})); }}\n"
            f".island.theme-liquid-glass.island--media {{ background-color: rgba(14, 16, 22, {opacity}); "
            f"background-image: linear-gradient(180deg, alpha(@accent, 0.16), rgba(14, 16, 22, 0) 58%), "
            f"linear-gradient(180deg, rgba(28, 32, 42, {glass_top}), rgba(12, 14, 20, {opacity})); }}\n"
            f".island.theme-frosted {{ background-color: rgba(24, 27, 34, {opacity}); }}\n"
            f".island.theme-midnight {{ background-color: rgba(8, 12, 26, {opacity}); }}\n"
            f".island.theme-amber {{ background-color: rgba(24, 16, 10, {opacity}); }}\n"
        )
        font = (
            str(self.cfg.get("custom_font", "") or "")
            .replace('"', "")
            .replace("\n", "")
            .replace(";", "")
            .strip()
        )
        font_rule = f'.island, .island * {{ font-family: "{font}"; }}\n' if font else ""
        with open(CSS_PATH) as f:
            css = (
                f"@define-color accent {accent};\n"
                f"@define-color island_bg rgba(12, 12, 14, {opacity});\n"
                + font_rule
                + f.read()
                + "\n"
                + theme_css
            )
        provider.load_from_string(css)
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._css_provider = provider

    # -- settings ----------------------------------------------------------

    def open_settings(self):
        from ledgepill.ui.settings import SettingsWindow

        if self.settings_win:
            self.settings_win.present()
            return
        self.settings_win = SettingsWindow(self.cfg, self.apply_config)
        self.settings_win.connect(
            "close-request", lambda *_: (setattr(self, "settings_win", None), False)[1]
        )
        self.settings_win.present()

    def apply_config(self, structural: bool = False):
        """Called by the settings window after each change."""
        if structural:
            self.rebuild()
            return
        self._sync_dynamic_accent(reload_css=False)
        self._load_css()
        if self.weather is not None:
            self.weather.set_city(self.cfg.get("weather_city", ""))
        if self._layer_shell and self.win:
            self._apply_layer()
        if self.island:
            self.island.refresh_style()
            self._apply_fullscreen_state()
            rgb = _hex_rgb(self._active_accent())
            self.island.expanded.ring.accent = rgb
            self.island.expanded.ring.queue_draw()
            self.island.pill.mascot.accent = rgb
            self.island.pill.mascot.queue_draw()
            self.island.pill.halo.accent = rgb
            self.island.pill.halo.queue_draw()
            self.island.on_media_changed()
            self.island.expanded.refresh_battery()
            self.island.expanded._refresh_big_clock()
            if hasattr(self.island.expanded, "_sync_opacity_ui"):
                self.island.expanded._sync_opacity_ui()
            if self.island.expanded.ai_panel is not None:
                self.island.expanded.ai_panel.refresh()
            self.island.pill.show_battery(self.power.percentage, self.power.charging)
            self.island.pill.show_dnd(bool(self.cfg.get("do_not_disturb", False)))
            self.island.pill.render()

    def rebuild(self):
        self._rebuilding = True
        self.hold()
        try:
            self._stop_services()
            if self.win:
                self.win.destroy()
            self.win = self.island = None
            self._build_services()
            self._sync_dynamic_accent(reload_css=False)
            self._load_css()
            self._build_window()
        finally:
            self.release()
            self._rebuilding = False

    # -- services ----------------------------------------------------------

    def _stop_services(self):
        for svc in (
            self.cursor_watcher,
            self.webhook,
            self.cava,
            self.lyrics,
            self.weather,
            self.ollama,
            self.feeds,
            self.calendar,
            self.system,
            self.audio,
            self.brightness,
            self.notifier,
            self.clipboard,
            self.network,
            self.bluetooth,
            self.power,
            self.media,
            self.privacy,
            self.desktop,
            self.kdeconnect,
            self.productivity,
        ):
            if svc is not None and hasattr(svc, "stop"):
                svc.stop()
        self.cursor_watcher = None
        self.system_agent = None

    def _build_services(self):
        mods = self.cfg.get("modules", {})
        self.media = MprisService() if mods.get("music", True) else _NullMedia()
        self.cava = CavaService() if mods.get("music", True) else None
        self.lyrics = (
            LyricsService()
            if mods.get("music", True) and mods.get("lyrics", True)
            else None
        )
        self.power = PowerService()
        self.audio = (
            AudioService()
            if _needs_audio_service(mods, self.cfg.get("ai_system_control", False))
            else None
        )
        self.brightness = BrightnessService() if mods.get("brightness", True) else None
        self.notifier = (
            NotifyService(
                self.cfg.get("notification_blocklist", []),
                self.cfg.get("do_not_disturb", False),
                rules=self.cfg.get("notification_rules", []),
            )
            if mods.get("notifications", True)
            else None
        )
        self.network = NetworkService() if mods.get("network", True) else None
        self.bluetooth = BluetoothService() if mods.get("bluetooth", True) else None
        self.weather = (
            WeatherService(self.cfg.get("weather_city", ""))
            if mods.get("weather", True)
            else None
        )
        self.system = SystemService() if mods.get("system", True) else None
        self.clipboard = ClipboardService() if mods.get("clipboard", True) else None
        self.todos = TodoService() if mods.get("todos", True) else None
        self.calendar = (
            CalendarService(self.cfg.get("calendar_ics", []))
            if mods.get("calendar", True)
            else None
        )
        self.ollama = OllamaService() if mods.get("ollama", True) else None
        self.feeds = (
            FeedService(self.cfg.get("feeds", [])) if mods.get("feeds", True) else None
        )
        self.webhook = (
            WebhookService(
                self._on_webhook_message,
                self.cfg.get("webhook_port", 17334),
                on_agent_event=self._on_agent_event,
            )
            if mods.get("notifications", True)
            else None
        )
        self.privacy = (
            PrivacyService(audio=self.audio) if mods.get("privacy", True) else None
        )
        self.desktop = DesktopService() if mods.get("desktop", True) else None
        self.kdeconnect = KdeConnectService() if mods.get("kdeconnect", True) else None
        if mods.get("assistant", True):
            self.system_agent = SystemAgent(
                self.cfg,
                media=self.media,
                audio=self.audio,
                brightness=self.brightness,
                network=self.network,
                bluetooth=self.bluetooth,
                power=self.power,
                system=self.system,
                desktop=self.desktop,
                weather=self.weather,
                clipboard=self.clipboard,
                notifier=self.notifier,
                ollama=self.ollama,
                todos=self.todos,
                calendar=self.calendar,
                on_dnd_change=self._set_dnd,
                on_apply_config=self.apply_config,
                on_ui_action=self._handle_agent_ui_action,
            )
            self.productivity = ProductivityService(
                self.cfg, ollama=self.ollama, system_agent=self.system_agent
            )
            self.system_agent.on_confirm_request = (
                self.productivity.request_action_confirm
            )
        else:
            self.system_agent = None
            self.productivity = None

    def _handle_agent_ui_action(self, kind: str, args: dict) -> dict:
        if self.island is None:
            return {"ok": False, "error": "UI unavailable"}
        exp = self.island.expanded
        if kind == "timer":
            return exp.control_timer(str(args.get("action", "")), args.get("minutes"))
        if kind == "quick_action":
            act = str(args.get("action", "")).strip()
            if act == "caffeine_on":
                return exp.set_caffeine(True)
            if act == "caffeine_off":
                return exp.set_caffeine(False)
            if act == "screenshot":
                exp._on_screenshot()
                return {"ok": True, "action": "screenshot"}
            return {"ok": False, "error": f"Unknown quick action '{act}'"}
        return {"ok": False, "error": f"Unknown UI action '{kind}'"}

    def _sync_pill_activity(self, *_):
        if self.island is None:
            return
        if self.desktop is not None:
            if self.desktop.build_activity:
                self.island.pill.show_activity(
                    self.desktop.build_activity.removesuffix(" running"),
                    state="working",
                )
                return
            if self.desktop.package_activity:
                self.island.pill.show_activity(
                    self.desktop.package_activity.removesuffix(" running"),
                    state="working",
                )
                return
        self.island.pill.show_activity("", state="idle")

    def _on_agent_event(self, _agent: str, state: str, _message: str):
        clean_state = (state or "active").strip().lower()
        if clean_state == "idle":
            self._sync_pill_activity()
            return
        self._sync_pill_activity()
        if self.island is not None and (clean_state in ("done", "attention", "error")):
            self.island.show_peek(
                self.island.peek.show_agent_event,
                clean_state,
                priority=85 if clean_state in ("attention", "error") else 65,
                key="agent-status",
            )

    def _on_webhook_message(self, app: str, title: str, body: str):
        if self.notifier is not None:
            self.notifier.push_custom(app, title, body, category="webhook")
        elif self.island is not None:
            self.island.show_peek(
                self.island.peek.show_notification,
                app,
                title,
                body,
                "",
                priority=60,
                key="webhook",
            )

    def _sync_dynamic_accent(self, reload_css: bool = True):
        mode = self._effective_accent_mode()
        art_path = (
            self.media.active.art_path
            if self.media and getattr(self.media, "active", None)
            else ""
        )
        if mode == "manual":
            col = None
        elif mode == "artwork":
            col = extract_accent_color(art_path) if art_path else None
        elif mode == "system":
            col = detect_desktop_accent()
        else:  # "auto"
            col = (
                extract_accent_color(art_path) if art_path else None
            ) or detect_desktop_accent()
        if col != self._dynamic_accent:
            self._dynamic_accent = col
            if reload_css:
                self._load_css()
                if self.island is not None:
                    rgb = _hex_rgb(self._active_accent())
                    self.island.expanded.ring.accent = rgb
                    self.island.expanded.ring.queue_draw()
                    self.island.pill.mascot.accent = rgb
                    self.island.pill.mascot.queue_draw()
                    self.island.pill.halo.accent = rgb
                    self.island.pill.halo.queue_draw()

    def _wire_services(self):
        isl = self.island
        isl.pill.show_dnd(bool(self.cfg.get("do_not_disturb", False)))

        if isinstance(self.media, MprisService):

            def on_media(*_):
                isl.on_media_changed()
                self._sync_dynamic_accent()

            self.media.connect("changed", on_media)
            self.media.connect("art-ready", on_media)

        self.power.connect(
            "changed",
            lambda *_: (
                isl.expanded.refresh_battery(),
                isl.pill.show_battery(self.power.percentage, self.power.charging),
            ),
        )
        if self.cfg.get("modules", {}).get("battery", True):
            self.power.connect(
                "plug-event",
                lambda _s, plugged: isl.show_peek(
                    isl.peek.show_battery,
                    self.power.percentage,
                    plugged,
                    priority=80,
                    key="power-plug",
                ),
            )
            self.power.connect(
                "power-alert",
                lambda _s, severity, msg: isl.show_peek(
                    isl.peek.show_power_alert,
                    severity,
                    msg,
                    priority=100,
                    key="power-alert",
                ),
            )
        if self.audio:

            def on_audio_ready(_s, p, m):
                isl.expanded.refresh_volume(p, m)
                isl.expanded.refresh_mic(self.audio.mic_muted)

            def on_volume(_s, p, m):
                if self.cfg.get("modules", {}).get("volume_osd", True):
                    isl.show_peek(isl.peek.show_volume, p, m, priority=40, key="volume")
                isl.expanded.refresh_volume(p, m)

            def on_mic_muted(_s, m):
                isl.expanded.refresh_mic(m)

            self.audio.connect("ready", on_audio_ready)
            self.audio.connect("volume", on_volume)
            self.audio.connect("mic-muted", on_mic_muted)
        if self.brightness:
            self.brightness.connect(
                "brightness",
                lambda _s, p: (
                    isl.show_peek(
                        isl.peek.show_brightness, p, priority=40, key="brightness"
                    ),
                    isl.expanded.refresh_brightness(p),
                ),
            )
            isl.expanded.refresh_brightness(self.brightness.percent)
        if self.notifier:
            self.notifier.connect(
                "notified",
                lambda _s, app, summary, body, icon: isl.show_peek(
                    isl.peek.show_notification,
                    app,
                    summary,
                    body,
                    icon,
                    priority=60,
                ),
            )
            self.notifier.connect(
                "call",
                lambda _s, app, summary, body, _icon="": isl.show_peek(
                    isl.peek.show_call,
                    app,
                    summary,
                    body,
                    priority=100,
                    key="call",
                ),
            )
        if self.privacy:

            def on_priv(*_):
                isl.pill.show_privacy(
                    self.privacy.mic_active,
                    self.privacy.camera_active,
                    self.privacy.screen_recording,
                    self.privacy.screen_sharing,
                )
                isl.expanded.refresh_privacy(
                    self.privacy.mic_active,
                    self.privacy.camera_active,
                    self.privacy.screen_recording,
                    self.privacy.screen_sharing,
                )

            self.privacy.connect("changed", on_priv)
            self.privacy.connect(
                "alert",
                lambda _s, kind, label: isl.show_peek(
                    isl.peek.show_privacy, kind, label, priority=90, key="privacy"
                ),
            )
            self.privacy.start()
            on_priv()
        if self.desktop:
            self.desktop.connect(
                "layout-changed",
                lambda _s, layout: isl.show_peek(
                    isl.peek.show_keyboard_layout, layout, priority=30, key="layout"
                ),
            )
            self.desktop.connect(
                "fullscreen-changed",
                lambda _s, fs: (
                    setattr(self, "_fullscreen_active", bool(fs)),
                    self._apply_fullscreen_state(),
                ),
            )
            self.desktop.connect("activity-changed", self._sync_pill_activity)
            self.desktop.start()
        if self.kdeconnect:
            self.kdeconnect.connect(
                "changed", lambda *_: isl.expanded.refresh_kdeconnect()
            )
        if self.productivity:
            self.productivity.start()
        self._sync_pill_activity()
        if self.webhook:
            self.webhook.start()
        if self.calendar:
            self.calendar.connect(
                "upcoming-alert",
                lambda _s, title, mins, loc: isl.show_peek(
                    isl.peek.show_calendar_alert,
                    title,
                    mins,
                    loc,
                    priority=75,
                    key=f"cal:{title}",
                ),
            )
            self.calendar.start()
        if self.ollama is not None:
            self.ollama.refresh_now()
        if self.network:
            self.network.connect(
                "synced",
                lambda *_: isl.expanded.refresh_network(
                    self.network.name,
                    self.network.kind,
                    self.network.connected,
                    self.network.wifi_signal,
                ),
            )
            self.network.connect(
                "changed",
                lambda _s, name, kind, up: (
                    isl.show_peek(
                        isl.peek.show_network,
                        name,
                        kind,
                        up,
                        priority=70,
                        key="network",
                    ),
                    isl.expanded.refresh_network(
                        name, kind, up, self.network.wifi_signal
                    ),
                ),
            )
            self.network.connect(
                "wifi-quality",
                lambda _s, sig: isl.expanded.refresh_network(
                    self.network.name,
                    self.network.kind,
                    self.network.connected,
                    sig,
                ),
            )
            self.network.connect(
                "wifi-enabled",
                lambda _s, _enabled: isl.expanded.refresh_network(
                    self.network.name,
                    self.network.kind,
                    self.network.connected,
                    self.network.wifi_signal,
                ),
            )
            isl.expanded.refresh_network(
                self.network.name,
                self.network.kind,
                self.network.connected,
                self.network.wifi_signal,
            )
        else:
            isl.expanded.refresh_network("", "", False)
        if self.bluetooth:
            isl.expanded.set_bluetooth_service(self.bluetooth)
            self.bluetooth.connect(
                "synced",
                lambda *_: (
                    isl.expanded.refresh_bluetooth(
                        self.bluetooth.device,
                        self.bluetooth.connected,
                        self.bluetooth.battery,
                    ),
                    isl.pill.show_bluetooth(
                        self.bluetooth.device,
                        self.bluetooth.connected,
                        self.bluetooth.battery,
                    ),
                ),
            )
            self.bluetooth.connect(
                "changed",
                lambda _s, dev, up, batt: (
                    isl.show_peek(
                        isl.peek.show_bluetooth,
                        dev,
                        up,
                        priority=70,
                        key="bluetooth",
                    ),
                    isl.expanded.refresh_bluetooth(dev, up, batt),
                    isl.pill.show_bluetooth(dev, up, batt),
                ),
            )
            isl.expanded.refresh_bluetooth(
                self.bluetooth.device, self.bluetooth.connected, self.bluetooth.battery
            )
            isl.pill.show_bluetooth(
                self.bluetooth.device, self.bluetooth.connected, self.bluetooth.battery
            )
        else:
            isl.expanded.refresh_bluetooth("", False, -1)


class _NullMedia:
    """Stand-in when the music module is disabled."""

    active = None

    def connect(self, *a): ...
    def play_pause(self): ...
    def next(self): ...
    def previous(self): ...
    def seek_to(self, *a): ...
    def get_position(self, *a): ...
    def stop(self): ...
    def list_players(self):
        return []

    def select_player(self, *a): ...
    def get_playlists(self, callback=None, *a):
        if callable(callback):
            callback([])
        return []

    def activate_playlist(self, *a): ...
    def get_tracks(self, callback=None, *a):
        if callable(callback):
            callback([])
        return []

    def go_to_track(self, *a): ...


def main() -> int:
    args = sys.argv[1:]
    if "-v" in args or "--version" in args:
        print(f"ledge-pill {__version__}")
        return 0

    if not ("-h" in args or "--help" in args or "--help-all" in args):
        _install_gdk_log_filter()
        Gtk.init_check()
        if Gdk.Display.get_default() is None:
            print(
                "ledge-pill: no graphical display detected; launch this app from a "
                "desktop session (Wayland or X11).",
                file=sys.stderr,
            )
            return 1

    app = LedgePill()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
