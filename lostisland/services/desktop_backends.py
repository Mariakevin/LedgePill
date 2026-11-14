"""Backend helpers, compositor bridges, tray matching, and coding-agent detection."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

from gi.repository import Gio, GLib

_PKG_PROCS = {"pacman", "paru", "yay", "apt", "apt-get", "dpkg", "dnf", "zypper"}
_BUILD_PROCS = {
    "gradle": "Gradle",
    "gradlew": "Gradle",
    "mvn": "Maven",
    "cargo": "Cargo",
    "ninja": "Ninja",
    "make": "Make",
    "cmake": "CMake",
}
_AGENT_PROCS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "aider": "Aider",
    "opencode": "OpenCode",
    "goose": "Goose",
}
_AGENT_ARG_ALIASES = {
    "claude-code": "claude",
    "aider-chat": "aider",
}
_AGENT_PACKAGE_MARKERS = {
    "/@anthropic-ai/claude-code/": "claude",
    "/@openai/codex/": "codex",
    "/site-packages/aider/": "aider",
    "/opencode-ai/": "opencode",
}
_WRAPPER_PROCS = {
    "env",
    "sh",
    "bash",
    "dash",
    "zsh",
    "fish",
    "python",
    "python3",
    "node",
    "bun",
    "deno",
    "electron",
    "java",
    "flatpak",
    "snap",
    "xdg-open",
    "gio",
    "gtk-launch",
    "dbus-launch",
}
_CMDLINE_INTERPRETERS = {
    "python",
    "python3",
    "node",
    "bun",
    "deno",
    "electron",
    "java",
    "flatpak",
}
_AGENT_CMDLINE_WRAPPERS = {"python", "python3", "node", "bun", "deno"}
_GENERIC_APP_TOKENS = {
    "app",
    "bin",
    "client",
    "desktop",
    "gui",
    "gtk",
    "launcher",
    "main",
    "qt",
    "ui",
}
_IGNORE_PROCS = {
    "systemd",
    "sd-pam",
    "dbus-daemon",
    "dbus-broker",
    "dbus-broker-launch",
    "pipewire",
    "pipewire-pulse",
    "wireplumber",
    "dconf-service",
    "gnome-keyring-daemon",
    "kwalletd5",
    "kwalletd6",
    "ssh-agent",
    "gpg-agent",
    "sh",
    "bash",
    "zsh",
    "fish",
    "dash",
    "sleep",
    "fusermount3",
    "bwrap",
    "polkitd",
    "gamemoded",
    "kwin_wayland",
    "kwin_x11",
    "plasmashell",
    "kded5",
    "kded6",
    "ksmserver",
    "kglobalacceld",
    "gnome-shell",
    "mutter",
    "hyprland",
    "sway",
    "xwayland",
    "xdg-dbus-proxy",
    "flatpak-session-helper",
    "flatpak-portal",
    "chrome_crashpad_handler",
    "crashpad_handler",
}
_IGNORE_PREFIXES = (
    "gvfs",
    "xdg-desktop-portal",
    "xdg-document-portal",
    "xdg-permission-store",
    "at-spi",
    "evolution-",
    "kactivitymanagerd",
    "uresourced",
    "polkit-",
    "xwayland",
)
_NON_APP_TRAY_CATEGORIES = {"Hardware", "SystemServices"}
_IGNORE_TRAY_TOKENS = (
    "fcitx",
    "ibus",
    "xembedsniproxy",
    "klipper",
    "kmix",
    "powerdevil",
    "discovernotifier",
    "org.kde.discover",
    "org.kde.plasma",
    "kded5",
    "kded6",
    "pipewire",
    "wireplumber",
)
_GENERIC_TRAY_NAMES = {
    "",
    "menu",
    "statusnotifieritem",
    "application",
    "org",
    "ayatana",
    "notificationitem",
    "kde",
    "freedesktop",
}
_WINDOWS_IFACE_XML = """
<node>
  <interface name="it.zorn.LostIsland.Windows">
    <method name="SyncWindows">
      <arg type="s" name="windows_json" direction="in"/>
    </method>
  </interface>
</node>
"""
_WINDOWS_DBUS_PATH = "/it/zorn/LostIsland/Windows"
_WINDOWS_DBUS_IFACE = "it.zorn.LostIsland.Windows"
_KWIN_WINDOWS_SCRIPT = "lostisland-windows"


def _spawn(cmd: list[str]) -> bool:
    try:
        Gio.Subprocess.new(cmd, Gio.SubprocessFlags.NONE)
        return True
    except GLib.Error:
        return False


def _coding_agent_for_process(pid: str, comm: str) -> str:
    """Return a known coding-agent label without retaining its command line."""
    key = comm.lower().removesuffix("-bin")
    key = _AGENT_ARG_ALIASES.get(key, key)
    if key in _AGENT_PROCS:
        return _AGENT_PROCS[key]
    if key not in _AGENT_CMDLINE_WRAPPERS:
        return ""

    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read(8192)
    except OSError:
        return ""
    args = [part.decode("utf-8", "ignore") for part in raw.split(b"\x00") if part]
    candidates: list[str] = []
    if args:
        candidates.append(args[0])
    for index, arg in enumerate(args[:-1]):
        if arg == "-m" or key in {"bun", "deno"} and arg in {"run", "x", "--bun"}:
            candidates.append(args[index + 1])
        elif arg.startswith("/") and (
            arg.endswith((".js", ".cjs", ".mjs", ".py"))
            or "node_modules" in arg
            or "/bin/" in arg
        ):
            candidates.append(arg)

    for candidate in candidates:
        path = candidate.lower().replace("\\", "/")
        for marker, alias in _AGENT_PACKAGE_MARKERS.items():
            if marker in path:
                return _AGENT_PROCS[alias]
        name = os.path.basename(path.rstrip("/"))
        for suffix in (".js", ".cjs", ".mjs", ".py", ".exe"):
            name = name.removesuffix(suffix)
        name = _AGENT_ARG_ALIASES.get(
            name.removesuffix("-bin"), name.removesuffix("-bin")
        )
        if name in _AGENT_PROCS:
            return _AGENT_PROCS[name]
    return ""


def _run_json(cmd: list[str]):
    out = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=1.0)
    if out.returncode == 0 and out.stdout:
        return json.loads(out.stdout)
    return None


def _sway_walk(node: dict, windows: list[dict]) -> bool:
    fs = False
    if node.get("type") in ("con", "floating_con") and node.get("name"):
        if len(windows) < 25:
            windows.append(
                {
                    "id": str(node.get("id", "")),
                    "title": str(node.get("name", "")),
                    "app": str(node.get("app_id") or ""),
                    "active": bool(node.get("focused", False)),
                }
            )
        if node.get("focused") and node.get("fullscreen_mode", 0):
            fs = True
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        if isinstance(child, dict) and _sway_walk(child, windows):
            fs = True
    return fs


def _app_id_aliases(app_id: str) -> list[str]:
    """Return non-generic lookup tokens extracted from a reverse-DNS app id."""
    parts = [p for p in app_id.split(".") if p]
    if not parts:
        return []
    aliases: list[str] = []
    tail = parts[-1]
    if tail not in _GENERIC_APP_TOKENS and tail not in _WRAPPER_PROCS:
        aliases.append(tail)
    elif len(parts) >= 2:
        prev = parts[-2]
        if prev not in _GENERIC_APP_TOKENS and prev not in _WRAPPER_PROCS:
            aliases.append(prev)
    return aliases


def _tray_pid_from_service(item: str) -> int:
    service = item.split("/", 1)[0]
    for tok in service.split("-"):
        if tok.isdigit() and int(tok) > 1:
            return int(tok)
    return 0


def _read_ppid(pid: str) -> int:
    try:
        with open(f"/proc/{pid}/stat") as f:
            tail = f.read().rsplit(")", 1)[-1].split()
        return int(tail[1]) if len(tail) > 1 else 0
    except (OSError, ValueError, IndexError):
        return 0


def _is_generic_tray_name(name: str) -> bool:
    cf = name.strip().casefold()
    return cf in _GENERIC_TRAY_NAMES or cf.startswith("chrome_status_icon")


def _is_user_tray_item(item: str, meta: dict) -> bool:
    if str(meta.get("category") or "") in _NON_APP_TRAY_CATEGORIES:
        return False
    hay = " ".join(
        str(v or "") for v in (meta.get("id"), meta.get("title"), item)
    ).casefold()
    return not any(tok in hay for tok in _IGNORE_TRAY_TOKENS)


def _friendly_tray_name(
    item: str, meta: dict, dmap: dict[str, str], fallback: str = ""
) -> str:
    tid = str(meta.get("id") or "").strip()
    if tid:
        for cand in (tid.lower(), *_app_id_aliases(tid.lower())):
            if cand in dmap:
                return dmap[cand]
    for cand in (meta.get("title"), meta.get("tooltip")):
        txt = str(cand or "").strip()
        if txt and not _is_generic_tray_name(txt):
            return txt
    if fallback and not _is_generic_tray_name(fallback):
        return fallback
    if tid and not _is_generic_tray_name(tid):
        return tid.replace("-", " ").replace("_", " ").title() if tid.islower() else tid
    path = item.split("/", 1)[-1] if "/" in item else ""
    for seg in path.split("/"):
        if seg and not _is_generic_tray_name(seg):
            return (
                seg.replace("_", " ").replace("-", " ").title()
                if seg.islower()
                else seg
            )
    return fallback.strip()


def _match_tray_item(
    tray_list: list[str],
    tray_meta: dict[str, dict],
    pid: int,
    ppid: int,
    key: str,
    low: str,
    gppid: int = 0,
) -> tuple[str, str]:
    pid_markers = (f"-{pid}-", f"-{pid}/")
    ancestors = {p for p in (pid, ppid, gppid) if p > 1}
    for t in tray_list:
        meta = tray_meta.get(t, {})
        tpid = int(meta.get("pid") or 0)
        tid = str(meta.get("id") or "")
        title = str(meta.get("title") or "")
        disp = title or str(meta.get("tooltip") or "") or tid
        if tpid and tpid in ancestors:
            return t, disp
        tl = t.casefold()
        if any(m in tl for m in pid_markers) or tl.endswith(f"-{pid}"):
            return t, disp
        id_cf = tid.casefold()
        title_cf = title.casefold()
        if (len(key) >= 3 and (key in tl or key in id_cf or key in title_cf)) or (
            len(low) >= 3 and (low in tl or low in id_cf or low in title_cf)
        ):
            return t, disp
    return "", ""


def _match_open_window(
    win_list: list[dict],
    pid: int,
    ppid: int,
    key: str,
    low: str,
    label: str = "",
    dmap: dict[str, str] | None = None,
) -> str:
    lbl_cf = (label or key).casefold()
    for w in win_list:
        wpid = w.get("pid")
        if wpid and (wpid == pid or (ppid > 1 and wpid == ppid)):
            return str(w.get("id", ""))
        wapp = str(w.get("app", "")).casefold()
        wdesk = str(w.get("desktop", "")).removesuffix(".desktop").casefold()
        wtitle = str(w.get("title", "")).casefold()
        if dmap is not None:
            for cand in (wdesk, wapp):
                if cand and dmap.get(cand, "").casefold() == lbl_cf:
                    return str(w.get("id", ""))
        for tok in (wapp, wdesk):
            if tok and (tok in key or key in tok or (len(low) >= 3 and low in tok)):
                return str(w.get("id", ""))
        if wtitle and len(key) >= 3 and key in wtitle:
            return str(w.get("id", ""))
    return ""


def _build_kwin_windows_js(unique_name: str) -> str:
    return (
        "(function () {\n"
        f"    var SVC = {json.dumps(unique_name)};\n"
        f"    var PATH = {json.dumps(_WINDOWS_DBUS_PATH)};\n"
        f"    var IFACE = {json.dumps(_WINDOWS_DBUS_IFACE)};\n"
        "    function sendWindows() {\n"
        '        if (typeof workspace === "undefined") { return; }\n'
        "        var list = workspace.windowList ? workspace.windowList() : [];\n"
        "        var out = [];\n"
        "        for (var i = 0; i < list.length; i++) {\n"
        "            var w = list[i];\n"
        "            if (!w || w.specialWindow || w.skipTaskbar) { continue; }\n"
        "            out.push({\n"
        "                id: String(w.internalId || i),\n"
        "                pid: Number(w.pid || 0),\n"
        '                app: String(w.resourceClass || w.resourceName || ""),\n'
        '                desktop: String(w.desktopFileName || ""),\n'
        '                title: String(w.caption || ""),\n'
        "                active: Boolean(workspace.activeWindow === w)\n"
        "            });\n"
        "        }\n"
        '        callDBus(SVC, PATH, IFACE, "SyncWindows", JSON.stringify(out));\n'
        "    }\n"
        '    if (typeof workspace !== "undefined") {\n'
        '        if (workspace.windowAdded && typeof workspace.windowAdded.connect === "function") { workspace.windowAdded.connect(sendWindows); }\n'
        '        if (workspace.windowRemoved && typeof workspace.windowRemoved.connect === "function") { workspace.windowRemoved.connect(sendWindows); }\n'
        '        if (workspace.windowActivated && typeof workspace.windowActivated.connect === "function") { workspace.windowActivated.connect(sendWindows); }\n'
        "    }\n"
        "    sendWindows();\n"
        "})();\n"
    )


class _KdeScriptBridge:
    """Reusable KWin Plasma 5/6 script loader and D-Bus callback endpoint."""

    def __init__(
        self,
        dbus_path: str,
        iface_xml: str,
        method_name: str,
        script_name: str,
        js_builder,
        on_call,
        on_ready=None,
    ):
        self._dbus_path = dbus_path
        self._iface_xml = iface_xml
        self._method_name = method_name
        self._script_name = script_name
        self._js_builder = js_builder
        self._on_call = on_call
        self._on_ready = on_ready
        self._bus: Gio.DBusConnection | None = None
        self._reg_id = 0
        self._loaded = False
        self._js_path = ""
        self._active = False
        self._gen = 0

    def start(self, bus: Gio.DBusConnection) -> bool:
        self._bus = bus
        unique = bus.get_unique_name()
        if not unique:
            return False
        self._gen += 1
        gen = self._gen
        try:
            node = Gio.DBusNodeInfo.new_for_xml(self._iface_xml)
            self._reg_id = bus.register_object(
                self._dbus_path,
                node.interfaces[0],
                self._handle_call,
                None,
                None,
            )
        except GLib.Error:
            self._reg_id = 0
            return False
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                prefix=f"{self._script_name}-",
                suffix=".js",
                encoding="utf-8",
                delete=False,
            ) as tmp:
                tmp.write(self._js_builder(unique))
                js_path = tmp.name
        except OSError:
            self.stop()
            return False
        self._active = True
        self._js_path = js_path
        bus.call(
            "org.kde.KWin",
            "/Scripting",
            "org.kde.kwin.Scripting",
            "unloadScript",
            GLib.Variant("(s)", (self._script_name,)),
            None,
            Gio.DBusCallFlags.NONE,
            1000,
            None,
            lambda b, r: self._on_unloaded(b, r, js_path, gen),
        )
        return True

    def _on_unloaded(self, bus, res, js_path: str, gen: int):
        try:
            bus.call_finish(res)
        except GLib.Error:
            pass
        if not self._active or gen != self._gen or self._bus is None:
            self._remove_file(js_path)
            return
        self._bus.call(
            "org.kde.KWin",
            "/Scripting",
            "org.kde.kwin.Scripting",
            "loadScript",
            GLib.Variant("(ss)", (js_path, self._script_name)),
            GLib.VariantType("(i)"),
            Gio.DBusCallFlags.NONE,
            1500,
            None,
            lambda b, r: self._on_loaded(b, r, js_path, gen),
        )

    def _on_loaded(self, bus, res, js_path: str, gen: int):
        try:
            script_id = int(bus.call_finish(res).unpack()[0])
        except (GLib.Error, TypeError, ValueError):
            self._remove_file(js_path)
            return
        if script_id < 0 or not self._active or gen != self._gen or self._bus is None:
            self._remove_file(js_path)
            return
        self._loaded = True
        self._run(script_id, f"/{script_id}", js_path, gen, fallback=True)

    def _run(
        self, script_id: int, obj_path: str, js_path: str, gen: int, fallback: bool
    ):
        if self._bus is None:
            self._remove_file(js_path)
            return
        self._bus.call(
            "org.kde.KWin",
            obj_path,
            "org.kde.kwin.Script",
            "run",
            None,
            None,
            Gio.DBusCallFlags.NONE,
            1000,
            None,
            lambda b, r: self._on_ran(b, r, script_id, js_path, gen, fallback),
        )

    def _on_ran(self, bus, res, script_id: int, js_path: str, gen: int, fallback: bool):
        try:
            bus.call_finish(res)
        except GLib.Error:
            if fallback and self._active and gen == self._gen:
                self._run(
                    script_id,
                    f"/Scripting/Script{script_id}",
                    js_path,
                    gen,
                    fallback=False,
                )
                return
            self._remove_file(js_path)
            return
        self._remove_file(js_path)
        if self._active and gen == self._gen and self._on_ready is not None:
            self._on_ready()

    def _handle_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        invocation.return_value(None)
        if not self._active or method != self._method_name:
            return
        try:
            arg = params.unpack()[0]
        except (TypeError, IndexError):
            return
        self._on_call(arg)

    def stop(self):
        self._active = False
        self._gen += 1
        if self._bus is not None and self._reg_id:
            try:
                self._bus.unregister_object(self._reg_id)
            except GLib.Error:
                pass
            self._reg_id = 0
        if self._bus is not None and self._loaded:
            self._loaded = False
            try:
                self._bus.call_sync(
                    "org.kde.KWin",
                    "/Scripting",
                    "org.kde.kwin.Scripting",
                    "unloadScript",
                    GLib.Variant("(s)", (self._script_name,)),
                    None,
                    Gio.DBusCallFlags.NONE,
                    250,
                    None,
                )
            except GLib.Error:
                pass
        if self._js_path:
            self._remove_file(self._js_path)
            self._js_path = ""

    @staticmethod
    def _remove_file(path: str):
        try:
            if path and os.path.exists(path):
                os.unlink(path)
        except OSError:
            pass


def _wmctrl_windows() -> list[dict]:
    out = subprocess.run(
        ["wmctrl", "-lpx"], capture_output=True, text=True, check=False, timeout=1.0
    )
    if out.returncode != 0 or not out.stdout:
        return []
    windows: list[dict] = []
    for line in out.stdout.splitlines()[:25]:
        cols = line.split(None, 5)
        if len(cols) < 6:
            continue
        wid, _desk, pid_s, wm_class, _host, title = cols
        try:
            wpid = int(pid_s)
        except ValueError:
            wpid = 0
        app_tok = wm_class.split(".", 1)[0]
        windows.append(
            {"id": wid, "pid": wpid, "app": app_tok, "title": title, "active": False}
        )
    return windows
