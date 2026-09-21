import json
import unittest
from unittest.mock import patch

import capture


def helper_result(payload):
    """Fake a successful DayTrackerHelper invocation."""
    class R:
        returncode = 0
        stdout = json.dumps(payload)
    return R()


class IsOverlayTitleTests(unittest.TestCase):
    def test_recognises_screen_recording_indicator(self):
        self.assertTrue(capture.is_overlay_title("StatusIndicator"))
        self.assertTrue(capture.is_overlay_title("  StatusIndicator  "))

    def test_leaves_real_titles_alone(self):
        for title in ["aisa re-skin", "Status", "", "My StatusIndicator notes"]:
            self.assertFalse(capture.is_overlay_title(title))


class FrontmostWindowTitleTests(unittest.TestCase):
    def _run_with_windows(self, windows):
        fake = type("Q", (), {
            "CGWindowListCopyWindowInfo": staticmethod(lambda *a: windows),
            "kCGWindowListOptionOnScreenOnly": 1,
            "kCGWindowListExcludeDesktopElements": 2,
            "kCGNullWindowID": 0,
        })
        with patch.dict("sys.modules", {"Quartz": fake}):
            return capture.get_frontmost_window_title()

    def test_skips_window_server_overlay_and_returns_real_window(self):
        title = self._run_with_windows([
            {"kCGWindowLayer": 0, "kCGWindowName": "StatusIndicator",
             "kCGWindowOwnerName": "Window Server"},
            {"kCGWindowLayer": 0, "kCGWindowName": "aisa re-skin",
             "kCGWindowOwnerName": "Warp"},
        ])
        self.assertEqual(title, "aisa re-skin")

    def test_skips_any_window_server_window(self):
        title = self._run_with_windows([
            {"kCGWindowLayer": 0, "kCGWindowName": "Some Overlay",
             "kCGWindowOwnerName": "WindowServer"},
            {"kCGWindowLayer": 0, "kCGWindowName": "Inbox — Gmail",
             "kCGWindowOwnerName": "Google Chrome"},
        ])
        self.assertEqual(title, "Inbox — Gmail")

    def test_still_ignores_non_zero_layers(self):
        title = self._run_with_windows([
            {"kCGWindowLayer": 25, "kCGWindowName": "Item-0",
             "kCGWindowOwnerName": "Tailscale"},
            {"kCGWindowLayer": 0, "kCGWindowName": "notes.md",
             "kCGWindowOwnerName": "Typora"},
        ])
        self.assertEqual(title, "notes.md")

    def test_returns_empty_when_only_overlays_present(self):
        title = self._run_with_windows([
            {"kCGWindowLayer": 0, "kCGWindowName": "StatusIndicator",
             "kCGWindowOwnerName": "Window Server"},
        ])
        self.assertEqual(title, "")


class GetWindowInfoTests(unittest.TestCase):
    def test_overlay_title_from_helper_is_replaced_by_quartz(self):
        payload = {"app": "Google Chrome", "title": "StatusIndicator",
                   "visible_apps": "Google Chrome|||Finder|||"}
        with patch("capture.subprocess.run", return_value=helper_result(payload)), \
             patch("capture.get_frontmost_window_title", return_value="Inbox — Gmail"):
            active, visible = capture.get_window_info()
        self.assertEqual(active.app, "Google Chrome")
        self.assertEqual(active.title, "Inbox — Gmail")
        self.assertEqual(visible, ["Google Chrome", "Finder"])

    def test_overlay_title_falls_back_to_empty_not_wrong(self):
        """If Quartz also only sees the overlay, an empty title beats a wrong one."""
        payload = {"app": "Slack", "title": "StatusIndicator", "visible_apps": "Slack|||"}
        with patch("capture.subprocess.run", return_value=helper_result(payload)), \
             patch("capture.get_frontmost_window_title", return_value="StatusIndicator"):
            active, _ = capture.get_window_info()
        self.assertEqual(active.app, "Slack")
        self.assertEqual(active.title, "")

    def test_good_helper_title_is_preserved(self):
        payload = {"app": "stable", "title": "aisa re-skin", "visible_apps": "stable|||"}
        with patch("capture.subprocess.run", return_value=helper_result(payload)), \
             patch("capture.get_frontmost_window_title", return_value="SHOULD NOT BE USED"):
            active, _ = capture.get_window_info()
        self.assertEqual(active.title, "aisa re-skin")


class SensitivePreFilterTests(unittest.TestCase):
    """The point of the fix: a corrupted title must not mask a sensitive window."""

    def test_sensitive_pattern_matches_once_real_title_is_restored(self):
        from config import CaptureConfig
        from models import ActiveWindow
        config = CaptureConfig()

        masked = ActiveWindow(app="Google Chrome", title="StatusIndicator")
        self.assertFalse(capture.check_sensitive_window(masked, config))

        restored = ActiveWindow(app="Google Chrome", title="1Password — Personal Vault")
        self.assertTrue(capture.check_sensitive_window(restored, config))


if __name__ == "__main__":
    unittest.main()
