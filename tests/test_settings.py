import unittest

from ledgepill.ui.settings import SettingsWindow


class SettingsFaceTest(unittest.TestCase):
    def test_cannot_disable_the_only_enabled_face(self):
        class Row:
            active = False

            def get_active(self):
                return self.active

            def set_active(self, active):
                self.active = active

        class Window:
            def __init__(self):
                self.cfg = {"pill_faces": ["clock"], "pill_face": "clock"}

            def _apply(self):
                raise AssertionError("unchanged face selection should not be saved")

        row = Row()
        window = Window()
        SettingsWindow._toggle_face(window, "clock", row)

        self.assertTrue(row.get_active())
        self.assertEqual(window.cfg["pill_faces"], ["clock"])
        self.assertEqual(window.cfg["pill_face"], "clock")


if __name__ == "__main__":
    unittest.main()
