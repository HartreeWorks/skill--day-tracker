#!/usr/bin/env python3
"""Weekly time digest for day-tracker.

Generates a time-by-project table for a given week, consuming daily summaries
produced by daily-rollup.py (falling back to raw entry analysis if unavailable).

Usage:
    python3 weekly-digest.py --week 2026-W05
    python3 weekly-digest.py --start 2026-01-26 --end 2026-01-30
    python3 weekly-digest.py --week 2026-W05 --json

Output is a human-readable table by default, or structured JSON with --json.
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path.home() / "Documents" / "day-tracker" / "data"
DAILY_DIR = DATA_DIR / "daily"
PROJECTS_YAML = Path.home() / "Documents" / "Projects" / "projects.yaml"

CAPTURE_INTERVAL = 2
LOW_DATA_THRESHOLD = 20
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def load_project_display_names():
    """Load folder → display name mapping from projects.yaml."""
    import yaml
    if not PROJECTS_YAML.exists():
        return {}
    with open(PROJECTS_YAML) as f:
        data = yaml.safe_load(f)
    return {p["folder"]: p.get("name", p["folder"]) for p in data.get("projects", [])}


def iso_week_to_dates(week_str):
    """Convert ISO week string (YYYY-Wnn) to (monday, sunday) dates."""
    # Parse YYYY-Wnn
    year, week = week_str.split("-W")
    year, week = int(year), int(week)
    # Monday of that ISO week
    monday = datetime.strptime(f"{year}-W{week:02d}-1", "%G-W%V-%u").date()
    sunday = monday + timedelta(days=6)
    return monday, sunday


def load_daily_data(date):
    """Load daily JSON for a date. Returns (by_project dict, num_entries, low_data)."""
    date_str = date.strftime("%Y-%m-%d")
    daily_file = DAILY_DIR / f"{date_str}.json"

    if not daily_file.exists():
        return {}, 0, True

    with open(daily_file) as f:
        data = json.load(f)

    entries = data.get("entries", [])
    num_entries = len(entries)

    # Prefer rollup summary if available
    summary = data.get("summary")
    if summary and "by_project" in summary:
        return summary["by_project"], num_entries, summary.get("low_data", num_entries < LOW_DATA_THRESHOLD)

    # Fallback: compute from raw entries
    by_project = defaultdict(int)
    for e in entries:
        proj = e.get("canonical_project") or e.get("inferred_project") or "unclassified"
        by_project[proj] += CAPTURE_INTERVAL

    return dict(by_project), num_entries, num_entries < LOW_DATA_THRESHOLD


def load_daily_completions(date):
    """Load completions sidecar for a date. Returns dict or empty dict."""
    date_str = date.strftime("%Y-%m-%d")
    completions_file = DAILY_DIR / f"{date_str}.completions.json"
    if not completions_file.exists():
        return {}
    try:
        with open(completions_file) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def aggregate_completions(week_completions):
    """Aggregate completions across days into week totals."""
    from collections import Counter
    totals = {
        "git_commits_by_repo": Counter(),
        "git_commit_count": 0,
        "agent_session_count": 0,
        "calendar_event_count": 0,
        "emails_sent_count": 0,
        "google_docs_edited": [],
        "google_docs_edited_count": 0,
    }
    seen_doc_ids = set()

    for c in week_completions:
        if not c:
            continue
        for commit in c.get("git_commits", []):
            if isinstance(commit, dict) and "repo" in commit:
                totals["git_commits_by_repo"][commit["repo"]] += 1
                totals["git_commit_count"] += 1
        totals["agent_session_count"] += c.get("agent_sessions", {}).get("chat_count", 0)
        totals["calendar_event_count"] += len(c.get("calendar_events", []))
        totals["emails_sent_count"] += len(c.get("emails_sent", []))
        for doc in c.get("google_docs_edited", []):
            doc_id = doc.get("id", "")
            if doc_id and doc_id not in seen_doc_ids:
                seen_doc_ids.add(doc_id)
                totals["google_docs_edited"].append(doc.get("title", ""))
                totals["google_docs_edited_count"] += 1

    return totals


def format_hours(minutes):
    """Format minutes as ~Nh string."""
    if minutes == 0:
        return "—"
    hours = minutes / 60
    if hours < 1:
        return f"~{minutes}m"
    return f"~{hours:.0f}h"


def render_table(week_str, monday, days_data, project_names, notes, comp_totals=None):
    """Render a human-readable table."""
    sunday = monday + timedelta(days=6)
    mon_str = monday.strftime("%a %d %b")
    sun_str = sunday.strftime("%a %d %b %Y")

    lines = []
    lines.append(f"Week {week_str.split('-W')[1]} ({mon_str} – {sun_str}) — Day tracker time estimate")
    lines.append("")

    # Collect all projects across all days
    all_projects = set()
    for day in days_data:
        all_projects.update(day["by_project"].keys())

    # Sort projects by total time descending, unclassified last
    project_totals = {}
    for proj in all_projects:
        project_totals[proj] = sum(d["by_project"].get(proj, 0) for d in days_data)

    sorted_projects = sorted(
        all_projects,
        key=lambda p: (p == "unclassified", -project_totals[p])
    )

    # Determine column widths
    name_width = max(
        len(project_names.get(p, p.replace("-", " ").title())) for p in sorted_projects
    ) if sorted_projects else 20
    name_width = max(name_width, 24)
    col_width = 6

    # Header
    header = f"{'Project':<{name_width}}"
    for i, d in enumerate(days_data):
        day_name = DAY_NAMES[d["date"].weekday()]
        header += f"  {day_name:>{col_width}}"
    header += f"  {'Total':>{col_width}}"
    lines.append(header)
    lines.append("─" * len(header))

    # Rows
    grand_totals = [0] * len(days_data)
    grand_total = 0

    for proj in sorted_projects:
        display = project_names.get(proj, proj.replace("-", " ").title())
        if len(display) > name_width:
            display = display[:name_width - 1] + "…"
        row = f"{display:<{name_width}}"
        row_total = 0
        for i, d in enumerate(days_data):
            mins = d["by_project"].get(proj, 0)
            row += f"  {format_hours(mins):>{col_width}}"
            row_total += mins
            grand_totals[i] += mins
        row += f"  {format_hours(row_total):>{col_width}}"
        grand_total += row_total
        lines.append(row)

    # Total row
    lines.append("─" * len(header))
    total_row = f"{'TOTAL':<{name_width}}"
    for i, gt in enumerate(grand_totals):
        total_row += f"  {format_hours(gt):>{col_width}}"
    total_row += f"  {format_hours(grand_total):>{col_width}}"
    lines.append(total_row)

    # Completions summary
    if comp_totals and comp_totals.get("git_commit_count", 0) > 0:
        lines.append("")
        lines.append("Completions:")
        lines.append(f"  Git commits: {comp_totals['git_commit_count']}")
        for repo, count in comp_totals["git_commits_by_repo"].most_common():
            lines.append(f"    {repo}: {count}")
        lines.append(f"  Agent sessions: {comp_totals['agent_session_count']}")
        lines.append(f"  Calendar events: {comp_totals['calendar_event_count']}")
        lines.append(f"  Emails sent: {comp_totals['emails_sent_count']}")
        if comp_totals["google_docs_edited_count"] > 0:
            lines.append(f"  Google Docs edited: {comp_totals['google_docs_edited_count']}")
            for title in comp_totals["google_docs_edited"]:
                lines.append(f"    {title}")

    # Notes
    if notes:
        lines.append("")
        lines.append("Notes:")
        for note in notes:
            lines.append(f"* {note}")

    return "\n".join(lines)


def render_json(week_str, monday, days_data, project_names, notes, comp_totals=None):
    """Render structured JSON output."""
    result = {
        "week": week_str,
        "start": monday.strftime("%Y-%m-%d"),
        "end": (monday + timedelta(days=6)).strftime("%Y-%m-%d"),
        "days": [],
        "totals": {},
        "notes": notes,
    }

    all_projects = set()
    for d in days_data:
        all_projects.update(d["by_project"].keys())
        result["days"].append({
            "date": d["date"].strftime("%Y-%m-%d"),
            "by_project": d["by_project"],
            "num_entries": d["num_entries"],
            "low_data": d["low_data"],
        })

    for proj in all_projects:
        result["totals"][proj] = sum(d["by_project"].get(proj, 0) for d in days_data)

    if comp_totals:
        result["completions"] = {
            "git_commit_count": comp_totals.get("git_commit_count", 0),
            "git_commits_by_repo": dict(comp_totals.get("git_commits_by_repo", {})),
            "agent_session_count": comp_totals.get("agent_session_count", 0),
            "calendar_event_count": comp_totals.get("calendar_event_count", 0),
            "emails_sent_count": comp_totals.get("emails_sent_count", 0),
            "google_docs_edited": comp_totals.get("google_docs_edited", []),
        }

    return json.dumps(result, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Weekly time digest from day-tracker")
    parser.add_argument("--week", help="ISO week (YYYY-Wnn)")
    parser.add_argument("--start", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", help="End date (YYYY-MM-DD)")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of table")
    args = parser.parse_args()

    if args.week:
        monday, sunday = iso_week_to_dates(args.week)
        week_str = args.week
    elif args.start and args.end:
        monday = datetime.strptime(args.start, "%Y-%m-%d").date()
        sunday = datetime.strptime(args.end, "%Y-%m-%d").date()
        week_str = monday.strftime("%G-W%V")
    else:
        # Default to current week
        today = datetime.now().date()
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        week_str = monday.strftime("%G-W%V")

    project_names = load_project_display_names()
    # Add unclassified display name
    project_names["unclassified"] = "Unclassified"

    days_data = []
    week_completions = []
    notes = []

    current = monday
    while current <= sunday:
        by_project, num_entries, low_data = load_daily_data(current)
        days_data.append({
            "date": current,
            "by_project": by_project,
            "num_entries": num_entries,
            "low_data": low_data,
        })
        week_completions.append(load_daily_completions(current))

        if low_data and num_entries > 0:
            day_name = DAY_NAMES[current.weekday()]
            date_str = current.strftime("%d %b")
            notes.append(
                f"{day_name} {date_str}: Tracker barely running "
                f"({num_entries} captures) — data unreliable"
            )
        elif num_entries == 0:
            day_name = DAY_NAMES[current.weekday()]
            date_str = current.strftime("%d %b")
            notes.append(f"{day_name} {date_str}: No tracker data")

        current += timedelta(days=1)

    comp_totals = aggregate_completions(week_completions)

    if args.json:
        print(render_json(week_str, monday, days_data, project_names, notes, comp_totals))
    else:
        print(render_table(week_str, monday, days_data, project_names, notes, comp_totals))


if __name__ == "__main__":
    main()
