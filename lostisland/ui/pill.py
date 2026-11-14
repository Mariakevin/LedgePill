"""The collapsed pill — what the island looks like 99% of the time.

Faces, cycled with a click or a horizontal swipe (config `pill_faces`):
  auto       clock when idle, art + EQ + song title when music plays
  status     clock + battery, always
  lyrics     the current synced lyric line while music plays
  title      just the song title (never the artist)
  compact    just art + EQ
  clock      only the time
  battery    only the battery
  weather    temperature + condition icon
  bluetooth  connected device + its battery
  mascot     clock + cursor-tracking animated mascot eyes
"""

from __future__ import annotations

import os
import time

from gi.repository import GLib, Gtk, Pango

from lostisland import config
from lostisland.ui.draw import (
    BatteryRing,
    EqBars,
    MascotEyes,
    bluetooth_icon,
    pick_icon,
    weather_icon,
)

FACES = [
    "auto",
    "status",
    "lyrics",
    "title",
    "compact",
    "clock",
    "battery",
    "weather",
    "bluetooth",
    "mascot",
]


class Pill(Gtk.Box):
    def __init__(
        self,
        cfg: dict,
        media=None,
        cava=None,
        weather=None,
        lyrics=None,
        on_mascot_active=None,
    ):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.add_css_class("pill")
        self.cfg = cfg
        self.media = media
        self.cava = cava
        self.weather = weather
        self.lyrics = lyrics
        self._on_mascot_active = on_mascot_active
        self._mascot_active = False
        self._minute_timer = 0
        self._lyric_timer = 0
        self._music = None  # (title, art_path, playing) while music is active
        self._batt = (-1.0, False)
        self._bt = ("", False, -1)
        self._dnd = bool(cfg.get("do_not_disturb", False))
        self._privacy = (False, False, False, False)
        self._rendering = False

        # music side
        self.art = Gtk.Image()
        self.art.set_pixel_size(20)
        self.art.add_css_class("pill-art")
        self.title = Gtk.Label()
        self.title.add_css_class("pill-title")
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.title.set_max_width_chars(32)
        self.eq = EqBars()
        self.eq.connect("map", lambda *_: self._sync_cava())
        self.eq.connect("unmap", lambda *_: self._sync_cava())
        if self.cava is not None:
            self.cava.connect("levels", self._on_levels)
            self.cava.connect("stopped", lambda *_: self.eq.set_external(False))

        # lyric line
        self.lyric = Gtk.Label()
        self.lyric.add_css_class("pill-lyric")
        self.lyric.set_ellipsize(Pango.EllipsizeMode.END)
        self.lyric.set_width_chars(22)
        self.lyric.set_max_width_chars(30)
        if self.lyrics is not None:
            self.lyrics.connect("ready", lambda *_: self._lyric_tick())

        if self.weather is not None:
            self.weather.connect(
                "changed",
                lambda *_: (
                    (self.face == "weather" or self._is_dashboard()) and self.render()
                ),
            )

        # idle side
        self.dot = Gtk.Label(label="●")
        self.dot.add_css_class("pill-dot")
        self.mascot = MascotEyes()
        self.clock = Gtk.Label()
        self.clock.add_css_class("pill-clock")
        self.halo = BatteryRing(accent=(1.0, 0.62, 0.04), size=34)
        self.halo.add_css_class("pill-halo")
        self.halo.set_visible(False)
        self.clock_overlay = Gtk.Overlay()
        self.clock_overlay.set_child(self.halo)
        self.clock_overlay.add_overlay(self.clock)
        self.clock.set_halign(Gtk.Align.CENTER)
        self.clock.set_valign(Gtk.Align.CENTER)
        self.clock_overlay.set_valign(Gtk.Align.CENTER)

        # dashboard compact extra chip
        self.dash_chip = Gtk.Label()
        self.dash_chip.add_css_class("pill-dash")
        self.dash_chip.set_visible(False)

        # generic icon face (weather / bluetooth)
        self.face_icon = Gtk.Image()
        self.face_icon.set_pixel_size(16)
        self.face_label = Gtk.Label()
        self.face_label.add_css_class("pill-title")
        self.face_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.face_label.set_max_width_chars(24)

        # shared badges (DND, Privacy, Timer, Battery)
        self.dnd_icon = Gtk.Image.new_from_icon_name("notifications-disabled-symbolic")
        self.dnd_icon.set_pixel_size(13)
        self.dnd_icon.add_css_class("pill-dnd")
        self.dnd_icon.set_visible(self._dnd)

        self.priv_dot = Gtk.Label(label="●")
        self.priv_dot.add_css_class("pill-priv")
        self.priv_dot.set_visible(False)

        self.timer_chip = Gtk.Label()
        self.timer_chip.add_css_class("timer-chip")
        self.timer_chip.set_visible(False)

        # Local build/package activity badge
        self.activity_chip = Gtk.Label()
        self.activity_chip.add_css_class("pill-activity")
        self.activity_chip.set_ellipsize(Pango.EllipsizeMode.END)
        self.activity_chip.set_max_width_chars(22)
        self.activity_chip.set_visible(False)

        # small battery readout
        self.batt_icon = Gtk.Image()
        self.batt_icon.set_pixel_size(13)
        self.batt_icon.add_css_class("pill-batt")
        self.batt_label = Gtk.Label()
        self.batt_label.add_css_class("pill-batt")

        # queued peek indicator
        self.queue_badge = Gtk.Label()
        self.queue_badge.add_css_class("pill-queue-badge")
        self.queue_badge.set_visible(False)
        self.queue_badge.set_tooltip_text("More alerts queued")

        for widget in (
            self.art,
            self.title,
            self.eq,
            self.lyric,
            self.face_icon,
            self.face_label,
            self.dot,
            self.mascot,
            self.clock_overlay,
            self.dash_chip,
            self.dnd_icon,
            self.priv_dot,
            self.timer_chip,
            self.activity_chip,
            self.batt_icon,
            self.batt_label,
            self.queue_badge,
        ):
            self.append(widget)

        self.connect("map", lambda *_: (self._start_clock(), self.render()))
        self.connect(
            "unmap",
            lambda *_: (
                self._stop_clock(),
                self._sync_cava(),
                self._sync_lyric_timer(),
            ),
        )
        self.mascot.connect("map", lambda *_: self._sync_mascot_active())
        self.mascot.connect("unmap", lambda *_: self._sync_mascot_active())
        self.mascot.connect("notify::visible", lambda *_: self._sync_mascot_active())
        self.render()

    def _sync_mascot_active(self):
        active = bool(self.mascot.get_mapped() and self.mascot.get_visible())
        if active != self._mascot_active:
            self._mascot_active = active
            if self._on_mascot_active is not None:
                self._on_mascot_active(active)

    def _is_dashboard(self) -> bool:
        return self.cfg.get("island_style", "pill") == "dashboard"

    def set_orientation_style(self, style: str):
        vertical = style == "vertical"
        self.set_orientation(
            Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL
        )
        self.set_spacing(8 if style == "waterdrop" else 10)
        self.title.set_max_width_chars(22 if style == "waterdrop" else 32)
        self.render()

    def look_at(self, nx: float, ny: float):
        if self.mascot.get_visible():
            self.mascot.look_at(nx, ny)

    def trigger_wake(self):
        """Play the assistant wake pulse on the pill's mascot / indicators."""
        self.mascot.trigger_wake()

    def set_awake(self, awake: bool):
        self.mascot.set_awake(awake)

    # -- face state ---------------------------------------------------------

    @property
    def face(self) -> str:
        return self.cfg.get("pill_face", "auto")

    def enabled_faces(self) -> list[str]:
        faces = [f for f in self.cfg.get("pill_faces", FACES) if f in FACES]
        return faces or ["auto"]

    def _has_content(self, face: str) -> bool:
        if face in ("lyrics", "title", "compact"):
            return self._music is not None
        if face == "battery":
            return (
                bool(self.cfg.get("modules", {}).get("battery", True))
                and self._batt[0] >= 0
            )
        if face == "weather":
            return self.weather is not None
        if face == "bluetooth":
            return bool(self._bt[1])
        return True

    def cycle(self, step: int = 1):
        faces = self.enabled_faces()
        active = [f for f in faces if self._has_content(f)] or faces
        try:
            i = active.index(self.face)
            nxt = active[(i + step) % len(active)]
        except ValueError:
            nxt = active[0] if step >= 0 else active[-1]
        self.cfg["pill_face"] = nxt
        config.save(self.cfg)
        self.render()

    # -- rendering ----------------------------------------------------------

    def _hide_all(self):
        for w in (
            self.art,
            self.title,
            self.eq,
            self.lyric,
            self.face_icon,
            self.face_label,
            self.dot,
            self.mascot,
            self.clock_overlay,
            self.clock,
            self.dash_chip,
            self.batt_icon,
            self.batt_label,
        ):
            w.set_visible(False)

    def render(self):
        if self._rendering:
            return
        self._rendering = True
        try:
            self._render_body()
        finally:
            self._rendering = False

    def _render_body(self):
        face = self.face
        if face not in self.enabled_faces():
            face = self.cfg["pill_face"] = self.enabled_faces()[0]
        self._hide_all()
        music = self._music
        playing = bool(music and music[2])
        batt_enabled = self.cfg.get("modules", {}).get("battery", True)
        use_mascot = bool(self.cfg.get("mascot", False))

        if face == "auto" and music:
            self._show_music_row(title=True)
            if self._is_dashboard():
                self._show_clock(dot=False, mascot=False)
                self._render_battery(force=batt_enabled and self._batt[0] >= 0)
        elif face == "auto":
            self._show_clock(dot=not use_mascot, mascot=use_mascot)
            self._render_battery(
                force=self._is_dashboard() and batt_enabled and self._batt[0] >= 0
            )
            if self._is_dashboard():
                self._render_dashboard_chip()
        elif face == "mascot":
            self._show_clock(dot=False, mascot=True)
            self._render_battery()
        elif face == "status":
            self._show_clock(dot=False, mascot=False)
            self._render_battery(force=batt_enabled and self._batt[0] >= 0)
        elif face == "lyrics" and music:
            self.lyric.set_visible(True)
            self._lyric_tick()
        elif face == "title" and music:
            self.title.set_label(music[0] or "…")
            self.title.set_visible(True)
        elif face == "compact" and music:
            self._show_music_row(title=False)
        elif face == "battery" and batt_enabled and self._batt[0] >= 0:
            self._render_battery(force=True)
        elif face == "weather" and self.weather is not None:
            is_stale = getattr(self.weather, "is_stale", None)
            if not self.weather.temp or (callable(is_stale) and is_stale()):
                self.weather.request()
            if self.weather.temp:
                self.face_icon.set_from_icon_name(weather_icon(self, self.weather.icon))
                self.face_label.set_label(self.weather.temp)
                self.face_icon.set_visible(True)
                self.face_label.set_visible(True)
            else:
                self._show_clock(dot=True, mascot=False)
        elif face == "bluetooth" and self._bt[1]:
            dev, _up, batt = self._bt
            self.face_icon.set_from_icon_name(bluetooth_icon(self))
            self.face_label.set_label(dev if batt < 0 else f"{dev} · {batt}%")
            self.face_icon.set_visible(True)
            self.face_label.set_visible(True)
        else:
            # face has nothing to show right now: fall back to the clock
            self._show_clock(dot=not use_mascot, mascot=use_mascot)
            self._render_battery()

        self.dnd_icon.set_visible(self._dnd)
        self.eq.set_playing(playing and self.eq.get_visible())
        self._sync_cava()
        self._sync_lyric_timer()

    def _render_dashboard_chip(self):
        parts = []
        if self.weather is not None:
            if not self.weather.temp:
                self.weather.request()
            else:
                parts.append(self.weather.temp)
        if parts:
            self.dash_chip.set_label(" · ".join(parts))
            self.dash_chip.set_visible(True)

    def _show_music_row(self, title: bool):
        music = self._music
        if music and music[1] and os.path.exists(music[1]):
            self.art.set_from_file(music[1])
            self.art.set_visible(True)
        if title:
            self.title.set_label(music[0] or "…")
            self.title.set_visible(True)
        self.eq.set_visible(True)

    def _show_clock(self, dot: bool, mascot: bool = False):
        if not self.cfg.get("idle_clock", True) and self.face == "auto":
            return
        self.dot.set_visible(dot and not mascot)
        self.mascot.set_visible(mascot)
        self.clock_overlay.set_visible(True)
        self._update_halo(self._halo_allowed())
        self._refresh_clock()

    def _halo_allowed(self) -> bool:
        return (
            bool(self.cfg.get("status_ring", True))
            and bool(self.cfg.get("modules", {}).get("battery", True))
            and self._batt[0] >= 0
        )

    def _update_halo(self, visible: bool):
        percent, charging = self._batt
        if percent < 0:
            self.halo.set_visible(False)
            return
        self.halo.update(percent, charging)
        self.halo.set_visible(visible)

    # -- music / indicators ------------------------------------------------

    def show_music(self, title: str, art_path: str, playing: bool):
        self._music = (title, art_path, playing)
        self.render()

    def show_idle(self):
        self._music = None
        self.render()

    def show_timer_chip(self, text: str | None):
        self.timer_chip.set_visible(text is not None)
        if text is not None:
            self.timer_chip.set_label(text)

    def show_activity(self, label: str, state: str = "idle"):
        """Show local build/package activity and sync mascot eyes mood."""
        txt = (label or "").strip()
        self.activity_chip.set_visible(bool(txt))
        self.activity_chip.remove_css_class("pill-activity--attention")
        if txt:
            prefix = "⚠ " if state == "attention" else "✦ "
            self.activity_chip.set_label(f"{prefix}{txt}")
            self.activity_chip.set_tooltip_text(
                f"Detected local process: {txt}"
                if state == "active"
                else f"{txt} needs attention"
                if state == "attention"
                else f"{txt} is running"
            )
            if state == "attention":
                self.activity_chip.add_css_class("pill-activity--attention")
        else:
            self.activity_chip.set_tooltip_text(None)
        if hasattr(self.mascot, "set_mood"):
            mood = (
                "attention"
                if state == "attention"
                else ("working" if state == "working" else "normal")
            )
            self.mascot.set_mood(mood)

    def show_dnd(self, enabled: bool):
        self._dnd = bool(enabled)
        self.dnd_icon.set_visible(self._dnd)

    def show_queue_count(self, count: int):
        if count <= 0:
            self.queue_badge.set_visible(False)
            return
        self.queue_badge.set_label(f"+{count}")
        self.queue_badge.set_visible(True)

    def show_privacy(self, mic: bool, cam: bool, rec: bool, share: bool):
        self._privacy = (mic, cam, rec, share)
        active = mic or cam or rec or share
        self.priv_dot.set_visible(active)
        for cls in ("pill-priv--mic", "pill-priv--cam", "pill-priv--rec"):
            self.priv_dot.remove_css_class(cls)
        if rec or share:
            self.priv_dot.add_css_class("pill-priv--rec")
            self.priv_dot.set_tooltip_text("Screen recording/sharing active")
        elif cam:
            self.priv_dot.add_css_class("pill-priv--cam")
            self.priv_dot.set_tooltip_text("Camera active")
        elif mic:
            self.priv_dot.add_css_class("pill-priv--mic")
            self.priv_dot.set_tooltip_text("Microphone active")

    # -- battery / bluetooth ------------------------------------------------

    def show_battery(self, percent: float, charging: bool):
        self._batt = (percent, charging)
        self._update_halo(self.clock_overlay.get_visible() and self._halo_allowed())
        if self.face in ("battery", "status") or self._is_dashboard():
            self.render()
        else:
            self._render_battery()

    def show_bluetooth(self, device: str, connected: bool, battery: int):
        self._bt = (device, connected, battery)
        if self.face == "bluetooth":
            self.render()

    def _render_battery(self, force: bool = False):
        if not self.cfg.get("modules", {}).get("battery", True):
            self.batt_icon.set_visible(False)
            self.batt_label.set_visible(False)
            self._update_halo(False)
            return
        percent, charging = self._batt
        show = force or (
            self.cfg.get("pill_battery", True)
            and percent >= 0
            and (charging or percent <= 30)
        )
        self.batt_icon.set_visible(show)
        self.batt_label.set_visible(show)
        # Use one battery signal at a time in the collapsed pill: the ring
        # carries the level until a numeric readout takes its place.
        self._update_halo(
            self.clock_overlay.get_visible() and self._halo_allowed() and not show
        )
        if show:
            if charging:
                icon = pick_icon(
                    self,
                    "battery-charging-symbolic",
                    "battery-full-charging-symbolic",
                    "battery-good-charging-symbolic",
                    "battery-symbolic",
                )
            elif percent <= 30:
                icon = pick_icon(
                    self,
                    "battery-low-symbolic",
                    "battery-caution-symbolic",
                    "battery-empty-symbolic",
                    "battery-symbolic",
                )
            else:
                icon = pick_icon(self, "battery-good-symbolic", "battery-symbolic")
            self.batt_icon.set_from_icon_name(icon)
            self.batt_label.set_label(f"{percent:.0f}%")

    # -- lyrics -------------------------------------------------------------

    def _sync_lyric_timer(self):
        want = (
            self.face == "lyrics"
            and self._music
            and self._music[2]
            and self.get_mapped()
            and self.lyrics is not None
            and self.media is not None
        )
        if want and not self._lyric_timer:
            self._lyric_timer = GLib.timeout_add_seconds(1, self._lyric_tick)
        elif not want and self._lyric_timer:
            GLib.source_remove(self._lyric_timer)
            self._lyric_timer = 0

    def _lyric_tick(self):
        if self.face != "lyrics" or self.lyrics is None or self.media is None:
            return True
        if not self.lyrics.times:
            title = self._music[0] if self._music else ""
            self.lyric.set_label(f"♪ {title}" if title else "♪")
            return True
        self.media.get_position(self._on_lyric_pos)
        return True

    def _on_lyric_pos(self, pos_us: int):
        line = self.lyrics.line_at(pos_us / 1_000_000)
        self.lyric.set_label(line or "♪")

    # -- cava ---------------------------------------------------------------

    def _on_levels(self, _svc, levels):
        # only trust cava once data actually flows
        if not self.eq._external:
            self.eq.set_external(True)
        self.eq.feed(levels)

    def _sync_cava(self):
        if self.cava is None or not self.cava.available:
            return
        playing = bool(self._music and self._music[2])
        want = playing and self.eq.get_visible() and self.eq.get_mapped()
        if want:
            self.cava.start()
        else:
            self.cava.stop()
            self.eq.set_external(False)

    # -- clock, ticking once per minute, aligned to :00 ----------------------

    def _start_clock(self):
        self._refresh_clock()
        self._arm_minute()

    def _stop_clock(self):
        if self._minute_timer:
            GLib.source_remove(self._minute_timer)
            self._minute_timer = 0

    def _arm_minute(self):
        self._stop_clock()
        delay = max(1, 60 - time.localtime().tm_sec)
        self._minute_timer = GLib.timeout_add_seconds(delay, self._on_minute)

    def _on_minute(self):
        self._refresh_clock()
        self._arm_minute()
        return False

    def _refresh_clock(self):
        fmt = "%H:%M" if self.cfg.get("clock_24h", True) else "%I:%M"
        self.clock.set_label(time.strftime(fmt))
