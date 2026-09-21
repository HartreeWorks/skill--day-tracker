import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import collectors  # noqa: E402


def _write_jsonl(path: Path, entries: list[dict], mtime: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    epoch = datetime.fromisoformat(mtime.replace("Z", "+00:00")).timestamp()
    os.utime(path, (epoch, epoch))


class AgentSessionDateSlicingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.projects_dir = self.root / "claude-projects"
        self.codex_dir = self.root / "codex-sessions"
        self.daily_dir = self.root / "daily"
        self.summary_dir = self.root / "summaries"
        self.gate_events_dir = self.root / "gate-events"
        self.gate_sessions_dir = self.root / "gate-sessions"
        self.zone = ZoneInfo("Atlantic/Reykjavik")
        self.patches = [
            patch.object(collectors, "PROJECTS_DIR", self.projects_dir),
            patch.object(collectors, "CODEX_SESSIONS_DIR", self.codex_dir),
            patch.object(collectors, "DAY_TRACKER_DAILY_DIR", self.daily_dir),
            patch.object(collectors, "SESSION_SUMMARY_DIR", self.summary_dir),
            patch.object(collectors, "TASK_INIT_GATE_EVENTS_DIR", self.gate_events_dir),
            patch.object(collectors, "TASK_INIT_GATE_SESSIONS_DIR", self.gate_sessions_dir),
            patch.object(collectors, "_SYSTEM_TZ", self.zone),
            patch.dict(os.environ, {"DAY_TRACKER_DISABLE_SESSION_SUMMARIES": "1"}),
        ]
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.temp_dir.cleanup()

    def test_claude_session_is_sliced_across_both_dates(self):
        session_path = self.projects_dir / "project" / "session.jsonl"
        _write_jsonl(
            session_path,
            [
                {
                    "type": "user",
                    "sessionId": "claude-cross-midnight",
                    "cwd": "/tmp/project",
                    "timestamp": "2026-07-15T23:50:00Z",
                    "isSidechain": False,
                    "agentId": None,
                    "entrypoint": "cli",
                    "message": {"content": "Wednesday request"},
                },
                {
                    "type": "assistant",
                    "sessionId": "claude-cross-midnight",
                    "cwd": "/tmp/project",
                    "timestamp": "2026-07-16T00:10:00Z",
                    "message": {"content": "Thursday response"},
                },
            ],
            "2026-07-16T00:10:00Z",
        )

        wednesday = collectors.collect_agent_sessions("2026-07-15")
        thursday = collectors.collect_agent_sessions("2026-07-16")

        self.assertEqual(wednesday["chat_count"], 1)
        self.assertEqual(thursday["chat_count"], 0)
        self.assertEqual(thursday["interactive_root_count"], 1)
        self.assertEqual(thursday["total_session_count"], 1)
        self.assertEqual(wednesday["sessions"][0]["message_count"], 1)
        self.assertEqual(thursday["sessions"][0]["message_count"], 1)
        self.assertEqual(wednesday["sessions"][0]["recent_user_turns"], ["Wednesday request"])
        self.assertEqual(thursday["sessions"][0]["recent_assistant_turns"], ["Thursday response"])
        self.assertEqual(wednesday["sessions"][0]["session_origin"], "interactive_root")
        self.assertTrue(wednesday["sessions"][0]["started_on_target_date"])
        self.assertFalse(thursday["sessions"][0]["started_on_target_date"])

    def test_resumed_codex_session_appears_on_next_date(self):
        session_path = (
            self.codex_dir / "2026" / "07" / "15" / "rollout-cross-midnight.jsonl"
        )
        _write_jsonl(
            session_path,
            [
                {
                    "type": "session_meta",
                    "timestamp": "2026-07-15T23:45:00Z",
                    "payload": {
                        "id": "codex-cross-midnight",
                        "cwd": "/tmp/codex-project",
                        "timestamp": "2026-07-15T23:45:00Z",
                        "source": "cli",
                        "originator": "codex-tui",
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-15T23:50:00Z",
                    "payload": {"type": "user_message", "message": "Wednesday Codex request"},
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-16T00:20:00Z",
                    "payload": {"type": "agent_message", "message": "Thursday Codex response"},
                },
            ],
            "2026-07-16T00:20:00Z",
        )

        thursday = collectors.collect_agent_sessions("2026-07-16")

        self.assertEqual(thursday["chat_count"], 0)
        self.assertEqual(thursday["interactive_root_count"], 1)
        self.assertEqual(thursday["total_session_count"], 1)
        self.assertEqual(thursday["sessions"][0]["agent"], "codex")
        self.assertEqual(thursday["sessions"][0]["message_count"], 1)
        self.assertEqual(
            thursday["sessions"][0]["recent_assistant_turns"],
            ["Thursday Codex response"],
        )
        self.assertFalse(thursday["sessions"][0]["started_on_target_date"])

    def test_gate_after_midnight_is_attributed_to_start_date(self):
        session_path = self.codex_dir / "2026" / "07" / "27" / "rollout-midnight-gate.jsonl"
        _write_jsonl(
            session_path,
            [
                {
                    "type": "session_meta",
                    "timestamp": "2026-07-27T23:59:00Z",
                    "payload": {
                        "id": "codex-midnight-gate",
                        "cwd": "/tmp/project",
                        "timestamp": "2026-07-27T23:59:00Z",
                        "source": "cli",
                        "originator": "codex-tui",
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-27T23:59:30Z",
                    "payload": {"type": "user_message", "message": "Late task"},
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-28T00:00:01Z",
                    "payload": {"type": "agent_message", "message": "**5-minute gate**\nProceed?"},
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-28T00:00:30Z",
                    "payload": {"type": "user_message", "message": "trim: stop after one file"},
                },
            ],
            "2026-07-28T00:00:30Z",
        )
        self.gate_events_dir.mkdir(parents=True)
        (self.gate_events_dir / "2026-07-27.jsonl").write_text(json.dumps({
            "session_id": "codex-midnight-gate",
            "disposition": "injected",
        }) + "\n")

        monday = collectors.collect_agent_sessions("2026-07-27")
        session = monday["sessions"][0]

        self.assertTrue(session["started_on_target_date"])
        self.assertTrue(session["gate_shown"])
        self.assertEqual(session["gate_outcome"], "trimmed")

    def test_identical_claude_turns_are_not_deduplicated(self):
        session_path = self.projects_dir / "project" / "repeated.jsonl"
        _write_jsonl(
            session_path,
            [
                {
                    "type": "user",
                    "sessionId": "claude-repeated",
                    "cwd": "/tmp/project",
                    "timestamp": "2026-07-27T08:00:10Z",
                    "isSidechain": False,
                    "entrypoint": "cli",
                    "message": {"content": [{"type": "tool_result", "content": "tool output"}]},
                },
                {
                    "type": "assistant",
                    "sessionId": "claude-repeated",
                    "cwd": "/tmp/project",
                    "timestamp": "2026-07-27T08:00:20Z",
                    "isSidechain": False,
                    "entrypoint": "cli",
                    "message": {"id": "msg-1", "content": [{"type": "text", "text": "Working"}]},
                },
                {
                    "type": "assistant",
                    "sessionId": "claude-repeated",
                    "cwd": "/tmp/project",
                    "timestamp": "2026-07-27T08:00:21Z",
                    "isSidechain": False,
                    "entrypoint": "cli",
                    "message": {"id": "msg-1", "content": [{"type": "text", "text": "Working"}]},
                },
                {
                    "type": "user",
                    "sessionId": "claude-repeated",
                    "cwd": "/tmp/project",
                    "timestamp": "2026-07-27T08:00:00Z",
                    "isSidechain": False,
                    "entrypoint": "cli",
                    "message": {"content": "go"},
                },
                {
                    "type": "user",
                    "sessionId": "claude-repeated",
                    "cwd": "/tmp/project",
                    "timestamp": "2026-07-27T08:01:00Z",
                    "isSidechain": False,
                    "entrypoint": "cli",
                    "message": {"content": "go"},
                },
            ],
            "2026-07-27T08:01:00Z",
        )

        result = collectors.collect_agent_sessions("2026-07-27")

        self.assertEqual(result["sessions"][0]["user_turn_count"], 2)
        self.assertEqual(result["sessions"][0]["followup_count"], 1)
        self.assertEqual(result["sessions"][0]["assistant_turn_count"], 1)
        self.assertEqual(result["sessions"][0]["assistant_visible_chars"], 7)

    def test_hook_event_overrides_root_like_claude_headless_metadata(self):
        session_path = self.projects_dir / "project" / "headless.jsonl"
        _write_jsonl(
            session_path,
            [{
                "type": "user",
                "sessionId": "claude-headless",
                "cwd": "/tmp/project",
                "timestamp": "2026-07-27T08:00:00Z",
                "isSidechain": False,
                "entrypoint": "cli",
                "message": {"content": "Headless request"},
            }],
            "2026-07-27T08:00:00Z",
        )
        self.gate_events_dir.mkdir(parents=True)
        unknown_path = self.projects_dir / "project" / "unknown-invocation.jsonl"
        _write_jsonl(
            unknown_path,
            [{
                "type": "user",
                "sessionId": "claude-unknown-invocation",
                "cwd": "/tmp/project",
                "timestamp": "2026-07-27T08:05:00Z",
                "isSidechain": False,
                "entrypoint": "cli",
                "message": {"content": "Ambiguous request"},
            }],
            "2026-07-27T08:05:00Z",
        )
        (self.gate_events_dir / "2026-07-27.jsonl").write_text("\n".join([
            json.dumps({"session_id": "claude-headless", "disposition": "headless_excluded"}),
            json.dumps({"session_id": "claude-unknown-invocation", "disposition": "invocation_unknown"}),
        ]) + "\n")

        result = collectors.collect_agent_sessions("2026-07-27")
        sessions = {session["session_id"]: session for session in result["sessions"]}

        self.assertEqual(sessions["claude-headless"]["session_origin"], "automation")
        self.assertEqual(sessions["claude-unknown-invocation"]["session_origin"], "unknown")
        self.assertEqual(result["initiation_audit"]["interactive_roots_started"], 0)

    def test_initiation_metrics_distinguish_root_and_subagent_sessions(self):
        root_path = self.codex_dir / "2026" / "07" / "27" / "rollout-root.jsonl"
        _write_jsonl(
            root_path,
            [
                {
                    "type": "session_meta",
                    "timestamp": "2026-07-27T09:00:00Z",
                    "payload": {
                        "id": "codex-root",
                        "cwd": "/tmp/root-project",
                        "timestamp": "2026-07-27T09:00:00Z",
                        "source": "vscode",
                        "originator": "Codex Desktop",
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-27T09:00:01Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Research this possibility"}],
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-27T09:00:01Z",
                    "payload": {"type": "user_message", "message": "Research this possibility"},
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-27T09:01:00Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "**5-minute gate**\nProceed?"}],
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-27T09:01:00Z",
                    "payload": {"type": "agent_message", "message": "**5-minute gate**\nProceed?"},
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-27T09:02:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "trim: only inspect one file"}],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-27T09:08:00Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Finished the narrow check."}],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-07-27T09:09:00Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "x" * 1200}],
                    },
                },
            ],
            "2026-07-27T09:08:00Z",
        )
        subagent_path = self.codex_dir / "2026" / "07" / "27" / "rollout-subagent.jsonl"
        _write_jsonl(
            subagent_path,
            [
                {
                    "type": "session_meta",
                    "timestamp": "2026-07-27T10:00:00Z",
                    "payload": {
                        "id": "codex-subagent",
                        "cwd": "/tmp/root-project",
                        "timestamp": "2026-07-27T10:00:00Z",
                        "source": {"subagent": {"thread_spawn": {"depth": 1}}},
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-27T10:00:01Z",
                    "payload": {"type": "user_message", "message": "Worker task"},
                },
            ],
            "2026-07-27T10:00:01Z",
        )
        unobserved_path = self.codex_dir / "2026" / "07" / "27" / "rollout-unobserved.jsonl"
        _write_jsonl(
            unobserved_path,
            [
                {
                    "type": "session_meta",
                    "timestamp": "2026-07-27T11:00:00Z",
                    "payload": {
                        "id": "codex-unobserved",
                        "cwd": "/tmp/root-project",
                        "timestamp": "2026-07-27T11:00:00Z",
                        "source": "cli",
                        "originator": "codex-tui",
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-27T11:00:01Z",
                    "payload": {"type": "agent_message", "message": "We discussed **5-minute gate** wording."},
                },
            ],
            "2026-07-27T11:00:01Z",
        )
        proceed_path = self.codex_dir / "2026" / "07" / "27" / "rollout-proceed.jsonl"
        _write_jsonl(
            proceed_path,
            [
                {
                    "type": "session_meta",
                    "timestamp": "2026-07-27T12:00:00Z",
                    "payload": {
                        "id": "codex-proceed",
                        "cwd": "/tmp/root-project",
                        "timestamp": "2026-07-27T12:00:00Z",
                        "source": "cli",
                        "originator": "codex-tui",
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-27T12:00:01Z",
                    "payload": {"type": "agent_message", "message": "**5-minute gate**\nProceed?"},
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-27T12:01:00Z",
                    "payload": {"type": "user_message", "message": "No need to trim; go ahead"},
                },
            ],
            "2026-07-27T12:01:00Z",
        )
        self.gate_events_dir.mkdir(parents=True)
        (self.gate_events_dir / "2026-07-27.jsonl").write_text(
            json.dumps({"session_id": "codex-root", "disposition": "injected"}) + "\n"
        )
        self.gate_sessions_dir.mkdir(parents=True)
        (self.gate_sessions_dir / "proceed.json").write_text(json.dumps({
            "session_id": "codex-proceed",
            "date": "2026-07-27",
            "disposition": "injected",
        }) + "\n")

        result = collectors.collect_agent_sessions("2026-07-27")
        sessions = {session["session_id"]: session for session in result["sessions"]}
        root = sessions["codex-root"]
        subagent = sessions["codex-subagent"]
        unobserved = sessions["codex-unobserved"]
        proceed = sessions["codex-proceed"]

        self.assertEqual(root["session_origin"], "interactive_root")
        self.assertTrue(root["started_on_target_date"])
        self.assertEqual(root["user_turn_count"], 2)
        self.assertEqual(root["followup_count"], 1)
        self.assertEqual(root["assistant_turn_count"], 3)
        self.assertEqual(root["assistant_visible_chars"], 1252)
        self.assertEqual(root["duration_minutes"], 9.0)
        self.assertTrue(root["gate_injected"])
        self.assertTrue(root["gate_shown"])
        self.assertEqual(root["gate_outcome"], "trimmed")
        self.assertEqual(subagent["session_origin"], "subagent")
        self.assertFalse(subagent["gate_injected"])
        self.assertFalse(unobserved["gate_shown"])
        self.assertEqual(unobserved["gate_outcome"], "not_shown")
        self.assertEqual(proceed["gate_outcome"], "proceeded")
        self.assertEqual(result["chat_count"], 3)
        self.assertEqual(result["interactive_roots_started"], 3)
        self.assertEqual(result["interactive_root_count"], 3)
        self.assertEqual(result["total_session_count"], 4)
        self.assertEqual(result["session_origin_counts"]["subagent"], 1)
        self.assertEqual(sum(
            project["chat_count"] for project in result["by_project"].values()
        ), 3)
        self.assertEqual(sum(
            project["chat_count"]
            for project in result["total_sessions_by_project"].values()
        ), 4)
        self.assertEqual(result["initiation_audit"]["interactive_roots_started"], 3)
        self.assertEqual(result["initiation_audit"]["gate_shown"], 2)
        self.assertEqual(result["initiation_audit"]["telemetry_status"], "reconciled")


if __name__ == "__main__":
    unittest.main()
