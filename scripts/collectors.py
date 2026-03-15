#!/usr/bin/env python3
"""Completion signal collectors for the daily rollup.

Each collector takes a date_str (YYYY-MM-DD), returns its portion of the
completions dict, and fails gracefully (returns empty on error).
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
DAY_TRACKER_DAILY_DIR = Path.home() / "Documents" / "day-tracker" / "data" / "daily"
PROJECTS_YAML = Path.home() / "Documents" / "Projects" / "projects.yaml"

# Fixed CET offset (UTC+1) — acceptable approximation; off by 1h during CEST
_CET = timezone(timedelta(hours=1))


def _warn(msg: str) -> None:
    print(f"collectors: {msg}", file=sys.stderr)


def _run(cmd: list[str], timeout: int = 30) -> Optional[str]:
    """Run a command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            _warn(f"command failed ({result.returncode}): {' '.join(cmd[:4])}...")
            if result.stderr.strip():
                _warn(f"  stderr: {result.stderr.strip()[:200]}")
            return None
        return result.stdout
    except subprocess.TimeoutExpired:
        _warn(f"command timed out: {' '.join(cmd[:4])}...")
        return None
    except FileNotFoundError:
        _warn(f"command not found: {cmd[0]}")
        return None


# ---------------------------------------------------------------------------
# 1a. Git commits
# ---------------------------------------------------------------------------

def collect_git_commits(date_str: str) -> list[dict]:
    """Collect git commits from all repos for the given date."""
    repos: list[Path] = []

    # Discover repos under ~/Documents (excluding Backups/)
    try:
        result = subprocess.run(
            ["find", str(Path.home() / "Documents"), "-maxdepth", "5",
             "-name", ".git", "-type", "d",
             "-not", "-path", "*/Backups/*"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if line:
                    repos.append(Path(line).parent)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        _warn("find for git repos timed out or failed")

    # Always include ~/.agents
    agents_dir = Path.home() / ".agents"
    if (agents_dir / ".git").exists():
        repos.append(agents_dir)

    commits = []
    after = f"{date_str}T00:00:00"
    # Use day+1 for --before to capture the full day
    try:
        next_day = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        return commits
    before = f"{next_day}T00:00:00"

    for repo in repos:
        try:
            out = _run([
                "git", "-C", str(repo), "log",
                f"--after={after}", f"--before={before}",
                "--format=%H|%s|%aI|%an",
                "--all",
            ])
            if not out or not out.strip():
                continue

            for line in out.strip().split("\n"):
                if not line or "|" not in line:
                    continue
                parts = line.split("|", 3)
                if len(parts) < 4:
                    continue
                hash_val, message, timestamp, author = parts
                commits.append({
                    "repo": repo.name,
                    "message": message,
                    "timestamp": timestamp,
                    "author": author,
                })
        except Exception as e:
            _warn(f"git log failed for {repo.name}: {e}")

    return commits


# ---------------------------------------------------------------------------
# 1b. Todoist completed
# ---------------------------------------------------------------------------

def collect_todoist_completed(date_str: str) -> list[dict]:
    """Collect completed Todoist tasks. Stub until td CLI is installed."""
    if not shutil.which("td"):
        return [{"_note": "td CLI not installed"}]
    # Future: parse td output
    return []


# ---------------------------------------------------------------------------
# 1c. Calendar events
# ---------------------------------------------------------------------------

def collect_calendar_events(date_str: str) -> list[dict]:
    """Collect calendar events from both calendars."""
    calendars = [
        ("primary", "primary"),
        ("jno364pp9c545r5s1n99k3q39s@group.calendar.google.com", "meetings"),
    ]

    events = []
    seen_ids: set[str] = set()

    for cal_id, cal_label in calendars:
        out = _run([
            "gog", "--json", "cal", "events", cal_id,
            "--from", date_str, "--to", date_str,
            "--account", "pete.hartree@gmail.com",
            "--max", "50", "--results-only",
        ])
        if not out:
            continue

        try:
            data = json.loads(out)
            # gog --json --results-only returns a list of events
            if isinstance(data, dict):
                data = data.get("items") or data.get("events") or []
            if not isinstance(data, list):
                continue

            for ev in data:
                ev_id = ev.get("id", "")
                if ev_id and ev_id in seen_ids:
                    continue
                if ev_id:
                    seen_ids.add(ev_id)

                # Extract start/end times
                start = ev.get("start", {})
                end = ev.get("end", {})
                start_time = start.get("dateTime") or start.get("date", "")
                end_time = end.get("dateTime") or end.get("date", "")

                events.append({
                    "summary": ev.get("summary", "(no title)"),
                    "start": start_time,
                    "end": end_time,
                    "calendar": cal_label,
                })
        except (json.JSONDecodeError, TypeError) as e:
            _warn(f"calendar JSON parse error ({cal_label}): {e}")

    return events


# ---------------------------------------------------------------------------
# 1d. Agent sessions (extracted from chief-of-staff generate_digest.py)
# ---------------------------------------------------------------------------

def _parse_timestamp(ts: str) -> datetime:
    """Parse an ISO timestamp string to timezone-aware datetime."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_CET)
    return dt


def _mtime_dt(path: Path) -> datetime:
    """Return file modification time as timezone-aware UTC datetime."""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _extract_jsonl_metadata(jsonl_path: Path) -> Optional[dict]:
    """Extract session metadata from a JSONL file (first 50 lines)."""
    session_id = None
    project_path = None
    created = None
    first_prompt = None

    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for i, raw_line in enumerate(f):
                if i >= 50:
                    break
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                obj_type = obj.get("type", "")

                if session_id is None and obj.get("sessionId"):
                    session_id = obj["sessionId"]
                if project_path is None and obj.get("cwd"):
                    project_path = obj["cwd"]
                if created is None:
                    if obj_type == "queue-operation" and obj.get("timestamp"):
                        created = obj["timestamp"]
                    elif obj.get("timestamp") and project_path:
                        created = obj["timestamp"]
                if first_prompt is None and obj_type == "user":
                    msg = obj.get("message", {})
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for part in content:
                            if not isinstance(part, dict):
                                continue
                            text = part.get("text", "")
                            if text.startswith("#") or text.startswith("<") or len(text) < 10:
                                continue
                            first_prompt = text[:200]
                            break

                if session_id and project_path and created and first_prompt:
                    break
    except OSError:
        return None

    if not project_path:
        return None
    if not created:
        created = _mtime_dt(jsonl_path).isoformat()

    return {
        "sessionId": session_id,
        "projectPath": project_path,
        "created": created,
        "modified": _mtime_dt(jsonl_path).isoformat(),
        "summary": "",
        "firstPrompt": first_prompt or "",
        "_source": "claude-jsonl",
    }


def _get_project_name(project_path: str) -> str:
    """Extract a readable project name from the projectPath."""
    if not project_path:
        return "Unknown"

    if ".agents/skills" in project_path or ".claude/skills" in project_path:
        parts = re.split(r"\.(?:agents|claude)/skills/", project_path)
        if len(parts) > 1:
            skill_name = parts[1].split("/")[0]
            return f"Skills ({skill_name})"
        return "Skills"

    if ".claude" in project_path and "skills" not in project_path:
        return "Claude Code config"
    if ".agents" in project_path and "skills" not in project_path:
        return ".agents"

    name = Path(project_path).name
    return name or "Unknown"


def _categorise_project(project_path: str, project_name: str) -> str:
    """Determine the project type."""
    path_lower = project_path.lower() if project_path else ""
    if ".claude" in path_lower or ".agents" in path_lower:
        return "tools"
    if "plans-and-reviews" in path_lower:
        return "planning"
    return "personal"


def collect_agent_sessions(date_str: str) -> dict:
    """Collect Claude Code + Codex sessions for a specific date (midnight to midnight CET)."""
    try:
        day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=_CET)
    except ValueError:
        return {"chat_count": 0, "by_project": {}}

    day_end = day_start + timedelta(days=1)
    since_iso = day_start.isoformat()

    # Scan JSONL sessions
    sessions = []
    seen_ids: set[str] = set()

    if PROJECTS_DIR.exists():
        for project_dir in PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            for jsonl_path in project_dir.glob("*.jsonl"):
                try:
                    mtime = _mtime_dt(jsonl_path)
                    if mtime < day_start or mtime >= day_end:
                        continue
                except OSError:
                    continue

                session = _extract_jsonl_metadata(jsonl_path)
                if not session:
                    continue

                sid = session.get("sessionId")
                if sid:
                    if sid in seen_ids:
                        continue
                    seen_ids.add(sid)

                # Filter: session must have activity within the target day
                try:
                    modified = _parse_timestamp(session["modified"])
                    if modified < day_start or modified >= day_end:
                        continue
                except (ValueError, KeyError):
                    pass

                sessions.append(session)

    # Also check day-tracker for sessions (supplementary)
    dt_daily = DAY_TRACKER_DAILY_DIR / f"{date_str}.json"
    if dt_daily.exists():
        try:
            data = json.loads(dt_daily.read_text())
            dt_seen: set[tuple] = set()
            for entry in data.get("entries", []):
                for sess in entry.get("active_sessions") or []:
                    if sess.get("agent") != "claude":
                        continue
                    key = (sess.get("project_path", ""), sess.get("title", ""))
                    if key not in dt_seen:
                        dt_seen.add(key)
                        # Only add if not already covered by JSONL scan
                        pp = sess.get("project_path", "")
                        if not any(s.get("projectPath") == pp for s in sessions):
                            sessions.append({
                                "sessionId": None,
                                "projectPath": pp,
                                "created": entry.get("timestamp", ""),
                                "modified": entry.get("timestamp", ""),
                                "summary": sess.get("title", ""),
                                "firstPrompt": "",
                                "_source": "day-tracker",
                            })
        except (json.JSONDecodeError, OSError) as e:
            _warn(f"day-tracker session load failed: {e}")

    # Codex sessions
    codex_sessions = []
    if CODEX_SESSIONS_DIR.exists():
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            year_dir = CODEX_SESSIONS_DIR / str(dt.year)
            month_dir = year_dir / str(dt.month)
            day_dir = month_dir / str(dt.day)
            if day_dir.exists():
                for f in day_dir.glob("rollout-*.jsonl"):
                    try:
                        with open(f) as fh:
                            for line in fh:
                                obj = json.loads(line)
                                if obj.get("type") == "session_meta":
                                    payload = obj["payload"]
                                    codex_sessions.append({
                                        "sessionId": payload.get("id", ""),
                                        "projectPath": payload.get("cwd", ""),
                                        "created": payload.get("timestamp", ""),
                                        "modified": obj.get("timestamp", ""),
                                        "summary": "",
                                        "firstPrompt": "",
                                        "_source": "codex",
                                    })
                                    break
                    except (json.JSONDecodeError, OSError):
                        pass
        except ValueError:
            pass

    all_sessions = sessions + codex_sessions

    # Group by project
    groups: dict[str, dict] = {}
    for s in all_sessions:
        pp = s.get("projectPath", "")
        name = _get_project_name(pp)
        if name not in groups:
            groups[name] = {
                "type": _categorise_project(pp, name),
                "chat_count": 0,
                "claude_count": 0,
                "codex_count": 0,
            }
        groups[name]["chat_count"] += 1
        if s.get("_source") == "codex":
            groups[name]["codex_count"] += 1
        else:
            groups[name]["claude_count"] += 1

    return {
        "chat_count": len(all_sessions),
        "by_project": groups,
        "key_completions": [],
        "still_in_progress": [],
    }


# ---------------------------------------------------------------------------
# 1e. Google Docs edited
# ---------------------------------------------------------------------------

def collect_google_docs_edited(date_str: str) -> list[dict]:
    """Collect Google Docs that I personally edited on the given date.

    Shells out to gdoc's own Python to call list_files (which now returns
    modifiedByMeTime), then filters client-side to only include docs where
    modifiedByMeTime falls within the target date.
    """
    gdoc_python = "/Users/ph/.local/share/uv/tools/gdoc/bin/python3"
    script = f"""
import json
from gdoc.api.drive import list_files
query = (
    "modifiedTime > '{date_str}T00:00:00' and "
    "modifiedTime < '{date_str}T23:59:59' and "
    "mimeType='application/vnd.google-apps.document'"
)
files = list_files(query)
print(json.dumps(files))
"""
    out = _run([gdoc_python, "-c", script], timeout=30)
    if not out:
        _warn("gdoc python call failed, falling back to gog")
        return _collect_google_docs_via_gog(date_str)

    try:
        files = json.loads(out)
        docs = []
        for item in files:
            my_time = item.get("modifiedByMeTime", "")
            if not my_time:
                continue
            if my_time.startswith(date_str):
                docs.append({
                    "title": item.get("name", ""),
                    "modified_time": item.get("modifiedTime", ""),
                    "modified_by_me_time": my_time,
                    "id": item.get("id", ""),
                })
        return docs
    except (json.JSONDecodeError, TypeError) as e:
        _warn(f"gdoc JSON parse error: {e}")
        return _collect_google_docs_via_gog(date_str)


def _collect_google_docs_via_gog(date_str: str) -> list[dict]:
    """Fallback: collect docs via gog CLI (no modifiedByMeTime filtering)."""
    query = (
        f"modifiedTime > '{date_str}T00:00:00' and "
        f"modifiedTime < '{date_str}T23:59:59' and "
        "mimeType='application/vnd.google-apps.document' and "
        "'pete.hartree@gmail.com' in writers"
    )
    out = _run([
        "gog", "--json", "drive", "search", query,
        "--raw-query",
        "--account", "pete.hartree@gmail.com",
        "--max", "50", "--results-only",
    ])
    if not out:
        return []

    try:
        data = json.loads(out)
        if isinstance(data, dict):
            data = data.get("files") or data.get("items") or []
        if not isinstance(data, list):
            return []

        docs = []
        for item in data:
            docs.append({
                "title": item.get("name") or item.get("title", ""),
                "modified_time": item.get("modifiedTime", ""),
                "id": item.get("id", ""),
            })
        return docs
    except (json.JSONDecodeError, TypeError) as e:
        _warn(f"google docs JSON parse error: {e}")
        return []


# ---------------------------------------------------------------------------
# 1f. Emails sent
# ---------------------------------------------------------------------------

def _extract_body_from_payload(payload: dict) -> str:
    """Recursively extract plain text body from Gmail message payload."""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        import base64
        data = payload.get("body", {}).get("data", "")
        if data:
            try:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            except Exception:
                return ""
    for part in payload.get("parts", []):
        text = _extract_body_from_payload(part)
        if text:
            return text
    return ""


def _strip_quoted_reply(body: str) -> str:
    """Strip the quoted reply portion from an email body.

    Cuts at 'On ... wrote:' lines or blocks of '>' quoted lines at the end.
    """
    lines = body.split("\n")
    cut_at = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        # "On <date> <person> wrote:" pattern
        if stripped.startswith("On ") and stripped.endswith("wrote:"):
            cut_at = i
            break
        # Gmail sometimes wraps this across two lines
        if stripped.startswith("On ") and i + 1 < len(lines) and lines[i + 1].strip().endswith("wrote:"):
            cut_at = i
            break
    result = "\n".join(lines[:cut_at]).rstrip()
    return result


def _parse_thread_headers(msg: dict) -> dict:
    """Extract key headers from a raw Gmail API message."""
    hdrs = {}
    for h in msg.get("payload", {}).get("headers", []):
        name = h.get("name", "").lower()
        if name in ("from", "to", "cc", "date", "subject"):
            hdrs[name] = h.get("value", "")
    return hdrs


def collect_emails_sent(date_str: str) -> list[dict]:
    """Collect emails sent on the given date across all accounts.

    Fetches threads from sent mail, then gets the full thread to find
    Peter's actual sent message(s) plus the preceding message for context.
    """
    search_date = date_str.replace("-", "/")
    try:
        next_day = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y/%m/%d")
    except ValueError:
        return []

    query = f"in:sent after:{search_date} before:{next_day}"

    accounts = [
        "pete.hartree@gmail.com",
        "peter@type3.audio",
        "inboxwhenready@gmail.com",
    ]

    emails = []
    for account in accounts:
        out = _run([
            "gog", "--json", "gmail", "search", query,
            "--account", account,
            "--max", "50", "--results-only",
        ])
        if not out:
            continue

        try:
            threads = json.loads(out)
            if isinstance(threads, dict):
                threads = threads.get("messages") or threads.get("items") or []
            if not isinstance(threads, list):
                continue

            for thread_stub in threads:
                thread_id = thread_stub.get("id", "")
                if not thread_id:
                    continue

                # Fetch full thread to get all messages
                thread_out = _run([
                    "gog", "--json", "gmail", "thread", "get", thread_id,
                    "--account", account,
                ])
                if not thread_out:
                    emails.append({
                        "to": "",
                        "subject": thread_stub.get("subject", ""),
                        "timestamp": thread_stub.get("date", ""),
                        "body": "",
                        "account": account,
                    })
                    continue

                try:
                    thread_data = json.loads(thread_out)
                    messages = thread_data.get("thread", {}).get("messages", [])
                    if not messages:
                        continue

                    # Find messages with SENT label (Peter's replies)
                    for i, msg in enumerate(messages):
                        labels = msg.get("labelIds", [])
                        if "SENT" not in labels:
                            continue

                        hdrs = _parse_thread_headers(msg)
                        body = _extract_body_from_payload(msg.get("payload", {}))
                        if body:
                            body = _strip_quoted_reply(body)
                        # Also try snippet as fallback
                        if not body:
                            body = msg.get("snippet", "")

                        if len(body) > 2000:
                            body = body[:2000] + "\n[truncated]"

                        # Get the preceding message for context
                        previous_body = ""
                        previous_from = ""
                        if i > 0:
                            prev_msg = messages[i - 1]
                            prev_hdrs = _parse_thread_headers(prev_msg)
                            previous_from = prev_hdrs.get("from", "")
                            previous_body = _extract_body_from_payload(prev_msg.get("payload", {}))
                            if not previous_body:
                                previous_body = prev_msg.get("snippet", "")
                            if len(previous_body) > 1000:
                                previous_body = previous_body[:1000] + "\n[truncated]"

                        emails.append({
                            "to": hdrs.get("to", ""),
                            "cc": hdrs.get("cc", ""),
                            "subject": hdrs.get("subject", "") or thread_stub.get("subject", ""),
                            "timestamp": hdrs.get("date", "") or thread_stub.get("date", ""),
                            "body": body,
                            "previous_message": {
                                "from": previous_from,
                                "body": previous_body,
                            } if previous_body else None,
                            "account": account,
                        })

                except (json.JSONDecodeError, TypeError, KeyError):
                    emails.append({
                        "to": "",
                        "subject": thread_stub.get("subject", ""),
                        "timestamp": thread_stub.get("date", ""),
                        "body": "",
                        "account": account,
                    })

        except (json.JSONDecodeError, TypeError) as e:
            _warn(f"email JSON parse error ({account}): {e}")

    return emails


# ---------------------------------------------------------------------------
# Collect all
# ---------------------------------------------------------------------------

def collect_all(date_str: str) -> dict:
    """Run all collectors and return assembled completions dict."""
    collectors = [
        ("git_commits", collect_git_commits),
        ("todoist_completed", collect_todoist_completed),
        ("calendar_events", collect_calendar_events),
        ("agent_sessions", collect_agent_sessions),
        ("google_docs_edited", collect_google_docs_edited),
        ("emails_sent", collect_emails_sent),
    ]

    completions: dict = {}
    errors: list[str] = []

    for name, fn in collectors:
        try:
            completions[name] = fn(date_str)
        except Exception as e:
            _warn(f"collector {name} failed: {e}")
            errors.append(f"{name}: {e}")
            completions[name] = [] if name != "agent_sessions" else {"chat_count": 0, "by_project": {}}

    completions["_errors"] = errors
    return completions
