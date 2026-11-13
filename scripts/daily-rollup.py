#!/usr/bin/env python3
"""Daily rollup script for day-tracker.

Post-processes the raw daily JSON to:
1. Retry analysis for captures whose Gemini call failed
2. Map inferred_project values to canonical project names via fuzzy matching
3. Fill gaps in unclassified entries between same-project entries
4. Populate the summary field with time breakdowns and display-on coverage

Usage:
    python3 daily-rollup.py [YYYY-MM-DD]

If no date is given, defaults to today.
Idempotent — safe to re-run.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

# Allow imports from parent directory (for config module)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

VENV_PYTHON = Path.home() / "venvs" / "ph" / "bin" / "python3"


def ensure_yaml_interpreter():
    """Re-exec under the ph venv Python if this interpreter lacks PyYAML.

    Callers sometimes run this script with a bare `python3` that resolves to
    the system or Homebrew interpreter, neither of which has PyYAML.
    """
    import importlib.util
    if importlib.util.find_spec("yaml"):
        return
    if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
        os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

DATA_DIR = Path.home() / "Documents" / "day-tracker" / "data"
DAILY_DIR = DATA_DIR / "daily"
PROJECTS_YAML = Path.home() / "Documents" / "Projects" / "projects.yaml"

# Capture interval in minutes (used for time estimation)
CAPTURE_INTERVAL = 2
# Gap threshold for attributing unclassified entries (minutes)
GAP_THRESHOLD = 15
# Minimum captures to consider data reliable
LOW_DATA_THRESHOLD = 20
# Limits on Gemini retries per rollup, so a slow or unreachable API can't run
# the job past the scheduler's timeout before it saves anything
MAX_REANALYSES = 300
REANALYSIS_BUDGET_SECONDS = 15 * 60
MAX_CONSECUTIVE_REANALYSIS_FAILURES = 5
# Calendar attribution: a capture inside a qualifying event takes its project from the event
ACME_PROJECT = "acme-project"
NON_ACME_MEETING = "non-acme-meeting"
# Weekday coverage below this, with at least COVERAGE_MIN_DISPLAY_MINUTES of display-on time, raises an alert
COVERAGE_ALERT_THRESHOLD = 0.8
COVERAGE_MIN_DISPLAY_MINUTES = 120
PMSET_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) [+-]\d{4}\s+(\w+)\s+(.*)$")
TERMINAL_APPS = {"stable", "warp", "terminal", "iterm", "iterm2", "ghostty"}


def agent_session_counts(agent_sessions):
    """Return (interactive roots started or None, total records)."""
    sessions = agent_sessions.get("sessions") or []
    roots_started = agent_sessions.get("interactive_roots_started")
    if roots_started is None:
        has_provenance = bool(sessions) and all(
            "session_origin" in session and "started_on_target_date" in session
            for session in sessions
        )
        if has_provenance:
            roots_started = sum(
                session.get("session_origin") == "interactive_root"
                and session.get("started_on_target_date") is True
                for session in sessions
            )
        elif not sessions and not agent_sessions.get("chat_count"):
            roots_started = 0
    total_records = agent_sessions.get("total_session_count")
    if total_records is None:
        total_records = len(sessions) if sessions else agent_sessions.get("chat_count", 0)
    return roots_started, total_records


def load_projects():
    """Load canonical project list from projects.yaml."""
    import yaml
    if not PROJECTS_YAML.exists():
        return []
    with open(PROJECTS_YAML) as f:
        data = yaml.safe_load(f)
    return data.get("projects", [])


def build_alias_map(projects):
    """Build a map of lowercase aliases to canonical folder names.

    Includes the folder name itself, the display name, and common variations.
    """
    alias_map = {}
    for p in projects:
        folder = p["folder"]
        name = p.get("name", "")
        # Exact folder name
        alias_map[folder.lower()] = folder
        # Display name
        if name:
            alias_map[name.lower()] = folder
        # Explicit aliases for repo names, terminal tab titles, and legacy labels
        for alias in p.get("aliases", []):
            if alias:
                alias_map[str(alias).lower()] = folder
        # Folder without date prefix (e.g. "acme-ai-uplift")
        parts = folder.split("-", 3)
        if len(parts) >= 4 and parts[0].isdigit() and parts[1].isdigit():
            short = "-".join(parts[2:])
            alias_map[short.lower()] = folder
    return alias_map


def strip_date_prefix(value):
    """Strip YYYY-MM- or YYYY-MM-DD- date prefixes from a project string."""
    import re
    return re.sub(r"^\d{4}-\d{2}(-\d{2})?-", "", value)


def fuzzy_match_project(value, alias_map, projects, threshold=0.8):
    """Match a project string to a canonical project name.

    Tries exact match first, then stripped-prefix match, then fuzzy matching.
    Returns (canonical_folder, confidence) or (None, 0).
    """
    if not value:
        return None, 0

    val_lower = value.lower().strip()

    # Model fallback labels are not project names. At a low fuzzy threshold,
    # values such as "other" can otherwise map to an unrelated project.
    if val_lower in {"other", "unknown", "unclassified", "none", "null"}:
        return None, 0

    # Exact match against aliases
    if val_lower in alias_map:
        return alias_map[val_lower], 1.0

    # Strip date prefix from input and try again
    val_stripped = strip_date_prefix(val_lower)
    if val_stripped != val_lower and val_stripped in alias_map:
        return alias_map[val_stripped], 0.95

    # Check if the stripped value contains or is contained in any alias
    for alias, folder in alias_map.items():
        if alias in val_stripped or val_stripped in alias:
            return folder, 0.9

    # Fuzzy match against all aliases (using both original and stripped)
    best_score = 0
    best_folder = None
    for alias, folder in alias_map.items():
        for candidate in (val_lower, val_stripped):
            score = SequenceMatcher(None, candidate, alias).ratio()
            if score > best_score:
                best_score = score
                best_folder = folder

    if best_score >= threshold:
        return best_folder, best_score

    return None, 0


def apply_project_mapping(entries, projects, config=None):
    """Apply canonical and deterministic project attribution to entries.

    AI inference is the fallback. Explicit capture-time project matches,
    configured title patterns, and app rules take precedence in that order.
    """
    alias_map = build_alias_map(projects)

    for entry in entries:
        entry.pop("project_suppressed", None)
        entry.pop("project_gap_filled", None)
        inferred = entry.get("inferred_project")
        canonical, _ = fuzzy_match_project(inferred, alias_map, projects)
        entry["canonical_project"] = canonical

        explicit_project = entry.get("project")
        if explicit_project:
            explicit_canonical, _ = fuzzy_match_project(
                explicit_project, alias_map, projects
            )
            entry["canonical_project"] = explicit_canonical or explicit_project

        active_app = (entry.get("active_app") or "").strip().lower()
        window_title = (entry.get("window_title") or "").strip().lower()
        # A pending-permission bell (warp-tab-flag) is not part of the tab name.
        window_title = window_title.removeprefix("\U0001F514").strip()
        if active_app in TERMINAL_APPS and window_title in alias_map:
            entry["canonical_project"] = alias_map[window_title]

        if not config:
            continue

        context = f'{entry.get("active_app") or ""} {entry.get("window_title") or ""}'
        for rule in config.project_patterns:
            pattern = rule.get("pattern", "")
            project = rule.get("project")
            if not pattern or not project:
                continue
            try:
                matched = re.search(pattern, context, re.IGNORECASE)
            except re.error as exc:
                print(f"Warning: Invalid project pattern {pattern!r}: {exc}")
                continue
            if matched:
                rule_canonical, _ = fuzzy_match_project(project, alias_map, projects)
                entry["canonical_project"] = rule_canonical or project
                break

        active_app = entry.get("active_app")
        if not active_app or not config.app_rules:
            continue
        app_lower = active_app.lower()
        title_lower = (entry.get("window_title") or "").lower()
        for rule in config.app_rules:
            if rule.get("app", "").lower() != app_lower:
                continue
            title_contains = rule.get("title_contains", "")
            if title_contains and title_contains.lower() not in title_lower:
                continue
            if "category" in rule:
                entry["category"] = rule["category"]
            if "project" in rule:
                project = rule["project"]
                if project:
                    rule_canonical, _ = fuzzy_match_project(
                        project, alias_map, projects
                    )
                    entry["canonical_project"] = rule_canonical or project
                else:
                    entry["canonical_project"] = None
                    entry["project_suppressed"] = True
            if "is_work" in rule:
                entry["is_work"] = rule["is_work"]
            break

    # Manual tags beat every automatic rule.
    for entry in entries:
        manual = entry.get("manual_project")
        if manual:
            manual_canonical, _ = fuzzy_match_project(manual, alias_map, projects)
            entry["canonical_project"] = manual_canonical or manual
            entry.pop("project_suppressed", None)


def calendar_windows(events):
    """(start, end, is_acme) for each event that decides attribution.

    An event qualifies when it is timed (not all-day), Peter hasn't declined
    it, and someone other than Peter is invited. It is Acme when any
    attendee, Peter included, uses an @acme.example address, or when it is
    on the Acme calendar. Events from sidecars collected before
    attendee facts were recorded never qualify. Times are converted to the
    local timezone of the process, which is the timezone capture timestamps
    use when the rollup runs on the same machine and day.
    """
    windows = []
    for ev in events:
        if ev.get("all_day") or ev.get("self_response") == "declined":
            continue
        if not ev.get("other_attendee_count"):
            continue
        try:
            start = datetime.fromisoformat(ev["start"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(ev["end"].replace("Z", "+00:00"))
        except (KeyError, ValueError, AttributeError):
            continue
        if start.tzinfo is None or end.tzinfo is None:
            continue
        is_acme = bool(ev.get("has_acme_attendee")) or "acme" in (ev.get("calendars") or [])
        windows.append((start.astimezone().replace(tzinfo=None), end.astimezone().replace(tzinfo=None), is_acme))
    return windows


def apply_calendar_attribution(entries, events):
    """Set the project of captures taken during a qualifying calendar event.

    Acme events make the capture Acme work. Other events mark it
    non-Acme: its project becomes NON_ACME_MEETING unless the
    screenshot already gave a project other than Acme. When qualifying
    events that disagree overlap, or a capture has a manual tag, the calendar
    leaves it alone.
    """
    windows = calendar_windows(events)
    for entry in entries:
        entry.pop("calendar_attributed", None)
        if not windows or entry.get("manual_project"):
            continue
        try:
            t = datetime.fromisoformat(entry["timestamp"])
        except (KeyError, ValueError):
            continue
        kinds = {ft for start, end, ft in windows if start <= t < end}
        if len(kinds) != 1:
            continue
        if kinds == {True}:
            entry["canonical_project"] = ACME_PROJECT
            entry["is_work"] = True
        elif entry.get("canonical_project") in (None, ACME_PROJECT):
            entry["canonical_project"] = NON_ACME_MEETING
        else:
            continue
        entry.pop("project_suppressed", None)
        entry["calendar_attributed"] = True


def fill_gaps(entries):
    """Attribute unclassified entries between same-project entries.

    When an unclassified entry sits between entries for the same project
    within a GAP_THRESHOLD window, attribute it to that project with
    lower confidence.
    """
    if len(entries) < 3:
        return

    for i in range(1, len(entries) - 1):
        entry = entries[i]
        # Only fill if currently unclassified
        if entry.get("canonical_project") or entry.get("project_suppressed"):
            continue

        prev_proj = entries[i - 1].get("canonical_project")
        next_proj = entries[i + 1].get("canonical_project")

        if not prev_proj or prev_proj != next_proj:
            continue

        # Check time gap
        try:
            t_prev = datetime.fromisoformat(entries[i - 1]["timestamp"])
            t_curr = datetime.fromisoformat(entries[i]["timestamp"])
            t_next = datetime.fromisoformat(entries[i + 1]["timestamp"])
        except (ValueError, KeyError):
            continue

        if (t_next - t_prev).total_seconds() <= GAP_THRESHOLD * 60:
            entry["canonical_project"] = prev_proj
            entry["project_gap_filled"] = True


def recover_unanalysed(date_str, entries):
    """Retry failed analyses and refresh manual tags from capture metadata.

    Covers unanalysed placeholder entries, older failed captures that kept
    their screenshots and metadata but got no entry, and tags set on capture
    metadata that never reached the entry. A capture that fails again stays as
    an unanalysed entry, so its time still counts. Retries stop at the time
    budget or after several failures in a row (usually offline).

    Mutates entries. Returns (counts, capture_dirs of entries it changed).
    """
    day_dir = DAILY_DIR.parent / "captures" / date_str
    stats = Counter()
    touched = set()
    if not day_dir.is_dir():
        return stats, touched

    from capture import needs_reanalysis, reanalyse_capture, sweep_orphan_captures, unanalysed_analysis
    from models import CaptureMetadata, DailyEntry

    swept = sweep_orphan_captures(day_dir)
    if swept:
        stats["orphans_swept"] = swept

    deadline = time.monotonic() + REANALYSIS_BUDGET_SECONDS
    consecutive_failures = 0
    by_dir = {e.get("capture_dir"): e for e in entries}
    for meta_path in sorted(day_dir.glob("*/metadata.json")):
        rel_dir = f"{date_str}/{meta_path.parent.name}"
        entry = by_dir.get(rel_dir)
        try:
            metadata = CaptureMetadata.load(meta_path)
        except (OSError, ValueError, KeyError, TypeError):
            stats["unreadable"] += 1
            continue

        if entry is not None and entry.get("category") != "unanalysed":
            if (metadata.manual_project or None) != entry.get("manual_project"):
                if metadata.manual_project:
                    entry["manual_project"] = metadata.manual_project
                else:
                    entry.pop("manual_project", None)
                touched.add(rel_dir)
                stats["tags_refreshed"] += 1
            continue

        if needs_reanalysis(metadata):
            updated = None
            can_retry = (stats["reanalysed"] + stats["still_unanalysed"] < MAX_REANALYSES
                         and time.monotonic() < deadline
                         and consecutive_failures < MAX_CONSECUTIVE_REANALYSIS_FAILURES)
            if can_retry:
                try:
                    updated = reanalyse_capture(meta_path.parent)
                except Exception as e:
                    print(f"  Reanalysis failed for {rel_dir}: {e}", file=sys.stderr)
            if updated:
                metadata = updated
                stats["reanalysed"] += 1
                consecutive_failures = 0
            else:
                stats["still_unanalysed" if can_retry else "retry_deferred"] += 1
                consecutive_failures += can_retry
                if metadata.analysis is None:
                    metadata.analysis = unanalysed_analysis()
        new_entry = DailyEntry.from_metadata(metadata, rel_dir).to_dict()
        if entry is None:
            entries.append(new_entry)
            stats["added"] += 1
        else:
            entry.clear()
            entry.update(new_entry)
        touched.add(rel_dir)
    entries.sort(key=lambda e: e.get("timestamp", ""))
    return stats, touched


def merge_entries(on_disk, ours, touched):
    """Combine the daily log as it is now with the rollup's changes.

    The capture may have appended entries while the rollup was busy. Entries
    the rollup changed take its version; everything else comes from disk.
    """
    ours_by_dir = {e.get("capture_dir"): e for e in ours}
    merged = {e.get("capture_dir"): e for e in on_disk}
    for capture_dir in touched:
        if capture_dir in ours_by_dir:
            merged[capture_dir] = ours_by_dir[capture_dir]
    return sorted(merged.values(), key=lambda e: e.get("timestamp", ""))


def display_on_minutes(date_str, pmset_text=None, now=None):
    """Minutes the display was on during date_str, from `pmset -g log`.

    Returns None when pmset history does not reach back to the start of the
    day. pmset prints every event in the current timezone, so a day spent in
    another timezone is shifted by the difference.
    """
    if pmset_text is None:
        try:
            pmset_text = subprocess.run(["pmset", "-g", "log"], capture_output=True,
                                        text=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError):
            return None
    now = now or datetime.now()
    day_start = datetime.strptime(date_str, "%Y-%m-%d")
    day_end = min(day_start + timedelta(days=1), now)

    first_event = None
    on_since = None
    seconds = 0.0
    for line in pmset_text.splitlines():
        m = PMSET_LINE.match(line)
        if not m:
            continue
        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        first_event = first_event or ts
        kind, rest = m.group(2), m.group(3)
        if "Display is turned on" in rest:
            on_since = on_since or ts
        elif on_since and ("Display is turned off" in rest or (kind == "Sleep" and "Entering Sleep" in rest)):
            seconds += max(0.0, (min(ts, day_end) - max(on_since, day_start)).total_seconds())
            on_since = None
    if on_since:
        seconds += max(0.0, (day_end - max(on_since, day_start)).total_seconds())
    if first_event is None or first_event > day_start:
        return None
    return round(seconds / 60)


def add_coverage(summary, date_str, display_minutes):
    """Compare tracked time with display-on time; True when a weekday's coverage is low."""
    summary["display_on_minutes"] = display_minutes
    if not display_minutes:
        summary["coverage"] = None
        summary["untracked_display_on_minutes"] = None
        return False
    tracked = summary["total_tracked_minutes"]
    summary["coverage"] = round(tracked / display_minutes, 2)
    summary["untracked_display_on_minutes"] = max(0, display_minutes - tracked)
    weekday = datetime.strptime(date_str, "%Y-%m-%d").weekday() < 5
    return (weekday and display_minutes >= COVERAGE_MIN_DISPLAY_MINUTES
            and summary["coverage"] < COVERAGE_ALERT_THRESHOLD)


def add_completion_counts(summary, completions):
    """Add counts of the day's completion signals to the summary."""
    git_commits = completions.get("git_commits", [])
    summary["git_commit_count"] = len([c for c in git_commits if isinstance(c, dict) and "repo" in c])

    agent_sessions = completions.get("agent_sessions", {})
    roots_started, total_records = agent_session_counts(agent_sessions)
    summary["agent_session_count"] = roots_started
    summary["interactive_roots_started"] = roots_started
    summary["total_agent_session_count"] = total_records

    summary["calendar_event_count"] = len(completions.get("calendar_events", []))
    summary["emails_sent_count"] = len(completions.get("emails_sent", []))
    summary["google_docs_edited_count"] = len(completions.get("google_docs_edited", []))


def compute_summary(entries):
    """Compute time summary from entries.

    is_work None (private or unanalysed entries) counts towards
    unknown_minutes; a missing is_work counts as work, as it always has.
    """
    total_minutes = len(entries) * CAPTURE_INTERVAL
    work_minutes = sum(CAPTURE_INTERVAL for e in entries if e.get("is_work", True))
    personal_minutes = sum(CAPTURE_INTERVAL for e in entries if e.get("is_work") is False)
    unknown_minutes = total_minutes - work_minutes - personal_minutes

    by_project = defaultdict(int)
    by_category = defaultdict(int)

    for e in entries:
        if e.get("project_suppressed"):
            proj = "unclassified"
        else:
            proj = e.get("canonical_project") or e.get("inferred_project") or "unclassified"
        by_project[proj] += CAPTURE_INTERVAL
        cat = e.get("category", "other")
        by_category[cat] += CAPTURE_INTERVAL

    low_data = len(entries) < LOW_DATA_THRESHOLD

    return {
        "total_tracked_minutes": total_minutes,
        "work_minutes": work_minutes,
        "personal_minutes": personal_minutes,
        "unknown_minutes": unknown_minutes,
        "by_project": dict(sorted(by_project.items(), key=lambda x: -x[1])),
        "by_category": dict(sorted(by_category.items(), key=lambda x: -x[1])),
        "low_data": low_data,
        "rollup_timestamp": datetime.now().isoformat(),
    }


def send_alert(severity: str, title: str, message: str) -> None:
    """Send an alert to hartreeworks.org/api/alerts. Fails silently."""
    api_key = os.environ.get("HARTREEWORKS_INTERNAL_API_KEY", "")
    if not api_key:
        print(f"Alert ({severity}): {title} — {message}", file=sys.stderr)
        print("  (HARTREEWORKS_INTERNAL_API_KEY not set, alert not sent)", file=sys.stderr)
        return
    try:
        payload = json.dumps({
            "source": "day-tracker-rollup",
            "severity": severity,
            "title": title,
            "message": message,
        })
        subprocess.run(
            ["curl", "-s", "-X", "POST",
             "https://hartreeworks.org/api/alerts",
             "-H", "Content-Type: application/json",
             "-H", f"x-api-key: {api_key}",
             "-d", payload],
            capture_output=True, timeout=10,
        )
    except Exception as e:
        print(f"Alert delivery failed: {e}", file=sys.stderr)


def run_rollup(date_str, collect_completions=True, reanalyse=True, refresh_calendar=False):
    """Run the daily rollup for a given date.

    Retries failed analyses and collects completion signals on a snapshot of
    the daily log, then, holding the daily-log lock, merges in anything the
    capture wrote meanwhile, recomputes attribution and the summary, and
    writes the file atomically. Completion collection can be disabled to
    preserve an existing sidecar; reanalyse=False avoids Gemini calls as well.
    refresh_calendar recollects only calendar events into the existing sidecar.
    """
    from models import daily_log_lock, write_json_atomic

    daily_file = DAILY_DIR / f"{date_str}.json"

    def load_daily():
        if daily_file.exists():
            with open(daily_file) as f:
                return json.load(f)
        return {"date": date_str, "entries": []}

    # Slow work happens outside the lock, on a snapshot.
    snapshot = load_daily().get("entries", [])
    if reanalyse:
        recovery, touched = recover_unanalysed(date_str, snapshot)
    else:
        recovery, touched = Counter(), set()

    completions_file = DAILY_DIR / f"{date_str}.completions.json"
    if collect_completions:
        from collectors import collect_all
        completions = collect_all(date_str)
    elif completions_file.exists():
        try:
            with open(completions_file) as f:
                completions = json.load(f)
        except (json.JSONDecodeError, OSError):
            completions = {}
    else:
        completions = {}
    if refresh_calendar and not collect_completions:
        from collectors import collect_calendar_events
        completions["calendar_events"] = collect_calendar_events(date_str)

    display_minutes = display_on_minutes(date_str)
    projects = load_projects()
    try:
        from config import load_config
        config = load_config()
    except Exception as e:
        config = None
        print(f"Warning: Could not load deterministic project/app rules: {e}")

    with daily_log_lock():
        data = load_daily()
        entries = merge_entries(data.get("entries", []), snapshot, touched)
        has_entries = len(entries) > 0

        if has_entries:
            apply_project_mapping(entries, projects, config)
            apply_calendar_attribution(entries, completions.get("calendar_events", []))
            fill_gaps(entries)
            summary = compute_summary(entries)
        else:
            summary = {
                "total_tracked_minutes": 0,
                "work_minutes": 0,
                "personal_minutes": 0,
                "unknown_minutes": 0,
                "by_project": {},
                "by_category": {},
                "low_data": True,
                "rollup_timestamp": datetime.now().isoformat(),
            }
        add_completion_counts(summary, completions)
        low_coverage = add_coverage(summary, date_str, display_minutes)

        # Remove old inline completions if present (migration from v1)
        data.pop("completions", None)
        data["summary"] = summary
        data["entries"] = entries
        write_json_atomic(daily_file, data)

    # Write completions to sidecar file
    if collect_completions or refresh_calendar:
        write_json_atomic(completions_file, completions)

    print(f"Rollup complete for {date_str}:")
    if recovery:
        print(f"  Failed analyses: {dict(recovery)}")
    if has_entries:
        print(f"  Entries: {len(entries)}")
        print(f"  Total: {summary['total_tracked_minutes']}min")
        print(f"  Work: {summary['work_minutes']}min, Personal: {summary['personal_minutes']}min, "
              f"Unknown: {summary['unknown_minutes']}min")
        print(f"  Projects: {len(summary['by_project'])}")
    else:
        print(f"  No screenshot data (completions only)")
    if summary["display_on_minutes"] is not None:
        print(f"  Display on: {summary['display_on_minutes']}min, coverage {summary['coverage']}")
    print(f"  Git commits: {summary['git_commit_count']}")
    if summary["interactive_roots_started"] is None:
        print("  Interactive conversations started: unavailable (legacy data lacks provenance)")
    else:
        print(f"  Interactive conversations started: {summary['interactive_roots_started']}")
    if summary["total_agent_session_count"] != summary["interactive_roots_started"]:
        print(f"  Total agent session records: {summary['total_agent_session_count']}")
    print(f"  Calendar events: {summary['calendar_event_count']}")
    print(f"  Emails sent: {summary['emails_sent_count']}")
    print(f"  Google Docs edited: {summary['google_docs_edited_count']}")
    calendar_count = sum(1 for e in entries if e.get("calendar_attributed"))
    if calendar_count:
        print(f"  Attributed from calendar: {calendar_count} entries")
    if collect_completions:
        print(f"  Completions: {completions_file}")
    else:
        print("  Completions: preserved (collection skipped)")

    # Alert on collector errors
    collector_errors = completions.get("_errors", [])
    if collect_completions and collector_errors:
        send_alert(
            "critical",
            f"Rollup collector errors ({date_str})",
            "\n".join(collector_errors),
        )

    if collect_completions and low_coverage:
        send_alert(
            "warning",
            f"Day tracker missed time ({date_str})",
            f"Tracked {summary['total_tracked_minutes']} of {summary['display_on_minutes']} display-on minutes "
            f"({summary['coverage']:.0%}). Check `scheduler status` and "
            "~/Documents/day-tracker/data/logs/launchd.log.",
        )

    # Alert if no screenshot data for 3+ consecutive days
    if collect_completions and not has_entries:
        consecutive_empty = 1
        check_date = datetime.strptime(date_str, "%Y-%m-%d")
        for i in range(1, 5):  # check up to 4 days back
            prev = (check_date - timedelta(days=i)).strftime("%Y-%m-%d")
            prev_file = DAILY_DIR / f"{prev}.json"
            if prev_file.exists():
                try:
                    with open(prev_file) as f:
                        prev_data = json.load(f)
                    if len(prev_data.get("entries", [])) > 0:
                        break
                except (json.JSONDecodeError, OSError):
                    pass
            consecutive_empty += 1

        if consecutive_empty >= 3:
            send_alert(
                "critical",
                f"No screenshot data for {consecutive_empty} consecutive days",
                f"Last {consecutive_empty} days have no screenshot entries. "
                "The day-tracker capture may be broken or the Mac has been off.",
            )

    return True


def main():
    parser = argparse.ArgumentParser(description="Post-process a day-tracker log")
    parser.add_argument("date", nargs="?", help="Date to process (YYYY-MM-DD)")
    parser.add_argument(
        "--skip-completions",
        action="store_true",
        help="Refresh screenshot rollup without recollecting or overwriting completion signals",
    )
    parser.add_argument(
        "--refresh-calendar",
        action="store_true",
        help="With --skip-completions, recollect calendar events into the existing sidecar",
    )
    parser.add_argument(
        "--skip-reanalysis",
        action="store_true",
        help="Do not retry failed analyses (no Gemini calls for screenshots)",
    )
    args = parser.parse_args()
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")

    # Validate date format
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        print(f"Invalid date format: {date_str} (expected YYYY-MM-DD)")
        sys.exit(1)

    try:
        run_rollup(date_str, collect_completions=not args.skip_completions,
                   reanalyse=not args.skip_reanalysis, refresh_calendar=args.refresh_calendar)
    except Exception as e:
        send_alert("critical", f"Rollup script crashed ({date_str})", str(e))
        raise


if __name__ == "__main__":
    ensure_yaml_interpreter()
    main()
