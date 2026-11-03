"""The unchanged-screen check compares with the previous kept capture."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import capture


class PreviousCaptureTests(unittest.TestCase):
    def test_current_and_unfinished_captures_are_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            captures = Path(tmp)
            from datetime import datetime
            day = captures / datetime.now().strftime("%Y-%m-%d")
            kept = day / "10-00-00"
            unfinished = day / "10-02-00"
            current = day / "10-04-00"
            for d in (kept, unfinished, current):
                d.mkdir(parents=True)
            (kept / "metadata.json").write_text("{}")
            (current / "metadata.json").write_text("{}")
            with patch.object(capture, "CAPTURES_DIR", captures):
                self.assertEqual(capture.get_previous_capture_dir(exclude=current), kept)

    def test_no_previous_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(capture, "CAPTURES_DIR", Path(tmp)):
                self.assertIsNone(capture.get_previous_capture_dir(exclude=Path(tmp) / "x"))

    def test_first_capture_is_not_unchanged(self):
        # Regression: the current capture used to be compared with itself,
        # so every capture was discarded as "screen unchanged".
        from datetime import datetime
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            captures = Path(tmp)
            current = captures / datetime.now().strftime("%Y-%m-%d") / "10-00-00"
            current.mkdir(parents=True)
            Image.new("RGB", (64, 64), "white").save(current / "screen-1.webp", "WebP")
            with patch.object(capture, "CAPTURES_DIR", captures):
                self.assertFalse(
                    capture.is_screen_similar_to_previous(current, ["screen-1.webp"], 0.02)
                )


if __name__ == "__main__":
    unittest.main()
