"""User configuration, stored as plain JSON in ~/.config/ledge-pill/config.json.

Missing or malformed keys fall back to defaults, so upgrades or hand edits
never break the app.
"""

from __future__ import annotations

import copy
import json
import os

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "ledge-pill"
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "ledge-pill"
)

VALID_FACES = (
    "auto",
    "status",
    "lyrics",
    "title",
    "compact",
    "clock",
    "battery",
    "weather",
    "bluetooth",
    "mascot",
)

VALID_THEMES = ("obsidian", "liquid-glass", "frosted", "midnight", "amber")
VALID_ANIMATIONS = ("fluid", "snappy", "gentle")
VALID_STYLES = ("pill", "dashboard", "vertical", "waterdrop", "notch")
VALID_ACCENT_MODES = ("manual", "auto", "system", "artwork")
VALID_FULLSCREEN = ("ignore", "hide", "compact")
VALID_AI_PROVIDERS = ("ollama", "lmstudio", "claude", "gemini", "openrouter")

DEFAULTS = {
    # top margin in px between screen edge and the island
    "margin_top": 0,
    # sit on top of panels/bars instead of being pushed below them
    "overlap_panel": True,
    # monitor connector name ("" = primary / compositor default)
    "monitor": "",
    # per-monitor layout overrides: {connector: {margin_top, style, layer, overlap_panel}}
    "monitor_layouts": {},
    # "top" stays below fullscreen apps, "overlay" is always on top
    "layer": "top",
    # "ignore", "hide", or "compact" when an active window is fullscreen
    "fullscreen_behavior": "ignore",
    # show the clock in the idle pill
    "idle_clock": True,
    # current collapsed face; cycled by click / swipe
    "pill_face": "auto",
    # faces included in the cycle, in order
    "pill_faces": ["auto", "status", "lyrics", "title", "clock"],
    # schema version for one-time setting migrations
    "config_version": 2,
    # what a left click on the pill does: "wake" (AI wobble pop-up), "expand" the card, or "cycle" faces
    "click_action": "wake",
    # island background opacity, 0.3 .. 1.0 (1.0 = solid)
    "opacity": 1.0,
    # 24h clock
    "clock_24h": True,
    # small battery readout in the pill while charging or below 30%
    "pill_battery": True,
    # seconds a peek (volume / battery / notification) stays visible
    "peek_seconds": 2.2,
    # appearance & animations
    "theme": "obsidian",
    "custom_font": "",
    "animation_style": "fluid",
    "island_style": "pill",
    "accent_mode": "manual",
    "dynamic_artwork_colors": False,
    "background_media": "",
    "mascot": False,
    # thin battery ring around the idle clock
    "status_ring": True,
    # tint the island surface with the active accent/artwork
    "ambient_tint": True,
    # show queued peeks one after another instead of replacing
    "activity_queue": True,
    # modules that may take over the island
    "modules": {
        "music": True,
        "volume_osd": True,
        "brightness": True,
        "battery": True,
        "notifications": True,
        "network": True,
        "bluetooth": True,
        "weather": True,
        "system": True,
        "toggles": True,
        "lyrics": True,
        "clipboard": True,
        "ollama": True,
        "feeds": True,
        "privacy": True,
        "desktop": True,
        "kdeconnect": True,
        "launcher": True,
        "assistant": True,
        "todos": True,
        "calendar": True,
        "camera_mirror": True,
    },
    # weather location ("" = auto by IP); any wttr.in place name works
    "weather_city": "",
    # User configured RSS/Atom or JSON sources shown in the expanded card.
    "feeds": [],
    # Optional .ics file paths or read-only http(s) iCal feeds
    "calendar_ics": [],
    "do_not_disturb": False,
    "pomodoro_auto_cycle": False,
    "focus_auto_dnd": False,
    "notification_blocklist": [],
    "notification_rules": [],
    "favorite_apps": [],
    "webhook_port": 17334,
    "ai_provider": "ollama",
    "ai_model": "",
    "ai_api_key": "",
    "ai_system_control": False,
    "shortcuts": {
        "toggle": "Super+I",
        "dnd": "Super+Shift+D",
        "lock": "Super+Shift+L",
    },
    # accent color used for progress bars and highlights
    "accent": "#ff9f0a",
}


def _clamp_int(val, default: int, lo: int, hi: int) -> int:
    if isinstance(val, bool):
        return default
    try:
        return max(lo, min(hi, int(val)))
    except (TypeError, ValueError):
        return default


def _clamp_float(val, default: float, lo: float, hi: float) -> float:
    if isinstance(val, bool):
        return default
    try:
        return max(lo, min(hi, float(val)))
    except (TypeError, ValueError):
        return default


def _as_bool(val, default: bool) -> bool:
    """Accept JSON booleans only; strings like ``"false"`` must not enable a setting."""
    return val if isinstance(val, bool) else default


def _str_list(val, limit: int = 50, max_len: int = 512) -> list[str]:
    if not isinstance(val, list):
        return []
    return list(
        dict.fromkeys(
            item.strip()[:max_len]
            for item in val[:limit]
            if isinstance(item, str) and item.strip()
        )
    )


def _normalize(cfg: dict) -> dict:
    out = copy.deepcopy(DEFAULTS)
    if not isinstance(cfg, dict):
        return out

    out["margin_top"] = _clamp_int(cfg.get("margin_top"), 0, 0, 200)
    out["overlap_panel"] = _as_bool(cfg.get("overlap_panel"), True)
    layer = cfg.get("layer", "top")
    out["layer"] = layer if layer in ("top", "overlay") else "top"
    style = cfg.get("island_style", "pill")
    out["island_style"] = style if style in VALID_STYLES else "pill"
    monitor = cfg.get("monitor", "")
    out["monitor"] = monitor if isinstance(monitor, str) else ""
    raw_ml = cfg.get("monitor_layouts")
    if isinstance(raw_ml, dict):
        ml_clean = {}
        for mon, ov in raw_ml.items():
            if not isinstance(mon, str) or not mon.strip() or not isinstance(ov, dict):
                continue
            ml_clean[mon.strip()[:64]] = {
                "margin_top": _clamp_int(
                    ov.get("margin_top"), out["margin_top"], 0, 200
                ),
                "style": (
                    ov.get("style")
                    if ov.get("style") in VALID_STYLES
                    else out["island_style"]
                ),
                "layer": (
                    ov.get("layer")
                    if ov.get("layer") in ("top", "overlay")
                    else out["layer"]
                ),
                "overlap_panel": _as_bool(
                    ov.get("overlap_panel"), out["overlap_panel"]
                ),
            }
        out["monitor_layouts"] = ml_clean

    fs = cfg.get("fullscreen_behavior", "ignore")
    out["fullscreen_behavior"] = fs if fs in VALID_FULLSCREEN else "ignore"
    out["idle_clock"] = _as_bool(cfg.get("idle_clock"), True)

    raw_faces = cfg.get("pill_faces")
    if isinstance(raw_faces, list):
        seen: list[str] = []
        for f in raw_faces:
            if isinstance(f, str) and f in VALID_FACES and f not in seen:
                seen.append(f)
        if seen:
            out["pill_faces"] = seen

    face = cfg.get("pill_face", out["pill_faces"][0])
    out["pill_face"] = (
        face if isinstance(face, str) and face in VALID_FACES else out["pill_faces"][0]
    )
    if out["pill_face"] not in out["pill_faces"]:
        out["pill_face"] = out["pill_faces"][0]

    click_action = cfg.get("click_action", "wake")
    out["config_version"] = _clamp_int(
        cfg.get("config_version"), DEFAULTS["config_version"], 1, 99
    )
    out["click_action"] = (
        click_action if click_action in ("wake", "expand", "cycle") else "wake"
    )
    out["opacity"] = round(_clamp_float(cfg.get("opacity"), 1.0, 0.3, 1.0), 2)
    out["clock_24h"] = _as_bool(cfg.get("clock_24h"), True)
    out["pill_battery"] = _as_bool(cfg.get("pill_battery"), True)
    out["do_not_disturb"] = _as_bool(cfg.get("do_not_disturb"), False)
    out["pomodoro_auto_cycle"] = _as_bool(cfg.get("pomodoro_auto_cycle"), False)
    out["focus_auto_dnd"] = _as_bool(cfg.get("focus_auto_dnd"), False)
    out["peek_seconds"] = round(
        _clamp_float(cfg.get("peek_seconds"), 2.2, 0.5, 15.0), 1
    )

    theme = cfg.get("theme", "obsidian")
    out["theme"] = theme if theme in VALID_THEMES else "obsidian"
    font = cfg.get("custom_font", "")
    out["custom_font"] = font.strip()[:80] if isinstance(font, str) else ""
    anim = cfg.get("animation_style", "fluid")
    out["animation_style"] = anim if anim in VALID_ANIMATIONS else "fluid"
    dyn_art = _as_bool(cfg.get("dynamic_artwork_colors"), False)
    raw_mode = cfg.get("accent_mode")
    if isinstance(raw_mode, str) and raw_mode in VALID_ACCENT_MODES:
        out["accent_mode"] = raw_mode
        out["dynamic_artwork_colors"] = raw_mode in ("auto", "artwork")
    elif dyn_art:
        out["accent_mode"] = "artwork"
        out["dynamic_artwork_colors"] = True
    else:
        out["accent_mode"] = "manual"
        out["dynamic_artwork_colors"] = False
    bg_media = cfg.get("background_media", "")
    out["background_media"] = (
        bg_media.strip()[:1024] if isinstance(bg_media, str) else ""
    )
    out["mascot"] = _as_bool(cfg.get("mascot"), False)
    out["status_ring"] = _as_bool(cfg.get("status_ring"), True)
    out["ambient_tint"] = _as_bool(cfg.get("ambient_tint"), True)
    out["activity_queue"] = _as_bool(cfg.get("activity_queue"), True)

    raw_mods = cfg.get("modules")
    if isinstance(raw_mods, dict):
        for k in out["modules"]:
            if k in raw_mods:
                out["modules"][k] = _as_bool(raw_mods[k], out["modules"][k])

    city = cfg.get("weather_city", "")
    out["weather_city"] = city.strip() if isinstance(city, str) else ""
    raw_feeds = cfg.get("feeds", [])
    if isinstance(raw_feeds, list):
        out["feeds"] = [
            {
                "name": str(feed.get("name", ""))[:80],
                "type": feed.get("type", "rss"),
                "url": feed.get("url", ""),
            }
            for feed in raw_feeds[:20]
            if isinstance(feed, dict)
            and feed.get("type", "rss") in ("rss", "json")
            and isinstance(feed.get("url"), str)
            and len(feed["url"]) <= 2048
        ]
    out["calendar_ics"] = _str_list(cfg.get("calendar_ics"), limit=16, max_len=512)
    out["notification_blocklist"] = _str_list(
        cfg.get("notification_blocklist"), limit=100, max_len=120
    )
    out["notification_rules"] = _str_list(
        cfg.get("notification_rules"), limit=100, max_len=200
    )
    out["favorite_apps"] = _str_list(cfg.get("favorite_apps"), limit=24, max_len=128)
    out["webhook_port"] = _clamp_int(
        cfg.get("webhook_port"), DEFAULTS["webhook_port"], 1024, 65535
    )

    ai_prov = cfg.get("ai_provider", "ollama")
    out["ai_provider"] = ai_prov if ai_prov in VALID_AI_PROVIDERS else "ollama"
    ai_model = cfg.get("ai_model", "")
    out["ai_model"] = ai_model.strip()[:120] if isinstance(ai_model, str) else ""
    ai_key = cfg.get("ai_api_key", "")
    out["ai_api_key"] = ai_key.strip()[:256] if isinstance(ai_key, str) else ""
    out["ai_system_control"] = _as_bool(cfg.get("ai_system_control"), False)
    raw_sc = cfg.get("shortcuts")
    if isinstance(raw_sc, dict):
        for k in out["shortcuts"]:
            if isinstance(raw_sc.get(k), str) and raw_sc[k].strip():
                out["shortcuts"][k] = raw_sc[k].strip()[:64]

    accent = cfg.get("accent", "#ff9f0a")
    out["accent"] = accent if isinstance(accent, str) and accent.strip() else "#ff9f0a"
    return out


def _merge(base: dict, override: dict) -> dict:
    if not isinstance(base, dict):
        return {}
    if not isinstance(override, dict):
        return copy.deepcopy(base)

    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            out[k] = _merge(base[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return copy.deepcopy(DEFAULTS)

    if not isinstance(raw, dict):
        return copy.deepcopy(DEFAULTS)

    migrated = False
    if (
        _clamp_int(raw.get("config_version"), 1, 1, 99) < 2
        and raw.get("click_action") == "cycle"
    ):
        raw = {**raw, "click_action": "wake", "config_version": 2}
        migrated = True

    out = _normalize(_merge(DEFAULTS, raw))
    if migrated:
        try:
            save(out)
        except OSError:
            pass
    return out


def write_default() -> str:
    """Write a default config if none exists; returns the path."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        tmp = f"{CONFIG_PATH}.tmp"
        with open(tmp, "w") as f:
            json.dump(DEFAULTS, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_PATH)
    return CONFIG_PATH


def save(cfg: dict) -> None:
    normalized = (
        _normalize(_merge(DEFAULTS, cfg))
        if isinstance(cfg, dict)
        else copy.deepcopy(DEFAULTS)
    )
    parent = os.path.dirname(CONFIG_PATH) or CONFIG_DIR
    os.makedirs(parent, exist_ok=True)
    tmp = f"{CONFIG_PATH}.tmp"
    with open(tmp, "w") as f:
        json.dump(normalized, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)
