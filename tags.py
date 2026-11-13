"""Manual project tags, written to capture metadata and the daily log together."""

from collections import defaultdict
from pathlib import Path
from typing import Iterable, Optional

from config import CAPTURES_DIR, DAILY_DIR
from models import CaptureMetadata, DailyLog, daily_log_lock


def tag_captures(capture_dirs: Iterable[str], project: Optional[str]) -> int:
    """Tag captures (paths like "2026-10-21/14-30-00") with a project, or clear it with None.

    The daily entry gets manual_project at once; the rollup turns it into
    canonical_project, ahead of every automatic rule. Returns the number of
    captures tagged.
    """
    project = project or None
    by_day = defaultdict(set)
    for rel_dir in capture_dirs:
        metadata_file = CAPTURES_DIR / rel_dir / "metadata.json"
        if not metadata_file.exists():
            continue
        metadata = CaptureMetadata.load(metadata_file)
        metadata.manual_project = project
        metadata.save(metadata_file)
        by_day[Path(rel_dir).parts[0]].add(rel_dir)

    with daily_log_lock():
        for day, rel_dirs in by_day.items():
            daily_file = DAILY_DIR / f"{day}.json"
            if not daily_file.exists():
                continue
            daily_log = DailyLog.load(daily_file)
            for entry in daily_log.entries:
                if entry.capture_dir in rel_dirs:
                    entry.manual_project = project
            daily_log.save(daily_file)

    return sum(len(d) for d in by_day.values())
