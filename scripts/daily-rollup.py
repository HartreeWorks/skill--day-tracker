#!/usr/bin/env python3
"""Daily rollup script for day-tracker.

Post-processes the raw daily JSON to:
1. Map inferred_project values to canonical project names via fuzzy matching
2. Fill gaps in unclassified entries between same-project entries
3. Populate the summary field with time breakdowns

Usage:
    python3 daily-rollup.py [YYYY-MM-DD]

If no date is given, defaults to today.
Idempotent — safe to re-run.
"""

import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

# Allow imports from parent directory (for config module)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATA_DIR = Path.home() / "Documents" / "day-tracker" / "data"
DAILY_DIR = DATA_DIR / "daily"
PROJECTS_YAML = Path.home() / "Documents" / "Projects" / "projects.yaml"

# Capture interval in minutes (used for time estimation)
CAPTURE_INTERVAL = 2
# Gap threshold for attributing unclassified entries (minutes)
GAP_THRESHOLD = 15
# Minimum captures to consider data reliable
LOW_DATA_THRESHOLD = 20


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
        # Folder without date prefix (e.g. "forethought-ai-uplift")
        parts = folder.split("-", 3)
        if len(parts) >= 4 and parts[0].isdigit() and parts[1].isdigit():
            short = "-".join(parts[2:])
            alias_map[short.lower()] = folder
    return alias_map


def strip_date_prefix(value):
    """Strip YYYY-MM- or YYYY-MM-DD- date prefixes from a project string."""
    import re
    return re.sub(r"^\d{4}-\d{2}(-\d{2})?-", "", value)


def fuzzy_match_project(value, alias_map, projects, threshold=0.6):
    """Match a project string to a canonical project name.

    Tries exact match first, then stripped-prefix match, then fuzzy matching.
    Returns (canonical_folder, confidence) or (None, 0).
    """
    if not value:
        return None, 0

    val_lower = value.lower().strip()

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
        if entry.get("canonical_project"):
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


def compute_summary(entries):
    """Compute time summary from entries."""
    total_minutes = len(entries) * CAPTURE_INTERVAL
    work_minutes = sum(CAPTURE_INTERVAL for e in entries if e.get("is_work", True))
    personal_minutes = total_minutes - work_minutes

    by_project = defaultdict(int)
    by_category = defaultdict(int)

    for e in entries:
        proj = e.get("canonical_project") or e.get("inferred_project") or "unclassified"
        by_project[proj] += CAPTURE_INTERVAL
        cat = e.get("category", "other")
        by_category[cat] += CAPTURE_INTERVAL

    low_data = len(entries) < LOW_DATA_THRESHOLD

    return {
        "total_tracked_minutes": total_minutes,
        "work_minutes": work_minutes,
        "personal_minutes": personal_minutes,
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


def run_rollup(date_str):
    """Run the daily rollup for a given date.

    Processes screenshot entries if available (project mapping, gap filling,
    summary). Always runs completion collectors and writes the sidecar file,
    even on days with no screenshot data.
    """
    daily_file = DAILY_DIR / f"{date_str}.json"
    has_entries = False

    if daily_file.exists():
        with open(daily_file) as f:
            data = json.load(f)
        entries = data.get("entries", [])
        has_entries = len(entries) > 0
    else:
        data = {"date": date_str, "entries": []}
        entries = []

    # Process screenshot entries if present
    if has_entries:
        projects = load_projects()
        alias_map = build_alias_map(projects)

        for entry in entries:
            inferred = entry.get("inferred_project")
            canonical, confidence = fuzzy_match_project(inferred, alias_map, projects)
            entry["canonical_project"] = canonical

        try:
            from config import load_config
            config = load_config()
            for entry in entries:
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
                        entry["canonical_project"] = rule["project"]
                    if "is_work" in rule:
                        entry["is_work"] = rule["is_work"]
                    break
        except Exception as e:
            print(f"Warning: Could not apply app_rules: {e}")

        fill_gaps(entries)
        summary = compute_summary(entries)
    else:
        summary = {
            "total_tracked_minutes": 0,
            "work_minutes": 0,
            "personal_minutes": 0,
            "by_project": {},
            "by_category": {},
            "low_data": True,
            "rollup_timestamp": datetime.now().isoformat(),
        }

    # Always collect completion signals
    from collectors import collect_all
    completions = collect_all(date_str)

    # Add completion counts to summary
    git_commits = completions.get("git_commits", [])
    summary["git_commit_count"] = len([c for c in git_commits if isinstance(c, dict) and "repo" in c])

    agent_sessions = completions.get("agent_sessions", {})
    summary["agent_session_count"] = agent_sessions.get("chat_count", 0)

    calendar_events = completions.get("calendar_events", [])
    summary["calendar_event_count"] = len(calendar_events)

    emails_sent = completions.get("emails_sent", [])
    summary["emails_sent_count"] = len(emails_sent)

    google_docs = completions.get("google_docs_edited", [])
    summary["google_docs_edited_count"] = len(google_docs)

    # Remove old inline completions if present (migration from v1)
    data.pop("completions", None)

    data["summary"] = summary
    data["entries"] = entries

    # Write main daily file (entries + summary only)
    with open(daily_file, "w") as f:
        json.dump(data, f, indent=2)

    # Write completions to sidecar file
    completions_file = DAILY_DIR / f"{date_str}.completions.json"
    with open(completions_file, "w") as f:
        json.dump(completions, f, indent=2)

    print(f"Rollup complete for {date_str}:")
    if has_entries:
        print(f"  Entries: {len(entries)}")
        print(f"  Total: {summary['total_tracked_minutes']}min")
        print(f"  Work: {summary['work_minutes']}min, Personal: {summary['personal_minutes']}min")
        print(f"  Projects: {len(summary['by_project'])}")
    else:
        print(f"  No screenshot data (completions only)")
    print(f"  Git commits: {summary['git_commit_count']}")
    print(f"  Agent sessions: {summary['agent_session_count']}")
    print(f"  Calendar events: {summary['calendar_event_count']}")
    print(f"  Emails sent: {summary['emails_sent_count']}")
    print(f"  Google Docs edited: {summary['google_docs_edited_count']}")
    print(f"  Completions: {completions_file}")

    # Alert on collector errors
    collector_errors = completions.get("_errors", [])
    if collector_errors:
        send_alert(
            "critical",
            f"Rollup collector errors ({date_str})",
            "\n".join(collector_errors),
        )

    # Alert if no screenshot data for 3+ consecutive days
    if not has_entries:
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
    if len(sys.argv) > 1:
        date_str = sys.argv[1]
    else:
        date_str = datetime.now().strftime("%Y-%m-%d")

    # Validate date format
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        print(f"Invalid date format: {date_str} (expected YYYY-MM-DD)")
        sys.exit(1)

    try:
        run_rollup(date_str)
    except Exception as e:
        send_alert("critical", f"Rollup script crashed ({date_str})", str(e))
        raise


if __name__ == "__main__":
    main()
