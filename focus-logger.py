#!/usr/bin/env python3
"""
Focus Logger Daemon for Day Tracker

Event-driven logger that records app focus changes using macOS NSWorkspace
notifications. Writes JSON lines to daily log files for consumption by
capture.py at screenshot time.

Run as a persistent launchd agent (com.ph.daytracker.focus-logger).
"""

import json
import signal
import sys
import threading
from datetime import datetime, date
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import objc
import AppKit
import Quartz
from Foundation import NSObject, NSRunLoop, NSDate

FOCUS_LOG_DIR = Path.home() / "Documents" / "day-tracker" / "data" / "focus-log"
HTTP_PORT = 7847

# Shared reference so the HTTP handler can write to the same log file
_observer_ref = None


class FocusLogHandler(BaseHTTPRequestHandler):
    """Handles POST /log from the Chrome extension."""

    def do_POST(self):
        if self.path != "/log":
            self.send_response(404)
            self.end_headers()
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self.send_response(400)
            self.end_headers()
            return

        app = body.get("app", "")
        title = body.get("title", "")

        if _observer_ref:
            _observer_ref.write_entry(app, title)

        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress default stderr logging


def start_http_server():
    server = HTTPServer(("127.0.0.1", HTTP_PORT), FocusLogHandler)
    server.serve_forever()


def get_window_title(app_name: str) -> str:
    """Get the frontmost window title for the given app using Quartz."""
    try:
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        if windows:
            for win in windows:
                if (
                    win.get("kCGWindowLayer", 999) == 0
                    and win.get("kCGWindowOwnerName") == app_name
                    and win.get("kCGWindowName")
                ):
                    return win["kCGWindowName"]
    except Exception:
        pass
    return ""


class FocusObserver(NSObject):
    """Observes NSWorkspaceDidActivateApplicationNotification."""

    def init(self):
        self = objc.super(FocusObserver, self).init()
        if self is None:
            return None
        self._current_date = date.today()
        self._log_file = None
        self._open_log_file()
        return self

    def _log_path_for_date(self, d: date) -> Path:
        return FOCUS_LOG_DIR / f"{d.isoformat()}.jsonl"

    def _open_log_file(self):
        """Open (or reopen) the log file for today."""
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
        FOCUS_LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._current_date = date.today()
        self._log_file = open(self._log_path_for_date(self._current_date), "a")

    def _ensure_correct_date(self):
        """Roll over to a new file at midnight."""
        today = date.today()
        if today != self._current_date:
            self._open_log_file()

    def write_entry(self, app: str, title: str):
        """Write a focus entry to the log file. Thread-safe via GIL for simple appends."""
        try:
            self._ensure_correct_date()
            entry = {
                "t": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "app": app,
                "title": title,
            }
            self._log_file.write(json.dumps(entry) + "\n")
            self._log_file.flush()
        except Exception as e:
            print(f"Error writing focus entry: {e}", file=sys.stderr)

    def appActivated_(self, notification):
        """Called when an application gains focus."""
        try:
            user_info = notification.userInfo()
            app = user_info["NSWorkspaceApplicationKey"]
            app_name = app.localizedName()
            title = get_window_title(app_name)
            self.write_entry(app_name, title)
        except Exception as e:
            print(f"Error logging focus change: {e}", file=sys.stderr)


def main():
    global _observer_ref

    FOCUS_LOG_DIR.mkdir(parents=True, exist_ok=True)

    observer = FocusObserver.alloc().init()
    _observer_ref = observer

    # Start HTTP server for Chrome extension tab tracking
    http_thread = threading.Thread(target=start_http_server, daemon=True)
    http_thread.start()
    print(f"HTTP server listening on 127.0.0.1:{HTTP_PORT}")

    workspace = AppKit.NSWorkspace.sharedWorkspace()
    nc = workspace.notificationCenter()
    nc.addObserver_selector_name_object_(
        observer,
        "appActivated:",
        AppKit.NSWorkspaceDidActivateApplicationNotification,
        None,
    )

    print(f"Focus logger started. Logging to {FOCUS_LOG_DIR}/")

    # Handle SIGTERM gracefully
    def shutdown(signum, frame):
        print("Focus logger shutting down.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Run the event loop
    run_loop = NSRunLoop.currentRunLoop()
    while True:
        run_loop.runMode_beforeDate_(
            AppKit.NSDefaultRunLoopMode,
            NSDate.dateWithTimeIntervalSinceNow_(1.0),
        )


if __name__ == "__main__":
    main()
