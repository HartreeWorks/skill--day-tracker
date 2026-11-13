import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import collectors  # noqa: E402


PARIS = ZoneInfo("Europe/Paris")


def _ts(year, month, day, hour, minute=0):
    return str(datetime(year, month, day, hour, minute, tzinfo=PARIS).timestamp())


class EventKeyTests(unittest.TestCase):
    def test_same_instant_in_different_offsets_matches(self):
        self.assertEqual(
            collectors._event_key("Call: Alice", "2026-09-29T14:00:00+02:00"),
            collectors._event_key("call: alice ", "2026-09-29T13:00:00+01:00"),
        )

    def test_all_day_date_is_kept_verbatim(self):
        self.assertEqual(collectors._event_key("Trip", "2026-09-29"), ("trip", "2026-09-29"))


class SlackSentTests(unittest.TestCase):
    def test_keeps_only_the_local_day(self):
        matches = [
            {"ts": _ts(2026, 9, 24, 23, 59), "channel": {"name": "client--example"}},
            {"ts": _ts(2026, 9, 25, 0, 1), "channel": {"name": "client--example"}, "text": "update"},
            {"ts": _ts(2026, 9, 26, 0, 0), "channel": {"name": "client--example"}},
        ]
        sent = collectors._slack_sent_from_matches(matches, "my-workspace", "2026-09-25", tz=PARIS)
        self.assertEqual([m["text"] for m in sent], ["update"])
        self.assertEqual(sent[0]["workspace"], "my-workspace")

    def test_dm_channel_resolves_through_lookup(self):
        matches = [{"ts": _ts(2026, 9, 25, 10), "channel": {"name": "U123", "is_im": True}}]
        sent = collectors._slack_sent_from_matches(
            matches, "my-workspace", "2026-09-25", names={"U123": "Bob"}, tz=PARIS
        )
        self.assertEqual(sent[0]["channel"], "DM Bob")


if __name__ == "__main__":
    unittest.main()
