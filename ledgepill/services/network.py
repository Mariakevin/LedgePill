"""Connectivity chip via NetworkManager — reacts to PrimaryConnection changes."""

from __future__ import annotations

import logging
import re
import shutil
from typing import ClassVar

from gi.repository import Gio, GLib, GObject

NM = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
NM_ACTIVE = NM + ".Connection.Active"
NM_DEVICE_WIRELESS = NM + ".Device.Wireless"


class NetworkService(GObject.Object):
    __gsignals__: ClassVar[dict] = {
        # connection id ("HomeWifi"), type ("802-11-wireless"), connected
        "changed": (GObject.SignalFlags.RUN_FIRST, None, (str, str, bool)),
        # emitted once the initial connection state is known
        "synced": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # Wi-Fi signal quality 0-100
        "wifi-quality": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        # whether the NetworkManager wireless radio is enabled
        "wifi-enabled": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        # scanned Wi-Fi list or IP address updated
        "wifi-list-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # connect / disconnect result: ok, message
        "wifi-action-done": (GObject.SignalFlags.RUN_FIRST, None, (bool, str)),
    }

    def __init__(self):
        super().__init__()
        self.name = ""
        self.kind = ""
        self.connected = False
        self.wifi_enabled = True
        self.wifi_quality = None
        self.wifi_networks: list[dict] = []  # [{ssid, signal, security, active}]
        self.ip_address = ""
        self.wifi_busy = False
        self._nmcli = shutil.which("nmcli") or ""
        self._initial = True
        self._stopped = False
        self._scan_gen = 0
        self._primary_sub = 0
        self._wireless_sub = 0
        self._wireless_device_path = None
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error:
            return
        self._primary_sub = self._bus.signal_subscribe(
            NM,
            "org.freedesktop.DBus.Properties",
            "PropertiesChanged",
            NM_PATH,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_props,
        )
        self._fetch_primary()
        self._fetch_wireless_enabled()

    @property
    def wifi_signal(self):
        return self.wifi_quality if self.wifi_quality is not None else 0

    def _unsubscribe(self, sub_id: int):
        bus = getattr(self, "_bus", None)
        if bus is not None and sub_id:
            try:
                bus.signal_unsubscribe(sub_id)
            except (GLib.Error, TypeError) as exc:
                logging.getLogger(__name__).debug(
                    "Network signal_unsubscribe failed: %s", exc
                )

    def toggle_wifi(self):
        self.set_wifi_enabled(not self.wifi_enabled)

    def set_wifi_enabled(self, enabled: bool):
        bus = getattr(self, "_bus", None)
        if bus is None:
            return
        bus.call(
            NM,
            NM_PATH,
            "org.freedesktop.DBus.Properties",
            "Set",
            GLib.Variant(
                "(ssv)",
                (NM, "WirelessEnabled", GLib.Variant("b", bool(enabled))),
            ),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
        )

    @staticmethod
    def parse_nmcli_wifi(raw: str) -> list[dict]:
        """Parse `nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY dev wifi list` output."""
        by_ssid: dict[str, dict] = {}
        for line in (raw or "").splitlines():
            if not line.strip():
                continue
            parts = re.split(r"(?<!\\):", line, maxsplit=3)
            if len(parts) < 4:
                continue
            in_use = parts[0].strip() == "*"
            ssid = parts[1].replace("\\:", ":").strip()
            if not ssid or ssid == "--":
                continue
            try:
                sig = max(0, min(100, int(parts[2].strip() or 0)))
            except ValueError:
                sig = 0
            sec = parts[3].replace("\\:", ":").strip()
            if sec == "--":
                sec = ""
            prev = by_ssid.get(ssid)
            if prev is None or in_use or (not prev["active"] and sig > prev["signal"]):
                by_ssid[ssid] = {
                    "ssid": ssid,
                    "signal": sig,
                    "security": sec,
                    "active": in_use,
                }
        nets = list(by_ssid.values())
        nets.sort(key=lambda n: (not n["active"], -n["signal"], n["ssid"].casefold()))
        return nets[:15]

    @staticmethod
    def parse_nmcli_ip(raw: str) -> str:
        for line in (raw or "").splitlines():
            s = line.strip()
            if s.startswith("IP4.ADDRESS"):
                val = s.split(":", 1)[1].strip() if ":" in s else ""
                val = val.split("/", 1)[0].strip()
                if val and val != "127.0.0.1":
                    return val
        return ""

    def scan_wifi(self, rescan: bool = True):
        """Refresh nearby Wi-Fi networks & active IPv4 via nmcli asynchronously."""
        if self._stopped or not self._nmcli or not self.wifi_enabled:
            if not self.wifi_enabled and self.wifi_networks:
                self.wifi_networks = []
                self.emit("wifi-list-changed")
            return
        self._scan_gen += 1
        gen = self._scan_gen
        self.wifi_busy = True
        cmd = [
            self._nmcli,
            "-t",
            "-e",
            "yes",
            "-f",
            "IN-USE,SSID,SIGNAL,SECURITY",
            "dev",
            "wifi",
            "list",
            "--rescan",
            "yes" if rescan else "no",
        ]
        try:
            proc = Gio.Subprocess.new(
                cmd,
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            )
            proc.communicate_utf8_async(
                None, None, lambda p, r, g=gen: self._on_wifi_scan_out(p, r, g)
            )
        except GLib.Error:
            self.wifi_busy = False
        self._fetch_ip_async()

    def _on_wifi_scan_out(self, proc, res, gen: int):
        if self._stopped or gen != self._scan_gen:
            return
        self.wifi_busy = False
        try:
            ok, out, _err = proc.communicate_utf8_finish(res)
        except GLib.Error:
            return
        if ok:
            nets = self.parse_nmcli_wifi(out or "")
            if nets != self.wifi_networks:
                self.wifi_networks = nets
                self.emit("wifi-list-changed")

    def _fetch_ip_async(self):
        if self._stopped or not self._nmcli:
            return
        try:
            proc = Gio.Subprocess.new(
                [self._nmcli, "-t", "-f", "IP4.ADDRESS", "dev", "show"],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            )
            proc.communicate_utf8_async(None, None, self._on_ip_out)
        except GLib.Error:
            pass

    def _on_ip_out(self, proc, res):
        if self._stopped:
            return
        try:
            _ok, out, _err = proc.communicate_utf8_finish(res)
        except GLib.Error:
            return
        ip = self.parse_nmcli_ip(out or "")
        if ip != self.ip_address:
            self.ip_address = ip
            self.emit("wifi-list-changed")

    def connect_wifi(self, ssid: str, password: str | None = None):
        clean_ssid = (ssid or "").strip()
        if not clean_ssid or not self._nmcli or self._stopped:
            return
        cmd = [self._nmcli, "dev", "wifi", "connect", clean_ssid]
        if password:
            cmd.extend(["password", password])
        try:
            proc = Gio.Subprocess.new(
                cmd,
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            )
            proc.communicate_utf8_async(
                None, None, lambda p, r, s=clean_ssid: self._on_wifi_action(p, r, s)
            )
        except GLib.Error as exc:
            self.emit("wifi-action-done", False, str(exc))

    def disconnect_wifi(self):
        if not self._nmcli or self._stopped:
            return
        target = self.name.strip()
        cmd = (
            [self._nmcli, "con", "down", "id", target]
            if target
            else [self._nmcli, "radio", "wifi", "off"]
        )
        try:
            proc = Gio.Subprocess.new(
                cmd,
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            )
            proc.communicate_utf8_async(
                None,
                None,
                lambda p, r: self._on_wifi_action(p, r, target or "Wi-Fi"),
            )
        except GLib.Error as exc:
            self.emit("wifi-action-done", False, str(exc))

    def _on_wifi_action(self, proc, res, ssid: str):
        if self._stopped:
            return
        try:
            _ok, out, err = proc.communicate_utf8_finish(res)
            success = proc.get_successful()
        except GLib.Error as exc:
            self.emit("wifi-action-done", False, str(exc))
            return
        msg = (err or out or "").strip() or (
            f"Updated {ssid}" if success else f"Failed connecting to {ssid}"
        )
        self.emit("wifi-action-done", bool(success), msg.splitlines()[0][:140])
        self.scan_wifi(rescan=False)

    def stop(self):
        self._stopped = True
        self._scan_gen += 1
        self.wifi_busy = False
        for sub in (self._primary_sub, self._wireless_sub):
            self._unsubscribe(sub)
        self._primary_sub = 0
        self._wireless_sub = 0
        self._wireless_device_path = None

    def _on_props(self, bus, sender, path, iface, signal, params):
        if self._stopped or getattr(self, "_bus", None) is None:
            return
        try:
            _i, changed, _inv = params.unpack()
        except (GLib.Error, TypeError, ValueError):
            return
        if "WirelessEnabled" in changed:
            self._apply_wireless_enabled(bool(changed["WirelessEnabled"]))
        if "PrimaryConnection" in changed:
            self._fetch_primary()

    def _fetch_primary(self):
        bus = getattr(self, "_bus", None)
        if bus is None:
            return
        bus.call(
            NM,
            NM_PATH,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (NM, "PrimaryConnection")),
            GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_primary,
        )

    def _fetch_wireless_enabled(self):
        bus = getattr(self, "_bus", None)
        if bus is None:
            return
        bus.call(
            NM,
            NM_PATH,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (NM, "WirelessEnabled")),
            GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_wireless_enabled,
        )

    def _on_wireless_enabled(self, bus, res):
        if self._stopped:
            return
        try:
            enabled = bus.call_finish(res).unpack()[0]
        except GLib.Error:
            return
        self._apply_wireless_enabled(bool(enabled))

    def _apply_wireless_enabled(self, enabled):
        if enabled == self.wifi_enabled:
            return
        self.wifi_enabled = enabled
        if not enabled and self.wifi_networks:
            self.wifi_networks = []
            self.emit("wifi-list-changed")
        self.emit("wifi-enabled", enabled)

    def _on_primary(self, bus, res):
        if self._stopped:
            return
        try:
            path = bus.call_finish(res).unpack()[0]
        except GLib.Error:
            return
        if not path or path == "/":
            self._update("", "", False)
            self._clear_wireless()
            if self.ip_address:
                self.ip_address = ""
                self.emit("wifi-list-changed")
            return
        bus = getattr(self, "_bus", None)
        if bus is None:
            return
        bus.call(
            NM,
            path,
            "org.freedesktop.DBus.Properties",
            "GetAll",
            GLib.Variant("(s)", (NM_ACTIVE,)),
            GLib.VariantType("(a{sv})"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_active,
        )

    def _on_active(self, bus, res):
        if self._stopped:
            return
        try:
            props = bus.call_finish(res).unpack()[0]
        except GLib.Error:
            return
        conn_type = props.get("Type", "")
        self._update(props.get("Id", ""), conn_type, True)
        self._fetch_ip_async()
        if conn_type == "802-11-wireless":
            device_path = None
            devices = props.get("Devices", props.get("Device"))
            if isinstance(devices, str):
                device_path = devices
            elif isinstance(devices, (list, tuple)) and devices:
                device_path = devices[0]
            if not device_path:
                device_path = props.get("SpecificObject")
            if device_path and device_path != "/":
                self._wireless_device_path = device_path
                self._fetch_wireless_strength(device_path)
                self._subscribe_wireless_device(device_path)
            else:
                self._clear_wireless()
        else:
            self._clear_wireless()

    def _clear_wireless(self):
        self._unsubscribe(self._wireless_sub)
        self._wireless_sub = 0
        self._wireless_device_path = None

    def _subscribe_wireless_device(self, device_path):
        bus = getattr(self, "_bus", None)
        if self._stopped or bus is None or not device_path:
            return
        if device_path == self._wireless_device_path and self._wireless_sub:
            return
        if self._wireless_sub:
            self._unsubscribe(self._wireless_sub)
            self._wireless_sub = 0
        self._wireless_device_path = device_path
        try:
            self._wireless_sub = bus.signal_subscribe(
                NM,
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                device_path,
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_wireless_props,
            )
        except (GLib.Error, TypeError):
            self._wireless_sub = 0

    def _on_wireless_props(self, bus, sender, path, iface, signal, params):
        if self._stopped:
            return
        try:
            _i, changed, _inv = params.unpack()
        except (GLib.Error, TypeError, ValueError):
            return
        if "Strength" in changed:
            self._apply_wireless_strength(changed["Strength"])

    def _fetch_wireless_strength(self, device_path):
        bus = getattr(self, "_bus", None)
        if self._stopped or bus is None or not device_path:
            return
        bus.call(
            NM,
            device_path,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (NM_DEVICE_WIRELESS, "Strength")),
            GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_wireless_strength,
        )

    def _on_wireless_strength(self, bus, res):
        if self._stopped:
            return
        try:
            strength = bus.call_finish(res).unpack()[0]
        except GLib.Error:
            return
        self._apply_wireless_strength(strength)

    def _apply_wireless_strength(self, strength):
        self._set_wifi_signal(strength)

    def _update(self, name, kind, connected):
        changed = (name, kind, connected) != (self.name, self.kind, self.connected)
        self.name, self.kind, self.connected = name, kind, connected
        if self._initial:
            self.emit("synced")
        elif changed:
            self.emit("changed", name, kind, connected)
        self._initial = False

    def _set_wifi_signal(self, quality):
        try:
            value = max(0, min(100, int(quality)))
        except (TypeError, ValueError):
            value = 0
        if value == self.wifi_quality:
            return
        self.wifi_quality = value
        self.emit("wifi-quality", value)
