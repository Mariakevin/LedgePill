"""Privacy indicator service: tracks microphone, camera, screen recording,
and screen sharing activity cleanly on Linux."""

from __future__ import annotations

import json
import os
import shutil
import threading
from typing import ClassVar

from gi.repository import Gio, GLib, GObject

_RECORDERS = (
    "obs",
    "wf-recorder",
    "wl-screenrec",
    "kooha",
    "gpu-screen-recorder",
    "simplescreenrecorder",
    "kazam",
)


class PrivacyService(GObject.Object):
    __gsignals__: ClassVar[dict] = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # kind ("camera"/"mic"/"screenshare"/"recording"), label
        "alert": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
    }

    def __init__(self, audio=None):
        super().__init__()
        self.audio = audio
        self.mic_active = False
        self.camera_active = False
        self.screen_recording = False
        self.screen_sharing = False
        self._timer = 0
        self._pw_busy = False
        self._busy = False
        self._stopped = False
        self._privacy_generation = 0
        self._fd_cam = False
        self._pw_cam = False
        self._pw_share = False
        self._rec = False
        self._has_pw_dump = bool(shutil.which("pw-dump"))
        self._has_video_devs = any(
            name.startswith("video") for name in _safe_listdir("/dev")
        )
        if self.audio is not None:
            self.mic_active = bool(getattr(self.audio, "mic_in_use", False))
            self.audio.connect("mic-activity", self._on_mic_activity)

    @property
    def _gen(self):
        return self._privacy_generation

    @_gen.setter
    def _gen(self, value):
        self._privacy_generation = value

    @property
    def _generation(self):
        return self._privacy_generation

    @_generation.setter
    def _generation(self, value):
        self._privacy_generation = value

    def _on_mic_activity(self, _svc, active: bool):
        if active != self.mic_active:
            self.mic_active = active
            if active:
                self.emit("alert", "mic", "Microphone in use")
            self.emit("changed")

    def start(self):
        self._stopped = False
        if not self._timer:
            self.refresh_now()
            self._timer = GLib.timeout_add_seconds(4, self._tick)

    def stop(self):
        self._stopped = True
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self._generation += 1
        self._busy = False
        self._pw_busy = False

    def _tick(self) -> bool:
        self.refresh_now()
        return True

    def refresh_now(self):
        if self._busy or self._stopped:
            return
        self._busy = True
        gen = self._generation
        threading.Thread(target=self._scan_worker, args=(gen,), daemon=True).start()
        if self._has_pw_dump and not self._pw_busy:
            self._probe_pipewire()

    def _scan_worker(self, gen):
        try:
            fd_cam = self._scan_camera_fds()
            rec = self._scan_recorders()
        except (OSError, ValueError):
            fd_cam = getattr(self, "_fd_cam", False)
            rec = getattr(self, "_rec", False)
        GLib.idle_add(self._apply_scan, fd_cam, rec, gen)

    def _apply_scan(self, fd_cam, rec, gen):
        if self._stopped or gen != self._privacy_generation:
            self._busy = False
            return False
        self._fd_cam = fd_cam
        self._rec = rec
        self._apply_state(
            self._fd_cam or self._pw_cam,
            self._rec,
            self._pw_share,
        )
        self._busy = False
        return False

    def _scan_camera_fds(self) -> bool:
        if not self._has_video_devs:
            return False
        uid = os.getuid()
        for pid in _safe_listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                if os.stat(f"/proc/{pid}").st_uid != uid:
                    continue
                fddir = f"/proc/{pid}/fd"
                for fd in os.listdir(fddir):
                    target = os.readlink(f"{fddir}/{fd}")
                    if target.startswith("/dev/video"):
                        return True
            except OSError:
                continue
        return False

    def _scan_recorders(self) -> bool:
        for pid in _safe_listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/comm") as f:
                    comm = f.read().strip().lower()
                if comm in _RECORDERS:
                    return True
                if comm == "ffmpeg":
                    with open(f"/proc/{pid}/cmdline", "rb") as f:
                        cmd = f.read().decode("utf-8", "ignore").lower()
                    if any(k in cmd for k in ("x11grab", "kmsgrab", "pipewire")):
                        return True
            except OSError:
                continue
        return False

    def _probe_pipewire(self):
        self._pw_busy = True
        gen = self._privacy_generation
        try:
            proc = Gio.Subprocess.new(
                ["pw-dump"],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE,
            )
        except GLib.Error:
            self._pw_busy = False
            return
        proc.communicate_utf8_async(
            None, None, lambda p, r, g=gen: self._on_pw_dump(p, r, g)
        )

    def _on_pw_dump(self, proc, res, gen: int | None = None):
        self._pw_busy = False
        if self._stopped or (gen is not None and gen != self._privacy_generation):
            return
        try:
            _ok, out, _err = proc.communicate_utf8_finish(res)
            objs = json.loads(out or "[]")
        except (GLib.Error, ValueError):
            return
        if not isinstance(objs, list):
            return
        sharing = False
        pw_cam = False
        for obj in objs:
            if not isinstance(obj, dict):
                continue
            info = obj.get("info") or {}
            props = info.get("props") or {}
            state = str(info.get("state", "")).lower()
            media_class = str(props.get("media.class", ""))
            node_name = str(props.get("node.name", "")).lower()
            app_name = str(props.get("application.name", "")).lower()
            if state == "running" and "Video" in media_class:
                if "screencast" in node_name or "xdg-desktop-portal" in app_name:
                    sharing = True
                elif "v4l2" in node_name or "camera" in node_name:
                    pw_cam = True
        self._pw_cam = pw_cam
        self._pw_share = sharing
        self._apply_state(self._fd_cam or self._pw_cam, self._rec, self._pw_share)

    def _apply_state(self, cam: bool, rec: bool, share: bool):
        prev = (self.camera_active, self.screen_recording, self.screen_sharing)
        self.camera_active = cam
        self.screen_recording = rec
        self.screen_sharing = share
        if cam and not prev[0]:
            self.emit("alert", "camera", "Camera active")
        if rec and not prev[1]:
            self.emit("alert", "recording", "Screen recording active")
        if share and not prev[2]:
            self.emit("alert", "screenshare", "Screen sharing active")
        if (cam, rec, share) != prev:
            self.emit("changed")


def _safe_listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []
