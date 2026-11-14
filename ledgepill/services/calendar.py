"""Calendar / upcoming meeting service reading local .ics files and iCal URLs."""

from __future__ import annotations

import glob
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timezone
from typing import ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gi.repository import GLib, GObject

_MAX_ICS_BYTES = 1024 * 1024
_ALERT_WINDOW_MIN = 10
_POLL_SEC = 60


def _unfold_ics(text: str) -> list[str]:
    """Unfold RFC 5545 continuation lines starting with space or horizontal tab."""
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _unescape_ics(val: str) -> str:
    return (
        val.replace("\\n", " ")
        .replace("\\N", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _parse_ics_dt(raw_val: str, params: dict[str, str]) -> tuple[float, bool] | None:
    """Parse an ICS DTSTART/DTEND value into ``(unix_ts, all_day)``."""
    val = raw_val.strip()
    if not val:
        return None
    all_day = params.get("VALUE", "").upper() == "DATE" or bool(
        re.fullmatch(r"\d{8}", val)
    )
    if all_day:
        try:
            dt = datetime.strptime(val[:8], "%Y%m%d")
            return dt.timestamp(), True
        except ValueError:
            return None
    tz: timezone | ZoneInfo | None = None
    if val.endswith("Z"):
        tz = UTC
        val = val[:-1]
    elif tzid := params.get("TZID", "").strip():
        try:
            tz = ZoneInfo(tzid)
        except (ZoneInfoNotFoundError, ValueError):
            tz = None
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            dt = datetime.strptime(val, fmt)
            if tz is not None:
                dt = dt.replace(tzinfo=tz)
            return dt.timestamp(), False
        except ValueError:
            continue
    return None


def parse_ics_events(
    text: str, now_ts: float | None = None, horizon_days: int = 7
) -> list[dict]:
    """Parse RFC 5545 VEVENT records into sorted upcoming event dicts."""
    now = time.time() if now_ts is None else float(now_ts)
    cutoff = now + max(1, horizon_days) * 86400
    events: list[dict] = []
    cur: dict | None = None
    in_valarm = False

    for line in _unfold_ics(text):
        upper = line.strip().upper()
        if upper == "BEGIN:VEVENT":
            cur = {}
            in_valarm = False
            continue
        if upper == "END:VEVENT":
            if cur is not None and not in_valarm:
                status = str(cur.get("STATUS", "")).upper()
                start_info = cur.get("_start")
                summary = str(cur.get("SUMMARY", "")).strip()
                if status != "CANCELLED" and start_info and summary:
                    start_ts, all_day = start_info
                    end_info = cur.get("_end")
                    end_ts = (
                        end_info[0]
                        if end_info
                        else start_ts + (86400 if all_day else 3600)
                    )
                    if end_ts >= now - 60 and start_ts <= cutoff:
                        events.append(
                            {
                                "summary": summary[:140],
                                "start_ts": float(start_ts),
                                "end_ts": float(end_ts),
                                "all_day": bool(all_day),
                                "location": str(cur.get("LOCATION", ""))[:160],
                                "url": str(cur.get("URL", ""))[:256],
                            }
                        )
            cur = None
            continue
        if cur is None:
            continue
        if upper == "BEGIN:VALARM":
            in_valarm = True
            continue
        if upper == "END:VALARM":
            in_valarm = False
            continue
        if in_valarm or ":" not in line:
            continue

        key_part, raw_val = line.split(":", 1)
        segs = key_part.split(";")
        prop = segs[0].strip().upper()
        params: dict[str, str] = {}
        for seg in segs[1:]:
            if "=" in seg:
                pk, pv = seg.split("=", 1)
                params[pk.strip().upper()] = pv.strip().strip('"')
        if prop == "DTSTART":
            cur["_start"] = _parse_ics_dt(raw_val, params)
        elif prop == "DTEND":
            cur["_end"] = _parse_ics_dt(raw_val, params)
        elif prop in ("SUMMARY", "LOCATION", "URL", "STATUS"):
            cur[prop] = _unescape_ics(raw_val)

    events.sort(key=lambda ev: (ev["all_day"], ev["start_ts"], ev["summary"]))
    return events


def discover_local_ics() -> list[str]:
    """Return existing default local Linux .ics calendar paths if present."""
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".config", "ledge-pill", "calendar.ics"),
        *sorted(
            glob.glob(
                os.path.join(
                    home,
                    ".local",
                    "share",
                    "evolution",
                    "calendar",
                    "*",
                    "calendar.ics",
                )
            )
        ),
        *sorted(
            glob.glob(
                os.path.join(home, ".local", "share", "gnome-calendar", "*", "*.ics")
            )
        ),
    ]
    return [p for p in dict.fromkeys(candidates) if os.path.isfile(p)][:8]


class CalendarService(GObject.Object):
    """Tracks upcoming events from local .ics files and optional iCal URLs."""

    __gsignals__: ClassVar[dict] = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "upcoming-alert": (GObject.SignalFlags.RUN_FIRST, None, (str, int, str)),
    }

    def __init__(self, sources: list[str] | None = None):
        super().__init__()
        self.sources: list[str] = list(sources or [])
        self.events: list[dict] = []
        self._alerted_keys: set[str] = set()
        self._timer = 0
        self._busy = False
        self._stopped = False
        self._gen = 0

    def start(self):
        if self._timer:
            return
        self._stopped = False
        self._busy = False
        self.refresh_now()
        self._timer = GLib.timeout_add_seconds(_POLL_SEC, self._tick)

    def stop(self):
        self._stopped = True
        self._gen += 1
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _tick(self) -> bool:
        if self._stopped:
            return False
        self.refresh_now()
        return True

    def refresh_now(self):
        if self._stopped or self._busy:
            return
        self._busy = True
        gen = self._gen
        configured = list(self.sources)
        threading.Thread(
            target=self._worker, args=(configured, gen), daemon=True
        ).start()

    def _read_source(self, src: str) -> str:
        s = src.strip()
        if not s:
            return ""
        parsed = urllib.parse.urlparse(s)
        if parsed.scheme in ("http", "https"):
            req = urllib.request.Request(s, headers={"User-Agent": "LedgePill/1.7"})
            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    return resp.read(_MAX_ICS_BYTES).decode("utf-8", "replace")
            except (urllib.error.URLError, OSError, TimeoutError, ValueError):
                return ""
        path = os.path.expanduser(s)
        if not os.path.isfile(path):
            return ""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read(_MAX_ICS_BYTES)
        except OSError:
            return ""

    def _worker(self, configured: list[str], gen: int):
        srcs = configured or discover_local_ics()
        now = time.time()
        all_events: list[dict] = []
        seen: set[tuple[str, int]] = set()
        for src in srcs[:12]:
            if self._stopped or gen != self._gen:
                break
            raw = self._read_source(src)
            if not raw:
                continue
            for ev in parse_ics_events(raw, now_ts=now):
                key = (ev["summary"], int(ev["start_ts"]))
                if key not in seen:
                    seen.add(key)
                    all_events.append(ev)
        all_events.sort(key=lambda ev: (ev["all_day"], ev["start_ts"], ev["summary"]))
        GLib.idle_add(self._apply, all_events, gen)

    def _apply(self, events: list[dict], gen: int) -> bool:
        if self._stopped or gen != self._gen:
            return False
        self._busy = False
        changed = events != self.events
        self.events = events
        if changed:
            self.emit("changed")
        self._check_upcoming_alert()
        return False

    def _check_upcoming_alert(self):
        ev = self.next_event()
        if ev is None or ev.get("all_day"):
            return
        mins = ev["minutes_until"]
        if 0 <= mins <= _ALERT_WINDOW_MIN:
            alert_key = f"{ev['summary']}:{int(ev['start_ts'])}"
            if alert_key not in self._alerted_keys:
                self._alerted_keys.add(alert_key)
                if len(self._alerted_keys) > 64:
                    self._alerted_keys.clear()
                    self._alerted_keys.add(alert_key)
                self.emit(
                    "upcoming-alert",
                    ev["summary"],
                    max(0, int(mins)),
                    ev.get("location") or ev.get("url") or "",
                )

    def next_event(self, now_ts: float | None = None) -> dict | None:
        now = time.time() if now_ts is None else float(now_ts)
        timed = [e for e in self.events if not e.get("all_day") and e["end_ts"] >= now]
        ev = timed[0] if timed else (self.events[0] if self.events else None)
        if ev is None:
            return None
        mins = round((ev["start_ts"] - now) / 60.0)
        return {**ev, "minutes_until": mins}

    def format_next_label(self, now_ts: float | None = None) -> str:
        ev = self.next_event(now_ts=now_ts)
        if ev is None:
            return ""
        if ev.get("all_day"):
            return f"{ev['summary']} (today)"
        mins = ev["minutes_until"]
        if mins <= 0:
            when = "now"
        elif mins < 60:
            when = f"in {mins}m"
        elif mins < 24 * 60:
            when = time.strftime("%H:%M", time.localtime(ev["start_ts"]))
        else:
            when = time.strftime("%a %H:%M", time.localtime(ev["start_ts"]))
        return f"{ev['summary']} · {when}"
