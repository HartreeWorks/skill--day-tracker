# Day Tracker

Keep a searchable record of what you worked on, with screenshots, AI captions, a daily timeline and approximate time by project.

This is the source and agent instructions for a personal macOS tool. Use it as a reference for your agent to adapt to your own computer and workflow. Installing the skill copies the instructions and code; it does not configure a working tracker automatically.

## How it works

1. A macOS launchd job captures each display every two minutes, skipping locked or sleeping screens and applying window filters.
2. Gemini analyses the screenshots alongside window titles, recent focus history, project names and available agent-session context.
3. Images and structured captions are saved locally. A Python web server displays the timeline at `http://localhost:8765`.
4. A daily rollup groups activity by project. Optional collectors add commits, agent sessions, calendar events, edited documents, sent emails and completed tasks.

An illustrative caption record, using fictional data:

```json
{
  "oneline": "Drafting the launch plan for Sample Project",
  "category": "writing",
  "inferred_project": "sample-project",
  "is_work": true
}
```

Time totals are estimates from two-minute samples. Completion records remain separate: an agent running in the background does not count as human working time.

`visible_apps` lists each app once when a window has exposed area on an active display. Minimised windows, other desktops, fully covered windows and menu-bar icons do not count. Wispr Flow is always ignored, including when checking whether other windows are covered. Visibility uses window bounds and opacity, so rounded corners and per-pixel transparency are approximate. If the window query fails, the list is empty.

## Requirements and scope

- macOS, Python and a Gemini API key. The Python code uses Pillow, PyYAML, google-genai, FastAPI, Uvicorn, Jinja2 and PyObjC.
- Screen Recording permission for the capture process; Accessibility and browser Automation permissions for the window helper.
- Local path configuration and launchd setup. This repository does not include a dependency manifest or a complete installer. Paths and account placeholders need adapting before use.
- Optional integrations use Git, Google Workspace command-line tools, Todoist and local Claude Code/Codex transcripts. Start with screenshot capture and captions if you do not need these.

In Python code, adapt scrubbed `~/...` paths with `Path(...).expanduser()` or configured absolute paths; Python and subprocess arguments do not expand `~` automatically.

The app-focus logger is included. Chrome tab-focus reporting comes from a separate browser extension and is not bundled here; an adapter can post `{"app":"Google Chrome","title":"Active tab title"}` to the logger's local `http://127.0.0.1:7847/log` endpoint. Scheduled job definitions and the personal project list are also external to this repository.

Run `capture.py` for a manual capture and use `capture-launchd.py` as the scheduled entry point. The machine-specific capture app wrapper is excluded from the public copy. The window-information helper is included as readable shell/AppleScript source.

Screenshots and context are sent to Google's Gemini API for analysis, while the files are stored locally. Optional session summaries also send transcript excerpts. Sensitive-window filters run before analysis, but AI sensitivity detection runs after upload. Chrome incognito filtering depends on Automation permission and proceeds with capture if detection fails; other browsers' private modes are not covered. Review these choices before enabling scheduled capture.

## Get the skill

```bash
npx skills add HartreeWorks/skill--day-tracker
```

Or give your agent this repository URL directly.

## Try it

Ask your agent:

```text
Read https://github.com/HartreeWorks/skill--day-tracker and help me make
something like this for my computer. Start with screenshots every two
minutes, AI captions, a local timeline and approximate time by project.
Adapt the paths and dependencies to my setup. Explain what will be sent
to the AI provider and let me choose exclusions before enabling capture.
Leave the optional calendar, email, task and agent-session integrations
until the basic loop works.
```

Your agent should inspect the code, identify the setup work for your machine, and help you verify a manual capture before scheduling it.

## Documentation

See [SKILL.md](./SKILL.md) for commands, configuration, data formats and operational details.

## About

Created by [Peter Hartree](https://x.com/peterhartree) of [AI Wow](https://wow.pjh.is).

Find more skills at [HartreeWorks/skills](https://github.com/HartreeWorks/skills).
