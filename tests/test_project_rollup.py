import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "daily-rollup.py"
SPEC = importlib.util.spec_from_file_location("daily_rollup", SCRIPT_PATH)
daily_rollup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daily_rollup)

from config import CaptureConfig
from models import ActiveWindow, Analysis, CaptureMetadata, DailyLog


PROJECTS = [
    {
        "folder": "Podcatcher",
        "name": "Podcatcher",
        "aliases": ["podcatch", "podcatch srs"],
    },
    {"folder": "roughdraft-ph", "name": "Roughdraft"},
]


class ProjectRollupTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            project_patterns=[
                {"pattern": r"\bpodcatch(?:er)?\b", "project": "Podcatcher"}
            ],
            app_rules=[
                {
                    "app": "Signal",
                    "category": "social",
                    "project": None,
                    "is_work": False,
                }
            ],
        )

    def test_legacy_agent_sessions_without_provenance_are_unknown(self):
        roots_started, total_records = daily_rollup.agent_session_counts({
            "chat_count": 3,
            "sessions": [{"agent": "claude"}, {"agent": "codex"}, {"agent": "codex"}],
        })

        self.assertIsNone(roots_started)
        self.assertEqual(total_records, 3)

    def test_active_window_pattern_overrides_incorrect_ai_project(self):
        entries = [
            {
                "timestamp": "2026-07-20T13:45:00",
                "active_app": "Cursor",
                "window_title": "Podcatcher notes — README",
                "inferred_project": "roughdraft-ph",
                "category": "coding",
                "is_work": True,
            }
        ]

        daily_rollup.apply_project_mapping(entries, PROJECTS, self.config)

        self.assertEqual(entries[0]["canonical_project"], "Podcatcher")

    def test_exact_terminal_title_overrides_incorrect_ai_project(self):
        entries = [
            {
                "timestamp": "2026-07-20T13:45:00",
                "active_app": "stable",
                "window_title": "roughdraft-ph",
                "inferred_project": "Podcatcher",
                "category": "coding",
                "is_work": True,
            }
        ]

        daily_rollup.apply_project_mapping(entries, PROJECTS, self.config)

        self.assertEqual(entries[0]["canonical_project"], "roughdraft-ph")

    def test_personal_app_rule_suppresses_but_preserves_ai_evidence(self):
        entries = [
            {
                "timestamp": "2026-07-20T14:43:00",
                "active_app": "Signal",
                "window_title": "Signal",
                "inferred_project": "Podcatcher",
                "category": "coding",
                "is_work": True,
            }
        ]

        daily_rollup.apply_project_mapping(entries, PROJECTS, self.config)
        summary = daily_rollup.compute_summary(entries)

        self.assertEqual(entries[0]["inferred_project"], "Podcatcher")
        self.assertTrue(entries[0]["project_suppressed"])
        self.assertEqual(entries[0]["category"], "social")
        self.assertFalse(entries[0]["is_work"])
        self.assertEqual(summary["by_project"], {"unclassified": 2})

    def test_default_personal_app_rule_suppresses_ai_project(self):
        entries = [
            {
                "timestamp": "2026-07-20T14:43:00",
                "active_app": "Signal",
                "window_title": "Signal",
                "inferred_project": "Podcatcher",
                "category": "coding",
                "is_work": True,
            }
        ]

        daily_rollup.apply_project_mapping(entries, PROJECTS, CaptureConfig())

        self.assertTrue(entries[0]["project_suppressed"])
        self.assertIsNone(entries[0]["canonical_project"])

    def test_suppressed_entry_is_not_gap_filled(self):
        entries = [
            {"timestamp": "2026-07-20T14:40:00", "canonical_project": "Podcatcher"},
            {
                "timestamp": "2026-07-20T14:42:00",
                "canonical_project": None,
                "project_suppressed": True,
            },
            {"timestamp": "2026-07-20T14:44:00", "canonical_project": "Podcatcher"},
        ]

        daily_rollup.fill_gaps(entries)

        self.assertIsNone(entries[1]["canonical_project"])
        self.assertNotIn("project_gap_filled", entries[1])

    def test_aliases_and_case_variants_map_to_canonical_folder(self):
        aliases = daily_rollup.build_alias_map(PROJECTS)

        for label in ["Podcatcher", "podcatcher", "podcatch", "podcatch srs"]:
            canonical, _ = daily_rollup.fuzzy_match_project(label, aliases, PROJECTS)
            self.assertEqual(canonical, "Podcatcher")

        canonical, _ = daily_rollup.fuzzy_match_project("other", aliases, PROJECTS)
        self.assertIsNone(canonical)

    def test_skip_completions_preserves_existing_sidecar(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            daily_dir = Path(temp_dir)
            date_str = "2026-07-20"
            daily_file = daily_dir / f"{date_str}.json"
            sidecar = daily_dir / f"{date_str}.completions.json"
            daily_file.write_text(
                json.dumps(
                    {
                        "date": date_str,
                        "entries": [
                            {
                                "timestamp": f"{date_str}T13:45:00",
                                "active_app": "stable",
                                "window_title": "podcatcher",
                                "inferred_project": "roughdraft-ph",
                                "category": "coding",
                                "is_work": True,
                            }
                        ],
                    }
                )
            )
            original_sidecar = {
                "git_commits": [{"repo": "Podcatcher"}],
                "agent_sessions": {
                    "chat_count": 3,
                    "by_project": {},
                    "sessions": [
                        {
                            "session_origin": "interactive_root",
                            "started_on_target_date": True,
                        },
                        {
                            "session_origin": "subagent",
                            "started_on_target_date": True,
                        },
                    ],
                },
            }
            sidecar.write_text(json.dumps(original_sidecar))

            with (
                patch.object(daily_rollup, "DAILY_DIR", daily_dir),
                patch.object(daily_rollup, "load_projects", return_value=PROJECTS),
                patch("config.load_config", return_value=self.config),
            ):
                daily_rollup.run_rollup(date_str, collect_completions=False)

            self.assertEqual(json.loads(sidecar.read_text()), original_sidecar)
            refreshed = json.loads(daily_file.read_text())
            self.assertEqual(refreshed["summary"]["by_project"], {"Podcatcher": 2})
            self.assertEqual(refreshed["summary"]["git_commit_count"], 1)
            self.assertEqual(refreshed["summary"]["agent_session_count"], 1)
            self.assertEqual(refreshed["summary"]["total_agent_session_count"], 2)

    def test_skip_completions_does_not_resend_preserved_errors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            daily_dir = Path(temp_dir)
            date_str = "2026-07-20"
            (daily_dir / f"{date_str}.json").write_text(
                json.dumps({"date": date_str, "entries": []})
            )
            (daily_dir / f"{date_str}.completions.json").write_text(
                json.dumps({"_errors": ["old collector failure"]})
            )

            with (
                patch.object(daily_rollup, "DAILY_DIR", daily_dir),
                patch.object(daily_rollup, "send_alert") as send_alert,
            ):
                daily_rollup.run_rollup(date_str, collect_completions=False)

            send_alert.assert_not_called()

    def test_capture_round_trip_preserves_rollup_annotations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            daily_file = Path(temp_dir) / "2026-07-20.json"
            daily_file.write_text(
                json.dumps(
                    {
                        "date": "2026-07-20",
                        "entries": [
                            {
                                "timestamp": "2026-07-20T14:40:00",
                                "capture_dir": "/tmp/first",
                                "oneline": "Podcatcher work",
                                "category": "coding",
                                "inferred_project": "roughdraft-ph",
                                "canonical_project": "Podcatcher",
                                "project_gap_filled": True,
                            },
                            {
                                "timestamp": "2026-07-20T14:42:00",
                                "capture_dir": "/tmp/second",
                                "oneline": "Signal message",
                                "category": "social",
                                "inferred_project": "Podcatcher",
                                "project_suppressed": True,
                            },
                        ],
                    }
                )
            )
            daily_log = DailyLog.load(daily_file)
            daily_log.add_entry(
                CaptureMetadata(
                    timestamp="2026-07-20T14:44:00",
                    screens=[],
                    active_window=ActiveWindow(app="Cursor", title="Podcatcher"),
                    visible_apps=["Cursor"],
                    analysis=Analysis(
                        description="Coding",
                        category="coding",
                        oneline="More Podcatcher work",
                    ),
                ),
                "/tmp/third",
            )
            daily_log.save(daily_file)

            saved_entries = json.loads(daily_file.read_text())["entries"]
            self.assertEqual(saved_entries[0]["canonical_project"], "Podcatcher")
            self.assertTrue(saved_entries[0]["project_gap_filled"])
            self.assertTrue(saved_entries[1]["project_suppressed"])


if __name__ == "__main__":
    unittest.main()
