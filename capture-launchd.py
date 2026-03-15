#!/usr/bin/env python3
"""
Launchd wrapper for day-tracker capture.

Calls run_capture() directly (bypassing argparse main()) since capture.py's
main() exits with code 78 under launchd for unknown reasons.

Handles its own logging since launchd's StandardOutPath can be unreliable.
"""
import sys
import os

LOG_PATH = os.path.expanduser("~/Documents/day-tracker/data/logs/launchd.log")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Redirect stdout/stderr to log file
log_file = open(LOG_PATH, "a")
sys.stdout = log_file
sys.stderr = log_file

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
    log_file.close()
