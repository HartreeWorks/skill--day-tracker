"""Window-list fallback for the active window."""

import json
import unittest
from unittest.mock import patch

import capture


def window(owner, name, layer=0):
    return {"kCGWindowOwnerName": owner, "kCGWindowName": name, "kCGWindowLayer": layer}


class FrontmostWindowQuartzTests(unittest.TestCase):
    def lookup(self, windows):
        import Quartz
        with patch.object(Quartz, "CGWindowListCopyWindowInfo", return_value=windows):
            return capture.get_frontmost_window_quartz()

    def test_first_normal_window_wins_over_overlays_and_menu_bar(self):
        active = self.lookup([
            window("Window Server", "StatusIndicator", layer=0),
            window("Control Centre", "", layer=25),
            window("1Password", "Vault - Personal"),
            window("Google Chrome", "Inbox"),
        ])
        self.assertEqual((active.app, active.title), ("1Password", "Vault - Personal"))
        self.assertTrue(capture.check_sensitive_window(active, capture.CaptureConfig()))

    def test_no_normal_window_gives_none(self):
        self.assertIsNone(self.lookup([window("Dock", "", layer=20)]))

    def test_helper_without_app_uses_the_window_list(self):
        class R:
            returncode = 0
            stdout = json.dumps({"app": "", "title": ""})
        fallback = capture.ActiveWindow(app="Keychain Access", title="login")
        with patch("capture.subprocess.run", return_value=R()), \
             patch("capture.get_visible_apps", return_value=[]), \
             patch("capture.get_frontmost_window_quartz", return_value=fallback), \
             patch("capture._should_log_throttled", return_value=False):
            active, _ = capture.get_window_info()
        self.assertEqual(active, fallback)


if __name__ == "__main__":
    unittest.main()
