# Fix plan: chief-of-staff digest misses Claude Code sessions

**Date:** 2026-03-09
**Symptom:** Today's chief-of-staff briefing reported "No Claude Code sessions" for 2026-03-08, but the user ran at least one session that day (e.g. `c704505c` in `/Users/ph/clawd`).

---

## Root cause

### Primary: `sessions-index.json` is absent or severely stale for most active projects

`generate_digest.py` discovers sessions exclusively via:

```python
def find_session_files() -> list[Path]:
    return list(PROJECTS_DIR.glob("*/sessions-index.json"))
```

It then loads sessions from those index files. **It never reads JSONL files directly.**

The problem: `sessions-index.json` files are maintained by Claude Code itself and have not been updated since approximately 2026-02-03. Evidence:

| Project dir | JSONL files | Indexed sessions | Most recent indexed |
|---|---|---|---|
| `-Users-ph-clawd` | 13 | **NONE** (no index file at all) | — |
| `-Users-ph--agents-skills` | 30 | **NONE** | — |
| `-Users-ph-Documents-Projects-infra` | 78 | **NONE** | — |
| `-Users-ph--claude-skills` | 613 | 227 | 2026-02-03 |
| `-Users-ph-Documents-Projects-plans-and-reviews` | 72 | 31 | 2026-02-03 |

Of 33 project directories scanned:
- ~14 have **no** `sessions-index.json` at all
- All 19 that do have one are frozen at entries no newer than 2026-02-03

The `/Users/ph/clawd` project—where Peter's current Claude Code sessions live—has no index. Its JSONL files from 2026-03-08 and other recent dates are completely invisible to the digest generator:

```
2026-03-08 | c704505c-f98f-4665-925f-3ca5d3414a64.jsonl  (135 lines, real session)
2026-03-07 | 0679ccbe-03ee-4eb8-a37e-bda0d9ab29b2.jsonl
2026-03-06 | 59131044-7ca4-4c6c-bff0-64c8543a6eb0.jsonl
...
```

### Secondary: filter uses `created` timestamp, not `modified`

Even for sessions that ARE indexed, `filter_sessions()` compares the session's `created` timestamp against `period.to`. A session started before the cutoff but actively used yesterday would be excluded. This is a lesser issue given the primary problem.

### Not a bug: timezone handling

The `parse_timestamp()` function correctly handles both `Z`-suffix UTC and `+HH:MM` offset formats. The digest's `period.to` is stored with `+01:00` offset. Comparison is timezone-aware. This is not a contributing factor.

### Not a bug: Codex scanning

The Codex JSONL scanner (`find_codex_session_files`) skips directories by date correctly. Peter's recent sessions are Claude Code, not Codex. Not relevant here.

---

## Data path trace (end-to-end)

```
Claude Code writes sessions to:
  ~/.claude/projects/<project-dir>/<session-id>.jsonl
  ~/.claude/projects/<project-dir>/sessions-index.json  ← maintained by Claude Code app

generate_digest.py:
  1. find_session_files()       → glob("*/sessions-index.json")
  2. load_sessions()            → reads "entries" from each index
  3. filter_sessions()          → keeps entries where created > period.to
  4. group_by_project()         → groups by projectPath
  5. generate_draft_digest()    → builds JSON with chat_count, by_project
  6. save_digest()              → writes to data/claude-code-summaries/YYYY-MM-DD.json

chief-of-staff SKILL.md:
  Step 1: runs generate_digest.py
  Step 2: reads data/claude-code-summaries/YYYY-MM-DD.json
  Step 3: renders "Yesterday" section from chat_count / by_project
```

Because step 1 finds no sessions (index absent/stale), `chat_count = 0`, and the briefing correctly (but wrongly) says "No Claude Code sessions."

---

## Fix plan

### Goal

Make `generate_digest.py` discover sessions directly from JSONL files when `sessions-index.json` is absent or stale, without breaking existing behaviour for indexed sessions.

### Approach: direct JSONL scan as fallback

Add a new function `scan_jsonl_sessions(since: Optional[str]) -> list[dict]` that:

1. Iterates all subdirectories in `PROJECTS_DIR`
2. For each subdir, checks whether a `sessions-index.json` exists **and** contains entries newer than `since`
3. If no valid index → scans `*.jsonl` files in that subdir directly
4. For each JSONL file:
   a. Uses OS `mtime` as a quick pre-filter: skip files not modified after `since`
   b. Reads the **first line** to extract `sessionId` and `timestamp` (type `queue-operation`)
   c. Reads forward to find the first `type=user` message for `firstPrompt`
   d. Constructs a session dict matching the format used by `load_sessions()`:
      ```python
      {
          "sessionId": ...,
          "projectPath": ...,   # derived from the subdir name (reverse the path encoding)
          "created": ...,        # from line 0 timestamp
          "modified": ...,       # from OS mtime (ISO format)
          "summary": "",
          "firstPrompt": ...,
          "_source_file": str(jsonl_path),
          "_source": "claude",
      }
      ```
5. Applies `filter_sessions()` as normal after collection

### Path decoding

Project directory names encode the path by replacing `/` with `-`. The decode logic already exists implicitly in `get_project_name()`. For `projectPath`, reconstruct by replacing `-` with `/` and prepending `/` — but this is ambiguous for paths with hyphens. Safer: read the actual `projectPath` from inside the JSONL by scanning for the first `type=user` message that contains a `cwd` field, or fall back to heuristic decoding. Check the JSONL structure; some files may contain a `system` message with the working directory.

**Alternative**: store `projectPath` as the raw subdir name and adjust `get_project_name()` / `categorise_project()` to also accept the encoded form. The encoded form is already what `_source_file` points to, so no decoding needed for display purposes.

### Stale index detection

For projects that DO have a `sessions-index.json`, determine if the index is stale:

```python
def index_is_stale(index_data: dict, since: Optional[str]) -> bool:
    """Returns True if the index has no entries newer than `since`."""
    if not since:
        return False
    entries = index_data.get("entries", [])
    if not entries:
        return True
    since_dt = parse_timestamp(since)
    for e in entries:
        modified = e.get("modified") or e.get("created", "")
        if modified:
            try:
                if parse_timestamp(modified) > since_dt:
                    return False
            except ValueError:
                pass
    return True
```

If stale, also scan JSONL files in that directory for the period.

### Changes to `generate_digest.py`

1. **Add** `scan_jsonl_sessions(since)` function (new, ~60 lines)
2. **Modify** `find_session_files()` / `load_sessions()` or add a new combined loader:
   - Load from index for non-stale projects (existing path, unchanged)
   - Load directly from JSONL for projects with absent or stale index (new path)
3. **Modify** `main()` and `get_status()` to use the combined loader
4. No changes to filtering, grouping, digest generation, or saving logic

### Files to change

| File | Change |
|---|---|
| `/Users/ph/.agents/skills/chief-of-staff/generate_digest.py` | Add `scan_jsonl_sessions()`, `index_is_stale()`, update callers |
| `/Users/ph/.agents/skills/chief-of-staff/config.py` | No change needed |
| `/Users/ph/.agents/skills/chief-of-staff/SKILL.md` | No change needed |

### Edge cases to handle

- JSONL files that are empty or malformed (skip gracefully, log to stderr)
- JSONL files with no `queue-operation` first line (use file mtime as `created` fallback)
- Very large JSONL files: only read first ~50 lines to extract metadata, then stop
- Sessions already captured via index + also found via JSONL scan: deduplicate by `sessionId`
- Projects where the subdir contains only subdirectories (not JSONL files at root): currently matches existing behaviour since `glob("*.jsonl")` returns nothing

### Testing plan

After implementing:

1. Run `python3 generate_digest.py --status` → should show >0 sessions since last digest
2. Run `python3 generate_digest.py --force --json | python3 -m json.tool | head -30` → `chat_count` should be >0, `by_project` should include `clawd`
3. Verify `c704505c` (2026-03-08) and other recent clawd sessions appear in output
4. Run `/cos` and check the "Yesterday" section shows correct session data
5. Confirm no duplicate sessions appear between index-sourced and JSONL-sourced entries

---

## What to do now (before implementing)

1. Verify whether Claude Code stopped writing `sessions-index.json` globally (check if newer Claude Code versions changed this behaviour) — this may affect whether a permanent fix or a one-time backfill is needed
2. Confirm the JSONL first-line structure holds across all project dirs (spot-check 3–5 non-clawd JSONL files)
3. Check if `projectPath` is accessible from within the JSONL content (would simplify path reconstruction)
