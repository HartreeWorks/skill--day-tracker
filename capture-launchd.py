#!/usr/bin/env python3
"""
Launchd wrapper for day-tracker capture.

Calls run_capture() directly (bypassing argparse main()) since capture.py's
main() exits with code 78 under launchd for unknown reasons.

Handles its own logging since launchd's StandardOutPath can be unreliable.

Watchdog: a hard max-runtime guard kills the process if a capture hangs (e.g.
a wedged Gemini API call, a stuck incognito-detection AppleScript, or blocked
network I/O). launchd won't start a new instance while the previous one is
still "running", so without this a single hung capture can silently block ALL
captures until the next reboot — which is exactly what happened on 2026-06-24
(one process stuck for ~6 days). The guard must fire well before the 120s
StartInterval so each tick gets a fresh process. Override the timeout with
DAY_TRACKER_CAPTURE_TIMEOUT_SECONDS.
"""
import sys
import os
import signal
import threading

LOG_PATH = os.path.expanduser("~/Documents/day-tracker/data/logs/launchd.log")
WATCHDOG_TIMEOUT_SECONDS = int(
    os.environ.get("DAY_TRACKER_CAPTURE_TIMEOUT_SECONDS", "100")
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Redirect stdout/stderr to log file
log_file = open(LOG_PATH, "a")
sys.stdout = log_file
sys.stderr = log_file


def _watchdog():
    """Daemon thread: SIGKILL self if the capture overruns.

    Runs in a separate thread because the failure mode is a blocking network
    call. Such calls release the GIL, so this thread keeps running and can act
    even while the main thread is wedged. SIGKILL is used (not os._exit) so the
    process dies even if the main thread is stuck in an uninterruptible state;
    it can't be caught or ignored. We write + flush the log line first because
    SIGKILL skips the normal `finally` cleanup.
    """
    print(
        f"WATCHDOG: capture exceeded {WATCHDOG_TIMEOUT_SECONDS}s — killing "
        f"pid {os.getpid()} so launchd can start a fresh capture next tick.",
        flush=True,
    )
    log_file.flush()
    os.fsync(log_file.fileno())
    os.kill(os.getpid(), signal.SIGKILL)


# Daemon thread: torn down automatically when the process exits normally, so it
# never fires after a successful, timely capture.
watchdog = threading.Timer(WATCHDOG_TIMEOUT_SECONDS, _watchdog)
watchdog.daemon = True
watchdog.start()

try:
    from capture import run_capture
    result = run_capture()
    if result:
        print("Capture complete!", flush=True)
    else:
        print("Capture skipped or failed.", flush=True)
except Exception as e:
    print(f"Error: {e}", flush=True)
    import traceback
    traceback.print_exc()
    sys.exit(1)
finally:
    watchdog.cancel()
    log_file.close()
