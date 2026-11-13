import importlib.util
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "daily-rollup.py"
SPEC = importlib.util.spec_from_file_location("daily_rollup_coverage", SCRIPT_PATH)
daily_rollup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daily_rollup)

import capture
from models import ActiveWindow, Analysis, CaptureMetadata


PMSET = """\
2026-10-18 22:00:00 +0200 Notification        \tDisplay is turned off
2026-10-19 08:48:00 +0200 Notification        \tDisplay is turned on
2026-10-19 10:00:00 +0200 Notification        \tDisplay is turned off
2026-10-19 10:30:00 +0200 Wake                \tWake from Deep Idle
2026-10-19 10:30:00 +0200 Notification        \tDisplay is turned on
2026-10-19 11:00:00 +0200 Sleep               \tEntering Sleep state due to 'Idle Sleep'
2026-10-19 23:30:00 +0200 Notification        \tDisplay is turned on
2026-10-20 00:30:00 +0200 Notification        \tDisplay is turned off
"""


class DisplayOnTests(unittest.TestCase):
    def test_sums_intervals_and_clips_to_the_day(self):
        minutes = daily_rollup.display_on_minutes("2026-10-19", PMSET, now=datetime(2026, 10, 21))
        self.assertEqual(minutes, 72 + 30 + 30)

    def test_none_when_history_starts_after_the_day(self):
        self.assertIsNone(daily_rollup.display_on_minutes("2026-10-18", PMSET, now=datetime(2026, 10, 21)))

    def test_open_interval_runs_to_now(self):
        text = "2026-10-20 23:00:00 +0200 Notification \tDisplay is turned off\n" \
               "2026-10-21 09:00:00 +0200 Notification \tDisplay is turned on\n"
        minutes = daily_rollup.display_on_minutes("2026-10-21", text, now=datetime(2026, 10, 21, 10, 30))
        self.assertEqual(minutes, 90)

    def test_low_weekday_coverage_is_flagged(self):
        summary = {"total_tracked_minutes": 60}
        self.assertTrue(daily_rollup.add_coverage(summary, "2026-10-19", 480))  # Monday
        self.assertEqual(summary["coverage"], 0.12)
        self.assertEqual(summary["untracked_display_on_minutes"], 420)

    def test_weekend_and_unknown_display_time_are_not_flagged(self):
        self.assertFalse(daily_rollup.add_coverage({"total_tracked_minutes": 0}, "2026-10-18", 480))  # Sunday
        summary = {"total_tracked_minutes": 0}
        self.assertFalse(daily_rollup.add_coverage(summary, "2026-10-19", None))
        self.assertIsNone(summary["coverage"])


class SummaryTests(unittest.TestCase):
    def test_unknown_minutes_for_private_and_unanalysed(self):
        entries = [
            {"is_work": True, "category": "coding"},
            {"category": "coding"},
            {"is_work": False, "category": "social"},
            {"is_work": None, "category": "private"},
            {"is_work": None, "category": "unanalysed"},
        ]
        summary = daily_rollup.compute_summary(entries)
        self.assertEqual(summary["total_tracked_minutes"], 10)
        self.assertEqual(summary["work_minutes"], 4)
        self.assertEqual(summary["personal_minutes"], 2)
        self.assertEqual(summary["unknown_minutes"], 4)


def write_capture(day_dir, name, analysis):
    capture_dir = day_dir / name
    capture_dir.mkdir(parents=True)
    (capture_dir / "screen-1.webp").write_bytes(b"x")
    CaptureMetadata(
        timestamp=f"2026-10-18T{name.replace('-', ':')}",
        screens=["screen-1.webp"],
        active_window=ActiveWindow(app="Warp", title="day-tracker"),
        visible_apps=["Warp"],
        analysis=analysis,
    ).save(capture_dir / "metadata.json")
    return capture_dir


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.daily_dir = root / "daily"
        self.daily_dir.mkdir()
        self.day_dir = root / "captures" / "2026-10-18"

    def tearDown(self):
        self.tmp.cleanup()

    def test_failed_captures_are_retried_or_kept_as_unanalysed(self):
        analysed = Analysis(description="d", category="coding", oneline="Coding", is_work=True)
        write_capture(self.day_dir, "10-00-00", analysed)
        write_capture(self.day_dir, "10-02-00", None)               # old failure: no entry
        write_capture(self.day_dir, "10-04-00", capture.unanalysed_analysis())
        write_capture(self.day_dir, "10-06-00", None)               # fails again
        entries = [{"timestamp": "2026-10-18T10:00:00", "capture_dir": "2026-10-18/10-00-00",
                    "oneline": "Coding", "category": "coding", "is_work": True},
                   {"timestamp": "2026-10-18T10:04:00", "capture_dir": "2026-10-18/10-04-00",
                    "oneline": capture.UNANALYSED_ONELINE, "category": "unanalysed", "is_work": None}]

        def fake_reanalyse(capture_dir, config=None):
            if capture_dir.name == "10-06-00":
                return None
            metadata = CaptureMetadata.load(capture_dir / "metadata.json")
            metadata.analysis = Analysis(description="d", category="writing", oneline="Writing", is_work=True)
            return metadata

        with patch.object(daily_rollup, "DAILY_DIR", self.daily_dir), \
                patch.object(capture, "reanalyse_capture", side_effect=fake_reanalyse) as reanalyse:
            stats, touched = daily_rollup.recover_unanalysed("2026-10-18", entries)

        self.assertEqual(reanalyse.call_count, 3)
        self.assertEqual(stats, {"reanalysed": 2, "still_unanalysed": 1, "added": 2})
        self.assertEqual([e["capture_dir"][-8:] for e in entries], ["10-00-00", "10-02-00", "10-04-00", "10-06-00"])
        self.assertEqual([e["category"] for e in entries], ["coding", "writing", "writing", "unanalysed"])
        self.assertIsNone(entries[3]["is_work"])

    def test_no_capture_folder_is_a_no_op(self):
        entries = []
        with patch.object(daily_rollup, "DAILY_DIR", self.daily_dir):
            self.assertEqual(daily_rollup.recover_unanalysed("2026-10-18", entries), ({}, set()))
        self.assertEqual(entries, [])


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.daily_dir = self.root / "daily"
        self.daily_dir.mkdir()
        self.day_dir = self.root / "captures" / "2026-10-18"

    def tearDown(self):
        self.tmp.cleanup()

    def test_merge_keeps_concurrent_captures_and_rollup_changes(self):
        on_disk = [{"capture_dir": "d/1", "timestamp": "1", "category": "unanalysed"},
                   {"capture_dir": "d/2", "timestamp": "2", "category": "coding", "manual_project": "x"},
                   {"capture_dir": "d/3", "timestamp": "3", "category": "coding"}]  # written meanwhile
        ours = [{"capture_dir": "d/1", "timestamp": "1", "category": "writing"},
                {"capture_dir": "d/2", "timestamp": "2", "category": "coding"},
                {"capture_dir": "d/0", "timestamp": "0", "category": "coding"}]  # recovered
        merged = daily_rollup.merge_entries(on_disk, ours, {"d/1", "d/0"})
        self.assertEqual([e["capture_dir"] for e in merged], ["d/0", "d/1", "d/2", "d/3"])
        self.assertEqual(merged[1]["category"], "writing")
        self.assertEqual(merged[2]["manual_project"], "x")

    def test_manual_tag_beats_automatic_rules(self):
        entries = [{"inferred_project": "Podcatcher", "manual_project": "acme-project"}]
        projects = [{"folder": "acme-project", "name": "Acme Project"},
                    {"folder": "Podcatcher", "name": "Podcatcher"}]
        daily_rollup.apply_project_mapping(entries, projects)
        self.assertEqual(entries[0]["canonical_project"], "acme-project")

    def test_recovery_refreshes_tags_and_stops_after_repeated_failures(self):
        analysed = Analysis(description="d", category="coding", oneline="Coding", is_work=True)
        tagged = write_capture(self.day_dir, "09-00-00", analysed)
        metadata = CaptureMetadata.load(tagged / "metadata.json")
        metadata.manual_project = "ai-wow"
        metadata.save(tagged / "metadata.json")
        for minute in range(10):
            write_capture(self.day_dir, f"10-{minute:02d}-00", None)
        entries = [{"timestamp": "2026-10-18T09:00:00", "capture_dir": "2026-10-18/09-00-00",
                    "category": "coding", "is_work": True}]
        with patch.object(daily_rollup, "DAILY_DIR", self.daily_dir), \
                patch.object(capture, "reanalyse_capture", return_value=None) as reanalyse:
            stats, touched = daily_rollup.recover_unanalysed("2026-10-18", entries)
        self.assertEqual(reanalyse.call_count, daily_rollup.MAX_CONSECUTIVE_REANALYSIS_FAILURES)
        self.assertEqual(stats["retry_deferred"], 10 - daily_rollup.MAX_CONSECUTIVE_REANALYSIS_FAILURES)
        self.assertEqual(entries[0]["manual_project"], "ai-wow")
        self.assertEqual(len(entries), 11)
        self.assertEqual(len(touched), 11)

    def test_orphan_sweep_deletes_only_old_folders_without_metadata(self):
        old_orphan = self.day_dir / "08-00-00"
        new_orphan = self.day_dir / "08-02-00"
        for d in (old_orphan, new_orphan):
            d.mkdir(parents=True)
            (d / "screen-1.png").write_bytes(b"x")
        complete = write_capture(self.day_dir, "08-04-00", None)
        import os
        os.utime(old_orphan, (0, 0))
        os.utime(complete, (0, 0))
        with patch.object(capture, "log_capture_event"):
            self.assertEqual(capture.sweep_orphan_captures(self.day_dir), 1)
        self.assertFalse(old_orphan.exists())
        self.assertTrue(new_orphan.exists())
        self.assertTrue(complete.exists())

    def test_titles_scrubbed_when_a_display_was_private(self):
        from config import CaptureConfig
        config = CaptureConfig()
        window = ActiveWindow(app="Google Chrome", title="secret page")
        history = [{"app": "Google Chrome", "title": "secret page", "pct": 60},
                   {"app": "1Password", "title": "Bank login", "pct": 20},
                   {"app": "Warp", "title": "day-tracker", "pct": 20}]
        w, h = capture.scrub_private_titles(window, history, config, private_display_seen=False)
        self.assertEqual(w.title, "secret page")
        self.assertEqual([i["title"] for i in h], ["secret page", "", "day-tracker"])
        w, h = capture.scrub_private_titles(window, history, config, private_display_seen=True)
        self.assertEqual((w.app, w.title), ("Google Chrome", ""))
        self.assertTrue(all(i["title"] == "" for i in h))

    def test_capture_finishing_after_midnight_logs_to_its_own_day(self):
        captures = self.root / "captures"
        capture_dir = write_capture(captures / "2026-10-18", "23-59-50",
                                    Analysis(description="d", category="coding", oneline="Coding"))
        metadata = CaptureMetadata.load(capture_dir / "metadata.json")
        with patch.object(capture, "CAPTURES_DIR", captures), \
                patch.object(capture, "DAILY_DIR", self.daily_dir), \
                patch.object(capture, "HEARTBEAT_FILE", self.root / "heartbeat"):
            capture.update_daily_log(metadata, capture_dir)
            capture.update_daily_log(metadata, capture_dir)  # a second write replaces, not duplicates
        daily = json.loads((self.daily_dir / "2026-10-18.json").read_text())
        self.assertEqual(len(daily["entries"]), 1)


class PrivateEntryTests(unittest.TestCase):
    def test_private_entry_keeps_no_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            captures = Path(tmp) / "captures"
            capture_dir = captures / "2026-10-18" / "10-00-00"
            capture_dir.mkdir(parents=True)
            (capture_dir / "screen-1.webp").write_bytes(b"secret")
            with patch.object(capture, "CAPTURES_DIR", captures), \
                    patch.object(capture, "DAILY_DIR", Path(tmp) / "daily"), \
                    patch.object(capture, "HEARTBEAT_FILE", Path(tmp) / "heartbeat"):
                (Path(tmp) / "daily").mkdir()
                capture.record_private_entry("incognito", capture_dir, "2026-10-18T10:00:00")
                daily = json.loads(next((Path(tmp) / "daily").glob("*.json")).read_text())
            self.assertEqual(sorted(p.name for p in capture_dir.iterdir()), ["metadata.json"])
            entry = daily["entries"][0]
            self.assertEqual(entry["category"], "private")
            self.assertIsNone(entry["is_work"])
            self.assertIsNone(entry["window_title"])
            self.assertTrue((Path(tmp) / "heartbeat").exists())


if __name__ == "__main__":
    unittest.main()
