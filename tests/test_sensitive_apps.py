import unittest
from unittest.mock import patch

import capture


def win(owner, bounds, *, alpha=1.0, layer=0):
    return {"owner": owner, "pid": 1, "bounds": bounds, "alpha": alpha, "layer": layer}


class SensitiveAppDisplayTests(unittest.TestCase):
    DISPLAYS = [(1, 0, 0, 100, 100), (2, 100, 0, 100, 100)]
    OWNERS = ["1Password", "Keychain Access"]

    def _detect(self, windows, owners=None):
        with patch("capture._get_display_rects", return_value=self.DISPLAYS), \
             patch("capture._cg_onscreen_windows", return_value=windows):
            return capture.get_sensitive_app_display_numbers(owners or self.OWNERS)

    def test_unfocused_visible_window_flags_only_its_display(self):
        covered = self._detect([
            win("Google Chrome", (0, 0, 100, 100)),
            win("1Password", (120, 20, 180, 80)),
        ])

        self.assertEqual(covered, {2})

    def test_window_spanning_displays_flags_both(self):
        self.assertEqual(self._detect([win("1Password", (50, 0, 150, 100))]), {1, 2})

    def test_fully_buried_window_is_ignored(self):
        covered = self._detect([
            win("Warp", (100, 0, 200, 100)),
            win("1Password", (120, 20, 180, 80)),
        ])

        self.assertEqual(covered, set())

    def test_small_exposed_sliver_counts(self):
        covered = self._detect([
            win("Warp", (100, 0, 200, 100)),
            win("1Password", (95, 20, 180, 80)),
        ])

        self.assertEqual(covered, {1})

    def test_floating_quick_access_panel_counts_menu_bar_icon_does_not(self):
        self.assertEqual(self._detect([win("1Password", (10, 10, 40, 40), layer=3)]), {1})
        self.assertEqual(self._detect([win("1Password", (10, 0, 30, 20), layer=25)]), set())

    def test_translucent_window_in_front_does_not_hide_it(self):
        covered = self._detect([
            win("Overlay", (0, 0, 100, 100), alpha=0.5, layer=3),
            win("1Password", (10, 10, 40, 40)),
        ])

        self.assertEqual(covered, {1})

    def test_owner_match_is_case_insensitive_substring(self):
        self.assertEqual(self._detect([win("1password 8", (10, 10, 40, 40))]), {1})
        self.assertEqual(self._detect([win("Notes", (10, 10, 40, 40))]), set())

    def test_empty_owner_list_disables_detection(self):
        with patch("capture._cg_onscreen_windows") as reader:
            self.assertEqual(capture.get_sensitive_app_display_numbers([]), set())
        reader.assert_not_called()

    def test_enumeration_failure_returns_none(self):
        with patch("capture._get_display_rects", side_effect=RuntimeError("quartz")):
            self.assertIsNone(capture.get_sensitive_app_display_numbers(self.OWNERS))


if __name__ == "__main__":
    unittest.main()
