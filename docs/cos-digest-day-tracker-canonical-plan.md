# COS digest: day-tracker as canonical session source

**Date:** 2026-03-09
**Status:** Implemented

---

## Problem

The chief-of-staff briefing's "Yesterday" section reports zero or stale Claude Code sessions because `generate_digest.py` relies exclusively on `sessions-index.json`, which Claude Code has not been updating since ~2026-02-03. Many active projects (e.g. `/Users/ph/clawd`) have no index file at all.

---

## Data sources and precedence order

| Priority | Source | Location | Why |
|----------|--------|----------|-----|
| 1 (primary) | JSONL scan | `~/.claude/projects/*/*.jsonl` | Direct session files, always current, has session IDs + first prompts |
| 2 (supplementary) | Day-tracker daily JSONs | `~/Documents/day-tracker/data/daily/YYYY-MM-DD.json` | Canonical time-based truth; covers projects the JSONL scan could miss; confirmed "session was actively running" |
| 3 (legacy fallback) | sessions-index.json | `~/.claude/projects/*/sessions-index.json` | Kept for backward compatibility; only used when non-stale |

Day-tracker is "canonical" in the sense that it provides a time-confirmed record of what was actually active at known timestamps. JSONL scanning is primary because it provides richer data (session IDs, first prompts, precise created/modified times).

---

## Schema / field mapping

### JSONL scan → session dict

JSONL files come in two formats:
- **Newer format**: starts with `file-history-snapshot` lines, then `progress` line with `cwd` + `sessionId`
- **Older format**: starts with `queue-operation` line with `sessionId` + `timestamp`

Scan strategy: read first 50 lines, collect:

| Field | JSONL source |
|-------|-------------|
| `sessionId` | First line with `sessionId` field |
| `projectPath` | First line with `cwd` field |
| `created` | `queue-operation` timestamp, or first `progress`/`user` timestamp |
| `modified` | File mtime (ISO UTC) |
| `summary` | Empty string (not available without reading whole file) |
| `firstPrompt` | First `user` message with non-system text content |
| `_source` | `"claude-jsonl"` |

### Day-tracker entries → session dict

Day-tracker captures every ~5 minutes. A single Claude Code session may appear in many captures. Deduplication key: `(project_path, title)` per calendar day.

| Field | Day-tracker source |
|-------|-------------------|
| `sessionId` | `None` (not tracked by day-tracker) |
| `projectPath` | `active_sessions[i].project_path` |
| `created` | Timestamp of first capture where this `(project_path, title)` appears |
| `modified` | Timestamp of last capture where it appears |
| `summary` | `active_sessions[i].title` (Claude Code session title) |
| `firstPrompt` | Empty string |
| `_source` | `"day-tracker"` |

Only entries where `active_sessions[i].agent == "claude"` are included.

---

## Timezone / day-boundary strategy

- All digest period timestamps are stored in ISO format with timezone offset (e.g. `+01:00` for CET)
- JSONL timestamps use UTC (`Z` suffix) → handled by existing `parse_timestamp()`
- Day-tracker timestamps are **naive local time** (Europe/Paris, CET = UTC+1 in winter, CEST = UTC+2 in summer)
- Day-tracker naive timestamps are treated as CET (UTC+1) year-round in the implementation — a simplification that is accurate in winter and off by 1 hour in summer (CEST). Acceptable given the granularity of 5-minute captures
- For robustness, if `zoneinfo` is available, use `ZoneInfo("Europe/Paris")`; else fall back to +01:00 fixed offset
- Day filter: use `file.stem` date (e.g. `2026-03-08`) for quick pre-filtering before parsing individual entry timestamps

---

## Fallback behavior

| Scenario | Behaviour |
|----------|-----------|
| Day-tracker data exists, JSONL exists | JSONL used as primary; day-tracker fills only projects not seen in JSONL |
| Day-tracker data missing for period | JSONL scan only (no degradation visible to user) |
| JSONL scan finds nothing for a project | Day-tracker sessions shown with title as summary |
| Both missing | sessions-index.json used (existing legacy path) |
| sessions-index.json stale | Skipped (stale detection added) |
| sessions-index.json non-stale | Used as before |

---

## Implementation: files changed

| File | Change |
|------|--------|
| `/Users/ph/.agents/skills/chief-of-staff/generate_digest.py` | Added `scan_jsonl_sessions()`, `extract_jsonl_metadata()`, `load_sessions_from_day_tracker()`, `index_is_stale()`, `merge_sessions()`, updated `main()` and `get_status()` |
| `/Users/ph/.agents/skills/chief-of-staff/config.py` | Added `DAY_TRACKER_DAILY_DIR` constant |

---

## New functions summary

### `index_is_stale(index_data, since) → bool`
Returns `True` if all entries in the index predate `since`. Projects with stale indexes fall through to JSONL scan.

### `extract_jsonl_metadata(jsonl_path) → Optional[dict]`
Reads up to 50 lines of a JSONL file to extract `sessionId`, `projectPath` (from `cwd`), `created`, `modified` (file mtime), and `firstPrompt`. Returns `None` on failure or if no useful data found.

### `scan_jsonl_sessions(since) → list[dict]`
Iterates all subdirs of `~/.claude/projects/`. For each:
- Skips if non-stale `sessions-index.json` exists
- For each `*.jsonl` file newer than `since` (mtime filter): calls `extract_jsonl_metadata()`
- Returns list of session dicts with `_source: "claude-jsonl"`

### `load_sessions_from_day_tracker(since) → list[dict]`
Reads daily JSON files from `~/Documents/day-tracker/data/daily/` dated on or after `since`. Collects all `active_sessions` entries where `agent == "claude"`. Deduplicates by `(project_path, title)` across the day. Returns session dicts with `_source: "day-tracker"`.

### `merge_sessions(index_sessions, jsonl_sessions, dt_sessions, since) → list[dict]`
Combines sessions from all three sources:
1. Filters `index_sessions` to non-stale only
2. Adds all `jsonl_sessions` (by sessionId dedup)
3. From `dt_sessions`: adds only those whose `projectPath` is NOT already covered by jsonl or index sessions
Returns merged, deduplicated list.

---

## Test / validation strategy

After implementation, validate with:

```bash
cd /Users/ph/.agents/skills/chief-of-staff

# 1. Status check — should show sessions
python3 generate_digest.py --status

# 2. JSON preview — check chat_count > 0 and clawd/other projects present
python3 generate_digest.py --force --json | python3 -m json.tool | head -60

# 3. Verify specific session appears (clawd 2026-03-08)
python3 generate_digest.py --force --json | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('chat_count:', d['chat_count'])
print('projects:', list(d['by_project'].keys()))
"

# 4. Check sources breakdown
python3 generate_digest.py --status --json
```

Expected results:
- `chat_count` > 0
- `clawd` or equivalent project appears in `by_project`
- Day-tracker and JSONL sources both represented
- No duplicate session counts (same session counted once)

---

## Caveats

1. **Day-tracker title ≠ session summary**: `active_sessions[i].title` is the Claude Code UI title, not a substantive summary. Shown as `summary` field; the COS Claude agent will still need to synthesise from available data.

2. **JSONL firstPrompt extraction**: For newer JSONL files starting with `file-history-snapshot`, the user prompt may be in a system-injected message. The extractor skips messages starting with `#` or `<`. Edge cases may slip through.

3. **Naive timestamp offset**: Day-tracker timestamps get a fixed +01:00 offset. During CEST (late March–late October), sessions from midnight–1am local may be miscategorised (counted to previous day by 1 hour). Acceptable for now.

4. **Project dedup across day-tracker and JSONL**: Projects are matched by exact `projectPath` string. If day-tracker captured `/Users/ph/clawd` and JSONL shows `/Users/ph/clawd`, they match correctly. If paths differ (symlinks, trailing slashes), both may be included — acceptable edge case.

5. **Large number of JSONL files**: `/Users/ph/.claude/projects/` has many files. mtime pre-filter avoids reading old files, but directory iteration still takes O(n) time. Acceptable for daily COS runs.
