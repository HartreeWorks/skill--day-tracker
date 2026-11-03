"""First typed message from Claude Code and Codex transcripts."""

import json
import tempfile
import unittest
from pathlib import Path

import capture


def write_jsonl(path: Path, entries):
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


class FirstUserMessageTests(unittest.TestCase):
    def test_claude_code_format_skips_injected_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            write_jsonl(path, [
                {"type": "summary"},
                {"type": "user", "message": {"content": "<local-command-caveat>x</local-command-caveat>"}},
                {"type": "user", "message": {"content": [{"type": "text", "text": "Fix the timeline"}]}},
            ])
            self.assertEqual(capture._get_first_user_message(path), "Fix the timeline")

    def test_codex_format_skips_agents_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            write_jsonl(path, [
                {"type": "session_meta", "payload": {"cwd": "/x"}},
                {"type": "response_item", "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "# AGENTS.md instructions"}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "developer",
                    "content": [{"type": "input_text", "text": "system stuff"}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "Review the rollup"}]}},
            ])
            self.assertEqual(capture._get_first_user_message(path), "Review the rollup")

    def test_typed_prompt_starting_with_angle_bracket_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            write_jsonl(path, [
                {"type": "user", "message": {"content": "<system-reminder>ctx</system-reminder>"}},
                {"type": "user", "message": {"content": "<div> renders blank in Safari"}},
            ])
            self.assertEqual(capture._get_first_user_message(path), "<div> renders blank in Safari")


class ActiveSessionTests(unittest.TestCase):
    def test_same_first_message_in_two_projects_is_two_sessions(self):
        import os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for project in ("-a-one", "-a-two"):
                d = home / ".claude" / "projects" / project
                d.mkdir(parents=True)
                for n in (1, 2):  # a session and its resumed copy
                    write_jsonl(d / f"{project}-{n}.jsonl",
                                [{"type": "user", "message": {"content": "Continue"}}])
            with patch.object(Path, "home", return_value=home), \
                 patch.object(capture, "_decode_claude_project_dir",
                              side_effect=lambda name, known: name):
                sessions = capture.get_active_agent_sessions()
        self.assertEqual(sorted(s["project_path"] for s in sessions), ["-a-one", "-a-two"])


if __name__ == "__main__":
    unittest.main()
