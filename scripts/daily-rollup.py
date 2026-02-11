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
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

DATA_DIR = Path.home() / "Documents" / "day-tracker" / "data"
DAILY_DIR = DATA_DIR / "daily"
PROJECTS_YAML = Path.home() / "Documents" / "Projects" / "projects.yaml"

# Capture interval in minutes (used for time estimation)
CAPTURE_INTERVAL = 5
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


def run_rollup(date_str):
    """Run the daily rollup for a given date."""
    daily_file = DAILY_DIR / f"{date_str}.json"
    if not daily_file.exists():
        print(f"No daily file found for {date_str}")
        return False

    with open(daily_file) as f:
        data = json.load(f)

    entries = data.get("entries", [])
    if not entries:
        print(f"No entries for {date_str}")
        return False

    # Load projects and build alias map
    projects = load_projects()
    alias_map = build_alias_map(projects)

    # Step 1: Map inferred_project to canonical_project
    for entry in entries:
        inferred = entry.get("inferred_project")
        canonical, confidence = fuzzy_match_project(inferred, alias_map, projects)
        entry["canonical_project"] = canonical

    # Step 2: Fill gaps
    fill_gaps(entries)

    # Step 3: Compute summary
    data["summary"] = compute_summary(entries)
    data["entries"] = entries

    # Write back
    with open(daily_file, "w") as f:
        json.dump(data, f, indent=2)

    summary = data["summary"]
    print(f"Rollup complete for {date_str}:")
    print(f"  Entries: {len(entries)}")
    print(f"  Total: {summary['total_tracked_minutes']}min")
    print(f"  Work: {summary['work_minutes']}min, Personal: {summary['personal_minutes']}min")
    print(f"  Projects: {len(summary['by_project'])}")
    if summary["low_data"]:
        print(f"  ⚠ Low data ({len(entries)} captures < {LOW_DATA_THRESHOLD})")

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

    success = run_rollup(date_str)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
