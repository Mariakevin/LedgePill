import io
import json
import os
import subprocess
import tempfile
import threading
import time
import types
import unittest
import urllib.error
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import GdkPixbuf, GLib, Gtk

from ledgepill.app import _needs_audio_service, _should_suppress_gdk_log
from ledgepill.services.audio import AudioService, _parse_default_source_state
from ledgepill.services.bluetooth import (
    ADAPTER_IFACE,
    BATTERY_IFACE,
    DEVICE_IFACE,
    BluetoothService,
)
from ledgepill.services.brightness import BrightnessService
from ledgepill.services.clipboard import ClipboardService
from ledgepill.services.desktop import CursorWatcher, DesktopService
from ledgepill.services.feeds import FeedService, _clean_text, _SafeRedirectHandler
from ledgepill.services.lyrics import LyricsService
from ledgepill.services.mpris import (
    MprisService,
    detect_desktop_accent,
    extract_accent_color,
)
from ledgepill.services.network import NetworkService
from ledgepill.services.notify import NotifyService
from ledgepill.services.ollama import OllamaService, _lmstudio_url, _ollama_url
from ledgepill.services.power import PowerService
from ledgepill.services.privacy import PrivacyService
from ledgepill.services.productivity import ProductivityService
from ledgepill.services.system import SystemService
from ledgepill.services.system_agent import SystemAgent
from ledgepill.services.weather import WeatherService, _icon_for_code
from ledgepill.ui.draw import brightness_icon, volume_icon
from ledgepill.ui.expanded import (
    Expanded,
    _hex_rgb,
    classify_clipboard_text,
)
from ledgepill.ui.island import Island


class ServicesTest(unittest.TestCase):
    def test_audio_service_is_kept_for_all_enabled_consumers(self):
        self.assertTrue(
            _needs_audio_service(
                {"volume_osd": False, "privacy": True, "toggles": False}, False
            )
        )
        self.assertTrue(
            _needs_audio_service(
                {
                    "volume_osd": False,
                    "privacy": False,
                    "toggles": False,
                    "assistant": True,
                    "ollama": False,
                },
                True,
            )
        )
        self.assertFalse(
            _needs_audio_service(
                {
                    "volume_osd": False,
                    "privacy": False,
                    "toggles": False,
                    "assistant": False,
                    "ollama": False,
                },
                True,
            )
        )

    def test_default_source_mute_parsing(self):
        source_data = json.dumps(
            [
                {
                    "name": "mic-a",
                    "mute": True,
                    "properties": {"api.alsa.pcm.card": "1"},
                },
                {
                    "name": "mic-b",
                    "mute": False,
                    "properties": {},
                },
            ]
        )
        self.assertTrue(_parse_default_source_state(source_data, "mic-a"))
        self.assertFalse(_parse_default_source_state(source_data, "mic-b"))
        self.assertIsNone(_parse_default_source_state(source_data, "missing"))
        self.assertIsNone(_parse_default_source_state("{}", "mic-a"))

    def test_brightness_parser_edge_cases(self):
        self.assertEqual(BrightnessService.parse_percent(None), 0)
        self.assertEqual(BrightnessService.parse_percent(""), 0)
        self.assertEqual(BrightnessService.parse_percent("120%"), 100)
        self.assertEqual(BrightnessService.parse_percent("300/1023"), 0)

    def test_expanded_footer_has_no_opacity_control(self):
        ui = Expanded(
            {"modules": {}},
            media=types.SimpleNamespace(active=None),
            power=types.SimpleNamespace(available=False),
        )

        self.assertFalse(hasattr(ui, "opacity_btn"))

    def test_audio_and_brightness_rows_are_read_only_indicators(self):
        cfg = {
            "modules": {
                "toggles": False,
                "assistant": False,
                "ollama": False,
                "bluetooth": False,
                "camera_mirror": False,
            }
        }
        ui = Expanded(
            cfg,
            media=types.SimpleNamespace(active=None),
            power=types.SimpleNamespace(available=False),
            audio=types.SimpleNamespace(percent=50, muted=False, mic_muted=False),
            brightness=types.SimpleNamespace(available=True, percent=50),
        )

        self.assertFalse(hasattr(ui, "vol_slider"))
        self.assertFalse(hasattr(ui, "vol_mute_btn"))
        self.assertFalse(hasattr(ui, "brightness_slider"))
        ui.refresh_volume(62, False)
        ui.refresh_mic(True)
        ui.refresh_brightness(35)
        self.assertEqual(ui.vol_pct_lbl.get_label(), "62%")
        self.assertEqual(ui.mic_lbl.get_label(), "Microphone muted")
        self.assertEqual(ui.brightness_pct_lbl.get_label(), "35%")

    def test_brightness_kde_screen_probe_unpacks_display_names(self):
        with mock.patch(
            "gi.repository.Gio.bus_get_sync", side_effect=GLib.Error("no bus")
        ):
            svc = BrightnessService()

        properties = GLib.Variant(
            "(a{sv})",
            (
                {
                    "MaxBrightness": GLib.Variant("i", 100),
                    "Brightness": GLib.Variant("i", 45),
                    "IsInternal": GLib.Variant("b", True),
                },
            ),
        )
        with mock.patch.object(
            svc,
            "_call_sync",
            side_effect=[
                GLib.Variant("(v)", (GLib.Variant("as", ["display0"]),)),
                properties,
            ],
        ):
            self.assertTrue(svc._probe_kde_screen_api())

        self.assertEqual(svc._kde_display, "display0")
        self.assertEqual(svc._kde_max, 100)

    def test_network_initial_sync_does_not_peek(self):
        with mock.patch(
            "gi.repository.Gio.bus_get_sync", side_effect=GLib.Error("no bus")
        ):
            svc = NetworkService()
        synced = []
        changed = []
        svc.connect("synced", lambda *_: synced.append(True))
        svc.connect("changed", lambda _s, n, k, c: changed.append((n, k, c)))

        svc._update("HomeNet", "802-11-wireless", True)
        self.assertEqual(len(synced), 1)
        self.assertEqual(changed, [])

        svc._update("WorkNet", "802-11-wireless", True)
        self.assertEqual(changed, [("WorkNet", "802-11-wireless", True)])

    def test_network_wifi_signal_clamp_and_dedup(self):
        with mock.patch(
            "gi.repository.Gio.bus_get_sync", side_effect=GLib.Error("no bus")
        ):
            svc = NetworkService()
        signals = []
        svc.connect("wifi-quality", lambda _s, q: signals.append(q))
        svc._set_wifi_signal(150)
        svc._set_wifi_signal(100)
        svc._set_wifi_signal(-5)
        self.assertEqual(signals, [100, 0])

    def test_bluetooth_sync_and_prefers_battery_device(self):
        with mock.patch(
            "gi.repository.Gio.bus_get_sync", side_effect=GLib.Error("no bus")
        ):
            svc = BluetoothService()
        changed = []
        svc.connect("changed", lambda _s, d, c, b: changed.append((d, c, b)))

        svc._ingest_interfaces(
            "/org/bluez/hci0",
            {ADAPTER_IFACE: {"Powered": True}},
        )
        svc._ingest_interfaces(
            "/org/bluez/hci0/dev_11",
            {DEVICE_IFACE: {"Alias": "Mouse", "Connected": True}},
        )
        svc._ingest_interfaces(
            "/org/bluez/hci0/dev_AA",
            {
                DEVICE_IFACE: {"Alias": "Buds", "Connected": True},
                BATTERY_IFACE: {"Percentage": 80},
            },
        )
        svc._elect()
        svc._initial = False
        self.assertEqual(changed, [])
        self.assertEqual((svc.device, svc.connected, svc.battery), ("Buds", True, 80))

        # Disconnect Buds -> falls back to Mouse
        fake_params = types.SimpleNamespace(
            unpack=lambda: (DEVICE_IFACE, {"Connected": False}, [])
        )
        svc._on_props(None, None, "/org/bluez/hci0/dev_AA", None, None, fake_params)
        self.assertEqual(changed, [("Mouse", True, -1)])

    def test_mpris_deduplicates_unchanged_and_uses_owner_cache(self):
        with mock.patch(
            "gi.repository.Gio.bus_get_sync", side_effect=GLib.Error("no bus")
        ):
            svc = MprisService()
        svc._bus = mock.MagicMock()
        svc._add_player("org.mpris.MediaPlayer2.demo")
        svc._owners[":1.99"] = "org.mpris.MediaPlayer2.demo"

        changes = []
        svc.connect(
            "changed",
            lambda *_: changes.append(svc.active.title if svc.active else None),
        )

        # Unrelated Volume property change should be ignored without D-Bus lookup
        vol_params = types.SimpleNamespace(
            unpack=lambda: ("org.mpris.MediaPlayer2.Player", {"Volume": 0.5}, [])
        )
        svc._on_props_changed(None, ":1.99", None, None, None, vol_params)
        self.assertEqual(changes, [])

        # Metadata + PlaybackStatus updates via owner cache
        play_params = types.SimpleNamespace(
            unpack=lambda: (
                "org.mpris.MediaPlayer2.Player",
                {
                    "PlaybackStatus": "Playing",
                    "Metadata": {
                        "xesam:title": "Song A",
                        "xesam:artist": ["", "Artist A"],
                    },
                },
                [],
            )
        )
        svc._on_props_changed(None, ":1.99", None, None, None, play_params)
        self.assertEqual(changes, ["Song A"])
        self.assertEqual(svc.active.artist, "Artist A")

        # Duplicate status/metadata does not re-emit
        svc._on_props_changed(None, ":1.99", None, None, None, play_params)
        self.assertEqual(changes, ["Song A"])

    def test_ollama_fetch_apply_and_host_normalization(self):
        with mock.patch.dict(os.environ, {"OLLAMA_HOST": "127.0.0.1:11435/"}):
            self.assertEqual(_ollama_url(), "http://127.0.0.1:11435")
        with mock.patch.dict(os.environ, {"LMSTUDIO_HOST": "http://127.0.0.1:1245/"}):
            self.assertEqual(_lmstudio_url(), "http://127.0.0.1:1245")

        svc = OllamaService()
        svc._apply(
            True, ["llama3:latest"], ["llama3:latest"], "active", "llama3:latest"
        )
        self.assertTrue(svc.available)
        self.assertEqual(svc.status, "active")
        self.assertEqual(svc.model, "llama3:latest")
        self.assertFalse(svc._busy)

        # Stale generation after stop() is ignored
        svc.stop()
        svc._apply(False, [], [], "offline", "", gen=0)
        self.assertTrue(svc.available)

        # Error during _get_json resets busy safely
        svc._busy = True
        with (
            mock.patch.object(svc, "_get_json", side_effect=ValueError("bad json")),
            mock.patch(
                "gi.repository.GLib.idle_add", side_effect=lambda fn, *a: fn(*a)
            ),
        ):
            svc._fetch(svc._gen)
        self.assertFalse(svc._busy)
        self.assertFalse(svc.available)
        self.assertEqual(svc.status, "offline")

        # LM Studio discovery is independent of whether Ollama is installed.
        def local_response(url):
            if "/api/" in url:
                raise urllib.error.URLError("Ollama is offline")
            return {"data": [{"id": "local/qwen"}]}

        with (
            mock.patch.object(svc, "_get_json", side_effect=local_response),
            mock.patch(
                "gi.repository.GLib.idle_add", side_effect=lambda fn, *a: fn(*a)
            ),
        ):
            svc._fetch(svc._gen)
        self.assertFalse(svc.available)
        self.assertTrue(svc.lmstudio_available)
        self.assertEqual(svc.lmstudio_models, ["local/qwen"])

    def test_system_rocm_nvidia_and_baseline_prime(self):
        svc = SystemService()
        svc._nvidia_smi = None
        svc._rocm_smi = "/usr/bin/rocm-smi"

        fake_out = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="GPU[1] : GPU use (%): 15\nGPU[1] : Temperature (Sensor edge) (C): 45.0\n",
            stderr="",
        )
        with (
            mock.patch("subprocess.run", return_value=fake_out),
            mock.patch(
                "gi.repository.GLib.idle_add", side_effect=lambda fn, *a: fn(*a)
            ),
        ):
            svc._poll_gpu()
        self.assertEqual(svc._gpu_util, 15)
        self.assertEqual(svc._gpu_temp, 45.0)

        # Empty nvidia-smi output does not crash
        svc._nvidia_smi = "/usr/bin/nvidia-smi"
        svc._rocm_smi = None
        empty_out = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with (
            mock.patch("subprocess.run", return_value=empty_out),
            mock.patch(
                "gi.repository.GLib.idle_add", side_effect=lambda fn, *a: fn(*a)
            ),
        ):
            svc._poll_gpu()
        self.assertIsNone(svc._gpu_util)

        # Baseline sample with emit=False does not emit
        emitted = []
        svc.connect("changed", lambda _s, d: emitted.append(d))
        svc._nvidia_smi = None
        svc._sample(emit=False)
        self.assertEqual(emitted, [])

    def test_weather_icon_for_code_handles_invalid(self):
        self.assertEqual(_icon_for_code("113"), "weather-clear-symbolic")
        self.assertEqual(_icon_for_code("200"), "weather-storm-symbolic")
        self.assertEqual(_icon_for_code("330"), "weather-snow-symbolic")
        self.assertEqual(_icon_for_code("296"), "weather-showers-symbolic")
        self.assertEqual(_icon_for_code("invalid"), "weather-few-clouds-symbolic")
        self.assertEqual(_icon_for_code(None), "weather-few-clouds-symbolic")

    def test_lyrics_line_lookup(self):
        svc = LyricsService()
        svc.times = [5.0, 10.0, 15.0]
        svc.lines = ["First", "Second", "Third"]
        self.assertEqual(svc.line_at(2.0), "…")
        self.assertEqual(svc.line_at(5.0), "First")
        self.assertEqual(svc.line_at(12.0), "Second")

    def test_clipboard_init_safe_without_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "clipboard.json")
            with mock.patch("gi.repository.Gdk.Display.get_default", return_value=None):
                svc = ClipboardService(storage_path=path)
            self.assertEqual(svc.history, [])
            svc.stop()

    def test_draw_icon_helpers_and_css_parse(self):
        self.assertEqual(volume_icon(50, True), "audio-volume-muted-symbolic")
        self.assertEqual(volume_icon(10, False), "audio-volume-low-symbolic")
        self.assertEqual(volume_icon(50, False), "audio-volume-medium-symbolic")
        self.assertEqual(volume_icon(90, False), "audio-volume-high-symbolic")
        self.assertEqual(brightness_icon(5), "display-brightness-off-symbolic")
        self.assertEqual(brightness_icon(25), "display-brightness-low-symbolic")
        self.assertEqual(brightness_icon(50), "display-brightness-medium-symbolic")
        self.assertEqual(brightness_icon(90), "display-brightness-high-symbolic")

        self.assertEqual(_hex_rgb("#ffffff"), (1.0, 1.0, 1.0))
        self.assertEqual(_hex_rgb("#fff"), (1.0, 1.0, 1.0))
        self.assertEqual(_hex_rgb("not-a-color"), (1.0, 0.62, 0.04))

        css_path = os.path.join(
            os.path.dirname(__file__), "..", "ledgepill", "ui", "style.css"
        )
        with open(css_path) as f:
            css = (
                "@define-color accent #ff9f0a;\n"
                "@define-color island_bg rgba(12, 12, 14, 0.97);\n" + f.read()
            )
        provider = Gtk.CssProvider()
        errs = []
        provider.connect("parsing-error", lambda _p, _s, err: errs.append(err.message))
        provider.load_from_string(css)
        self.assertEqual(errs, [])

    def test_feeds_parsing_cleaning_and_redirect_guard(self):
        self.assertEqual(
            _clean_text("<p>Hello &amp; <b>world</b></p>"), "Hello & world"
        )
        rss = b"""<rss><channel><item><title>&lt;b&gt;News&lt;/b&gt;</title>
        <description><p>Summary &amp; text</p></description>
        <link>https://example.com/1</link></item></channel></rss>"""
        items = FeedService._rss_items(rss, "Tech")
        self.assertEqual(items[0]["title"], "News")
        self.assertEqual(items[0]["summary"], "Summary & text")
        self.assertEqual(items[0]["source"], "Tech")

        js = json.dumps(
            {
                "items": [
                    {
                        "title": "Release 2.0",
                        "summary": "<i>Fast</i>",
                        "url": "https://x",
                    }
                ]
            }
        ).encode()
        jitems = FeedService._json_items(js, "JSONSrc")
        self.assertEqual(jitems[0]["summary"], "Fast")
        self.assertEqual(jitems[0]["source"], "JSONSrc")

        handler = _SafeRedirectHandler()
        req = mock.MagicMock()
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(req, None, 302, "Found", {}, "file:///etc/passwd")

        # TTL guard avoids rapid duplicate fetches in start()
        svc = FeedService([{"url": "https://example.com/rss", "name": "Ex"}])
        svc._last_fetch = time.monotonic()
        with mock.patch.object(svc, "refresh_now") as m_ref:
            svc.start()
            svc.stop()
        m_ref.assert_not_called()

    def test_auto_desktop_accent_detection_and_log_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            kde_cfg = os.path.join(tmp, "kdeglobals")
            with open(kde_cfg, "w", encoding="utf-8") as f:
                f.write("[General]\nAccentColor=52,211,153\n")
            self.assertEqual(
                detect_desktop_accent(kdeglobals_path=kde_cfg, use_portal=False),
                "#34d399",
            )

            # Desaturated grey AccentColor falls back to vibrant Plasma wallpaper color
            with open(kde_cfg, "w", encoding="utf-8") as f:
                f.write("[General]\nAccentColor=128,128,128\n")
            wp = os.path.join(tmp, "wall.png")
            pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 8, 8)
            pb.fill(0x1E90FFFF)
            pb.savev(wp, "png", [], [])
            appletsrc = os.path.join(tmp, "appletsrc")
            with open(appletsrc, "w", encoding="utf-8") as f:
                f.write(f"[Wallpaper]\nImage=file://{wp}\n")
            self.assertEqual(
                detect_desktop_accent(
                    kdeglobals_path=kde_cfg,
                    appletsrc_path=appletsrc,
                    use_portal=False,
                ),
                "#1e90ff",
            )

        self.assertTrue(
            _should_suppress_gdk_log(
                "Gdk",
                "Duplicate call to gdk_frame_timings_presented() with different values: presentation time is 16.668ms off from expected",
            )
        )
        self.assertTrue(
            _should_suppress_gdk_log(
                "Gdk",
                "gdk_frame_timings_presented() called on discarded frame.",
            )
        )
        self.assertFalse(_should_suppress_gdk_log("Gdk", "Unrelated Gdk warning"))
        self.assertFalse(
            _should_suppress_gdk_log(
                "Gtk", "Duplicate call to gdk_frame_timings_presented()"
            )
        )

    def test_notify_rules_dnd_calls_and_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            hpath = os.path.join(tmp, "notif.json")
            svc = NotifyService(
                blocked_apps=["NoiseApp"],
                do_not_disturb=True,
                history_path=hpath,
                rules=[r"regex:spam\s+\d+", r"/promo-\d+/"],
            )
            peeks, calls, hist_events = [], [], []
            svc.connect("notified", lambda _s, a, su, b, ic: peeks.append((a, su)))
            svc.connect("call", lambda _s, a, su, b, ic: calls.append((a, su, b, ic)))
            svc.connect("history-changed", lambda *_: hist_events.append(True))

            # Filtered by blocklist and rule
            svc._emit("NoiseApp", "Hi", "body", "")
            svc._emit("Mail", "spam 99", "offer", "")
            svc._emit("Mail", "promo-12", "discount", "")
            self.assertEqual(svc.history, [])

            # Suppressed peek in DND, saved in history
            svc._emit("Slack", "Deploy", "Live", "", category="im.received")
            self.assertEqual(peeks, [])
            self.assertEqual(len(svc.history), 1)

            # Incoming call rings through DND via "call" signal with 4 args
            svc._emit(
                "Discord",
                "Incoming Call",
                "Alice",
                "call-icon",
                category="x-gnome.incoming-call",
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1], "Incoming Call")

            # Push custom + dismiss + clear when DND off
            svc.set_dnd(False)
            svc.push_custom("Script", "Done", "Body")
            self.assertEqual(len(peeks), 1)
            svc.dismiss(0)
            self.assertEqual(len(svc.history), 2)
            svc.clear()
            self.assertEqual(svc.history, [])
            svc.stop()

    def test_clipboard_search_remove_pin_persist_and_self_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            cpath = os.path.join(tmp, "clipboard.json")
            with mock.patch("gi.repository.Gdk.Display.get_default", return_value=None):
                svc = ClipboardService(storage_path=cpath)
            svc._record("alpha line")
            svc._record("beta line")
            svc._record("alpha line")  # moves to top
            self.assertEqual(svc.history, ["alpha line", "beta line"])
            self.assertEqual(svc.search("beta"), [(1, "beta line")])

            # Pinning protects item on partial clear and keeps pinned at top
            self.assertTrue(svc.toggle_pin(1))
            self.assertEqual(svc.pinned, ["beta line"])
            self.assertEqual(svc.history, ["beta line", "alpha line"])
            svc.clear(include_pinned=False)
            self.assertEqual(svc.history, ["beta line"])

            # Persistence & permissions
            svc.stop()
            self.assertEqual(os.stat(cpath).st_mode & 0o777, 0o600)
            with mock.patch("gi.repository.Gdk.Display.get_default", return_value=None):
                svc2 = ClipboardService(storage_path=cpath)
            self.assertEqual(svc2.history, ["beta line"])
            self.assertEqual(svc2.pinned, ["beta line"])

            # Self-copy sets ignore window so immediate echo isn't re-recorded
            svc2._record("gamma item")
            with mock.patch.object(svc2, "emit") as m_emit:
                svc2.copy_text("gamma item")
                svc2._record("gamma item")
            m_emit.assert_not_called()

            # Removing unpinned/pinned cleans both
            svc2.remove(0)
            self.assertEqual(svc2.pinned, [])
            svc2.stop()

    def test_clipboard_clear_and_remove_suppress_external_reingest(self):
        with tempfile.TemporaryDirectory() as tmp:
            cpath = os.path.join(tmp, "clipboard.json")
            with (
                mock.patch("gi.repository.Gdk.Display.get_default", return_value=None),
                mock.patch(
                    "gi.repository.GLib.find_program_in_path", return_value="wl-copy"
                ),
            ):
                svc = ClipboardService(storage_path=cpath)
            svc._record("first item")
            svc._record("second item")
            fake_wl = mock.MagicMock(returncode=0)

            # Removing top item clears OS clipboard & suppresses stale wl-paste
            with mock.patch(
                "gi.repository.Gio.Subprocess.new", return_value=fake_wl
            ) as m_sub:
                svc.remove(0)
            m_sub.assert_called_with(
                ["wl-copy", "--clear"],
                mock.ANY,
            )
            done_second = mock.MagicMock()
            done_second.get_successful.return_value = True
            done_second.communicate_utf8_finish.return_value = (True, "second item", "")
            svc._on_wl_paste(done_second, None)
            self.assertEqual(svc.history, ["first item"])

            # Clear empties history and persists suppression across reload on reopen
            with mock.patch("gi.repository.Gio.Subprocess.new", return_value=fake_wl):
                svc.clear(include_pinned=True)
            self.assertEqual(svc.history, [])
            svc._on_wl_paste(done_second, None)
            done_first = mock.MagicMock()
            done_first.get_successful.return_value = True
            done_first.communicate_utf8_finish.return_value = (True, "first item", "")
            svc._on_wl_paste(done_first, None)
            self.assertEqual(svc.history, [])

            with mock.patch("gi.repository.Gdk.Display.get_default", return_value=None):
                svc_reopened = ClipboardService(storage_path=cpath)
            svc_reopened._on_wl_paste(done_first, None)
            self.assertEqual(svc_reopened.history, [])

            # Genuinely new copied text records normally and resets suppression
            done_fresh = mock.MagicMock()
            done_fresh.get_successful.return_value = True
            done_fresh.communicate_utf8_finish.return_value = (
                True,
                "brand new text",
                "",
            )
            svc_reopened._on_wl_paste(done_fresh, None)
            self.assertEqual(svc_reopened.history, ["brand new text"])
            svc_reopened.stop()

    def test_audio_sinks_and_sink_inputs_json_parsing(self):
        with mock.patch(
            "gi.repository.Gio.Subprocess.new", side_effect=GLib.Error("no pactl")
        ):
            svc = AudioService()
        svc.default_sink = "alsa_out"
        devs_changed = []
        svc.connect("devices-changed", lambda *_: devs_changed.append(True))

        fake_sinks_proc = mock.MagicMock()
        fake_sinks_proc.communicate_utf8_finish.return_value = (
            True,
            json.dumps([{"name": "alsa_out", "description": "Built-in Audio"}]),
            "",
        )
        with mock.patch(
            "gi.repository.Gio.Subprocess.new", side_effect=GLib.Error("skip inputs")
        ):
            svc._on_sinks_json(fake_sinks_proc, None, svc._dev_seq)
        self.assertEqual(svc.sinks[0]["default"], True)
        self.assertEqual(len(devs_changed), 1)

        fake_inputs_proc = mock.MagicMock()
        fake_inputs_proc.communicate_utf8_finish.return_value = (
            True,
            json.dumps(
                [
                    {
                        "index": 42,
                        "mute": False,
                        "properties": {"application.name": "Firefox"},
                        "volume": {"front-left": {"value_percent": "65%"}},
                    }
                ]
            ),
            "",
        )
        svc._on_sink_inputs_json(fake_inputs_proc, None, svc._dev_seq)
        self.assertEqual(svc.sink_inputs[0]["id"], 42)
        self.assertEqual(svc.sink_inputs[0]["volume"], 65)
        self.assertEqual(svc.sink_inputs[0]["name"], "Firefox")

        # Non-ASCII names containing 0xc2 UTF-8 sequences fall back to plain pactl text
        bad_json_proc = mock.MagicMock()
        bad_json_proc.communicate_utf8_finish.return_value = (True, "", "")
        plain_text = (
            "Sink Input #77\n"
            "\tMute: no\n"
            "\tVolume: front-left: 52428 / 80%\n"
            "\tProperties:\n"
            '\t\tmedia.name = "Lo-Fi · Beats ®"\n'
            '\t\tapplication.name = "Anker Soundcore ®"\n'
        )
        plain_proc = mock.MagicMock()
        plain_proc.communicate_utf8_finish.return_value = (True, plain_text, "")
        with mock.patch("gi.repository.Gio.Subprocess.new", return_value=plain_proc):
            svc._on_sink_inputs_json(bad_json_proc, None, svc._dev_seq)
        plain_proc.communicate_utf8_async.call_args.args[2](plain_proc, None)
        self.assertEqual(svc.sink_inputs[0]["id"], 77)
        self.assertEqual(svc.sink_inputs[0]["volume"], 80)
        self.assertEqual(svc.sink_inputs[0]["name"], "Anker Soundcore ®")
        svc.stop()

    def test_mpris_extract_accent_color(self):
        pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 8, 8)
        pb.fill(0xE05030FF)  # bright orange-red
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        try:
            pb.savev(path, "png", [], [])
            color = extract_accent_color(path)
            self.assertIsNotNone(color)
            self.assertTrue(color.startswith("#"))
            self.assertIsNone(extract_accent_color("/nonexistent/cover.png"))
        finally:
            os.unlink(path)

    def test_power_alerts_and_extra_batteries(self):
        with mock.patch(
            "gi.repository.Gio.bus_get_sync", side_effect=GLib.Error("no bus")
        ):
            svc = PowerService()
        alerts = []
        svc.connect("power-alert", lambda _s, sev, msg: alerts.append((sev, msg)))
        svc.available = True
        svc.state = 2  # DISCHARGING
        svc.percentage = 19.0
        svc._apply({"Percentage": 19.0, "State": 2}, initial=False)
        svc._apply({"Percentage": 9.0, "State": 2}, initial=False)
        self.assertEqual([a[0] for a in alerts], ["low", "critical"])

        fake_bus = mock.MagicMock()
        fake_bus.call_finish.return_value = types.SimpleNamespace(
            unpack=lambda: ({"IsPresent": True, "Model": "Earbuds", "Percentage": 78},)
        )
        svc._on_extra_device(fake_bus, None, "/org/upower/buds")
        self.assertEqual(svc.extra_batteries[0]["model"], "Earbuds")
        self.assertEqual(svc.extra_batteries[0]["percent"], 78)
        svc.stop()

    def test_system_fps_and_hwmon(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("144\n")
            fps_path = f.name
        try:
            with mock.patch.dict(os.environ, {"LOST_ISLAND_FPS_FILE": fps_path}):
                svc = SystemService()
                self.assertEqual(svc._read_fps(), 144)
                svc.stop()
        finally:
            os.unlink(fps_path)

    def test_privacy_service_signals_and_pipewire(self):
        audio = mock.MagicMock()
        audio.mic_in_use = False
        svc = PrivacyService(audio=audio)
        alerts = []
        svc.connect("alert", lambda _s, kind, lbl: alerts.append((kind, lbl)))
        svc._on_mic_activity(audio, True)
        self.assertTrue(svc.mic_active)

        fake_proc = mock.MagicMock()
        fake_proc.communicate_utf8_finish.return_value = (
            True,
            json.dumps(
                [
                    {
                        "info": {
                            "state": "running",
                            "props": {
                                "media.class": "Video/Source",
                                "node.name": "xdg-desktop-portal-screencast",
                                "application.name": "OBS",
                            },
                        }
                    }
                ]
            ),
            "",
        )
        svc._on_pw_dump(fake_proc, None)
        self.assertTrue(svc.screen_sharing)
        self.assertEqual([a[0] for a in alerts], ["mic", "screenshare"])
        svc.stop()

    def test_desktop_service_poll_apply_and_signals(self):
        with mock.patch(
            "gi.repository.Gio.bus_get_sync", side_effect=GLib.Error("no bus")
        ):
            svc = DesktopService()
        fs_events, lay_events, chg_events = [], [], []
        svc.connect("fullscreen-changed", lambda _s, v: fs_events.append(v))
        svc.connect("layout-changed", lambda _s, l: lay_events.append(l))
        svc.connect("changed", lambda *_: chg_events.append(True))
        svc._apply_poll(
            [{"id": "1", "name": "1", "active": True}],
            [{"id": "0x1", "title": "Term", "app": "kitty", "active": True}],
            True,
            "us",
            "",
            "",
        )
        svc._apply_poll(
            [{"id": "1", "name": "1", "active": True}],
            [],
            False,
            "fr",
            "pacman running",
            "Cargo build running",
        )
        # Identical poll does not re-emit "changed"
        svc._apply_poll(
            [{"id": "1", "name": "1", "active": True}],
            [],
            False,
            "fr",
            "pacman running",
            "Cargo build running",
        )
        self.assertEqual(len(chg_events), 2)
        self.assertEqual(fs_events, [True, False])
        self.assertEqual(lay_events, ["fr"])
        self.assertEqual(svc.package_activity, "pacman running")
        self.assertEqual(svc.build_activity, "Cargo build running")
        svc.stop()

    def test_cursor_watcher_parsers_and_island_tracking(self):
        self.assertEqual(CursorWatcher.parse_pair("640, 320"), (640.0, 320.0))
        self.assertIsNone(CursorWatcher.parse_pair("bad"))
        self.assertEqual(
            CursorWatcher.parse_hypr_cursor('{"x": 120, "y": 480}'), (120.0, 480.0)
        )
        self.assertEqual(CursorWatcher.parse_hypr_cursor("90, 45"), (90.0, 45.0))
        self.assertEqual(
            CursorWatcher.parse_sway_cursor(
                [{"name": "seat0", "cursor": {"x": 300, "y": 400}}]
            ),
            (300.0, 400.0),
        )
        self.assertEqual(
            CursorWatcher.parse_xdotool_shell("X=512\nY=256\nSCREEN=0\n"),
            (512.0, 256.0),
        )
        # Pill centered horizontally at (1920/2=960), top near y=18+10=28
        nx_c, ny_c = CursorWatcher.normalize_cursor(960, 28, 0, 0, 1920, 1080, 10)
        self.assertAlmostEqual(nx_c, 0.0, places=3)
        self.assertAlmostEqual(ny_c, 0.0, places=3)
        nx_l, ny_d = CursorWatcher.normalize_cursor(0, 1080, 0, 0, 1920, 1080, 0)
        self.assertEqual(nx_l, -1.0)
        self.assertEqual(ny_d, 1.0)

        fake_pill = mock.MagicMock()
        fake_island = types.SimpleNamespace(
            state="pill",
            pill=fake_pill,
            _pointer_inside=False,
            _global_cursor_active=False,
            _collapse_timeout=0,
            expanded=mock.MagicMock(),
            collapse=mock.MagicMock(),
        )
        Island.set_global_cursor_active(fake_island, True)
        Island.set_global_cursor(fake_island, 0.75, 0.5)
        fake_pill.look_at.assert_called_once_with(0.75, 0.5)
        fake_pill.look_at.reset_mock()
        # Leaving pill does not snap eyes to center while global watcher is active
        Island._on_leave(fake_island, None)
        fake_pill.look_at.assert_not_called()
        # Stopping global watcher restores neutral resting gaze
        Island.set_global_cursor_active(fake_island, False)
        fake_pill.look_at.assert_called_once_with(0.0, 0.0)

    def test_clipboard_classification(self):
        self.assertEqual(classify_clipboard_text("https://example.com")[0], "URL")
        self.assertEqual(classify_clipboard_text("/home/user/notes.md")[0], "PATH")
        self.assertEqual(classify_clipboard_text("git status")[0], "CMD")
        self.assertEqual(classify_clipboard_text("def foo():\n    return 1")[0], "CODE")
        self.assertEqual(classify_clipboard_text("hello world"), ("TEXT", "11 chars"))

    def test_weather_cached_request_does_not_reemit_changed(self):
        svc = WeatherService()
        events = []
        svc.connect("changed", lambda *args: events.append(args))
        svc.temp = "21°"
        svc.desc = "Clear"
        svc.icon = "weather-clear-symbolic"
        svc._stamp = time.time()
        self.assertFalse(svc.is_stale())
        svc.request()
        self.assertEqual(events, [])

    def test_privacy_pw_and_fd_camera_do_not_oscillate(self):
        svc = PrivacyService(audio=None)
        alerts = []
        svc.connect("alert", lambda _s, k, l: alerts.append((k, l)))
        fake_proc = mock.MagicMock()
        fake_proc.communicate_utf8_finish.return_value = (
            True,
            json.dumps(
                [
                    {
                        "info": {
                            "state": "running",
                            "props": {
                                "media.class": "Video/Source",
                                "node.name": "v4l2_cam",
                            },
                        }
                    }
                ]
            ),
            "",
        )
        svc._on_pw_dump(fake_proc, None)
        self.assertTrue(svc.camera_active)
        # Subsequent fd scan with _has_pw_dump=True preserves _pw_cam without flapping
        with (
            mock.patch.object(svc, "_scan_camera_fds", return_value=False),
            mock.patch.object(svc, "_scan_recorders", return_value=False),
            mock.patch.object(svc, "_probe_pipewire"),
        ):
            svc.refresh_now()
        self.assertTrue(svc.camera_active)
        self.assertEqual(alerts, [("camera", "Camera active")])
        svc.stop()

    def test_desktop_running_and_background_apps(self):
        with mock.patch(
            "gi.repository.Gio.bus_get_sync", side_effect=GLib.Error("no bus")
        ):
            svc = DesktopService()
        svc._desktop_map = {
            "firefox": "Firefox",
            "spotify": "Spotify",
            "discord": "Discord",
        }
        svc.tray_items = [
            "org.kde.StatusNotifierItem-spotify",
            "org.kde.StatusNotifierItem-205-1/StatusNotifierItem",
        ]

        fake_files = {
            "/proc/101/comm": "firefox\n",
            "/proc/101/statm": "10000 5000 0 0 0 0 0\n",
            "/proc/102/comm": "firefox\n",
            "/proc/102/statm": "10000 3000 0 0 0 0 0\n",
            "/proc/201/comm": "spotify\n",
            "/proc/201/statm": "8000 4000 0 0 0 0 0\n",
            "/proc/205/comm": "Discord\n",
            "/proc/205/statm": "9000 4200 0 0 0 0 0\n",
            "/proc/301/comm": "bash\n",
            "/proc/301/statm": "2000 2000 0 0 0 0 0\n",
            "/proc/302/comm": "randomdaemon\n",
            "/proc/302/statm": "12000 8000 0 0 0 0 0\n",
        }
        uid = os.getuid()
        real_open = open
        real_stat = os.stat

        def fake_open(path, *a, **kw):
            if path in fake_files:
                return io.StringIO(fake_files[path])
            return real_open(path, *a, **kw)

        def fake_stat(path, *a, **kw):
            if str(path).startswith("/proc/"):
                return types.SimpleNamespace(st_uid=uid)
            return real_stat(path, *a, **kw)

        with (
            mock.patch(
                "os.listdir",
                return_value=[
                    "101",
                    "102",
                    "201",
                    "205",
                    "301",
                    "302",
                    str(os.getpid()),
                ],
            ),
            mock.patch("os.stat", side_effect=fake_stat),
            mock.patch("builtins.open", side_effect=fake_open),
        ):
            apps = svc._scan_running_apps(
                windows=[
                    {"id": "0x1", "app": "firefox", "title": "Docs", "active": True}
                ]
            )
        by_key = {a["key"]: a for a in apps}
        # Only background desktop/tray apps appear; open windows & raw daemons are excluded.
        self.assertNotIn("firefox", by_key)
        self.assertNotIn("bash", by_key)
        self.assertNotIn("randomdaemon", by_key)
        self.assertIn("spotify", by_key)
        self.assertIn("discord", by_key)
        self.assertEqual(by_key["spotify"]["pids"], [201])
        self.assertTrue(by_key["spotify"]["background"])
        self.assertTrue(by_key["spotify"]["tray_item"])
        self.assertEqual(
            by_key["discord"]["tray_item"],
            "org.kde.StatusNotifierItem-205-1/StatusNotifierItem",
        )

        svc.running_apps = apps
        self.assertEqual(
            [a["name"] for a in svc.list_running_apps("spot")], ["Spotify"]
        )

    def test_kwin_wayland_windows_and_tray_metadata_background_apps(self):
        with mock.patch(
            "gi.repository.Gio.bus_get_sync", side_effect=GLib.Error("no bus")
        ):
            svc = DesktopService()
        svc._desktop_map = {
            "app.zen_browser.zen": "Zen Browser",
            "zen": "Zen Browser",
            "com.microsoft.vscode": "Visual Studio Code",
            "code": "Visual Studio Code",
        }
        tray_item = ":1.42/org/ayatana/NotificationItem/anker/Menu"
        svc.tray_items = [tray_item]
        svc.tray_meta = {
            tray_item: {
                "pid": 500,
                "id": "anker-games-launcher",
                "title": "Anker Launcher",
            }
        }

        kwin_payload = json.dumps(
            [
                {
                    "id": "{1111}",
                    "pid": 0,
                    "app": "zen",
                    "desktop": "app.zen_browser.zen",
                    "title": "New Tab — Zen Browser",
                    "active": True,
                },
                {
                    "id": "{2222}",
                    "pid": 402,
                    "app": "Code",
                    "desktop": "com.microsoft.VSCode",
                    "title": "expanded.py - ledge-pill - Visual Studio Code",
                    "active": False,
                },
            ]
        )
        with mock.patch.object(svc, "refresh_now"):
            svc._on_kwin_sync_windows(kwin_payload)
        self.assertEqual(len(svc._kwin_windows), 2)

        fake_files = {
            "/proc/401/comm": "zen-bin\n",
            "/proc/401/statm": "20000 12000 0 0 0 0 0\n",
            "/proc/402/comm": "code\n",
            "/proc/402/statm": "18000 9000 0 0 0 0 0\n",
            "/proc/500/comm": "electron\n",
            "/proc/500/statm": "12000 5000 0 0 0 0 0\n",
            "/proc/501/comm": "anker-renderer\n",
            "/proc/501/stat": "501 (anker-renderer) S 500 500 500 0 -1 0 0 0 0 0 0 0\n",
            "/proc/501/statm": "8000 2000 0 0 0 0 0\n",
        }
        uid = os.getuid()
        real_open = open
        real_stat = os.stat

        def fake_open(path, *a, **kw):
            if path in fake_files:
                return io.StringIO(fake_files[path])
            return real_open(path, *a, **kw)

        def fake_stat(path, *a, **kw):
            if str(path).startswith("/proc/"):
                return types.SimpleNamespace(st_uid=uid)
            return real_stat(path, *a, **kw)

        with (
            mock.patch(
                "os.listdir",
                return_value=["401", "402", "500", "501", str(os.getpid())],
            ),
            mock.patch("os.stat", side_effect=fake_stat),
            mock.patch("builtins.open", side_effect=fake_open),
        ):
            apps = svc._scan_running_apps(windows=svc._kwin_windows)

        by_key = {a["key"]: a for a in apps}
        self.assertNotIn("zen browser", by_key)
        self.assertNotIn("visual studio code", by_key)
        self.assertEqual(list(by_key.keys()), ["anker launcher"])
        self.assertEqual(by_key["anker launcher"]["name"], "Anker Launcher")
        self.assertEqual(by_key["anker launcher"]["tray_item"], tray_item)
        self.assertEqual(sorted(by_key["anker launcher"]["pids"]), [500, 501])
        self.assertTrue(by_key["anker launcher"]["background"])

        # Non-tray desktop helpers, compositor/IM tray items, and sandboxed tray apps behave cleanly
        im_tray = ":1.99/StatusNotifierItem"
        svc.tray_items = [tray_item, im_tray]
        svc.tray_meta = {
            tray_item: {
                "pid": 0,
                "id": "anker-games-launcher",
                "title": "Anker Launcher",
                "category": "ApplicationStatus",
            },
            im_tray: {
                "pid": 600,
                "id": "fcitx",
                "title": "Fcitx5",
                "category": "SystemServices",
            },
        }
        with (
            mock.patch("os.listdir", return_value=["401", "402", str(os.getpid())]),
            mock.patch("os.stat", side_effect=fake_stat),
            mock.patch("builtins.open", side_effect=fake_open),
        ):
            tray_only = svc._scan_running_apps(windows=svc._kwin_windows)
        self.assertEqual([a["name"] for a in tray_only], ["Anker Launcher"])
        self.assertEqual(tray_only[0]["tray_item"], tray_item)

        with (
            mock.patch("os.stat", return_value=types.SimpleNamespace(st_uid=uid)),
            mock.patch("os.kill") as m_kill,
        ):
            self.assertFalse(svc.terminate_app([1, os.getpid()]))
            m_kill.assert_not_called()
            self.assertTrue(svc.terminate_app([201]))
            m_kill.assert_called_once()
        svc.stop()

    def test_productivity_ai_history_and_model_resolution(self):
        ollama = mock.MagicMock()
        ollama.running_models = []
        ollama.models = ["qwen2.5-coder:7b", "llama3.1:8b"]
        ollama.lmstudio_models = ["lm-test"]
        cfg = {"ai_provider": "ollama", "ai_model": ""}
        with mock.patch(
            "gi.repository.Gio.bus_get_sync", side_effect=GLib.Error("no bus")
        ):
            svc = ProductivityService(cfg, ollama=ollama)
        self.assertEqual(svc.resolved_ai_model(), "qwen2.5-coder:7b")
        ollama.running_models = ["llama3.1:8b"]
        self.assertEqual(svc.resolved_ai_model(), "llama3.1:8b")
        self.assertEqual(svc.resolved_ai_model("lmstudio"), "lm-test")
        cfg["ai_model"] = "llama3.1:8b"
        self.assertEqual(svc.resolved_ai_model("lmstudio"), "lm-test")
        self.assertEqual(svc.resolved_ai_model("claude"), "claude-sonnet-4-5")
        cfg["ai_model"] = "custom-remote-model"
        self.assertEqual(svc.resolved_ai_model("claude"), "custom-remote-model")

        replies = []
        svc.connect("ai-reply", lambda _s, r, ok: replies.append((r, ok)))
        svc._ai_done("Hello", "Hi there!", True)
        self.assertEqual(svc.last_ai_reply, "Hi there!")
        self.assertEqual(len(svc.ai_history), 2)
        svc._ai_done("Fail", "AI error: boom", False)
        self.assertEqual(len(svc.ai_history), 2)
        svc.clear_ai_history()
        self.assertEqual(svc.ai_history, [])
        self.assertEqual(svc.last_ai_reply, "")
        svc.stop()

    def test_ai_system_context_is_opt_in(self):
        cfg = {"ai_provider": "claude", "ai_system_control": False}
        agent = mock.MagicMock()
        svc = ProductivityService(cfg, system_agent=agent)
        with (
            mock.patch(
                "ledgepill.services.productivity.ProductivityService._dispatch_ai",
                return_value="A concise answer.",
            ) as dispatch,
            mock.patch(
                "ledgepill.services.productivity.GLib.idle_add",
                side_effect=lambda fn, *args: fn(*args),
            ),
        ):
            svc._ai_worker("claude", "claude-sonnet-4-5", "key", "Hello")

        agent.system_prompt.assert_not_called()
        self.assertEqual(
            dispatch.call_args.args[4], [{"role": "user", "content": "Hello"}]
        )
        svc.stop()

    def test_stopped_service_callbacks_ignored(self):
        # OllamaService stop() clears busy wedge and invalidates old generation
        oll = OllamaService()
        oll._busy = True
        oll.stop()
        self.assertFalse(oll._busy)
        self.assertEqual(oll._gen, 1)

        # DesktopService ignores stale poll generation after stop()
        with mock.patch(
            "gi.repository.Gio.bus_get_sync", side_effect=GLib.Error("no bus")
        ):
            desk = DesktopService()
        d_changes = []
        desk.connect("changed", lambda *_: d_changes.append(True))
        desk.stop()
        desk._apply_poll([], [], True, "fr", "pkg", "bld", [], gen=0)
        self.assertEqual(d_changes, [])

        # PrivacyService ignores in-flight pw-dump callback after stop()
        priv = PrivacyService()
        p_alerts = []
        priv.connect("alert", lambda _s, k, l: p_alerts.append((k, l)))
        fake_proc = mock.MagicMock()
        fake_proc.communicate_utf8_finish.return_value = (
            True,
            json.dumps(
                [
                    {
                        "info": {
                            "state": "running",
                            "props": {
                                "media.class": "Video/Source",
                                "node.name": "v4l2_cam",
                            },
                        }
                    }
                ]
            ),
            "",
        )
        priv.stop()
        priv._on_pw_dump(fake_proc, None, gen=0)
        self.assertEqual(p_alerts, [])

        # ProductivityService ignores stale AI reply after stop()
        prod = ProductivityService({})
        ai_replies = []
        prod.connect("ai-reply", lambda *_: ai_replies.append(True))
        prod.stop()
        prod._ai_done("hi", "hello", True, gen=0)
        self.assertEqual(ai_replies, [])

    def test_weather_open_meteo_city_and_ip_geolocation(self):
        def fake_json(url: str, timeout: float = 6.0):
            if "geocoding-api.open-meteo.com" in url:
                return {
                    "results": [
                        {
                            "latitude": 35.6895,
                            "longitude": 139.6917,
                            "name": "Tokyo",
                            "country_code": "JP",
                        }
                    ]
                }
            if "ipwho.is" in url:
                return {"success": False}
            if "ipapi.co" in url:
                return {
                    "latitude": 12.9716,
                    "longitude": 77.5946,
                    "city": "Bengaluru",
                    "country_code": "IN",
                }
            if "api.open-meteo.com/v1/forecast" in url:
                return {
                    "current": {"temperature_2m": 19.4, "weather_code": 0},
                    "daily": {
                        "time": ["2026-11-11", "2026-11-12"],
                        "temperature_2m_min": [14.2, 15.0],
                        "temperature_2m_max": [22.8, 24.1],
                        "weather_code": [0, 61],
                    },
                }
            return None

        with mock.patch(
            "ledgepill.services.weather._http_json", side_effect=fake_json
        ):
            res_city = WeatherService._fetch_open_meteo("Tokyo")
            self.assertIsNotNone(res_city)
            temp, desc, icon, forecast, loc = res_city
            self.assertEqual(
                (temp, desc, icon, loc),
                ("19°", "Clear sky", "weather-clear-symbolic", "Tokyo, JP"),
            )
            self.assertEqual(len(forecast), 2)
            self.assertEqual(forecast[1]["icon"], "weather-showers-symbolic")

            coords = WeatherService._resolve_coords("")
            self.assertEqual(coords, (12.9716, 77.5946, "Bengaluru, IN"))

            svc = WeatherService("")
            svc._stamp = time.time()
            with mock.patch.object(svc, "request") as m_req:
                svc.set_city("Paris")
                self.assertEqual(svc.city, "Paris")
                self.assertEqual(svc._stamp, 0.0)
                m_req.assert_called_once()

    def test_network_wifi_scanner_and_ip_parser(self):
        raw = (
            ":Caf\\:Net:52:WPA2\n"
            "*:HomeWiFi:78:WPA2 WPA3\n"
            ":HomeWiFi:40:WPA2\n"
            ":--:90:WPA2\n"
            ":WeakNet:15:--\n"
        )
        nets = NetworkService.parse_nmcli_wifi(raw)
        self.assertEqual([n["ssid"] for n in nets], ["HomeWiFi", "Caf:Net", "WeakNet"])
        self.assertTrue(nets[0]["active"])
        self.assertEqual(nets[0]["signal"], 78)
        self.assertEqual(nets[2]["security"], "")

        ip = NetworkService.parse_nmcli_ip(
            "GENERAL.TYPE:wifi\nIP4.ADDRESS[1]:127.0.0.1/8\nIP4.ADDRESS[2]:192.168.1.42/24\n"
        )
        self.assertEqual(ip, "192.168.1.42")

    def test_bluetooth_discovery_sorting_and_interfaces_added(self):
        with mock.patch(
            "gi.repository.Gio.bus_get_sync", side_effect=GLib.Error("no bus")
        ):
            svc = BluetoothService()
        svc._adapters["/org/bluez/hci0"] = {"powered": True, "name": "hci0"}
        svc._ingest_interfaces(
            "/org/bluez/hci0/dev_C",
            {
                DEVICE_IFACE: {
                    "Alias": "Zebra Buds",
                    "Connected": False,
                    "Paired": True,
                    "Icon": "audio-headset",
                }
            },
        )
        svc._ingest_interfaces(
            "/org/bluez/hci0/dev_A",
            {
                DEVICE_IFACE: {
                    "Alias": "Alpha Phone",
                    "Connected": False,
                    "Paired": False,
                    "Icon": "phone",
                }
            },
        )
        svc._ingest_interfaces(
            "/org/bluez/hci0/dev_B",
            {
                DEVICE_IFACE: {
                    "Alias": "Sony WH",
                    "Connected": True,
                    "Paired": True,
                    "Icon": "audio-headphones",
                },
                BATTERY_IFACE: {"Percentage": 85},
            },
        )
        ordered = [d["name"] for d in svc.devices()]
        self.assertEqual(ordered, ["Sony WH", "Zebra Buds", "Alpha Phone"])
        self.assertEqual(svc.devices()[0]["icon"], "audio-headphones")
        self.assertFalse(svc.devices()[2]["paired"])

    def test_audio_live_sink_input_and_controls(self):
        with mock.patch(
            "gi.repository.Gio.Subprocess.new", side_effect=GLib.Error("no pactl")
        ):
            svc = AudioService()
        svc._read_next = lambda: None
        fake_res = mock.MagicMock()
        svc._stream = mock.MagicMock()
        svc._stream.read_line_finish_utf8.return_value = (
            "Event 'new' on sink-input #9\n",
            29,
        )
        with mock.patch("gi.repository.GLib.timeout_add", return_value=99) as m_to:
            svc._on_line(svc._stream, fake_res)
            self.assertTrue(
                any(
                    call.args[1] == svc._refresh_devices_once
                    for call in m_to.call_args_list
                )
            )
            self.assertEqual(svc._dev_timer, 99)

        with mock.patch("gi.repository.Gio.Subprocess.new") as m_sub:
            svc.set_volume(200)
            svc.set_muted(True)
            svc.set_mic_muted(False)
            cmds = [c.args[0] for c in m_sub.call_args_list]
        self.assertEqual(
            cmds[0], ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "150%"]
        )
        self.assertEqual(cmds[1], ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1"])
        self.assertEqual(cmds[2], ["pactl", "set-source-mute", "@DEFAULT_SOURCE@", "0"])
        svc._pending_timer = 0
        svc._dev_timer = 0
        svc.stop()

    def test_microphone_source_events_refresh_without_sink_events(self):
        with mock.patch(
            "gi.repository.Gio.Subprocess.new", side_effect=GLib.Error("no pactl")
        ):
            svc = AudioService()
        svc._read_next = lambda: None
        svc._stream = mock.MagicMock()
        svc._stream.read_line_finish_utf8.return_value = (
            "Event 'change' on source #62\n",
            29,
        )
        queued = []
        with mock.patch(
            "gi.repository.GLib.timeout_add",
            side_effect=lambda _delay, callback: queued.append(callback) or len(queued),
        ):
            svc._on_line(svc._stream, mock.MagicMock())

        self.assertEqual(len(queued), 1)
        svc.mic_muted = True
        proc = mock.MagicMock()
        proc.communicate_utf8_finish.side_effect = [
            (True, "mic-device\n", ""),
            (
                True,
                json.dumps(
                    [
                        {
                            "name": "mic-device",
                            "mute": False,
                            "properties": {"api.alsa.pcm.card": "1"},
                        }
                    ]
                ),
                "",
            ),
        ]
        with mock.patch("gi.repository.Gio.Subprocess.new", return_value=proc) as new:
            queued[0]()
            proc.communicate_utf8_async.call_args.args[2](proc, None)
            self.assertEqual(
                new.call_args.args[0],
                ["pactl", "-f", "json", "list", "sources"],
            )
            proc.communicate_utf8_async.call_args_list[-1].args[2](proc, None)
        self.assertFalse(svc.mic_muted)
        svc.stop()

    def test_microphone_poll_reconciles_state_without_source_events(self):
        with mock.patch(
            "gi.repository.Gio.Subprocess.new", side_effect=GLib.Error("no pactl")
        ):
            svc = AudioService()
        with mock.patch.object(svc, "_query_mic") as query_mic:
            self.assertTrue(svc._poll_mic_state())
            query_mic.assert_called_once_with()
        svc._stopping = True
        self.assertFalse(svc._poll_mic_state())
        svc.stop()

    def test_system_agent_tools_and_ai_tool_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_path = os.path.join(tmp, "cfg.json")
            from ledgepill import config as _cfg_mod

            orig_cfg_path = _cfg_mod.CONFIG_PATH
            _cfg_mod.CONFIG_PATH = old_path
            try:
                cfg = {
                    "ai_provider": "ollama",
                    "ai_system_control": True,
                    "theme": "obsidian",
                    "island_style": "pill",
                    "accent_mode": "manual",
                }
                audio = mock.MagicMock(
                    percent=40,
                    muted=False,
                    mic_muted=False,
                    default_sink="hdmi",
                    sinks=[
                        {"name": "hdmi", "description": "HDMI Out", "default": False}
                    ],
                    sink_inputs=[
                        {"id": 1, "name": "Firefox", "volume": 80, "muted": False}
                    ],
                )
                brightness = mock.MagicMock(available=True, percent=50)
                network = mock.MagicMock(
                    kind="802-11-wireless",
                    connected=True,
                    wifi_enabled=True,
                    wifi_signal=82,
                    ip_address="192.168.1.10",
                    wifi_networks=[],
                )
                network.name = "Home"
                bt = mock.MagicMock(
                    powered=True,
                    connected=True,
                    device="Headphones",
                    battery=70,
                )
                bt.devices.return_value = [
                    {"name": "Headphones", "connected": True, "battery": 70}
                ]
                desktop = mock.MagicMock(keyboard_layout="us")
                desktop.list_running_apps.return_value = [
                    {
                        "name": "Firefox",
                        "pids": [42],
                        "window_id": "w1",
                        "rss_mb": 200.0,
                    }
                ]
                applied = []
                dnd_calls = []

                agent = SystemAgent(
                    cfg,
                    audio=audio,
                    brightness=brightness,
                    network=network,
                    bluetooth=bt,
                    desktop=desktop,
                    on_dnd_change=lambda v: dnd_calls.append(v),
                    on_apply_config=lambda s: applied.append(s),
                )
                prod = ProductivityService(cfg, system_agent=agent)
                agent.on_confirm_request = prod.request_action_confirm

                snap = agent.snapshot()
                self.assertEqual(snap["audio"]["volume"], 40)
                self.assertEqual(snap["network"]["ip_address"], "192.168.1.10")

                parsed = SystemAgent.parse_tool_call(
                    'Sure <tool_call>{"tool": "set_dnd", "args": {"enabled": true}}</tool_call>'
                )
                self.assertEqual(parsed, ("set_dnd", {"enabled": True}))
                tool_schemas = agent.tool_schemas()
                tool_names = {item["function"]["name"] for item in tool_schemas}
                self.assertIn("set_dnd", tool_names)
                self.assertNotIn("refresh_weather", tool_names)
                for blocked_control in (
                    "set_volume",
                    "set_mute",
                    "set_mic_mute",
                    "set_app_volume",
                    "set_audio_output",
                    "set_brightness",
                ):
                    self.assertNotIn(blocked_control, tool_names)
                self.assertFalse(agent.execute_tool("set_volume", {"percent": 75})["ok"])
                audio.set_volume.assert_not_called()
                self.assertFalse(
                    agent.execute_tool("set_brightness", {"percent": 75})["ok"]
                )
                brightness.set_percent.assert_not_called()

                app_res = agent.execute_tool(
                    "set_appearance", {"theme": "liquid-glass", "accent_mode": "auto"}
                )
                self.assertTrue(app_res["ok"])
                self.assertEqual(applied, [False])

                # Destructive session actions must gate behind confirmation
                confirms = []
                prod.connect(
                    "action-confirm", lambda _s, lbl, _cb: confirms.append(lbl)
                )
                r_req = agent.execute_tool(
                    "request_session_action", {"action": "reboot"}
                )
                self.assertEqual(r_req["pending_confirmation"], "Reboot system")
                desktop.power_action.assert_not_called()
                self.assertEqual(confirms, ["Reboot system"])
                duplicate_req = agent.execute_tool(
                    "request_session_action", {"action": "poweroff"}
                )
                self.assertFalse(duplicate_req["ok"])
                self.assertEqual(confirms, ["Reboot system"])
                prod.resolve_pending_action(True)
                desktop.power_action.assert_called_once_with("reboot")

                # Verify AI multi-step tool loop & Claude/Gemini system formatting
                seen_payloads = []

                def fake_post(url, payload, headers=None, timeout=20):
                    seen_payloads.append((url, payload))
                    if len(seen_payloads) == 1:
                        return {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "type": "function",
                                        "function": {
                                            "name": "set_dnd",
                                            "arguments": {"enabled": True},
                                        },
                                    }
                                ],
                            }
                        }
                    return {"message": {"role": "assistant", "content": "DND enabled."}}

                with (
                    mock.patch(
                        "ledgepill.services.productivity._post_json",
                        side_effect=fake_post,
                    ),
                    mock.patch(
                        "ledgepill.services.productivity.GLib.idle_add",
                        side_effect=lambda fn, *a: fn(*a),
                    ),
                ):
                    prod._ai_worker("ollama", "llama3.2", "", "Turn on DND")
                self.assertEqual(dnd_calls, [True])
                self.assertEqual(prod.last_ai_reply, "DND enabled.")
                self.assertIn("tools", seen_payloads[0][1])
                self.assertEqual(seen_payloads[0][1]["tools"], tool_schemas)
                self.assertEqual(
                    seen_payloads[1][1]["messages"][-1]["tool_name"], "set_dnd"
                )

                with mock.patch(
                    "ledgepill.services.productivity._post_json",
                    return_value={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call-123",
                                            "type": "function",
                                            "function": {
                                                "name": "set_dnd",
                                                "arguments": '{"enabled": true}',
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                ) as m_post:
                    response = ProductivityService._dispatch_ai(
                        "lmstudio",
                        "local-model",
                        "",
                        "Enable DND",
                        [{"role": "user", "content": "Enable DND"}],
                        tools=tool_schemas,
                    )
                    self.assertEqual(response["tool_calls"][0]["id"], "call-123")
                    self.assertEqual(response["tool_calls"][0]["name"], "set_dnd")
                    self.assertEqual(m_post.call_args.args[1]["tools"], tool_schemas)

                lm_rounds = []

                def fake_lmstudio_post(url, payload, headers=None, timeout=20):
                    lm_rounds.append(payload)
                    if len(lm_rounds) == 1:
                        return {
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": None,
                                        "tool_calls": [
                                            {
                                                "id": "dnd-call",
                                                "type": "function",
                                                "function": {
                                                    "name": "set_dnd",
                                                    "arguments": '{"enabled": true}',
                                                },
                                            }
                                        ],
                                    }
                                }
                            ]
                        }
                    return {
                        "choices": [
                            {"message": {"role": "assistant", "content": "DND enabled."}}
                        ]
                    }

                with mock.patch(
                    "ledgepill.services.productivity._post_json",
                    side_effect=fake_lmstudio_post,
                ):
                    lm_reply = ProductivityService._run_native_tool_loop(
                        "lmstudio",
                        "local-model",
                        "",
                        "Enable DND",
                        [{"role": "user", "content": "Enable DND"}],
                        agent,
                        tool_schemas,
                    )
                self.assertEqual(lm_reply, "DND enabled.")
                self.assertEqual(lm_rounds[1]["messages"][-1]["role"], "tool")
                self.assertEqual(
                    lm_rounds[1]["messages"][-1]["tool_call_id"], "dnd-call"
                )
                self.assertEqual(dnd_calls, [True, True])

                seen_payloads.clear()
                with mock.patch(
                    "ledgepill.services.productivity._post_json"
                ) as m_post:
                    m_post.return_value = {"content": [{"text": "ok"}]}
                    ProductivityService._dispatch_ai(
                        "claude",
                        "claude-sonnet-4-5",
                        "sk-test",
                        "hi",
                        [
                            {"role": "system", "content": "SYS"},
                            {"role": "user", "content": "hi"},
                        ],
                    )
                    claude_body = m_post.call_args.args[1]
                    self.assertEqual(claude_body["system"], "SYS")
                    self.assertEqual(
                        claude_body["messages"], [{"role": "user", "content": "hi"}]
                    )

                    m_post.return_value = {
                        "candidates": [{"content": {"parts": [{"text": "ok"}]}}]
                    }
                    ProductivityService._dispatch_ai(
                        "gemini",
                        "gemini-2.5-flash",
                        "gm-test",
                        "hi",
                        [
                            {"role": "system", "content": "SYS"},
                            {"role": "user", "content": "hi"},
                        ],
                    )
                    gem_body = m_post.call_args.args[1]
                    self.assertEqual(
                        gem_body["systemInstruction"], {"parts": [{"text": "SYS"}]}
                    )
            finally:
                _cfg_mod.CONFIG_PATH = orig_cfg_path

    def test_system_agent_marshals_background_calls_to_creator_thread(self):
        agent = SystemAgent({})
        creator_thread = threading.get_ident()
        result = {}
        queued = threading.Event()
        callbacks = []

        def capture_idle(callback):
            callbacks.append(callback)
            queued.set()
            return 1

        worker = threading.Thread(
            target=lambda: result.update(
                agent._run_on_main(lambda: {"thread_id": threading.get_ident()})
            ),
            daemon=True,
        )
        with mock.patch(
            "ledgepill.services.system_agent.GLib.idle_add",
            side_effect=capture_idle,
        ):
            worker.start()
            self.assertTrue(queued.wait(1))
            callbacks[0]()
        worker.join(timeout=0.1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result.get("thread_id"), creator_thread)

    def test_network_ip_lifecycle_and_stopped_power_mpris_guards(self):
        with mock.patch(
            "gi.repository.Gio.bus_get_sync", side_effect=GLib.Error("no bus")
        ):
            net = NetworkService()
        net.wifi_networks = [
            {"ssid": "Home", "signal": 80, "security": "WPA2", "active": True}
        ]
        net.ip_address = "192.168.1.50"
        net._apply_wireless_enabled(False)
        self.assertEqual(net.wifi_networks, [])

        fake_bus = mock.MagicMock()
        fake_bus.call_finish.return_value = types.SimpleNamespace(unpack=lambda: ("/",))
        net._on_primary(fake_bus, None)
        self.assertEqual(net.ip_address, "")

        with mock.patch.object(net, "_fetch_ip_async") as m_ip:
            fake_bus.call_finish.return_value = types.SimpleNamespace(
                unpack=lambda: ({"Type": "802-11-wireless", "Id": "Home"},)
            )
            net._on_active(fake_bus, None)
            m_ip.assert_called_once()

        with mock.patch(
            "gi.repository.Gio.bus_get_sync", side_effect=GLib.Error("no bus")
        ):
            pwr = PowerService()
        pwr_changes = []
        pwr.connect("changed", lambda *_: pwr_changes.append(True))
        pwr.stop()
        pwr._on_get_all(fake_bus, None)
        self.assertEqual(pwr_changes, [])

        with mock.patch(
            "gi.repository.Gio.bus_get_sync", side_effect=GLib.Error("no bus")
        ):
            mpris = MprisService()
        mpris._add_player = mock.MagicMock()
        mpris.stop()
        fake_bus.call_finish.return_value = types.SimpleNamespace(
            unpack=lambda: (["org.mpris.MediaPlayer2.spotify"],)
        )
        mpris._on_list_names(fake_bus, None)
        mpris._add_player.assert_not_called()

    def test_desktop_app_id_aliases_and_wmctrl_parsing(self):
        from ledgepill.services.desktop import _app_id_aliases, _wmctrl_windows

        self.assertIn("spotify", _app_id_aliases("com.spotify.client"))
        self.assertIn("telegram", _app_id_aliases("org.telegram.desktop"))
        self.assertIn("firefox", _app_id_aliases("org.mozilla.firefox"))
        sample = "0x02a00003  0 4120 firefox.firefox myhost Docs - Mozilla Firefox\n"
        with mock.patch(
            "subprocess.run",
            return_value=types.SimpleNamespace(returncode=0, stdout=sample),
        ):
            wins = _wmctrl_windows()
        self.assertEqual(len(wins), 1)
        self.assertEqual(wins[0]["pid"], 4120)
        self.assertEqual(wins[0]["app"], "firefox")

    def test_ollama_vram_capabilities_lmstudio_v0_and_unload(self):
        svc = OllamaService()
        ps_payload = {
            "models": [
                {
                    "name": "qwen2.5:7b",
                    "size": 4 * (1024**3),
                    "size_vram": 3 * (1024**3),
                    "context_length": 8192,
                    "details": {
                        "quantization_level": "Q4_K_M",
                        "parameter_size": "7.6B",
                    },
                }
            ]
        }
        parsed = svc._parse_ps_details(ps_payload)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["vram_pct"], 75)
        self.assertEqual(parsed[0]["context_length"], 8192)
        self.assertEqual(parsed[0]["quantization"], "Q4_K_M")

        def fake_get(url):
            if url.endswith("/api/v0/models"):
                return {
                    "data": [
                        {"id": "idle-model", "state": "not-loaded"},
                        {
                            "id": "loaded-qwen",
                            "state": "loaded",
                            "quantization": "Q4_K_M",
                            "loaded_context_length": 16384,
                        },
                    ]
                }
            raise urllib.error.URLError("offline")

        with mock.patch.object(svc, "_get_json", side_effect=fake_get):
            lm_models, lm_avail, lm_loaded, lm_det = svc._fetch_lmstudio()
        self.assertTrue(lm_avail)
        self.assertEqual(lm_models, ["loaded-qwen", "idle-model"])
        self.assertEqual(lm_loaded, ["loaded-qwen"])
        self.assertEqual(lm_det["loaded-qwen"]["loaded_context_length"], 16384)

        svc._apply(
            True,
            ["qwen2.5:7b"],
            ["qwen2.5:7b"],
            "active",
            "qwen2.5:7b",
            running_details=parsed,
            caps_update=("qwen2.5:7b", ["completion", "tools"]),
        )
        self.assertTrue(svc.model_supports_tools("qwen2.5:7b"))
        svc.mark_no_tools("qwen2.5:7b")
        self.assertFalse(svc.model_supports_tools("qwen2.5:7b"))

        with mock.patch.object(svc, "_post_json", return_value={}) as m_post:
            self.assertTrue(svc.unload_model("qwen2.5:7b"))
            self.assertEqual(m_post.call_args.args[1]["keep_alive"], 0)
            self.assertTrue(svc.preload_model("qwen2.5:7b", "15m"))
            self.assertEqual(m_post.call_args.args[1]["keep_alive"], "15m")

        with mock.patch(
            "ledgepill.services.ollama.GLib.idle_add",
            side_effect=lambda fn, *a: fn(*a),
        ):
            svc.record_metrics(48.67, 120, "qwen2.5:7b")
        self.assertEqual(svc.tokens_per_sec, 48.7)
        self.assertEqual(svc.last_eval_tokens, 120)

    def test_productivity_streaming_think_stripping_and_tool_fallback(self):
        from ledgepill.services.productivity import (
            _stream_ollama_chat,
            _strip_thinking,
        )

        self.assertEqual(
            _strip_thinking("<think>step 1\nstep 2</think>\nFinal reply."),
            "Final reply.",
        )

        ndjson_lines = [
            b'{"message":{"content":"<think>reasoning"},"done":false}\n',
            b'{"message":{"content":"</think>Hello "},"done":false}\n',
            b'{"message":{"content":"world"},"done":true,"eval_count":20,"eval_duration":500000000}\n',
        ]
        fake_resp = mock.MagicMock()
        fake_resp.__enter__.return_value = iter(ndjson_lines)
        seen_tokens = []
        with mock.patch(
            "ledgepill.services.productivity.urllib.request.urlopen",
            return_value=fake_resp,
        ):
            text, stats = _stream_ollama_chat(
                "http://127.0.0.1:11434/api/chat",
                {"model": "deepseek-r1", "messages": [], "stream": True},
                on_token=lambda t: seen_tokens.append(t),
            )
        self.assertEqual(text, "Hello world")
        self.assertIsNotNone(stats)
        self.assertAlmostEqual(stats["tok_s"], 40.0, places=1)

        # Native tools returning HTTP 400 fallback seamlessly to XML tool loop
        ollama = OllamaService()
        agent = SystemAgent({"ai_system_control": True}, ollama=ollama)
        prod = ProductivityService(
            {"ai_provider": "ollama", "ai_system_control": True},
            ollama=ollama,
            system_agent=agent,
        )

        call_count = {"n": 0}

        def fake_post(url, payload, headers=None, timeout=20):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise urllib.error.HTTPError(
                    url,
                    400,
                    "Bad Request",
                    {},
                    io.BytesIO(b'{"error":"llama2 does not support tools"}'),
                )
            if call_count["n"] == 2:
                return {
                    "message": {
                        "content": '<tool_call>{"tool":"manage_local_llm","args":{"action":"status"}}</tool_call>'
                    }
                }
            return {"message": {"content": "Model status checked cleanly."}}

        replies = []
        prod.connect("ai-reply", lambda _s, txt, ok: replies.append((txt, ok)))
        with (
            mock.patch(
                "ledgepill.services.productivity._post_json", side_effect=fake_post
            ),
            mock.patch(
                "ledgepill.services.productivity.GLib.idle_add",
                side_effect=lambda fn, *a: fn(*a),
            ),
        ):
            prod._ai_worker("ollama", "legacy-mod", "", "Check model")
        self.assertEqual(replies, [("Model status checked cleanly.", True)])
        self.assertFalse(ollama.model_supports_tools("legacy-mod"))

        prod._ai_busy = True
        prod.cancel_ai()
        self.assertFalse(prod._ai_busy)
        self.assertEqual(replies[-1], ("Cancelled.", False))

    def test_system_agent_deep_os_tools(self):
        from ledgepill.services.system_agent import (
            _read_disk_usage,
            _read_systemd_status,
            _read_top_processes,
        )

        procs = _read_top_processes("ram", limit=3)
        self.assertIsInstance(procs, list)
        self.assertLessEqual(len(procs), 3)

        disks = _read_disk_usage()
        self.assertIsInstance(disks, list)

        with (
            mock.patch("shutil.which", return_value="/usr/bin/systemctl"),
            mock.patch(
                "subprocess.run",
                return_value=types.SimpleNamespace(
                    returncode=0, stdout="pipewire.service loaded failed failed\n"
                ),
            ),
        ):
            sd = _read_systemd_status("user", include_logs=True)
        self.assertTrue(sd["ok"])
        self.assertEqual(sd["scope"], "user")
        self.assertEqual(sd["failed_units"], ["pipewire.service loaded failed failed"])

        audio = mock.MagicMock(
            percent=50,
            muted=False,
            mic_muted=False,
            default_sink="s1",
            sinks=[],
            sink_inputs=[{"id": 9, "name": "Spotify", "volume": 80, "muted": False}],
        )
        notifier = mock.MagicMock(
            history=[{"app": "Signal", "summary": "New message", "body": "Hi"}]
        )
        clip = mock.MagicMock(history=["git status", "secret"], pinned=["git status"])
        ollama = OllamaService()
        ollama.running_details = [
            {"name": "qwen2.5:7b", "vram_gb": 4.2, "vram_pct": 100}
        ]
        ui_calls = []
        agent = SystemAgent(
            {"ai_system_control": True},
            audio=audio,
            clipboard=clip,
            notifier=notifier,
            ollama=ollama,
            on_ui_action=lambda k, a: ui_calls.append((k, a)) or {"ok": True},
        )
        snap = agent.snapshot()
        self.assertEqual(snap["notifications"]["count"], 1)
        self.assertEqual(snap["clipboard"]["pinned_count"], 1)
        self.assertEqual(snap["local_llm"]["running"][0]["vram_pct"], 100)

        app_vol = agent.execute_tool(
            "set_app_volume", {"app": "spot", "percent": 45, "muted": False}
        )
        self.assertFalse(app_vol["ok"])
        audio.set_app_volume.assert_not_called()
        audio.set_app_mute.assert_not_called()

        self.assertEqual(
            agent.execute_tool("search_clipboard", {"query": "git"})["items"][0][
                "pinned"
            ],
            True,
        )
        self.assertEqual(
            len(agent.execute_tool("get_notifications", {})["notifications"]), 1
        )
        self.assertTrue(
            agent.execute_tool(
                "control_timer", {"action": "set_minutes", "minutes": 10}
            )["ok"]
        )
        self.assertEqual(
            ui_calls, [("timer", {"action": "set_minutes", "minutes": 10})]
        )

    def test_todo_service_and_agent_tools(self):
        from ledgepill.services.todos import TodoService

        with tempfile.TemporaryDirectory() as tmp:
            todo_path = os.path.join(tmp, "todos.json")

            todos = TodoService(storage_path=todo_path)
            self.assertTrue(todos.add("  Ship the release  "))
            self.assertEqual(todos.active_task(), "Ship the release")
            self.assertEqual(todos.pending_count(), 1)
            self.assertEqual(os.stat(todo_path).st_mode & 0o777, 0o600)

            agent = SystemAgent({"ai_system_control": True}, todos=todos)
            snap = agent.snapshot()
            self.assertEqual(snap["focus_tasks"]["pending"], 1)
            self.assertNotIn("shelf", snap)
            self.assertNotIn(
                "manage_shelf",
                {tool["function"]["name"] for tool in agent.tool_schemas()},
            )

            res_add = agent.execute_tool(
                "manage_todos", {"action": "add", "text": "Verify Broadway"}
            )
            self.assertTrue(res_add["ok"])
            self.assertEqual(todos.pending_count(), 2)
            res_toggle = agent.execute_tool(
                "manage_todos", {"action": "toggle", "index": 0}
            )
            self.assertTrue(res_toggle["done"])
            res_clear_done = agent.execute_tool(
                "manage_todos", {"action": "clear_done"}
            )
            self.assertEqual(res_clear_done["removed"], 1)

    def test_desktop_coding_agent_activity_detection(self):
        from ledgepill.services.desktop import _AGENT_PROCS

        self.assertEqual(_AGENT_PROCS["claude"], "Claude Code")
        desk = DesktopService()
        events = []
        desk.connect("activity-changed", lambda *_: events.append(desk.agent_activity))
        desk._apply_poll([], [], False, "us", "", "", [], None, "Claude Code")
        self.assertEqual(desk.agent_activity, "Claude Code")
        self.assertEqual(events, ["Claude Code"])

    def test_calendar_ics_parser_and_agent_tool(self):
        from ledgepill.services.calendar import CalendarService, parse_ics_events

        now_ts = 1_800_000_000.0
        dt_start = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now_ts + 480))
        dt_end = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now_ts + 2280))
        ics = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            "SUMMARY:Design Review\r\n"
            " Continuation Line\r\n"
            f"DTSTART:{dt_start}\r\n"
            f"DTEND:{dt_end}\r\n"
            "URL:https://meet.example.com/room\r\n"
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\n"
            "SUMMARY:Cancelled Sync\r\n"
            f"DTSTART:{dt_start}\r\n"
            "STATUS:CANCELLED\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        parsed = parse_ics_events(ics, now_ts=now_ts)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["summary"], "Design ReviewContinuation Line")
        self.assertEqual(parsed[0]["url"], "https://meet.example.com/room")

        cal = CalendarService([])
        cal.events = parsed
        nxt = cal.next_event(now_ts=now_ts)
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt["minutes_until"], 8)
        self.assertIn("in 8m", cal.format_next_label(now_ts=now_ts))

        agent = SystemAgent({"ai_system_control": True}, calendar=cal)
        self.assertEqual(agent.snapshot()["calendar"]["upcoming_count"], 1)
        res = agent.execute_tool("get_upcoming_events", {"limit": 5})
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["events"]), 1)

    def test_calendar_service_restarts_after_stop(self):
        from ledgepill.services.calendar import CalendarService

        cal = CalendarService([])
        with (
            mock.patch.object(cal, "refresh_now") as refresh,
            mock.patch(
                "ledgepill.services.calendar.GLib.timeout_add_seconds",
                side_effect=[11, 12],
            ) as add_timer,
            mock.patch("ledgepill.services.calendar.GLib.source_remove"),
        ):
            cal.start()
            self.assertEqual(refresh.call_count, 1)
            self.assertEqual(cal._timer, 11)

            cal._busy = True
            cal.stop()
            self.assertTrue(cal._stopped)
            self.assertEqual(cal._timer, 0)

            cal.start()
            self.assertFalse(cal._stopped)
            self.assertFalse(cal._busy)
            self.assertEqual(refresh.call_count, 2)
            self.assertEqual(cal._timer, 12)

            self.assertTrue(cal._tick())
            self.assertEqual(refresh.call_count, 3)
            self.assertEqual(add_timer.call_count, 2)

    def test_kdeconnect_share_file(self):
        from ledgepill.services.kdeconnect import KdeConnectService

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "share.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("share this")

            with mock.patch("gi.repository.Gio.bus_get_sync") as bus_mock:
                bus = bus_mock.return_value
                kde = KdeConnectService()
                kde.devices = [
                    {"id": "dev1", "name": "Pixel 9", "reachable": True, "battery": 80}
                ]
                self.assertEqual(kde.first_reachable_device()["id"], "dev1")
                self.assertTrue(kde.share_file("dev1", path))
                bus.call.assert_called()

    def test_webhook_agent_endpoint(self):
        import urllib.request

        from ledgepill.services.webhook import WebhookService

        received = []
        with tempfile.TemporaryDirectory() as tmp:
            wh = WebhookService(
                lambda *_a: None,
                port=17399,
                token_path=os.path.join(tmp, "tok"),
                on_agent_event=lambda ag, st, msg: received.append((ag, st, msg)),
            )
            with mock.patch(
                "ledgepill.services.webhook.GLib.idle_add",
                side_effect=lambda fn, *a: fn(*a),
            ):
                if wh.start():
                    try:
                        req = urllib.request.Request(
                            "http://127.0.0.1:17399/agent",
                            data=json.dumps(
                                {
                                    "agent": "Claude Code",
                                    "state": "attention",
                                    "message": "Approve write",
                                }
                            ).encode("utf-8"),
                            headers={
                                "Authorization": f"Bearer {wh.token}",
                                "Content-Type": "application/json",
                            },
                            method="POST",
                        )
                        with urllib.request.urlopen(req, timeout=2) as resp:
                            self.assertEqual(resp.status, 202)
                        self.assertEqual(
                            received, [("Claude Code", "attention", "Approve write")]
                        )
                    finally:
                        wh.stop()


if __name__ == "__main__":
    unittest.main()
