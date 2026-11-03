#!/usr/bin/env python3
"""
DayLogger Web UI Server

A simple FastAPI server for browsing captured screenshots and data.
"""

import os
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import CAPTURES_DIR, DAILY_DIR, DATA_DIR, REPORTS_DIR, load_config
from models import CaptureMetadata, DailyLog
from logging_config import read_logs

HOST = "127.0.0.1"
PORT = 8765

# Create FastAPI app
app = FastAPI(title="DayLogger", description="Personal time tracking viewer")

# --- Request guards -------------------------------------------------------
# The server holds every screenshot, so it accepts only requests from its
# own pages or typed into the address bar:
# - the Host header must name this server, which defeats DNS rebinding;
# - browsers mark requests started by another site (including other
#   localhost ports) with Sec-Fetch-Site, and those are refused;
# - a request carrying a foreign Origin header is refused.
# The page also gets a Content-Security-Policy that stops scripts sending
# data to any other origin.
ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}
CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}-\d{2}-\d{2}$")
SCREEN_FILE_RE = re.compile(r"^screen-\d+(--blank)?\.webp$")


@app.middleware("http")
async def guard_requests(request: Request, call_next):
    if request.headers.get("host") not in ALLOWED_HOSTS:
        return PlainTextResponse("Forbidden host", status_code=403)
    if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
        # A top-level GET navigation (a link or bookmark to the timeline) is
        # safe: the other site cannot read the page, and GETs change nothing.
        # Everything else from another site (fetches, images, frames, form
        # POSTs) is refused.
        is_navigation = (
            request.method in ("GET", "HEAD")
            and request.headers.get("sec-fetch-mode") == "navigate"
            and request.headers.get("sec-fetch-dest") == "document"
        )
        if not is_navigation:
            return PlainTextResponse("Cross-site request refused", status_code=403)
    origin = request.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        return PlainTextResponse("Foreign origin refused", status_code=403)
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def require_date(value: str) -> str:
    """Reject anything but a YYYY-MM-DD date (blocks path traversal)."""
    if not value or not DATE_RE.match(value):
        raise HTTPException(status_code=400, detail="Invalid date")
    return value


def capture_dir_for(capture_path: str) -> Path:
    """Map 'YYYY-MM-DD/HH-MM-SS' to its capture folder, or raise 400."""
    parts = str(capture_path).split("/")
    if len(parts) != 2 or not DATE_RE.match(parts[0]) or not TIME_RE.match(parts[1]):
        raise HTTPException(status_code=400, detail="Invalid capture path")
    return CAPTURES_DIR / parts[0] / parts[1]

# Get the directory where this script is located
SCRIPT_DIR = Path(__file__).parent
TEMPLATES_DIR = SCRIPT_DIR / "templates"
STATIC_DIR = SCRIPT_DIR / "static"

# Set up templates
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Mount static files
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def calculate_costs(days: int = 7) -> dict:
    """Calculate API costs for the last N days using model-specific pricing."""
    from datetime import datetime, timedelta
    from config import MODEL_PRICING

    total_input_tokens = 0
    total_output_tokens = 0
    total_cost = 0.0
    daily_costs = []

    for i in range(days):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        date_dir = CAPTURES_DIR / date
        day_input = 0
        day_output = 0
        day_cost = 0.0

        if date_dir.exists():
            for time_dir in date_dir.iterdir():
                if not time_dir.is_dir():
                    continue
                metadata_file = time_dir / "metadata.json"
                if metadata_file.exists():
                    try:
                        metadata = CaptureMetadata.load(metadata_file)
                        if metadata.analysis:
                            day_input += metadata.analysis.input_tokens
                            day_output += metadata.analysis.output_tokens
                            # Use model-specific pricing (fallback to default for old captures)
                            model = getattr(metadata.analysis, 'model', 'gemini-2.5-flash-lite')
                            pricing = MODEL_PRICING.get(model, MODEL_PRICING["default"])
                            day_cost += (metadata.analysis.input_tokens / 1_000_000) * pricing["input"]
                            day_cost += (metadata.analysis.output_tokens / 1_000_000) * pricing["output"]
                    except Exception:
                        pass

        total_input_tokens += day_input
        total_output_tokens += day_output
        total_cost += day_cost
        daily_costs.append({"date": date, "cost": round(day_cost, 4)})

    return {
        "days": days,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cost_usd": round(total_cost, 4),
        "daily_breakdown": daily_costs
    }


def get_available_dates() -> List[str]:
    """Get list of dates with captures, sorted descending."""
    if not CAPTURES_DIR.exists():
        return []

    dates = []
    for d in CAPTURES_DIR.iterdir():
        if d.is_dir() and len(d.name) == 10:  # YYYY-MM-DD format
            dates.append(d.name)

    return sorted(dates, reverse=True)


def get_captures_for_date(date_str: str) -> List[dict]:
    """Get all captures for a specific date."""
    date_dir = CAPTURES_DIR / date_str
    if not date_dir.exists():
        return []

    captures = []
    for time_dir in sorted(date_dir.iterdir()):
        if not time_dir.is_dir():
            continue

        metadata_file = time_dir / "metadata.json"
        if metadata_file.exists():
            try:
                metadata = CaptureMetadata.load(metadata_file)
                capture_data = metadata.to_dict()
                capture_data["time"] = time_dir.name
                capture_data["path"] = f"{date_str}/{time_dir.name}"

                # Add thumbnail path (first screen)
                if metadata.screens:
                    capture_data["thumbnail"] = f"/screenshot/{date_str}/{time_dir.name}/{metadata.screens[0]}"

                captures.append(capture_data)
            except Exception as e:
                print(f"Error loading {metadata_file}: {e}")

    return captures


def get_daily_stats(date_str: str) -> dict:
    """Calculate statistics for a day."""
    daily_file = DAILY_DIR / f"{date_str}.json"

    stats = {
        "total_captures": 0,
        "by_category": {},
        "by_project": {},
        "sensitive_count": 0
    }

    if daily_file.exists():
        try:
            daily_log = DailyLog.load(daily_file)
            stats["total_captures"] = len(daily_log.entries)

            for entry in daily_log.entries:
                # Category stats
                cat = entry.category or "other"
                stats["by_category"][cat] = stats["by_category"].get(cat, 0) + 1

                # Project stats
                proj = entry.project or "untagged"
                stats["by_project"][proj] = stats["by_project"].get(proj, 0) + 1

                # Sensitive count
                if entry.sensitive:
                    stats["sensitive_count"] += 1

        except Exception as e:
            print(f"Error loading daily log: {e}")

    return stats


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Home page - show today's timeline."""
    today = datetime.now().strftime("%Y-%m-%d")
    return await day_view(request, today)


@app.get("/day/{date_str}", response_class=HTMLResponse)
async def day_view(request: Request, date_str: str):
    """View captures for a specific day."""
    require_date(date_str)
    captures = get_captures_for_date(date_str)
    stats = get_daily_stats(date_str)
    available_dates = get_available_dates()
    costs = calculate_costs(7)  # 7-day costs

    # Parse date for display
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        date_display = date_obj.strftime("%A, %B %d, %Y")
    except ValueError:
        date_display = date_str

    # Calculate prev/next dates
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        prev_date = (date_obj - timedelta(days=1)).strftime("%Y-%m-%d")
        next_date = (date_obj + timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        prev_date = next_date = None

    # Current Starlette takes the request first; the old (name, context)
    # order fails with "unhashable type: 'dict'".
    return templates.TemplateResponse(request, "timeline.html", {
        "date": date_str,
        "date_display": date_display,
        "captures": captures,
        "stats": stats,
        "available_dates": available_dates,
        "prev_date": prev_date,
        "next_date": next_date,
        "costs": costs
    })


@app.get("/screenshot/{date_str}/{time_str}/{filename}")
async def serve_screenshot(date_str: str, time_str: str, filename: str):
    """Serve a screenshot image."""
    if not SCREEN_FILE_RE.match(filename):
        raise HTTPException(status_code=400, detail="Invalid file name")
    file_path = capture_dir_for(f"{date_str}/{time_str}") / filename

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Screenshot not found")

    return FileResponse(file_path, media_type="image/webp")


@app.get("/api/captures")
async def api_captures(
    date: str = Query(default=None, description="Date in YYYY-MM-DD format"),
    category: str = Query(default=None, description="Filter by category"),
    project: str = Query(default=None, description="Filter by project")
):
    """API endpoint to get captures."""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    require_date(date)

    captures = get_captures_for_date(date)

    # Apply filters
    if category:
        captures = [c for c in captures if c.get("analysis", {}).get("category") == category]
    if project:
        captures = [c for c in captures if c.get("auto_project") == project or c.get("manual_project") == project]

    return {"date": date, "captures": captures, "count": len(captures)}


@app.post("/api/tag")
async def api_tag(request: Request):
    """API endpoint to manually tag captures."""
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(status_code=415, detail="JSON required")
    data = await request.json()
    captures = data.get("captures", [])  # List of paths like "2025-12-28/14-30-00"
    project = str(data.get("project", ""))[:200]

    updated = 0
    for capture_path in captures:
        metadata_file = capture_dir_for(capture_path) / "metadata.json"
        if metadata_file.exists():
            try:
                metadata = CaptureMetadata.load(metadata_file)
                metadata.manual_project = project if project else None
                metadata.save(metadata_file)
                updated += 1
            except Exception as e:
                print(f"Error updating {metadata_file}: {e}")

    return {"updated": updated, "project": project}


@app.get("/api/projects")
async def api_projects():
    """Get list of all known projects."""
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

    return {"projects": sorted(projects)}


@app.get("/api/dates")
async def api_dates():
    """Get list of available dates."""
    return {"dates": get_available_dates()}


@app.get("/api/costs")
async def api_costs(days: int = Query(default=7, description="Number of days to calculate")):
    """Get API cost breakdown for the last N days."""
    return calculate_costs(max(1, min(days, 366)))


@app.get("/api/logs")
async def api_logs(
    limit: int = Query(default=50, description="Number of log entries to return"),
    action: str = Query(default=None, description="Filter by action type")
):
    """
    Get recent capture log entries.

    Actions: started, captured, analyzed, completed, skipped_paused,
             skipped_sensitive, skipped_similar, deleted_sensitive, error
    """
    logs = read_logs(limit=limit)

    # Filter by action if specified
    if action:
        logs = [log for log in logs if log.get("action") == action]

    # Calculate summary stats
    actions = {}
    for log in logs:
        a = log.get("action", "unknown")
        actions[a] = actions.get(a, 0) + 1

    return {
        "logs": logs,
        "count": len(logs),
        "summary": actions
    }


def get_gemini_api_key() -> Optional[str]:
    """Get Gemini API key from environment or keychain."""
    import subprocess

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if api_key:
        return api_key

    # Try macOS keychain
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "daylogger-gemini", "-w"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass

    return None


@app.post("/api/story")
async def api_story(date: str = Query(description="Date in YYYY-MM-DD format"), force: bool = Query(default=False, description="Force regeneration even if cached")):
    """Generate a narrative story of the day using Gemini.

    POST only: generating calls Gemini, which costs money.
    """
    import mistune

    require_date(date)
    # Raw HTML in the digest (which quotes window titles) is escaped.
    render_markdown = mistune.create_markdown(escape=True)

    # Check for a cached report first. Only the Markdown is cached and it is
    # rendered (escaped) on every read: a *-digest.html written before raw
    # HTML was escaped could hold script, so it is never served.
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_md_path = REPORTS_DIR / f"{date}-digest.md"

    if not force and report_md_path.exists():
        return {"html": render_markdown(report_md_path.read_text()), "cached": True}

    # Get API key
    api_key = get_gemini_api_key()
    if not api_key:
        return {"error": "No Gemini API key configured", "html": None}

    # Get all captures for the day
    daily_file = DAILY_DIR / f"{date}.json"

    if not daily_file.exists():
        return {"error": "No data for this date", "html": None}

    try:
        daily_log = DailyLog.load(daily_file)

        if not daily_log.entries:
            return {"error": "No entries for this date", "html": None}

        # Generate the digest via the single canonical generator (shared with
        # the CLI daily summary). Sensitive entries are excluded inside it.
        from summarize import generate_digest

        activities_count = sum(1 for e in daily_log.entries if not e.sensitive)
        if activities_count == 0:
            return {"error": "No non-sensitive activities to summarize", "html": None}

        markdown_text = generate_digest(daily_log, date)

        # Save raw markdown
        report_md_path.write_text(markdown_text)

        # Convert to HTML using mistune
        html = render_markdown(markdown_text)

        return {"html": html, "cached": False, "activities_count": activities_count}

    except Exception as e:
        return {"error": str(e), "html": None}


@app.get("/export/coach")
async def export_coach(
    start: str = Query(description="Start date YYYY-MM-DD"),
    end: str = Query(default=None, description="End date YYYY-MM-DD (optional)")
):
    """Export data for AI coach in JSON format."""
    if end is None:
        end = start

    exports = []
    current = datetime.strptime(start, "%Y-%m-%d")
    end_date = datetime.strptime(end, "%Y-%m-%d")

    while current <= end_date:
        date_str = current.strftime("%Y-%m-%d")
        daily_file = DAILY_DIR / f"{date_str}.json"

        if daily_file.exists():
            try:
                daily_log = DailyLog.load(daily_file)
                export_day = {
                    "date": date_str,
                    "entries": [
                        {
                            "time": e.timestamp,
                            "activity": e.oneline,
                            "category": e.category,
                            "project": e.project
                        }
                        for e in daily_log.entries
                        if not e.sensitive  # Exclude sensitive entries
                    ]
                }
                exports.append(export_day)
            except Exception:
                pass

        current += timedelta(days=1)

    return JSONResponse(content={"days": exports})


@app.get("/export/invoice")
async def export_invoice(
    start: str = Query(description="Start date YYYY-MM-DD"),
    end: str = Query(description="End date YYYY-MM-DD"),
    project: str = Query(description="Project name")
):
    """Export time data for invoicing."""
    entries = []
    current = datetime.strptime(start, "%Y-%m-%d")
    end_date = datetime.strptime(end, "%Y-%m-%d")
    interval_minutes = 2  # Default capture interval

    while current <= end_date:
        date_str = current.strftime("%Y-%m-%d")
        daily_file = DAILY_DIR / f"{date_str}.json"

        if daily_file.exists():
            try:
                daily_log = DailyLog.load(daily_file)
                for entry in daily_log.entries:
                    if entry.project == project:
                        entries.append({
                            "date": date_str,
                            "time": entry.timestamp,
                            "activity": entry.oneline,
                            "duration_minutes": interval_minutes
                        })
            except Exception:
                pass

        current += timedelta(days=1)

    total_minutes = len(entries) * interval_minutes
    total_hours = total_minutes / 60

    return {
        "project": project,
        "period": {"start": start, "end": end},
        "total_hours": round(total_hours, 2),
        "total_captures": len(entries),
        "entries": entries
    }


def main():
    """Run the server."""
    import uvicorn

    # Ensure templates exist
    if not TEMPLATES_DIR.exists():
        print(f"Creating templates directory at {TEMPLATES_DIR}")
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

    # Check for timeline template
    timeline_template = TEMPLATES_DIR / "timeline.html"
    if not timeline_template.exists():
        print(f"Warning: {timeline_template} not found. Run with templates first.")

    print(f"Starting DayLogger Web UI...")
    print(f"Data directory: {DATA_DIR}")
    print(f"Open http://{HOST}:{PORT} in your browser")
    print()

    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
