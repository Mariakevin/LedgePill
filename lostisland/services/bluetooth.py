"""Bluetooth chip via BlueZ: tracks the connected device, peeks on
connect/disconnect. Signal driven through the object manager."""

from __future__ import annotations

import logging
from typing import ClassVar

from gi.repository import Gio, GLib, GObject

BLUEZ = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
BATTERY_IFACE = "org.bluez.Battery1"


class BluetoothService(GObject.Object):
    __gsignals__: ClassVar[dict] = {
        # device name, connected, battery percent (-1 unknown)
        "changed": (GObject.SignalFlags.RUN_FIRST, None, (str, bool, int)),
        # emitted once the initial BlueZ state has been ingested
        "synced": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # emitted when the adapter power state changes
        "adapter-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self.device = ""
        self.connected = False
        self.battery = -1
        self.discovering = False
        self._devices: dict[str, dict] = {}
        self._adapters: dict[str, dict] = {}
        self._subs: list[int] = []
        self._disc_timer = 0
        self._initial = True
        self._stopped = False
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error:
            return
        self._subs.append(
            self._bus.signal_subscribe(
                BLUEZ,
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                None,
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_props,
            )
        )
        self._subs.append(
            self._bus.signal_subscribe(
                BLUEZ,
                "org.freedesktop.DBus.ObjectManager",
                "InterfacesAdded",
                None,
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_interfaces_added,
            )
        )
        self._subs.append(
            self._bus.signal_subscribe(
                BLUEZ,
                "org.freedesktop.DBus.ObjectManager",
                "InterfacesRemoved",
                None,
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_interfaces_removed,
            )
        )
        self._bus.call(
            BLUEZ,
            "/",
            "org.freedesktop.DBus.ObjectManager",
            "GetManagedObjects",
            None,
            GLib.VariantType("(a{oa{sa{sv}}})"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_objects,
        )

    def stop(self):
        self._stopped = True
        self.stop_discovery()
        bus = getattr(self, "_bus", None)
        if bus is not None:
            for sub in self._subs:
                try:
                    bus.signal_unsubscribe(sub)
                except (GLib.Error, TypeError) as exc:
                    logging.getLogger(__name__).debug(
                        "Bluetooth signal_unsubscribe failed: %s", exc
                    )
        self._subs.clear()

    def _on_interfaces_added(self, bus, sender, path, iface, signal, params):
        if self._stopped:
            return
        try:
            obj_path, ifaces = params.unpack()
        except (GLib.Error, TypeError, ValueError):
            return
        if isinstance(ifaces, dict) and obj_path:
            self._ingest_interfaces(obj_path, ifaces)
            self._elect()
            self.emit("adapter-changed")

    def _coerce_battery(self, value):
        if value is None:
            return -1
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1

    def _on_interfaces_removed(self, bus, sender, path, iface, signal, params):
        if self._stopped:
            return
        try:
            unpacked = params.unpack()
        except (GLib.Error, TypeError, ValueError):
            return
        if isinstance(unpacked, (list, tuple)):
            obj_path = unpacked[0] if unpacked else None
            interfaces = unpacked[1] if len(unpacked) > 1 else []
        else:
            obj_path = unpacked
            interfaces = []
        if obj_path is None:
            return
        interfaces = list(interfaces) if interfaces is not None else []
        changed = False
        if obj_path in self._devices:
            if not interfaces or DEVICE_IFACE in interfaces:
                del self._devices[obj_path]
                changed = True
            elif (
                BATTERY_IFACE in interfaces
                and self._devices[obj_path].get("battery", -1) != -1
            ):
                self._devices[obj_path]["battery"] = -1
                changed = True
        if obj_path in self._adapters and (
            not interfaces or ADAPTER_IFACE in interfaces
        ):
            del self._adapters[obj_path]
            changed = True
        if changed:
            self._elect()

    def _on_objects(self, bus, res):
        if self._stopped:
            return
        try:
            objects = bus.call_finish(res).unpack()[0]
        except GLib.Error:
            return
        for path, ifaces in objects.items():
            self._ingest_interfaces(path, ifaces)
        self._elect()
        self._initial = False
        self.emit("synced")

    @property
    def powered(self):
        return any(adapter["powered"] for adapter in self._adapters.values())

    def devices(self):
        items = [{"path": path, **state} for path, state in self._devices.items()]
        items.sort(
            key=lambda d: (
                not d.get("connected", False),
                not d.get("paired", False),
                str(d.get("name", "")).casefold(),
            )
        )
        return items

    def toggle_power(self):
        self.set_power(not self.powered)

    def set_power(self, enabled: bool):
        path = next(iter(self._adapters), None)
        if path is None:
            return
        self._set_adapter_powered(path, bool(enabled))

    def start_discovery(self):
        if self._stopped or not self.powered:
            return
        path = next(iter(self._adapters), None)
        bus = getattr(self, "_bus", None)
        if path is None or bus is None:
            return
        self.discovering = True
        self.emit("adapter-changed")
        bus.call(
            BLUEZ,
            path,
            ADAPTER_IFACE,
            "StartDiscovery",
            None,
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
        )
        if self._disc_timer:
            GLib.source_remove(self._disc_timer)
        self._disc_timer = GLib.timeout_add_seconds(12, self._stop_disc_timer)

    def _stop_disc_timer(self):
        self._disc_timer = 0
        self.stop_discovery()
        return False

    def stop_discovery(self):
        if self._disc_timer:
            GLib.source_remove(self._disc_timer)
            self._disc_timer = 0
        if not self.discovering:
            return
        self.discovering = False
        path = next(iter(self._adapters), None)
        bus = getattr(self, "_bus", None)
        if path is not None and bus is not None:
            bus.call(
                BLUEZ,
                path,
                ADAPTER_IFACE,
                "StopDiscovery",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
                None,
            )
        if not self._stopped:
            self.emit("adapter-changed")

    def _set_adapter_powered(self, path, enabled):
        bus = getattr(self, "_bus", None)
        if bus is None:
            return
        bus.call(
            BLUEZ,
            path,
            "org.freedesktop.DBus.Properties",
            "Set",
            GLib.Variant(
                "(ssv)",
                (ADAPTER_IFACE, "Powered", GLib.Variant("b", enabled)),
            ),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
        )

    def connect_device(self, path, connect):
        bus = getattr(self, "_bus", None)
        if bus is None:
            return
        bus.call(
            BLUEZ,
            path,
            DEVICE_IFACE,
            "Connect" if connect else "Disconnect",
            None,
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
        )

    def _ingest_interfaces(self, path: str, ifaces: dict):
        adapter = ifaces.get(ADAPTER_IFACE)
        if adapter is not None:
            self._adapters[path] = {
                "powered": bool(adapter.get("Powered", False)),
                "name": adapter.get("Alias", adapter.get("Name", "")),
            }
            return
        dev = ifaces.get(DEVICE_IFACE)
        if not dev:
            return
        name = str(dev.get("Alias") or dev.get("Name") or "").strip()
        if not name:
            return
        self._devices[path] = {
            "name": name,
            "connected": bool(dev.get("Connected", False)),
            "paired": bool(dev.get("Paired", True)),
            "icon": str(dev.get("Icon") or "bluetooth-symbolic"),
            "battery": self._coerce_battery(
                ifaces.get(BATTERY_IFACE, {}).get("Percentage", -1)
            ),
        }

    def _on_props(self, bus, sender, path, iface, signal, params):
        if self._stopped:
            return
        try:
            which, changed, _inv = params.unpack()
        except (GLib.Error, TypeError, ValueError):
            return
        if which == ADAPTER_IFACE:
            adapter = self._adapters.setdefault(path, {"powered": False, "name": ""})
            if "Powered" in changed and bool(changed["Powered"]) != adapter["powered"]:
                adapter["powered"] = bool(changed["Powered"])
                self.emit("adapter-changed")
            return
        if which == DEVICE_IFACE:
            entry = self._devices.setdefault(
                path,
                {
                    "name": "",
                    "connected": False,
                    "paired": False,
                    "icon": "bluetooth-symbolic",
                    "battery": -1,
                },
            )
            if "Alias" in changed:
                entry["name"] = changed["Alias"]
            if "Paired" in changed:
                entry["paired"] = bool(changed["Paired"])
            if "Icon" in changed:
                entry["icon"] = str(changed["Icon"] or "bluetooth-symbolic")
            if "Connected" in changed:
                entry["connected"] = bool(changed["Connected"])
            self._elect()
            self.emit("adapter-changed")
        elif (
            which == BATTERY_IFACE and path in self._devices and "Percentage" in changed
        ):
            self._devices[path]["battery"] = self._coerce_battery(changed["Percentage"])
            self._elect()

    def _elect(self):
        active = [d for d in self._devices.values() if d["connected"]]
        if active:
            # Prefer a device that reports a battery (headphones/earbuds),
            # then the highest charge.
            top = max(active, key=lambda d: (d["battery"] >= 0, d["battery"]))
            state = (top["name"], True, top["battery"])
        else:
            state = ("", False, -1)
        if state != (self.device, self.connected, self.battery):
            self.device, self.connected, self.battery = state
            if not self._initial:
                self.emit("changed", *state)
