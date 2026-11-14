"""KDE Connect Session D-Bus integration."""

from __future__ import annotations

import logging
import os
from typing import ClassVar

from gi.repository import Gio, GLib, GObject

KDECONNECT = "org.kde.kdeconnect"
DAEMON_PATH = "/modules/kdeconnect"
DAEMON_IFACE = "org.kde.kdeconnect.daemon"
DEV_IFACE = "org.kde.kdeconnect.device"
BATT_IFACE = "org.kde.kdeconnect.device.battery"
FIND_IFACE = "org.kde.kdeconnect.device.findmyphone"


class KdeConnectService(GObject.Object):
    __gsignals__: ClassVar[dict] = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self.devices: list[dict] = []  # [{id, name, reachable, battery, charging}]
        self._bus: Gio.DBusConnection | None = None
        self._subs: list[int] = []
        self._generation = 0
        self._refresh_timer = 0
        self._stopped = False
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            return
        for sig in ("deviceAdded", "deviceRemoved", "deviceVisibilityChanged"):
            self._subs.append(
                self._bus.signal_subscribe(
                    KDECONNECT,
                    DAEMON_IFACE,
                    sig,
                    DAEMON_PATH,
                    None,
                    Gio.DBusSignalFlags.NONE,
                    lambda *_: self.refresh(),
                )
            )
        self._subs.append(
            self._bus.signal_subscribe(
                KDECONNECT,
                BATT_IFACE,
                "refreshed",
                None,
                None,
                Gio.DBusSignalFlags.NONE,
                lambda *_: self.refresh(),
            )
        )
        self.refresh()

    def stop(self):
        self._stopped = True
        self._generation += 1
        if self._refresh_timer:
            GLib.source_remove(self._refresh_timer)
            self._refresh_timer = 0
        if self._bus is not None:
            for sub in self._subs:
                try:
                    self._bus.signal_unsubscribe(sub)
                except (GLib.Error, TypeError) as exc:
                    logging.getLogger(__name__).debug(
                        "KDE Connect signal_unsubscribe failed: %s", exc
                    )
        self._subs.clear()

    def _coerce_battery(self, value):
        if value is None:
            return -1
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1

    def refresh(self):
        if self._stopped or self._bus is None or self._refresh_timer:
            return
        self._refresh_timer = GLib.timeout_add(250, self._do_refresh)

    def _do_refresh(self):
        self._refresh_timer = 0
        if self._stopped or self._bus is None:
            return False
        self._generation += 1
        gen = self._generation
        self._bus.call(
            KDECONNECT,
            DAEMON_PATH,
            DAEMON_IFACE,
            "devices",
            GLib.Variant("(bb)", (True, True)),
            GLib.VariantType("(as)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            lambda bus, res, g=gen: self._on_devices(bus, res, g),
        )
        return False

    def _on_devices(self, bus, res, gen=None):
        if gen is None:
            gen = self._generation
        if self._stopped or gen != self._generation:
            return
        try:
            dev_ids = list(bus.call_finish(res).unpack()[0])
        except GLib.Error:
            if gen == self._generation and self.devices:
                self.devices = []
                self.emit("changed")
            return
        if not dev_ids:
            if gen == self._generation and self.devices:
                self.devices = []
                self.emit("changed")
            return
        pending = {"items": [], "left": len(dev_ids), "gen": gen}
        for dev_id in dev_ids:
            self._fetch_device(dev_id, pending, gen)

    def _fetch_device(self, dev_id: str, pending: dict, gen: int | None = None):
        if gen is None:
            gen = (
                pending.get("gen", self._generation)
                if isinstance(pending, dict)
                else self._generation
            )
        if self._bus is None:
            pending["left"] -= 1
            if pending["left"] <= 0:
                self._finish_pending(pending, gen)
            return
        path = f"{DAEMON_PATH}/devices/{dev_id}"
        self._bus.call(
            KDECONNECT,
            path,
            "org.freedesktop.DBus.Properties",
            "GetAll",
            GLib.Variant("(s)", (DEV_IFACE,)),
            GLib.VariantType("(a{sv})"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            lambda b, r, d=dev_id, p=pending, g=gen: self._on_dev_props(b, r, d, p, g),
        )

    def _on_dev_props(
        self, bus, res, dev_id: str, pending: dict, gen: int | None = None
    ):
        if gen is None:
            gen = (
                pending.get("gen", self._generation)
                if isinstance(pending, dict)
                else self._generation
            )
        if self._stopped or gen != self._generation:
            return
        name = dev_id
        reachable = True
        try:
            props = bus.call_finish(res).unpack()[0]
            name = str(props.get("name", dev_id) or dev_id)
            reachable = bool(props.get("isReachable", True))
        except GLib.Error:
            pass
        entry = {
            "id": dev_id,
            "name": name,
            "reachable": reachable,
            "battery": -1,
            "charging": False,
        }
        path = f"{DAEMON_PATH}/devices/{dev_id}/battery"
        bus.call(
            KDECONNECT,
            path,
            "org.freedesktop.DBus.Properties",
            "GetAll",
            GLib.Variant("(s)", (BATT_IFACE,)),
            GLib.VariantType("(a{sv})"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            lambda b, r, e=entry, p=pending, g=gen: self._on_batt_props(b, r, e, p, g),
        )

    def _finish_pending(self, pending: dict, gen: int):
        if self._stopped or gen != self._generation:
            return
        if pending["left"] <= 0:
            self.devices = pending["items"]
            self.emit("changed")

    def _on_batt_props(
        self, bus, res, entry: dict, pending: dict, gen: int | None = None
    ):
        if gen is None:
            gen = (
                pending.get("gen", self._generation)
                if isinstance(pending, dict)
                else self._generation
            )
        if self._stopped or gen != self._generation:
            return
        try:
            props = bus.call_finish(res).unpack()[0]
            entry["battery"] = self._coerce_battery(props.get("charge", -1))
            entry["charging"] = bool(props.get("isCharging", False))
        except GLib.Error:
            pass
        pending["items"].append(entry)
        pending["left"] -= 1
        if pending["left"] <= 0:
            self.devices = pending["items"]
            self.emit("changed")

    def ring_device(self, dev_id: str):
        if self._bus is None or not dev_id:
            return
        self._bus.call(
            KDECONNECT,
            f"{DAEMON_PATH}/devices/{dev_id}/findmyphone",
            FIND_IFACE,
            "ring",
            None,
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
        )

    def first_reachable_device(self) -> dict | None:
        for dev in self.devices:
            if dev.get("reachable", False):
                return dev
        return None

    def share_file(self, dev_id: str, path: str) -> bool:
        """Send a local file to a paired reachable KDE Connect device."""
        if self._bus is None or not dev_id or not path or not os.path.exists(path):
            return False
        uri = Gio.File.new_for_path(path).get_uri()
        self._bus.call(
            KDECONNECT,
            f"{DAEMON_PATH}/devices/{dev_id}/share",
            "org.kde.kdeconnect.device.share",
            "shareUrl",
            GLib.Variant("(s)", (uri,)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
        )
        return True
