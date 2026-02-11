"""Configuration management for Day Tracker."""

import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# Model pricing per 1M tokens (USD) - Updated Jan 2026
# Source: https://ai.google.dev/gemini-api/docs/pricing
MODEL_PRICING = {
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00},
    # Fallback for unknown models
    "default": {"input": 0.50, "output": 3.00},
}

# Default paths - data stored in Documents for easy access
SKILL_DIR = Path(__file__).parent
DATA_DIR = Path.home() / "Documents" / "day-tracker" / "data"
CAPTURES_DIR = DATA_DIR / "captures"
DAILY_DIR = DATA_DIR / "daily"
REPORTS_DIR = DATA_DIR / "reports"
CONFIG_FILE = DATA_DIR / "config.json"
PROJECTS_FILE = DATA_DIR / "projects.json"
REFERENCE_WALLPAPERS_DIR = DATA_DIR / "reference-wallpapers"

# External projects.yaml for project inference
PROJECTS_YAML = Path.home() / "Documents" / "Projects" / "projects.yaml"

# Category definitions with work/personal classification
CATEGORIES = {
    # Work categories
    "coding": {"label": "Coding", "is_work": True},
    "writing": {"label": "Writing", "is_work": True},
    "research": {"label": "Research", "is_work": True},
    "meetings": {"label": "Meetings", "is_work": True},
    "communication": {"label": "Work communication", "is_work": True},
    "admin": {"label": "Admin", "is_work": True},
    "design": {"label": "Design", "is_work": True},

    # Personal categories
    "personal_admin": {"label": "Personal admin", "is_work": False},
    "social": {"label": "Social/messaging", "is_work": False},
    "entertainment": {"label": "Entertainment", "is_work": False},
    "break": {"label": "Break/idle", "is_work": False},

    # Fallback
    "other": {"label": "Unclassified", "is_work": None}
}

# Default configuration
DEFAULT_CONFIG = {
    "capture_interval_minutes": 2,
    "gemini_model": "gemini-2.5-flash-lite",  # Options: gemini-2.5-flash-lite, gemini-2.5-flash, gemini-3-flash-preview
    "jpeg_quality": 70,
    "thumbnail_size": [400, 225],  # 16:9 aspect
    "skip_similar_threshold": 0.02,  # Skip capture if <2% pixels changed (0 to disable)
    "categories": list(CATEGORIES.keys()),
    "sensitive_window_patterns": [
        "1Password",
        "Keychain",
        ".env",
        "secrets",
        "credentials",
        "password",
        "API Key",
        "Bearer "
    ],
    "project_patterns": [],
    "auto_delete_sensitive": False,
    "pause_until": None,
    "user_name": "",  # Your name — excluded from "people" extraction
    "blank_desktop_threshold": 0.05,  # Skip screen if <5% different from reference wallpaper (0 to disable)
    "blank_desktop_crop_top": 50  # Pixels to crop from top before comparison (removes menu bar + notch)
}


@dataclass
class CaptureConfig:
    """Runtime configuration for capture."""
    capture_interval_minutes: int = 2
    gemini_model: str = "gemini-2.5-flash-lite"
    jpeg_quality: int = 50
    thumbnail_size: tuple = (400, 225)
    skip_similar_threshold: float = 0.02  # Skip if <2% pixels changed (0 to disable)
    categories: list = field(default_factory=lambda: list(CATEGORIES.keys()))
    sensitive_window_patterns: list = field(default_factory=lambda: DEFAULT_CONFIG["sensitive_window_patterns"].copy())
    project_patterns: list = field(default_factory=list)
    auto_delete_sensitive: bool = False
    pause_until: Optional[str] = None
    user_name: str = ""  # Your name — excluded from "people" extraction
    blank_desktop_threshold: float = 0.05  # Skip screen if <5% different from reference wallpaper
    blank_desktop_crop_top: int = 50  # Pixels to crop from top before comparison


def ensure_directories():
    """Create necessary directories if they don't exist."""
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> CaptureConfig:
    """Load configuration from file or return defaults."""
    ensure_directories()

    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                data = json.load(f)
            return CaptureConfig(**{k: v for k, v in data.items() if k in CaptureConfig.__dataclass_fields__})
        except Exception as e:
            print(f"Warning: Could not load config: {e}")

    return CaptureConfig()


def save_config(config: CaptureConfig):
    """Save configuration to file."""
    ensure_directories()

    data = {
        "capture_interval_minutes": config.capture_interval_minutes,
        "gemini_model": config.gemini_model,
        "jpeg_quality": config.jpeg_quality,
        "thumbnail_size": list(config.thumbnail_size),
        "skip_similar_threshold": config.skip_similar_threshold,
        "categories": config.categories,
        "sensitive_window_patterns": config.sensitive_window_patterns,
        "project_patterns": config.project_patterns,
        "auto_delete_sensitive": config.auto_delete_sensitive,
        "pause_until": config.pause_until,
        "user_name": config.user_name,
        "blank_desktop_threshold": config.blank_desktop_threshold,
        "blank_desktop_crop_top": config.blank_desktop_crop_top
    }

    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_projects() -> dict:
    """Load project definitions."""
    if PROJECTS_FILE.exists():
        try:
            with open(PROJECTS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"projects": {}}


def save_projects(projects: dict):
    """Save project definitions."""
    ensure_directories()
    with open(PROJECTS_FILE, "w") as f:
        json.dump(projects, f, indent=2)


def load_known_projects() -> list:
    """Load projects from projects.yaml for AI context.

    Returns a list of dicts with folder, name, and type.
    """
    if PROJECTS_YAML.exists():
        try:
            import yaml
            with open(PROJECTS_YAML) as f:
                data = yaml.safe_load(f)
            return [
                {"folder": p["folder"], "name": p["name"], "type": p.get("type", "other")}
                for p in data.get("projects", [])
                if p.get("status") == "active"  # Only include active projects
            ]
        except Exception:
            pass
    return []


def get_category_is_work(category: str) -> Optional[bool]:
    """Get the is_work classification for a category.

    Returns True for work, False for personal, None for unclassified.
    """
    cat_info = CATEGORIES.get(category, CATEGORIES.get("other"))
    return cat_info.get("is_work") if cat_info else None
