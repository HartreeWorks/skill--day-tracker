#!/usr/bin/env python3
"""
DayLogger CLI

Command-line interface for managing DayLogger.
"""

import argparse
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from config import load_config, save_config, CAPTURES_DIR, DAILY_DIR, DATA_DIR, REFERENCE_WALLPAPERS_DIR


def cmd_status(args):
    """Show current status."""
    config = load_config()

    print("DayLogger Status")
    print("=" * 40)
    print(f"Data directory: {DATA_DIR}")
    print(f"Capture interval: {config.capture_interval_minutes} minutes")
    print()

    # Check if daemon is running
    result = subprocess.run(
        ["launchctl", "list"],
        capture_output=True,
        text=True
    )
    is_running = "com.user.daylogger" in result.stdout
    print(f"Daemon: {'Running' if is_running else 'Stopped'}")

    # Check pause status
    if config.pause_until:
        try:
            pause_until = datetime.fromisoformat(config.pause_until)
            if datetime.now() < pause_until:
                remaining = pause_until - datetime.now()
                print(f"Paused for: {int(remaining.total_seconds() / 60)} more minutes")
            else:
                print("Pause expired, will resume on next capture")
        except Exception:
            pass
    else:
        print("Pause: Not paused")

    # Count captures today
    today = datetime.now().strftime("%Y-%m-%d")
    today_dir = CAPTURES_DIR / today
    if today_dir.exists():
        capture_count = sum(1 for d in today_dir.iterdir() if d.is_dir())
        print(f"Captures today: {capture_count}")
    else:
        print("Captures today: 0")

    # API key status
    import os
    has_env_key = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "daylogger-gemini", "-w"],
            capture_output=True
        )
        has_keychain_key = result.returncode == 0
    except Exception:
        has_keychain_key = False

    if has_env_key:
        print("Gemini API: Configured (environment)")
    elif has_keychain_key:
        print("Gemini API: Configured (keychain)")
    else:
        print("Gemini API: Not configured")


def cmd_pause(args):
    """Pause capturing."""
    config = load_config()

    if args.duration:
        # Parse duration (e.g., "30m", "1h", "2h30m")
        duration = args.duration.lower()
        minutes = 0

        if "h" in duration:
            parts = duration.split("h")
            minutes += int(parts[0]) * 60
            duration = parts[1] if len(parts) > 1 else ""

        if "m" in duration:
            minutes += int(duration.replace("m", ""))

        if minutes == 0:
            print("Invalid duration. Use format like '30m', '1h', or '1h30m'")
            return

        pause_until = datetime.now() + timedelta(minutes=minutes)
        config.pause_until = pause_until.isoformat()
        save_config(config)
        print(f"Paused until {pause_until.strftime('%H:%M')}")
    else:
        # Toggle pause off
        config.pause_until = None
        save_config(config)
        print("Resumed capturing")


def cmd_capture(args):
    """Run a manual capture."""
    from capture import run_capture

    print("Running capture...")
    metadata = run_capture(skip_analysis=args.skip_analysis)

    if metadata:
        print(f"Capture complete!")
        if metadata.analysis:
            print(f"  Category: {metadata.analysis.category}")
            print(f"  Summary: {metadata.analysis.oneline}")
    else:
        print("Capture skipped or failed")


def cmd_summary(args):
    """Generate or view summary."""
    from summarize import generate_summary

    if args.yesterday:
        date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        date_str = args.date

    summary = generate_summary(date_str, save=not args.no_save)
    print(summary)


def cmd_serve(args):
    """Start the web UI."""
    from server import main as server_main
    server_main()


def cmd_tag(args):
    """Tag recent captures."""
    from models import CaptureMetadata

    project = args.project
    minutes = args.last

    # Find captures from the last N minutes
    now = datetime.now()
    cutoff = now - timedelta(minutes=minutes)

    today = now.strftime("%Y-%m-%d")
    today_dir = CAPTURES_DIR / today

    if not today_dir.exists():
        print("No captures today")
        return

    tagged = 0
    for time_dir in sorted(today_dir.iterdir(), reverse=True):
        if not time_dir.is_dir():
            continue

        # Parse time from directory name (HH-MM-SS)
        try:
            time_str = time_dir.name
            capture_time = datetime.strptime(f"{today} {time_str}", "%Y-%m-%d %H-%M-%S")
        except ValueError:
            continue

        if capture_time < cutoff:
            break  # Past the time window

        metadata_file = time_dir / "metadata.json"
        if metadata_file.exists():
            try:
                metadata = CaptureMetadata.load(metadata_file)
                metadata.manual_project = project
                metadata.save(metadata_file)
                tagged += 1
                print(f"Tagged {time_dir.name} -> {project}")
            except Exception as e:
                print(f"Error tagging {time_dir.name}: {e}")

    print(f"Tagged {tagged} capture(s) as '{project}'")


def cmd_logs(args):
    """View capture logs."""
    from logging_config import read_logs

    logs = read_logs(limit=args.limit)

    if args.action:
        logs = [log for log in logs if log.get("action") == args.action]

    if not logs:
        print("No logs found.")
        return

    # Print summary if requested
    if args.summary:
        actions = {}
        for log in logs:
            a = log.get("action", "unknown")
            actions[a] = actions.get(a, 0) + 1
        print("Summary:")
        for action, count in sorted(actions.items(), key=lambda x: -x[1]):
            print(f"  {action}: {count}")
        print()

    # Print logs
    for log in logs:
        ts = log.get("timestamp", "")[:19].replace("T", " ")
        action = log.get("action", "info")
        msg = log.get("message", "")
        reason = log.get("reason", "")

        # Color coding for terminal
        color = ""
        reset = "\033[0m"
        if action.startswith("skipped"):
            color = "\033[33m"  # Yellow
        elif action == "error":
            color = "\033[31m"  # Red
        elif action == "completed":
            color = "\033[32m"  # Green
        elif action == "analyzed":
            color = "\033[35m"  # Magenta

        print(f"{ts} {color}[{action}]{reset} {msg}")
        if reason and args.verbose:
            print(f"           └─ {reason}")


def cmd_projects(args):
    """List all projects."""
    import json

    projects = set()

    for date_dir in CAPTURES_DIR.iterdir():
        if not date_dir.is_dir():
            continue
        for time_dir in date_dir.iterdir():
            metadata_file = time_dir / "metadata.json"
            if metadata_file.exists():
                try:
                    with open(metadata_file) as f:
                        data = json.load(f)
                    if data.get("auto_project"):
                        projects.add(data["auto_project"])
                    if data.get("manual_project"):
                        projects.add(data["manual_project"])
                except Exception:
                    pass

    if projects:
        print("Projects:")
        for p in sorted(projects):
            print(f"  - {p}")
    else:
        print("No projects found")


def cmd_install(args):
    """Run the installer."""
    script_dir = Path(__file__).parent
    install_script = script_dir / "install.sh"

    if install_script.exists():
        subprocess.run(["bash", str(install_script)])
    else:
        print(f"Install script not found at {install_script}")


def cmd_digest(args):
    """Show a quick recap of recent activity."""
    from models import DailyLog
    from config import DAILY_DIR

    today = datetime.now().strftime("%Y-%m-%d")
    daily_file = DAILY_DIR / f"{today}.json"

    if not daily_file.exists():
        print("No captures today yet.")
        return

    log = DailyLog.load(daily_file)
    if len(log.entries) < 2:
        print("Not enough data yet — fewer than 2 entries captured today.")
        return

    # Find the most recent entry's timestamp, then filter entries within N minutes before it
    minutes = args.minutes
    most_recent_ts = datetime.fromisoformat(log.entries[-1].timestamp)
    cutoff = most_recent_ts - timedelta(minutes=minutes)

    entries = [e for e in log.entries if datetime.fromisoformat(e.timestamp) >= cutoff]

    if len(entries) < 2:
        print(f"Not enough data in the last {minutes} minutes (only {len(entries)} entry).")
        return

    # Build compact text block
    lines = []
    for e in entries:
        ts = datetime.fromisoformat(e.timestamp).strftime("%H:%M")
        proj = e.inferred_project or e.project or ""
        proj_str = f" [{proj}]" if proj else ""
        lines.append(f"{ts} | {e.category} | {e.oneline}{proj_str}")

    entries_text = "\n".join(lines)
    span_start = datetime.fromisoformat(entries[0].timestamp).strftime("%H:%M")
    span_end = datetime.fromisoformat(entries[-1].timestamp).strftime("%H:%M")

    prompt = f"""You are a concise working-memory assistant. Given these {len(entries)} screen captures from {span_start}–{span_end}, write a quick recap of what the user has been doing.

Format: "Here's what you've been doing ({span_start}–{span_end}). You had N threads active: [thread summaries]. Most recently you were [X]."

Keep it to 2-4 sentences. Group related captures into "threads" (e.g. coding on project X, reviewing emails). Don't list every entry — synthesise.

Entries:
{entries_text}"""

    from analyze import get_gemini_client
    client = get_gemini_client()
    model = client.GenerativeModel(model_name="gemini-2.5-flash-lite")
    response = model.generate_content(prompt)
    print(response.text.strip())


def cmd_capture_wallpaper(args):
    """Capture current screens as reference wallpapers for blank desktop detection."""
    from capture import capture_screenshots, get_screen_count
    import shutil
    import tempfile

    print("Capturing reference wallpapers...")
    print("Make sure all screens show only the desktop wallpaper (no windows)!")
    print()

    # Create reference wallpapers directory
    REFERENCE_WALLPAPERS_DIR.mkdir(parents=True, exist_ok=True)

    # Capture to temp directory first
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        screenshots = capture_screenshots(tmpdir, quality=70)

        if not screenshots:
            print("Failed to capture screenshots!")
            return

        screen_count = get_screen_count()
        print(f"Captured {len(screenshots)} screen(s)")

        # Copy to reference directory with correct naming
        for screenshot in screenshots:
            screen_num = screenshot.split("-")[1].split(".")[0]  # "screen-2.jpg" -> "2"
            src = tmpdir / screenshot
            dst = REFERENCE_WALLPAPERS_DIR / f"screen-{screen_num}-wallpaper.jpg"

            shutil.copy2(src, dst)
            print(f"  Saved: {dst.name}")

    print()
    print(f"Reference wallpapers saved to: {REFERENCE_WALLPAPERS_DIR}")
    print("Blank desktop detection is now enabled.")


def main():
    parser = argparse.ArgumentParser(
        prog="daylog",
        description="DayLogger - Personal time tracking with AI"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # status
    sub_status = subparsers.add_parser("status", help="Show current status")
    sub_status.set_defaults(func=cmd_status)

    # pause
    sub_pause = subparsers.add_parser("pause", help="Pause/resume capturing")
    sub_pause.add_argument("duration", nargs="?", help="Duration (e.g., 30m, 1h)")
    sub_pause.set_defaults(func=cmd_pause)

    # capture
    sub_capture = subparsers.add_parser("capture", help="Run manual capture")
    sub_capture.add_argument("--skip-analysis", action="store_true", help="Skip AI analysis")
    sub_capture.set_defaults(func=cmd_capture)

    # summary
    sub_summary = subparsers.add_parser("summary", help="Generate daily summary")
    sub_summary.add_argument("date", nargs="?", help="Date (YYYY-MM-DD)")
    sub_summary.add_argument("--yesterday", "-y", action="store_true", help="Summarize yesterday")
    sub_summary.add_argument("--no-save", action="store_true", help="Don't save to file")
    sub_summary.set_defaults(func=cmd_summary)

    # serve
    sub_serve = subparsers.add_parser("serve", help="Start web UI")
    sub_serve.set_defaults(func=cmd_serve)

    # tag
    sub_tag = subparsers.add_parser("tag", help="Tag recent captures")
    sub_tag.add_argument("project", help="Project name")
    sub_tag.add_argument("--last", type=int, default=30, help="Minutes to look back (default: 30)")
    sub_tag.set_defaults(func=cmd_tag)

    # projects
    sub_projects = subparsers.add_parser("projects", help="List all projects")
    sub_projects.set_defaults(func=cmd_projects)

    # logs
    sub_logs = subparsers.add_parser("logs", help="View capture logs")
    sub_logs.add_argument("--limit", "-n", type=int, default=20, help="Number of logs to show")
    sub_logs.add_argument("--action", "-a", help="Filter by action (e.g., skipped_similar, completed)")
    sub_logs.add_argument("--summary", "-s", action="store_true", help="Show summary of actions")
    sub_logs.add_argument("--verbose", "-v", action="store_true", help="Show detailed reasons")
    sub_logs.set_defaults(func=cmd_logs)

    # digest
    sub_digest = subparsers.add_parser("digest", help="Quick recap of recent activity")
    sub_digest.add_argument("--minutes", type=int, default=30, help="Minutes to look back from most recent capture (default: 30)")
    sub_digest.set_defaults(func=cmd_digest)

    # install
    sub_install = subparsers.add_parser("install", help="Run installer")
    sub_install.set_defaults(func=cmd_install)

    # capture-wallpaper
    sub_wallpaper = subparsers.add_parser("capture-wallpaper", help="Capture reference wallpapers for blank screen detection")
    sub_wallpaper.set_defaults(func=cmd_capture_wallpaper)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
