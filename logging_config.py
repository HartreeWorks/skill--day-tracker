"""Logging configuration for Day Tracker."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import DATA_DIR

# Log file location
LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "capture.log"
LOG_MAX_LINES = 1000  # Keep last N log entries


class JSONFormatter(logging.Formatter):
    """Format log records as JSON for easy parsing."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }

        # Add extra fields if present
        if hasattr(record, "action"):
            log_entry["action"] = record.action
        if hasattr(record, "reason"):
            log_entry["reason"] = record.reason
        if hasattr(record, "details"):
            log_entry["details"] = record.details

        return json.dumps(log_entry)


def get_logger() -> logging.Logger:
    """Get configured logger for capture operations."""
    logger = logging.getLogger("day-tracker")

    if not logger.handlers:
        logger.setLevel(logging.INFO)

        # Ensure log directory exists
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

        # File handler with JSON formatting
        file_handler = logging.FileHandler(LOG_FILE)
        file_handler.setFormatter(JSONFormatter())
        logger.addHandler(file_handler)

        # Console handler for debugging
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(console_handler)

    return logger


def log_capture_event(
    action: str,
    message: str,
    reason: Optional[str] = None,
    details: Optional[dict] = None
):
    """
    Log a capture event with structured data.

    Actions:
    - started: Capture process started
    - skipped_paused: Skipped because paused
    - skipped_sensitive: Skipped due to sensitive window
    - skipped_similar: Skipped due to similar screenshot
    - captured: Screenshot captured successfully
    - analyzed: Analysis completed
    - completed: Full capture cycle completed
    - error: An error occurred
    """
    logger = get_logger()

    # Create a log record with extra fields
    extra = {"action": action}
    if reason:
        extra["reason"] = reason
    if details:
        extra["details"] = details

    logger.info(message, extra=extra)


def read_logs(limit: int = 100, level: Optional[str] = None) -> list:
    """
    Read recent log entries.

    Args:
        limit: Maximum number of entries to return
        level: Filter by log level (INFO, WARNING, ERROR)

    Returns:
        List of log entries (most recent first)
    """
    if not LOG_FILE.exists():
        return []

    entries = []
    try:
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()

        # Parse JSON lines (most recent last in file)
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if level and entry.get("level") != level:
                    continue
                entries.append(entry)
                if len(entries) >= limit:
                    break
            except json.JSONDecodeError:
                continue

    except Exception:
        pass

    return entries


def rotate_logs():
    """Rotate log file if it exceeds max lines."""
    if not LOG_FILE.exists():
        return

    try:
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()

        if len(lines) > LOG_MAX_LINES:
            # Keep only the most recent entries
            with open(LOG_FILE, "w") as f:
                f.writelines(lines[-LOG_MAX_LINES:])
    except Exception:
        pass
