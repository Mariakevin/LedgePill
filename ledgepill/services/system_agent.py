"""System context snapshot and safe typed tool-calling control for the AI Assistant."""

from __future__ import annotations

import json
import math
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable

from gi.repository import Gio, GLib

from ledgepill import config
from ledgepill.config import VALID_ACCENT_MODES, VALID_STYLES, VALID_THEMES

_TOOL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_REAL_FS = {"ext4", "ext3", "btrfs", "xfs", "f2fs", "zfs", "vfat", "exfat", "ntfs3"}
_POWER_DBUS_TARGETS = (
    ("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles"),
    (
        "org.freedesktop.UPower.PowerProfiles",
        "/org/freedesktop/UPower/PowerProfiles",
    ),
)
try:
    _PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
except (OSError, ValueError):
    _PAGE_SIZE = 4096


def _read_uptime_and_load() -> tuple[float, list[float]]:
    uptime_s = 0.0
    try:
        with open("/proc/uptime") as f:
            uptime_s = round(float(f.read().split()[0]), 1)
    except (OSError, ValueError, IndexError):
        pass
    try:
        load = [round(v, 2) for v in os.getloadavg()]
    except OSError:
        load = []
    return uptime_s, load


def _read_top_processes(sort_by: str = "ram", limit: int = 8) -> list[dict]:
    uid = os.getuid()
    cap = max(1, min(15, int(limit or 8)))
    rows: list[dict] = []
    try:
        pids = os.listdir("/proc")
    except OSError:
        return []
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
            if not comm:
                continue
            with open(f"/proc/{pid}/statm") as f:
                rss_pages = int(f.read().split()[1])
            with open(f"/proc/{pid}/stat") as f:
                stat_tail = f.read().rsplit(")", 1)[-1].split()
            # utime + stime (fields 14 & 15 -> index 11 & 12 after ')')
            cpu_ticks = (
                int(stat_tail[11]) + int(stat_tail[12]) if len(stat_tail) > 12 else 0
            )
            rss_mb = round((rss_pages * _PAGE_SIZE) / (1024 * 1024), 1)
            rows.append(
                {
                    "pid": int(pid),
                    "name": comm,
                    "rss_mb": rss_mb,
                    "cpu_time_ticks": cpu_ticks,
                }
            )
        except (OSError, ValueError, IndexError):
            continue
    key = (
        (lambda r: (-r["cpu_time_ticks"], -r["rss_mb"]))
        if sort_by == "cpu"
        else (lambda r: (-r["rss_mb"], -r["cpu_time_ticks"]))
    )
    rows.sort(key=key)
    return rows[:cap]


def _read_disk_usage() -> list[dict]:
    mounts: list[dict] = []
    seen_devs: set[str] = set()
    try:
        with open("/proc/mounts") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mnt, fstype = parts[0], parts[1], parts[2]
        if fstype not in _REAL_FS or dev in seen_devs:
            continue
        if mnt.startswith(("/snap", "/var/lib/docker", "/boot/efi")):
            continue
        try:
            st = os.statvfs(mnt)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if total <= 0:
            continue
        free = st.f_bavail * st.f_frsize
        used = total - free
        seen_devs.add(dev)
        gib = 1024**3
        mounts.append(
            {
                "mount": mnt,
                "fs": fstype,
                "total_gib": round(total / gib, 1),
                "used_gib": round(used / gib, 1),
                "free_gib": round(free / gib, 1),
                "used_pct": round(100.0 * used / total, 1),
            }
        )
        if len(mounts) >= 8:
            break
    return mounts


def _read_systemd_status(scope: str = "user", include_logs: bool = False) -> dict:
    sc = "system" if scope == "system" else "user"
    failed: list[str] = []
    errors: list[str] = []
    if shutil.which("systemctl"):
        cmd = ["systemctl"]
        if sc == "user":
            cmd.append("--user")
        cmd.extend(["--failed", "--no-pager", "--plain", "--no-legend"])
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, check=False, timeout=2.0
            )
            if out.returncode == 0 and out.stdout.strip():
                failed = [
                    ln.strip() for ln in out.stdout.splitlines()[:12] if ln.strip()
                ]
        except (OSError, subprocess.SubprocessError):
            pass
    if include_logs and shutil.which("journalctl"):
        jcmd = ["journalctl"]
        if sc == "user":
            jcmd.append("--user")
        jcmd.extend(["-p", "err", "-n", "8", "--no-pager", "-o", "short-iso"])
        try:
            jout = subprocess.run(
                jcmd, capture_output=True, text=True, check=False, timeout=2.0
            )
            if jout.returncode == 0 and jout.stdout.strip():
                errors = [
                    ln.strip()[:220]
                    for ln in jout.stdout.splitlines()[:8]
                    if ln.strip()
                ]
        except (OSError, subprocess.SubprocessError):
            pass
    return {"ok": True, "scope": sc, "failed_units": failed, "recent_errors": errors}


def _read_power_profile() -> str:
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        for service, path in _POWER_DBUS_TARGETS:
            try:
                res = bus.call_sync(
                    service,
                    path,
                    "org.freedesktop.DBus.Properties",
                    "Get",
                    GLib.Variant("(ss)", (service, "ActiveProfile")),
                    GLib.VariantType("(v)"),
                    Gio.DBusCallFlags.NONE,
                    600,
                    None,
                )
                val = str(res.unpack()[0] or "").strip()
                if val:
                    return val
            except GLib.Error:
                continue
    except GLib.Error:
        pass
    if shutil.which("powerprofilesctl"):
        try:
            out = subprocess.run(
                ["powerprofilesctl", "get"],
                capture_output=True,
                text=True,
                check=False,
                timeout=1.0,
            )
            if out.returncode == 0:
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return ""


def _set_power_profile(profile: str) -> dict:
    if profile not in ("power-saver", "balanced", "performance"):
        return {"ok": False, "error": f"Unsupported profile '{profile}'"}
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        for service, path in _POWER_DBUS_TARGETS:
            try:
                bus.call_sync(
                    service,
                    path,
                    "org.freedesktop.DBus.Properties",
                    "Set",
                    GLib.Variant(
                        "(ssv)",
                        (service, "ActiveProfile", GLib.Variant("s", profile)),
                    ),
                    None,
                    Gio.DBusCallFlags.NONE,
                    1200,
                    None,
                )
                return {"ok": True, "profile": profile}
            except GLib.Error:
                continue
    except GLib.Error:
        pass
    if shutil.which("powerprofilesctl"):
        try:
            out = subprocess.run(
                ["powerprofilesctl", "set", profile],
                capture_output=True,
                text=True,
                check=False,
                timeout=2.0,
            )
            if out.returncode == 0:
                return {"ok": True, "profile": profile}
            return {
                "ok": False,
                "error": (
                    out.stderr or out.stdout or "Failed to set power profile"
                ).strip(),
            }
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": "Power profile daemon unavailable"}


class SystemAgent:
    """Provides live desktop telemetry snapshots and typed control actions for LLMs."""

    def __init__(
        self,
        cfg: dict,
        *,
        media=None,
        audio=None,
        brightness=None,
        network=None,
        bluetooth=None,
        power=None,
        system=None,
        desktop=None,
        weather=None,
        clipboard=None,
        notifier=None,
        ollama=None,
        todos=None,
        calendar=None,
        on_dnd_change: Callable[[bool], None] | None = None,
        on_apply_config: Callable[[bool], None] | None = None,
        on_confirm_request: Callable[[str, Callable[[], dict]], None] | None = None,
        on_ui_action: Callable[[str, dict], dict] | None = None,
    ):
        self.cfg = cfg
        self.media = media
        self.audio = audio
        self.brightness = brightness
        self.network = network
        self.bluetooth = bluetooth
        self.power = power
        self.system = system
        self.desktop = desktop
        self.weather = weather
        self.clipboard = clipboard
        self.notifier = notifier
        self.ollama = ollama
        self.todos = todos
        self.calendar = calendar
        self.on_dnd_change = on_dnd_change
        self.on_apply_config = on_apply_config
        self.on_confirm_request = on_confirm_request
        self.on_ui_action = on_ui_action
        self._main_thread_id = threading.get_ident()

    def _run_on_main(self, fn: Callable[[], dict]) -> dict:
        if threading.get_ident() == self._main_thread_id:
            try:
                return fn()
            except (
                AttributeError,
                GLib.Error,
                IndexError,
                KeyError,
                OSError,
                OverflowError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as exc:
                return {"ok": False, "error": str(exc)}

        done = threading.Event()
        cancelled = threading.Event()
        box: dict = {}

        def _invoke():
            if cancelled.is_set():
                done.set()
                return False
            try:
                box["res"] = fn()
            except (
                AttributeError,
                GLib.Error,
                IndexError,
                KeyError,
                OSError,
                OverflowError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as exc:
                box["res"] = {"ok": False, "error": str(exc)}
            finally:
                done.set()
            return False

        GLib.idle_add(_invoke)
        if not done.wait(1.5):
            cancelled.set()
            return {"ok": False, "error": "Timed out waiting for UI thread"}
        return box.get("res", {"ok": False, "error": "Empty tool response"})

    @staticmethod
    def _bool_arg(args: dict, key: str, default: bool) -> bool | None:
        value = args.get(key, default)
        return value if isinstance(value, bool) else None

    @staticmethod
    def _percent_arg(args: dict, maximum: int = 100) -> int | None:
        value = args.get("percent", 50)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return max(0, min(maximum, int(value)))

    def snapshot(self) -> dict:
        def _gather() -> dict:
            uptime_s, load_avg = _read_uptime_and_load()
            data: dict = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "host": {
                    "hostname": platform.node(),
                    "kernel": platform.release(),
                    "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
                    "uptime_seconds": uptime_s,
                    "load_avg": load_avg,
                },
            }
            if self.system is not None and getattr(self.system, "last_data", None):
                sys_d = dict(self.system.last_data)
                sys_d.pop("temps", None)
                data["system"] = {
                    **sys_d,
                    "temps": dict(self.system.last_data.get("temps", {})),
                }
            if self.power is not None and getattr(self.power, "available", False):
                data["battery"] = {
                    "percentage": round(float(self.power.percentage), 1),
                    "charging": bool(self.power.charging),
                    "health_pct": round(float(self.power.health_pct), 1)
                    if getattr(self.power, "health_pct", -1) >= 0
                    else None,
                }
            if self.audio is not None:
                data["audio"] = {
                    "volume": self.audio.percent,
                    "muted": bool(self.audio.muted),
                    "mic_muted": bool(self.audio.mic_muted),
                    "default_sink": self.audio.default_sink,
                    "sinks": [
                        {
                            "name": s["name"],
                            "desc": s.get("description", s["name"]),
                            "default": bool(s.get("default")),
                        }
                        for s in getattr(self.audio, "sinks", [])[:8]
                    ],
                    "app_streams": list(getattr(self.audio, "sink_inputs", []))[:10],
                }
            if self.brightness is not None and getattr(
                self.brightness, "available", False
            ):
                data["brightness"] = {"percent": self.brightness.percent}
            if self.network is not None:
                data["network"] = {
                    "connected": bool(self.network.connected),
                    "name": self.network.name,
                    "kind": self.network.kind,
                    "wifi_enabled": bool(self.network.wifi_enabled),
                    "wifi_signal": self.network.wifi_signal,
                    "ip_address": getattr(self.network, "ip_address", ""),
                    "nearby_wifi": [
                        n["ssid"]
                        for n in getattr(self.network, "wifi_networks", [])[:8]
                    ],
                }
            if self.bluetooth is not None:
                data["bluetooth"] = {
                    "powered": bool(self.bluetooth.powered),
                    "connected_device": self.bluetooth.device,
                    "battery": self.bluetooth.battery,
                    "devices": [
                        {
                            "name": d.get("name", ""),
                            "connected": bool(d.get("connected")),
                            "battery": d.get("battery", -1),
                        }
                        for d in self.bluetooth.devices()[:10]
                    ],
                }
            if (
                self.media is not None
                and getattr(self.media, "active", None) is not None
            ):
                p = self.media.active
                data["media"] = {
                    "player": p.label,
                    "title": p.title,
                    "artist": p.artist,
                    "album": p.album,
                    "status": p.status,
                }
            if self.weather is not None and getattr(self.weather, "temp", ""):
                data["weather"] = {
                    "location": getattr(self.weather, "location", self.weather.city),
                    "temp": self.weather.temp,
                    "desc": self.weather.desc,
                }
            if self.desktop is not None:
                apps = [
                    {
                        "name": a.get("name", ""),
                        "background": bool(a.get("background", False)),
                        "rss_mb": round(a.get("rss_mb", 0.0)),
                    }
                    for a in self.desktop.list_running_apps("", limit=12)
                ]
                pkg_act = getattr(self.desktop, "package_activity", "")
                build_act = getattr(self.desktop, "build_activity", "")
                agent_act = getattr(self.desktop, "agent_activity", "")
                data["desktop"] = {
                    "keyboard_layout": self.desktop.keyboard_layout,
                    "package_activity": pkg_act if isinstance(pkg_act, str) else "",
                    "build_activity": build_act if isinstance(build_act, str) else "",
                    "agent_activity": agent_act if isinstance(agent_act, str) else "",
                    "running_apps": apps,
                }
            if self.notifier is not None:
                hist = list(getattr(self.notifier, "history", []) or [])
                data["notifications"] = {
                    "count": len(hist),
                    "recent": [
                        {
                            "app": str(n.get("app", "")),
                            "summary": str(n.get("summary", ""))[:80],
                        }
                        for n in hist[:5]
                    ],
                }
            if self.clipboard is not None:
                c_hist = list(getattr(self.clipboard, "history", []) or [])
                c_pin = list(getattr(self.clipboard, "pinned", []) or [])
                data["clipboard"] = {
                    "count": len(c_hist),
                    "pinned_count": len(c_pin),
                    "latest_preview": c_hist[0][:80] if c_hist else "",
                }
            if self.ollama is not None:
                data["local_llm"] = {
                    "provider": self.cfg.get("ai_provider", "ollama"),
                    "ollama_available": bool(self.ollama.available),
                    "lmstudio_available": bool(self.ollama.lmstudio_available),
                    "active_model": self.ollama.model,
                    "running": list(self.ollama.running_details or [])[:4],
                    "installed_count": len(self.ollama.models),
                    "lmstudio_loaded": list(
                        getattr(self.ollama, "lmstudio_loaded", []) or []
                    )[:4],
                    "tokens_per_sec": getattr(self.ollama, "tokens_per_sec", 0.0),
                }
            if self.todos is not None:
                t_items = list(getattr(self.todos, "items", []) or [])
                data["focus_tasks"] = {
                    "pending": sum(1 for t in t_items if not t.get("done")),
                    "items": t_items[:12],
                }
            if self.calendar is not None and hasattr(self.calendar, "next_event"):
                nxt = self.calendar.next_event()
                data["calendar"] = {
                    "upcoming_count": len(getattr(self.calendar, "events", []) or []),
                    "next": (
                        {
                            "summary": nxt["summary"],
                            "minutes_until": nxt["minutes_until"],
                            "location": nxt.get("location", ""),
                        }
                        if nxt
                        else None
                    ),
                }
            data["island"] = {
                "theme": self.cfg.get("theme", "obsidian"),
                "island_style": self.cfg.get("island_style", "pill"),
                "accent_mode": self.cfg.get("accent_mode", "manual"),
                "do_not_disturb": bool(self.cfg.get("do_not_disturb", False)),
            }
            return data

        return self._run_on_main(_gather)

    def system_prompt(self, native_tools: bool = False) -> str:
        snap = json.dumps(self.snapshot(), ensure_ascii=False)
        intro = (
            "You are LedgePill's system assistant on Linux.\n"
            f"Live system state JSON: {snap}\n\n"
        )
        if native_tools:
            return (
                intro
                + "Use the provided functions directly when the user asks for live system information or a system action. "
                "Only call functions that match the request. The function schemas describe the available actions. "
                "Actions that affect the session or end an app always require the user's confirmation. "
                "After receiving function results, summarize what happened briefly. "
                "If no function is needed, answer conversationally from the live state."
            )
        return (
            intro
            + "When the user asks to check fresh telemetry or control the system, emit ONE tool call block:\n"
            '<tool_call>{"tool": "<name>", "args": {...}}</tool_call>\n'
            "Available tools:\n"
            "- get_system_snapshot {}\n"
            '- get_top_processes {"sort_by": "ram|cpu", "limit": 1..15}\n'
            "- get_disk_usage {}\n"
            '- get_systemd_status {"scope": "user|system", "include_logs": true|false}\n'
            "- get_power_profile {}\n"
            '- set_power_profile {"profile": "power-saver|balanced|performance"}\n'
            '- media_control {"action": "play_pause"|"next"|"previous"}\n'
            '- set_wifi {"enabled": true|false}\n'
            '- connect_wifi {"ssid": "<network name>"}\n'
            '- set_bluetooth {"enabled": true|false}\n'
            '- connect_bluetooth {"device": "<name>", "connect": true|false}\n'
            '- set_dnd {"enabled": true|false}\n'
            '- get_notifications {"limit": 1..12}\n'
            "- clear_notifications {}\n"
            '- search_clipboard {"query": "<text>", "limit": 1..10}\n'
            '- copy_to_clipboard {"text": "..."}\n'
            '- launch_app {"query": "<app name>"}\n'
            '- focus_app {"query": "<app or window title>"}\n'
            '- control_timer {"action": "start|pause|reset|set_minutes", "minutes": 1..180}\n'
            '- manage_todos {"action": "list|add|toggle|clear_done", "text": "...", "index": 0}\n'
            '- trigger_quick_action {"action": "caffeine_on|caffeine_off|screenshot|scan_wifi|scan_bluetooth"}\n'
            '- manage_local_llm {"action": "status|unload|load|switch_model", "model": "..."}\n'
            '- refresh_weather {"city": "optional city"}\n'
            '- get_upcoming_events {"limit": 1..10}\n'
            '- set_appearance {"theme": "obsidian|liquid-glass|frosted|midnight|amber", '
            '"island_style": "pill|waterdrop|dashboard|vertical", '
            '"accent_mode": "auto|system|artwork|manual"}\n'
            '- request_session_action {"action": "lock|suspend|reboot|poweroff"} (asks confirmation)\n'
            '- request_end_app {"query": "<app name>"} (asks confirmation)\n\n'
            "If no tool is needed, answer concisely from the live state. After a tool result, reply concisely."
        )

    def tool_schemas(self) -> list[dict]:
        """Return native function schemas for the integrations currently available."""

        def schema(name: str, description: str, properties: dict, required=()):
            return {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": list(required),
                        "additionalProperties": False,
                    },
                },
            }

        def string(description: str) -> dict:
            return {"type": "string", "description": description}

        def boolean(description: str) -> dict:
            return {"type": "boolean", "description": description}

        def number(description: str, minimum=0, maximum=100) -> dict:
            return {
                "type": "integer",
                "description": description,
                "minimum": minimum,
                "maximum": maximum,
            }

        def enum(values, description: str) -> dict:
            return {
                "type": "string",
                "enum": list(values),
                "description": description,
            }

        available = {
            "get_system_snapshot": True,
            "get_top_processes": True,
            "get_disk_usage": True,
            "get_systemd_status": True,
            "get_power_profile": True,
            "set_power_profile": True,
            "media_control": self.media is not None,
            "set_wifi": self.network is not None,
            "connect_wifi": self.network is not None,
            "set_bluetooth": self.bluetooth is not None,
            "connect_bluetooth": self.bluetooth is not None,
            "set_dnd": self.on_dnd_change is not None,
            "get_notifications": self.notifier is not None,
            "clear_notifications": self.notifier is not None,
            "launch_app": self.desktop is not None,
            "focus_app": self.desktop is not None,
            "search_clipboard": self.clipboard is not None,
            "copy_to_clipboard": self.clipboard is not None,
            "control_timer": self.on_ui_action is not None,
            "manage_todos": self.todos is not None,
            "trigger_quick_action": (
                self.on_ui_action is not None
                or self.network is not None
                or self.bluetooth is not None
            ),
            "manage_local_llm": self.ollama is not None,
            "refresh_weather": self.weather is not None,
            "get_upcoming_events": self.calendar is not None,
            "set_appearance": True,
            "request_session_action": (
                self.desktop is not None and self.on_confirm_request is not None
            ),
            "request_end_app": (
                self.desktop is not None and self.on_confirm_request is not None
            ),
        }
        definitions = [
            schema("get_system_snapshot", "Read current desktop and device state.", {}),
            schema(
                "get_top_processes",
                "Inspect top user processes sorted by RAM or cumulative CPU time.",
                {
                    "sort_by": enum(("ram", "cpu"), "Sort metric"),
                    "limit": number("Number of processes to return", 1, 15),
                },
            ),
            schema(
                "get_disk_usage",
                "Inspect mounted real filesystem partitions and free disk space.",
                {},
            ),
            schema(
                "get_systemd_status",
                "Check failed systemd units and optional recent journal errors.",
                {
                    "scope": enum(("user", "system"), "Systemd manager scope"),
                    "include_logs": boolean("Include recent error journal entries"),
                },
            ),
            schema(
                "get_power_profile",
                "Read the active Linux power profile.",
                {},
            ),
            schema(
                "set_power_profile",
                "Switch Linux power profile (power-saver, balanced, or performance).",
                {
                    "profile": enum(
                        ("power-saver", "balanced", "performance"),
                        "Target power profile",
                    )
                },
                ("profile",),
            ),
            schema(
                "media_control",
                "Control the active media player.",
                {"action": enum(("play_pause", "next", "previous"), "Media action")},
                ("action",),
            ),
            schema(
                "set_wifi",
                "Enable or disable Wi-Fi.",
                {"enabled": boolean("True to enable Wi-Fi")},
                ("enabled",),
            ),
            schema(
                "connect_wifi",
                "Connect to a known Wi-Fi network.",
                {"ssid": string("Network name")},
                ("ssid",),
            ),
            schema(
                "set_bluetooth",
                "Enable or disable Bluetooth.",
                {"enabled": boolean("True to enable Bluetooth")},
                ("enabled",),
            ),
            schema(
                "connect_bluetooth",
                "Connect or disconnect a paired Bluetooth device.",
                {
                    "device": string("Device name"),
                    "connect": boolean("True to connect, false to disconnect"),
                },
                ("device", "connect"),
            ),
            schema(
                "set_dnd",
                "Enable or disable Do Not Disturb.",
                {"enabled": boolean("True to enable Do Not Disturb")},
                ("enabled",),
            ),
            schema(
                "launch_app",
                "Find and launch an installed desktop application.",
                {"query": string("Application name")},
                ("query",),
            ),
            schema(
                "focus_app",
                "Focus a running application window or activate its tray item.",
                {"query": string("Application name or window title")},
                ("query",),
            ),
            schema(
                "copy_to_clipboard",
                "Copy text to the system clipboard.",
                {"text": string("Text to copy")},
                ("text",),
            ),
            schema(
                "search_clipboard",
                "Search saved clipboard history snippets.",
                {
                    "query": string("Optional search filter; omit for latest items"),
                    "limit": number("Maximum items to return", 1, 10),
                },
            ),
            schema(
                "get_notifications",
                "List recent desktop notifications captured by LedgePill.",
                {"limit": number("Maximum notifications to return", 1, 12)},
            ),
            schema(
                "clear_notifications",
                "Dismiss all captured desktop notifications.",
                {},
            ),
            schema(
                "control_timer",
                "Control the focus timer (start, pause, reset, or set custom minutes).",
                {
                    "action": enum(
                        ("start", "pause", "reset", "set_minutes"), "Timer action"
                    ),
                    "minutes": number("Duration in minutes for set_minutes", 1, 180),
                },
                ("action",),
            ),
            schema(
                "manage_todos",
                "List, add, toggle, or clean up tasks in the Focus checklist.",
                {
                    "action": enum(
                        ("list", "add", "toggle", "clear_done"), "Task list action"
                    ),
                    "text": string("Task text for add"),
                    "index": number("0-based task index for toggle", 0, 50),
                },
                ("action",),
            ),
            schema(
                "trigger_quick_action",
                "Trigger desktop quick actions: caffeine, screenshot, or Wi-Fi/BT scan.",
                {
                    "action": enum(
                        (
                            "caffeine_on",
                            "caffeine_off",
                            "screenshot",
                            "scan_wifi",
                            "scan_bluetooth",
                        ),
                        "Quick action name",
                    )
                },
                ("action",),
            ),
            schema(
                "manage_local_llm",
                "Inspect VRAM usage, load/unload Ollama models, or switch active model.",
                {
                    "action": enum(
                        ("status", "unload", "load", "switch_model"),
                        "Local LLM management action",
                    ),
                    "model": string("Target model name"),
                },
                ("action",),
            ),
            schema(
                "refresh_weather",
                "Refresh weather, optionally for a city.",
                {"city": string("City name; omit to use the configured location")},
            ),
            schema(
                "get_upcoming_events",
                "List upcoming calendar meetings and events.",
                {"limit": number("Maximum events to return", 1, 10)},
            ),
            schema(
                "set_appearance",
                "Change one or more LedgePill appearance settings.",
                {
                    "theme": enum(VALID_THEMES, "Theme name"),
                    "island_style": enum(VALID_STYLES, "Island layout style"),
                    "accent_mode": enum(VALID_ACCENT_MODES, "Accent color mode"),
                },
            ),
            schema(
                "request_session_action",
                "Request a lock, suspend, reboot, or shutdown; the user must confirm.",
                {
                    "action": enum(
                        ("lock", "suspend", "reboot", "poweroff"),
                        "Requested session action",
                    )
                },
                ("action",),
            ),
            schema(
                "request_end_app",
                "Request to end a running app; the user must confirm.",
                {"query": string("Running application name")},
                ("query",),
            ),
        ]
        return [
            item
            for item in definitions
            if available.get(item["function"]["name"], False)
        ]

    @staticmethod
    def parse_tool_call(text: str) -> tuple[str, dict] | None:
        m = _TOOL_RE.search(text or "")
        if not m:
            return None
        try:
            payload = json.loads(m.group(1))
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        tool = str(payload.get("tool") or payload.get("name") or "").strip()
        args = payload.get("args") if "args" in payload else payload.get("arguments")
        if not tool:
            return None
        return tool, (args if isinstance(args, dict) else {})

    @staticmethod
    def strip_tool_call(text: str) -> str:
        return _TOOL_RE.sub("", text or "").strip()

    def execute_tool(self, tool: str, args: dict | None = None) -> dict:
        params = args if isinstance(args, dict) else {}
        if tool in ("get_top_processes", "get_disk_usage", "get_systemd_status"):
            return self._execute_worker_tool(tool, params)
        if tool in ("get_power_profile", "set_power_profile"):
            return self._execute_worker_tool(tool, params)
        if (
            tool == "manage_local_llm"
            and str(params.get("action", "status")) != "switch_model"
        ):
            return self._execute_worker_tool(tool, params)
        return self._run_on_main(lambda: self._execute_sync(tool, params))

    def _execute_worker_tool(self, tool: str, args: dict) -> dict:
        if tool == "get_top_processes":
            sort_by = "cpu" if args.get("sort_by") == "cpu" else "ram"
            lim = args.get("limit", 8)
            limit = (
                int(lim) if isinstance(lim, int) and not isinstance(lim, bool) else 8
            )
            return {"ok": True, "processes": _read_top_processes(sort_by, limit)}
        if tool == "get_disk_usage":
            return {"ok": True, "mounts": _read_disk_usage()}
        if tool == "get_systemd_status":
            scope = "system" if args.get("scope") == "system" else "user"
            inc = bool(args.get("include_logs", False))
            return _read_systemd_status(scope, inc)
        if tool == "get_power_profile":
            prof = _read_power_profile()
            return (
                {"ok": True, "profile": prof}
                if prof
                else {"ok": False, "error": "Power profile daemon unavailable"}
            )
        if tool == "set_power_profile":
            return _set_power_profile(str(args.get("profile", "")).strip())
        if tool == "manage_local_llm":
            if self.ollama is None:
                return {"ok": False, "error": "Local LLM monitor unavailable"}
            act = str(args.get("action", "status")).strip()
            mod = str(args.get("model", "")).strip()
            if act == "status":
                return {
                    "ok": True,
                    "ollama_available": bool(self.ollama.available),
                    "active_model": self.ollama.model,
                    "running": list(getattr(self.ollama, "running_details", []) or []),
                    "installed": list(self.ollama.models),
                    "lmstudio_loaded": list(
                        getattr(self.ollama, "lmstudio_loaded", []) or []
                    ),
                    "tokens_per_sec": getattr(self.ollama, "tokens_per_sec", 0.0),
                }
            if act == "unload":
                ok = self.ollama.unload_model(mod)
                return {"ok": ok, "action": "unload", "model": mod or self.ollama.model}
            if act == "load":
                ok = self.ollama.preload_model(mod)
                return {"ok": ok, "action": "load", "model": mod or self.ollama.model}
            return {"ok": False, "error": f"Unsupported local LLM action '{act}'"}
        return {"ok": False, "error": f"Unknown tool '{tool}'"}

    def _execute_sync(self, tool: str, args: dict) -> dict:
        if tool == "get_system_snapshot":
            return {"ok": True, "snapshot": self.snapshot()}

        if tool == "media_control":
            act = str(args.get("action", "")).strip()
            if self.media is None or getattr(self.media, "active", None) is None:
                return {"ok": False, "error": "No active media player"}
            if act == "play_pause":
                self.media.play_pause()
            elif act == "next":
                self.media.next()
            elif act == "previous":
                self.media.previous()
            else:
                return {"ok": False, "error": f"Unknown media action '{act}'"}
            return {"ok": True, "action": act}

        if tool == "set_wifi":
            if self.network is None:
                return {"ok": False, "error": "Network service unavailable"}
            enabled = self._bool_arg(args, "enabled", True)
            if enabled is None:
                return {"ok": False, "error": "enabled must be a boolean"}
            self.network.set_wifi_enabled(enabled)
            return {"ok": True, "wifi_enabled": enabled}

        if tool == "connect_wifi":
            if self.network is None:
                return {"ok": False, "error": "Network service unavailable"}
            ssid = str(args.get("ssid", "")).strip()
            if not ssid:
                return {"ok": False, "error": "Missing SSID"}
            self.network.connect_wifi(ssid)
            return {"ok": True, "connecting_ssid": ssid}

        if tool == "set_bluetooth":
            if self.bluetooth is None:
                return {"ok": False, "error": "Bluetooth service unavailable"}
            enabled = self._bool_arg(args, "enabled", True)
            if enabled is None:
                return {"ok": False, "error": "enabled must be a boolean"}
            self.bluetooth.set_power(enabled)
            return {"ok": True, "bluetooth_powered": enabled}

        if tool == "connect_bluetooth":
            if self.bluetooth is None:
                return {"ok": False, "error": "Bluetooth service unavailable"}
            q = str(args.get("device", "")).strip().casefold()
            connect = self._bool_arg(args, "connect", True)
            if connect is None:
                return {"ok": False, "error": "connect must be a boolean"}
            for dev in self.bluetooth.devices():
                name = str(dev.get("name", ""))
                path = str(dev.get("path", ""))
                if q and (q in name.casefold() or q == path.casefold()):
                    self.bluetooth.connect_device(path, connect)
                    return {"ok": True, "device": name, "connect": connect}
            return {"ok": False, "error": f"No Bluetooth device matching '{q}'"}

        if tool == "set_dnd":
            enabled = self._bool_arg(args, "enabled", False)
            if enabled is None:
                return {"ok": False, "error": "enabled must be a boolean"}
            if self.on_dnd_change is not None:
                self.on_dnd_change(enabled)
            return {"ok": True, "do_not_disturb": enabled}

        if tool == "launch_app":
            if self.desktop is None:
                return {"ok": False, "error": "Desktop service unavailable"}
            q = str(args.get("query", "")).strip()
            hits = self.desktop.list_apps(q, limit=5)
            if not hits:
                return {"ok": False, "error": f"No app matching '{q}'"}
            ok = self.desktop.launch_app(hits[0]["id"])
            return {"ok": ok, "launched": hits[0]["name"]}

        if tool == "focus_app":
            if self.desktop is None:
                return {"ok": False, "error": "Desktop service unavailable"}
            q = str(args.get("query", "")).strip().casefold()
            for app in self.desktop.list_running_apps(q, limit=10):
                if app.get("window_id"):
                    self.desktop.focus_window(app["window_id"])
                    return {"ok": True, "focused": app["name"]}
                if app.get("tray_item"):
                    self.desktop.activate_tray_item(app["tray_item"])
                    return {"ok": True, "activated": app["name"]}
            return {"ok": False, "error": f"No focusable window matching '{q}'"}

        if tool == "copy_to_clipboard":
            if self.clipboard is None or not hasattr(self.clipboard, "copy_text"):
                return {"ok": False, "error": "Clipboard service unavailable"}
            txt = str(args.get("text", ""))
            ok = self.clipboard.copy_text(txt)
            return {"ok": ok, "chars": len(txt)}

        if tool == "search_clipboard":
            if self.clipboard is None:
                return {"ok": False, "error": "Clipboard service unavailable"}
            q = str(args.get("query", "")).strip().casefold()
            lim = args.get("limit", 6)
            limit = max(1, min(10, int(lim))) if isinstance(lim, int) else 6
            pinned = set(getattr(self.clipboard, "pinned", []) or [])
            hits = []
            for item in getattr(self.clipboard, "history", []) or []:
                if q and q not in item.casefold():
                    continue
                hits.append({"text": item[:200], "pinned": item in pinned})
                if len(hits) >= limit:
                    break
            return {"ok": True, "items": hits}

        if tool == "get_notifications":
            if self.notifier is None:
                return {"ok": False, "error": "Notification service unavailable"}
            lim = args.get("limit", 8)
            limit = max(1, min(12, int(lim))) if isinstance(lim, int) else 8
            hist = list(getattr(self.notifier, "history", []) or [])[:limit]
            return {
                "ok": True,
                "notifications": [
                    {
                        "app": str(n.get("app", "")),
                        "summary": str(n.get("summary", ""))[:120],
                        "body": str(n.get("body", ""))[:180],
                    }
                    for n in hist
                ],
            }

        if tool == "clear_notifications":
            if self.notifier is None:
                return {"ok": False, "error": "Notification service unavailable"}
            self.notifier.clear()
            return {"ok": True, "cleared": True}

        if tool == "control_timer":
            if self.on_ui_action is None:
                return {"ok": False, "error": "Timer controller unavailable"}
            act = str(args.get("action", "")).strip()
            if act not in ("start", "pause", "reset", "set_minutes"):
                return {"ok": False, "error": f"Unknown timer action '{act}'"}
            return self.on_ui_action("timer", dict(args))

        if tool == "manage_todos":
            if self.todos is None:
                return {"ok": False, "error": "Focus task list unavailable"}
            act = str(args.get("action", "list")).strip()
            if act == "list":
                return {"ok": True, "items": list(self.todos.items)}
            if act == "add":
                txt = str(args.get("text", "")).strip()
                if not txt:
                    return {"ok": False, "error": "Task text is required"}
                ok = self.todos.add(txt)
                return {"ok": ok, "items": list(self.todos.items)}
            if act == "toggle":
                idx = args.get("index")
                if not isinstance(idx, int) or isinstance(idx, bool):
                    return {"ok": False, "error": "index must be an integer"}
                if not (0 <= idx < len(self.todos.items)):
                    return {"ok": False, "error": f"Task index {idx} out of range"}
                done = self.todos.toggle(idx)
                return {"ok": True, "index": idx, "done": done}
            if act == "clear_done":
                removed = self.todos.clear_done()
                return {"ok": True, "removed": removed}
            return {"ok": False, "error": f"Unknown todo action '{act}'"}

        if tool == "trigger_quick_action":
            act = str(args.get("action", "")).strip()
            if act == "scan_wifi" and self.network is not None:
                self.network.scan_wifi(rescan=True)
                return {"ok": True, "action": act}
            if act == "scan_bluetooth" and self.bluetooth is not None:
                if not self.bluetooth.powered:
                    self.bluetooth.set_power(True)
                self.bluetooth.start_discovery()
                return {"ok": True, "action": act}
            if self.on_ui_action is not None:
                return self.on_ui_action("quick_action", {"action": act})
            return {"ok": False, "error": f"Action '{act}' unavailable"}

        if tool == "manage_local_llm":
            act = str(args.get("action", "")).strip()
            mod = str(args.get("model", "")).strip()
            if act == "switch_model":
                if not mod:
                    return {"ok": False, "error": "Model name is required"}
                self.cfg["ai_model"] = mod
                config.save(self.cfg)
                if self.on_apply_config is not None:
                    self.on_apply_config(False)
                return {"ok": True, "active_model": mod}
            return self._execute_worker_tool(tool, args)

        if tool == "refresh_weather":
            if self.weather is None:
                return {"ok": False, "error": "Weather service unavailable"}
            city = str(args.get("city", "")).strip()
            if city:
                self.cfg["weather_city"] = city
                config.save(self.cfg)
                self.weather.set_city(city)
            else:
                self.weather.request(force=True)
            return {"ok": True, "city": city or self.weather.city or "auto"}

        if tool == "get_upcoming_events":
            if self.calendar is None:
                return {"ok": False, "error": "Calendar service unavailable"}
            lim = args.get("limit", 5)
            limit = (
                max(1, min(10, int(lim)))
                if isinstance(lim, int) and not isinstance(lim, bool)
                else 5
            )
            events = list(getattr(self.calendar, "events", []) or [])[:limit]
            return {"ok": True, "events": events}

        if tool == "set_appearance":
            changed = {}
            theme = str(args.get("theme", "")).strip()
            if theme in VALID_THEMES:
                self.cfg["theme"] = theme
                changed["theme"] = theme
            style = str(args.get("island_style", "")).strip()
            if style in VALID_STYLES:
                self.cfg["island_style"] = style
                changed["island_style"] = style
            mode = str(args.get("accent_mode", "")).strip()
            if mode in VALID_ACCENT_MODES:
                self.cfg["accent_mode"] = mode
                self.cfg["dynamic_artwork_colors"] = mode in ("auto", "artwork")
                changed["accent_mode"] = mode
            if not changed:
                return {"ok": False, "error": "No valid appearance fields supplied"}
            config.save(self.cfg)
            if self.on_apply_config is not None:
                self.on_apply_config(False)
            return {"ok": True, **changed}

        if tool == "request_session_action":
            act = str(args.get("action", "")).strip()
            if act not in ("lock", "suspend", "reboot", "poweroff"):
                return {"ok": False, "error": f"Unsupported action '{act}'"}
            if self.desktop is None:
                return {"ok": False, "error": "Desktop service unavailable"}
            if self.on_confirm_request is not None:
                label = {
                    "lock": "Lock screen",
                    "suspend": "Suspend system",
                    "reboot": "Reboot system",
                    "poweroff": "Shut down system",
                }[act]
                accepted = self.on_confirm_request(
                    label, lambda a=act: self.desktop.power_action(a)
                )
                if accepted is False:
                    return {
                        "ok": False,
                        "error": "Another action is already awaiting confirmation",
                    }
                return {
                    "ok": True,
                    "pending_confirmation": label,
                    "note": "Confirmation prompt shown in LedgePill AI chat.",
                }
            return {"ok": False, "error": "Confirmation handler unavailable"}

        if tool == "request_end_app":
            if self.desktop is None:
                return {"ok": False, "error": "Desktop service unavailable"}
            q = str(args.get("query", "")).strip()
            hits = self.desktop.list_running_apps(q, limit=5)
            if not hits:
                return {"ok": False, "error": f"No running app matching '{q}'"}
            app = hits[0]
            if self.on_confirm_request is not None:
                label = f"End {app['name']} ({len(app['pids'])} process(es))"
                pids = list(app["pids"])
                accepted = self.on_confirm_request(
                    label, lambda p=pids: self.desktop.terminate_app(p)
                )
                if accepted is False:
                    return {
                        "ok": False,
                        "error": "Another action is already awaiting confirmation",
                    }
                return {
                    "ok": True,
                    "pending_confirmation": label,
                    "note": "Confirmation prompt shown in LedgePill AI chat.",
                }
            return {"ok": False, "error": "Confirmation handler unavailable"}

        return {"ok": False, "error": f"Unknown tool '{tool}'"}
