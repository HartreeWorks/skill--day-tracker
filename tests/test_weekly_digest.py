import importlib.util
import json
import tempfile
import unittest
from collections import Counter
from datetime import date
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "weekly-digest.py"
SPEC = importlib.util.spec_from_file_location("weekly_digest", SCRIPT_PATH)
weekly_digest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(weekly_digest)


class WeeklyDigestTests(unittest.TestCase):
    def test_project_aliases_merge_without_changing_total_minutes(self):
        totals = weekly_digest.canonicalise_project_totals(
            {"Podcatcher": 30, "podcatcher": 8, "podcatch": 2, "ai-wow": 60},
            {
                "podcatcher": "Podcatcher",
                "podcatch": "Podcatcher",
                "ai-wow": "ai-wow",
            },
        )

        self.assertEqual(totals, {"Podcatcher": 40, "ai-wow": 60})
        self.assertEqual(sum(totals.values()), 100)

    def test_agent_session_projects_are_aggregated_separately_from_time(self):
        completions = [
            {
                "agent_sessions": {
                    "chat_count": 4,
                    "interactive_roots_started": 4,
                    "total_session_count": 4,
                    "by_project": {
                        "Podcatcher": {"chat_count": 3},
                        "infra": {"chat_count": 1},
                    },
                    "interactive_roots_by_project": {
                        "Podcatcher": {"chat_count": 3},
                        "infra": {"chat_count": 1},
                    },
                }
            },
            {
                "agent_sessions": {
                    "chat_count": 2,
                    "interactive_roots_started": 2,
                    "total_session_count": 2,
                    "by_project": {"Podcatcher": {"chat_count": 2}},
                    "interactive_roots_by_project": {
                        "Podcatcher": {"chat_count": 2}
                    },
                }
            },
        ]

        totals = weekly_digest.aggregate_completions(
            completions, {"podcatcher": "Podcatcher", "podcatch": "Podcatcher"}
        )

        self.assertEqual(totals["agent_session_count"], 6)
        self.assertEqual(
            totals["agent_sessions_by_project"], Counter({"Podcatcher": 5, "infra": 1})
        )

    def test_legacy_sidecars_exclude_subagents_when_session_provenance_exists(self):
        completions = [{
            "agent_sessions": {
                "chat_count": 4,
                "by_project": {"wrong-total": {"chat_count": 4}},
                "sessions": [
                    {
                        "project": "Podcatcher",
                        "session_origin": "interactive_root",
                        "started_on_target_date": True,
                    },
                    {
                        "project": "Podcatcher",
                        "session_origin": "interactive_root",
                        "started_on_target_date": False,
                    },
                    {
                        "project": "infra",
                        "session_origin": "subagent",
                        "started_on_target_date": True,
                    },
                    {
                        "project": "infra",
                        "session_origin": "unknown",
                        "started_on_target_date": True,
                    },
                ],
            }
        }]

        totals = weekly_digest.aggregate_completions(completions)

        self.assertEqual(totals["agent_session_count"], 1)
        self.assertEqual(totals["total_agent_session_count"], 4)
        self.assertEqual(
            totals["agent_sessions_by_project"], Counter({"Podcatcher": 1})
        )

    def test_provenance_free_legacy_sidecars_are_reported_as_unavailable(self):
        completions = [{
            "agent_sessions": {
                "chat_count": 4,
                "by_project": {"inflated-project": {"chat_count": 4}},
                "sessions": [
                    {"project": "Podcatcher", "agent": "claude"},
                    {"project": "infra", "agent": "claude"},
                    {"project": "infra", "agent": "codex"},
                    {"project": "infra", "agent": "codex"},
                ],
            }
        }]

        totals = weekly_digest.aggregate_completions(completions)

        self.assertEqual(totals["agent_session_count"], 0)
        self.assertEqual(totals["agent_sessions_by_project"], Counter())
        self.assertEqual(totals["legacy_agent_session_count"], 4)
        self.assertEqual(totals["legacy_agent_session_days"], 1)

        rendered = weekly_digest.render_table(
            "2026-W30", date(2026, 7, 20), [], {}, [], totals
        )
        self.assertIn("count unavailable for 1 legacy day(s)", rendered)
        self.assertIn("4 session records lack provenance", rendered)

        payload = json.loads(
            weekly_digest.render_json(
                "2026-W30", date(2026, 7, 20), [], {}, [], totals
            )
        )["completions"]
        self.assertIsNone(payload["interactive_roots_started"])
        self.assertIsNone(payload["interactive_roots_by_project"])
        self.assertEqual(payload["known_interactive_roots_started"], 0)

    def test_agent_session_aliases_are_canonicalised(self):
        completions = [
            {
                "agent_sessions": {
                    "chat_count": 2,
                    "interactive_roots_started": 2,
                    "total_session_count": 2,
                    "by_project": {
                        "podcatcher": {"chat_count": 1},
                        "podcatch": {"chat_count": 1},
                    },
                    "interactive_roots_by_project": {
                        "podcatcher": {"chat_count": 1},
                        "podcatch": {"chat_count": 1},
                    },
                }
            }
        ]

        totals = weekly_digest.aggregate_completions(
            completions, {"podcatcher": "Podcatcher", "podcatch": "Podcatcher"}
        )

        self.assertEqual(
            totals["agent_sessions_by_project"], Counter({"Podcatcher": 2})
        )

    def test_stale_summary_falls_back_to_all_raw_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            daily_dir = Path(temp_dir)
            (daily_dir / "2026-07-22.json").write_text(
                json.dumps(
                    {
                        "date": "2026-07-22",
                        "entries": [
                            {"inferred_project": "Podcatcher"},
                            {"inferred_project": "Podcatcher"},
                        ],
                        "summary": {
                            "total_tracked_minutes": 2,
                            "by_project": {"wrong-project": 2},
                            "low_data": True,
                        },
                    }
                )
            )

            with patch.object(weekly_digest, "DAILY_DIR", daily_dir):
                by_project, num_entries, low_data = weekly_digest.load_daily_data(
                    date(2026, 7, 22), {"podcatcher": "Podcatcher"}
                )

            self.assertEqual(by_project, {"Podcatcher": 4})
            self.assertEqual(num_entries, 2)
            self.assertTrue(low_data)

    def test_equal_count_stale_summary_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            daily_dir = Path(temp_dir)
            (daily_dir / "2026-07-22.json").write_text(
                json.dumps(
                    {
                        "date": "2026-07-22",
                        "entries": [
                            {"canonical_project": "Podcatcher"},
                            {"canonical_project": "Podcatcher"},
                        ],
                        "summary": {
                            "total_tracked_minutes": 4,
                            "by_project": {"wrong-project": 4},
                            "low_data": True,
                        },
                    }
                )
            )

            with patch.object(weekly_digest, "DAILY_DIR", daily_dir):
                by_project, _, _ = weekly_digest.load_daily_data(
                    date(2026, 7, 22), {"podcatcher": "Podcatcher"}
                )

            self.assertEqual(by_project, {"Podcatcher": 4})

    def test_agent_only_completions_are_rendered(self):
        comp_totals = {
            "git_commit_count": 0,
            "git_commits_by_repo": Counter(),
            "agent_session_count": 2,
            "agent_sessions_by_project": Counter({"Podcatcher": 2}),
            "calendar_event_count": 0,
            "emails_sent_count": 0,
            "google_docs_edited_count": 0,
            "google_docs_edited": [],
        }

        rendered = weekly_digest.render_table(
            "2026-W30", date(2026, 7, 20), [], {}, [], comp_totals
        )

        self.assertIn("Completions:", rendered)
        self.assertIn("Interactive conversations started: 2", rendered)
        self.assertIn("Podcatcher: 2", rendered)
        self.assertNotIn("Git commits:", rendered)

        payload = json.loads(
            weekly_digest.render_json(
                "2026-W30", date(2026, 7, 20), [], {}, [], comp_totals
            )
        )
        self.assertEqual(payload["completions"]["interactive_roots_started"], 2)
        self.assertEqual(
            payload["completions"]["interactive_roots_by_project"],
            {"Podcatcher": 2},
        )


if __name__ == "__main__":
    unittest.main()
