---
name: day-tracker
description: This skill should be used when the user says "day tracker", "day-tracker", "time tracking", "capture screenshots", "what was I working on", "what was I doing", "what have I been doing", "recent activity", "activity digest", "recap", "generate daily summary", or mentions tracking work time with screenshots. A local-first screenshot-based time tracker with AI analysis.
---

# Day Tracker

A local-first screenshot-based time tracker with AI analysis.

## Overview

Day Tracker captures screenshots every 2 minutes, analyzes them with Gemini 2.5 Flash-Lite to understand what you're working on, and provides:
- Timeline view of your day (multi-monitor support)
- Time breakdown by category and project
- AI-generated daily summaries
- Export for invoicing and AI coaching
- Sensitive content detection (API keys, passwords)

## Quick Start

```bash
# Run a capture manually
~/.claude/skills/day-tracker/DayTrackerCapture.app/Contents/MacOS/DayTrackerCapture

# Start the web UI
python3 ~/.claude/skills/day-tracker/server.py
# Open http://localhost:8765
```

## Scheduled Capture

The capture runs every 2 minutes via the `schedule-task` skill:

```bash
# View status
python3 ~/.claude/skills/schedule-task/scripts/scheduler.py list

# Disable temporarily
python3 ~/.claude/skills/schedule-task/scripts/scheduler.py disable --name day-tracker-capture

# Re-enable
python3 ~/.claude/skills/schedule-task/scripts/scheduler.py enable --name day-tracker-capture

# View logs
cat /tmp/claude-scheduled-day-tracker-capture.log
```

## macOS Permissions

Two helper apps require permissions (grant via System Settings > Privacy & Security):

| App | Permission | Purpose |
|-----|------------|---------|
| `DayTrackerCapture.app` | Screen Recording | Capture screenshots |
| `DayTrackerHelper.app` | Accessibility | Get active window info |

## CLI Commands

```bash
# Check status
python3 ~/.claude/skills/day-tracker/cli.py status

# Pause capturing for 1 hour
python3 ~/.claude/skills/day-tracker/cli.py pause 1h

# Resume capturing
python3 ~/.claude/skills/day-tracker/cli.py pause

# Tag last 30 minutes to a project
python3 ~/.claude/skills/day-tracker/cli.py tag "project-name" --last 30

# Quick recap of recent activity (default: last 30 mins before most recent capture)
python3 ~/.claude/skills/day-tracker/cli.py digest

# Recap of last 2 hours
python3 ~/.claude/skills/day-tracker/cli.py digest --minutes 120

# Generate today's summary
python3 ~/.claude/skills/day-tracker/cli.py summary

# Run daily rollup (consolidate projects, fill gaps, compute summary)
python3 ~/.claude/skills/day-tracker/scripts/daily-rollup.py [YYYY-MM-DD]

# Generate weekly time digest (human-readable table)
python3 ~/.claude/skills/day-tracker/scripts/weekly-digest.py --week 2026-W05

# Weekly digest with date range
python3 ~/.claude/skills/day-tracker/scripts/weekly-digest.py --start 2026-01-26 --end 2026-02-01

# Weekly digest as JSON
python3 ~/.claude/skills/day-tracker/scripts/weekly-digest.py --week 2026-W05 --json
```

### Daily rollup

The daily rollup (`scripts/daily-rollup.py`) post-processes raw daily JSON:

* **Project consolidation** — maps `inferred_project` to canonical names from `projects.yaml` via fuzzy matching
* **Gap filling** — attributes unclassified entries between same-project entries (within 15-min window)
* **Summary generation** — populates the `summary` field with time breakdowns by project and category

Runs at 23:55 daily via `schedule-task`. Idempotent — safe to re-run.

### Weekly digest

The weekly digest (`scripts/weekly-digest.py`) produces a time-by-project table for a given week. Uses daily rollup summaries if available, falls back to raw entry analysis. Flags days with low capture counts.

## Categories

Categories are split into work and personal for better activity tracking:

**Work categories:**
- `coding` - Programming, debugging, code review
- `writing` - Documents, emails, content creation
- `research` - Reading, learning, investigation
- `meetings` - Video calls, in-person meetings
- `communication` - Slack, email, messaging for work
- `admin` - Admin tasks, invoicing, planning
- `design` - Visual design, UI work

**Personal categories:**
- `personal_admin` - Banking, shopping, personal errands
- `social` - Social media, messaging friends
- `entertainment` - Videos, games, leisure browsing
- `break` - Away from desk, idle

**Fallback:**
- `other` - Unclassified activity

## Project inference

The AI automatically infers which project you're working on by matching visible content (repo names, URLs, Slack channels, document titles) against your projects list in `~/Documents/Projects/projects.yaml`.

Each capture includes:
- `inferred_project` - The matched project folder name (or null)
- `project_confidence` - Confidence score (0-1)

## Configuration

Edit `~/Documents/day-tracker/data/config.json`:

```json
{
  "capture_interval_minutes": 2,
  "gemini_model": "gemini-2.5-flash-lite",
  "skip_similar_threshold": 0.02,
  "project_patterns": [
    {"pattern": "github.com/myproject", "project": "myproject"},
    {"pattern": "ClientName.*Slack", "project": "client-work"}
  ],
  "sensitive_window_patterns": [
    "1Password",
    "Keychain",
    ".env"
  ],
  "auto_delete_sensitive": false
}
```

### Configuration options

| Option | Default | Description |
|--------|---------|-------------|
| `skip_similar_threshold` | 0.02 | Skip capture if screen changed less than this % (0 to disable) |
| `jpeg_quality` | 70 | JPEG compression quality (1-100) |
| `auto_delete_sensitive` | false | Auto-delete captures with detected sensitive content |
| `blank_desktop_threshold` | 0.05 | Exclude screens showing desktop wallpaper (0 to disable) |
| `blank_desktop_crop_top` | 50 | Pixels to crop from top before wallpaper comparison (removes menu bar + notch) |

## Blank Desktop Detection

Screens showing only the desktop wallpaper are automatically excluded from AI analysis, saving tokens and avoiding misinterpretation.

### Setup

1. Close all windows so all monitors show only your desktop wallpaper
2. Run the capture command:
```bash
python3 ~/.claude/skills/day-tracker/cli.py capture-wallpaper
```

This saves reference images for each screen to `~/Documents/day-tracker/data/reference-wallpapers/`.

### How It Works

- Before sending screenshots to Gemini, each screen is compared against its reference wallpaper
- The top 50 pixels are cropped (configurable) to ignore the menu bar/notch which changes with time/status
- If a screen matches the wallpaper within 5% difference (configurable), it's excluded from analysis
- If ALL screens are blank, the entire capture is skipped

## API Key Setup

Store your Gemini API key in the macOS keychain:

```bash
security add-generic-password -s daylogger-gemini -a gemini -w 'YOUR_API_KEY'
```

Get a key from: https://aistudio.google.com/apikey

## Data location

All data is stored in `~/Documents/day-tracker/data/`:

- Screenshots: `data/captures/YYYY-MM-DD/HH-MM-SS/`
- Daily logs: `data/daily/YYYY-MM-DD.json`
- Config: `data/config.json`

## Daily JSON structure

The daily log (`YYYY-MM-DD.json`) includes an enhanced `summary` object for Chief of Staff integration:

```json
{
  "date": "2026-01-16",
  "entries": [...],
  "summary": {
    "total_tracked_minutes": 480,
    "work_minutes": 420,
    "personal_minutes": 60,
    "by_project": {
      "website-redesign": 180,
      "api-integration": 120
    },
    "by_category": {
      "coding": 240,
      "meetings": 90,
      "communication": 60
    },
    "people_interacted": ["Alice Smith", "Bob Jones"],
    "organizations_touched": ["Acme Corp", "Example Inc"]
  }
}
```

Each entry also includes:
- `is_work` - Boolean indicating work vs personal activity
- `inferred_project` - AI-matched project folder name (or null)

## Web UI

Access at http://localhost:8765 when the server is running.

Features:
- Timeline view with thumbnails
- Multi-monitor: click to see all screens, click again for fullscreen
- Filter by date
- 7-day API cost tracking with Google Cloud link
- Export for invoicing (`/export/invoice?start=...&end=...&project=...`)

## Cost

Using Gemini 2.5 Flash-Lite: approx. $0.10/day ($3/month) at 720 captures/day.
