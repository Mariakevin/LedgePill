"""Weather tile backed by Open-Meteo (+ IP geolocation) with wttr.in fallback.

Deliberately not a poller: a fetch happens only when the expanded card asks
for one and the cached value is older than 30 minutes. Closed island = zero
network traffic.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import ClassVar

from gi.repository import GLib, GObject

TTL = 30 * 60

# wttr.in weather codes → symbolic icon
_ICONS = {
    "113": "weather-clear-symbolic",
    "116": "weather-few-clouds-symbolic",
    "119": "weather-overcast-symbolic",
    "122": "weather-overcast-symbolic",
    "143": "weather-fog-symbolic",
    "248": "weather-fog-symbolic",
    "260": "weather-fog-symbolic",
}

_WMO_MAP = {
    0: ("Clear sky", "weather-clear-symbolic"),
    1: ("Mainly clear", "weather-few-clouds-symbolic"),
    2: ("Partly cloudy", "weather-few-clouds-symbolic"),
    3: ("Overcast", "weather-overcast-symbolic"),
    45: ("Fog", "weather-fog-symbolic"),
    48: ("Rime fog", "weather-fog-symbolic"),
    51: ("Light drizzle", "weather-showers-symbolic"),
    53: ("Drizzle", "weather-showers-symbolic"),
    55: ("Dense drizzle", "weather-showers-symbolic"),
    56: ("Freezing drizzle", "weather-showers-symbolic"),
    57: ("Freezing drizzle", "weather-showers-symbolic"),
    61: ("Light rain", "weather-showers-symbolic"),
    63: ("Rain", "weather-showers-symbolic"),
    65: ("Heavy rain", "weather-showers-symbolic"),
    66: ("Freezing rain", "weather-showers-symbolic"),
    67: ("Freezing rain", "weather-showers-symbolic"),
    71: ("Light snow", "weather-snow-symbolic"),
    73: ("Snow", "weather-snow-symbolic"),
    75: ("Heavy snow", "weather-snow-symbolic"),
    77: ("Snow grains", "weather-snow-symbolic"),
    80: ("Rain showers", "weather-showers-symbolic"),
    81: ("Rain showers", "weather-showers-symbolic"),
    82: ("Heavy showers", "weather-showers-symbolic"),
    85: ("Snow showers", "weather-snow-symbolic"),
    86: ("Heavy snow", "weather-snow-symbolic"),
    95: ("Thunderstorm", "weather-storm-symbolic"),
    96: ("Thunderstorm", "weather-storm-symbolic"),
    99: ("Thunderstorm", "weather-storm-symbolic"),
}


def _icon_for_code(code: str) -> str:
    icon = _ICONS.get(str(code))
    if icon is not None:
        return icon
    try:
        n = int(code)
    except (TypeError, ValueError):
        return "weather-few-clouds-symbolic"
    if n in (200, 386, 389, 392, 395):
        return "weather-storm-symbolic"
    if n >= 320:
        return "weather-snow-symbolic"
    if n >= 263:
        return "weather-showers-symbolic"
    return "weather-few-clouds-symbolic"


def _wmo_info(code: int) -> tuple[str, str]:
    if code in _WMO_MAP:
        return _WMO_MAP[code]
    if code >= 95:
        return ("Thunderstorm", "weather-storm-symbolic")
    if code >= 71:
        return ("Snow", "weather-snow-symbolic")
    if code >= 51:
        return ("Rain", "weather-showers-symbolic")
    if code >= 45:
        return ("Fog", "weather-fog-symbolic")
    return ("Partly cloudy", "weather-few-clouds-symbolic")


def _http_json(url: str, timeout: float = 6.0) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": "lost-island"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    return data if isinstance(data, dict) else None


class WeatherService(GObject.Object):
    __gsignals__: ClassVar[dict] = {
        # temp string ("21°"), description, icon name
        "changed": (GObject.SignalFlags.RUN_FIRST, None, (str, str, str)),
    }

    def __init__(self, city: str = ""):
        super().__init__()
        self.city = (city or "").strip()
        self.location = self.city
        self.temp = ""
        self.desc = ""
        self.icon = "weather-clear-symbolic"
        self.forecast: list[dict] = []
        self._stamp = 0.0
        self._busy = False
        self._stopped = False
        self._gen = 0

    def set_city(self, city: str):
        clean = (city or "").strip()
        if clean != self.city:
            self.city = clean
            self._stamp = 0.0
            self._gen += 1
            self._busy = False
            self.request()

    def stop(self):
        self._stopped = True
        self._gen += 1
        self._busy = False

    def is_stale(self) -> bool:
        return not self._stamp or time.time() - self._stamp >= TTL

    def request(self, force: bool = False):
        """Refresh in the background if stale; emits `changed` when new data lands."""
        if force:
            self._stamp = 0.0
        if self._busy or not self.is_stale():
            return
        self._busy = True
        self._gen += 1
        threading.Thread(
            target=self._fetch, args=(self.city, self._gen), daemon=True
        ).start()

    @staticmethod
    def _resolve_coords(city: str) -> tuple[float, float, str] | None:
        if city:
            q = urllib.parse.quote(city)
            data = _http_json(
                f"https://geocoding-api.open-meteo.com/v1/search?name={q}&count=1&language=en&format=json"
            )
            results = (data or {}).get("results") or []
            if results:
                hit = results[0]
                lat = float(hit["latitude"])
                lon = float(hit["longitude"])
                parts = [
                    str(hit.get(k) or "").strip()
                    for k in ("name", "country_code")
                    if hit.get(k)
                ]
                return lat, lon, ", ".join(parts) or city
            return None

        for url in ("https://ipwho.is/", "https://ipapi.co/json/"):
            try:
                geo = _http_json(url, timeout=4.5)
                if not geo:
                    continue
                if geo.get("success") is False:
                    continue
                lat = float(geo["latitude"])
                lon = float(geo["longitude"])
                c_name = str(geo.get("city") or "").strip()
                cc = str(geo.get("country_code") or geo.get("country") or "").strip()
                loc = ", ".join(p for p in (c_name, cc) if p)
                return lat, lon, loc
            except (
                http.client.HTTPException,
                KeyError,
                OSError,
                TimeoutError,
                TypeError,
                ValueError,
                urllib.error.URLError,
            ):
                continue
        return None

    @staticmethod
    def _fetch_open_meteo(
        city: str,
    ) -> tuple[str, str, str, list[dict], str] | None:
        coords = WeatherService._resolve_coords(city)
        if coords is None:
            return None
        lat, lon, loc = coords
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat:.4f}&longitude={lon:.4f}"
            "&current=temperature_2m,weather_code"
            "&daily=temperature_2m_min,temperature_2m_max,weather_code"
            "&timezone=auto&forecast_days=4"
        )
        data = _http_json(url, timeout=6.0)
        if not data or "current" not in data:
            return None
        cur = data["current"]
        temp_c = round(float(cur["temperature_2m"]))
        wmo = int(cur.get("weather_code", 0))
        desc, icon = _wmo_info(wmo)

        forecast: list[dict] = []
        daily = data.get("daily") or {}
        dates = daily.get("time") or []
        mins = daily.get("temperature_2m_min") or []
        maxs = daily.get("temperature_2m_max") or []
        codes = daily.get("weather_code") or []
        for idx, d_str in enumerate(dates[:4]):
            d_code = (
                int(codes[idx]) if idx < len(codes) and codes[idx] is not None else wmo
            )
            _d, d_ico = _wmo_info(d_code)
            lo = (
                round(float(mins[idx]))
                if idx < len(mins) and mins[idx] is not None
                else ""
            )
            hi = (
                round(float(maxs[idx]))
                if idx < len(maxs) and maxs[idx] is not None
                else ""
            )
            forecast.append(
                {"date": str(d_str), "min": str(lo), "max": str(hi), "icon": d_ico}
            )
        return f"{temp_c}°", desc, icon, forecast, loc or city

    @staticmethod
    def _fetch_wttr(city: str) -> tuple[str, str, str, list[dict], str] | None:
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1"
        data = _http_json(url, timeout=8.0)
        if not data:
            return None
        now = data["current_condition"][0]
        temp = f"{now['temp_C']}°"
        desc = now["weatherDesc"][0]["value"]
        code = now["weatherCode"]
        icon = _icon_for_code(code)
        loc = city
        areas = data.get("nearest_area") or []
        if areas and isinstance(areas[0], dict):
            an = (areas[0].get("areaName") or [{}])[0].get("value", "")
            co = (areas[0].get("country") or [{}])[0].get("value", "")
            resolved = ", ".join(p for p in (str(an).strip(), str(co).strip()) if p)
            if resolved:
                loc = resolved
        forecast = []
        for day in data.get("weather", [])[:4]:
            hourly = day.get("hourly", [])
            noon = hourly[len(hourly) // 2] if hourly else {}
            day_code = noon.get("weatherCode", code)
            forecast.append(
                {
                    "date": day.get("date", ""),
                    "min": day.get("mintempC", ""),
                    "max": day.get("maxtempC", ""),
                    "icon": _icon_for_code(day_code),
                }
            )
        return temp, desc, icon, forecast, loc

    def _fetch(self, city: str, gen: int):
        result: tuple[str, str, str, list[dict], str] | None = None
        for fetcher in (self._fetch_open_meteo, self._fetch_wttr):
            try:
                result = fetcher(city)
                if result is not None:
                    break
            except (
                http.client.HTTPException,
                IndexError,
                KeyError,
                OSError,
                TimeoutError,
                TypeError,
                ValueError,
                urllib.error.URLError,
            ):
                continue

        if result is None:
            GLib.idle_add(self._clear_busy, gen)
            return

        temp, desc, icon, forecast, loc = result

        def done():
            self._busy = False
            if self._stopped or gen != self._gen:
                return False
            self.temp, self.desc, self.icon = temp, desc, icon
            self.forecast = forecast
            self.location = loc or city or "Local"
            self._stamp = time.time()
            self.emit("changed", temp, desc, icon)
            return False

        GLib.idle_add(done)

    def _clear_busy(self, gen: int | None = None):
        if gen is None or gen == self._gen:
            self._busy = False
        return False
