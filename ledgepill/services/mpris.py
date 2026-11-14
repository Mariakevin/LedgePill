"""MPRIS media service.

Tracks every org.mpris.MediaPlayer2.* player on the session bus and elects an
"active" one (playing beats paused, most recent wins, with optional manual
override). Supports track lists, playlists, and dominant artwork color
extraction.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import threading
import urllib.error
import urllib.request
from typing import ClassVar

from gi.repository import GdkPixbuf, Gio, GLib, GObject

from ledgepill import config

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PLAYLISTS_IFACE = "org.mpris.MediaPlayer2.Playlists"
TRACKLIST_IFACE = "org.mpris.MediaPlayer2.TrackList"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
MAX_ART_BYTES = 10 * 1024 * 1024


class Player:
    __slots__ = (
        "album",
        "art_path",
        "art_url",
        "artist",
        "bus_name",
        "can_next",
        "can_prev",
        "length_us",
        "stamp",
        "status",
        "title",
        "track_id",
    )

    def __init__(self, bus_name: str):
        self.bus_name = bus_name
        self.status = "Stopped"
        self.title = ""
        self.artist = ""
        self.album = ""
        self.art_url = ""
        self.art_path = ""
        self.length_us = 0
        self.track_id = ""
        self.can_next = False
        self.can_prev = False
        self.stamp = 0

    @property
    def label(self) -> str:
        name = self.bus_name.removeprefix(MPRIS_PREFIX).split(".", 1)[0]
        return name.capitalize()


def extract_accent_color(art_path: str) -> str | None:
    """Return a vibrant #rrggbb hex accent sampled from album artwork."""
    if not art_path or not os.path.isfile(art_path):
        return None
    try:
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(art_path, 24, 24, False)
    except GLib.Error:
        return None
    pixels = pb.get_pixels()
    n_channels = pb.get_n_channels()
    rowstride = pb.get_rowstride()
    w, h = pb.get_width(), pb.get_height()
    if n_channels < 3 or w <= 0 or h <= 0:
        return None

    best_score = -1.0
    best_rgb = None
    for y in range(h):
        row_off = y * rowstride
        for x in range(w):
            idx = row_off + x * n_channels
            r, g, b = pixels[idx], pixels[idx + 1], pixels[idx + 2]
            if n_channels >= 4 and pixels[idx + 3] < 128:
                continue
            hi = max(r, g, b)
            lo = min(r, g, b)
            if hi < 60 or lo > 235:
                continue
            sat = (hi - lo) / max(1, hi)
            lum = hi / 255.0
            score = sat * 2.0 + lum * 0.6
            if score > best_score:
                best_score = score
                best_rgb = (r, g, b)

    if best_rgb is None or best_score < 0.35:
        return None
    return f"#{best_rgb[0]:02x}{best_rgb[1]:02x}{best_rgb[2]:02x}"


_WALLPAPER_CACHE: dict[str, tuple[float, str | None]] = {}


def _parse_rgb_triplet(raw: str) -> str | None:
    text = (raw or "").strip()
    if text.startswith("#") and len(text) in (7, 9):
        hex_part = text[:7]
        try:
            int(hex_part[1:], 16)
            return hex_part.lower()
        except ValueError:
            return None
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 3:
        return None
    try:
        vals = [int(p) for p in parts[:3]]
    except ValueError:
        return None
    if not all(0 <= v <= 255 for v in vals):
        return None
    if max(vals) - min(vals) < 18:
        return None
    return f"#{vals[0]:02x}{vals[1]:02x}{vals[2]:02x}"


def _read_kde_accent(kdeglobals_path: str) -> str | None:
    if not kdeglobals_path or not os.path.isfile(kdeglobals_path):
        return None
    in_general = False
    try:
        with open(kdeglobals_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith(("#", ";")):
                    continue
                if s.startswith("[") and s.endswith("]"):
                    in_general = s.lower() == "[general]"
                    continue
                if in_general and s.startswith("AccentColor="):
                    col = _parse_rgb_triplet(s.split("=", 1)[1])
                    if col:
                        return col
    except OSError:
        return None
    return None


def _read_plasma_wallpaper(appletsrc_path: str) -> str | None:
    if not appletsrc_path or not os.path.isfile(appletsrc_path):
        return None
    candidate = ""
    try:
        with open(appletsrc_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = line.strip()
                if s.startswith("Image="):
                    val = s.split("=", 1)[1].strip().removeprefix("file://")
                    if val:
                        candidate = os.path.expanduser(val)
    except OSError:
        return None
    if not candidate:
        return None
    if os.path.isfile(candidate):
        return candidate
    if os.path.isdir(candidate):
        img_dir = os.path.join(candidate, "contents", "images")
        scan_dir = img_dir if os.path.isdir(img_dir) else candidate
        try:
            pics = [
                os.path.join(scan_dir, fn)
                for fn in sorted(os.listdir(scan_dir))
                if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
            ]
            if pics:
                return pics[-1]
        except OSError:
            pass
    return None


def _portal_accent() -> str | None:
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        res = bus.call_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.Settings",
            "Read",
            GLib.Variant("(ss)", ("org.freedesktop.appearance", "accent-color")),
            GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE,
            250,
            None,
        )
        inner = res.get_child_value(0).get_variant()
        if inner.get_type_string().startswith("v"):
            inner = inner.get_variant()
        rgb = inner.unpack()
        if isinstance(rgb, (tuple, list)) and len(rgb) >= 3:
            r, g, b = (float(rgb[0]), float(rgb[1]), float(rgb[2]))
            if 0.0 <= r <= 1.0 and 0.0 <= g <= 1.0 and 0.0 <= b <= 1.0:
                hi, lo = max(r, g, b), min(r, g, b)
                if hi > 0.08 and (hi - lo) >= 0.08:
                    return (
                        f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"
                    )
    except (GLib.Error, TypeError, ValueError):
        return None
    return None


def detect_desktop_accent(
    kdeglobals_path: str | None = None,
    appletsrc_path: str | None = None,
    use_portal: bool = True,
) -> str | None:
    """Detect automatic accent color from desktop portal, KDE config, or wallpaper."""
    if use_portal and kdeglobals_path is None and appletsrc_path is None:
        portal_col = _portal_accent()
        if portal_col:
            return portal_col

    cfg_dir = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    kde_path = kdeglobals_path or os.path.join(cfg_dir, "kdeglobals")
    kde_col = _read_kde_accent(kde_path)
    if kde_col:
        return kde_col

    app_path = appletsrc_path or os.path.join(
        cfg_dir, "plasma-org.kde.plasma.desktop-appletsrc"
    )
    wp_path = _read_plasma_wallpaper(app_path)
    if wp_path and os.path.isfile(wp_path):
        try:
            mtime = os.path.getmtime(wp_path)
        except OSError:
            mtime = 0.0
        cached = _WALLPAPER_CACHE.get(wp_path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        col = extract_accent_color(wp_path)
        _WALLPAPER_CACHE.clear()
        _WALLPAPER_CACHE[wp_path] = (mtime, col)
        return col
    return None


class MprisService(GObject.Object):
    __gsignals__: ClassVar[dict] = {
        # emitted whenever the active player or its metadata changes
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # emitted when album art for the active player finished loading
        "art-ready": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self._players: dict[str, Player] = {}
        self._owners: dict[str, str] = {}
        self._subs: dict[str, int] = {}
        self._counter = 0
        self._preferred_bus = ""
        self._last_snapshot: tuple | None = None
        self.active: Player | None = None
        self._bus: Gio.DBusConnection | None = None
        self._name_sub = 0
        self._stopped = False

        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            return
        self._name_sub = self._bus.signal_subscribe(
            "org.freedesktop.DBus",
            "org.freedesktop.DBus",
            "NameOwnerChanged",
            "/org/freedesktop/DBus",
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_name_owner_changed,
        )
        self._bus.call(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "ListNames",
            None,
            GLib.VariantType("(as)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_list_names,
        )

    def stop(self):
        self._stopped = True
        if self._bus is not None:
            if self._name_sub:
                self._bus.signal_unsubscribe(self._name_sub)
                self._name_sub = 0
            for sub in self._subs.values():
                self._bus.signal_unsubscribe(sub)
        self._subs.clear()
        self._owners.clear()
        self._players.clear()
        self.active = None

    def list_players(self) -> list[Player]:
        return list(self._players.values())

    def select_player(self, bus_name: str):
        self._preferred_bus = bus_name if bus_name in self._players else ""
        self._elect()

    # -- discovery ---------------------------------------------------------

    def _on_list_names(self, bus, res):
        if self._stopped:
            return
        try:
            names = bus.call_finish(res).unpack()[0]
        except GLib.Error:
            return
        for name in names:
            if name.startswith(MPRIS_PREFIX):
                self._add_player(name)

    def _on_name_owner_changed(self, bus, sender, path, iface, signal, params):
        if self._stopped:
            return
        try:
            name, old, new = params.unpack()
        except (GLib.Error, TypeError, ValueError):
            return
        if not name.startswith(MPRIS_PREFIX):
            return
        if old:
            self._owners.pop(old, None)
        if new and not old:
            self._owners[new] = name
            self._add_player(name)
        elif new and old:
            self._owners[new] = name
        elif old and not new:
            self._remove_player(name)

    def _add_player(self, name: str):
        if name in self._players or self._bus is None:
            return
        self._players[name] = Player(name)
        self._bus.call(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "GetNameOwner",
            GLib.Variant("(s)", (name,)),
            GLib.VariantType("(s)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_initial_owner,
            name,
        )
        self._subs[name] = self._bus.signal_subscribe(
            name,
            "org.freedesktop.DBus.Properties",
            "PropertiesChanged",
            OBJECT_PATH,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_props_changed,
        )
        self._bus.call(
            name,
            OBJECT_PATH,
            "org.freedesktop.DBus.Properties",
            "GetAll",
            GLib.Variant("(s)", (PLAYER_IFACE,)),
            GLib.VariantType("(a{sv})"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._on_get_all,
            name,
        )

    def _on_initial_owner(self, bus, res, name: str):
        if self._stopped:
            return
        try:
            owner = bus.call_finish(res).unpack()[0]
        except GLib.Error:
            return
        if name in self._players and owner:
            self._owners[owner] = name

    def _remove_player(self, name: str):
        self._players.pop(name, None)
        if self._preferred_bus == name:
            self._preferred_bus = ""
        for owner, wname in list(self._owners.items()):
            if wname == name:
                self._owners.pop(owner, None)
        sub = self._subs.pop(name, None)
        if sub and self._bus is not None:
            self._bus.signal_unsubscribe(sub)
        self._elect()

    # -- state -------------------------------------------------------------

    def _on_get_all(self, bus, res, name):
        if self._stopped:
            return
        try:
            props = bus.call_finish(res).unpack()[0]
        except GLib.Error:
            return
        self._apply(name, props)

    def _on_props_changed(self, bus, sender, path, iface, signal, params):
        if self._stopped:
            return
        try:
            _iface, changed, _invalid = params.unpack()
        except (GLib.Error, TypeError, ValueError):
            return
        if not any(
            k in changed
            for k in ("PlaybackStatus", "Metadata", "CanGoNext", "CanGoPrevious")
        ):
            return
        known = self._owners.get(sender)
        if known and known in self._players:
            self._apply(known, changed)
            return
        self._resolve_sender(sender, changed)

    def _resolve_sender(self, sender: str, changed: dict):
        if self._bus is None:
            return
        for name in list(self._players):
            self._bus.call(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "GetNameOwner",
                GLib.Variant("(s)", (name,)),
                GLib.VariantType("(s)"),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
                self._on_owner_resolved,
                (name, sender, changed),
            )

    def _on_owner_resolved(self, bus, res, data):
        if self._stopped:
            return
        name, sender, changed = data
        try:
            owner = bus.call_finish(res).unpack()[0]
        except GLib.Error:
            return
        if owner:
            self._owners[owner] = name
        if owner == sender:
            self._apply(name, changed)

    def _apply(self, name: str, props: dict):
        player = self._players.get(name)
        if player is None:
            return
        if "PlaybackStatus" in props:
            new_status = str(props["PlaybackStatus"] or "Stopped")
            if new_status != player.status:
                player.status = new_status
                self._counter += 1
                player.stamp = self._counter
        if "CanGoNext" in props:
            player.can_next = bool(props["CanGoNext"])
        if "CanGoPrevious" in props:
            player.can_prev = bool(props["CanGoPrevious"])
        if "Metadata" in props:
            meta = props["Metadata"]
            if isinstance(meta, dict):
                player.title = str(meta.get("xesam:title", "") or "")
                artists = meta.get("xesam:artist", [])
                if isinstance(artists, (list, tuple)):
                    player.artist = ", ".join(str(a) for a in artists if a)
                else:
                    player.artist = str(artists or "")
                player.album = str(meta.get("xesam:album", "") or "")
                player.length_us = int(meta.get("mpris:length", 0) or 0)
                player.track_id = str(meta.get("mpris:trackid", "") or "")
                art = str(meta.get("mpris:artUrl", "") or "")
                if art != player.art_url:
                    player.art_url = art
                    player.art_path = ""
                    self._fetch_art(player)
        self._elect()

    def _elect(self):
        if self._preferred_bus and self._preferred_bus in self._players:
            pref = self._players[self._preferred_bus]
            if pref.status in ("Playing", "Paused"):
                new = pref
            else:
                new = None
        else:
            new = None
        if new is None:
            candidates = list(self._players.values())
            playing = [p for p in candidates if p.status == "Playing"]
            paused = [p for p in candidates if p.status == "Paused"]
            pool = playing or paused
            new = max(pool, key=lambda p: p.stamp) if pool else None
        self.active = new
        snap = (
            (
                new.bus_name,
                new.status,
                new.title,
                new.artist,
                new.album,
                new.art_path,
                new.length_us,
                new.track_id,
                new.can_next,
                new.can_prev,
            )
            if new is not None
            else None
        )
        if snap != self._last_snapshot:
            self._last_snapshot = snap
            self.emit("changed")

    # -- album art ---------------------------------------------------------

    def _fetch_art(self, player: Player):
        url = player.art_url
        if not url:
            return
        if url.startswith("file://"):
            try:
                path = GLib.filename_from_uri(url)[0]
                if path and os.path.isfile(path):
                    player.art_path = path
                    self.emit("art-ready")
            except GLib.Error:
                player.art_path = ""
            return
        if not url.startswith(("http://", "https://")):
            return
        os.makedirs(config.CACHE_DIR, exist_ok=True)
        dest = os.path.join(
            config.CACHE_DIR, hashlib.sha1(url.encode()).hexdigest() + ".img"
        )
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            player.art_path = dest
            self.emit("art-ready")
            return

        def worker():
            tmp = f"{dest}.{threading.get_ident()}.tmp"
            try:
                with (
                    urllib.request.urlopen(url, timeout=10) as r,
                    open(tmp, "wb") as f,
                ):
                    data = r.read(MAX_ART_BYTES)
                    if not data:
                        raise ValueError("empty art response")
                    f.write(data)
                os.replace(tmp, dest)
            except (
                http.client.HTTPException,
                OSError,
                TimeoutError,
                ValueError,
                urllib.error.URLError,
            ):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return

            def done():
                if not self._stopped and player.art_url == url:
                    player.art_path = dest
                    self.emit("art-ready")
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    # -- controls / playlists / tracks ------------------------------------

    def _call(self, method: str, params=None, iface: str = PLAYER_IFACE):
        if not self.active or self._bus is None:
            return
        self._bus.call(
            self.active.bus_name,
            OBJECT_PATH,
            iface,
            method,
            params,
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
        )

    def play_pause(self):
        self._call("PlayPause")

    def next(self):
        self._call("Next")

    def previous(self):
        self._call("Previous")

    def seek_to(self, position_us: int):
        if self.active and self.active.track_id:
            self._call(
                "SetPosition", GLib.Variant("(ox)", (self.active.track_id, position_us))
            )

    def get_playlists(self, callback):
        """Fetch playlists from org.mpris.MediaPlayer2.Playlists if exposed."""
        if not self.active or self._bus is None:
            callback([])
            return
        self._bus.call(
            self.active.bus_name,
            OBJECT_PATH,
            PLAYLISTS_IFACE,
            "GetPlaylists",
            GLib.Variant("(uusb)", (0, 20, "Alphabetical", False)),
            GLib.VariantType("(a(oss))"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            lambda b, r: self._on_playlists(b, r, callback),
        )

    def _on_playlists(self, bus, res, callback):
        try:
            rows = bus.call_finish(res).unpack()[0]
            callback([{"id": p[0], "name": p[1]} for p in rows])
        except GLib.Error:
            callback([])

    def activate_playlist(self, playlist_id: str):
        if playlist_id:
            self._call(
                "ActivatePlaylist",
                GLib.Variant("(o)", (playlist_id,)),
                iface=PLAYLISTS_IFACE,
            )

    def get_tracks(self, callback):
        """Fetch tracks from org.mpris.MediaPlayer2.TrackList if exposed."""
        if not self.active or self._bus is None:
            callback([])
            return
        self._bus.call(
            self.active.bus_name,
            OBJECT_PATH,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (TRACKLIST_IFACE, "Tracks")),
            GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            lambda b, r: self._on_track_ids(b, r, callback),
        )

    def _on_track_ids(self, bus, res, callback):
        try:
            track_ids = list(bus.call_finish(res).unpack()[0])[:25]
        except GLib.Error:
            callback([])
            return
        if not track_ids or not self.active or self._bus is None:
            callback([])
            return
        self._bus.call(
            self.active.bus_name,
            OBJECT_PATH,
            TRACKLIST_IFACE,
            "GetTracksMetadata",
            GLib.Variant("(ao)", (track_ids,)),
            GLib.VariantType("(aa{sv})"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            lambda b, r: self._on_tracks_meta(b, r, callback),
        )

    def _on_tracks_meta(self, bus, res, callback):
        try:
            metas = bus.call_finish(res).unpack()[0]
        except GLib.Error:
            callback([])
            return
        out = []
        for meta in metas:
            tid = str(meta.get("mpris:trackid", "") or "")
            title = str(meta.get("xesam:title", "") or tid)
            if tid:
                out.append({"id": tid, "title": title})
        callback(out)

    def go_to_track(self, track_id: str):
        if track_id:
            self._call(
                "GoTo",
                GLib.Variant("(o)", (track_id,)),
                iface=TRACKLIST_IFACE,
            )

    def get_position(self, callback):
        """Async position fetch in µs; calls callback(pos_us)."""
        if not self.active or self._bus is None:
            return
        self._bus.call(
            self.active.bus_name,
            OBJECT_PATH,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (PLAYER_IFACE, "Position")),
            GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            lambda bus, res: self._on_position(bus, res, callback),
        )

    def _on_position(self, bus, res, callback):
        try:
            pos = bus.call_finish(res).unpack()[0]
        except GLib.Error:
            return
        callback(int(pos))
