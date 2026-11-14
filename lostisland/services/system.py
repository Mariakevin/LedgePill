"""System monitoring for the expanded card.

Samples CPU, RAM, disk (usage + free GiB), SSD/CPU/GPU temperatures, network,
power, GPU (non-blocking), and optional MangoHud FPS on a 3 s timer that exists
only between start() and stop().
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import threading
import time
from typing import ClassVar

from gi.repository import GLib, GObject

from lostisland import config

_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")
_PREFERRED_ZONES = ("x86_pkg_temp", "k10temp", "zenpower", "coretemp", "tctl", "cpu")
_SSD_HWMON = ("nvme", "drivetemp", "sata")
_GPU_HWMON = ("amdgpu", "i915", "xe", "nouveau")
_CPU_HWMON = ("k10temp", "coretemp", "zenpower", "acpitz")


class SystemService(GObject.Object):
    __gsignals__: ClassVar[dict] = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self):
        super().__init__()
        self._timer = 0
        self._prime_timer = 0
        self._prev_total = 0
        self._prev_idle = 0
        self._prev_net: tuple[int, int] | None = None
        self._prev_net_time = 0.0
        self.last_data: dict = {}
        self._nvidia_smi = shutil.which("nvidia-smi")
        self._rocm_smi = shutil.which("rocm-smi")
        self._gpu_busy = False
        self._gpu_util: int | None = None
        self._gpu_temp: float | None = None
        self._stopped = False

    def start(self):
        self._stopped = False
        if not self._timer:
            self._sample(emit=False)
            self._prime_timer = GLib.timeout_add(400, self._first_sample)
            self._timer = GLib.timeout_add_seconds(3, self._sample)

    def _first_sample(self):
        self._prime_timer = 0
        if not self._stopped:
            self._sample(emit=True)
        return False

    def stop(self):
        self._stopped = True
        if self._prime_timer:
            GLib.source_remove(self._prime_timer)
            self._prime_timer = 0
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self._prev_total = self._prev_idle = 0
        self._prev_net = None

    def _read_hwmon_temps(self) -> tuple[float | None, float | None, dict[str, float]]:
        ssd_temp: float | None = None
        gpu_temp: float | None = None
        cpu_temps: dict[str, float] = {}
        try:
            for hdir in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
                try:
                    with open(f"{hdir}/name") as f:
                        name = f.read().strip().lower()
                except OSError:
                    continue
                tpath = f"{hdir}/temp1_input"
                if not os.path.exists(tpath):
                    continue
                try:
                    with open(tpath) as f:
                        temp = round(int(f.read().strip()) / 1000.0, 1)
                except (OSError, ValueError):
                    continue
                if any(k in name for k in _SSD_HWMON) and ssd_temp is None:
                    ssd_temp = temp
                elif any(k in name for k in _GPU_HWMON) and gpu_temp is None:
                    gpu_temp = temp
                elif any(k in name for k in _CPU_HWMON):
                    cpu_temps[name] = temp
        except OSError:
            pass
        return ssd_temp, gpu_temp, cpu_temps

    def _read_fps(self) -> int | None:
        fps_path = os.path.join(config.CACHE_DIR, "fps")
        for path in (os.environ.get("LOST_ISLAND_FPS_FILE", ""), fps_path):
            if not path or not os.path.isfile(path):
                continue
            try:
                st = os.stat(path)
                if time.time() - st.st_mtime > 4.0:
                    continue
                with open(path) as f:
                    val = f.read().strip()
                m = _NUM_RE.search(val)
                if m:
                    return max(0, round(float(m.group(1))))
            except (OSError, ValueError):
                continue
        return None

    def _sample(self, emit: bool = True):
        data = {
            "cpu": 0,
            "ram": 0,
            "ram_gib": 0.0,
            "gpu": self._gpu_util,
            "gpu_temp": self._gpu_temp,
            "ssd_temp": None,
            "disk": None,
            "disk_free_gib": None,
            "net_up": 0.0,
            "net_down": 0.0,
            "power": None,
            "fps": self._read_fps(),
            "temps": {},
        }
        try:
            with open("/proc/stat") as f:
                parts = f.readline().split()[1:]
            nums = list(map(int, parts))
            idle = (nums[3] if len(nums) > 3 else 0) + (nums[4] if len(nums) > 4 else 0)
            total = sum(nums)
            cpu = 0
            if self._prev_total:
                dt = total - self._prev_total
                di = idle - self._prev_idle
                cpu = max(0, min(100, round(100 * (dt - di) / dt))) if dt else 0
            self._prev_total, self._prev_idle = total, idle
            data["cpu"] = cpu

            info: dict[str, int] = {}
            with open("/proc/meminfo") as f:
                for i, line in enumerate(f):
                    if ":" not in line:
                        continue
                    k, v = line.split(":", 1)
                    vals = v.split()
                    if vals:
                        info[k] = int(vals[0])
                    if "MemTotal" in info and "MemAvailable" in info:
                        break
                    if i >= 24:
                        break
            mem_total = max(1, info.get("MemTotal", 1))
            avail = info.get("MemAvailable", info.get("MemFree", 0))
            used_kb = max(0, mem_total - avail)
            data["ram"] = max(0, min(100, round(100 * used_kb / mem_total)))
            data["ram_gib"] = used_kb / 1024 / 1024
        except (OSError, ValueError):
            pass

        # disk usage for root
        try:
            usage = shutil.disk_usage("/")
            data["disk"] = round(100 * usage.used / max(1, usage.total))
            data["disk_free_gib"] = round(usage.free / 1024 / 1024 / 1024, 1)
        except OSError:
            pass

        # temperatures (thermal_zone + hwmon SSD/GPU/CPU fallback)
        raw_temps: list[tuple[int, str, float]] = []
        try:
            for name in os.listdir("/sys/class/thermal"):
                if not name.startswith("thermal_zone"):
                    continue
                base = f"/sys/class/thermal/{name}"
                try:
                    with open(f"{base}/type") as f:
                        ttype = f.read().strip()
                    with open(f"{base}/temp") as f:
                        temp = int(f.read().strip()) / 1000.0
                    low = ttype.lower()
                    rank = next(
                        (i for i, p in enumerate(_PREFERRED_ZONES) if p in low),
                        len(_PREFERRED_ZONES),
                    )
                    raw_temps.append((rank, ttype, round(temp, 1)))
                except (OSError, ValueError):
                    continue
        except OSError:
            pass
        raw_temps.sort(key=lambda item: (item[0], item[1]))
        temps_map = {t: v for _r, t, v in raw_temps}
        ssd_temp, hw_gpu_temp, hw_cpu_temps = self._read_hwmon_temps()
        data["ssd_temp"] = ssd_temp
        if data["gpu_temp"] is None and hw_gpu_temp is not None:
            data["gpu_temp"] = hw_gpu_temp
        for k, v in hw_cpu_temps.items():
            temps_map.setdefault(k, v)
        data["temps"] = temps_map

        # network rates (excluding loopback)
        try:
            now = time.time()
            rx = tx = 0
            with open("/proc/net/dev") as f:
                for line in f.readlines()[2:]:
                    if ":" not in line:
                        continue
                    iface, rest = line.split(":", 1)
                    if iface.strip() == "lo":
                        continue
                    cols = rest.split()
                    if len(cols) < 9:
                        continue
                    rx += int(cols[0])
                    tx += int(cols[8])
            if self._prev_net is not None and now > self._prev_net_time:
                dt = now - self._prev_net_time
                data["net_down"] = max(0.0, (rx - self._prev_net[0]) / 1024.0 / dt)
                data["net_up"] = max(0.0, (tx - self._prev_net[1]) / 1024.0 / dt)
            self._prev_net = (rx, tx)
            self._prev_net_time = now
        except (OSError, ValueError):
            pass

        # power (battery)
        try:
            for name in os.listdir("/sys/class/power_supply"):
                base = f"/sys/class/power_supply/{name}"
                try:
                    with open(f"{base}/type") as f:
                        if f.read().strip() != "Battery":
                            continue
                except OSError:
                    continue
                try:
                    with open(f"{base}/power_now") as f:
                        data["power"] = abs(int(f.read().strip())) / 1e6
                        break
                except (OSError, ValueError):
                    try:
                        with open(f"{base}/current_now") as f:
                            cur = abs(int(f.read().strip()))
                        with open(f"{base}/voltage_now") as f:
                            vol = abs(int(f.read().strip()))
                        data["power"] = cur * vol / 1e12
                        break
                    except (OSError, ValueError):
                        continue
        except OSError:
            pass

        if (self._nvidia_smi or self._rocm_smi) and not self._gpu_busy:
            self._gpu_busy = True
            threading.Thread(target=self._poll_gpu, daemon=True).start()
        elif not self._nvidia_smi and not self._rocm_smi and data["gpu"] is None:
            for path in sorted(
                glob.glob("/sys/class/drm/card*/device/gpu_busy_percent")
            ):
                try:
                    with open(path) as f:
                        data["gpu"] = max(0, min(100, int(f.read().strip())))
                    break
                except (OSError, ValueError):
                    continue

        self.last_data = data
        if emit:
            self.emit("changed", data)
        return True

    def _poll_gpu(self):
        util: int | None = None
        temp: float | None = None
        try:
            if self._nvidia_smi:
                out = subprocess.run(
                    [
                        self._nvidia_smi,
                        "--query-gpu=utilization.gpu,temperature.gpu",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=1.0,
                )
                lines = (out.stdout or "").strip().splitlines()
                if out.returncode == 0 and lines:
                    parts = [p.strip() for p in lines[0].split(",")]
                    if len(parts) >= 2:
                        util = int(float(parts[0]))
                        temp = float(parts[1])
            elif self._rocm_smi:
                out = subprocess.run(
                    [self._rocm_smi, "--showuse", "--showtemp"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=1.0,
                )
                if out.returncode == 0:
                    for line in (out.stdout or "").splitlines():
                        rhs = line.rsplit(":", 1)[-1]
                        m = _NUM_RE.search(rhs)
                        if not m:
                            continue
                        if "GPU use" in line and util is None:
                            util = int(float(m.group(1)))
                        elif "Temperature" in line and temp is None:
                            temp = float(m.group(1))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass

        def finish():
            if self._stopped:
                self._gpu_busy = False
                return False
            self._gpu_busy = False
            self._gpu_util = util
            self._gpu_temp = temp
            return False

        GLib.idle_add(finish)
