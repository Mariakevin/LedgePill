"""The island itself: one surface, three states, fluid morphs between them.

The morph is a Gtk.Stack with size interpolation — the window resizes with
it, so the layer surface always hugs the island exactly and clicks anywhere
else on screen go straight through to your apps.
"""

from __future__ import annotations

import math
import os

from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

from lostisland.config import VALID_ANIMATIONS, VALID_STYLES, VALID_THEMES
from lostisland.ui.activity import Activity, ActivityQueue
from lostisland.ui.expanded import Expanded
from lostisland.ui.peek import Peek
from lostisland.ui.pill import Pill

COLLAPSE_DELAY_MS = 700
_ANIM_MS = {"fluid": 260, "snappy": 130, "gentle": 420}
_WAKE_HOLD_MS = {"fluid": 3200, "snappy": 2500, "gentle": 3600}
_WAKE_STEPS = {"fluid": 12, "snappy": 8, "gentle": 15}
_MAX_BG_BYTES = 25 * 1024 * 1024


class Island(Gtk.Box):
    def __init__(
        self,
        cfg: dict,
        media,
        power,
        weather=None,
        system=None,
        on_settings=None,
        cava=None,
        lyrics=None,
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
        todos=None,
        calendar=None,
        on_dnd_change=None,
        on_expand_change=None,
        on_mascot_active=None,
    ):
        super().__init__()
        self.add_css_class("island")
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.START)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.cfg = cfg
        self.media = media
        self._on_expand_change = on_expand_change
        self._global_cursor_active = False
        self._pointer_inside = False

        self._peek_timeout = 0
        self._peek_queue = ActivityQueue()
        self._current_peek: Activity | None = None
        self._peek_seq = 0
        self._collapse_timeout = 0
        self._awake = False
        self._wake_step = 0
        self._wake_margins = (0, 0, 0, 0)
        self._wake_anim_timer = 0
        self._wake_sleep_timer = 0
        self._bg_anim_iter = None
        self._bg_anim_timeout = 0
        self._bg_media_path = ""

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(260)
        self.stack.set_interpolate_size(True)
        self.stack.set_hhomogeneous(False)
        self.stack.set_vhomogeneous(False)

        self.lyrics = lyrics
        self.pill = Pill(
            cfg,
            media=media,
            cava=cava,
            weather=weather,
            lyrics=lyrics,
            on_mascot_active=on_mascot_active,
        )
        self.peek = Peek()
        self.expanded = Expanded(
            cfg,
            media,
            power,
            on_timer_change=self.pill.show_timer_chip,
            weather=weather,
            system=system,
            brightness=brightness,
            notifier=notifier,
            clipboard=clipboard,
            audio=audio,
            network=network,
            ollama=ollama,
            feeds=feeds,
            privacy=privacy,
            desktop=desktop,
            kdeconnect=kdeconnect,
            productivity=productivity,
            lyrics=lyrics,
            todos=todos,
            calendar=calendar,
            on_dnd_change=on_dnd_change,
            on_settings=on_settings,
        )
        self.expanded.set_size_request(520, -1)

        self.expanded_wrap = Gtk.ScrolledWindow()
        self.expanded_wrap.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.EXTERNAL)
        self.expanded_wrap.set_propagate_natural_width(True)
        self.expanded_wrap.set_propagate_natural_height(True)
        self.expanded_wrap.set_child(self.expanded)

        self.stack.add_named(self.pill, "pill")
        self.stack.add_named(self.peek, "peek")
        self.stack.add_named(self.expanded_wrap, "expanded")

        self._bg_pic = Gtk.Picture()
        self._bg_pic.set_content_fit(Gtk.ContentFit.COVER)
        self._bg_pic.set_can_shrink(True)
        self._bg_pic.set_can_target(False)
        self._bg_pic.set_opacity(0.24)
        self._bg_pic.set_size_request(1, 1)
        self._bg_pic.set_visible(False)

        overlay = Gtk.Overlay()
        overlay.set_child(self._bg_pic)
        overlay.add_overlay(self.stack)
        overlay.set_measure_overlay(self.stack, True)
        self.append(overlay)

        click = Gtk.GestureClick(button=1)
        click.connect("released", self._on_click)
        self.add_controller(click)

        # right click always opens/closes the card
        right = Gtk.GestureClick(button=3)
        right.connect("released", lambda *_: self.toggle())
        self.add_controller(right)

        # horizontal swipe on the pill cycles faces, tide-style;
        # group with left-click so a swipe cancels click cleanly
        drag = Gtk.GestureDrag()
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)
        drag.group(click)

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key)

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self._on_enter)
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

        self.connect("map", lambda *_: self._schedule_bg_frame())
        self.connect("unmap", lambda *_: (self._pause_bg_anim(), self._settle_wake()))
        self.connect("destroy", lambda *_: self._cleanup())
        self.refresh_style()

    def refresh_style(self):
        anim = str(self.cfg.get("animation_style", "fluid"))
        if anim not in VALID_ANIMATIONS:
            anim = "fluid"
        self.stack.set_transition_duration(_ANIM_MS[anim])
        for a in VALID_ANIMATIONS:
            self.remove_css_class(f"anim-{a}")
        self.add_css_class(f"anim-{anim}")

        theme = str(self.cfg.get("theme", "obsidian"))
        if theme not in VALID_THEMES:
            theme = "obsidian"
        for t in VALID_THEMES:
            self.remove_css_class(f"theme-{t}")
        self.add_css_class(f"theme-{theme}")

        style = str(self.cfg.get("island_style", "pill"))
        if style not in VALID_STYLES:
            style = "pill"
        for s in VALID_STYLES:
            self.remove_css_class(f"island--{s}")
        self.add_css_class(f"island--{style}")
        self.pill.set_orientation_style(style)
        self._load_background_media()

    def _pause_bg_anim(self):
        if self._bg_anim_timeout:
            GLib.source_remove(self._bg_anim_timeout)
            self._bg_anim_timeout = 0

    def _stop_bg_anim(self):
        self._pause_bg_anim()
        self._bg_anim_iter = None
        self._bg_media_path = ""

    def _load_background_media(self):
        raw = str(self.cfg.get("background_media", "") or "").strip()
        path = os.path.expanduser(raw) if raw else ""
        if path == self._bg_media_path and (not path or self._bg_pic.get_visible()):
            return
        self._stop_bg_anim()
        if not path or not os.path.isfile(path):
            self._bg_pic.set_paintable(None)
            self._bg_pic.set_visible(False)
            return
        try:
            if os.path.getsize(path) > _MAX_BG_BYTES:
                self._bg_pic.set_visible(False)
                return
            anim = GdkPixbuf.PixbufAnimation.new_from_file(path)
        except (GLib.Error, OSError):
            self._bg_pic.set_paintable(None)
            self._bg_pic.set_visible(False)
            return

        self._bg_media_path = path
        self._bg_pic.set_visible(True)
        if anim.is_static_image():
            pb = anim.get_static_image()
            if pb is not None:
                self._bg_pic.set_paintable(Gdk.Texture.new_for_pixbuf(pb))
            return

        self._bg_anim_iter = anim.get_iter(None)
        if self._bg_anim_iter is not None:
            pb = self._bg_anim_iter.get_pixbuf()
            if pb is not None:
                self._bg_pic.set_paintable(Gdk.Texture.new_for_pixbuf(pb))
            self._schedule_bg_frame()

    def _schedule_bg_frame(self):
        if self._bg_anim_iter is None or self._bg_anim_timeout or not self.get_mapped():
            return
        delay = self._bg_anim_iter.get_delay_time()
        if delay < 0:
            return
        self._bg_anim_timeout = GLib.timeout_add(max(33, delay), self._advance_bg_frame)

    def _advance_bg_frame(self):
        self._bg_anim_timeout = 0
        if self._bg_anim_iter is None or not self.get_mapped():
            return False
        self._bg_anim_iter.advance(None)
        pb = self._bg_anim_iter.get_pixbuf()
        if pb is not None:
            self._bg_pic.set_paintable(Gdk.Texture.new_for_pixbuf(pb))
        self._schedule_bg_frame()
        return False

    def _cleanup(self):
        self._cancel_peek()
        self._clear_peek_queue()
        self._settle_wake()
        self._stop_bg_anim()
        if self._collapse_timeout:
            GLib.source_remove(self._collapse_timeout)
            self._collapse_timeout = 0

    def _clear_wake_timers(self):
        if self._wake_anim_timer:
            GLib.source_remove(self._wake_anim_timer)
            self._wake_anim_timer = 0
        if self._wake_sleep_timer:
            GLib.source_remove(self._wake_sleep_timer)
            self._wake_sleep_timer = 0

    def _apply_wake_margins(self, ms: int, me: int, mt: int, mb: int):
        target = (max(0, ms), max(0, me), max(0, mt), max(0, mb))
        if target == self._wake_margins:
            return
        self._wake_margins = target
        self.pill.set_margin_start(target[0])
        self.pill.set_margin_end(target[1])
        self.pill.set_margin_top(target[2])
        self.pill.set_margin_bottom(target[3])

    def _settle_wake(self):
        self._clear_wake_timers()
        self._awake = False
        self.remove_css_class("island--wake")
        self.pill.remove_css_class("pill--awake")
        self._apply_wake_margins(0, 0, 0, 0)
        self.pill.set_awake(False)
        return False

    def trigger_wake(self):
        """Play a damped spring wobble and enlarge pop-up when the pill wakes."""
        if self.state != "pill":
            return
        self._clear_wake_timers()
        self._awake = True
        self._wake_step = 0
        anim = str(self.cfg.get("animation_style", "fluid"))
        steps = _WAKE_STEPS.get(anim, 12)
        hold_ms = _WAKE_HOLD_MS.get(anim, 3200)
        waterdrop = self.cfg.get("island_style", "pill") == "waterdrop"

        self.add_css_class("island--wake")
        self.pill.add_css_class("pill--awake")
        self._apply_wake_margins(6, 6, 0 if waterdrop else 2, 4 if waterdrop else 2)
        self.pill.trigger_wake()

        def _spring_tick() -> bool:
            self._wake_step += 1
            if self.state != "pill" or not self._awake or self._wake_step >= steps:
                self._wake_anim_timer = 0
                self._apply_wake_margins(0, 0, 0, 0)
                return False
            t = self._wake_step / float(steps)
            env = math.exp(-3.6 * t)
            swell_x = 6.0 * env
            wobble = 3.0 * env * math.sin(4.0 * math.pi * t)
            swell_y = 4.0 * env * max(0.0, math.cos(1.5 * math.pi * t))
            ms = max(0, round(swell_x + wobble))
            me = max(0, round(swell_x - wobble))
            mt = 0 if waterdrop else max(0, round(swell_y * 0.5))
            mb = max(0, round(swell_y)) if waterdrop else max(0, round(swell_y * 0.5))
            self._apply_wake_margins(ms, me, mt, mb)
            return True

        self._wake_anim_timer = GLib.timeout_add(33, _spring_tick)
        self._wake_sleep_timer = GLib.timeout_add(hold_ms, self._settle_wake)

    # -- state -------------------------------------------------------------

    @property
    def state(self) -> str:
        return self.stack.get_visible_child_name() or "pill"

    def collapse(self):
        was_expanded = self.state == "expanded"
        self._cancel_peek()
        self._clear_peek_queue()
        self._settle_wake()
        self.remove_css_class("island--expanded")
        self.stack.set_visible_child_name("pill")
        if was_expanded and self._on_expand_change:
            self._on_expand_change(False)

    def expand(self):
        was_expanded = self.state == "expanded"
        self._cancel_peek()
        self._clear_peek_queue()
        self._settle_wake()
        self.add_css_class("island--expanded")
        self.stack.set_visible_child_name("expanded")
        if not was_expanded and self._on_expand_change:
            self._on_expand_change(True)

    def toggle(self):
        self.collapse() if self.state == "expanded" else self.expand()

    def expand_to_dock(self, which: str):
        """Expand the island card and open a specific dock drawer (ai|clipboard)."""
        self.expand()
        if hasattr(self.expanded, "open_dock_panel"):
            self.expanded.open_dock_panel(which)

    def show_peek(self, setter, *args, priority: int = 0, key: str | None = None):
        """Surface a peek now, or queue it behind the one currently showing."""
        if self.state == "expanded":
            return
        self._peek_seq += 1
        activity = Activity(
            setter, args, priority=priority, key=key, seq=self._peek_seq
        )
        if not self.cfg.get("activity_queue", True):
            self._clear_peek_queue()
            self._show_peek_now(activity)
            return
        if self._peek_timeout == 0 and self._current_peek is None:
            self._show_peek_now(activity)
            return
        if (
            self._current_peek is not None
            and key is not None
            and self._current_peek.key == key
        ):
            self._show_peek_now(activity)
            return
        if self._current_peek is not None and priority > self._current_peek.priority:
            self._peek_queue.push(self._current_peek)
            self._show_peek_now(activity)
            return
        self._peek_queue.push(activity)
        self.pill.show_queue_count(len(self._peek_queue))

    def _show_peek_now(self, activity: Activity):
        self._current_peek = activity
        self.pill.show_queue_count(len(self._peek_queue))
        self._settle_wake()
        activity.setter(*activity.args)
        self.stack.set_visible_child_name("peek")
        self._cancel_peek_timer()
        ms = int(float(self.cfg.get("peek_seconds", 2.2)) * 1000)
        self._peek_timeout = GLib.timeout_add(ms, self._peek_done)

    def _peek_done(self):
        self._peek_timeout = 0
        nxt = self._peek_queue.pop_next()
        self.pill.show_queue_count(len(self._peek_queue))
        if nxt is not None and self.state != "expanded":
            self._show_peek_now(nxt)
            return False
        self._current_peek = None
        if self.state == "peek":
            self.stack.set_visible_child_name("pill")
        return False

    def _cancel_peek_timer(self):
        if self._peek_timeout:
            GLib.source_remove(self._peek_timeout)
            self._peek_timeout = 0

    def _cancel_peek(self):
        self._cancel_peek_timer()
        self._current_peek = None

    def _clear_peek_queue(self):
        self._peek_queue.clear()
        self.pill.show_queue_count(len(self._peek_queue))

    # -- input -------------------------------------------------------------

    def _on_click(self, gesture, n, x, y):
        # clicks on buttons/sliders inside the expanded card never reach
        # here — GTK claims them first
        action = self.cfg.get("click_action", "wake")
        if self.state == "pill":
            if action == "wake":
                if self._awake:
                    self.expand()
                else:
                    self.trigger_wake()
            elif action == "cycle":
                self.pill.cycle(+1)
            else:
                self.expand()
        elif self.state == "peek":
            if action == "expand":
                self.expand()
            else:
                self.collapse()

    def _on_key_pressed(self, _ctrl, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape and self.state == "expanded":
            self.collapse()
            return True
        return False

    def _on_drag_update(self, gesture, dx, dy):
        # claim only when clearly an intentional horizontal swipe
        if self.state == "pill" and abs(dx) > 56 and abs(dx) > abs(dy) * 1.6:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _on_drag_end(self, gesture, dx, dy):
        if self.state == "pill" and abs(dx) > 56 and abs(dx) > abs(dy) * 1.6:
            self.pill.cycle(+1 if dx < 0 else -1)

    def _on_enter(self, motion, x, y):
        self._pointer_inside = True
        if self._collapse_timeout:
            GLib.source_remove(self._collapse_timeout)
            self._collapse_timeout = 0

    def _on_motion(self, motion, x, y):
        if self.state == "pill":
            w = max(1.0, float(self.get_width()))
            h = max(1.0, float(self.get_height()))
            self.pill.look_at((x / w) * 2.0 - 1.0, (y / h) * 2.0 - 1.0)

    def _on_leave(self, motion):
        self._pointer_inside = False
        if not self._global_cursor_active:
            self.pill.look_at(0.0, 0.0)
        if self.state != "expanded":
            return
        if self._collapse_timeout:
            GLib.source_remove(self._collapse_timeout)
        self._collapse_timeout = GLib.timeout_add(
            COLLAPSE_DELAY_MS, self._leave_collapse
        )

    def set_global_cursor_active(self, active: bool):
        self._global_cursor_active = bool(active)
        if not self._global_cursor_active and not self._pointer_inside:
            self.pill.look_at(0.0, 0.0)

    def set_global_cursor(self, nx: float, ny: float):
        if self.state == "pill" and not self._pointer_inside:
            self.pill.look_at(nx, ny)

    def _leave_collapse(self):
        if self.state == "expanded" and self.expanded.is_interacting():
            return True
        self._collapse_timeout = 0
        if self.state == "expanded":
            self.collapse()
        return False

    # -- service wiring ----------------------------------------------------

    def on_media_changed(self):
        p = self.media.active
        if self.cfg.get("ambient_tint", True) and p is not None:
            self.add_css_class("island--media")
        else:
            self.remove_css_class("island--media")
        if p is None:
            self.pill.show_idle()
        else:
            # the pill shows the title only — the artist lives in the card
            self.pill.show_music(p.title, p.art_path, p.status == "Playing")
            if self.lyrics is not None:
                self.lyrics.set_track(
                    p.artist, p.title, p.album, p.length_us // 1_000_000
                )
        if self.state == "expanded":
            self.expanded.refresh_media()
