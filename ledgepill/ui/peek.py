"""The peek capsule — a short-lived widening of the pill for one event:
volume change, charger plug, a notification, a call, privacy or network switch."""

from __future__ import annotations

import os

from gi.repository import Gdk, GLib, Gtk, Pango

from ledgepill.ui.draw import bluetooth_icon, brightness_icon, pick_icon, volume_icon


class Peek(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.add_css_class("peek")
        self.set_valign(Gtk.Align.CENTER)

        icon_wrap = Gtk.Box()
        icon_wrap.add_css_class("peek-icon-badge")
        icon_wrap.set_valign(Gtk.Align.CENTER)
        self.icon = Gtk.Image()
        self.icon.set_pixel_size(18)
        self.icon.add_css_class("peek-icon")
        self.icon.set_halign(Gtk.Align.CENTER)
        self.icon.set_valign(Gtk.Align.CENTER)
        self.icon.set_hexpand(True)
        self.icon.set_vexpand(True)
        icon_wrap.append(self.icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text_box.set_valign(Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0)
        self.title.add_css_class("peek-title")
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.title.set_max_width_chars(30)
        self.body = Gtk.Label(xalign=0)
        self.body.add_css_class("peek-body")
        self.body.set_ellipsize(Pango.EllipsizeMode.END)
        self.body.set_max_width_chars(38)
        text_box.append(self.title)
        text_box.append(self.body)

        self.level = Gtk.LevelBar()
        self.level.set_min_value(0)
        self.level.set_max_value(100)
        self.level.set_valign(Gtk.Align.CENTER)
        # kill the default warning/error color breakpoints
        self.level.remove_offset_value("low")
        self.level.remove_offset_value("high")
        self.level.remove_offset_value("full")

        self.append(icon_wrap)
        self.append(text_box)
        self.append(self.level)

    def _clear_moods(self):
        for cls in ("peek--charging", "peek--call", "peek--privacy", "peek--power"):
            self.remove_css_class(cls)

    def _set(
        self,
        icon: str,
        title: str,
        body: str = "",
        level: float | None = None,
        mood: str = "",
    ):
        self._clear_moods()
        if mood:
            self.add_css_class(mood)
        self.icon.set_from_icon_name(icon)
        self.title.set_label(title)
        self.body.set_label(body)
        self.body.set_visible(bool(body))
        self.level.set_visible(level is not None)
        if level is not None:
            self.level.set_value(max(0.0, min(100.0, float(level))))

    def show_volume(self, percent: int, muted: bool):
        if muted:
            self._set(volume_icon(0, True), "Muted", level=0)
        else:
            self._set(volume_icon(percent, False), f"{percent}%", level=percent)

    def show_battery(self, percent: float, plugged: bool):
        if plugged:
            icon = pick_icon(
                self,
                "battery-charging-symbolic",
                "battery-full-charging-symbolic",
                "battery-symbolic",
            )
            self._set(
                icon,
                "Charging",
                f"{percent:.0f}%",
                level=percent,
                mood="peek--charging",
            )
        else:
            icon = pick_icon(
                self,
                "battery-discharging-symbolic",
                "battery-good-symbolic",
                "battery-symbolic",
            )
            self._set(icon, "On battery", f"{percent:.0f}%")

    def show_power_alert(self, severity: str, message: str):
        icon = (
            "battery-empty-symbolic"
            if severity == "critical"
            else "battery-low-symbolic"
        )
        self._set(icon, message, "Connect a charger", mood="peek--power")

    def show_brightness(self, percent: int):
        self._set(brightness_icon(percent), "Brightness", f"{percent}%", level=percent)

    def show_notification(self, app: str, summary: str, body: str, icon: str):
        disp = self.get_display() or Gdk.Display.get_default()
        theme = Gtk.IconTheme.get_for_display(disp) if disp else None
        name = "dialog-information-symbolic"
        file_path = ""
        if icon:
            candidate_path = icon
            if icon.startswith("file://"):
                try:
                    candidate_path = GLib.filename_from_uri(icon)[0]
                except GLib.Error:
                    candidate_path = ""
            if candidate_path and os.path.isfile(candidate_path):
                file_path = candidate_path
        if not file_path and theme is not None:
            for candidate in (icon, app.lower().replace(" ", "-")):
                if candidate and theme.has_icon(candidate):
                    name = candidate
                    break
        self._set(name, summary or app, body)
        if file_path:
            self.icon.set_from_file(file_path)

    def show_call(self, app: str, summary: str, body: str):
        self._set(
            "call-start-symbolic",
            summary or app or "Incoming call",
            body,
            mood="peek--call",
        )

    def show_privacy(self, kind: str, label: str):
        icons = {
            "mic": "audio-input-microphone-symbolic",
            "camera": "camera-web-symbolic",
            "screenshare": "video-display-symbolic",
            "recording": "media-record-symbolic",
        }
        self._set(
            icons.get(kind, "dialog-warning-symbolic"), label, mood="peek--privacy"
        )

    def show_keyboard_layout(self, layout: str):
        self._set("input-keyboard-symbolic", "Keyboard layout", layout)

    def show_activity(self, title: str, subtitle: str = ""):
        self._set("applications-engineering-symbolic", title, subtitle)

    def show_calendar_alert(self, summary: str, minutes: int, location: str = ""):
        sub = "Starting now" if minutes <= 0 else f"In {minutes} min"
        if location:
            sub = f"{sub} · {location}"
        self._set("x-office-calendar-symbolic", summary, sub)

    def show_agent_event(self, state: str):
        labels = {
            "done": "Work complete",
            "error": "Work failed",
            "attention": "Action needed",
        }
        if state not in labels:
            return
        mood = "peek--privacy" if state in ("attention", "error") else ""
        self._set("utilities-terminal-symbolic", labels[state], mood=mood)

    def show_bluetooth(self, device: str, connected: bool):
        icon = bluetooth_icon(self)
        if connected:
            self._set(icon, device, "Connected")
        else:
            self._set(icon, device or "Bluetooth", "Disconnected")

    def show_network(self, name: str, kind: str, connected: bool):
        if not connected:
            self._set("network-offline-symbolic", "Disconnected")
        elif "vpn" in kind or "wireguard" in kind or "tun" in kind:
            self._set("network-vpn-symbolic", name, "VPN")
        elif "wireless" in kind:
            self._set("network-wireless-symbolic", name, "Wi-Fi")
        else:
            self._set("network-wired-symbolic", name, "Ethernet")
