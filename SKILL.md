---
name: day-tracker
description: Summarise recent screenshot-based activity and time tracking.
---

# Day Tracker

A screenshot-based time tracker with local storage and Gemini API analysis.

For setup on another computer, read [README.md](./README.md) first. It distinguishes the core capture loop from optional personal integrations and explains the external scheduling and browser-extension pieces. Adapt paths and account settings before running; installing the skill alone does not configure capture.

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
python3 ~/.claude/skills/day-tracker/capture.py

# Start the web UI
python3 ~/.claude/skills/day-tracker/server.py
# Open http://localhost:8765
```

## Scheduled Capture

The capture runs every 2 minutes via a launchd agent (`com.ph.daytracker.v2`):

```bash
# View status
launchctl print gui/$(id -u)/com.ph.daytracker.v2

# View logs
cat ~/Documents/day-tracker/data/logs/launchd.log

# Disable temporarily
launchctl bootout gui/$(id -u)/com.ph.daytracker.v2

# Re-enable
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ph.daytracker.v2.plist

# Force an immediate run
launchctl kickstart gui/$(id -u)/com.ph.daytracker.v2
```

Plist: `~/Library/LaunchAgents/com.ph.daytracker.v2.plist`
Wrapper: `~/.claude/skills/day-tracker/capture-launchd.py` (calls `run_capture()` directly; `capture.py`'s `main()` exits with code 78 under launchd for unknown reasons).

## macOS Permissions

Grant permissions via System Settings > Privacy & Security, then verify a manual capture and a scheduled capture:

| App | Permission | Purpose |
|-----|------------|---------|
| The process running capture (Python, its launcher or a locally built wrapper) | Screen Recording | Capture screenshots |
| `DayTrackerHelper.app` | Accessibility | Get active window info |
| `DayTrackerHelper.app` | Automation (control browsers) | Detect incognito/private windows (see below) |

For incognito detection, macOS will prompt once with "DayTrackerHelper wants to control Google Chrome". Grant it under System Settings > Privacy & Security > Automation. The grant attaches to the helper bundle and persists under launchd. **Detection is fail-open:** if Chrome can't be queried (permission not granted, browser busy), incognito windows are NOT caught and the capture proceeds — so the grant must be in place for protection to work. Watch for `incognito_detection_unavailable` / `incognito_detection_partial` events in `launchd.log` if you suspect the grant has lapsed.

## Incognito / private window detection

Screenshots of any display showing an **incognito/private browser window** are discarded before analysis — never sent to Gemini, never stored (deleted outright, not even kept as a thumbnail, since a thumbnail would still leak content). This mirrors blank-screen handling: per-display discard, and if *every* display shows incognito the whole capture is skipped.

Detection is **visibility-aware**, not just geometric: a display is only dropped where the incognito window has actually visible (unoccluded) on-screen area. An incognito window that is minimised, hidden, on another Space, or fully buried behind other windows does **not** trigger a discard — only what would really appear in the screenshot. Visibility is computed from the on-screen window list in front-to-back z-order (`CGWindowListCopyWindowInfo`), subtracting any windows stacked in front. A window must expose more than ~64×64 px (`INCOGNITO_VISIBLE_AREA_THRESHOLD` in `capture.py`) to count, so shadow-edge slivers don't cause false discards.

- **Covered:** Google Chrome only — incognito windows found via AppleScript `mode`/`bounds`, then matched to the Quartz on-screen window list for the visibility/occlusion test. (Brave, Edge and other Chromium browsers share the same dictionary and can be re-added in `SUPPORTED_BROWSERS` in the helper if ever needed.)
- **NOT covered (blind spots):** Safari Private, Arc, and Firefox private windows. Their AppleScript dictionaries don't expose private-browsing state, so these are **not** detected and will still be captured/described. If this matters, close them or pause the tracker.
- **Toggle:** `incognito_detection` in `config.json` (default `true`).
- **Log events:** `skipped_incognito` (one display dropped), `skipped_all_incognito` (whole capture skipped), `incognito_detection_unavailable` / `incognito_detection_partial` (fail-open — a browser couldn't be queried).

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
# Use the venv python: rollup/digest need PyYAML, which system/Homebrew python3 lack
~/venvs/ph/bin/python3 ~/.claude/skills/day-tracker/scripts/daily-rollup.py [YYYY-MM-DD]

# Refresh screenshot attribution without recollecting completion sidecars
~/venvs/ph/bin/python3 ~/.claude/skills/day-tracker/scripts/daily-rollup.py [YYYY-MM-DD] --skip-completions

# Generate weekly time digest (human-readable table)
~/venvs/ph/bin/python3 ~/.claude/skills/day-tracker/scripts/weekly-digest.py --week 2026-W05

# Weekly digest with date range
~/venvs/ph/bin/python3 ~/.claude/skills/day-tracker/scripts/weekly-digest.py --start 2026-01-26 --end 2026-02-01

# Weekly digest as JSON
~/venvs/ph/bin/python3 ~/.claude/skills/day-tracker/scripts/weekly-digest.py --week 2026-W05 --json
```

### Daily rollup

The daily rollup (`scripts/daily-rollup.py`) post-processes raw daily JSON:

* **Project consolidation** — maps AI inference, configured title patterns, and explicit aliases to canonical names from `projects.yaml`
* **Gap filling** — attributes unclassified entries between same-project entries (within 15-min window)
* **Summary generation** — populates the `summary` field with time breakdowns by project and category
* **Completion signals** — collects "what happened today" data from external sources into a sidecar `.completions.json` file

Runs at 23:55 daily via launchd (`com.claude.scheduled.day-tracker-rollup`). Idempotent — safe to re-run.

Plist: `~/Library/LaunchAgents/com.claude.scheduled.day-tracker-rollup.plist`
Logs: `/tmp/claude-scheduled-day-tracker-rollup.log`, `/tmp/claude-scheduled-day-tracker-rollup.err`

#### Completion data sources

| Source | CLI dependency | What it collects |
|--------|---------------|------------------|
| Git commits | `git` | All commits across `~/Documents` repos + `~/.agents` |
| Calendar events | `gog` | Both primary and meetings calendars, deduplicated |
| Agent sessions | Gemini API for titles/summaries | Claude Code + Codex sessions, grouped counts, generated titles, and per-session descriptions |
| Google Docs edited | `gog` | Docs modified on the target date |
| Emails sent | `gog` | Sent emails across all 3 Gmail accounts |
| Todoist completed | `td` | Tasks completed on the target date; launchd uses an NVM fallback if `td` is not on `PATH` |

Collector errors are logged to stderr and sent as warnings to the hartreeworks.org alerts API (requires `HARTREEWORKS_INTERNAL_API_KEY` env var).

Agent session presentation uses `gemini-3.1-flash-lite` to generate a compact title for each meaningful session. Claude Code transcript `away_summary` records are preferred as descriptions when present; otherwise Gemini also generates the description. Codex sessions always use Gemini-generated presentation fields because raw final answers are often too long or report-shaped. Calls are capped by `DAY_TRACKER_MAX_GEMINI_SESSION_SUMMARIES` (default `50`) and cached under `~/Documents/day-tracker/data/session-summaries/` by session ID, transcript mtime, model, and summariser version. Set `DAY_TRACKER_DISABLE_SESSION_SUMMARIES=1` to use cleaned local fallbacks without model calls. Each daily sidecar includes `agent_sessions.summary_stats` so token use and summary sources can be audited.

Conversation counts mean user-started interactive roots on the target date. `chat_count` and `by_project` remain compatibility aliases with that meaning. Use `interactive_roots_started` and `interactive_roots_by_project` in new consumers. `total_session_count`, `total_sessions_by_project` and `session_origin_counts` retain roots, resumed conversations, subagents, automations and unknown-origin records for diagnostics; never describe those totals as conversations the user initiated. Historical sidecars created before provenance fields existed cannot support a root-only count: report their interactive count as unavailable and show their records separately as legacy totals rather than silently returning zero or reusing the inflated all-session count. In weekly JSON, set the whole-week root count and project breakdown to `null` whenever any included day lacks provenance; expose the provenance-complete subtotal only under `known_interactive_*` keys.

For initiation audits, each session also carries provenance and burden signals: `session_origin` (`interactive_root`, `subagent`, `automation`, or `unknown`), `started_on_target_date`, `user_turn_count`, `followup_count`, `assistant_turn_count`, `assistant_visible_chars`, `duration_minutes`, `gate_disposition`, `gate_injected`, `gate_shown`, and `gate_outcome`. Codex duplicate event/response representations are counted once. Unknown provenance stays unknown rather than being treated as a root. `agent_sessions.initiation_audit` contains aggregate gate counts for interactive roots started on the target date.

### Weekly digest

The weekly digest (`scripts/weekly-digest.py`) produces a time-by-project table for a given week from the daily entries' rollup annotations. It derives totals from entries rather than trusting a cached summary, so later project remaps cannot leave stale weekly totals. It flags days with low capture counts.

Completion signals remain separate from screenshot-derived attention time. The weekly output includes commits and agent-session slices by project, but does not convert either into minutes because agents can run autonomously and in parallel.

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
| `blank_desktop_threshold` | 0.02 | Exclude a screen if app windows cover ≤ this fraction of it (0 to disable) |
| `incognito_detection` | true | Discard per-display screenshots showing a visible Chrome incognito window (see Incognito detection) |

## Blank Desktop Detection

Screens showing only the desktop wallpaper are automatically excluded from AI analysis, saving tokens and avoiding misinterpretation.

### How It Works

Detection is **window-geometry based**, not image based — it needs no reference wallpaper and works with any wallpaper (`get_blank_display_numbers` in `capture.py`):

- For each display, the on-screen window list (`CGWindowListCopyWindowInfo`, layer 0, desktop elements excluded — so Dock, menu bar, notch and wallpaper don't count) is used to compute what fraction of the display is covered by real application windows.
- If app windows cover ≤ `blank_desktop_threshold` of a display (default 2%), it shows only wallpaper and is excluded from analysis. A small always-on-top widget on an otherwise empty display falls under the threshold, so the display is still treated as blank.
- Excluded screens are saved as a small `screen-N--blank.webp` thumbnail (not sent to Gemini). If ALL screens are blank, the entire capture is skipped.
- **Fail-open:** if the window/display list can't be read, all screens are analysed (and `blank_detection_unavailable` is logged, throttled) rather than risk dropping real content.

This replaced an earlier reference-wallpaper pixel-comparison approach, which silently failed whenever the wallpaper changed (e.g. macOS's day-cycling dynamic wallpapers) or when displays were renumbered after reconnecting/rearranging monitors — blank desktops then sailed through to Gemini. The `reference-wallpapers/` directory and the old `capture-wallpaper` CLI command are no longer used.

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
  "completions": {
    "git_commits": [{"repo": "ai-wow", "message": "Fix nav", "timestamp": "14:32", "author": "Alice"}],
    "calendar_events": [{"summary": "Alice <> Bob", "start": "10:00", "end": "11:00", "calendar": "meetings"}],
    "agent_sessions": {
      "chat_count": 12,
      "interactive_roots_started": 12,
      "interactive_root_count": 14,
      "total_session_count": 31,
      "by_project": {"Skills (day-tracker)": {"type": "tools", "chat_count": 3}},
      "interactive_roots_by_project": {"Skills (day-tracker)": {"type": "tools", "chat_count": 3}},
      "session_origin_counts": {"interactive_root": 14, "subagent": 15, "automation": 1, "unknown": 1},
      "sessions": [{
        "session_id": "abc123",
        "agent": "claude",
        "project_path": "~/.agents/skills/day-tracker",
        "project": "Skills (day-tracker)",
        "session_origin": "interactive_root",
        "started_on_target_date": true,
        "followup_count": 2,
        "assistant_visible_chars": 1840,
        "duration_minutes": 18.5,
        "gate_injected": true,
        "gate_shown": true,
        "gate_outcome": "trimmed",
        "summary": "Implemented cached daily rollup completion signals.",
        "completed": ["Added Todoist completed-task collection"],
        "in_progress": [],
        "blockers": [],
        "summary_status": "generated",
        "title_source": "gemini_session_title",
        "summary_source": "claude_away_summary"
      }]
    },
    "google_docs_edited": [{"title": "W11 plan", "modified_time": "...", "id": "abc123"}],
    "emails_sent": [{"to": "alice@example.com", "subject": "Re: Monday", "timestamp": "09:45", "account": "your-email@gmail.com"}],
    "todoist_completed": [],
    "_errors": []
  },
  "summary": {
    "total_tracked_minutes": 480,
    "work_minutes": 420,
    "personal_minutes": 60,
    "by_project": {"website-redesign": 180, "api-integration": 120},
    "by_category": {"coding": 240, "meetings": 90, "communication": 60},
    "git_commit_count": 8,
    "agent_session_count": 12,
    "interactive_roots_started": 12,
    "total_agent_session_count": 31,
    "calendar_event_count": 3,
    "emails_sent_count": 2
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

The tracker records token usage and estimates cost using the rate table in `config.py`. Check that table against your provider's current pricing before relying on the estimate. Actual cost depends on captures, displays and optional session summaries.
