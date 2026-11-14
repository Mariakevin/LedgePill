"""Hand-drawn cairo widgets and shared icon helpers.

All Cairo widgets are strictly demand-driven: EQ bars and mascot blinking run
timers only while mapped/active; the battery ring redraws only on change.
"""

from __future__ import annotations

import math

from gi.repository import Gdk, GLib, Gtk


class EqBars(Gtk.DrawingArea):
    """Five bouncing bars, the universal 'something is playing' glyph.

    With cava feeding real spectrum levels through `feed()`, the bars follow
    the actual music; without it they fall back to a gentle sine animation.
    """

    BAR_W = 3.0
    GAP = 2.5
    N = 5

    def __init__(self, color=(1.0, 1.0, 1.0)):
        super().__init__()
        self.color = color
        self._phase = 0.0
        self._playing = False
        self._timer = 0
        self._levels: list[float] | None = None
        self._external = False
        width = int(self.N * self.BAR_W + (self.N - 1) * self.GAP)
        self.set_content_width(width)
        self.set_content_height(14)
        self.set_draw_func(self._draw)
        self.connect("map", lambda *_: self._sync_timer())
        self.connect("unmap", lambda *_: self._sync_timer())

    def set_playing(self, playing: bool):
        self._playing = playing
        self._sync_timer()
        self.queue_draw()

    def set_external(self, external: bool):
        """True while a live level source (cava) is driving the bars."""
        self._external = external
        self._levels = None
        self._sync_timer()

    def feed(self, levels: list[float]):
        self._levels = levels
        self.queue_draw()

    def _sync_timer(self):
        want = self._playing and self.get_mapped() and not self._external
        if want and not self._timer:
            self._timer = GLib.timeout_add(80, self._tick)
        elif not want and self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _tick(self):
        self._phase += 0.55
        self.queue_draw()
        return True

    def _draw(self, area, cr, w, h):
        cr.set_source_rgb(*self.color)
        for i in range(self.N):
            if self._playing and self._external and self._levels:
                raw = self._levels[i] if i < len(self._levels) else 0.0
                level = max(0.0, min(1.0, float(raw)))
                frac = 0.15 + 0.85 * level
            elif self._playing:
                frac = 0.35 + 0.65 * abs(math.sin(self._phase + i * 1.1))
            else:
                frac = 0.25
            bh = h * frac
            x = i * (self.BAR_W + self.GAP)
            y = (h - bh) / 2
            _rounded_rect(cr, x, y, self.BAR_W, bh, self.BAR_W / 2)
            cr.fill()


class MascotEyes(Gtk.DrawingArea):
    """Expressive reactive eyes that blink occasionally and track the cursor."""

    def __init__(self, accent=(1.0, 0.62, 0.04)):
        super().__init__()
        self.accent = accent
        self._gx = 0.0
        self._gy = 0.0
        self._blink = False
        self._timer = 0
        self._idle_timer = 0
        self._mood = "normal"  # "normal" | "sleepy" | "working" | "attention"
        self._awake = False
        self._wake_level = 0.0
        self._wake_step = 0
        self._wake_timer = 0
        self.set_content_width(24)
        self.set_content_height(14)
        self.set_draw_func(self._draw)
        self.connect("map", lambda *_: self._sync_timer())
        self.connect("unmap", lambda *_: self._sync_timer())

    def set_mood(self, mood: str):
        """Set mascot state: normal, sleepy, working (AI busy), or attention."""
        target = (
            mood if mood in ("normal", "sleepy", "working", "attention") else "normal"
        )
        if target == self._mood:
            return
        self._mood = target
        if target in ("working", "attention"):
            self._stop_idle_timer()
            self._blink = False
        elif target == "normal":
            self._arm_idle_timer()
        if self.get_mapped():
            self.queue_draw()

    def _stop_idle_timer(self):
        if self._idle_timer:
            GLib.source_remove(self._idle_timer)
            self._idle_timer = 0

    def _arm_idle_timer(self):
        self._stop_idle_timer()
        if self.get_mapped() and self.get_visible() and self._mood == "normal":
            self._idle_timer = GLib.timeout_add_seconds(20, self._enter_sleepy)

    def _enter_sleepy(self) -> bool:
        self._idle_timer = 0
        if (
            self._mood == "normal"
            and not self._awake
            and self.get_mapped()
            and self.get_visible()
        ):
            self._mood = "sleepy"
            self.queue_draw()
        return False

    def trigger_wake(self):
        """Animate a brief spring wake-up dilation & accent halo on the eyes."""
        self._stop_wake_timer()
        if self._mood == "sleepy":
            self._mood = "normal"
        self._arm_idle_timer()
        self._blink = False
        self._awake = True
        self._wake_step = 0
        self._wake_level = 1.0
        if self.get_mapped():
            self.queue_draw()
            self._wake_timer = GLib.timeout_add(30, self._wake_tick)

    def set_awake(self, awake: bool):
        self._awake = bool(awake)
        if not self._awake:
            self._stop_wake_timer()
            self._wake_level = 0.0
            if self.get_mapped():
                self.queue_draw()

    def _stop_wake_timer(self):
        if self._wake_timer:
            GLib.source_remove(self._wake_timer)
            self._wake_timer = 0

    def _wake_tick(self) -> bool:
        self._wake_step += 1
        t = self._wake_step / 14.0
        if t >= 1.0:
            self._wake_timer = 0
            self._wake_level = 0.35 if self._awake else 0.0
            if self.get_mapped():
                self.queue_draw()
            return False
        # damped cosine bounce: peaks outward then settles smoothly
        wave = max(-0.2, math.exp(-3.4 * t) * math.cos(2.6 * math.pi * t))
        base = 0.35 if self._awake else 0.0
        self._wake_level = max(0.0, base + 0.75 * abs(wave))
        if self.get_mapped():
            self.queue_draw()
        return True

    def look_at(self, nx: float, ny: float):
        gx = max(-1.0, min(1.0, float(nx)))
        gy = max(-1.0, min(1.0, float(ny)))
        moved = abs(gx - self._gx) > 0.002 or abs(gy - self._gy) > 0.002
        if not moved and self._mood != "sleepy":
            return
        if self._mood == "sleepy" and (abs(gx) > 0.01 or abs(gy) > 0.01):
            self._mood = "normal"
        if moved:
            self._arm_idle_timer()
        self._gx = gx
        self._gy = gy
        if self.get_mapped():
            self.queue_draw()

    def _sync_timer(self):
        want = self.get_mapped() and self.get_visible()
        if want and not self._timer:
            self._timer = GLib.timeout_add_seconds(4, self._blink_once)
            self._arm_idle_timer()
        elif not want:
            if self._timer:
                GLib.source_remove(self._timer)
                self._timer = 0
            self._stop_idle_timer()
            self._stop_wake_timer()

    def _blink_once(self):
        if self._wake_timer or self._mood == "sleepy":
            return True
        self._blink = True
        self.queue_draw()
        GLib.timeout_add(140, self._unblink)
        return True

    def _unblink(self):
        self._blink = False
        if self.get_mapped():
            self.queue_draw()
        return False

    def _draw(self, area, cr, w, h):
        r, g, b = self.accent
        cx1, cx2, cy = w * 0.30, w * 0.70, h * 0.50
        lvl = self._wake_level if self._awake or self._wake_timer else 0.0
        if self._mood in ("working", "attention") and lvl < 0.35:
            lvl = 0.35
        sclera_r = 4.2 + 0.55 * lvl
        iris_r = 2.0 + 0.25 * lvl
        pupil_r = 1.0 + 0.18 * lvl
        ring_col = (0.96, 0.62, 0.04) if self._mood == "attention" else (r, g, b)
        for cx in (cx1, cx2):
            if self._mood == "sleepy":
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.55)
                cr.set_line_width(1.6)
                cr.set_line_cap(1)
                cr.arc(cx, cy + 0.5, 3.6, 0.15 * math.pi, 0.85 * math.pi)
                cr.stroke()
                continue
            if lvl > 0.05 and not self._blink:
                cr.set_source_rgba(*ring_col, min(0.55, 0.42 * lvl))
                cr.set_line_width(1.2)
                cr.arc(cx, cy, sclera_r + 1.1, 0, 2 * math.pi)
                cr.stroke()
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.92)
            if self._blink:
                _rounded_rect(cr, cx - 3.5, cy - 0.8, 7.0, 1.6, 0.8)
                cr.fill()
            else:
                cr.arc(cx, cy, sclera_r, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgb(*ring_col)
                cr.arc(
                    cx + self._gx * 1.4,
                    cy + self._gy * 1.2,
                    iris_r,
                    0,
                    2 * math.pi,
                )
                cr.fill()
                cr.set_source_rgb(0.05, 0.05, 0.06)
                cr.arc(
                    cx + self._gx * 1.6,
                    cy + self._gy * 1.4,
                    pupil_r,
                    0,
                    2 * math.pi,
                )
                cr.fill()


class BatteryRing(Gtk.DrawingArea):
    """Charge level as a ring, accent-colored when charging."""

    def __init__(self, accent=(1.0, 0.62, 0.04), size: int = 40):
        super().__init__()
        self.accent = accent
        self.percent = 0.0
        self.charging = False
        sz = max(16, int(size))
        self.set_content_width(sz)
        self.set_content_height(sz)
        self.set_draw_func(self._draw)

    def update(self, percent: float, charging: bool):
        self.percent = max(0.0, min(100.0, float(percent)))
        self.charging = charging
        self.queue_draw()

    def _draw(self, area, cr, w, h):
        cx, cy = w / 2, h / 2
        lw = max(2.2, min(w, h) * 0.095)
        radius = min(w, h) / 2 - lw
        cr.set_line_width(lw)
        cr.set_line_cap(1)  # ROUND
        # track
        cr.set_source_rgba(1, 1, 1, 0.13)
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.stroke()
        # level, from 12 o'clock
        if self.charging:
            cr.set_source_rgb(*self.accent)
        elif self.percent <= 20:
            cr.set_source_rgb(1.0, 0.27, 0.23)
        else:
            cr.set_source_rgb(0.20, 0.84, 0.29)
        frac = max(0.01, min(1.0, self.percent / 100))
        start = -math.pi / 2
        cr.arc(cx, cy, radius, start, start + 2 * math.pi * frac)
        cr.stroke()


def pick_icon(widget, *names: str) -> str:
    """First icon name the current theme actually ships (breeze and adwaita
    disagree on several battery/bluetooth names)."""
    disp = widget.get_display() or Gdk.Display.get_default()
    if disp is None:
        return names[-1]
    theme = Gtk.IconTheme.get_for_display(disp)
    for name in names:
        if theme.has_icon(name):
            return name
    return names[-1]


def volume_icon(percent: int, muted: bool) -> str:
    if muted:
        return "audio-volume-muted-symbolic"
    if percent < 34:
        return "audio-volume-low-symbolic"
    if percent < 67:
        return "audio-volume-medium-symbolic"
    return "audio-volume-high-symbolic"


def brightness_icon(percent: int) -> str:
    if percent < 10:
        return "display-brightness-off-symbolic"
    if percent < 35:
        return "display-brightness-low-symbolic"
    if percent < 70:
        return "display-brightness-medium-symbolic"
    return "display-brightness-high-symbolic"


def bluetooth_icon(widget) -> str:
    return pick_icon(
        widget,
        "bluetooth-symbolic",
        "network-bluetooth-symbolic",
        "bluetooth-active-symbolic",
    )


def weather_icon(widget, icon: str) -> str:
    return pick_icon(
        widget,
        icon.replace("-symbolic", ""),
        icon,
        "weather-few-clouds",
    )


def _rounded_rect(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()
