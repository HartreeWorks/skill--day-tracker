import sys
import unittest
from unittest.mock import patch

import capture


def window(owner, bounds, *, pid=1, alpha=1.0, layer=0):
    """Build the Quartz-shaped window record consumed by the reader."""
    x1, y1, x2, y2 = bounds
    return {
        "kCGWindowOwnerName": owner,
        "kCGWindowOwnerPID": pid,
        "kCGWindowBounds": {
            "X": x1,
            "Y": y1,
            "Width": x2 - x1,
            "Height": y2 - y1,
        },
        "kCGWindowAlpha": alpha,
        "kCGWindowLayer": layer,
    }


class QuartzWindowReaderTests(unittest.TestCase):
    def _read(self, records, *, include_floating=False):
        calls = []

        class Quartz:
            kCGWindowListOptionOnScreenOnly = 0x01
            kCGWindowListExcludeDesktopElements = 0x02
            kCGNullWindowID = 0

            @staticmethod
            def CGWindowListCopyWindowInfo(option, relative_to_window):
                calls.append((option, relative_to_window))
                return records

        with patch.dict(sys.modules, {"Quartz": Quartz}):
            result = capture._cg_onscreen_windows(include_floating=include_floating)
        return result, calls

    def test_reader_uses_on_screen_desktop_exclusion_and_preserves_window_data(self):
        records = [
            window("Opaque", (10, 20, 40, 60), pid=11, alpha=1.0, layer=0),
            window("Faded", (0, 0, 20, 20), pid=12, alpha=0.35, layer=0),
            window("Transparent", (0, 0, 20, 20), pid=13, alpha=0.0, layer=0),
            window("Floating", (0, 0, 20, 20), pid=14, alpha=1.0, layer=3),
        ]

        result, calls = self._read(records)

        self.assertEqual(
            calls,
            [(
                QuartzWindowReaderTests._options(),
                0,
            )],
        )
        self.assertEqual([entry["owner"] for entry in result], ["Opaque", "Faded"])
        self.assertEqual(result[0]["pid"], 11)
        self.assertEqual(result[0]["bounds"], (10, 20, 40, 60))
        self.assertEqual(result[0]["alpha"], 1.0)
        self.assertEqual(result[0]["layer"], 0)
        self.assertEqual(result[1]["alpha"], 0.35)
        self.assertEqual(result[1]["layer"], 0)

    @staticmethod
    def _options():
        return 0x01 | 0x02

    def test_reader_includes_floating_windows_only_when_requested(self):
        records = [
            window("Regular", (0, 0, 50, 50), layer=0),
            window("Floating", (0, 0, 50, 50), pid=22, layer=3),
            window("MenuIcon", (0, 0, 50, 50), pid=23, layer=24),
            window("StatusIcon", (0, 0, 50, 50), pid=24, layer=25),
        ]

        default, _ = self._read(records)
        with_floating, _ = self._read(records, include_floating=True)

        self.assertEqual([entry["owner"] for entry in default], ["Regular"])
        self.assertEqual(
            [entry["owner"] for entry in with_floating],
            ["Regular", "Floating", "MenuIcon", "StatusIcon"],
        )
        self.assertEqual(
            {entry["owner"]: entry["layer"] for entry in with_floating},
            {"Regular": 0, "Floating": 3, "MenuIcon": 24, "StatusIcon": 25},
        )


class VisibleAppsTests(unittest.TestCase):
    DISPLAY = [(1, 0, 0, 100, 100)]

    def _visible(self, windows, displays=None):
        if displays is None:
            displays = self.DISPLAY
        with patch("capture._get_display_rects", return_value=displays), \
             patch("capture._cg_onscreen_windows", return_value=windows):
            return capture.get_visible_apps()

    def test_front_to_back_exposure_and_owner_deduplication(self):
        apps = self._visible([
            {
                "owner": "Front",
                "pid": 10,
                "bounds": (0, 0, 50, 100),
                "alpha": 1.0,
                "layer": 0,
            },
            {
                "owner": "Back",
                "pid": 11,
                "bounds": (0, 0, 100, 100),
                "alpha": 1.0,
                "layer": 0,
            },
            {
                "owner": "Back",
                "pid": 11,
                "bounds": (20, 20, 40, 40),
                "alpha": 1.0,
                "layer": 0,
            },
        ])

        self.assertEqual(apps, ["Front", "Back"])

    def test_fully_covered_windows_are_omitted(self):
        apps = self._visible([
            {
                "owner": "Front",
                "pid": 10,
                "bounds": (0, 0, 100, 100),
                "alpha": 1.0,
                "layer": 0,
            },
            {
                "owner": "Buried",
                "pid": 11,
                "bounds": (0, 0, 100, 100),
                "alpha": 1.0,
                "layer": 0,
            },
        ])

        self.assertEqual(apps, ["Front"])

    def test_translucent_windows_are_visible_but_do_not_occlude_beneath(self):
        apps = self._visible([
            {
                "owner": "Translucent",
                "pid": 10,
                "bounds": (0, 0, 100, 100),
                "alpha": 0.5,
                "layer": 3,
            },
            {
                "owner": "Underlying",
                "pid": 11,
                "bounds": (0, 0, 100, 100),
                "alpha": 1.0,
                "layer": 0,
            },
        ])

        self.assertEqual(apps, ["Translucent", "Underlying"])

    def test_system_ui_and_status_icon_owners_are_omitted_but_still_occlude(self):
        system_owners = [
            "Window Server",
            "WindowServer",
            "Dock",
            "SystemUIServer",
            "Control Center",
            "Control Centre",
            "Notification Center",
            "Notification Centre",
        ]

        for owner in system_owners:
            with self.subTest(owner=owner):
                self.assertEqual(
                    self._visible([
                        {
                            "owner": owner,
                            "pid": 1,
                            "bounds": (0, 0, 100, 100),
                            "alpha": 1.0,
                            "layer": 3,
                        },
                        {
                            "owner": "Underlying",
                            "pid": 2,
                            "bounds": (0, 0, 100, 100),
                            "alpha": 1.0,
                            "layer": 0,
                        },
                    ]),
                    [],
                )

        self.assertEqual(
            self._visible([
                {
                    "owner": "MenuBarApp",
                    "pid": 3,
                    "bounds": (0, 0, 100, 20),
                    "alpha": 1.0,
                    "layer": 24,
                },
                {
                    "owner": "StatusIconApp",
                    "pid": 4,
                    "bounds": (0, 0, 100, 20),
                    "alpha": 1.0,
                    "layer": 25,
                },
                {
                    "owner": "RegularFloating",
                    "pid": 5,
                    "bounds": (0, 0, 100, 100),
                    "alpha": 1.0,
                    "layer": 3,
                },
            ]),
            ["RegularFloating"],
        )

    def test_zero_alpha_and_off_display_windows_are_omitted(self):
        apps = self._visible([
            {
                "owner": "Transparent",
                "pid": 1,
                "bounds": (0, 0, 100, 100),
                "alpha": 0.0,
                "layer": 0,
            },
            {
                "owner": "OffDisplay",
                "pid": 2,
                "bounds": (200, 0, 300, 100),
                "alpha": 1.0,
                "layer": 0,
            },
            {
                "owner": "Visible",
                "pid": 3,
                "bounds": (0, 0, 10, 10),
                "alpha": 1.0,
                "layer": 0,
            },
        ])

        self.assertEqual(apps, ["Visible"])

    def test_windows_on_negative_coordinate_displays_are_visible_once(self):
        displays = [
            (1, 0, 0, 100, 100),
            (2, -100, 0, 100, 100),
        ]
        apps = self._visible([
            {
                "owner": "NegativeDisplayApp",
                "pid": 1,
                "bounds": (-100, 0, 0, 100),
                "alpha": 1.0,
                "layer": 0,
            },
            {
                "owner": "PositiveDisplayApp",
                "pid": 2,
                "bounds": (0, 0, 100, 100),
                "alpha": 1.0,
                "layer": 0,
            },
            {
                "owner": "OffDisplayApp",
                "pid": 3,
                "bounds": (100, 0, 200, 100),
                "alpha": 1.0,
                "layer": 0,
            },
        ], displays)

        self.assertEqual(apps, ["NegativeDisplayApp", "PositiveDisplayApp"])

    def test_display_or_window_query_failure_returns_empty_without_running_app_fallback(self):
        with patch("capture._get_display_rects", side_effect=RuntimeError("Quartz unavailable")), \
             patch("capture._cg_onscreen_windows") as read_windows, \
             patch("capture.subprocess.run") as run:
            self.assertEqual(capture.get_visible_apps(), [])
        read_windows.assert_not_called()
        run.assert_not_called()

        with patch("capture._get_display_rects", return_value=self.DISPLAY), \
             patch("capture._cg_onscreen_windows", side_effect=RuntimeError("window list failed")), \
             patch("capture.subprocess.run") as run:
            self.assertEqual(capture.get_visible_apps(), [])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
