#!/usr/bin/env python3
"""
Day Tracker Capture Script

Captures screenshots and active window information, analyzes with Gemini,
and saves to the daily log.

Run via launchd every 2 minutes, or manually for testing.
"""

import subprocess
import json
import re
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple

from PIL import Image
from config import load_config, CAPTURES_DIR, DAILY_DIR, REFERENCE_WALLPAPERS_DIR, CaptureConfig
from models import CaptureMetadata, ActiveWindow, Analysis, DailyLog
from logging_config import log_capture_event, rotate_logs

FOCUS_LOG_DIR = Path.home() / "Documents" / "day-tracker" / "data" / "focus-log"


def is_screen_locked() -> bool:
    """Check if the screen is locked."""
    try:
        import Quartz
        session = Quartz.CGSessionCopyCurrentDictionary()
        if session:
            return bool(session.get("CGSSessionScreenIsLocked", False))
    except Exception:
        pass
    return False


def is_display_off() -> bool:
    """
    Check if the display is off (asleep) but the computer is still running.

    Uses Quartz CGDisplayIsAsleep to detect display power state.
    Returns True if ALL displays are asleep (off).
    """
    try:
        import Quartz

        # Get all active displays
        err, display_ids, count = Quartz.CGGetActiveDisplayList(10, None, None)

        if err != 0 or not display_ids:
            return False  # Can't determine, assume on

        # Check if ALL displays are asleep
        for display_id in display_ids:
            if not Quartz.CGDisplayIsAsleep(display_id):
                return False  # At least one display is on

        return True  # All displays are asleep
    except Exception as e:
        print(f"Could not check display state: {e}")
        return False  # Assume display is on if we can't check


def get_previous_capture_dir() -> Optional[Path]:
    """Find the most recent capture directory."""
    today = datetime.now().strftime("%Y-%m-%d")
    today_dir = CAPTURES_DIR / today

    if today_dir.exists():
        # Get all time directories sorted descending
        time_dirs = sorted(
            [d for d in today_dir.iterdir() if d.is_dir()],
            key=lambda d: d.name,
            reverse=True
        )
        if time_dirs:
            return time_dirs[0]

    # Check yesterday if no captures today
    from datetime import timedelta
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_dir = CAPTURES_DIR / yesterday

    if yesterday_dir.exists():
        time_dirs = sorted(
            [d for d in yesterday_dir.iterdir() if d.is_dir()],
            key=lambda d: d.name,
            reverse=True
        )
        if time_dirs:
            return time_dirs[0]

    return None


def calculate_image_difference(img1_path: Path, img2_path: Path, sample_size: int = 100) -> float:
    """
    Calculate the percentage of pixels that differ between two images.

    Uses downsampling for speed - resizes to sample_size x sample_size before comparing.
    Returns a float from 0.0 (identical) to 1.0 (completely different).
    """
    try:
        from PIL import Image
        import numpy as np

        # Load and resize images to small size for fast comparison
        img1 = Image.open(img1_path).convert('RGB').resize((sample_size, sample_size), Image.LANCZOS)
        img2 = Image.open(img2_path).convert('RGB').resize((sample_size, sample_size), Image.LANCZOS)

        # Convert to numpy arrays
        arr1 = np.array(img1)
        arr2 = np.array(img2)

        # Calculate per-pixel difference (across all RGB channels)
        # A pixel is "different" if any channel differs by more than threshold
        threshold = 30  # Allow small color variations (out of 255)
        diff = np.abs(arr1.astype(int) - arr2.astype(int))
        changed_pixels = np.any(diff > threshold, axis=2)

        # Return percentage of changed pixels
        return float(np.mean(changed_pixels))

    except Exception as e:
        print(f"Image comparison failed: {e}")
        return 1.0  # Assume different if comparison fails


def is_screen_similar_to_previous(capture_dir: Path, screenshots: List[str], threshold: float) -> bool:
    """
    Check if current screenshots are similar to the previous capture.

    Returns True if ALL screens are below the difference threshold.
    """
    if threshold <= 0:
        return False  # Feature disabled

    prev_dir = get_previous_capture_dir()
    if not prev_dir:
        return False  # No previous capture to compare

    for screenshot in screenshots:
        current_path = capture_dir / screenshot
        prev_path = prev_dir / screenshot

        if not prev_path.exists():
            return False  # Different screen configuration

        diff = calculate_image_difference(current_path, prev_path)
        if diff >= threshold:
            return False  # This screen changed enough

    return True  # All screens are similar


def is_black_screen(screenshot_path: Path, threshold: float = 5.0) -> bool:
    """
    Check if screenshot is essentially black (display was off).

    Args:
        screenshot_path: Path to captured screenshot
        threshold: Maximum average pixel value to consider "black" (0-255, default 5)

    Returns:
        True if screen appears to be black/off
    """
    try:
        from PIL import Image
        import numpy as np

        # Load and resize for fast analysis
        img = Image.open(screenshot_path).convert('RGB')
        img_small = img.resize((100, 100), Image.LANCZOS)

        # Convert to numpy and calculate average pixel value
        arr = np.array(img_small)
        avg_value = np.mean(arr)

        # If average pixel value is very low, screen is black
        return avg_value < threshold

    except Exception as e:
        print(f"Black screen check failed: {e}")
        return False


def is_blank_desktop(screenshot_path: Path, screen_number: int, threshold: float = 0.05, crop_top: int = 25) -> bool:
    """
    Check if screenshot is just the desktop wallpaper.

    Compares the screenshot against a reference wallpaper image for this screen.
    Crops the top portion (menu bar) before comparison since it contains the clock/status.

    Args:
        screenshot_path: Path to captured screenshot
        screen_number: Which screen (1-4) to compare against
        threshold: Max difference to consider "blank" (default 5%)
        crop_top: Pixels to crop from top before comparison (removes menu bar)

    Returns:
        True if screen appears to be blank desktop wallpaper
    """
    if threshold <= 0:
        return False  # Feature disabled

    # Find reference wallpaper thumbnails — supports multiple per screen (e.g. portrait + landscape)
    # Thumbnails are pre-cropped and pre-resized to 100x100 for fast comparison
    reference_paths = sorted(REFERENCE_WALLPAPERS_DIR.glob(f"screen-{screen_number}-wallpaper*-thumb.webp"))
    if not reference_paths:
        # Fall back to full-size references (legacy or non-thumb)
        reference_paths = sorted(REFERENCE_WALLPAPERS_DIR.glob(f"screen-{screen_number}-wallpaper*.webp"))
        # Exclude thumb files if we somehow matched them
        reference_paths = [p for p in reference_paths if '-thumb' not in p.name]
    if not reference_paths:
        return False  # No reference to compare against

    try:
        from PIL import Image
        import numpy as np

        # Load screenshot and crop edges (menu bar top, shadow edges)
        crop_edge = 30  # pixels to crop from left/right/bottom edges (shadow)
        screenshot = Image.open(screenshot_path).convert('RGB')
        screenshot = screenshot.crop((
            crop_edge, crop_top,
            screenshot.width - crop_edge, screenshot.height - crop_edge
        ))

        sample_size = 100
        screenshot = screenshot.resize((sample_size, sample_size), Image.LANCZOS)

        # Try each reference — match if ANY reference is close enough
        # References are stored pre-cropped and pre-resized to 100x100
        for reference_path in reference_paths:
            reference = Image.open(reference_path).convert('RGB')
            if reference.size != (sample_size, sample_size):
                reference = reference.crop((
                    crop_edge, crop_top,
                    reference.width - crop_edge, reference.height - crop_edge
                ))
                reference = reference.resize((sample_size, sample_size), Image.LANCZOS)

            # Convert to numpy arrays
            arr1 = np.array(screenshot)
            arr2 = np.array(reference)

            # Calculate difference (same logic as calculate_image_difference)
            color_threshold = 30  # Allow small color variations
            diff = np.abs(arr1.astype(int) - arr2.astype(int))
            changed_pixels = np.any(diff > color_threshold, axis=2)
            difference = float(np.mean(changed_pixels))

            # If difference is less than threshold, it's a blank desktop
            if difference < threshold:
                return True

        return False

    except Exception as e:
        print(f"Blank desktop check failed: {e}")
        return False  # Assume not blank if comparison fails


def get_screen_count() -> int:
    """Get the number of connected displays."""
    result = subprocess.run(
        ["system_profiler", "SPDisplaysDataType", "-json"],
        capture_output=True,
        text=True
    )
    try:
        data = json.loads(result.stdout)
        displays = data.get("SPDisplaysDataType", [{}])[0].get("spdisplays_ndrvs", [])
        return max(1, len(displays))
    except Exception:
        return 1


def capture_screenshots(output_dir: Path, quality: int = 60) -> List[str]:
    """
    Capture all screens and save as WebP.

    Returns list of filenames.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    filenames = []
    screen_count = get_screen_count()

    for i in range(1, screen_count + 1):
        filename = f"screen-{i}.webp"
        # Capture as PNG first (lossless source for best quality downscale)
        tmp_filepath = output_dir / f"screen-{i}.png"
        filepath = output_dir / filename

        # -x: silent (no sound)
        # -t png: lossless capture
        # -D: display number (1-indexed)
        result = subprocess.run(
            ["screencapture", "-x", "-t", "png", "-D", str(i), str(tmp_filepath)],
            capture_output=True
        )

        if tmp_filepath.exists():
            filenames.append(filename)

            # Scale to 50% and compress to WebP
            img = Image.open(tmp_filepath)
            new_size = (int(img.width * 0.50), int(img.height * 0.50))
            img = img.resize(new_size, Image.LANCZOS)
            img.save(filepath, "WebP", quality=quality)
            img.close()
            tmp_filepath.unlink()

    return filenames


# Path to helper app that has Accessibility permissions
HELPER_APP = Path(__file__).parent / "DayTrackerHelper.app" / "Contents" / "MacOS" / "get-window-info"


def get_frontmost_window_title() -> str:
    """Get the title of the frontmost window using Quartz (works under launchd)."""
    try:
        import Quartz
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID
        )
        if windows:
            # First on-screen window at layer 0 is typically the frontmost app window
            for win in windows:
                if win.get("kCGWindowLayer", 999) == 0 and win.get("kCGWindowName"):
                    return win["kCGWindowName"]
    except Exception:
        pass
    return ""


def get_window_info() -> Tuple[Optional[ActiveWindow], List[str]]:
    """
    Get active window and visible apps using the helper app.
    The helper app has its own Accessibility permissions, keeping Python unprivileged.
    Window title is supplemented via Quartz (works under launchd where AppleScript can't).
    """
    try:
        result = subprocess.run(
            [str(HELPER_APP)],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip())

            if "error" in data:
                print(f"Helper error: {data['error']}")
                return None, []

            # Parse active window
            active_window = None
            if data.get("app"):
                title = data.get("title", "")
                # If AppleScript couldn't get the title (common under launchd),
                # fall back to Quartz CGWindowList
                if not title:
                    title = get_frontmost_window_title()
                active_window = ActiveWindow(
                    app=data.get("app", ""),
                    title=title
                )

            # Parse visible apps
            visible_apps = []
            if data.get("visible_apps"):
                visible_apps = [a.strip() for a in data["visible_apps"].split("|||") if a.strip()]

            return active_window, visible_apps

    except subprocess.TimeoutExpired:
        print("Helper timed out")
    except json.JSONDecodeError as e:
        print(f"Helper returned invalid JSON: {e}")
    except Exception as e:
        print(f"Helper failed: {e}")

    return None, []


def get_active_window() -> Optional[ActiveWindow]:
    """Get information about the currently active window."""
    active_window, _ = get_window_info()
    return active_window


def get_visible_apps() -> List[str]:
    """Get list of all visible (non-hidden) applications."""
    _, visible_apps = get_window_info()
    return visible_apps


def get_focus_history(minutes: int = 2) -> Optional[List[dict]]:
    """Read recent focus log entries and compute time-per-window percentages.

    Returns a list sorted by percentage descending:
        [{"app": "Google Chrome", "title": "AI Wow draft", "pct": 72}, ...]
    Returns None if no focus log data is available.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = FOCUS_LOG_DIR / f"{today}.jsonl"
    if not log_path.exists():
        return None

    now = datetime.now()
    cutoff = now - __import__("datetime").timedelta(minutes=minutes)

    # Read entries within the time window
    entries = []
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    t = datetime.strptime(entry["t"], "%Y-%m-%dT%H:%M:%S")
                    if t >= cutoff:
                        entries.append((t, entry["app"], entry.get("title", "")))
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
    except Exception:
        return None

    if not entries:
        return None

    # Compute duration for each entry (time until next entry; last runs until now)
    durations = {}  # (app, title) -> total seconds
    for i, (t, app, title) in enumerate(entries):
        if i + 1 < len(entries):
            duration = (entries[i + 1][0] - t).total_seconds()
        else:
            duration = (now - t).total_seconds()
        key = (app, title)
        durations[key] = durations.get(key, 0) + duration

    total = sum(durations.values())
    if total <= 0:
        return None

    result = []
    for (app, title), secs in durations.items():
        pct = round(secs / total * 100)
        if pct > 0:
            result.append({"app": app, "title": title, "pct": pct})

    result.sort(key=lambda x: x["pct"], reverse=True)
    return result if result else None


def check_sensitive_window(active_window: Optional[ActiveWindow], config: CaptureConfig) -> bool:
    """Check if current window matches sensitive patterns (pre-filter)."""
    if not active_window:
        return False

    text_to_check = f"{active_window.app} {active_window.title}".lower()

    for pattern in config.sensitive_window_patterns:
        if pattern.lower() in text_to_check:
            return True

    return False


def check_skip_window(active_window: Optional[ActiveWindow], config: CaptureConfig) -> bool:
    """Check if current app matches skip_window_patterns (video/entertainment apps)."""
    if not active_window or not config.skip_window_patterns:
        return False
    app_lower = active_window.app.lower()
    for pattern in config.skip_window_patterns:
        if pattern.lower() == app_lower:
            return True
    return False


def apply_app_rules(active_window: Optional[ActiveWindow], analysis: 'Analysis', config: CaptureConfig):
    """Apply deterministic app rules to override AI inference. Mutates analysis in place."""
    if not active_window or not config.app_rules:
        return
    app_lower = active_window.app.lower()
    title_lower = active_window.title.lower()
    for rule in config.app_rules:
        if rule.get("app", "").lower() != app_lower:
            continue
        title_contains = rule.get("title_contains", "")
        if title_contains and title_contains.lower() not in title_lower:
            continue
        # First match wins — apply overrides
        if "category" in rule:
            analysis.category = rule["category"]
        if "project" in rule:
            analysis.inferred_project = rule["project"]
            analysis.project_confidence = 1.0
        if "is_work" in rule:
            analysis.is_work = rule["is_work"]
        return


def get_active_agent_sessions(recency_minutes: int = 5) -> List[dict]:
    """Find recently active agent sessions (Claude Code and Codex).

    Returns list of dicts sorted by recency, each with keys:
        agent: "claude" or "codex"
        title: session title (from title cache) or first user message (truncated)
        project_path: filesystem path of the project (or None)
    """
    import time
    from pathlib import Path

    now = time.time()
    cutoff = now - (recency_minutes * 60)

    # Title cache
    title_cache = {}
    title_cache_path = Path.home() / ".claude" / "conversation-titles.json"
    try:
        with open(title_cache_path) as f:
            title_cache = json.load(f)
    except Exception:
        pass

    # Known projects for Claude directory decoding
    from config import load_known_projects, PROJECTS_YAML
    known_projects = []
    if PROJECTS_YAML.exists():
        try:
            import yaml
            with open(PROJECTS_YAML) as f:
                data = yaml.safe_load(f)
            known_projects = data.get("projects", [])
        except Exception:
            pass

    # Collect all recent session files with mtime
    candidates = []  # (mtime, agent, session_id, project_path, jsonl_path)

    # Claude sessions: ~/.claude/projects/<encoded-path>/<UUID>.jsonl
    claude_projects_dir = Path.home() / ".claude" / "projects"
    if claude_projects_dir.exists():
        for jsonl_file in claude_projects_dir.glob("*/*.jsonl"):
            try:
                mtime = jsonl_file.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            session_id = jsonl_file.stem
            dir_name = jsonl_file.parent.name
            project_path = _decode_claude_project_dir(dir_name, known_projects)
            candidates.append((mtime, "claude", session_id, project_path, jsonl_file))

    # Codex sessions: ~/.codex/sessions/YYYY/MM/DD/rollout-<datetime>-<UUID>.jsonl
    codex_sessions_dir = Path.home() / ".codex" / "sessions"
    if codex_sessions_dir.exists():
        for jsonl_file in codex_sessions_dir.glob("*/*/*/**.jsonl"):
            try:
                mtime = jsonl_file.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            match = re.match(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(.*)", jsonl_file.stem)
            if not match:
                continue
            session_id = match.group(1)
            # Extract project path from first line (session_meta)
            project_path = _get_codex_project_path(jsonl_file)
            candidates.append((mtime, "codex", session_id, project_path, jsonl_file))

    # Sort by mtime descending (most recent first)
    candidates.sort(key=lambda x: x[0], reverse=True)

    results = []
    for mtime, agent, session_id, project_path, jsonl_path in candidates:
        # Look up title
        cache_key = f"{agent}:{session_id}"
        cached = title_cache.get(cache_key, {})
        title = cached.get("title") if isinstance(cached, dict) else None

        # Fallback: first user message
        if not title:
            title = _get_first_user_message(jsonl_path)

        if not title:
            continue  # Skip sessions with no identifiable content

        results.append({
            "agent": agent,
            "title": title,
            "project_path": project_path,
        })

    return results


def _decode_claude_project_dir(dir_name: str, known_projects: list) -> Optional[str]:
    """Match an encoded Claude project directory to a known project path.

    First tries matching against known projects from projects.yaml.
    Falls back to scanning the actual ~/.claude/projects/ directory structure
    to reverse the encoding.
    """
    # Try known projects first (most common)
    for project in known_projects:
        folder = project.get("folder", "")
        if not folder:
            continue
        for base in ["/Users/ph/Documents/Projects/", "/Users/ph/.agents/skills/",
                     "/Users/ph/Documents/www/", "/Users/ph/"]:
            full_path = base + folder
            encoded = full_path.replace("/", "-").replace(".", "-")
            if dir_name == encoded or dir_name.startswith(encoded):
                return full_path

    # Fallback: try to reverse-decode the directory name by testing known filesystem paths
    # The encoding is: replace / with - and . with -
    # We can't perfectly reverse this, but we can try common parent directories
    home = str(Path.home())
    common_bases = [
        home + "/Documents/Projects",
        home + "/Documents/www",
        home + "/.agents/skills",
        home + "/.agents",
        home,
    ]
    for base in common_bases:
        encoded_base = base.replace("/", "-").replace(".", "-")
        if dir_name.startswith(encoded_base):
            # The remainder after the base is the project-specific part
            # Return the base path (best we can do without exact reversal)
            return base
    return None


def _get_codex_project_path(jsonl_path: Path) -> Optional[str]:
    """Extract project working directory from Codex session first line."""
    try:
        with open(jsonl_path) as f:
            first_line = f.readline()
        entry = json.loads(first_line)
        if entry.get("type") == "session_meta":
            return entry.get("payload", {}).get("cwd")
    except Exception:
        pass
    return None


def _get_first_user_message(jsonl_path: Path, max_lines: int = 20) -> Optional[str]:
    """Read the first user message from a session JSONL file."""
    try:
        with open(jsonl_path) as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "user":
                    content = entry.get("content", "")
                    if isinstance(content, list):
                        content = next((c.get("text", "") for c in content if c.get("type") == "text"), "")
                    return content[:200] if content else None
    except Exception:
        pass
    return None


def match_project(active_window: Optional[ActiveWindow], visible_apps: List[str], config: CaptureConfig) -> Optional[str]:
    """Auto-detect project from window title and apps."""
    if not active_window:
        return None

    text_to_check = f"{active_window.app} {active_window.title} {' '.join(visible_apps)}"

    for pattern_config in config.project_patterns:
        pattern = pattern_config.get("pattern", "")
        project = pattern_config.get("project", "")
        if pattern and project:
            if re.search(pattern, text_to_check, re.IGNORECASE):
                return project

    return None


def is_paused(config: CaptureConfig) -> bool:
    """Check if capture is currently paused."""
    if not config.pause_until:
        return False

    try:
        pause_until = datetime.fromisoformat(config.pause_until)
        return datetime.now() < pause_until
    except Exception:
        return False


def get_capture_dir() -> Tuple[Path, str]:
    """Get the capture directory for the current timestamp."""
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H-%M-%S")
    timestamp = now.isoformat()

    capture_dir = CAPTURES_DIR / date_str / time_str
    return capture_dir, timestamp


def update_daily_log(metadata: CaptureMetadata, capture_dir: Path):
    """Add the capture to today's daily log."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    daily_file = DAILY_DIR / f"{date_str}.json"

    if daily_file.exists():
        daily_log = DailyLog.load(daily_file)
    else:
        daily_log = DailyLog(date=date_str)

    # Store relative path from captures dir
    rel_path = str(capture_dir.relative_to(CAPTURES_DIR))
    daily_log.add_entry(metadata, rel_path)
    daily_log.save(daily_file)


def run_capture(config: Optional[CaptureConfig] = None, skip_analysis: bool = False) -> Optional[CaptureMetadata]:
    """
    Main capture function.

    Args:
        config: Configuration to use (loads default if None)
        skip_analysis: If True, skip Gemini analysis (for testing)

    Returns:
        CaptureMetadata if successful, None otherwise
    """
    if config is None:
        config = load_config()

    # Rotate logs if needed
    rotate_logs()

    log_capture_event("started", "Capture cycle started")

    # Check if paused
    if is_paused(config):
        log_capture_event(
            "skipped_paused",
            "Capture skipped: paused",
            reason="User paused capturing",
            details={"pause_until": config.pause_until}
        )
        print("Capture is paused, skipping.")
        return None

    # Check if display is off (screen dimmed due to inactivity)
    if is_display_off():
        log_capture_event(
            "skipped_display_off",
            "Capture skipped: display off",
            reason="Display is asleep/off"
        )
        print("Display is off, skipping capture.")
        return None

    # Check if screen is locked
    if is_screen_locked():
        log_capture_event(
            "skipped_locked",
            "Capture skipped: screen locked",
            reason="Screen is locked"
        )
        print("Screen is locked, skipping capture.")
        return None

    # Get active window info first (for pre-filtering)
    # Use get_window_info() once to get both values efficiently
    active_window, visible_apps = get_window_info()

    # Pre-filter: check for sensitive windows
    if check_sensitive_window(active_window, config):
        app_name = active_window.app if active_window else "unknown"
        log_capture_event(
            "skipped_sensitive",
            f"Capture skipped: sensitive window ({app_name})",
            reason="Sensitive window pattern matched",
            details={"app": app_name, "title": active_window.title if active_window else None}
        )
        print(f"Sensitive window detected ({app_name}), skipping capture.")
        return None

    # Check for video/entertainment apps — log minimal entry, skip screenshots and Gemini
    if check_skip_window(active_window, config):
        app_name = active_window.app if active_window else "unknown"
        log_capture_event(
            "skipped_entertainment",
            f"Capture skipped: entertainment app ({app_name})",
            reason="Skip window pattern matched",
            details={"app": app_name}
        )
        print(f"Entertainment app detected ({app_name}), logging minimal entry.")

        # Create a minimal entry with no screenshots or AI call
        capture_dir, timestamp = get_capture_dir()
        capture_dir.mkdir(parents=True, exist_ok=True)
        analysis = Analysis(
            description=f"Watching/using {app_name}",
            category="entertainment",
            oneline=f"Using {app_name}",
            is_work=False,
            inferred_project=None,
        )
        metadata = CaptureMetadata(
            timestamp=timestamp,
            screens=[],
            active_window=active_window,
            visible_apps=visible_apps,
            analysis=analysis,
            auto_project=None,
        )
        metadata.save(capture_dir / "metadata.json")
        update_daily_log(metadata, capture_dir)
        return metadata

    # Set up capture directory
    capture_dir, timestamp = get_capture_dir()

    # Capture screenshots
    print(f"Capturing screenshots to {capture_dir}...")
    screenshots = capture_screenshots(capture_dir, config.jpeg_quality)

    if not screenshots:
        log_capture_event("error", "No screenshots captured", reason="screencapture failed")
        print("No screenshots captured!")
        return None

    log_capture_event(
        "captured",
        f"Captured {len(screenshots)} screen(s)",
        details={"screens": len(screenshots), "path": str(capture_dir)}
    )
    print(f"Captured {len(screenshots)} screen(s)")

    # Check for black screens (display off)
    black_screens = []
    for screenshot in screenshots:
        screenshot_path = capture_dir / screenshot
        if is_black_screen(screenshot_path):
            screen_num = int(screenshot.split("-")[1].split(".")[0])
            black_screens.append(screen_num)

    if black_screens:
        if len(black_screens) == len(screenshots):
            # All screens are black - display is off
            log_capture_event(
                "skipped_display_off",
                "Capture skipped: all screens black (display off)",
                reason="Display appears to be off",
                details={"black_screens": black_screens}
            )
            print("All screens are black (display off), skipping capture.")
            import shutil
            shutil.rmtree(capture_dir)
            return None
        else:
            # Some screens black - remove them from the list
            for screen_num in black_screens:
                screenshot = f"screen-{screen_num}.webp"
                if screenshot in screenshots:
                    screenshots.remove(screenshot)
                    (capture_dir / screenshot).unlink(missing_ok=True)
                    log_capture_event(
                        "skipped_black_screen",
                        f"Screen {screen_num} removed: black (display off)",
                        details={"screen": screen_num}
                    )
                    print(f"Screen {screen_num} is black (display off), removing")

    # Check if screen is similar to previous capture (skip if idle/unchanged)
    if config.skip_similar_threshold > 0:
        if is_screen_similar_to_previous(capture_dir, screenshots, config.skip_similar_threshold):
            log_capture_event(
                "skipped_similar",
                f"Capture skipped: screen unchanged (<{config.skip_similar_threshold*100:.0f}% diff)",
                reason="Screenshot similar to previous",
                details={"threshold": config.skip_similar_threshold}
            )
            print(f"Screen unchanged (<{config.skip_similar_threshold*100:.0f}% diff), skipping capture.")
            # Clean up the captured screenshots
            import shutil
            shutil.rmtree(capture_dir)
            return None

    # Filter out blank desktop screens (wallpaper only)
    excluded_blank_screens = []
    if config.blank_desktop_threshold > 0:
        from PIL import Image
        active_screenshots = []
        for screenshot in screenshots:
            # Extract screen number: "screen-2.webp" -> 2
            screen_num = int(screenshot.split("-")[1].split(".")[0])
            screenshot_path = capture_dir / screenshot
            if is_blank_desktop(screenshot_path, screen_num, config.blank_desktop_threshold, config.blank_desktop_crop_top):
                # Resize to thumbnail and rename with --blank suffix
                blank_filename = f"screen-{screen_num}--blank.webp"
                blank_path = capture_dir / blank_filename
                try:
                    img = Image.open(screenshot_path)
                    # Resize to 300px wide, maintaining aspect ratio
                    ratio = 300 / img.width
                    new_size = (300, int(img.height * ratio))
                    img_thumb = img.resize(new_size, Image.LANCZOS)
                    img_thumb.save(blank_path, "WebP", quality=60)
                    img.close()
                    # Remove original full-size image
                    screenshot_path.unlink()
                except Exception as e:
                    print(f"Failed to create blank thumbnail: {e}")

                excluded_blank_screens.append(blank_filename)
                log_capture_event(
                    "skipped_blank_desktop",
                    f"Screen {screen_num} excluded: blank desktop wallpaper",
                    details={"screen": screen_num, "threshold": config.blank_desktop_threshold, "thumbnail": blank_filename}
                )
                print(f"Screen {screen_num} is blank desktop, excluding from analysis (saved as thumbnail)")
            else:
                active_screenshots.append(screenshot)

        if not active_screenshots:
            # All screens are blank - skip entire capture
            log_capture_event(
                "skipped_all_blank",
                "Capture skipped: all screens are blank desktop",
                reason="All screens showing wallpaper only",
                details={"blank_screens": [s for s in excluded_blank_screens]}
            )
            print("All screens are blank desktop, skipping capture.")
            import shutil
            shutil.rmtree(capture_dir)
            return None

        # Use filtered list for analysis
        screenshots = active_screenshots

    # Look up active agent sessions
    active_sessions = None
    try:
        sessions = get_active_agent_sessions()
        if sessions:
            active_sessions = sessions
    except Exception as e:
        print(f"Agent session lookup failed: {e}")

    # Get focus history from focus logger daemon
    focus_history = None
    try:
        focus_history = get_focus_history(minutes=2)
    except Exception as e:
        print(f"Focus history lookup failed: {e}")

    # Auto-detect project
    auto_project = match_project(active_window, visible_apps, config)

    # Create metadata
    metadata = CaptureMetadata(
        timestamp=timestamp,
        screens=screenshots,
        active_window=active_window,
        visible_apps=visible_apps,
        auto_project=auto_project,
        excluded_blank_screens=excluded_blank_screens,
        active_sessions=active_sessions,
        focus_history=focus_history
    )

    # Analyze with Gemini (unless skipped)
    if not skip_analysis:
        try:
            from analyze import analyze_capture
            analysis = analyze_capture(capture_dir, screenshots, active_window, visible_apps, config, session_context=active_sessions, focus_history=focus_history)
            metadata.analysis = analysis

            # Apply deterministic app rules (override AI inference)
            if analysis:
                apply_app_rules(active_window, analysis, config)

            if analysis:
                log_capture_event(
                    "analyzed",
                    f"Analysis complete: {analysis.oneline}",
                    details={
                        "category": analysis.category,
                        "is_meeting": analysis.is_meeting,
                        "people": analysis.people[:3] if analysis.people else [],
                        "organizations": analysis.organizations[:3] if analysis.organizations else [],
                        "sensitive": analysis.sensitive
                    }
                )

            # Handle sensitive content detection
            if analysis and analysis.sensitive:
                print(f"Sensitive content detected: {analysis.sensitive_reason}")
                if config.auto_delete_sensitive:
                    log_capture_event(
                        "deleted_sensitive",
                        "Capture deleted: sensitive content detected",
                        reason=analysis.sensitive_reason
                    )
                    print("Auto-deleting sensitive capture...")
                    import shutil
                    shutil.rmtree(capture_dir)
                    return None

        except Exception as e:
            log_capture_event("error", f"Analysis failed: {e}", reason=str(e))
            print(f"Analysis failed: {e}")
            # Continue without analysis

    # Save metadata
    metadata_path = capture_dir / "metadata.json"
    metadata.save(metadata_path)
    print(f"Saved metadata to {metadata_path}")

    # Update daily log
    if metadata.analysis:
        update_daily_log(metadata, capture_dir)
        print(f"Updated daily log")

    log_capture_event(
        "completed",
        "Capture cycle completed",
        details={
            "path": str(capture_dir.relative_to(CAPTURES_DIR)),
            "category": metadata.analysis.category if metadata.analysis else None,
            "project": metadata.project
        }
    )

    return metadata


def main():
    """Entry point for capture script."""
    import argparse

    parser = argparse.ArgumentParser(description="DayLogger Capture")
    parser.add_argument("--skip-analysis", action="store_true", help="Skip Gemini analysis")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    try:
        metadata = run_capture(skip_analysis=args.skip_analysis)
        if metadata:
            if args.verbose:
                print(json.dumps(metadata.to_dict(), indent=2))
            print("Capture complete!")
        else:
            print("Capture skipped or failed.")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
