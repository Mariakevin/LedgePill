"""System panels: Wi-Fi and Bluetooth connectivity."""

from __future__ import annotations

from gi.repository import GLib, Gtk, Pango

from ledgepill.ui.panel_shared import (
    _badge,
    _icon_btn,
    _section_card,
)


class ConnectivityPanel(Gtk.Box):
    """Side-by-side Wi-Fi and Bluetooth Bento tiles with expandable full-width drawers."""

    def __init__(self, network=None, bluetooth=None, kdeconnect=None, on_toggle=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.network = network
        self.bluetooth = bluetooth
        self.kde = kdeconnect
        self._on_toggle = on_toggle
        self._wifi_open = False
        self._bt_open = False
        self._pending_ssid = ""
        self._fb_timer = 0
        self._last_snap = None

        self.connect("destroy", lambda *_: self._cleanup())

        pair = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        pair.set_homogeneous(True)

        # Wi-Fi Bento tile
        self.wifi_card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.wifi_card.add_css_class("conn-card")
        self.wifi_pwr_btn = Gtk.Button()
        self.wifi_pwr_btn.set_icon_name("network-wireless-symbolic")
        self.wifi_pwr_btn.add_css_class("conn-icon-btn")
        self.wifi_pwr_btn.set_tooltip_text("Toggle Wi-Fi radio")
        self.wifi_pwr_btn.connect(
            "clicked", lambda *_: self.network and self.network.toggle_wifi()
        )
        self.wifi_pwr_btn.set_valign(Gtk.Align.CENTER)

        self.wifi_drawer_btn = Gtk.Button()
        self.wifi_drawer_btn.add_css_class("conn-body-btn")
        self.wifi_drawer_btn.set_hexpand(True)
        self.wifi_drawer_btn.connect("clicked", lambda *_: self.toggle_wifi_drawer())
        w_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        self.wifi_title_lbl = Gtk.Label(label="Wi-Fi", xalign=0)
        self.wifi_title_lbl.add_css_class("list-title")
        self.wifi_title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.wifi_title_lbl.set_width_chars(1)
        self.wifi_title_lbl.set_max_width_chars(13)
        self.wifi_drawer_lbl = Gtk.Label(label="Disconnected", xalign=0)
        self.wifi_drawer_lbl.add_css_class("tile-sub")
        self.wifi_drawer_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.wifi_drawer_lbl.set_width_chars(1)
        self.wifi_drawer_lbl.set_max_width_chars(15)
        w_txt.append(self.wifi_title_lbl)
        w_txt.append(self.wifi_drawer_lbl)
        self.wifi_chev = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        w_body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        w_body.append(w_txt)
        w_body.append(self.wifi_chev)
        self.wifi_drawer_btn.set_child(w_body)

        self.wifi_scan_btn = _icon_btn("view-refresh-symbolic", "Scan Wi-Fi networks")
        self.wifi_scan_btn.set_valign(Gtk.Align.CENTER)
        self.wifi_scan_btn.connect("clicked", self._on_wifi_scan)

        self.wifi_card.append(self.wifi_pwr_btn)
        self.wifi_card.append(self.wifi_drawer_btn)
        self.wifi_card.append(self.wifi_scan_btn)
        self.wifi_card.set_visible(self.network is not None)

        # Bluetooth & KDE Connect Bento tile
        self.bt_card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.bt_card.add_css_class("conn-card")
        self.bt_pwr_btn = Gtk.Button()
        self.bt_pwr_btn.set_icon_name("bluetooth-active-symbolic")
        self.bt_pwr_btn.add_css_class("conn-icon-btn")
        self.bt_pwr_btn.set_tooltip_text("Toggle Bluetooth power")
        self.bt_pwr_btn.connect(
            "clicked", lambda *_: self.bluetooth and self.bluetooth.toggle_power()
        )
        self.bt_pwr_btn.set_valign(Gtk.Align.CENTER)
        self.bt_pwr_btn.set_visible(self.bluetooth is not None)

        self.bt_drawer_btn = Gtk.Button()
        self.bt_drawer_btn.add_css_class("conn-body-btn")
        self.bt_drawer_btn.set_hexpand(True)
        self.bt_drawer_btn.connect("clicked", lambda *_: self.toggle_bt_drawer())
        b_txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        self.bt_title_lbl = Gtk.Label(label="Bluetooth", xalign=0)
        self.bt_title_lbl.add_css_class("list-title")
        self.bt_title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.bt_title_lbl.set_width_chars(1)
        self.bt_title_lbl.set_max_width_chars(13)
        self.bt_drawer_lbl = Gtk.Label(label="Ready", xalign=0)
        self.bt_drawer_lbl.add_css_class("tile-sub")
        self.bt_drawer_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.bt_drawer_lbl.set_width_chars(1)
        self.bt_drawer_lbl.set_max_width_chars(15)
        b_txt.append(self.bt_title_lbl)
        b_txt.append(self.bt_drawer_lbl)
        self.bt_chev = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        b_body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        b_body.append(b_txt)
        b_body.append(self.bt_chev)
        self.bt_drawer_btn.set_child(b_body)

        self.bt_scan_btn = _icon_btn("view-refresh-symbolic", "Scan Bluetooth devices")
        self.bt_scan_btn.set_valign(Gtk.Align.CENTER)
        self.bt_scan_btn.connect("clicked", self._on_bt_scan)
        self.bt_scan_btn.set_visible(self.bluetooth is not None)

        self.bt_card.append(self.bt_pwr_btn)
        self.bt_card.append(self.bt_drawer_btn)
        self.bt_card.append(self.bt_scan_btn)
        self.bt_card.set_visible(self.bluetooth is not None or self.kde is not None)

        pair.append(self.wifi_card)
        pair.append(self.bt_card)
        self.append(pair)
        self.set_visible(self.wifi_card.get_visible() or self.bt_card.get_visible())

        self.conn_feedback = Gtk.Label(xalign=0)
        self.conn_feedback.add_css_class("tile-sub")
        self.conn_feedback.set_ellipsize(Pango.EllipsizeMode.END)
        self.conn_feedback.set_margin_top(6)
        self.conn_feedback.set_visible(False)
        self.append(self.conn_feedback)

        # Wi-Fi networks full-width drawer
        self.wifi_revealer = Gtk.Revealer()
        self.wifi_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.wifi_revealer.set_reveal_child(False)
        wifi_drawer = _section_card(6)
        wifi_drawer.add_css_class("conn-drawer")
        wifi_drawer.set_margin_top(8)
        self.wifi_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        wifi_drawer.append(self.wifi_box)

        self.wifi_pw_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.wifi_pw_entry = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True)
        self.wifi_pw_entry.set_property("placeholder-text", "Wi-Fi password…")
        self.wifi_pw_entry.connect("activate", self._submit_wifi_password)
        pw_ok = Gtk.Button(label="Join")
        pw_ok.add_css_class("panel-row-btn")
        pw_ok.connect("clicked", self._submit_wifi_password)
        pw_no = _icon_btn("window-close-symbolic", "Cancel")
        pw_no.connect("clicked", lambda *_: self._hide_wifi_pw())
        self.wifi_pw_box.append(self.wifi_pw_entry)
        self.wifi_pw_box.append(pw_ok)
        self.wifi_pw_box.append(pw_no)
        self.wifi_pw_box.set_visible(False)
        wifi_drawer.append(self.wifi_pw_box)
        self.wifi_revealer.set_child(wifi_drawer)
        self.append(self.wifi_revealer)

        # Bluetooth & KDE Connect full-width drawer
        self.bt_revealer = Gtk.Revealer()
        self.bt_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.bt_revealer.set_reveal_child(False)
        bt_drawer = _section_card(6)
        bt_drawer.add_css_class("conn-drawer")
        bt_drawer.set_margin_top(8)
        self.bt_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bt_drawer.append(self.bt_box)
        self.bt_revealer.set_child(bt_drawer)
        self.append(self.bt_revealer)

        self._wire_signals()
        self.refresh()

    def _wire_signals(self):
        if self.network is not None:
            for sig in (
                "changed",
                "synced",
                "wifi-quality",
                "wifi-enabled",
                "wifi-list-changed",
            ):
                self.network.connect(sig, lambda *_: self.refresh())
            self.network.connect("wifi-action-done", self._on_wifi_action_done)
        if self.bluetooth is not None:
            for sig in ("changed", "synced", "adapter-changed"):
                self.bluetooth.connect(sig, lambda *_: self.refresh())
        if self.kde is not None:
            self.kde.connect("changed", lambda *_: self.refresh())

    def set_bluetooth_service(self, bt_svc):
        self.bluetooth = bt_svc
        self.bt_pwr_btn.set_visible(bt_svc is not None)
        self.bt_scan_btn.set_visible(bt_svc is not None)
        self.bt_card.set_visible(bt_svc is not None or self.kde is not None)
        self.set_visible(self.wifi_card.get_visible() or self.bt_card.get_visible())
        if bt_svc is not None:
            for sig in ("changed", "synced", "adapter-changed"):
                bt_svc.connect(sig, lambda *_: self.refresh())
        self.refresh()

    def _cleanup(self):
        if self._fb_timer:
            GLib.source_remove(self._fb_timer)
            self._fb_timer = 0
        if self.bluetooth is not None and hasattr(self.bluetooth, "stop_discovery"):
            self.bluetooth.stop_discovery()

    def _sync_drawer_styles(self):
        for card, active in (
            (self.wifi_card, self._wifi_open),
            (self.bt_card, self._bt_open),
        ):
            if active:
                card.add_css_class("conn-card--active")
            else:
                card.remove_css_class("conn-card--active")

    def collapse_drawers(self):
        self._wifi_open = False
        self._bt_open = False
        self.wifi_revealer.set_reveal_child(False)
        self.bt_revealer.set_reveal_child(False)
        self.wifi_chev.set_from_icon_name("pan-down-symbolic")
        self.bt_chev.set_from_icon_name("pan-down-symbolic")
        self._sync_drawer_styles()
        self._hide_wifi_pw()

    def toggle_wifi_drawer(self, open_only: bool = False):
        target = True if open_only else not self._wifi_open
        if target and self._bt_open:
            self._bt_open = False
            self.bt_revealer.set_reveal_child(False)
            self.bt_chev.set_from_icon_name("pan-down-symbolic")
        self._wifi_open = target
        self.wifi_revealer.set_reveal_child(target)
        self.wifi_chev.set_from_icon_name(
            "pan-up-symbolic" if target else "pan-down-symbolic"
        )
        self._sync_drawer_styles()
        if target and self._on_toggle is not None:
            self._on_toggle()
        if target and self.network is not None and hasattr(self.network, "scan_wifi"):
            self.network.scan_wifi(rescan=False)

    def toggle_bt_drawer(self, open_only: bool = False):
        target = True if open_only else not self._bt_open
        if target and self._wifi_open:
            self._wifi_open = False
            self.wifi_revealer.set_reveal_child(False)
            self.wifi_chev.set_from_icon_name("pan-down-symbolic")
            self._hide_wifi_pw()
        self._bt_open = target
        self.bt_revealer.set_reveal_child(target)
        self.bt_chev.set_from_icon_name(
            "pan-up-symbolic" if target else "pan-down-symbolic"
        )
        self._sync_drawer_styles()
        if target and self._on_toggle is not None:
            self._on_toggle()

    def _on_wifi_scan(self, *_):
        self.toggle_wifi_drawer(open_only=True)
        if self.network is not None and hasattr(self.network, "scan_wifi"):
            self._show_feedback("Scanning Wi-Fi networks…")
            self.network.scan_wifi(rescan=True)

    def _on_bt_scan(self, *_):
        self.toggle_bt_drawer(open_only=True)
        if self.bluetooth is not None and hasattr(self.bluetooth, "start_discovery"):
            if not self.bluetooth.powered:
                self.bluetooth.set_power(True)
            self._show_feedback("Scanning for Bluetooth devices…")
            self.bluetooth.start_discovery()

    def _show_feedback(self, msg: str):
        self.conn_feedback.set_label(msg)
        self.conn_feedback.set_visible(bool(msg))
        if self._fb_timer:
            GLib.source_remove(self._fb_timer)
        if msg:
            self._fb_timer = GLib.timeout_add_seconds(
                4,
                lambda: (
                    setattr(self, "_fb_timer", 0),
                    self.conn_feedback.set_visible(False),
                    False,
                )[2],
            )

    def _hide_wifi_pw(self):
        self._pending_ssid = ""
        self.wifi_pw_entry.set_text("")
        self.wifi_pw_box.set_visible(False)

    def _on_wifi_action_done(self, _svc, ok: bool, msg: str):
        if ok:
            self._hide_wifi_pw()
        elif self._pending_ssid and any(
            k in msg.lower()
            for k in (
                "secret",
                "password",
                "802-11",
                "psk",
                "key",
                "auth",
                "credential",
            )
        ):
            self.wifi_pw_box.set_visible(True)
            self.wifi_pw_entry.set_property(
                "placeholder-text", f"Password for {self._pending_ssid}…"
            )
            self.wifi_pw_entry.grab_focus()
        self._show_feedback(msg)

    def _connect_ssid(self, ssid: str):
        if self.network is None:
            return
        self._pending_ssid = ssid
        self._show_feedback(f"Connecting to {ssid}…")
        self.network.connect_wifi(ssid)

    def _submit_wifi_password(self, *_):
        pw = self.wifi_pw_entry.get_text()
        ssid = self._pending_ssid
        if self.network is not None and ssid and pw:
            self._show_feedback(f"Joining {ssid}…")
            self.network.connect_wifi(ssid, password=pw)

    def refresh(self):
        # Network header & drawer summary
        if self.network is not None:
            w_en = bool(self.network.wifi_enabled)
            if w_en:
                self.wifi_pwr_btn.add_css_class("conn-icon-btn--active")
            else:
                self.wifi_pwr_btn.remove_css_class("conn-icon-btn--active")
            if self.network.connected:
                self.wifi_title_lbl.set_label(self.network.name or "Wi-Fi")
                parts = []
                if "wireless" in self.network.kind and self.network.wifi_signal > 0:
                    parts.append(f"{self.network.wifi_signal}%")
                if getattr(self.network, "ip_address", ""):
                    parts.append(self.network.ip_address)
                self.wifi_drawer_lbl.set_label(
                    " · ".join(parts) if parts else "Connected"
                )
            else:
                self.wifi_title_lbl.set_label("Wi-Fi")
                self.wifi_drawer_lbl.set_label(
                    "Radio off" if not w_en else "Tap to connect"
                )

        # Bluetooth header & drawer summary
        if self.bluetooth is not None:
            bt_on = bool(self.bluetooth.powered)
            disc = bool(getattr(self.bluetooth, "discovering", False))
            if bt_on:
                self.bt_pwr_btn.add_css_class("conn-icon-btn--active")
            else:
                self.bt_pwr_btn.remove_css_class("conn-icon-btn--active")
            if self.bluetooth.connected and self.bluetooth.device:
                self.bt_title_lbl.set_label(self.bluetooth.device)
                sub = (
                    f"{self.bluetooth.battery}% · Connected"
                    if self.bluetooth.battery >= 0
                    else "Connected"
                )
                self.bt_drawer_lbl.set_label(sub)
            elif disc:
                self.bt_title_lbl.set_label("Bluetooth")
                self.bt_drawer_lbl.set_label("Scanning…")
            else:
                self.bt_title_lbl.set_label("Bluetooth")
                devs = self.bluetooth.devices()
                self.bt_drawer_lbl.set_label(
                    (f"{len(devs)} paired" if devs else "On") if bt_on else "Off"
                )
        elif self.kde is not None:
            self.bt_title_lbl.set_label("Phone link")
            if self.kde.devices:
                dev = self.kde.devices[0]
                b = f" · {dev['battery']}%" if dev["battery"] >= 0 else ""
                self.bt_drawer_lbl.set_label(f"{dev['name']}{b}")
            else:
                self.bt_drawer_lbl.set_label("No paired phone")

        snap = (
            tuple(
                (n["ssid"], n["signal"], n["security"], n["active"])
                for n in (
                    getattr(self.network, "wifi_networks", []) if self.network else ()
                )
            ),
            bool(self.network.wifi_enabled) if self.network else None,
            bool(self.bluetooth.powered) if self.bluetooth else None,
            tuple(
                (
                    d.get("path"),
                    d.get("name"),
                    bool(d.get("connected")),
                    bool(d.get("paired", True)),
                    d.get("icon", "bluetooth-symbolic"),
                    d.get("battery", -1),
                )
                for d in (self.bluetooth.devices() if self.bluetooth else ())
            ),
            tuple(
                (d.get("id"), d.get("name"), d.get("battery"))
                for d in (self.kde.devices if self.kde else ())
            ),
        )
        if snap == self._last_snap:
            return
        self._last_snap = snap

        while (c := self.wifi_box.get_first_child()) is not None:
            self.wifi_box.remove(c)
        if self.network is not None:
            nets = list(getattr(self.network, "wifi_networks", []))
            if not self.network.wifi_enabled:
                lbl = Gtk.Label(label="Wi-Fi radio is off", xalign=0)
                lbl.add_css_class("empty-hint")
                self.wifi_box.append(lbl)
            elif not nets:
                lbl = Gtk.Label(
                    label="Click refresh to scan nearby Wi-Fi networks", xalign=0
                )
                lbl.add_css_class("empty-hint")
                self.wifi_box.append(lbl)
            else:
                for net in nets[:8]:
                    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                    btn = Gtk.Button()
                    btn.add_css_class("panel-row-btn")
                    if net["active"]:
                        btn.add_css_class("panel-row-btn--active")
                    btn.set_hexpand(True)
                    inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                    nm = Gtk.Label(label=net["ssid"], xalign=0, hexpand=True)
                    nm.set_ellipsize(Pango.EllipsizeMode.END)
                    inner.append(nm)
                    if net["security"]:
                        sec = Gtk.Image.new_from_icon_name(
                            "system-lock-screen-symbolic"
                        )
                        sec.set_pixel_size(11)
                        inner.append(sec)
                    inner.append(_badge(f"{net['signal']}%", accent=net["active"]))
                    btn.set_child(inner)
                    if net["active"]:
                        btn.connect(
                            "clicked", lambda *_: self.network.disconnect_wifi()
                        )
                        btn.set_tooltip_text("Click to disconnect")
                    else:
                        btn.connect(
                            "clicked",
                            lambda _b, s=net["ssid"]: self._connect_ssid(s),
                        )
                    row.append(btn)
                    self.wifi_box.append(row)

        while (c := self.bt_box.get_first_child()) is not None:
            self.bt_box.remove(c)
        has_bt = False
        if self.bluetooth is not None:
            for d in self.bluetooth.devices():
                has_bt = True
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                b = Gtk.Button()
                b.add_css_class("panel-row-btn")
                if d["connected"]:
                    b.add_css_class("panel-row-btn--active")
                b.set_hexpand(True)
                inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                ico = Gtk.Image.new_from_icon_name(
                    d.get("icon") or "bluetooth-symbolic"
                )
                ico.set_pixel_size(14)
                nm = Gtk.Label(label=d["name"], xalign=0, hexpand=True)
                nm.set_ellipsize(Pango.EllipsizeMode.END)
                inner.append(ico)
                inner.append(nm)
                if d.get("battery", -1) >= 0:
                    inner.append(_badge(f"{d['battery']}%", accent=d["connected"]))
                elif d["connected"]:
                    inner.append(_badge("Connected", accent=True))
                elif not d.get("paired", True):
                    inner.append(_badge("Found"))
                b.set_child(inner)
                b.connect(
                    "clicked",
                    lambda _b, p=d["path"], c=d["connected"]: (
                        self.bluetooth.connect_device(p, not c)
                    ),
                )
                row.append(b)
                self.bt_box.append(row)

        if self.kde is not None:
            for dev in self.kde.devices:
                has_bt = True
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                b_txt = f" · {dev['battery']}%" if dev["battery"] >= 0 else ""
                lbl = Gtk.Label(
                    label=f"Phone: {dev['name']}{b_txt}", xalign=0, hexpand=True
                )
                lbl.add_css_class("list-title")
                ring = Gtk.Button(label="Ring")
                ring.add_css_class("panel-row-btn")
                ring.connect(
                    "clicked", lambda _b, did=dev["id"]: self.kde.ring_device(did)
                )
                row.append(lbl)
                row.append(ring)
                self.bt_box.append(row)

        if not has_bt:
            empty = Gtk.Label(
                label="No Bluetooth or KDE Connect devices found", xalign=0
            )
            empty.add_css_class("empty-hint")
            self.bt_box.append(empty)
