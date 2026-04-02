"""Data models for DayLogger."""

from dataclasses import dataclass, field, asdict
from typing import Optional, List
from datetime import datetime
import json


@dataclass
class ActiveWindow:
    """Information about the active window."""
    app: str
    title: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Analysis:
    """AI analysis of a capture."""
    description: str
    category: str
    oneline: str
    sensitive: bool = False
    sensitive_reason: Optional[str] = None
    confidence: float = 0.95
    # Enhanced context extraction
    urls: List[str] = field(default_factory=list)  # URLs visible on screen
    file_paths: List[str] = field(default_factory=list)  # File paths visible
    is_meeting: bool = False  # Currently in a video call/meeting
    meeting_app: Optional[str] = None  # Zoom, Meet, Teams, etc.
    people: List[str] = field(default_factory=list)  # People names visible (collaborators, meeting attendees)
    organizations: List[str] = field(default_factory=list)  # Organizations visible (clients, companies)
    # Work/personal classification
    is_work: bool = True  # True = work, False = personal
    # Project inference from visible context
    inferred_project: Optional[str] = None  # Folder name from projects.yaml
    project_confidence: float = 0.0  # Confidence in project attribution (0-1)
    # Token usage and model for cost tracking
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = "gemini-2.5-flash-lite"  # Model used for analysis

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def estimated_cost_usd(self) -> float:
        """Estimate cost in USD based on model-specific pricing."""
        from config import MODEL_PRICING
        pricing = MODEL_PRICING.get(self.model, MODEL_PRICING["default"])
        input_cost = (self.input_tokens / 1_000_000) * pricing["input"]
        output_cost = (self.output_tokens / 1_000_000) * pricing["output"]
        return input_cost + output_cost


@dataclass
class CaptureMetadata:
    """Metadata for a single capture."""
    timestamp: str
    screens: List[str]  # Active screens sent to AI analysis
    active_window: Optional[ActiveWindow]
    visible_apps: List[str]
    analysis: Optional[Analysis] = None
    auto_project: Optional[str] = None
    manual_project: Optional[str] = None
    excluded_blank_screens: List[str] = field(default_factory=list)  # Screens excluded (wallpaper only)
    active_sessions: Optional[List[dict]] = None  # Active agent sessions
    focus_history: Optional[List[dict]] = None  # Focus history summary [{app, title, pct}]
    modified_files: Optional[List[str]] = None  # Files modified in the last few minutes

    def to_dict(self) -> dict:
        d = {
            "timestamp": self.timestamp,
            "screens": self.screens,
            "active_window": self.active_window.to_dict() if self.active_window else None,
            "visible_apps": self.visible_apps,
            "analysis": self.analysis.to_dict() if self.analysis else None,
            "auto_project": self.auto_project,
            "manual_project": self.manual_project,
            "excluded_blank_screens": self.excluded_blank_screens
        }
        if self.active_sessions:
            d["active_sessions"] = self.active_sessions
        if self.focus_history:
            d["focus_history"] = self.focus_history
        if self.modified_files:
            d["modified_files"] = self.modified_files
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CaptureMetadata":
        return cls(
            timestamp=d["timestamp"],
            screens=d["screens"],
            active_window=ActiveWindow(**d["active_window"]) if d.get("active_window") else None,
            visible_apps=d.get("visible_apps", []),
            analysis=Analysis(**d["analysis"]) if d.get("analysis") else None,
            auto_project=d.get("auto_project"),
            manual_project=d.get("manual_project"),
            excluded_blank_screens=d.get("excluded_blank_screens", []),
            active_sessions=d.get("active_sessions"),
            focus_history=d.get("focus_history"),
            modified_files=d.get("modified_files")
        )

    @property
    def project(self) -> Optional[str]:
        """Return the effective project (manual overrides auto)."""
        return self.manual_project or self.auto_project

    def save(self, path):
        """Save metadata to a JSON file."""
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, path) -> "CaptureMetadata":
        """Load metadata from a JSON file."""
        with open(path) as f:
            return cls.from_dict(json.load(f))


@dataclass
class DailyEntry:
    """A single entry in the daily log."""
    timestamp: str
    capture_dir: str
    oneline: str
    category: str
    project: Optional[str] = None
    sensitive: bool = False
    is_work: bool = True  # Work vs personal classification
    inferred_project: Optional[str] = None  # AI-inferred project from visible context
    active_app: Optional[str] = None  # App name at capture time
    window_title: Optional[str] = None  # Window title at capture time
    active_sessions: Optional[List[dict]] = None  # Active agent sessions [{agent, title, project_path}]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DailyLog:
    """Aggregated daily log."""
    date: str
    entries: List[DailyEntry] = field(default_factory=list)
    summary: Optional[dict] = None  # Enhanced daily summary for Chief of Staff

    def to_dict(self) -> dict:
        result = {
            "date": self.date,
            "entries": [e.to_dict() for e in self.entries]
        }
        if self.summary:
            result["summary"] = self.summary
        return result

    @classmethod
    def from_dict(cls, d: dict) -> "DailyLog":
        known_fields = DailyEntry.__dataclass_fields__
        entries = [
            DailyEntry(**{k: v for k, v in e.items() if k in known_fields})
            for e in d.get("entries", [])
        ]
        return cls(
            date=d["date"],
            entries=entries,
            summary=d.get("summary")
        )

    def save(self, path):
        """Save daily log to JSON."""
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, path) -> "DailyLog":
        """Load daily log from JSON."""
        with open(path) as f:
            return cls.from_dict(json.load(f))

    def add_entry(self, metadata: CaptureMetadata, capture_dir: str):
        """Add a capture to the daily log."""
        if not metadata.analysis:
            return

        entry = DailyEntry(
            timestamp=metadata.timestamp,
            capture_dir=capture_dir,
            oneline=metadata.analysis.oneline,
            category=metadata.analysis.category,
            project=metadata.project,
            sensitive=metadata.analysis.sensitive,
            is_work=metadata.analysis.is_work,
            inferred_project=metadata.analysis.inferred_project,
            active_app=metadata.active_window.app if metadata.active_window else None,
            window_title=metadata.active_window.title if metadata.active_window else None,
            active_sessions=metadata.active_sessions
        )
        self.entries.append(entry)
