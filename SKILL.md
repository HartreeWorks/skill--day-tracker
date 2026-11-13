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

The capture runs every 2 minutes as the `day-tracker-capture` job in the Hammerspoon scheduler (`~/.agents/scheduler/README.md`):

```bash
scheduler status                          # last run, exit code, next run
scheduler log day-tracker-capture         # scheduler's view of each run
cat ~/Documents/day-tracker/data/logs/launchd.log   # the capture's own log
scheduler run day-tracker-capture         # force an immediate run
```

To pause it, set `"enabled": false` on the job in `~/.agents/scheduler/jobs/mbp.json`.

Every healthy cycle touches `~/Library/Application Support/day-tracker/heartbeat`: one that writes an entry, or one skipped because capture is paused or the screen is locked, off or blank. The job's `heartbeat` setting makes the scheduler raise a critical alert after 30 minutes of active use (awake, display on, unlocked) without a touch. Lines in `launchd.log` start with a timestamp.

Wrapper: `~/.claude/skills/day-tracker/capture-launchd.py` (calls `run_capture()` directly and adds a 100-second watchdog; the scheduler's own timeout is 110 seconds).

## macOS Permissions

Grant permissions via System Settings > Privacy & Security, then verify a manual capture and a scheduled capture:

| App | Permission | Purpose |
|-----|------------|---------|
| Hammerspoon (the scheduler that starts the capture) | Screen Recording | Capture screenshots |
| `DayTrackerHelper.app` | Accessibility | Get active window info |
| `DayTrackerHelper.app` | Automation (control browsers) | Detect incognito/private windows (see below) |

For incognito detection, macOS will prompt once with "DayTrackerHelper wants to control Google Chrome". Grant it under System Settings > Privacy & Security > Automation. The grant attaches to the helper bundle and persists across scheduled runs. **Detection is fail-open:** if Chrome can't be queried (permission not granted, browser busy), incognito windows are NOT caught and the capture proceeds — so the grant must be in place for protection to work. Watch for `incognito_detection_unavailable` / `incognito_detection_partial` events in `launchd.log` if you suspect the grant has lapsed.

## Incognito / private window detection

Screenshots of any display showing an **incognito/private browser window** are discarded before analysis — never sent to Gemini, never stored (deleted outright, not even kept as a thumbnail, since a thumbnail would still leak content). Each affected display is discarded. If *every* display shows incognito, the cycle is recorded as a content-free `private` entry (see Private entries) so its time still counts.

Detection is **visibility-aware**, not just geometric: a display is only dropped where the incognito window has actually visible (unoccluded) on-screen area. An incognito window that is minimised, hidden, on another Space, or fully buried behind other windows does **not** trigger a discard — only what would really appear in the screenshot. Visibility is computed from the on-screen window list in front-to-back z-order (`CGWindowListCopyWindowInfo`), subtracting any windows stacked in front. A window must expose more than ~64×64 px (`INCOGNITO_VISIBLE_AREA_THRESHOLD` in `capture.py`) to count, so shadow-edge slivers don't cause false discards.

- **Covered:** Google Chrome only — incognito windows found via AppleScript `mode`/`bounds`, then matched to the Quartz on-screen window list for the visibility/occlusion test. (Brave, Edge and other Chromium browsers share the same dictionary and can be re-added in `SUPPORTED_BROWSERS` in the helper if ever needed.)
- **NOT covered (blind spots):** Safari Private, Arc, and Firefox private windows. Their AppleScript dictionaries don't expose private-browsing state, so these are **not** detected and will still be captured/described. If this matters, close them or pause the tracker.
- **Toggle:** `incognito_detection` in `config.json` (default `true`).
- **Log events:** `skipped_incognito` (one display dropped), `skipped_all_incognito` (whole capture skipped), `incognito_detection_unavailable` / `incognito_detection_partial` (fail-open — a browser couldn't be queried).

## Sensitive app windows

Screenshots of any display showing a window owned by an app in `sensitive_window_owners` (default `1Password`, `Keychain Access`) are deleted immediately after capture, before any other check reads them and before Gemini sees them. This catches the case the frontmost-window pre-filter misses: the app visible on a display but not focused. Matching is a case-insensitive substring of the window owner, so 1Password's Quick Access panel counts; menu-bar icons do not.

Detection uses the same visibility test as incognito detection, but any exposed area counts, since a revealed password or one-time code needs little room. If every display is affected, the cycle becomes a `private` entry. Detection is **fail-closed**: if the window list can't be read, all screenshots are deleted and the cycle becomes a `private` entry.

## Private entries

A cycle whose content must not be kept still counts as tracked time. These cycles become a `private` entry: the frontmost window matches `sensitive_window_patterns`, every display shows incognito or a sensitive app, sensitive-app detection fails, or analysis flags explicit content or (with `auto_delete_sensitive`) sensitive content. Its screenshots are deleted. The entry keeps only the timestamp and a fixed `oneline` naming the reason, with `category: "private"` and `is_work: null`. It has no window title, app list or description.

When any display is discarded for an incognito or sensitive window, the capture drops the frontmost window's title and every focus-history title before Gemini, the metadata or the daily log see them. Focus-history titles of sensitive apps and of windows matching `sensitive_window_patterns` are always dropped. Deletions decided from the analysis happen before anything from it is logged, and their log lines use fixed text. The focus logger's own files (`data/focus-log/`) still record every window title.

- **Not covered:** the 1Password browser extension's popup, which belongs to the browser's process.
- **Toggle:** set `sensitive_window_owners` to `[]` in `config.json` to disable.
- **Log events:** `skipped_sensitive_screen` (one display dropped), `skipped_all_sensitive` (whole capture skipped), `skipped_sensitive_detection_failed` (fail-closed).

## CLI Commands

```bash
# Check status
python3 ~/.claude/skills/day-tracker/cli.py status

# Pause screenshot capture and focus logging for 1 hour
python3 ~/.claude/skills/day-tracker/cli.py pause 1h

# Resume both
python3 ~/.claude/skills/day-tracker/cli.py pause

# Tag last 30 minutes to a project (overrides automatic attribution from the next rollup)
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

# Recollect only calendar events into an existing sidecar, then re-attribute
~/venvs/ph/bin/python3 ~/.claude/skills/day-tracker/scripts/daily-rollup.py [YYYY-MM-DD] --skip-completions --refresh-calendar

# Weekly digest with date range
~/venvs/ph/bin/python3 ~/.claude/skills/day-tracker/scripts/weekly-digest.py --start 2026-01-26 --end 2026-02-01

# Weekly digest as JSON
~/venvs/ph/bin/python3 ~/.claude/skills/day-tracker/scripts/weekly-digest.py --week 2026-W05 --json
```

### Daily rollup

The daily rollup (`scripts/daily-rollup.py`) post-processes raw daily JSON:

* **Failed-analysis retry** — re-runs Gemini on the day's captures whose analysis failed (offline, timeouts): `unanalysed` entries, and older failed captures that kept their screenshots but never got an entry. Retries stop after 300 captures, 15 minutes or 5 failures in a row; a capture that fails again stays as an `unanalysed` entry. `--skip-reanalysis` turns this off. The same pass copies manual tags from capture metadata to entries and deletes unfinished capture folders (no `metadata.json`, older than 10 minutes), whose screenshots never passed the privacy checks
* **Calendar attribution** — a capture taken during a qualifying calendar event takes its project from the event (see Calendar attribution)
* **Manual tags** — an entry's `manual_project` (set by `cli.py tag` or the web UI) becomes its `canonical_project`, ahead of every automatic rule
* **Project consolidation** — maps AI inference, configured title patterns, and explicit aliases to canonical names from `projects.yaml`
* **Gap filling** — attributes unclassified entries between same-project entries (within 15-min window)
* **Summary generation** — populates the `summary` field with time breakdowns by project and category
* **Coverage** — compares tracked minutes with display-on minutes from `pmset -g log` (`display_on_minutes`, `coverage`, `untracked_display_on_minutes`). A full run raises a warning alert when a weekday with at least 2 hours of display-on time has coverage below 80%. pmset keeps about a week of history; older days get `null`. pmset prints times in the current timezone, so on a day spent in another timezone the comparison is shifted by the difference
* **Completion signals** — collects "what happened today" data from external sources into a sidecar `.completions.json` file

Runs at 23:55 daily as the `day-tracker-rollup` job in the Hammerspoon scheduler (`~/.agents/scheduler/README.md`). If the Mac is asleep at 23:55, it runs on wake for the missed day; only the most recent missed day is caught up. Idempotent — safe to re-run.

Log: `scheduler log day-tracker-rollup`

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

### Calendar attribution

Screenshots can't tell whose call Peter is on, so the rollup uses the calendar. An event qualifies when it is timed (not all-day), Peter hasn't declined it (an unanswered invite counts), and someone other than Peter is invited. Meeting rooms and other resources don't count as people.

- **Acme:** any attendee, Peter's own `you@acme.example` included, has an @acme.example address, or the event is on the Acme calendar. Every capture inside it gets `canonical_project: "acme-project"` and `is_work: true`.
- **Not Acme:** any other qualifying event. Captures inside it get `canonical_project: "non-acme-meeting"`, unless the screenshot already gave a project other than Acme. The attendees are never used to guess a project.
- **Left alone:** captures with a manual tag, and captures where qualifying events that disagree overlap.

Changed entries carry `calendar_attributed: true`. The "Busy" copies of personal events on the Acme calendar are ignored. The calendar collector records for each event `calendars`, `all_day`, `self_response`, `other_attendee_count` and `has_acme_attendee`, but no attendee addresses. Sidecars collected before these fields existed don't affect attribution; `--refresh-calendar` recollects them.

Event times are compared in the timezone the rollup runs in. To redo a day spent in another timezone, run the rollup with `TZ` set to that timezone, for example `TZ=Europe/London`.

The briefing's Acme hours step (`~/.agents/skills/briefing/scripts/acme-hours.py`) reads `timestamp`, `canonical_project`, `is_work`, `category` and `oneline` from daily entries, and the completions sidecar, to build its evidence digests. Tell Peter before changing their names or meaning.

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

**Set by the tracker, not the model** (`is_work: null`, counted in `unknown_minutes`):
- `private` - Content not kept (see Private entries)
- `unanalysed` - Analysis failed; the rollup retries it

## Project inference

The AI automatically infers which project you're working on by matching visible content (repo names, URLs, Slack channels, document titles) against your projects list in `~/Projects/projects.yaml`.

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
| `sensitive_window_owners` | `["1Password", "Keychain Access"]` | Discard per-display screenshots showing any visible window of these apps (see Sensitive app windows) |

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
    "work_minutes": 400,
    "personal_minutes": 60,
    "unknown_minutes": 20,
    "display_on_minutes": 510,
    "coverage": 0.94,
    "untracked_display_on_minutes": 30,
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

The capture, the rollup and tagging change daily logs only while holding a lock (`~/Library/Application Support/day-tracker/daily-log.lock`) and write them atomically. The rollup does its slow work (Gemini retries, completion collection) on a snapshot, then merges in entries the capture wrote meanwhile before saving. A capture lands in the daily log for the day it started, even if it finishes after midnight.

Each entry also includes:
- `is_work` - `true` for work, `false` for personal, `null` for `private` and `unanalysed` entries
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
