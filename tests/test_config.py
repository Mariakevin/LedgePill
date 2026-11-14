import json
import os
import tempfile
import unittest

from ledgepill import config
from ledgepill.app import layer_shell_hint
from ledgepill.services.brightness import BrightnessService


class ConfigTest(unittest.TestCase):
    def test_merge_keeps_defaults_for_missing_keys(self):
        merged = config._merge(config.DEFAULTS, {"margin_top": 20})
        self.assertEqual(merged["margin_top"], 20)
        self.assertEqual(merged["layer"], config.DEFAULTS["layer"])

    def test_merge_is_deep_for_modules(self):
        merged = config._merge(config.DEFAULTS, {"modules": {"music": False}})
        self.assertFalse(merged["modules"]["music"])
        self.assertTrue(merged["modules"]["battery"])

    def test_defaults_survive_broken_config(self):
        self.assertEqual(config._merge(config.DEFAULTS, {}), config.DEFAULTS)

    def test_load_returns_deep_copy_of_defaults(self):
        old_path = config.CONFIG_PATH
        config.CONFIG_PATH = "/nonexistent/ledge-pill-test-config.json"
        try:
            loaded = config.load()
            loaded["modules"]["music"] = False
            self.assertTrue(config.DEFAULTS["modules"]["music"])
        finally:
            config.CONFIG_PATH = old_path

    def test_load_falls_back_to_defaults_for_invalid_json(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("{not valid json")
            temp_path = f.name
        try:
            old_path = config.CONFIG_PATH
            config.CONFIG_PATH = temp_path
            self.assertEqual(config.load(), config.DEFAULTS)
        finally:
            config.CONFIG_PATH = old_path
            os.unlink(temp_path)

    def test_load_falls_back_to_defaults_for_non_mapping_config(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write('["not", "a", "dict"]')
            temp_path = f.name
        try:
            old_path = config.CONFIG_PATH
            config.CONFIG_PATH = temp_path
            self.assertEqual(config.load(), config.DEFAULTS)
        finally:
            config.CONFIG_PATH = old_path
            os.unlink(temp_path)

    def test_save_normalizes_non_mapping_input(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            temp_path = f.name
        try:
            old_path = config.CONFIG_PATH
            config.CONFIG_PATH = temp_path
            config.save(["bad", "payload"])
            with open(temp_path) as f:
                saved = json.load(f)
            self.assertEqual(saved, config.DEFAULTS)
        finally:
            config.CONFIG_PATH = old_path
            os.unlink(temp_path)

    def test_load_sanitizes_malformed_field_types(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            json.dump(
                {
                    "margin_top": "bad",
                    "opacity": 9.9,
                    "peek_seconds": -5,
                    "layer": "unknown",
                    "click_action": "unknown",
                    "pill_faces": "not-a-list",
                    "modules": "not-a-dict",
                },
                f,
            )
            temp_path = f.name
        try:
            old_path = config.CONFIG_PATH
            config.CONFIG_PATH = temp_path
            loaded = config.load()
            self.assertEqual(loaded["margin_top"], 0)
            self.assertEqual(loaded["opacity"], 1.0)
            self.assertEqual(loaded["peek_seconds"], 0.5)
            self.assertEqual(loaded["layer"], "top")
            self.assertEqual(loaded["click_action"], "wake")
            self.assertEqual(loaded["pill_faces"], config.DEFAULTS["pill_faces"])
            self.assertEqual(loaded["modules"], config.DEFAULTS["modules"])
            for valid_act in ("wake", "expand", "cycle"):
                self.assertEqual(
                    config._normalize({"click_action": valid_act})["click_action"],
                    valid_act,
                )
        finally:
            config.CONFIG_PATH = old_path
            os.unlink(temp_path)

    def test_boolean_settings_ignore_non_boolean_values(self):
        loaded = config._normalize(
            {
                "idle_clock": "false",
                "clock_24h": 0,
                "ai_system_control": "nope",
                "modules": {"music": "false", "battery": False},
            }
        )
        self.assertTrue(loaded["idle_clock"])
        self.assertTrue(loaded["clock_24h"])
        self.assertFalse(loaded["ai_system_control"])
        self.assertTrue(loaded["modules"]["music"])
        self.assertFalse(loaded["modules"]["battery"])

    def test_new_visual_toggle_defaults_and_normalization(self):
        for key in ("status_ring", "ambient_tint", "activity_queue"):
            self.assertIn(key, config.DEFAULTS)
            self.assertTrue(config.DEFAULTS[key])

        normalized = config._normalize(
            {
                "status_ring": False,
                "ambient_tint": False,
                "activity_queue": False,
            }
        )
        self.assertFalse(normalized["status_ring"])
        self.assertFalse(normalized["ambient_tint"])
        self.assertFalse(normalized["activity_queue"])

        invalid = config._normalize(
            {
                "status_ring": "false",
                "ambient_tint": 0,
                "activity_queue": None,
            }
        )
        self.assertTrue(invalid["status_ring"])
        self.assertTrue(invalid["ambient_tint"])
        self.assertTrue(invalid["activity_queue"])

    def test_layer_shell_hint_mentions_supported_package_names(self):
        msg = layer_shell_hint()
        self.assertIn("libgtk4-layer-shell0", msg)
        self.assertIn("gtk4-layer-shell", msg)

    def test_brightness_parser_handles_cli_output(self):
        self.assertEqual(BrightnessService.parse_percent("29%"), 29)
        self.assertEqual(
            BrightnessService.parse_percent(
                "Device 'intel_backlight' of class 'backlight': 300/1023 (29%)"
            ),
            29,
        )
        self.assertEqual(BrightnessService.parse_percent("55.0"), 55)

    def test_new_config_fields_and_permissions(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            temp_path = f.name
        try:
            old_path = config.CONFIG_PATH
            config.CONFIG_PATH = temp_path
            normalized = config._normalize(
                {
                    "theme": "liquid-glass",
                    "island_style": "waterdrop",
                    "animation_style": "snappy",
                    "fullscreen_behavior": "compact",
                    "ai_provider": "gemini",
                    "ai_system_control": False,
                    "accent_mode": "auto",
                    "focus_auto_dnd": True,
                    "mascot": True,
                    "pill_faces": ["auto", "mascot", "bogus"],
                    "feeds": [
                        {"url": "https://example.com/feed.xml", "name": "Example"},
                        "bad",
                    ],
                    "monitor_layouts": {
                        "DP-1": {
                            "margin_top": 999,
                            "layer": "invalid",
                            "style": "waterdrop",
                        }
                    },
                }
            )
            self.assertEqual(normalized["theme"], "liquid-glass")
            self.assertEqual(normalized["island_style"], "waterdrop")
            self.assertEqual(normalized["animation_style"], "snappy")
            self.assertEqual(normalized["fullscreen_behavior"], "compact")
            self.assertEqual(normalized["ai_provider"], "gemini")
            self.assertFalse(normalized["ai_system_control"])
            self.assertFalse(config.DEFAULTS["ai_system_control"])
            self.assertEqual(normalized["accent_mode"], "auto")
            self.assertTrue(normalized["dynamic_artwork_colors"])
            self.assertEqual(normalized["pill_faces"], ["auto", "mascot"])
            self.assertEqual(len(normalized["feeds"]), 1)
            self.assertEqual(normalized["monitor_layouts"]["DP-1"]["margin_top"], 200)
            self.assertEqual(normalized["monitor_layouts"]["DP-1"]["layer"], "top")
            self.assertEqual(
                normalized["monitor_layouts"]["DP-1"]["style"], "waterdrop"
            )

            legacy_dyn = config._normalize({"dynamic_artwork_colors": True})
            self.assertEqual(legacy_dyn["accent_mode"], "artwork")

            config.save(normalized)
            mode = os.stat(temp_path).st_mode & 0o777
            self.assertEqual(mode, 0o600)
        finally:
            config.CONFIG_PATH = old_path
            os.unlink(temp_path)

    def test_partial_monitor_layouts_inherit_normalized_global_style_and_layer(self):
        normalized = config._normalize(
            {
                "island_style": "dashboard",
                "layer": "overlay",
                "monitor_layouts": {
                    "DP-1": {"margin_top": 24},
                    "HDMI-1": {"style": "waterdrop", "layer": "top"},
                },
            }
        )

        self.assertEqual(
            normalized["monitor_layouts"]["DP-1"],
            {
                "margin_top": 24,
                "style": "dashboard",
                "layer": "overlay",
                "overlap_panel": True,
            },
        )
        self.assertEqual(
            normalized["monitor_layouts"]["HDMI-1"]["style"], "waterdrop"
        )
        self.assertEqual(normalized["monitor_layouts"]["HDMI-1"]["layer"], "top")

    def test_invalid_enum_and_nested_fields_fall_back_safely(self):
        loaded = config._normalize(
            {
                "theme": "neon",
                "island_style": "hexagon",
                "accent_mode": "rainbow",
                "animation_style": "warp",
                "fullscreen_behavior": "explode",
                "ai_provider": "unknown-llm",
                "shortcuts": "bad",
            }
        )
        self.assertEqual(loaded["theme"], "obsidian")
        self.assertEqual(loaded["island_style"], "pill")
        self.assertEqual(loaded["accent_mode"], "manual")
        self.assertEqual(loaded["animation_style"], "fluid")
        self.assertEqual(loaded["fullscreen_behavior"], "ignore")
        self.assertEqual(loaded["ai_provider"], "ollama")
        self.assertEqual(loaded["shortcuts"], config.DEFAULTS["shortcuts"])

    def test_load_migrates_legacy_cycle_click_to_wake(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            json.dump({"click_action": "cycle", "theme": "obsidian"}, f)
            temp_path = f.name
        try:
            old_path = config.CONFIG_PATH
            config.CONFIG_PATH = temp_path
            loaded = config.load()
            self.assertEqual(loaded["click_action"], "wake")
            self.assertEqual(loaded["config_version"], 2)
            with open(temp_path) as f:
                persisted = json.load(f)
            self.assertEqual(persisted["click_action"], "wake")
            self.assertEqual(persisted["config_version"], 2)
        finally:
            config.CONFIG_PATH = old_path
            os.unlink(temp_path)


if __name__ == "__main__":
    unittest.main()
