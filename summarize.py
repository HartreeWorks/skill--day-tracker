#!/usr/bin/env python3
"""
DayLogger Daily Summarizer

Generates a narrative summary of the day's activities using AI.
Can be run manually or via launchd at end of day.
"""

import os
import json
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
from typing import Optional

from config import DAILY_DIR, CAPTURES_DIR, load_config
from models import DailyLog, CaptureMetadata


def get_gemini_client():
    """Get configured Gemini client."""
    import google.generativeai as genai

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        try:
            import subprocess
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "daylogger-gemini", "-w"],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                api_key = result.stdout.strip()
        except Exception:
            pass

    if not api_key:
        raise ValueError("No Gemini API key found")

    genai.configure(api_key=api_key)
    return genai


def format_duration(minutes: int) -> str:
    """Format minutes as hours and minutes."""
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    mins = minutes % 60
    if mins == 0:
        return f"{hours}h"
    return f"{hours}h {mins}m"


def calculate_stats(daily_log: DailyLog, interval_minutes: int = 5, max_gap_minutes: int = 15) -> dict:
    """Calculate time statistics from daily log using actual timestamps.

    Instead of assuming each entry represents a fixed interval, we calculate
    the actual time between consecutive entries, capped at max_gap_minutes
    to avoid counting breaks/gaps as work time.

    Args:
        daily_log: The daily log to analyze
        interval_minutes: Default interval for the last entry (no next entry to measure against)
        max_gap_minutes: Maximum time to attribute to any single entry (caps long gaps)
    """
    by_category = defaultdict(int)
    by_project = defaultdict(int)
    work_minutes = 0
    personal_minutes = 0
    timeline = []

    # Filter out sensitive entries first
    entries = [e for e in daily_log.entries if not e.sensitive]

    # Parse timestamps and sort by time
    entries_with_ts = []
    for entry in entries:
        try:
            # Handle various timestamp formats
            ts_str = entry.timestamp
            if '.' in ts_str and '+' not in ts_str and 'Z' not in ts_str:
                # Has microseconds but no timezone
                ts = datetime.fromisoformat(ts_str)
            else:
                ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
            entries_with_ts.append((ts, entry))
        except (ValueError, AttributeError):
            # Skip entries with unparseable timestamps
            continue

    entries_with_ts.sort(key=lambda x: x[0])

    for i, (ts, entry) in enumerate(entries_with_ts):
        # Calculate minutes for this entry
        if i + 1 < len(entries_with_ts):
            # Time until next entry, capped at max_gap_minutes
            next_ts = entries_with_ts[i + 1][0]
            gap = (next_ts - ts).total_seconds() / 60
            minutes = min(gap, max_gap_minutes)
        else:
            # Last entry: use default interval
            minutes = interval_minutes

        # Round to nearest minute
        minutes = round(minutes)
        if minutes < 1:
            minutes = 1  # Minimum 1 minute per entry

        by_category[entry.category] += minutes

        if entry.is_work:
            work_minutes += minutes
        else:
            personal_minutes += minutes

        # Use inferred_project if available, else manual/auto project, else untagged
        project = entry.inferred_project or entry.project or "untagged"
        by_project[project] += minutes

        timeline.append({
            "time": entry.timestamp,
            "activity": entry.oneline,
            "category": entry.category,
            "project": entry.project,
            "inferred_project": entry.inferred_project,
            "is_work": entry.is_work,
            "minutes": minutes
        })

    total_minutes = sum(by_category.values())

    return {
        "total_minutes": total_minutes,
        "total_hours": round(total_minutes / 60, 1),
        "work_minutes": work_minutes,
        "personal_minutes": personal_minutes,
        "by_category": dict(by_category),
        "by_project": dict(by_project),
        "timeline": timeline,
        "entry_count": len(entries)
    }


def generate_enhanced_summary(daily_log: DailyLog, interval_minutes: int = 5, max_gap_minutes: int = 15) -> dict:
    """Generate rich daily summary for Chief of Staff by reading metadata files.

    This aggregates detailed context (people, organizations) from individual
    capture metadata files, which isn't stored in the lightweight daily log entries.

    Uses actual timestamps to calculate time, capped at max_gap_minutes per entry.
    """
    work_minutes = 0
    personal_minutes = 0
    by_project = defaultdict(int)
    by_category = defaultdict(int)
    all_people = set()
    all_orgs = set()

    # Filter out sensitive entries
    entries = [e for e in daily_log.entries if not e.sensitive]

    # Parse timestamps and sort
    entries_with_ts = []
    for entry in entries:
        try:
            ts_str = entry.timestamp
            if '.' in ts_str and '+' not in ts_str and 'Z' not in ts_str:
                ts = datetime.fromisoformat(ts_str)
            else:
                ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
            entries_with_ts.append((ts, entry))
        except (ValueError, AttributeError):
            continue

    entries_with_ts.sort(key=lambda x: x[0])

    for i, (ts, entry) in enumerate(entries_with_ts):
        # Calculate minutes for this entry based on gap to next
        if i + 1 < len(entries_with_ts):
            next_ts = entries_with_ts[i + 1][0]
            gap = (next_ts - ts).total_seconds() / 60
            minutes = min(gap, max_gap_minutes)
        else:
            minutes = interval_minutes

        minutes = round(minutes)
        if minutes < 1:
            minutes = 1

        by_category[entry.category] += minutes

        # Read the full metadata to get people and organizations
        metadata_path = CAPTURES_DIR / entry.capture_dir / "metadata.json"
        if metadata_path.exists():
            try:
                metadata = CaptureMetadata.load(metadata_path)
                if metadata.analysis:
                    all_people.update(metadata.analysis.people or [])
                    all_orgs.update(metadata.analysis.organizations or [])
            except Exception:
                pass

        # Track work/personal
        if entry.is_work:
            work_minutes += minutes
        else:
            personal_minutes += minutes

        # Track project time
        project = entry.inferred_project or entry.project
        if project:
            by_project[project] += minutes

    return {
        "total_tracked_minutes": work_minutes + personal_minutes,
        "work_minutes": work_minutes,
        "personal_minutes": personal_minutes,
        "by_project": dict(by_project),
        "by_category": dict(by_category),
        "people_interacted": sorted(all_people),
        "organizations_touched": sorted(all_orgs)
    }


def generate_narrative(stats: dict, date_str: str) -> str:
    """Generate AI narrative summary of the day."""
    try:
        genai = get_gemini_client()
    except ValueError as e:
        return f"*Could not generate narrative: {e}*"

    # Build prompt with day's data
    timeline_text = "\n".join([
        f"- {e['time'].split('T')[1][:5]}: {e['activity']} ({e['category']}, {e['project'] or 'untagged'})"
        for e in stats["timeline"][:50]  # Limit to avoid token limits
    ])

    category_breakdown = "\n".join([
        f"- {cat}: {format_duration(mins)}"
        for cat, mins in sorted(stats["by_category"].items(), key=lambda x: -x[1])
    ])

    project_breakdown = "\n".join([
        f"- {proj}: {format_duration(mins)}"
        for proj, mins in sorted(stats["by_project"].items(), key=lambda x: -x[1])
    ])

    prompt = f"""Write a brief narrative summary (2-3 paragraphs) of this person's work day.
Focus on:
1. Main accomplishments and focus areas
2. How time was distributed
3. Any notable patterns or observations

Be concise, professional, and insightful. Write in third person ("The user...").

DATE: {date_str}
TOTAL TRACKED TIME: {format_duration(stats['total_minutes'])}

TIME BY CATEGORY:
{category_breakdown}

TIME BY PROJECT:
{project_breakdown}

ACTIVITY TIMELINE (chronological samples):
{timeline_text}
"""

    try:
        model = genai.GenerativeModel("gemini-2.0-flash-exp")
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        return f"*Error generating narrative: {e}*"


def generate_summary(date_str: Optional[str] = None, save: bool = True) -> str:
    """
    Generate a complete daily summary.

    Args:
        date_str: Date in YYYY-MM-DD format (defaults to today)
        save: Whether to save the summary to a file

    Returns:
        The generated summary as markdown
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    daily_file = DAILY_DIR / f"{date_str}.json"

    if not daily_file.exists():
        return f"No data found for {date_str}"

    # Load and calculate stats
    daily_log = DailyLog.load(daily_file)
    config = load_config()
    stats = calculate_stats(daily_log, config.capture_interval_minutes)

    if stats["entry_count"] == 0:
        return f"No entries for {date_str}"

    # Generate enhanced summary for Chief of Staff integration
    enhanced_summary = generate_enhanced_summary(daily_log, config.capture_interval_minutes)

    # Save enhanced summary to daily log
    daily_log.summary = enhanced_summary
    if save:
        daily_log.save(daily_file)

    # Generate narrative
    narrative = generate_narrative(stats, date_str)

    # Format date for display
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        date_display = date_obj.strftime("%A, %B %d, %Y")
    except ValueError:
        date_display = date_str

    # Build markdown summary
    summary = f"""# Daily Summary: {date_display}

## Overview

- **Total Tracked Time:** {format_duration(stats['total_minutes'])}
- **Work Time:** {format_duration(stats['work_minutes'])}
- **Personal Time:** {format_duration(stats['personal_minutes'])}
- **Captures:** {stats['entry_count']}

## Time by Category

| Category | Time | % |
|----------|------|---|
"""

    for cat, mins in sorted(stats["by_category"].items(), key=lambda x: -x[1]):
        pct = round(100 * mins / stats["total_minutes"]) if stats["total_minutes"] > 0 else 0
        summary += f"| {cat} | {format_duration(mins)} | {pct}% |\n"

    summary += """
## Time by Project

| Project | Time | % |
|---------|------|---|
"""

    for proj, mins in sorted(stats["by_project"].items(), key=lambda x: -x[1]):
        pct = round(100 * mins / stats["total_minutes"]) if stats["total_minutes"] > 0 else 0
        summary += f"| {proj} | {format_duration(mins)} | {pct}% |\n"

    # Add people and organizations if available
    if enhanced_summary.get("people_interacted"):
        summary += f"""
## People interacted with

{', '.join(enhanced_summary['people_interacted'])}
"""

    if enhanced_summary.get("organizations_touched"):
        summary += f"""
## Organizations touched

{', '.join(enhanced_summary['organizations_touched'])}
"""

    summary += f"""
## Narrative

{narrative}

## For AI Coach

```json
{json.dumps({
    "date": date_str,
    "total_hours": stats["total_hours"],
    "work_minutes": stats["work_minutes"],
    "personal_minutes": stats["personal_minutes"],
    "categories": stats["by_category"],
    "projects": stats["by_project"],
    "people": enhanced_summary.get("people_interacted", []),
    "organizations": enhanced_summary.get("organizations_touched", []),
    "entry_count": stats["entry_count"]
}, indent=2)}
```

---
*Generated by DayLogger at {datetime.now().strftime("%Y-%m-%d %H:%M")}*
"""

    # Save markdown summary
    if save:
        summary_file = DAILY_DIR / f"{date_str}-summary.md"
        with open(summary_file, "w") as f:
            f.write(summary)
        print(f"Summary saved to {summary_file}")

    return summary


def main():
    """Entry point for summarizer."""
    import argparse

    parser = argparse.ArgumentParser(description="DayLogger Daily Summarizer")
    parser.add_argument("date", nargs="?", help="Date to summarize (YYYY-MM-DD, defaults to today)")
    parser.add_argument("--no-save", action="store_true", help="Don't save the summary file")
    parser.add_argument("--yesterday", "-y", action="store_true", help="Summarize yesterday")
    args = parser.parse_args()

    if args.yesterday:
        date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        date_str = args.date

    summary = generate_summary(date_str, save=not args.no_save)
    print(summary)


if __name__ == "__main__":
    main()
