"""Battery state, health, low-battery alerts, and sub-device batteries via UPower."""

from __future__ import annotations

import os
from typing import ClassVar

from gi.repository import Gio, GLib, GObject

UPOWER = "org.freedesktop.UPower"
DEVICE = "/org/freedesktop/UPower/devices/DisplayDevice"
DEVICE_IFACE = "org.freedesktop.UPower.Device"

# UPower state codes
CHARGING = 1
DISCHARGING = 2
FULL = 4


class PowerService(GObject.Object):
    __gsignals__: ClassVar[dict] = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # True = plugged in, False = unplugged
        "plug-event": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        # severity ("low"/"critical"), message
        "power-alert": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
    }

    def __init__(self):
        super().__init__()
        self.percentage = -1.0
        self.state = 0
        self.available = False
        self.health_pct = -1.0
        self.extra_batteries: list[dict] = []  # [{path, model, percent}]
        self._alerted_level = ""
        self._bus: Gio.DBusConnection | None = None
        self._sub = 0
        self._stopped = False
        self._read_sysfs_health()
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error:
            return
        self._sub = self._bus.signal_subscribe(
            UPOWER,
            "org.freedesktop.DBus.Properties",
            "PropertiesChanged",
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_changed,
        )
        self._bus.call(
            UPOWER,
            DEVICE,
            "org.freedesktop.DBus.Properties",
            "GetAll",
            GLib.Variant("(s)", (DEVICE_IFACE,)),
            GLib.VariantType("(a{sv})"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_get_all,
        )
        self._refresh_extra_devices()

    def stop(self):
        self._stopped = True
        if self._bus is not None and self._sub:
            self._bus.signal_unsubscribe(self._sub)
            self._sub = 0

    def _read_sysfs_health(self):
        try:
            for name in sorted(os.listdir("/sys/class/power_supply")):
                base = f"/sys/class/power_supply/{name}"
                try:
                    with open(f"{base}/type") as f:
                        if f.read().strip() != "Battery":
                            continue
                except OSError:
                    continue
                for full_n, design_n in (
                    ("energy_full", "energy_full_design"),
                    ("charge_full", "charge_full_design"),
                ):
                    try:
                        with (
                            open(f"{base}/{full_n}") as ff,
                            open(f"{base}/{design_n}") as df,
                        ):
                            full = float(ff.read().strip())
                            design = float(df.read().strip())
                        if design > 0:
                            self.health_pct = max(
                                0.0, min(100.0, 100.0 * full / design)
                            )
                            return
                    except (OSError, ValueError):
                        continue
        except OSError:
            pass

    def _refresh_extra_devices(self):
        if self._bus is None:
            return
        self._bus.call(
            UPOWER,
            "/org/freedesktop/UPower",
            UPOWER,
            "EnumerateDevices",
            None,
            GLib.VariantType("(ao)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_devices,
        )

    def _on_devices(self, bus, res):
        if self._stopped:
            return
        try:
            paths = bus.call_finish(res).unpack()[0]
        except GLib.Error:
            return
        for path in paths:
            if path == DEVICE or "battery_BAT" in path or "line_power" in path:
                continue
            bus.call(
                UPOWER,
                path,
                "org.freedesktop.DBus.Properties",
                "GetAll",
                GLib.Variant("(s)", (DEVICE_IFACE,)),
                GLib.VariantType("(a{sv})"),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
                lambda b, r, p=path: self._on_extra_device(b, r, p),
            )

    def _on_extra_device(self, bus, res, path: str):
        if self._stopped:
            return
        try:
            props = bus.call_finish(res).unpack()[0]
        except GLib.Error:
            return
        if not props.get("IsPresent", False):
            return
        model = str(props.get("Model") or os.path.basename(path)).strip()
        pct = float(props.get("Percentage", -1.0))
        if pct < 0:
            return
        self.extra_batteries = [d for d in self.extra_batteries if d["path"] != path]
        self.extra_batteries.append(
            {"path": path, "model": model, "percent": round(pct)}
        )
        self.emit("changed")

    def _on_get_all(self, bus, res):
        if self._stopped:
            return
        try:
            props = bus.call_finish(res).unpack()[0]
        except GLib.Error:
            return
        # IsPresent is False on desktops without a battery
        self.available = bool(props.get("IsPresent", False))
        cap = props.get("Capacity")
        if self.health_pct < 0 and isinstance(cap, (int, float)) and cap > 0:
            self.health_pct = max(0.0, min(100.0, float(cap)))
        self._apply(props, initial=True)

    def _on_changed(self, bus, sender, path, iface, signal, params):
        if self._stopped:
            return
        try:
            _iface, changed, _inv = params.unpack()
        except (GLib.Error, TypeError, ValueError):
            return
        if path == DEVICE:
            self._apply(changed, initial=False)
        elif "Percentage" in changed:
            for item in self.extra_batteries:
                if item["path"] == path:
                    item["percent"] = round(float(changed["Percentage"]))
                    self.emit("changed")
                    break

    def _apply(self, props: dict, initial: bool):
        old_pct = self.percentage
        old_state = self.state
        if "Percentage" in props:
            self.percentage = float(props["Percentage"])
        if "State" in props:
            self.state = int(props["State"])
        if not initial and old_state != self.state:
            if self.state == CHARGING:
                self._alerted_level = ""
                self.emit("plug-event", True)
            elif self.state == DISCHARGING and old_state in (CHARGING, FULL):
                self.emit("plug-event", False)
        if (
            not initial
            and self.available
            and self.state == DISCHARGING
            and self.percentage >= 0
        ):
            if self.percentage <= 10 and self._alerted_level != "critical":
                self._alerted_level = "critical"
                self.emit(
                    "power-alert",
                    "critical",
                    f"Critical battery ({self.percentage:.0f}%)",
                )
            elif self.percentage <= 20 and not self._alerted_level:
                self._alerted_level = "low"
                self.emit(
                    "power-alert",
                    "low",
                    f"Low battery ({self.percentage:.0f}%)",
                )
        elif self.percentage > 25:
            self._alerted_level = ""
        if initial or self.percentage != old_pct or self.state != old_state:
            self.emit("changed")

    @property
    def charging(self) -> bool:
        return self.state in (CHARGING, FULL)
