#!/usr/bin/env python3
"""
Day Tracker Capture Script

Captures screenshots and active window information, analyzes with Gemini,
and saves to the daily log.

Run via launchd every 5 minutes, or manually for testing.
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

    # Check if reference wallpaper exists
    reference_path = REFERENCE_WALLPAPERS_DIR / f"screen-{screen_number}-wallpaper.jpg"
    if not reference_path.exists():
        return False  # No reference to compare against

    try:
        from PIL import Image
        import numpy as np

        # Load images
        screenshot = Image.open(screenshot_path).convert('RGB')
        reference = Image.open(reference_path).convert('RGB')

        # Crop top portion (menu bar with clock/status that changes)
        if crop_top > 0:
            screenshot = screenshot.crop((0, crop_top, screenshot.width, screenshot.height))
            reference = reference.crop((0, crop_top, reference.width, reference.height))

        # Resize both to same small size for fast comparison
        sample_size = 100
        screenshot = screenshot.resize((sample_size, sample_size), Image.LANCZOS)
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
        return difference < threshold

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


def capture_screenshots(output_dir: Path, quality: int = 70) -> List[str]:
    """
    Capture all screens and save as JPEGs.

    Returns list of filenames.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    filenames = []
    screen_count = get_screen_count()

    for i in range(1, screen_count + 1):
        filename = f"screen-{i}.jpg"
        filepath = output_dir / filename

        # -x: silent (no sound)
        # -t jpg: JPEG format
        # -D: display number (1-indexed)
        result = subprocess.run(
            ["screencapture", "-x", "-t", "jpg", "-D", str(i), str(filepath)],
            capture_output=True
        )

        if filepath.exists():
            filenames.append(filename)

            # Scale to 75% and compress using PIL
            img = Image.open(filepath)
            new_size = (int(img.width * 0.75), int(img.height * 0.75))
            img = img.resize(new_size, Image.LANCZOS)
            img.save(filepath, "JPEG", quality=quality)

    return filenames


# Path to helper app that has Accessibility permissions
HELPER_APP = Path(__file__).parent / "DayTrackerHelper.app" / "Contents" / "MacOS" / "get-window-info"


def get_window_info() -> Tuple[Optional[ActiveWindow], List[str]]:
    """
    Get active window and visible apps using the helper app.
    The helper app has its own Accessibility permissions, keeping Python unprivileged.
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
                active_window = ActiveWindow(
                    app=data.get("app", ""),
                    title=data.get("title", "")
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


def check_sensitive_window(active_window: Optional[ActiveWindow], config: CaptureConfig) -> bool:
    """Check if current window matches sensitive patterns (pre-filter)."""
    if not active_window:
        return False

    text_to_check = f"{active_window.app} {active_window.title}".lower()

    for pattern in config.sensitive_window_patterns:
        if pattern.lower() in text_to_check:
            return True

    return False


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
                screenshot = f"screen-{screen_num}.jpg"
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
            # Extract screen number: "screen-2.jpg" -> 2
            screen_num = int(screenshot.split("-")[1].split(".")[0])
            screenshot_path = capture_dir / screenshot
            if is_blank_desktop(screenshot_path, screen_num, config.blank_desktop_threshold, config.blank_desktop_crop_top):
                # Resize to thumbnail and rename with --blank suffix
                blank_filename = f"screen-{screen_num}--blank.jpg"
                blank_path = capture_dir / blank_filename
                try:
                    img = Image.open(screenshot_path)
                    # Resize to 300px wide, maintaining aspect ratio
                    ratio = 300 / img.width
                    new_size = (300, int(img.height * ratio))
                    img_thumb = img.resize(new_size, Image.LANCZOS)
                    img_thumb.save(blank_path, "JPEG", quality=70)
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

    # Auto-detect project
    auto_project = match_project(active_window, visible_apps, config)

    # Create metadata
    metadata = CaptureMetadata(
        timestamp=timestamp,
        screens=screenshots,
        active_window=active_window,
        visible_apps=visible_apps,
        auto_project=auto_project,
        excluded_blank_screens=excluded_blank_screens
    )

    # Analyze with Gemini (unless skipped)
    if not skip_analysis:
        try:
            from analyze import analyze_capture
            analysis = analyze_capture(capture_dir, screenshots, active_window, visible_apps, config)
            metadata.analysis = analysis

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
