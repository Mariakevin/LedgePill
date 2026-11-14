"""Brightness monitor and controller for Linux displays.

On KDE, use PowerDevil's D-Bus API so brightness changes work without sysfs
permissions and stay in sync with the desktop. Other desktops fall back to
`brightnessctl` or `light` and monitor sysfs for external changes.
"""

from __future__ import annotations

import glob
import re
import subprocess
from typing import ClassVar

from gi.repository import Gio, GLib, GObject

__all__ = ["BrightnessService"]

_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_KDE_SCREEN_SERVICE = "org.kde.ScreenBrightness"
_KDE_SCREEN_PATH = "/org/kde/ScreenBrightness"
_KDE_SCREEN_IFACE = "org.kde.ScreenBrightness"
_KDE_DISPLAY_IFACE = "org.kde.ScreenBrightness.Display"
_KDE_LEGACY_SERVICE = "org.kde.Solid.PowerManagement"
_KDE_LEGACY_PATH = "/org/kde/Solid/PowerManagement/Actions/BrightnessControl"
_KDE_LEGACY_IFACE = "org.kde.Solid.PowerManagement.Actions.BrightnessControl"


class BrightnessService(GObject.Object):
    __gsignals__: ClassVar[dict] = {
        "brightness": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(self):
        super().__init__()
        self.percent = 0
        self.available = False
        self._monitors: list[Gio.FileMonitor] = []
        self._watch_timer = 0
        self._set_busy = False
        self._pending_set: int | None = None
        self._ignore_until = 0.0
        self._bus: Gio.DBusConnection | None = None
        self._subscriptions: list[int] = []
        self._kde_api = ""
        self._kde_display = ""
        self._kde_path = ""
        self._kde_max = 0
        self._backend = self._detect_backend()
        if self._backend == "kde":
            self._subscribe_kde()
            self._query_kde()
        elif self._backend is not None:
            self._query()
            self._watch_sysfs()

    def stop(self):
        if self._watch_timer:
            GLib.source_remove(self._watch_timer)
            self._watch_timer = 0
        for mon in self._monitors:
            mon.cancel()
        self._monitors.clear()
        if self._bus is not None:
            for subscription in self._subscriptions:
                self._bus.signal_unsubscribe(subscription)
        self._subscriptions.clear()

    @staticmethod
    def parse_percent(value) -> int:
        """Extract a 0-100 brightness percentage from CLI output."""
        if value is None:
            return 0
        text = str(value).strip()
        if not text:
            return 0

        # Prefer an explicit percentage sign when present.
        match = _PERCENT_RE.search(text)
        if match:
            return max(0, min(100, round(float(match.group(1)))))

        # Fall back to a bare number, but only when it already looks like a
        # percentage (<= 100). Raw counts (e.g. "300/1023") are ignored so we
        # never report nonsense.
        match = re.search(r"(\d+(?:\.\d+)?)", text)
        if match:
            numeric = float(match.group(1))
            if 0 <= numeric <= 100:
                return max(0, min(100, round(numeric)))
        return 0

    def _detect_backend(self):
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            self._bus = None
        if self._bus is not None:
            if self._probe_kde_screen_api():
                self._kde_api = "screen"
                return "kde"
            if self._probe_kde_legacy_api():
                self._kde_api = "legacy"
                return "kde"

        for cmd in (["brightnessctl", "-m"], ["light", "-G"]):
            try:
                subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=0.5,
                )
                return list(cmd)
            except (OSError, subprocess.SubprocessError):
                continue
        return None

    def _call_sync(
        self,
        service: str,
        path: str,
        interface: str,
        method: str,
        parameters: GLib.Variant | None = None,
        reply_type: GLib.VariantType | None = None,
    ) -> GLib.Variant:
        if self._bus is None:
            raise RuntimeError("Session bus unavailable")
        return self._bus.call_sync(
            service,
            path,
            interface,
            method,
            parameters,
            reply_type,
            Gio.DBusCallFlags.NONE,
            500,
            None,
        )

    @staticmethod
    def _variant_value(properties: dict, name: str, default=None):
        value = properties.get(name)
        if value is None:
            return default
        return value.unpack() if isinstance(value, GLib.Variant) else value

    def _probe_kde_screen_api(self) -> bool:
        try:
            reply = self._call_sync(
                _KDE_SCREEN_SERVICE,
                _KDE_SCREEN_PATH,
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", (_KDE_SCREEN_IFACE, "DisplaysDBusNames")),
                GLib.VariantType.new("(v)"),
            )
            names = reply.unpack()[0]
            displays = []
            for name in names:
                path = f"{_KDE_SCREEN_PATH}/{name}"
                try:
                    props_reply = self._call_sync(
                        _KDE_SCREEN_SERVICE,
                        path,
                        "org.freedesktop.DBus.Properties",
                        "GetAll",
                        GLib.Variant("(s)", (_KDE_DISPLAY_IFACE,)),
                        GLib.VariantType.new("(a{sv})"),
                    )
                    props = props_reply.unpack()[0]
                    maximum = int(self._variant_value(props, "MaxBrightness", 0) or 0)
                    current = int(self._variant_value(props, "Brightness", -1))
                    if maximum > 0 and current >= 0:
                        displays.append(
                            (
                                bool(self._variant_value(props, "IsInternal", False)),
                                str(name),
                                path,
                                maximum,
                                current,
                            )
                        )
                except (GLib.Error, IndexError, TypeError, ValueError):
                    continue
            if not displays:
                return False
            internal = next((display for display in displays if display[0]), None)
            _is_internal, self._kde_display, self._kde_path, self._kde_max, _value = (
                internal or displays[0]
            )
            return True
        except (GLib.Error, IndexError, TypeError, ValueError):
            return False

    def _probe_kde_legacy_api(self) -> bool:
        try:
            current = self._call_sync(
                _KDE_LEGACY_SERVICE,
                _KDE_LEGACY_PATH,
                _KDE_LEGACY_IFACE,
                "brightness",
                GLib.Variant("()", ()),
                GLib.VariantType.new("(i)"),
            ).unpack()[0]
            maximum = self._call_sync(
                _KDE_LEGACY_SERVICE,
                _KDE_LEGACY_PATH,
                _KDE_LEGACY_IFACE,
                "brightnessMax",
                GLib.Variant("()", ()),
                GLib.VariantType.new("(i)"),
            ).unpack()[0]
            if int(maximum) <= 0 or int(current) < 0:
                return False
            self._kde_max = int(maximum)
            return True
        except (GLib.Error, IndexError, TypeError, ValueError):
            return False

    def _subscribe_kde(self):
        if self._bus is None:
            return
        if self._kde_api == "screen":
            signals = (
                (
                    _KDE_SCREEN_SERVICE,
                    _KDE_SCREEN_IFACE,
                    "BrightnessChanged",
                    _KDE_SCREEN_PATH,
                ),
                (
                    _KDE_SCREEN_SERVICE,
                    _KDE_SCREEN_IFACE,
                    "BrightnessRangeChanged",
                    _KDE_SCREEN_PATH,
                ),
                (
                    _KDE_SCREEN_SERVICE,
                    _KDE_SCREEN_IFACE,
                    "DisplayAdded",
                    _KDE_SCREEN_PATH,
                ),
                (
                    _KDE_SCREEN_SERVICE,
                    _KDE_SCREEN_IFACE,
                    "DisplayRemoved",
                    _KDE_SCREEN_PATH,
                ),
            )
            callback = self._on_kde_screen_signal
        else:
            signals = (
                (
                    _KDE_LEGACY_SERVICE,
                    _KDE_LEGACY_IFACE,
                    "brightnessChanged",
                    _KDE_LEGACY_PATH,
                ),
                (
                    _KDE_LEGACY_SERVICE,
                    _KDE_LEGACY_IFACE,
                    "brightnessMaxChanged",
                    _KDE_LEGACY_PATH,
                ),
            )
            callback = self._on_kde_legacy_signal
        for service, interface, signal, path in signals:
            subscription = self._bus.signal_subscribe(
                service,
                interface,
                signal,
                path,
                None,
                Gio.DBusSignalFlags.NONE,
                callback,
                None,
            )
            self._subscriptions.append(subscription)

    def _on_kde_screen_signal(
        self, _bus, _sender, _path, _interface, signal, parameters, _data
    ):
        values = parameters.unpack()
        if signal == "BrightnessChanged" and values[0] == self._kde_display:
            self._apply_raw_brightness(values[1], self._kde_max)
        elif signal == "BrightnessRangeChanged" and values[0] == self._kde_display:
            self._kde_max = int(values[1])
            self._apply_raw_brightness(values[2], self._kde_max)
        elif signal in {"DisplayAdded", "DisplayRemoved"}:
            if self._probe_kde_screen_api():
                self._query_kde()
            elif self._kde_display:
                self._kde_display = ""
                self._kde_path = ""
                self._kde_max = 0
                self.available = False
                self.emit("brightness", self.percent)

    def _on_kde_legacy_signal(
        self, _bus, _sender, _path, _interface, signal, parameters, _data
    ):
        if signal == "brightnessChanged":
            self._apply_raw_brightness(parameters.unpack()[0], self._kde_max)
        else:
            self._query_kde()

    def _apply_raw_brightness(self, value: int, maximum: int):
        if maximum <= 0:
            return
        percent = round(max(0, min(maximum, int(value))) * 100 / maximum)
        changed = percent != self.percent
        was_available = self.available
        self.percent = percent
        self.available = True
        if changed or not was_available:
            self.emit("brightness", percent)

    def _query_kde(self):
        try:
            if self._kde_api == "screen":
                reply = self._call_sync(
                    _KDE_SCREEN_SERVICE,
                    self._kde_path,
                    "org.freedesktop.DBus.Properties",
                    "GetAll",
                    GLib.Variant("(s)", (_KDE_DISPLAY_IFACE,)),
                    GLib.VariantType.new("(a{sv})"),
                )
                props = reply.unpack()[0]
                self._kde_max = int(self._variant_value(props, "MaxBrightness", 0) or 0)
                current = int(self._variant_value(props, "Brightness", -1))
            else:
                current = self._call_sync(
                    _KDE_LEGACY_SERVICE,
                    _KDE_LEGACY_PATH,
                    _KDE_LEGACY_IFACE,
                    "brightness",
                    GLib.Variant("()", ()),
                    GLib.VariantType.new("(i)"),
                ).unpack()[0]
            self._apply_raw_brightness(current, self._kde_max)
        except (GLib.Error, IndexError, TypeError, ValueError):
            self.available = False

    def _watch_sysfs(self):
        for path in sorted(glob.glob("/sys/class/backlight/*/brightness")):
            try:
                mon = Gio.File.new_for_path(path).monitor_file(
                    Gio.FileMonitorFlags.NONE, None
                )
                mon.connect("changed", self._on_sysfs_changed)
                self._monitors.append(mon)
            except GLib.Error:
                continue

    def _on_sysfs_changed(self, _mon, _file, _other, event):
        if event not in (
            Gio.FileMonitorEvent.CHANGED,
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
        ):
            return
        if GLib.get_monotonic_time() < self._ignore_until:
            return
        if not self._watch_timer:
            self._watch_timer = GLib.timeout_add(60, self._query_async_once)

    def _query_async_once(self):
        self._watch_timer = 0
        if not self._backend:
            return False
        if self._backend == "kde":
            self._query_kde()
            return False
        try:
            proc = Gio.Subprocess.new(
                self._backend,
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            )
        except GLib.Error:
            return False
        proc.communicate_utf8_async(None, None, self._on_query_out)
        return False

    def _on_query_out(self, proc, res):
        try:
            _ok, out, err = proc.communicate_utf8_finish(res)
        except GLib.Error:
            return
        if not proc.get_successful():
            return
        self._apply_output((out or "") + (err or ""), emit_on_first=False)

    def _apply_output(self, text: str, emit_on_first: bool):
        percent = self.parse_percent(text)
        changed = percent != self.percent
        was_available = self.available
        self.available = True
        self.percent = percent
        if changed or (emit_on_first and not was_available):
            self.emit("brightness", percent)

    def _query(self):
        if self._backend == "kde":
            self._query_kde()
            return
        if not self._backend:
            return
        try:
            proc = subprocess.run(
                self._backend,
                capture_output=True,
                text=True,
                check=True,
                timeout=0.5,
            )
        except (OSError, subprocess.SubprocessError):
            self.available = False
            return
        stdout = (proc.stdout or "") + (proc.stderr or "")
        self._apply_output(stdout, emit_on_first=True)

    def set_percent(self, percent: int) -> None:
        if not self._backend or not self.available:
            return
        value = max(0, min(100, int(percent)))
        if value == self.percent and not self._set_busy and self._pending_set is None:
            return
        self._pending_set = value
        if not self._set_busy:
            self._flush_set()

    def _flush_set(self):
        if self._pending_set is None or not self._backend:
            self._set_busy = False
            return
        value = self._pending_set
        self._pending_set = None
        self._set_busy = True
        self._ignore_until = GLib.get_monotonic_time() + 250_000
        if self._backend == "kde":
            self._set_kde(value)
            return
        if self._backend[0] == "brightnessctl":
            cmd = ["brightnessctl", "set", f"{value}%"]
        else:
            cmd = ["light", "-S", str(value)]
        try:
            proc = Gio.Subprocess.new(cmd, Gio.SubprocessFlags.NONE)
        except GLib.Error:
            self._set_busy = False
            return
        proc.wait_check_async(None, lambda p, r: self._on_set_done(p, r, value))

    def _set_kde(self, percent: int):
        if self._bus is None:
            self._set_busy = False
            return
        if self._kde_api == "screen":
            service = _KDE_SCREEN_SERVICE
            path = self._kde_path
            interface = _KDE_DISPLAY_IFACE
            method = "SetBrightness"
            value = round(percent * self._kde_max / 100)
            parameters = GLib.Variant("(iu)", (value, 0))
        else:
            service = _KDE_LEGACY_SERVICE
            path = _KDE_LEGACY_PATH
            interface = _KDE_LEGACY_IFACE
            method = "setBrightnessSilent"
            value = round(percent * self._kde_max / 100)
            parameters = GLib.Variant("(i)", (value,))
        try:
            self._bus.call(
                service,
                path,
                interface,
                method,
                parameters,
                None,
                Gio.DBusCallFlags.NONE,
                1000,
                None,
                self._on_kde_set_done,
                percent,
            )
        except GLib.Error:
            self._set_busy = False
            self._query_kde()
            self._flush_set()

    def _on_kde_set_done(self, bus, res, percent: int):
        try:
            bus.call_finish(res)
            changed = percent != self.percent
            self.percent = percent
            self.available = True
            if changed:
                self.emit("brightness", percent)
        except GLib.Error:
            self._query_kde()
        self._set_busy = False
        self._flush_set()

    def _on_set_done(self, proc, res, value: int):
        try:
            ok = proc.wait_check_finish(res)
        except GLib.Error:
            ok = False
        if ok:
            changed = value != self.percent
            self.percent = value
            self.available = True
            if changed:
                self.emit("brightness", value)
        else:
            self._query_async_once()
        self._flush_set()
