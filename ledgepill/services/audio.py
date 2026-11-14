"""Volume, mic, device selection, per-app mixer, and active stream watcher.

Listens on `pactl subscribe` (a blocking pipe — the process sleeps until
PulseAudio/PipeWire actually emits an event) and reads sink/source/stream state
only when needed. Restarts cleanly if PipeWire restarts.
"""

from __future__ import annotations

import json
import logging
import re
from typing import ClassVar

from gi.repository import Gio, GLib, GObject

_VOL_RE = re.compile(r"(\d+)%")
_SINK_HDR_RE = re.compile(r"^Sink #(\d+)")
_INPUT_HDR_RE = re.compile(r"^Sink Input #(\d+)")
_QUOTED_PROP_RE = re.compile(r'^\s*(application\.name|media\.name)\s*=\s*"(.*)"\s*$')
_PACTL_QUIET = Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE


def _parse_pactl_sinks_text(text: str, default_sink: str) -> list[dict]:
    """Parse plain-text ``pactl list sinks`` output (UTF-8 safe fallback)."""
    sinks: list[dict] = []
    cur: dict | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if _SINK_HDR_RE.match(line):
            if cur and cur.get("name"):
                cur.setdefault("description", cur["name"])
                cur["default"] = cur["name"] == default_sink
                sinks.append(cur)
            cur = {}
            continue
        if cur is None:
            continue
        if line.startswith("Name:"):
            cur["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("Description:"):
            cur["description"] = line.split(":", 1)[1].strip()
    if cur and cur.get("name"):
        cur.setdefault("description", cur["name"])
        cur["default"] = cur["name"] == default_sink
        sinks.append(cur)
    return sinks


def _parse_pactl_sink_inputs_text(text: str) -> list[dict]:
    """Parse plain-text ``pactl list sink-inputs`` output (UTF-8 safe fallback)."""
    inputs: list[dict] = []
    cur: dict | None = None

    def _flush():
        if cur is not None and "id" in cur:
            inputs.append(
                {
                    "id": cur["id"],
                    "name": cur.get("app") or cur.get("media") or f"Stream {cur['id']}",
                    "volume": cur.get("volume", 100),
                    "muted": cur.get("muted", False),
                }
            )

    for raw in (text or "").splitlines():
        line = raw.strip()
        m_hdr = _INPUT_HDR_RE.match(line)
        if m_hdr:
            _flush()
            cur = {"id": int(m_hdr.group(1))}
            continue
        if cur is None:
            continue
        if line.startswith("Mute:"):
            cur["muted"] = line.split(":", 1)[1].strip().lower() in ("yes", "true", "1")
        elif line.startswith("Volume:") and "volume" not in cur:
            m_vol = _VOL_RE.search(line)
            if m_vol:
                cur["volume"] = max(0, min(150, int(m_vol.group(1))))
        else:
            m_prop = _QUOTED_PROP_RE.match(line)
            if m_prop:
                key, val = m_prop.group(1), m_prop.group(2)
                if key == "application.name" and val:
                    cur["app"] = val
                elif key == "media.name" and val:
                    cur["media"] = val
    _flush()
    return inputs


def _parse_default_source_state(
    text: str, default_source: str
) -> bool | None:
    """Return the mute state for the current default source."""
    try:
        sources = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(sources, list):
        return None
    for source in sources:
        if not isinstance(source, dict) or source.get("name") != default_source:
            continue
        muted = source.get("mute")
        return muted if isinstance(muted, bool) else None
    return None


class AudioService(GObject.Object):
    __gsignals__: ClassVar[dict] = {
        # percent (0-150), muted — emitted once baseline is ready
        "ready": (GObject.SignalFlags.RUN_FIRST, None, (int, bool)),
        # percent (0-150), muted — emitted on subsequent sink changes
        "volume": (GObject.SignalFlags.RUN_FIRST, None, (int, bool)),
        # mic muted — emitted when default source mute state changes
        "mic-muted": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        # mic recording active (any source-output running)
        "mic-activity": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        # sinks / sink-inputs updated
        "devices-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self.percent = -1
        self.muted = False
        self.mic_muted = False
        self.mic_in_use = False
        self.default_sink = ""
        self.sinks: list[dict] = []  # [{name, description, default}]
        self.sink_inputs: list[dict] = []  # [{id, name, volume, muted}]
        self._primed = False
        self._pending_timer = 0
        self._mic_timer = 0
        self._mic_poll_timer = 0
        self._dev_timer = 0
        self._reconnect_timer = 0
        self._retries = 0
        self._stopping = False
        self._seq = 0
        self._mic_seq = 0
        self._dev_seq = 0
        self._proc: Gio.Subprocess | None = None
        self._stream: Gio.DataInputStream | None = None
        self._start_listener()
        self._mic_poll_timer = GLib.timeout_add_seconds(1, self._poll_mic_state)

    def _start_listener(self):
        if self._stopping:
            return
        try:
            self._proc = Gio.Subprocess.new(
                ["pactl", "subscribe"], Gio.SubprocessFlags.STDOUT_PIPE
            )
        except GLib.Error:
            return
        self._proc.wait_async(None, self._on_proc_exit)
        self._stream = Gio.DataInputStream.new(self._proc.get_stdout_pipe())
        self._read_next()
        self._query()
        self._query_mic()

    def _on_proc_exit(self, proc, res):
        try:
            proc.wait_finish(res)
        except GLib.Error:
            pass
        if self._stopping or proc is not self._proc:
            return
        self._proc = None
        self._stream = None
        if self._retries < 5 and not self._reconnect_timer:
            self._retries += 1
            self._reconnect_timer = GLib.timeout_add_seconds(2, self._reconnect)

    def _reconnect(self):
        self._reconnect_timer = 0
        self._start_listener()
        return False

    def stop(self):
        self._stopping = True
        if self._pending_timer:
            GLib.source_remove(self._pending_timer)
            self._pending_timer = 0
        if self._dev_timer:
            GLib.source_remove(self._dev_timer)
            self._dev_timer = 0
        if self._mic_timer:
            GLib.source_remove(self._mic_timer)
            self._mic_timer = 0
        if self._mic_poll_timer:
            GLib.source_remove(self._mic_poll_timer)
            self._mic_poll_timer = 0
        if self._reconnect_timer:
            GLib.source_remove(self._reconnect_timer)
            self._reconnect_timer = 0
        if self._proc is not None:
            self._proc.force_exit()
            self._proc = None
        self._stream = None
        self._mic_seq += 1

    def _read_next(self):
        if self._stream is None:
            return
        self._stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line)

    def _on_line(self, stream, res):
        if stream is not self._stream:
            return
        try:
            line, _len = stream.read_line_finish_utf8(res)
        except GLib.Error:
            return
        if line is None:
            return
        self._retries = 0
        event = line.casefold()
        if (
            "on sink" in event or "'change' on server" in event
        ) and not self._pending_timer:
            # coalesce bursts: pactl fires several events per keypress
            self._pending_timer = GLib.timeout_add(40, self._query_once)
        if (
            "on source" in event or "on card" in event or "on server" in event
        ) and not self._mic_timer:
            # Keyboard mute shortcuts can arrive through PipeWire as source
            # changes, while device/profile changes may be reported on the
            # card or server. Re-read the default source for all three so the
            # hardware LED follows the actual server mute state.
            self._mic_timer = GLib.timeout_add(40, self._query_mic_once)
        if (
            "sink-input" in event
            or "'new' on sink" in event
            or "'remove' on sink" in event
            or "'change' on server" in event
        ) and not self._dev_timer:
            self._dev_timer = GLib.timeout_add(160, self._refresh_devices_once)
        self._read_next()

    def _refresh_devices_once(self):
        self._dev_timer = 0
        if not self._stopping:
            self.refresh_devices()
        return False

    def _query_once(self):
        self._pending_timer = 0
        self._query()
        return False

    def _query_mic_once(self):
        self._mic_timer = 0
        if not self._stopping:
            self._query_mic()
        return False

    def _poll_mic_state(self):
        if self._stopping:
            self._mic_poll_timer = 0
            return False
        self._query_mic()
        return True

    def _query(self):
        self._seq += 1
        seq = self._seq
        try:
            proc = Gio.Subprocess.new(
                ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                Gio.SubprocessFlags.STDOUT_PIPE,
            )
        except GLib.Error:
            return
        proc.communicate_utf8_async(
            None, None, lambda p, r: self._on_volume_out(p, r, seq)
        )

    def _on_volume_out(self, proc, res, seq: int):
        if seq != self._seq:
            return
        try:
            _ok, out, _err = proc.communicate_utf8_finish(res)
        except GLib.Error:
            return
        m = _VOL_RE.search(out or "")
        if not m:
            return
        percent = max(0, min(150, int(m.group(1))))
        try:
            mproc = Gio.Subprocess.new(
                ["pactl", "get-sink-mute", "@DEFAULT_SINK@"],
                Gio.SubprocessFlags.STDOUT_PIPE,
            )
        except GLib.Error:
            return
        mproc.communicate_utf8_async(
            None, None, lambda p, r: self._on_mute_out(p, r, percent, seq)
        )

    def _on_mute_out(self, proc, res, percent: int, seq: int):
        if seq != self._seq:
            return
        try:
            _ok, out, _err = proc.communicate_utf8_finish(res)
        except GLib.Error:
            return
        muted = "yes" in (out or "").lower()
        changed = percent != self.percent or muted != self.muted
        self.percent, self.muted = percent, muted
        if not self._primed:
            self._primed = True
            self.emit("ready", percent, muted)
        elif changed:
            self.emit("volume", percent, muted)
        self._query_source_outputs(seq)

    def _query_mic(self):
        if self._stopping:
            return
        self._mic_seq += 1
        mic_seq = self._mic_seq
        try:
            proc = Gio.Subprocess.new(
                ["pactl", "get-default-source"],
                Gio.SubprocessFlags.STDOUT_PIPE,
            )
        except GLib.Error:
            return
        proc.communicate_utf8_async(
            None, None, lambda p, r: self._on_mic_out(p, r, mic_seq)
        )

    def _on_mic_out(self, proc, res, mic_seq: int):
        if self._stopping or mic_seq != self._mic_seq:
            return
        try:
            ok, out, _err = proc.communicate_utf8_finish(res)
        except GLib.Error:
            return
        if not ok:
            return
        default_source = (out or "").strip()
        if not default_source:
            return
        try:
            sources_proc = Gio.Subprocess.new(
                ["pactl", "-f", "json", "list", "sources"], _PACTL_QUIET
            )
        except GLib.Error:
            return
        sources_proc.communicate_utf8_async(
            None,
            None,
            lambda p, r: self._on_sources_out(p, r, mic_seq, default_source),
        )

    def _on_sources_out(self, proc, res, mic_seq: int, default_source: str):
        if self._stopping or mic_seq != self._mic_seq:
            return
        try:
            ok, out, _err = proc.communicate_utf8_finish(res)
        except GLib.Error:
            return
        if not ok:
            return
        mic_muted = _parse_default_source_state(out or "", default_source)
        if mic_muted is None:
            return
        if mic_muted != self.mic_muted:
            self.mic_muted = mic_muted
            self.emit("mic-muted", mic_muted)

    def _query_source_outputs(self, seq: int):
        try:
            proc = Gio.Subprocess.new(
                ["pactl", "list", "short", "source-outputs"],
                Gio.SubprocessFlags.STDOUT_PIPE,
            )
        except GLib.Error:
            return
        proc.communicate_utf8_async(
            None, None, lambda p, r: self._on_source_outputs(p, r, seq)
        )

    def _on_source_outputs(self, proc, res, seq: int):
        if seq != self._seq:
            return
        try:
            _ok, out, _err = proc.communicate_utf8_finish(res)
        except GLib.Error:
            return
        lines = [ln for ln in (out or "").splitlines() if ln.strip()]
        active = bool(lines)
        if active != self.mic_in_use:
            self.mic_in_use = active
            self.emit("mic-activity", active)

    # -- devices & per-application audio -----------------------------------

    def refresh_devices(self):
        """Fetch output sinks and active playback streams asynchronously."""
        self._dev_seq += 1
        seq = self._dev_seq
        try:
            proc = Gio.Subprocess.new(
                ["pactl", "get-default-sink"],
                Gio.SubprocessFlags.STDOUT_PIPE,
            )
        except GLib.Error:
            return
        proc.communicate_utf8_async(
            None, None, lambda p, r: self._on_default_sink(p, r, seq)
        )

    def _on_default_sink(self, proc, res, seq: int):
        if seq != self._dev_seq:
            return
        try:
            _ok, out, _err = proc.communicate_utf8_finish(res)
            self.default_sink = (out or "").strip()
        except GLib.Error:
            self.default_sink = ""
        try:
            sproc = Gio.Subprocess.new(
                ["pactl", "list", "sinks"],
                _PACTL_QUIET,
            )
        except GLib.Error:
            return
        sproc.communicate_utf8_async(
            None, None, lambda p, r: self._on_sinks_text(p, r, seq)
        )

    def _on_sinks_text(self, proc, res, seq: int):
        if seq != self._dev_seq:
            return
        sinks: list[dict] = []
        try:
            _ok, out, _err = proc.communicate_utf8_finish(res)
            sinks = _parse_pactl_sinks_text(out or "", self.default_sink)
        except GLib.Error:
            pass
        self.sinks = sinks
        self._fetch_sink_inputs(seq)

    def _on_sinks_json(self, proc, res, seq: int):
        if seq != self._dev_seq:
            return
        sinks: list[dict] = []
        parsed_ok = False
        try:
            _ok, out, _err = proc.communicate_utf8_finish(res)
            raw = (out or "").strip()
            if raw:
                data = json.loads(raw)
                if isinstance(data, list):
                    parsed_ok = True
                    for item in data:
                        if not isinstance(item, dict):
                            continue
                        name = str(item.get("name", ""))
                        if not name:
                            continue
                        desc = str(item.get("description") or name)
                        sinks.append(
                            {
                                "name": name,
                                "description": desc,
                                "default": name == self.default_sink,
                            }
                        )
        except (GLib.Error, ValueError):
            parsed_ok = False
        if not parsed_ok:
            try:
                sproc = Gio.Subprocess.new(["pactl", "list", "sinks"], _PACTL_QUIET)
            except GLib.Error:
                self.sinks = []
                self._fetch_sink_inputs(seq)
                return
            sproc.communicate_utf8_async(
                None, None, lambda p, r: self._on_sinks_text(p, r, seq)
            )
            return
        self.sinks = sinks
        self._fetch_sink_inputs(seq)

    def _fetch_sink_inputs(self, seq: int):
        try:
            iproc = Gio.Subprocess.new(
                ["pactl", "list", "sink-inputs"],
                _PACTL_QUIET,
            )
        except GLib.Error:
            self.emit("devices-changed")
            return
        iproc.communicate_utf8_async(
            None, None, lambda p, r: self._on_sink_inputs_text(p, r, seq)
        )

    def _on_sink_inputs_text(self, proc, res, seq: int):
        if seq != self._dev_seq:
            return
        inputs: list[dict] = []
        try:
            _ok, out, _err = proc.communicate_utf8_finish(res)
            inputs = _parse_pactl_sink_inputs_text(out or "")
        except GLib.Error:
            pass
        self.sink_inputs = inputs
        self.emit("devices-changed")

    def _on_sink_inputs_json(self, proc, res, seq: int):
        if seq != self._dev_seq:
            return
        inputs: list[dict] = []
        parsed_ok = False
        try:
            _ok, out, _err = proc.communicate_utf8_finish(res)
            raw = (out or "").strip()
            if raw:
                data = json.loads(raw)
                if isinstance(data, list):
                    parsed_ok = True
                    for item in data:
                        if not isinstance(item, dict):
                            continue
                        idx = item.get("index")
                        if idx is None:
                            continue
                        props = item.get("properties") or {}
                        app = str(
                            props.get("application.name")
                            or props.get("media.name")
                            or f"Stream {idx}"
                        )
                        vol_pct = 100
                        vol = item.get("volume")
                        if isinstance(vol, dict):
                            for ch in vol.values():
                                if isinstance(ch, dict) and "value_percent" in ch:
                                    m = _VOL_RE.search(str(ch["value_percent"]))
                                    if m:
                                        vol_pct = max(0, min(150, int(m.group(1))))
                                        break
                        inputs.append(
                            {
                                "id": int(idx),
                                "name": app,
                                "volume": vol_pct,
                                "muted": bool(item.get("mute", False)),
                            }
                        )
        except (GLib.Error, ValueError, TypeError):
            parsed_ok = False
        if not parsed_ok:
            try:
                iproc = Gio.Subprocess.new(
                    ["pactl", "list", "sink-inputs"], _PACTL_QUIET
                )
            except GLib.Error:
                self.sink_inputs = []
                self.emit("devices-changed")
                return
            iproc.communicate_utf8_async(
                None, None, lambda p, r: self._on_sink_inputs_text(p, r, seq)
            )
            return
        self.sink_inputs = inputs
        self.emit("devices-changed")

    def set_volume(self, percent: int):
        val = max(0, min(150, int(percent)))
        try:
            Gio.Subprocess.new(
                ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{val}%"],
                Gio.SubprocessFlags.NONE,
            )
        except GLib.Error:
            pass

    def set_muted(self, muted: bool):
        try:
            Gio.Subprocess.new(
                ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0"],
                Gio.SubprocessFlags.NONE,
            )
        except GLib.Error:
            pass

    def set_mic_muted(self, muted: bool):
        try:
            proc = Gio.Subprocess.new(
                ["pactl", "set-source-mute", "@DEFAULT_SOURCE@", "1" if muted else "0"],
                Gio.SubprocessFlags.NONE,
            )
            proc.wait_async(None, self._on_mic_mute_set)
        except GLib.Error as exc:
            logging.getLogger(__name__).warning(
                "Could not set microphone mute: %s", exc
            )

    def _on_mic_mute_set(self, proc, res):
        try:
            proc.wait_finish(res)
        except GLib.Error as exc:
            logging.getLogger(__name__).warning(
                "Could not set microphone mute: %s", exc
            )
            return
        if not proc.get_successful():
            logging.getLogger(__name__).warning("Could not set microphone mute")
            return
        self._query_mic()

    def set_default_sink(self, sink_name: str):
        if not sink_name:
            return
        try:
            proc = Gio.Subprocess.new(
                ["pactl", "set-default-sink", sink_name],
                Gio.SubprocessFlags.NONE,
            )
            proc.wait_async(None, lambda *_: self.refresh_devices())
        except GLib.Error:
            pass

    def set_app_volume(self, input_id: int, percent: int):
        val = max(0, min(150, int(percent)))
        try:
            Gio.Subprocess.new(
                ["pactl", "set-sink-input-volume", str(int(input_id)), f"{val}%"],
                Gio.SubprocessFlags.NONE,
            )
        except GLib.Error:
            pass

    def set_app_mute(self, input_id: int, muted: bool):
        try:
            Gio.Subprocess.new(
                [
                    "pactl",
                    "set-sink-input-mute",
                    str(int(input_id)),
                    "1" if muted else "0",
                ],
                Gio.SubprocessFlags.NONE,
            )
        except GLib.Error:
            pass
