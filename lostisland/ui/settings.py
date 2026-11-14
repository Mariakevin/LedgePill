"""Preferences window. Every change applies and persists instantly;
structural changes (modules) rebuild the island in place."""

from __future__ import annotations

from gi.repository import Adw, Gdk, Gtk

from lostisland import config

FACES = [
    ("auto", "Automatic", "Music with art + EQ when playing, clock otherwise"),
    ("status", "Clock + battery", "Time and charge, always"),
    ("lyrics", "Lyrics", "The current synced lyric line"),
    ("title", "Song title", "Just the title — never the artist"),
    ("compact", "Music icon", "Album art and EQ only"),
    ("clock", "Clock only", ""),
    ("battery", "Battery only", ""),
    ("weather", "Weather", "Temperature and conditions"),
    ("bluetooth", "Bluetooth", "Connected device and its battery"),
    ("mascot", "Mascot eyes", "Clock with cursor-tracking animated eyes"),
]

MODULES = [
    ("music", "Music", "MPRIS players: pill face, full controls, album art"),
    ("lyrics", "Lyrics", "Synced lyrics from lrclib.net for the lyrics face"),
    ("volume_osd", "Volume OSD", "Peek on volume changes"),
    ("brightness", "Brightness", "Brightness indicator and brightness OSD"),
    ("battery", "Battery", "Charge ring and plug/unplug peeks"),
    ("notifications", "Notifications", "Mirror notifications as peeks"),
    (
        "privacy",
        "Privacy indicators",
        "Microphone, camera, and screen recording indicators",
    ),
    ("network", "Network", "Connectivity chip and peeks"),
    ("bluetooth", "Bluetooth", "Connected device chip and peeks"),
    ("weather", "Weather", "Weather tile in the expanded card"),
    ("system", "System stats", "CPU and RAM readout while expanded"),
    ("toggles", "Quick toggles", "Caffeine and screenshot"),
    ("clipboard", "Clipboard history", "Stores recently copied text"),
    (
        "desktop",
        "Desktop controls",
        "Background apps, keyboard layout, power menu",
    ),
    ("kdeconnect", "KDE Connect", "Phone battery and device actions via KDE Connect"),
    (
        "launcher",
        "Background apps",
        "Monitor and manage apps running in the background",
    ),
    ("ollama", "Ollama", "Local Ollama AI model monitor"),
    ("assistant", "AI assistant", "AI chat and local LLM build telemetry"),
    ("todos", "Focus tasks", "Lightweight Focus checklist inside the timer tile"),
    ("calendar", "Calendar agenda", "Upcoming events from local .ics or iCal feeds"),
    ("camera_mirror", "Camera mirror", "On-demand pre-call webcam preview in header"),
    ("feeds", "RSS and JSON feeds", "User-configured information feeds in the card"),
]


class SettingsWindow(Adw.PreferencesWindow):
    def __init__(self, cfg: dict, on_apply):
        super().__init__(title="Lost Island")
        self.set_default_size(440, 660)
        self.set_search_enabled(False)
        self.cfg = cfg
        self.on_apply = on_apply

        self.add(self._island_page())
        self.add(self._modules_page())
        self.add(self._integrations_page())

    # -- helpers -----------------------------------------------------------

    def _apply(self, structural: bool = False):
        config.save(self.cfg)
        self.on_apply(structural)

    def _switch(
        self,
        title: str,
        subtitle: str,
        key: str,
        structural: bool = False,
        mod: bool = False,
    ):
        row = Adw.SwitchRow(use_markup=False)
        row.set_title(title)
        row.set_subtitle(subtitle)
        default_val = (
            config.DEFAULTS.get("modules", {}).get(key, True)
            if mod
            else config.DEFAULTS.get(key, True)
        )
        val = (
            self.cfg.get("modules", {}).get(key, default_val)
            if mod
            else self.cfg.get(key, default_val)
        )
        row.set_active(bool(val))

        def on_change(r, _p):
            if mod:
                self.cfg.setdefault("modules", {})[key] = r.get_active()
            else:
                self.cfg[key] = r.get_active()
            self._apply(structural)

        row.connect("notify::active", on_change)
        return row

    def _combo(
        self,
        title: str,
        subtitle: str,
        key: str,
        options: list[tuple[str, str]],
        structural: bool = False,
    ):
        row = Adw.ComboRow(use_markup=False)
        row.set_title(title)
        row.set_subtitle(subtitle)
        row.set_model(Gtk.StringList.new([label for _val, label in options]))
        cur = str(self.cfg.get(key, options[0][0]))
        idx = next((i for i, (val, _lbl) in enumerate(options) if val == cur), 0)
        row.set_selected(idx)

        def on_sel(r, _p):
            sel = r.get_selected()
            if 0 <= sel < len(options):
                self.cfg[key] = options[sel][0]
                self._apply(structural)

        row.connect("notify::selected", on_sel)
        return row

    def _entry(
        self,
        title: str,
        key: str,
        structural: bool = False,
        secret: bool = False,
        csv_list: bool = False,
    ):
        row = (
            Adw.PasswordEntryRow(use_markup=False)
            if secret
            else Adw.EntryRow(use_markup=False)
        )
        row.set_title(title)
        raw = self.cfg.get(key, [] if csv_list else "")
        if csv_list:
            row.set_text(", ".join(str(x) for x in raw))
        else:
            row.set_text(str(raw or ""))
        row.set_show_apply_button(True)

        def on_apply(r):
            txt = r.get_text().strip()
            if csv_list:
                self.cfg[key] = [x.strip() for x in txt.split(",") if x.strip()]
            else:
                self.cfg[key] = txt
            self._apply(structural)

        row.connect("apply", on_apply)
        return row

    # -- pages -------------------------------------------------------------

    def _island_page(self):
        page = Adw.PreferencesPage(title="Island", icon_name="go-home-symbolic")

        pos = Adw.PreferencesGroup(title="Position")
        pos.add(
            self._switch(
                "Sit on top of panels",
                "Overlap the bar at the screen edge instead of sitting below it",
                "overlap_panel",
            )
        )

        margin = Adw.SpinRow.new_with_range(0, 80, 1)
        margin.set_title("Top margin")
        margin.set_subtitle("Distance from the screen edge, px")
        margin.set_value(self.cfg.get("margin_top", 0))
        margin.connect(
            "notify::value",
            lambda r, _p: (
                self.cfg.__setitem__("margin_top", int(r.get_value())),
                self._apply(),
            ),
        )
        pos.add(margin)

        monitors = ["Primary"]
        display = Gdk.Display.get_default()
        if display:
            items = display.get_monitors()
            monitors += [
                items.get_item(i).get_connector() or f"#{i}"
                for i in range(items.get_n_items())
            ]
        combo = Adw.ComboRow(title="Monitor")
        combo.set_model(Gtk.StringList.new(monitors))
        try:
            combo.set_selected(monitors.index(self.cfg.get("monitor", "")))
        except ValueError:
            combo.set_selected(0)

        def on_monitor(r, _p):
            i = r.get_selected()
            self.cfg["monitor"] = "" if i == 0 else monitors[i]
            self._apply()

        combo.connect("notify::selected", on_monitor)
        pos.add(combo)

        overlay = Adw.SwitchRow(
            title="Above fullscreen apps",
            subtitle="Keep the island visible over games and videos",
        )
        overlay.set_active(self.cfg.get("layer", "top") == "overlay")
        overlay.connect(
            "notify::active",
            lambda r, _p: (
                self.cfg.__setitem__("layer", "overlay" if r.get_active() else "top"),
                self._apply(),
            ),
        )
        pos.add(overlay)
        pos.add(
            self._combo(
                "Fullscreen apps",
                "How the island responds when an active window goes fullscreen",
                "fullscreen_behavior",
                [
                    ("ignore", "Ignore fullscreen"),
                    ("hide", "Hide while fullscreen"),
                    ("compact", "Keep compact pill"),
                ],
            )
        )
        page.add(pos)

        look = Adw.PreferencesGroup(title="Look")
        look.add(
            self._combo(
                "Theme",
                "Surface glass and atmosphere palette",
                "theme",
                [
                    ("obsidian", "Obsidian"),
                    ("liquid-glass", "Liquid Glass"),
                    ("frosted", "Frosted"),
                    ("midnight", "Midnight"),
                    ("amber", "Amber"),
                ],
            )
        )
        look.add(
            self._combo(
                "Island style",
                "Collapsed surface layout profile",
                "island_style",
                [
                    ("pill", "Floating Pill"),
                    ("notch", "Edge Notch"),
                    ("waterdrop", "Waterdrop Notch"),
                    ("dashboard", "Dashboard Strip"),
                    ("vertical", "Compact Vertical"),
                ],
            )
        )
        look.add(
            self._combo(
                "Animation style",
                "State transition feel between pill, peek, and card",
                "animation_style",
                [
                    ("fluid", "Fluid"),
                    ("snappy", "Snappy"),
                    ("gentle", "Gentle"),
                ],
            )
        )
        op_row = Adw.ActionRow(
            title="Background opacity",
            subtitle="100% = solid dark surface (recommended)",
            use_markup=False,
        )
        op_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        op_box.set_valign(Gtk.Align.CENTER)
        op_val = Gtk.Label(label=f"{round(float(self.cfg.get('opacity', 1.0)) * 100)}%")
        op_val.set_width_chars(4)
        op_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 30, 100, 1)
        op_scale.set_size_request(116, -1)
        op_scale.set_draw_value(False)
        op_scale.set_value(round(float(self.cfg.get("opacity", 1.0)) * 100))

        def on_op_changed(sc):
            pct = max(30, min(100, round(sc.get_value())))
            op_val.set_label(f"{pct}%")
            self.cfg["opacity"] = round(pct / 100, 2)
            self._apply()

        op_scale.connect("value-changed", on_op_changed)
        solid_btn = Gtk.Button(label="Solid")
        solid_btn.add_css_class("flat")
        solid_btn.set_tooltip_text("Set 100% solid opaque background")
        solid_btn.connect("clicked", lambda *_: op_scale.set_value(100))
        op_box.append(op_scale)
        op_box.append(op_val)
        op_box.append(solid_btn)
        op_row.add_suffix(op_box)
        look.add(op_row)
        look.add(
            self._combo(
                "Accent selection",
                "Automatically match media art, system theme/wallpaper, or set manually",
                "accent_mode",
                [
                    ("auto", "Automatic (artwork, system & wallpaper)"),
                    ("system", "System theme & wallpaper"),
                    ("artwork", "Playing media artwork"),
                    ("manual", "Manual custom color"),
                ],
            )
        )
        color = Adw.ActionRow(
            title="Accent color",
            subtitle="Custom color (used in Manual mode or as fallback)",
            use_markup=False,
        )
        btn = Gtk.ColorDialogButton()
        btn.set_dialog(Gtk.ColorDialog(with_alpha=False))
        rgba = Gdk.RGBA()
        if not rgba.parse(self.cfg.get("accent", "#ff9f0a")):
            rgba.parse("#ff9f0a")
        btn.set_rgba(rgba)
        btn.set_valign(Gtk.Align.CENTER)

        def on_color(b, _p):
            c = b.get_rgba()
            self.cfg["accent"] = (
                f"#{round(c.red * 255):02x}"
                f"{round(c.green * 255):02x}"
                f"{round(c.blue * 255):02x}"
            )
            self._apply()

        btn.connect("notify::rgba", on_color)
        color.add_suffix(btn)
        look.add(color)
        look.add(
            self._switch(
                "Mascot eyes on idle clock",
                "Show animated cursor-tracking eyes beside the clock pill",
                "mascot",
            )
        )
        look.add(self._entry("Custom font family", "custom_font"))
        look.add(self._entry("Background image / GIF path", "background_media"))
        look.add(self._switch("24-hour clock", "", "clock_24h"))
        look.add(self._switch("Clock in the idle pill", "", "idle_clock"))
        look.add(
            self._switch(
                "Battery in the pill",
                "Shown while charging or below 30%",
                "pill_battery",
            )
        )
        look.add(
            self._switch(
                "Status ring around clock",
                "Battery charge ring; hidden while the pill shows a percentage",
                "status_ring",
            )
        )
        look.add(
            self._switch(
                "Ambient artwork tint",
                "Tint the island surface with the current accent/artwork",
                "ambient_tint",
            )
        )
        look.add(
            self._switch(
                "Queue peek alerts",
                "Show alerts one after another instead of replacing the current one",
                "activity_queue",
            )
        )
        page.add(look)

        behavior = Adw.PreferencesGroup(title="Behavior")
        click = Adw.ComboRow(
            title="Left click on the pill",
            subtitle="Wake pops up on 1st click & opens card on 2nd; right click toggles card; horizontal drag >56px switches faces",
        )
        actions = [
            ("wake", "Wake & pop up (AI wobble)"),
            ("expand", "Open the card"),
            ("cycle", "Cycle faces"),
        ]
        click.set_model(Gtk.StringList.new([a[1] for a in actions]))
        action_keys = [a[0] for a in actions]
        cur_action = self.cfg.get("click_action", "wake")
        click.set_selected(
            action_keys.index(cur_action) if cur_action in action_keys else 0
        )
        click.connect(
            "notify::selected",
            lambda r, _p: (
                self.cfg.__setitem__("click_action", actions[r.get_selected()][0]),
                self._apply(),
            ),
        )
        behavior.add(click)
        peek = Adw.SpinRow.new_with_range(0.8, 6.0, 0.2)
        peek.set_digits(1)
        peek.set_title("Peek duration")
        peek.set_subtitle("Seconds a volume/notification peek stays up")
        peek.set_value(self.cfg.get("peek_seconds", 2.2))
        peek.connect(
            "notify::value",
            lambda r, _p: (
                self.cfg.__setitem__("peek_seconds", round(r.get_value(), 1)),
                self._apply(),
            ),
        )
        behavior.add(peek)
        behavior.add(
            self._switch(
                "Automatic focus and breaks",
                "Cycle 25-minute focus rounds, 5-minute breaks, and a 15-minute break every fourth round",
                "pomodoro_auto_cycle",
            )
        )
        behavior.add(
            self._switch(
                "Enable DND during Focus sessions",
                "Suppress non-essential notification peeks while a focus session runs",
                "focus_auto_dnd",
            )
        )
        page.add(behavior)

        faces = Adw.PreferencesGroup(
            title="Pill faces",
            description="Faces in the click/swipe cycle. The order is fixed; "
            "a face with nothing to show falls back to the clock",
        )
        enabled = self.cfg.get("pill_faces", [])
        for key, title, subtitle in FACES:
            row = Adw.SwitchRow(use_markup=False)
            row.set_title(title)
            row.set_subtitle(subtitle)
            row.set_active(key in enabled)
            row.connect("notify::active", lambda r, _p, k=key: self._toggle_face(k, r))
            faces.add(row)
        page.add(faces)
        return page

    def _toggle_face(self, key: str, row):
        current = [f for f in self.cfg.get("pill_faces", []) if f != key]
        if not row.get_active() and not current:
            row.set_active(True)
            return
        if row.get_active():
            order = [f[0] for f in FACES]
            current.append(key)
            current.sort(key=order.index)
        if not current:
            current = ["auto"]
        self.cfg["pill_faces"] = current
        if self.cfg.get("pill_face") not in current:
            self.cfg["pill_face"] = current[0]
        self._apply()

    def _modules_page(self):
        page = Adw.PreferencesPage(title="Modules", icon_name="view-grid-symbolic")
        group = Adw.PreferencesGroup(
            title="Modules",
            description="Turning a module off removes its widgets, peeks "
            "and background listeners entirely",
        )
        for key, title, subtitle in MODULES:
            group.add(self._switch(title, subtitle, key, structural=True, mod=True))
        page.add(group)

        notifications = Adw.PreferencesGroup(title="Notifications")
        notifications.add(
            self._switch(
                "Do Not Disturb",
                "Keep notifications in history without showing them as peeks",
                "do_not_disturb",
                structural=True,
            )
        )
        notifications.add(
            self._entry(
                "Blocked application names",
                "notification_blocklist",
                structural=True,
                csv_list=True,
            )
        )
        page.add(notifications)

        weather = Adw.PreferencesGroup(title="Weather")
        weather.add(
            self._entry(
                "Location (empty = automatic)",
                "weather_city",
                structural=False,
            )
        )
        page.add(weather)
        return page

    def _integrations_page(self):
        page = Adw.PreferencesPage(
            title="Integrations", icon_name="emblem-system-symbolic"
        )

        ai = Adw.PreferencesGroup(
            title="AI Assistant",
            description="Local Ollama/LM Studio or optional remote API credentials",
        )
        ai.add(
            self._combo(
                "Provider",
                "Backend used by the AI Assistant panel",
                "ai_provider",
                [
                    ("ollama", "Ollama (local)"),
                    ("lmstudio", "LM Studio (local)"),
                    ("claude", "Anthropic Claude API"),
                    ("gemini", "Google Gemini API"),
                    ("openrouter", "OpenRouter API"),
                ],
                structural=True,
            )
        )
        ai.add(
            self._switch(
                "System info & control",
                "Off by default. AI gets live device context and control tools when enabled; "
                "online providers may receive that context.",
                "ai_system_control",
            )
        )
        ai.add(self._entry("Model override (optional)", "ai_model", structural=True))
        ai.add(
            self._entry(
                "API key (remote providers only)",
                "ai_api_key",
                structural=True,
                secret=True,
            )
        )
        page.add(ai)

        cal = Adw.PreferencesGroup(
            title="Calendar & Agent Hooks",
            description="Auto-discovers local Evolution/GNOME calendars if empty",
        )
        cal.add(
            self._entry(
                "ICS paths / feeds (comma-separated)",
                "calendar_ics",
                structural=True,
                csv_list=True,
            )
        )
        hook_row = Adw.ActionRow(
            title="Coding-agent webhook / CLI",
            subtitle='lost-island --agent "Claude Code:attention:Needs approval"',
        )
        cal.add(hook_row)
        page.add(cal)
        return page
