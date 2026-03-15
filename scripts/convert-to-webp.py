#!/usr/bin/env python3
"""Batch convert day-tracker JPG screenshots to WebP at 50% dimensions, quality 60.

Regular screenshots (screen-N.jpg): resize to 50% of current dimensions, save as WebP.
Blank thumbnails (screen-N--blank.jpg): convert to WebP without resizing (already 300px wide).
Updates metadata.json in each capture folder to reference .webp instead of .jpg.

Usage: python3 convert-to-webp.py [--dry-run]
"""

import json
import sys
from pathlib import Path
from PIL import Image

CAPTURES_DIR = Path("/Users/ph/Documents/day-tracker/data/captures")
QUALITY = 60
SCALE = 0.5  # 50% of current (full native) dimensions
DRY_RUN = "--dry-run" in sys.argv


def convert_folder(capture_dir: Path) -> tuple[int, int, int]:
    """Convert all JPGs in a capture event folder. Returns (count, bytes_before, bytes_after)."""
    count = 0
    bytes_before = 0
    bytes_after = 0

    jpg_files = sorted(capture_dir.glob("*.jpg"))
    if not jpg_files:
        return 0, 0, 0

    for jpg_path in jpg_files:
        is_blank = "--blank" in jpg_path.stem
        webp_name = jpg_path.stem + ".webp"
        webp_path = jpg_path.with_suffix(".webp")

        # Skip if already converted
        if webp_path.exists():
            continue

        before_size = jpg_path.stat().st_size
        bytes_before += before_size

        if DRY_RUN:
            count += 1
            continue

        img = Image.open(jpg_path)

        if is_blank:
            # Already a small thumbnail — just convert format
            img.save(webp_path, "WebP", quality=QUALITY)
        else:
            # Resize to 50% of current dimensions
            new_size = (int(img.width * SCALE), int(img.height * SCALE))
            img = img.resize(new_size, Image.LANCZOS)
            img.save(webp_path, "WebP", quality=QUALITY)

        img.close()

        after_size = webp_path.stat().st_size
        bytes_after += after_size

        # Delete original
        jpg_path.unlink()
        count += 1

    # Update metadata.json
    meta_path = capture_dir / "metadata.json"
    if meta_path.exists() and not DRY_RUN:
        meta = json.loads(meta_path.read_text())
        changed = False

        if "screens" in meta:
            new_screens = [s.replace(".jpg", ".webp") for s in meta["screens"]]
            if new_screens != meta["screens"]:
                meta["screens"] = new_screens
                changed = True

        if "excluded_blank_screens" in meta:
            new_blanks = [s.replace(".jpg", ".webp") for s in meta["excluded_blank_screens"]]
            if new_blanks != meta["excluded_blank_screens"]:
                meta["excluded_blank_screens"] = new_blanks
                changed = True

        if changed:
            meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")

    return count, bytes_before, bytes_after


def fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    elif b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    elif b < 1024 * 1024 * 1024:
        return f"{b / (1024 * 1024):.1f} MB"
    else:
        return f"{b / (1024 * 1024 * 1024):.2f} GB"


def main():
    if DRY_RUN:
        print("DRY RUN — no files will be modified\n")

    date_dirs = sorted(d for d in CAPTURES_DIR.iterdir() if d.is_dir())
    total_count = 0
    total_before = 0
    total_after = 0

    for date_dir in date_dirs:
        date_count = 0
        date_before = 0
        date_after = 0

        event_dirs = sorted(d for d in date_dir.iterdir() if d.is_dir())
        for event_dir in event_dirs:
            c, b, a = convert_folder(event_dir)
            date_count += c
            date_before += b
            date_after += a

        if date_count > 0:
            if DRY_RUN:
                print(f"{date_dir.name}: {date_count} files to convert ({fmt_size(date_before)})")
            else:
                print(f"{date_dir.name}: {date_count} files converted ({fmt_size(date_before)} → {fmt_size(date_after)})")

        total_count += date_count
        total_before += date_before
        total_after += date_after

    print(f"\n{'=' * 60}")
    if DRY_RUN:
        print(f"Total: {total_count} files to convert ({fmt_size(total_before)})")
    else:
        print(f"Total: {total_count} files converted")
        print(f"Before: {fmt_size(total_before)}")
        print(f"After:  {fmt_size(total_after)}")
        if total_before > 0:
            reduction = (1 - total_after / total_before) * 100
            print(f"Reduction: {reduction:.1f}%")


if __name__ == "__main__":
    main()
