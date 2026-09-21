import os
import sys
import unittest
from datetime import timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import collectors  # noqa: E402


class SystemTimezoneTests(unittest.TestCase):
    def test_tz_environment_selects_iana_zone(self):
        with patch.dict(os.environ, {"TZ": "Atlantic/Reykjavik"}):
            zone = collectors._system_timezone(Path("/nonexistent/localtime"))

        self.assertEqual(getattr(zone, "key", None), "Atlantic/Reykjavik")

    def test_reykjavik_day_is_midnight_to_midnight_utc(self):
        start, end = collectors._day_bounds(
            "2026-07-16", ZoneInfo("Atlantic/Reykjavik")
        )

        self.assertEqual(start.isoformat(), "2026-07-16T00:00:00+00:00")
        self.assertEqual(end.isoformat(), "2026-07-17T00:00:00+00:00")

    def test_paris_day_bounds_follow_dst(self):
        start, end = collectors._day_bounds(
            "2026-03-29", ZoneInfo("Europe/Paris")
        )

        self.assertEqual(start.utcoffset().total_seconds(), 3600)
        self.assertEqual(end.utcoffset().total_seconds(), 7200)
        self.assertEqual(
            end.astimezone(timezone.utc) - start.astimezone(timezone.utc),
            timedelta(hours=23),
        )

    def test_paris_autumn_day_has_25_hours(self):
        start, end = collectors._day_bounds(
            "2026-10-25", ZoneInfo("Europe/Paris")
        )

        self.assertEqual(start.utcoffset().total_seconds(), 7200)
        self.assertEqual(end.utcoffset().total_seconds(), 3600)
        self.assertEqual(
            end.astimezone(timezone.utc) - start.astimezone(timezone.utc),
            timedelta(hours=25),
        )

    def test_macos_localtime_symlink_resolves_iana_zone(self):
        localtime = Path("/etc/localtime")
        if "/zoneinfo/" not in str(localtime.resolve()):
            self.skipTest("macOS-style /etc/localtime zoneinfo symlink unavailable")

        with patch.dict(os.environ, {}, clear=True):
            zone = collectors._system_timezone(localtime)

        expected = str(localtime.resolve()).split("/zoneinfo/", 1)[1]
        self.assertEqual(getattr(zone, "key", None), expected)

    def test_naive_timestamp_uses_system_timezone(self):
        with patch.object(collectors, "_SYSTEM_TZ", ZoneInfo("Atlantic/Reykjavik")):
            parsed = collectors._parse_timestamp("2026-07-16T00:30:00")

        self.assertEqual(parsed.isoformat(), "2026-07-16T00:30:00+00:00")

    def test_aware_timestamp_preserves_its_offset(self):
        parsed = collectors._parse_timestamp("2026-07-16T00:30:00+02:00")

        self.assertEqual(parsed.isoformat(), "2026-07-16T00:30:00+02:00")


if __name__ == "__main__":
    unittest.main()
