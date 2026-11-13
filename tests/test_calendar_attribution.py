import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("daily_rollup_calendar", ROOT / "scripts" / "daily-rollup.py")
daily_rollup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daily_rollup)
sys.path.insert(0, str(ROOT / "scripts"))
import collectors

FT = daily_rollup.ACME_PROJECT
NON_FT = daily_rollup.NON_ACME_MEETING


def event(start, end, **facts):
    base = {"start": start, "end": end, "calendars": ["primary"], "all_day": False,
            "self_response": "accepted", "other_attendee_count": 1, "has_acme_attendee": False}
    base.update(facts)
    return base


def entry(time, project=None, **extra):
    return {"timestamp": f"2026-10-20T{time}", "canonical_project": project, "is_work": None, **extra}


def local(time):
    """An offset-aware ISO time for 2026-10-20 in this process's timezone."""
    from datetime import datetime
    return datetime.fromisoformat(f"2026-10-20T{time}").astimezone().isoformat()


class CalendarAttributionTests(unittest.TestCase):
    def attribute(self, entries, events):
        daily_rollup.apply_calendar_attribution(entries, events)
        return [(e["canonical_project"], e.get("calendar_attributed")) for e in entries]

    def test_acme_attendee_or_calendar_makes_it_acme(self):
        events = [event(local("10:00:00"), local("11:00:00"), has_acme_attendee=True),
                  event(local("12:00:00"), local("13:00:00"), calendars=["acme"])]
        entries = [entry("10:30:00", "ai-wow"), entry("12:30:00"), entry("14:00:00", "ai-wow")]
        self.assertEqual(self.attribute(entries, events),
                         [(FT, True), (FT, True), ("ai-wow", None)])
        self.assertTrue(entries[0]["is_work"])

    def test_other_events_are_not_acme_but_keep_another_screenshot_project(self):
        events = [event(local("10:00:00"), local("11:00:00"))]
        entries = [entry("10:00:00", FT), entry("10:10:00"), entry("10:20:00", "ai-wow")]
        self.assertEqual(self.attribute(entries, events),
                         [(NON_FT, True), (NON_FT, True), ("ai-wow", None)])

    def test_events_that_do_not_qualify(self):
        events = [event(local("10:00:00"), local("11:00:00"), all_day=True, has_acme_attendee=True),
                  event(local("10:00:00"), local("11:00:00"), self_response="declined", has_acme_attendee=True),
                  event(local("10:00:00"), local("11:00:00"), other_attendee_count=0, has_acme_attendee=True),
                  {"start": local("10:00:00"), "end": local("11:00:00"), "calendar": "acme"}]  # old sidecar
        self.assertEqual(self.attribute([entry("10:30:00", "ai-wow")], events), [("ai-wow", None)])

    def test_unanswered_invites_qualify(self):
        events = [event(local("10:00:00"), local("11:00:00"), self_response="needsAction", calendars=["acme"])]
        self.assertEqual(self.attribute([entry("10:30:00")], events), [(FT, True)])

    def test_conflicting_overlap_and_manual_tags_are_left_alone(self):
        events = [event(local("10:00:00"), local("11:00:00"), has_acme_attendee=True),
                  event(local("10:30:00"), local("11:30:00"))]
        entries = [entry("10:15:00"), entry("10:45:00", "ai-wow"), entry("11:15:00", FT),
                   entry("10:15:00", "ai-wow", manual_project="ai-wow")]
        self.assertEqual(self.attribute(entries, events),
                         [(FT, True), ("ai-wow", None), (NON_FT, True), ("ai-wow", None)])

    def test_utc_event_times_compare_in_local_time(self):
        from datetime import datetime, timezone
        start = datetime.fromisoformat("2026-10-20T10:00:00").astimezone(timezone.utc)
        end = datetime.fromisoformat("2026-10-20T11:00:00").astimezone(timezone.utc)
        events = [event(start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z"),
                        has_acme_attendee=True)]
        self.assertEqual(self.attribute([entry("10:59:00")], events), [(FT, True)])


class CollectorAttendanceTests(unittest.TestCase):
    def test_peter_and_rooms_are_not_other_attendees_but_his_acme_address_counts(self):
        facts = collectors._attendance({"attendees": [
            {"email": "you@acme.example", "self": True, "responseStatus": "needsAction"},
            {"email": "room@resource.calendar.google.com", "resource": True},
            {"email": "friend@gmail.com", "responseStatus": "accepted"},
        ]})
        self.assertEqual(facts, {"self_response": "needsAction", "other_attendee_count": 1,
                                 "has_acme_attendee": True})

    def test_merging_copies_from_two_calendars(self):
        first = {"calendars": ["primary"], "other_attendee_count": 1, "has_acme_attendee": False,
                 "self_response": "needsAction"}
        second = {"calendars": ["acme"], "other_attendee_count": 2, "has_acme_attendee": True,
                  "self_response": "accepted"}
        collectors._merge_event(first, second)
        self.assertEqual(first, {"calendars": ["acme", "primary"], "other_attendee_count": 2,
                                 "has_acme_attendee": True, "self_response": "accepted"})


if __name__ == "__main__":
    unittest.main()
