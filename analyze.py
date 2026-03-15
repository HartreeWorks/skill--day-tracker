"""
Gemini Flash integration for screenshot analysis.

Analyzes screenshots and returns structured output with description,
category, and sensitivity detection.
"""

import os
import base64
from pathlib import Path
from typing import Optional, List

from config import CaptureConfig, load_known_projects, CATEGORIES
from models import ActiveWindow, Analysis

# Will be imported when needed to avoid startup cost
genai = None


def get_gemini_client():
    """Lazily initialize and return the Gemini client."""
    global genai
    if genai is None:
        import google.generativeai as _genai
        genai = _genai

        # Check for API key
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            # Try to load from keychain
            try:
                import subprocess
                result = subprocess.run(
                    ["security", "find-generic-password", "-s", "daylogger-gemini", "-w"],
                    capture_output=True,
                    text=True
                )
                if result.returncode == 0:
                    api_key = result.stdout.strip()
            except Exception:
                pass

        if not api_key:
            raise ValueError(
                "No Gemini API key found. Set GEMINI_API_KEY environment variable "
                "or store in keychain with: security add-generic-password -s daylogger-gemini -a gemini -w 'YOUR_API_KEY'"
            )

        genai.configure(api_key=api_key)

    return genai


def load_image_as_base64(image_path: Path) -> str:
    """Load an image file and return as base64 data URI."""
    ext = image_path.suffix.lower()
    mime_type = {"webp": "image/webp", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(ext.lstrip("."), "image/webp")
    with open(image_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime_type};base64,{data}"


def build_prompt(active_window: Optional[ActiveWindow], visible_apps: List[str], categories: List[str], num_screens: int = 1, user_name: str = "", session_context: Optional[list] = None, focus_history: Optional[list] = None) -> str:
    """Build the analysis prompt."""
    context_parts = []

    if active_window:
        context_parts.append(f"Active Application: {active_window.app}")
        if active_window.title:
            context_parts.append(f"Window Title: {active_window.title}")

    if visible_apps:
        context_parts.append(f"Visible Applications: {', '.join(visible_apps)}")

    context = "\n".join(context_parts) if context_parts else "No application context available."

    # Build agent session context section
    session_section = ""
    if session_context:
        session_lines = []
        for i, s in enumerate(session_context):
            agent = s.get("agent", "unknown").title()
            title = s.get("title", "untitled")
            project_path = s.get("project_path", "")
            line = f"  {i+1}. [{agent}] \"{title}\""
            if project_path:
                line += f" (path: {project_path})"
            session_lines.append(line)
        session_section = f"""
AGENT SESSION CONTEXT (the user is working in AI coding assistants):
Recently active sessions (ordered by recency):
{chr(10).join(session_lines)}
Use this context to inform your project attribution and activity description.
Session titles are reliable indicators of what the user is working on.
"""

    categories_str = ", ".join(categories)

    # Build category descriptions with work/personal classification
    work_categories = [k for k, v in CATEGORIES.items() if v.get("is_work") is True]
    personal_categories = [k for k, v in CATEGORIES.items() if v.get("is_work") is False]
    category_help = f"""
CATEGORY GUIDANCE:
- Work categories: {', '.join(work_categories)}
- Personal categories: {', '.join(personal_categories)}
- Use "other" only if none of the above fit"""

    # Build focus history section
    focus_section = ""
    if focus_history:
        focus_lines = []
        for fh in focus_history:
            title_part = f'"{fh["title"]}"' if fh.get("title") else "(no title)"
            focus_lines.append(f'- {title_part} ({fh["app"]}): {fh["pct"]}%')
        focus_section = f"""
FOCUS HISTORY (which windows had keyboard/mouse focus in the last 2 minutes):
{chr(10).join(focus_lines)}
Weight your description toward the windows that had more focus time.
"""

    # Multi-screen instruction
    screen_instruction = ""
    if num_screens > 1:
        focus_ref = ""
        if focus_history:
            focus_ref = "\nUse the FOCUS HISTORY below to understand which content the user was actively working with.\nScreens showing apps that had no recent focus are likely background/reference material."
        screen_instruction = f"""
IMPORTANT: You are viewing {num_screens} screenshots from different monitors.
Describe the activity on ALL screens, not just the one with the active window.
If a screen shows static content (desktop, unchanged app), note it briefly as "idle" or "background".{focus_ref}
"""

    # Load known projects for context
    known_projects = load_known_projects()
    projects_section = ""
    if known_projects:
        project_lines = [
            f'- "{p["folder"]}" ({p["name"]}) - {p["type"]}'
            for p in known_projects
        ]
        projects_section = f"""
KNOWN PROJECTS (use folder name for inferred_project if you can match the visible context):
{chr(10).join(project_lines)}
"""

    prompt = f"""Analyze this screenshot of a user's computer activity.
{screen_instruction}
APPLICATION CONTEXT:
{context}
{session_section}{focus_section}{category_help}
{projects_section}
Provide a JSON response with these fields:

1. "description": A detailed description of what the user is working on (2-3 sentences, max 200 words). Include:
   - Activity on EACH screen/monitor (if multiple screenshots provided)
   - The main application in use
   - The specific task or content visible
   - Any project names, file names, or identifiable context

2. "category": One of [{categories_str}]

3. "oneline": A brief 5-10 word summary suitable for a timeline view

4. "sensitive": Boolean - true if ANY screenshot contains:
   - API keys, tokens, or secrets
   - Passwords or credentials
   - Credit card or financial account numbers
   - Personal identification numbers

5. "sensitive_reason": If sensitive is true, briefly explain why (otherwise null)

6. "confidence": Your confidence in the analysis (0.0 to 1.0)

7. "urls": Array of URLs visible on screen (browser address bar, links, etc.). Extract the full URL if visible. Max 5 most relevant URLs. Empty array if none visible.

8. "file_paths": Array of file paths visible on screen (editor title bars, terminal, file managers). Include project/repo names if identifiable. Max 5 most relevant paths. Empty array if none visible.

9. "is_meeting": Boolean - true if the user appears to be in a video call or meeting (Zoom, Google Meet, Microsoft Teams, FaceTime, Discord call, etc.)

10. "meeting_app": If is_meeting is true, the name of the meeting application (e.g., "Zoom", "Google Meet", "Teams"). Null if not in a meeting.

11. "people": Array of people's names visible on screen (meeting attendees, document authors, chat participants, email senders). Exclude the user themselves{f" ({user_name})" if user_name else ""}. Max 5 names. Empty array if none visible.

12. "organizations": Array of organization/company names visible on screen (from Slack workspaces, email domains, document headers, website names). Examples: "80000 Hours", "Anthropic", "Google". Max 5 organizations. Empty array if none visible.

13. "is_work": Boolean - true if this appears to be work activity, false if personal (shopping, social media for non-work purposes, entertainment, personal finance, etc.)

14. "inferred_project": String or null - the folder name from KNOWN PROJECTS if you can confidently match this activity to a specific project based on visible content (repo names, URLs, document titles, Slack channels). Use null if no project match or if uncertain.

15. "project_confidence": Float 0-1 - your confidence in the project attribution (0 = no match, 1 = certain match)

Respond ONLY with valid JSON, no markdown or explanation."""

    return prompt


# Schema for structured output
ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": "Detailed description of user activity"
        },
        "category": {
            "type": "string",
            "description": "Activity category"
        },
        "oneline": {
            "type": "string",
            "description": "Brief 5-10 word summary"
        },
        "sensitive": {
            "type": "boolean",
            "description": "Whether screenshot contains sensitive content"
        },
        "sensitive_reason": {
            "type": "string",
            "nullable": True,
            "description": "Reason for sensitivity flag"
        },
        "confidence": {
            "type": "number",
            "description": "Confidence score 0-1"
        },
        "urls": {
            "type": "array",
            "items": {"type": "string"},
            "description": "URLs visible on screen"
        },
        "file_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "File paths visible on screen"
        },
        "is_meeting": {
            "type": "boolean",
            "description": "Whether user is in a video call/meeting"
        },
        "meeting_app": {
            "type": "string",
            "nullable": True,
            "description": "Name of meeting application if in meeting"
        },
        "people": {
            "type": "array",
            "items": {"type": "string"},
            "description": "People's names visible on screen"
        },
        "organizations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Organization names visible on screen"
        },
        "is_work": {
            "type": "boolean",
            "description": "Whether this is work (true) or personal (false) activity"
        },
        "inferred_project": {
            "type": "string",
            "nullable": True,
            "description": "Folder name of matched project from known projects list"
        },
        "project_confidence": {
            "type": "number",
            "description": "Confidence in project attribution (0-1)"
        }
    },
    "required": ["description", "category", "oneline", "sensitive", "confidence", "urls", "file_paths", "is_meeting", "people", "organizations", "is_work", "inferred_project", "project_confidence"]
}


def analyze_capture(
    capture_dir: Path,
    screenshots: List[str],
    active_window: Optional[ActiveWindow],
    visible_apps: List[str],
    config: CaptureConfig,
    session_context: Optional[list] = None,
    focus_history: Optional[list] = None
) -> Optional[Analysis]:
    """
    Analyze screenshots using Gemini Flash.

    Args:
        capture_dir: Directory containing screenshots
        screenshots: List of screenshot filenames
        active_window: Active window information
        visible_apps: List of visible applications
        config: Configuration
        session_context: Active agent sessions list (from get_active_agent_sessions)

    Returns:
        Analysis object with results, or None if analysis failed
    """
    try:
        client = get_gemini_client()
    except ValueError as e:
        print(f"Gemini not configured: {e}")
        return None

    # Build prompt (pass number of screens for multi-monitor awareness)
    prompt = build_prompt(active_window, visible_apps, config.categories, num_screens=len(screenshots), user_name=config.user_name, session_context=session_context, focus_history=focus_history)

    # Load images
    images = []
    for screenshot in screenshots:
        image_path = capture_dir / screenshot
        if image_path.exists():
            try:
                # Use PIL Image for Gemini
                from PIL import Image
                img = Image.open(image_path)
                images.append(img)
            except Exception as e:
                print(f"Failed to load image {screenshot}: {e}")

    if not images:
        print("No valid images to analyze")
        return None

    # Create model
    model = client.GenerativeModel(
        model_name=config.gemini_model,
        generation_config={
            "response_mime_type": "application/json",
            "temperature": 0.2
        }
    )

    # Build content (prompt + images)
    content = [prompt] + images

    try:
        # Generate response
        response = model.generate_content(content)

        # Parse JSON response
        import json
        result = json.loads(response.text)

        # Extract token usage from response metadata
        input_tokens = 0
        output_tokens = 0
        try:
            if hasattr(response, 'usage_metadata'):
                usage = response.usage_metadata
                input_tokens = getattr(usage, 'prompt_token_count', 0) or 0
                output_tokens = getattr(usage, 'candidates_token_count', 0) or 0
        except Exception:
            pass

        # Create Analysis object
        analysis = Analysis(
            description=result.get("description", ""),
            category=result.get("category", "other"),
            oneline=result.get("oneline", ""),
            sensitive=result.get("sensitive", False),
            sensitive_reason=result.get("sensitive_reason"),
            confidence=result.get("confidence", 0.5),
            urls=result.get("urls", []),
            file_paths=result.get("file_paths", []),
            is_meeting=result.get("is_meeting", False),
            meeting_app=result.get("meeting_app"),
            people=result.get("people", []),
            organizations=result.get("organizations", []),
            is_work=result.get("is_work", True),
            inferred_project=result.get("inferred_project"),
            project_confidence=result.get("project_confidence", 0.0),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=config.gemini_model
        )

        # Validate category
        if analysis.category not in config.categories:
            analysis.category = "other"

        return analysis

    except Exception as e:
        print(f"Gemini analysis failed: {e}")
        return None


def test_api_connection() -> bool:
    """Test if Gemini API is properly configured."""
    try:
        client = get_gemini_client()
        model = client.GenerativeModel(model_name="gemini-2.5-flash-lite")
        response = model.generate_content("Say 'DayLogger API test successful' in exactly those words.")
        return "successful" in response.text.lower()
    except Exception as e:
        print(f"API test failed: {e}")
        return False


if __name__ == "__main__":
    # Test API connection
    print("Testing Gemini API connection...")
    if test_api_connection():
        print("API connection successful!")
    else:
        print("API connection failed. Check your API key.")
