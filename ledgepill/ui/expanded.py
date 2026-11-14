"""The expanded card — header, session/power bar, player, tiles, and unified panels.

Timers live only while the card is mapped: 1 Hz seek tick, 3 s system/desktop
samplers, feed/weather requests. Unmapping tears polling down and resets
collapsible drawers.
"""

from __future__ import annotations

import os
import time

from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

from ledgepill import config
from ledgepill.services.desktop import DesktopService
from ledgepill.ui.draw import (
    BatteryRing,
    brightness_icon,
    pick_icon,
    volume_icon,
    weather_icon,
)
from ledgepill.ui.expanded_lists import (
    _chip,
    _ExpandedListsMixin,
    _fmt_us,
    _hex_rgb,
    _label,
    _media_btn,
    _set_chip,
    classify_clipboard_text,
)
from ledgepill.ui.panels import (
    AiDevPanel,
    CameraMirror,
    ConnectivityPanel,
    _icon_btn,
    _section_card,
    _section_title,
)

__all__ = [
    "POMODORO",
    "SCREENSHOT_TOOLS",
    "Expanded",
    "_hex_rgb",
    "classify_clipboard_text",
]

POMODORO = 25 * 60
SCREENSHOT_TOOLS = (
    ["spectacle", "-r"],
    ["flameshot", "gui"],
    ["gnome-screenshot", "-i"],
    ["xfce4-screenshooter", "-r"],
)


class Expanded(_ExpandedListsMixin, Gtk.Box):
    def __init__(
        self,
        cfg: dict,
        media,
        power,
        on_timer_change=None,
        weather=None,
        system=None,
        brightness=None,
        notifier=None,
        clipboard=None,
        audio=None,
        network=None,
        ollama=None,
        feeds=None,
        privacy=None,
        desktop=None,
        kdeconnect=None,
        productivity=None,
        lyrics=None,
        todos=None,
        calendar=None,
        on_dnd_change=None,
        on_settings=None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add_css_class("expanded")
        self.cfg = cfg
        self.media = media
        self.power = power
        self.weather = weather
        self.system = system
        self.brightness = brightness
        self.notifier = notifier
        self.clipboard = clipboard
        self.audio = audio
        self.network = network
        self.ollama = ollama
        self.feeds = feeds
        self.privacy = privacy
        self.desktop = desktop
        self.kde = kdeconnect
        self.prod = productivity
        self.lyrics = lyrics
        self.todos = todos
        self.calendar = calendar
        self.on_timer_change = on_timer_change
        self.on_dnd_change = on_dnd_change
        self.on_settings = on_settings
        self._pos_timer = 0
        self._pop_gen = 0
        self._seek_dragging = False
        self._clip_feedback_timer = 0
        self._clip_snap = None
        self._clip_show_all = False
        self._clip_open = False
        self._dock_syncing = False
        self._notif_snap = None
        self._position_us = 0
        self._caffeine: Gio.Subprocess | None = None
        self._power_armed = ""
        self._power_arm_labels: dict[str, Gtk.Label] = {}
        self._power_disarm_timer = 0
        self.connectivity_panel: ConnectivityPanel | None = None

        self._build_header()
        self._build_player()
        self._build_bento_controls()
        self._build_sections()
        self._build_footer()

        self.connect("map", lambda *_: self._on_map())
        self.connect("unmap", lambda *_: self._on_unmap())
        self.connect("destroy", lambda *_: self._on_destroy())

    # -- top utility bar: settings, DND, privacy + session/power -----------

    def _build_header(self):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        gear = Gtk.Button()
        gear.set_icon_name("emblem-system-symbolic")
        gear.add_css_class("chip-btn")
        gear.set_tooltip_text("Settings")
        gear.connect("clicked", lambda *_: self.on_settings and self.on_settings())

        self.dnd_btn = Gtk.ToggleButton()
        self.dnd_btn.set_icon_name("notifications-disabled-symbolic")
        self.dnd_btn.add_css_class("chip-btn")
        self.dnd_btn.set_tooltip_text("Do Not Disturb")
        self.dnd_btn.set_active(bool(self.cfg.get("do_not_disturb", False)))
        self.dnd_btn.connect("toggled", self._on_dnd_toggle)

        self.priv_chip = _chip("camera-web-symbolic")
        self.priv_chip.add_css_class("chip--privacy")
        self.priv_chip.set_visible(False)

        self.battery_tile = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self.battery_tile.add_css_class("chip")
        self.battery_tile.add_css_class("header-batt-chip")
        self.ring = BatteryRing(
            accent=_hex_rgb(self.cfg.get("accent", "#ff9f0a")), size=16
        )
        self.ring.set_valign(Gtk.Align.CENTER)
        self.batt_label = Gtk.Label(label="—%", xalign=0)
        self.batt_label.add_css_class("header-batt-lbl")
        self.battery_tile.append(self.ring)
        self.battery_tile.append(self.batt_label)
        if not self.cfg.get("modules", {}).get("battery", True):
            self.battery_tile.set_visible(False)

        spacer = Gtk.Box(hexpand=True)

        self.power_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.power_bar.add_css_class("power-bar")
        specs = (
            ("lock", "Lock", "system-lock-screen-symbolic", False),
            ("suspend", "Sleep", "system-suspend-symbolic", False),
            ("reboot", "Reboot", "system-reboot-symbolic", True),
            ("poweroff", "Power", "system-shutdown-symbolic", True),
        )
        for action, label, icon_name, confirm in specs:
            btn = Gtk.Button()
            btn.add_css_class("power-btn")
            if confirm:
                btn.add_css_class("danger-btn")
            btn.set_tooltip_text(
                f"{label} (click twice to confirm)" if confirm else label
            )
            inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            inner.set_halign(Gtk.Align.CENTER)
            img = Gtk.Image.new_from_icon_name(icon_name)
            img.set_pixel_size(12)
            lbl = Gtk.Label(label=label)
            inner.append(img)
            inner.append(lbl)
            btn.set_child(inner)
            self._power_arm_labels[action] = lbl
            btn.connect(
                "clicked",
                lambda _b, a=action, t=label, c=confirm: self._on_power_click(a, t, c),
            )
            self.power_bar.append(btn)

        for w in (
            gear,
            self.dnd_btn,
        ):
            w.set_valign(Gtk.Align.CENTER)
            row.append(w)

        mods = self.cfg.get("modules", {})
        self.ask_ai_btn = Gtk.Button()
        self.ask_ai_btn.set_icon_name(
            pick_icon(
                self,
                "starred-symbolic",
                "applications-science-symbolic",
                "emblem-system-symbolic",
            )
        )
        self.ask_ai_btn.add_css_class("chip-btn")
        self.ask_ai_btn.add_css_class("chip-btn--accent")
        self.ask_ai_btn.set_tooltip_text("Ask AI Assistant")
        self.ask_ai_btn.set_visible(
            bool(mods.get("assistant", True) or mods.get("ollama", True))
        )
        self.ask_ai_btn.connect("clicked", lambda *_: self.open_dock_panel("ai"))

        self.mirror_popover = CameraMirror()
        self.mirror_btn = Gtk.MenuButton()
        self.mirror_btn.set_icon_name("camera-web-symbolic")
        self.mirror_btn.add_css_class("chip-btn")
        self.mirror_btn.set_tooltip_text("Pre-call Camera Mirror")
        self.mirror_btn.set_popover(self.mirror_popover)
        self.mirror_btn.set_visible(bool(mods.get("camera_mirror", True)))

        for w in (
            self.ask_ai_btn,
            self.mirror_btn,
            self.priv_chip,
            self.battery_tile,
            spacer,
            self.power_bar,
        ):
            w.set_valign(Gtk.Align.CENTER)
            row.append(w)
        self.append(row)

    def _disarm_power(self):
        if self._power_disarm_timer:
            GLib.source_remove(self._power_disarm_timer)
            self._power_disarm_timer = 0
        self._power_armed = ""
        defaults = {
            "lock": "Lock",
            "suspend": "Sleep",
            "reboot": "Reboot",
            "poweroff": "Power",
        }
        for act, lbl in self._power_arm_labels.items():
            lbl.set_label(defaults.get(act, act.title()))

    def _run_power_action(self, action: str):
        if self.desktop is not None:
            self.desktop.power_action(action)
        else:
            DesktopService.power_action(action)

    def _on_power_click(self, action: str, label: str, confirm: bool):
        if confirm and self._power_armed != action:
            self._disarm_power()
            self._power_armed = action
            if action in self._power_arm_labels:
                self._power_arm_labels[action].set_label(f"{label}?")
            self._power_disarm_timer = GLib.timeout_add_seconds(
                4, lambda: (self._disarm_power(), False)[1]
            )
            return
        self._disarm_power()
        self._run_power_action(action)

    def _on_dnd_toggle(self, btn):
        enabled = btn.get_active()
        self.cfg["do_not_disturb"] = enabled
        config.save(self.cfg)
        if self.notifier is not None:
            self.notifier.set_dnd(enabled)
        if self.on_dnd_change:
            self.on_dnd_change(enabled)

    def set_dnd_state(self, enabled: bool):
        if hasattr(self, "dnd_btn") and self.dnd_btn.get_active() != enabled:
            self.dnd_btn.handler_block_by_func(self._on_dnd_toggle)
            self.dnd_btn.set_active(enabled)
            self.dnd_btn.handler_unblock_by_func(self._on_dnd_toggle)

    # -- media player / idle clock hero card -------------------------------

    def _build_player(self):
        self.hero_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.hero_card.add_css_class("bento-card")
        self.hero_card.add_css_class("bento-card--tight")

        self.player_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        art_frame = Gtk.Box()
        art_frame.add_css_class("art-frame")
        art_frame.set_overflow(Gtk.Overflow.HIDDEN)
        self.art = Gtk.Picture()
        self.art.set_size_request(58, 58)
        self.art.set_content_fit(Gtk.ContentFit.COVER)
        art_frame.append(self.art)

        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        meta.set_valign(Gtk.Align.CENTER)
        meta.set_hexpand(True)
        self.title = _label("track-title", 22)
        self.artist = _label("track-artist", 26)
        self.album = _label("track-album", 28)
        for w in (self.title, self.artist, self.album):
            meta.append(w)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        controls.set_valign(Gtk.Align.CENTER)
        self.btn_prev = _media_btn("media-skip-backward-symbolic")
        self.btn_play = _media_btn("media-playback-start-symbolic", main=True)
        self.btn_next = _media_btn("media-skip-forward-symbolic")
        self.btn_prev.connect("clicked", lambda *_: self.media.previous())
        self.btn_play.connect("clicked", lambda *_: self.media.play_pause())
        self.btn_next.connect("clicked", lambda *_: self.media.next())

        self.player_pick_btn = Gtk.MenuButton()
        self.player_pick_btn.set_icon_name("view-list-symbolic")
        self.player_pick_btn.add_css_class("chip-btn")
        self.player_pick_btn.set_valign(Gtk.Align.CENTER)
        self.player_pick_btn.set_tooltip_text("Players, tracks & playlists")
        self.player_popover = Gtk.Popover()
        self.player_pop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.player_popover.set_child(self.player_pop_box)
        self.player_pick_btn.set_popover(self.player_popover)
        self.player_pick_btn.connect(
            "notify::active",
            lambda b, _p: b.get_active() and self._populate_player_menu(),
        )

        for b in (self.btn_prev, self.btn_play, self.btn_next, self.player_pick_btn):
            controls.append(b)

        head.append(art_frame)
        head.append(meta)
        head.append(controls)

        seek_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.time_now = Gtk.Label(label="0:00")
        self.time_now.add_css_class("seek-time")
        self.seek = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.seek.add_css_class("seek")
        self.seek.set_hexpand(True)
        self.seek.set_draw_value(False)
        self.time_total = Gtk.Label(label="0:00")
        self.time_total.add_css_class("seek-time")
        seek_box.append(self.time_now)
        seek_box.append(self.seek)
        seek_box.append(self.time_total)

        drag = Gtk.GestureClick()
        drag.connect("pressed", lambda *_: setattr(self, "_seek_dragging", True))
        drag.connect("released", self._on_seek_released)
        drag.connect("cancel", lambda *_: setattr(self, "_seek_dragging", False))
        self.seek.add_controller(drag)

        self.player_box.append(head)
        self.player_box.append(seek_box)
        self.hero_lyric_lbl = Gtk.Label(xalign=0)
        self.hero_lyric_lbl.add_css_class("hero-lyric")
        self.hero_lyric_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.hero_lyric_lbl.set_max_width_chars(46)
        self.hero_lyric_lbl.set_visible(False)
        self.player_box.append(self.hero_lyric_lbl)
        if self.lyrics is not None and hasattr(self.lyrics, "connect"):
            self.lyrics.connect("ready", lambda *_: self._refresh_hero_lyric())
        self.hero_card.append(self.player_box)

        # idle header shown when nothing plays
        self.idle_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.idle_box.set_valign(Gtk.Align.CENTER)
        clock_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        clock_row.set_valign(Gtk.Align.CENTER)
        self.big_clock = Gtk.Label(xalign=0)
        self.big_clock.add_css_class("hero-clock")
        self.big_date = Gtk.Label(xalign=1, hexpand=True)
        self.big_date.add_css_class("track-artist")
        clock_row.append(self.big_clock)
        clock_row.append(self.big_date)

        self.next_event_btn = Gtk.Button()
        self.next_event_btn.add_css_class("hero-event-btn")
        self.next_event_btn.set_halign(Gtk.Align.START)
        ev_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        ev_ico = Gtk.Image.new_from_icon_name("x-office-calendar-symbolic")
        ev_ico.set_pixel_size(13)
        self.next_event_lbl = Gtk.Label(xalign=0)
        self.next_event_lbl.add_css_class("hero-lyric")
        self.next_event_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.next_event_lbl.set_max_width_chars(44)
        ev_inner.append(ev_ico)
        ev_inner.append(self.next_event_lbl)
        self.next_event_btn.set_child(ev_inner)
        self.next_event_btn.set_visible(False)
        self.next_event_btn.connect("clicked", self._on_next_event_click)
        if self.calendar is not None and hasattr(self.calendar, "connect"):
            self.calendar.connect("changed", lambda *_: self._refresh_calendar_next())

        self.idle_box.append(clock_row)
        self.idle_box.append(self.next_event_btn)
        self.hero_card.append(self.idle_box)
        self.append(self.hero_card)

    def _populate_player_menu(self):
        self._pop_gen += 1
        gen = self._pop_gen
        while (c := self.player_pop_box.get_first_child()) is not None:
            self.player_pop_box.remove(c)
        if hasattr(self.media, "list_players"):
            players = self.media.list_players()
            if players:
                lbl = Gtk.Label(label="Players", xalign=0)
                lbl.add_css_class("tile-sub")
                self.player_pop_box.append(lbl)
            for pl in players:
                b = Gtk.Button(
                    label=f"{'● ' if pl is self.media.active else '○ '}{pl.label}: {pl.title or pl.status}"
                )
                b.add_css_class("panel-row-btn")
                b.connect(
                    "clicked",
                    lambda _b, bn=pl.bus_name: (
                        self.media.select_player(bn),
                        self.player_popover.popdown(),
                    ),
                )
                self.player_pop_box.append(b)
        if self.player_pop_box.get_first_child() is None:
            self._pop_empty = Gtk.Label(label="No extra players or queues", xalign=0)
            self._pop_empty.add_css_class("list-sub")
            self.player_pop_box.append(self._pop_empty)
        else:
            self._pop_empty = None
        if hasattr(self.media, "get_tracks"):
            self.media.get_tracks(
                lambda tracks, g=gen: self._append_tracks_menu(tracks, g)
            )
        if hasattr(self.media, "get_playlists"):
            self.media.get_playlists(
                lambda pls, g=gen: self._append_playlists_menu(pls, g)
            )

    def _clear_pop_placeholder(self):
        if getattr(self, "_pop_empty", None) is not None:
            if self._pop_empty.get_parent() is self.player_pop_box:
                self.player_pop_box.remove(self._pop_empty)
            self._pop_empty = None

    def _append_tracks_menu(self, tracks: list[dict], gen: int | None = None):
        if (gen is not None and gen != self._pop_gen) or not tracks:
            return
        self._clear_pop_placeholder()
        lbl = Gtk.Label(label="Track queue", xalign=0)
        lbl.add_css_class("tile-sub")
        self.player_pop_box.append(lbl)
        for tr in tracks[:8]:
            b = Gtk.Button(label=tr["title"][:36])
            b.add_css_class("panel-row-btn")
            b.connect(
                "clicked",
                lambda _b, tid=tr["id"]: (
                    self.media.go_to_track(tid),
                    self.player_popover.popdown(),
                ),
            )
            self.player_pop_box.append(b)

    def _append_playlists_menu(self, playlists: list[dict], gen: int | None = None):
        if (gen is not None and gen != self._pop_gen) or not playlists:
            return
        self._clear_pop_placeholder()
        lbl = Gtk.Label(label="Playlists", xalign=0)
        lbl.add_css_class("tile-sub")
        self.player_pop_box.append(lbl)
        for pl in playlists[:8]:
            b = Gtk.Button(label=pl["name"][:36])
            b.add_css_class("panel-row-btn")
            b.connect(
                "clicked",
                lambda _b, pid=pl["id"]: (
                    self.media.activate_playlist(pid),
                    self.player_popover.popdown(),
                ),
            )
            self.player_pop_box.append(b)

    # -- symmetrical 2-column Bento controls -------------------------------

    @staticmethod
    def _make_action_pill(
        icon_name: str, text: str, tooltip: str, toggle: bool = True
    ) -> tuple[Gtk.Button | Gtk.ToggleButton, Gtk.Image]:
        btn = Gtk.ToggleButton() if toggle else Gtk.Button()
        btn.add_css_class("action-pill")
        btn.set_tooltip_text(tooltip)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.set_halign(Gtk.Align.CENTER)
        ico = Gtk.Image.new_from_icon_name(icon_name)
        ico.set_pixel_size(12)
        lbl = Gtk.Label(label=text)
        box.append(ico)
        box.append(lbl)
        btn.set_child(box)
        return btn, ico

    def _build_bento_controls(self):
        self.bento_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.bento_row.set_homogeneous(True)

        # Left Bento card: read-only audio/brightness status and quick toggles
        self.controls_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.controls_card.add_css_class("bento-card")

        has_toggles = bool(self.cfg.get("modules", {}).get("toggles", True))

        self.volume_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        volume_badge = Gtk.Box()
        volume_badge.set_size_request(30, 30)
        volume_badge.set_halign(Gtk.Align.CENTER)
        volume_badge.set_valign(Gtk.Align.CENTER)
        self.volume_icon = Gtk.Image.new_from_icon_name("audio-volume-high-symbolic")
        self.volume_icon.set_pixel_size(14)
        volume_badge.append(self.volume_icon)
        self.volume_status_lbl = Gtk.Label(label="Volume", xalign=0, hexpand=True)
        self.volume_status_lbl.add_css_class("tile-sub")
        self.vol_pct_lbl = Gtk.Label(label="—", xalign=1)
        self.vol_pct_lbl.add_css_class("slider-pct")
        self.vol_pct_lbl.set_width_chars(5)
        self.volume_row.append(volume_badge)
        self.volume_row.append(self.volume_status_lbl)
        self.volume_row.append(self.vol_pct_lbl)
        self.volume_row.set_visible(self.audio is not None)
        self.controls_card.append(self.volume_row)

        self.mic_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mic_badge = Gtk.Box()
        mic_badge.set_size_request(30, 30)
        mic_badge.set_halign(Gtk.Align.CENTER)
        mic_badge.set_valign(Gtk.Align.CENTER)
        self.mic_icon = Gtk.Image.new_from_icon_name(
            "microphone-sensitivity-high-symbolic"
        )
        self.mic_icon.set_pixel_size(14)
        mic_badge.append(self.mic_icon)
        self.mic_lbl = Gtk.Label(label="Microphone on", xalign=0, hexpand=True)
        self.mic_lbl.add_css_class("tile-sub")
        self.mic_row.append(mic_badge)
        self.mic_row.append(self.mic_lbl)
        self.mic_row.set_visible(self.audio is not None)
        self.controls_card.append(self.mic_row)

        self.brightness_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        b_badge = Gtk.Box()
        b_badge.set_size_request(30, 30)
        b_badge.set_halign(Gtk.Align.CENTER)
        b_badge.set_valign(Gtk.Align.CENTER)
        self.brightness_icon = Gtk.Image.new_from_icon_name(
            "display-brightness-high-symbolic"
        )
        self.brightness_icon.set_pixel_size(14)
        self.brightness_icon.set_tooltip_text("Display brightness")
        self.brightness_icon.set_halign(Gtk.Align.CENTER)
        self.brightness_icon.set_valign(Gtk.Align.CENTER)
        b_badge.append(self.brightness_icon)
        self.brightness_status_lbl = Gtk.Label(
            label="Brightness", xalign=0, hexpand=True
        )
        self.brightness_status_lbl.add_css_class("tile-sub")
        self.brightness_pct_lbl = Gtk.Label(label="0%", xalign=1)
        self.brightness_pct_lbl.add_css_class("slider-pct")
        self.brightness_pct_lbl.set_width_chars(5)
        self.brightness_row.append(b_badge)
        self.brightness_row.append(self.brightness_status_lbl)
        self.brightness_row.append(self.brightness_pct_lbl)
        self.brightness_row.set_visible(
            self.brightness is not None and self.brightness.available
        )
        self.controls_card.append(self.brightness_row)

        self.toggles_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.toggles_row.set_homogeneous(True)
        self.toggles_row.set_vexpand(True)
        self.toggles_row.set_valign(Gtk.Align.END)

        self.tg_caffeine, _ = self._make_action_pill(
            pick_icon(
                self,
                "preferences-desktop-screensaver-symbolic",
                "caffeine-cup-full-symbolic",
                "video-display-symbolic",
            ),
            "Awake",
            "Caffeine: prevent screen sleep",
            True,
        )
        self.tg_caffeine.connect("toggled", self._on_caffeine_toggle)

        shot, _ = self._make_action_pill(
            pick_icon(self, "applets-screenshooter-symbolic", "camera-photo-symbolic"),
            "Shot",
            "Take screenshot",
            False,
        )
        if not any(GLib.find_program_in_path(t[0]) for t in SCREENSHOT_TOOLS):
            shot.set_sensitive(False)
            shot.set_tooltip_text("No screenshot tool installed")
        shot.connect("clicked", self._on_screenshot)

        self.tg_caffeine.set_visible(has_toggles)
        shot.set_visible(has_toggles)
        for w in (self.tg_caffeine, shot):
            self.toggles_row.append(w)
        self.toggles_row.set_visible(has_toggles)
        self.controls_card.append(self.toggles_row)

        self._sync_controls_card_visibility()

        # Right Bento column: Weather card stacked cleanly above Focus timer
        self.right_bento = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        self.weather_tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.weather_tile.add_css_class("tile")
        self.weather_tile.add_css_class("tile--slim")
        self.weather_tile.set_vexpand(True)
        w_top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        w_top.set_vexpand(True)
        w_top.set_valign(Gtk.Align.START)
        self.weather_icon = Gtk.Image.new_from_icon_name("weather-clear-symbolic")
        self.weather_icon.set_pixel_size(24)
        self.weather_icon.add_css_class("weather-ico")
        self.weather_temp = Gtk.Label(label="—", xalign=0)
        self.weather_temp.add_css_class("tile-big")
        self.weather_loc = Gtk.Label(label="", xalign=1, hexpand=True)
        self.weather_loc.add_css_class("list-title")
        self.weather_loc.set_ellipsize(Pango.EllipsizeMode.END)
        self.weather_loc.set_width_chars(1)
        self.weather_loc.set_max_width_chars(18)
        self.weather_loc.set_visible(False)
        w_top.append(self.weather_icon)
        w_top.append(self.weather_temp)
        w_top.append(self.weather_loc)

        w_bot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        w_bot.set_valign(Gtk.Align.END)
        self.weather_desc = Gtk.Label(label="Weather", xalign=0, hexpand=True)
        self.weather_desc.add_css_class("tile-sub")
        self.weather_desc.set_ellipsize(Pango.EllipsizeMode.END)
        self.weather_desc.set_width_chars(1)
        self.weather_desc.set_max_width_chars(18)
        self.weather_forecast = Gtk.Label(label="", xalign=1)
        self.weather_forecast.add_css_class("tile-sub")
        self.weather_forecast.set_ellipsize(Pango.EllipsizeMode.END)
        self.weather_forecast.set_max_width_chars(16)
        self.weather_forecast.set_visible(False)
        w_bot.append(self.weather_desc)
        w_bot.append(self.weather_forecast)

        self.weather_tile.append(w_top)
        self.weather_tile.append(w_bot)
        weather_click = Gtk.GestureClick()
        weather_click.connect(
            "released",
            lambda g, *_: (
                g.set_state(Gtk.EventSequenceState.CLAIMED),
                self.weather and self.weather.request(force=True),
            ),
        )
        self.weather_tile.add_controller(weather_click)
        self.weather_tile.set_tooltip_text("Click to refresh weather")
        if self.weather is None:
            self.weather_tile.set_visible(False)
        else:
            self.weather.connect("changed", self._on_weather)

        # Focus Timer sleek horizontal Bento bar + presets / Focus checklist popover
        self.timer_tile = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.timer_tile.add_css_class("tile")
        self.timer_tile.add_css_class("tile--slim")
        self.timer_phase = Gtk.Label(label="Focus · 1/4", xalign=0, hexpand=True)
        self.timer_phase.add_css_class("tile-sub")
        self.timer_phase.set_ellipsize(Pango.EllipsizeMode.END)
        phase_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        phase_row.append(self.timer_phase)
        phase_chev = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        phase_chev.set_pixel_size(10)
        phase_row.append(phase_chev)

        self.timer_menu_btn = Gtk.MenuButton()
        self.timer_menu_btn.add_css_class("timer-phase-btn")
        self.timer_menu_btn.set_hexpand(True)
        self.timer_menu_btn.set_child(phase_row)
        self.timer_menu_btn.set_tooltip_text("Timer presets & Focus checklist")
        self.timer_popover = Gtk.Popover()
        pop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        pop_box.set_size_request(250, -1)
        preset_hdr = _section_title("Timer presets")
        preset_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        preset_row.set_homogeneous(True)
        for p_lbl, p_min, p_focus in (
            ("15m", 15, True),
            ("25m", 25, True),
            ("45m", 45, True),
            ("5m", 5, False),
            ("15m ✦", 15, False),
        ):
            pb = Gtk.Button(label=p_lbl)
            pb.add_css_class("panel-row-btn")
            pb.connect(
                "clicked",
                lambda _b, m=p_min, f=p_focus: self._start_preset_timer(m, f),
            )
            preset_row.append(pb)
        pop_box.append(preset_hdr)
        pop_box.append(preset_row)

        if self.todos is not None and self.cfg.get("modules", {}).get("todos", True):
            t_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            t_lbl = _section_title("Focus checklist")
            t_lbl.set_hexpand(True)
            t_clr = Gtk.Button(label="Clear done")
            t_clr.add_css_class("list-clear-btn")
            t_clr.connect("clicked", lambda *_: self.todos.clear_done())
            t_hdr.append(t_lbl)
            t_hdr.append(t_clr)
            self.todo_entry = Gtk.Entry(placeholder_text="Add focus task…")
            self.todo_entry.add_css_class("ai-entry")
            self.todo_entry.connect("activate", self._on_add_todo)
            self.todo_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            self.todo_empty = Gtk.Label(label="No focus tasks yet", xalign=0)
            self.todo_empty.add_css_class("empty-hint")
            pop_box.append(t_hdr)
            pop_box.append(self.todo_entry)
            pop_box.append(self.todo_box)
            pop_box.append(self.todo_empty)
            self.todos.connect("changed", lambda *_: self._refresh_todos())

        self.timer_popover.set_child(pop_box)
        self.timer_menu_btn.set_popover(self.timer_popover)

        self.timer_label = Gtk.Label(label="25:00", xalign=1)
        self.timer_label.add_css_class("timer-readout")
        self.timer_toggle = Gtk.Button()
        self.timer_toggle.add_css_class("tile-btn")
        self.timer_toggle.set_icon_name("media-playback-start-symbolic")
        self.timer_toggle.connect("clicked", self._on_timer_toggle)
        timer_reset = Gtk.Button()
        timer_reset.add_css_class("tile-btn")
        timer_reset.set_icon_name("view-refresh-symbolic")
        timer_reset.connect("clicked", self._on_timer_reset)
        self.timer_tile.append(self.timer_menu_btn)
        self.timer_tile.append(self.timer_label)
        self.timer_tile.append(self.timer_toggle)
        self.timer_tile.append(timer_reset)

        self.right_bento.append(self.weather_tile)
        self.right_bento.append(self.timer_tile)

        self.bento_row.append(self.controls_card)
        self.bento_row.append(self.right_bento)
        self.append(self.bento_row)

        # timer state
        self.timer_left = POMODORO
        self.timer_running = False
        self.timer_is_focus = True
        self.focus_rounds = 0
        self._timer_src = 0

    def _sync_controls_card_visibility(self):
        if hasattr(self, "controls_card"):
            any_ctrl = any(
                getattr(self, a, None) is not None and getattr(self, a).get_visible()
                for a in ("volume_row", "brightness_row", "toggles_row")
            )
            self.controls_card.set_visible(any_ctrl)

    def _on_caffeine_toggle(self, btn):
        if btn.get_active() and self._caffeine is None:
            try:
                self._caffeine = Gio.Subprocess.new(
                    [
                        "systemd-inhibit",
                        "--what=idle:sleep",
                        "--who=ledge-pill",
                        "--why=Caffeine",
                        "sleep",
                        "infinity",
                    ],
                    Gio.SubprocessFlags.NONE,
                )
            except GLib.Error:
                btn.set_active(False)
        elif not btn.get_active() and self._caffeine is not None:
            self._caffeine.force_exit()
            self._caffeine = None

    def _on_screenshot(self, *_):
        for tool in SCREENSHOT_TOOLS:
            if GLib.find_program_in_path(tool[0]):
                try:
                    Gio.Subprocess.new(tool, Gio.SubprocessFlags.NONE)
                except GLib.Error:
                    pass
                return

    # -- unified single-section workspace ---------------------------------

    def _build_sections(self):
        self.content_scroll = Gtk.ScrolledWindow()
        self.content_scroll.add_css_class("content-scroll")
        self.content_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.content_scroll.set_propagate_natural_height(True)
        self.content_scroll.set_max_content_height(380)

        self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        mods = self.cfg.get("modules", {})

        if (
            self.network is not None
            or mods.get("bluetooth", True)
            or self.kde is not None
        ):
            self.connectivity_panel = ConnectivityPanel(
                network=self.network,
                bluetooth=None,
                kdeconnect=self.kde,
                on_toggle=self._collapse_non_conn_drawers,
            )
            self.content_box.append(self.connectivity_panel)

        # Utility dock (AI · Clipboard) with its shared drawer.
        self.dock_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.dock_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.dock_row.set_homogeneous(True)
        self.dock_drawer = _section_card(0)
        self.dock_drawer.add_css_class("dock-drawer")
        self.dock_drawer.set_visible(False)

        show_ai = bool(mods.get("assistant", True) or mods.get("ollama", True))
        self.ai_panel = (
            AiDevPanel(
                self.cfg,
                ollama=self.ollama,
                productivity=self.prod,
                desktop=self.desktop,
                clipboard=self.clipboard,
                embedded=True,
                on_toggle=self._on_dock_toggle,
            )
            if show_ai
            else None
        )
        if self.ai_panel is not None:
            self.dock_row.append(self.ai_panel.head_tile)
            self.dock_drawer.append(self.ai_panel.revealer)

        self._build_clipboard(self.dock_row, self.dock_drawer)

        self.dock_box.append(self.dock_row)
        self.dock_box.append(self.dock_drawer)
        self.dock_box.set_visible(self.dock_row.get_first_child() is not None)
        self.content_box.append(self.dock_box)

        self._build_notifications(self.content_box)
        self._build_feeds(self.content_box)

        self.overview_empty = _section_card(4)
        ov_lbl = Gtk.Label(
            label="All quiet — active panels, notifications, and feeds appear here.",
            xalign=0,
        )
        ov_lbl.add_css_class("empty-hint")
        ov_lbl.set_wrap(True)
        self.overview_empty.append(ov_lbl)
        self.content_box.append(self.overview_empty)
        self._sync_overview_empty()

        self.content_scroll.set_child(self.content_box)
        self.append(self.content_scroll)

    def _collapse_dock_drawers(self):
        if self._clip_open:
            self._set_clipboard_open(False)
        if self.ai_panel is not None and self.ai_panel._open:
            self.ai_panel.set_open(False)
        if hasattr(self, "dock_drawer"):
            self.dock_drawer.set_visible(False)

    def _collapse_non_conn_drawers(self):
        self._collapse_dock_drawers()

    def _on_dock_toggle(self, active_key: str, opened: bool):
        if self._dock_syncing:
            return
        self._dock_syncing = True
        try:
            if opened:
                if self.connectivity_panel is not None:
                    self.connectivity_panel.collapse_drawers()
                if (
                    active_key != "ai"
                    and self.ai_panel is not None
                    and self.ai_panel._open
                ):
                    self.ai_panel.set_open(False)
                if active_key != "clipboard" and self._clip_open:
                    self._set_clipboard_open(False)
            any_open = bool(
                self._clip_open
                or (self.ai_panel is not None and self.ai_panel._open)
            )
            if hasattr(self, "dock_drawer"):
                self.dock_drawer.set_visible(any_open)
        finally:
            self._dock_syncing = False

    def set_bluetooth_service(self, bt_svc):
        if self.connectivity_panel is not None:
            self.connectivity_panel.set_bluetooth_service(bt_svc)

    def open_dock_panel(self, which: str):
        """Open one dock utility drawer and focus AI prompt when opening 'ai'."""
        key = (which or "").strip().lower()
        if key == "ai" and self.ai_panel is not None:
            self.ai_panel.set_open(True)
            if hasattr(self.ai_panel, "entry"):
                GLib.idle_add(lambda: (self.ai_panel.entry.grab_focus(), False)[1])
        elif key == "clipboard" and self.clipboard is not None:
            self._set_clipboard_open(True)

    # -- overview sections -------------------------------------------------

    def _sync_overview_empty(self):
        if not hasattr(self, "overview_empty"):
            return
        any_vis = any(
            getattr(self, attr, None) is not None and getattr(self, attr).get_visible()
            for attr in (
                "connectivity_panel",
                "dock_box",
                "notif_group",
                "feed_group",
            )
        )
        self.overview_empty.set_visible(not any_vis)

    def _build_footer(self):
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.sys_label = Gtk.Label(xalign=0, hexpand=True)
        self.sys_label.add_css_class("sys-stats")
        self.sys_label.set_wrap(False)
        self.sys_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.sys_label.set_width_chars(1)
        self.sys_label.set_max_width_chars(52)
        self.sys_label.set_visible(self.system is not None)
        if self.system is not None:
            self.system.connect("changed", self._on_system)

        footer.append(self.sys_label)
        self.append(footer)

    # -- refresh from services --------------------------------------------

    def refresh_media(self):
        p = self.media.active
        has = p is not None
        self.player_box.set_visible(has)
        self.idle_box.set_visible(not has)
        if not has:
            if hasattr(self, "hero_lyric_lbl"):
                self.hero_lyric_lbl.set_visible(False)
            self._refresh_big_clock()
            self._sync_pos_timer()
            return
        self.title.set_label(p.title or "Unknown track")
        self.artist.set_label(p.artist or "")
        self.album.set_label(p.album or "")
        self.artist.set_visible(bool(p.artist))
        self.album.set_visible(bool(p.album))
        if p.art_path and os.path.exists(p.art_path):
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(p.art_path, 58, 58, False)
                self.art.set_paintable(Gdk.Texture.new_for_pixbuf(pb))
            except GLib.Error:
                self.art.set_paintable(None)
        else:
            self.art.set_paintable(None)
        playing = p.status == "Playing"
        self.btn_play.set_icon_name(
            "media-playback-pause-symbolic"
            if playing
            else "media-playback-start-symbolic"
        )
        self.btn_prev.set_sensitive(p.can_prev)
        self.btn_next.set_sensitive(p.can_next)
        total = p.length_us
        self.seek.set_range(0, max(1, total))
        self.seek.set_sensitive(total > 0)
        self.time_total.set_label(_fmt_us(total))
        self._sync_pos_timer()
        self._poll_position()
        self._refresh_hero_lyric()

    def refresh_battery(self):
        if (
            not self.cfg.get("modules", {}).get("battery", True)
            or not self.power.available
        ):
            self.battery_tile.set_visible(False)
            return
        self.battery_tile.set_visible(True)
        self.ring.update(self.power.percentage, self.power.charging)
        suffix = " ⚡" if self.power.charging else ""
        self.batt_label.set_label(f"{self.power.percentage:.0f}%{suffix}")
        health = getattr(self.power, "health_pct", -1)
        if hasattr(self, "batt_sub"):
            if self.power.charging:
                self.batt_sub.set_label("Charging")
            elif health >= 0:
                self.batt_sub.set_label(f"{health:.0f}% health")
            else:
                self.batt_sub.set_label("Battery")
        tips = []
        if self.power.charging:
            tips.append("Charging")
        if health >= 0:
            tips.append(f"Health: {health:.0f}%")
        for ex in getattr(self.power, "extra_batteries", []):
            tips.append(f"{ex['model']}: {ex['percent']}%")
        self.battery_tile.set_tooltip_text("\n".join(tips) if tips else "")

    def is_interacting(self) -> bool:
        if self._seek_dragging:
            return True
        if self._power_armed:
            return True
        if self.player_pick_btn.get_active() or self.player_popover.get_visible():
            return True
        if getattr(self, "timer_menu_btn", None) is not None and (
            self.timer_menu_btn.get_active() or self.timer_popover.get_visible()
        ):
            return True
        if getattr(self, "mirror_btn", None) is not None and (
            self.mirror_btn.get_active() or self.mirror_popover.get_visible()
        ):
            return True
        if self.connectivity_panel is not None and (
            self.connectivity_panel.wifi_pw_box.get_visible()
        ):
            return True
        if self.ai_panel is not None and (
            getattr(self.ai_panel, "_pending_prompt", "")
            or (
                hasattr(self.ai_panel, "confirm_box")
                and self.ai_panel.confirm_box.get_visible()
            )
        ):
            return True
        root = self.get_root() if self.get_mapped() else None
        if root is not None and hasattr(root, "get_focus"):
            focus = root.get_focus()
            if (
                isinstance(focus, Gtk.Widget)
                and isinstance(focus, Gtk.Editable)
                and focus.is_ancestor(self)
            ):
                return True
        return False

    def refresh_mic(self, muted: bool):
        self.mic_icon.set_from_icon_name(
            "microphone-sensitivity-muted-symbolic"
            if muted
            else "microphone-sensitivity-high-symbolic"
        )
        self.mic_lbl.set_label("Microphone muted" if muted else "Microphone on")

    def refresh_volume(self, percent: int, muted: bool = False):
        clamped = max(0, min(150, int(percent)))
        self.vol_pct_lbl.set_label("Muted" if muted else f"{clamped}%")
        self.volume_icon.set_from_icon_name(volume_icon(clamped, bool(muted)))

    def refresh_brightness(self, percent: int):
        if self.brightness is None or not self.brightness.available:
            self.brightness_row.set_visible(False)
            self._sync_controls_card_visibility()
            return
        self.brightness_row.set_visible(True)
        self._sync_controls_card_visibility()
        clamped = max(0, min(100, int(percent)))
        self.brightness_pct_lbl.set_label(f"{clamped}%")
        self._update_brightness_icon(clamped)

    def _update_brightness_icon(self, percent: int):
        self.brightness_icon.set_from_icon_name(brightness_icon(percent))

    def refresh_network(
        self, name: str, kind: str, connected: bool, wifi_signal: int | None = -1
    ):
        if self.connectivity_panel is not None:
            self.connectivity_panel.refresh()

    def refresh_bluetooth(self, device: str, connected: bool, battery: int):
        if self.connectivity_panel is not None:
            self.connectivity_panel.refresh()

    def refresh_kdeconnect(self):
        if self.connectivity_panel is not None:
            self.connectivity_panel.refresh()

    def refresh_privacy(self, mic: bool, cam: bool, rec: bool, share: bool):
        active = []
        icon = "audio-input-microphone-symbolic"
        if mic:
            active.append("Mic")
        if cam:
            active.append("Cam")
            icon = "camera-web-symbolic"
        if rec:
            active.append("Rec")
            icon = "media-record-symbolic"
        if share:
            active.append("Share")
            icon = "video-display-symbolic"
        self.priv_chip.set_visible(bool(active))
        if active:
            _set_chip(self.priv_chip, icon, " + ".join(active))

    def _on_weather(self, _svc, temp, desc, icon):
        self.weather_temp.set_label(temp)
        self.weather_desc.set_label(desc)
        self.weather_icon.set_from_icon_name(weather_icon(self, icon))
        loc = (
            getattr(self.weather, "location", "")
            if getattr(self, "weather", None) is not None
            else ""
        )
        if hasattr(self, "weather_loc"):
            self.weather_loc.set_label(loc)
            self.weather_loc.set_visible(bool(loc))
        tip_parts = [p for p in (loc, f"{temp} · {desc}".strip(" ·")) if p]
        if getattr(self, "weather", None) is not None and getattr(
            self.weather, "forecast", None
        ):
            parts = []
            for day in self.weather.forecast[1:3]:
                lo = day.get("min", "")
                hi = day.get("max", "")
                if lo == "" and hi == "":
                    continue
                date = day.get("date", "")
                short = date[5:] if len(date) >= 10 else date
                parts.append(f"{short} {lo}/{hi}°" if short else f"{lo}/{hi}°")
            self.weather_forecast.set_label("  ".join(parts))
            self.weather_forecast.set_visible(bool(parts))
            if parts:
                tip_parts.append("Forecast: " + "  ".join(parts))
        else:
            self.weather_forecast.set_label("")
            self.weather_forecast.set_visible(False)
        tip_parts.append("Click to refresh weather")
        self.weather_tile.set_tooltip_text("\n".join(tip_parts))

    def _on_system(self, _svc, data):
        parts = [f"CPU {data.get('cpu', 0)}%"]
        parts.append(f"RAM {data.get('ram_gib', 0.0):.1f} GiB ({data.get('ram', 0)}%)")
        if data.get("gpu") is not None:
            parts.append(f"GPU {data['gpu']}%")
        if data.get("gpu_temp") is not None:
            parts.append(f"{data['gpu_temp']:.0f}°C")
        if data.get("ssd_temp") is not None:
            parts.append(f"SSD {data['ssd_temp']:.0f}°C")
        if data.get("fps") is not None:
            parts.append(f"{data['fps']} FPS")
        if data.get("disk") is not None:
            free = data.get("disk_free_gib")
            parts.append(
                f"Disk {data['disk']}% ({free}G free)"
                if free is not None
                else f"Disk {data['disk']}%"
            )
        if data.get("net_down", 0) or data.get("net_up", 0):
            parts.append(f"Net ↓{data['net_down']:.0f} ↑{data['net_up']:.0f} KB/s")
        if data.get("power") is not None and data["power"] > 0.05:
            parts.append(f"Power {data['power']:.1f}W")
        temps = data.get("temps", {})
        if temps:
            first = next(iter(temps.items()))
            parts.append(f"{first[0]} {first[1]}°C")
        self.sys_label.set_label("   ·   ".join(parts))

    # -- lifecycle ----------------------------------------------------------

    def _on_map(self):
        self.refresh_media()
        self.refresh_battery()
        self._refresh_big_clock()
        self._refresh_calendar_next()
        self._refresh_todos()
        self._sync_timer_ui()
        if self.audio is not None:
            if self.audio.percent >= 0:
                self.refresh_volume(self.audio.percent, self.audio.muted)
            self.refresh_mic(self.audio.mic_muted)
        if self.brightness is not None:
            self.refresh_brightness(self.brightness.percent)
        if self.clipboard is not None and hasattr(self.clipboard, "sync_external"):
            self.clipboard.sync_external()
        if self.weather is not None:
            if self.weather.temp:
                self._on_weather(
                    self.weather,
                    self.weather.temp,
                    self.weather.desc,
                    self.weather.icon,
                )
            self.weather.request()
        if self.system is not None:
            self.system.start()
        if self.ollama is not None:
            self.ollama.refresh_now()
        if self.feeds is not None:
            self._refresh_feeds()
            self.feeds.start()
        if self.privacy is not None:
            self.privacy.start()
        if self.desktop is not None and hasattr(self.desktop, "start"):
            self.desktop.start()
        if self.connectivity_panel is not None:
            self.connectivity_panel.refresh()
        if self.ai_panel is not None:
            self.ai_panel.refresh()
    def _on_unmap(self):
        if self._pos_timer:
            GLib.source_remove(self._pos_timer)
            self._pos_timer = 0
        if self.system is not None:
            self.system.stop()
        if self.feeds is not None:
            self.feeds.stop()
        self._set_clipboard_open(False)
        if self.connectivity_panel is not None:
            self.connectivity_panel.collapse_drawers()
        if self.ai_panel is not None:
            self.ai_panel.set_open(False)
        if getattr(self, "mirror_popover", None) is not None:
            self.mirror_popover.popdown()
            self.mirror_popover.stop_preview()
        self._disarm_power()

    def _on_destroy(self):
        self._on_unmap()
        if self._clip_feedback_timer:
            GLib.source_remove(self._clip_feedback_timer)
            self._clip_feedback_timer = 0
        if self._timer_src:
            GLib.source_remove(self._timer_src)
            self._timer_src = 0
        if self._power_disarm_timer:
            GLib.source_remove(self._power_disarm_timer)
            self._power_disarm_timer = 0
        if self._caffeine is not None:
            self._caffeine.force_exit()
            self._caffeine = None

    # -- seek bar ----------------------------------------------------------

    def _sync_pos_timer(self):
        playing = self.media.active and self.media.active.status == "Playing"
        want = bool(playing) and self.get_mapped()
        if want and not self._pos_timer:
            self._pos_timer = GLib.timeout_add_seconds(1, self._poll_position)
        elif not want and self._pos_timer:
            GLib.source_remove(self._pos_timer)
            self._pos_timer = 0

    def _poll_position(self):
        self.media.get_position(self._on_position)
        return True

    def _on_position(self, pos_us: int):
        self._position_us = pos_us
        if not self._seek_dragging:
            self.seek.set_value(pos_us)
        self.time_now.set_label(_fmt_us(pos_us))
        self._refresh_hero_lyric(pos_us)

    def _refresh_hero_lyric(self, pos_us: int | None = None):
        if not hasattr(self, "hero_lyric_lbl"):
            return
        if (
            self.lyrics is None
            or not getattr(self.lyrics, "times", None)
            or self.media.active is None
        ):
            self.hero_lyric_lbl.set_visible(False)
            return
        pos_s = (pos_us if pos_us is not None else self._position_us) / 1_000_000
        line = (self.lyrics.line_at(pos_s) or "").strip()
        show = bool(line and line != "…")
        self.hero_lyric_lbl.set_visible(show)
        if show:
            self.hero_lyric_lbl.set_label(f"♪  {line}")

    def _on_seek_released(self, gesture, n, x, y):
        self._seek_dragging = False
        self.media.seek_to(int(self.seek.get_value()))

    # -- pomodoro ----------------------------------------------------------

    def _on_timer_toggle(self, *_):
        self.timer_running = not self.timer_running
        if self.timer_running and not self._timer_src:
            self._timer_src = GLib.timeout_add_seconds(1, self._timer_tick)
        if self.cfg.get("focus_auto_dnd", False):
            dnd = self.timer_running and self.timer_is_focus
            self.set_dnd_state(dnd)
            if self.notifier is not None:
                self.notifier.set_dnd(dnd)
            if self.on_dnd_change:
                self.on_dnd_change(dnd)
        self._sync_timer_ui()

    def _on_timer_reset(self, *_):
        self.timer_running = False
        self.timer_left = POMODORO
        self.timer_is_focus = True
        self.focus_rounds = 0
        if self._timer_src:
            GLib.source_remove(self._timer_src)
            self._timer_src = 0
        self._sync_timer_ui()

    def control_timer(self, action: str, minutes=None) -> dict:
        """Control the focus timer from keyboard/AI commands."""
        if action == "start":
            if not self.timer_running:
                self._on_timer_toggle()
            return {"ok": True, "running": True, "seconds": self.timer_left}
        if action == "pause":
            if self.timer_running:
                self._on_timer_toggle()
            return {"ok": True, "running": False, "seconds": self.timer_left}
        if action == "reset":
            self._on_timer_reset()
            return {"ok": True, "running": False, "seconds": self.timer_left}
        if action == "set_minutes":
            if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
                return {"ok": False, "error": "minutes must be a number"}
            mins = max(1, min(180, int(minutes)))
            self.timer_is_focus = True
            self.timer_left = mins * 60
            if not self.timer_running:
                self._on_timer_toggle()
            else:
                self._sync_timer_ui()
            return {"ok": True, "running": True, "minutes": mins}
        return {"ok": False, "error": f"Unknown timer action '{action}'"}

    def set_caffeine(self, enabled: bool) -> dict:
        """Toggle screen sleep inhibition in sync with the Quick Toggles chip."""
        if not hasattr(self, "tg_caffeine"):
            return {"ok": False, "error": "Caffeine toggle unavailable"}
        self.tg_caffeine.set_active(bool(enabled))
        return {"ok": True, "caffeine": bool(self.tg_caffeine.get_active())}

    def _timer_tick(self):
        if not self.timer_running:
            self._timer_src = 0
            return False
        self.timer_left -= 1
        if self.timer_left <= 0:
            self.timer_left = 0
            completed_focus = self.timer_is_focus
            if self.cfg.get("pomodoro_auto_cycle", False):
                if completed_focus:
                    self.focus_rounds += 1
                    self.timer_is_focus = False
                    self.timer_left = 15 * 60 if self.focus_rounds % 4 == 0 else 5 * 60
                else:
                    self.timer_is_focus = True
                    self.timer_left = POMODORO
                self.timer_running = True
                if self.cfg.get("focus_auto_dnd", False):
                    if self.notifier is not None:
                        self.notifier.set_dnd(self.timer_is_focus)
                    self.set_dnd_state(self.timer_is_focus)
                    if self.on_dnd_change:
                        self.on_dnd_change(self.timer_is_focus)
                self._notify_done(completed_focus)
                self._sync_timer_ui()
                return True
            self.timer_running = False
            self._timer_src = 0
            self._notify_done(completed_focus)
            self._sync_timer_ui()
            return False
        self._sync_timer_ui()
        return True

    def _sync_timer_ui(self):
        m, s = divmod(self.timer_left, 60)
        text = f"{m:02d}:{s:02d}"
        self.timer_label.set_label(text)
        if self.timer_is_focus:
            base = f"Focus · {self.focus_rounds % 4 + 1}/4"
            active = self.todos.active_task() if self.todos is not None else ""
            if active:
                self.timer_phase.set_label(f"Focus · {active}")
                self.timer_tile.set_tooltip_text(f"{base} — {active}")
            else:
                self.timer_phase.set_label(base)
                self.timer_tile.set_tooltip_text("")
        elif self.focus_rounds and self.focus_rounds % 4 == 0:
            self.timer_phase.set_label("Long break")
        else:
            self.timer_phase.set_label("Short break")
        self.timer_toggle.set_icon_name(
            "media-playback-pause-symbolic"
            if self.timer_running
            else "media-playback-start-symbolic"
        )
        if self.timer_running:
            self.timer_tile.add_css_class("tile--timer-running")
        else:
            self.timer_tile.remove_css_class("tile--timer-running")
        if self.on_timer_change:
            self.on_timer_change(text if self.timer_running else None)

    def _start_preset_timer(self, minutes: int, is_focus: bool):
        self.timer_is_focus = bool(is_focus)
        self.timer_left = max(60, int(minutes) * 60)
        if hasattr(self, "timer_popover"):
            self.timer_popover.popdown()
        if not self.timer_running:
            self._on_timer_toggle()
        else:
            self._sync_timer_ui()

    def _on_add_todo(self, entry: Gtk.Entry):
        txt = entry.get_text().strip()
        if txt and self.todos is not None and self.todos.add(txt):
            entry.set_text("")

    def _refresh_todos(self):
        if self.todos is None or not hasattr(self, "todo_box"):
            return
        while (c := self.todo_box.get_first_child()) is not None:
            self.todo_box.remove(c)
        items = list(self.todos.items)
        self.todo_empty.set_visible(not items)
        for idx, item in enumerate(items[:8]):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            chk = Gtk.CheckButton()
            chk.set_active(bool(item.get("done")))
            chk.connect(
                "toggled", lambda _c, i=idx: self.todos and self.todos.toggle(i)
            )
            lbl = Gtk.Label(label=item.get("text", ""), xalign=0, hexpand=True)
            lbl.add_css_class("tile-sub" if item.get("done") else "list-title")
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            lbl.set_max_width_chars(24)
            rm = _icon_btn("window-close-symbolic", "Remove task")
            rm.connect("clicked", lambda _b, i=idx: self.todos and self.todos.remove(i))
            row.append(chk)
            row.append(lbl)
            row.append(rm)
            self.todo_box.append(row)
        self._sync_timer_ui()

    def _notify_done(self, completed_focus: bool):
        app = Gio.Application.get_default()
        if app:
            note = Gio.Notification.new(
                "Focus round complete" if completed_focus else "Break complete"
            )
            note.set_body(
                "25 minutes are up — take a break."
                if completed_focus
                else "Break time is over — start a focus round."
            )
            app.send_notification("pomodoro", note)

    def _refresh_big_clock(self):
        fmt = "%H:%M" if self.cfg.get("clock_24h", True) else "%I:%M %p"
        self.big_clock.set_label(time.strftime(fmt))
        self.big_date.set_label(time.strftime("%A %d %B"))

    def _refresh_calendar_next(self):
        if not hasattr(self, "next_event_btn") or self.calendar is None:
            return
        text = (
            self.calendar.format_next_label()
            if hasattr(self.calendar, "format_next_label")
            else ""
        )
        self.next_event_btn.set_visible(bool(text))
        if text:
            self.next_event_lbl.set_label(text)
            ev = (
                self.calendar.next_event()
                if hasattr(self.calendar, "next_event")
                else None
            )
            tip = []
            if ev and ev.get("location"):
                tip.append(ev["location"])
            if ev and ev.get("url"):
                tip.append(ev["url"])
            self.next_event_btn.set_tooltip_text("\n".join(tip) if tip else text)

    def _on_next_event_click(self, *_):
        if self.calendar is None or not hasattr(self.calendar, "next_event"):
            return
        ev = self.calendar.next_event()
        url = ev.get("url", "") if ev else ""
        if url.startswith(("https://", "http://")):
            try:
                Gio.AppInfo.launch_default_for_uri(url, None)
            except GLib.Error:
                pass
        elif hasattr(self.calendar, "refresh_now"):
            self.calendar.refresh_now()
