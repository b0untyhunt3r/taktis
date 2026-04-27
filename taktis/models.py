from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, fields, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Status & type enums (str-based so comparisons with raw DB strings work)
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    """Canonical task statuses."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    AWAITING_INPUT = "awaiting_input"
    PAUSED = "paused"


class PhaseStatus(str, Enum):
    """Canonical phase statuses."""
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"



class TaskType(str, Enum):
    """Well-known task types checked in scheduling/pipeline logic."""
    PLANNER = "planner"
    INTERVIEWER = "interviewer"
    RESEARCHER_STACK = "researcher_stack"
    RESEARCHER_FEATURES = "researcher_features"
    RESEARCHER_ARCHITECTURE = "researcher_architecture"
    RESEARCHER_PITFALLS = "researcher_pitfalls"
    SYNTHESIZER = "synthesizer"
    ROADMAPPER = "roadmapper"
    PLAN_CHECKER = "plan_checker"
    DISCUSS = "discuss_task"
    RESEARCH = "task_researcher"
    PHASE_REVIEW = "phase_review"
    PHASE_REVIEW_FIX = "phase_review_fix"


# ---------------------------------------------------------------------------
# Derived constant sets (used by scheduler, recovery, templates)
# ---------------------------------------------------------------------------

#: Terminal statuses — a task in one of these will not be waited on further.
TERMINAL_STATUSES: frozenset[str] = frozenset({
    TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED,
})

#: Statuses that count as "done enough" when waiting for a wave.
DONE_STATUSES: frozenset[str] = frozenset({
    TaskStatus.COMPLETED, TaskStatus.FAILED,
    TaskStatus.CANCELLED, TaskStatus.AWAITING_INPUT,
})

#: Task types that are preparatory/ancillary and skipped on phase re-run.
SKIP_TASK_TYPES: frozenset[str] = frozenset({
    TaskType.DISCUSS, TaskType.RESEARCH,
    TaskType.PHASE_REVIEW, TaskType.PHASE_REVIEW_FIX,
})


def _full_uuid() -> str:
    return uuid.uuid4().hex


def _short_uuid() -> str:
    return uuid.uuid4().hex[:8]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# JSON-serialised field names per model (stored as TEXT in SQLite).
_JSON_FIELDS: dict[str, set[str]] = {
    "Project": {"default_env_vars"},
    "ProjectState": {"decisions", "blockers", "metrics"},
    "Phase": {"success_criteria"},
    "Task": {"depends_on", "env_vars"},
    "TaskOutput": {"content"},
    "TaskTemplate": {"env_vars"},
}


class Base:
    """Empty base retained for import compatibility."""


def _parse_datetime(val) -> Optional[datetime]:
    """Parse a datetime value that may be a string (from SQLite) or already a datetime."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    # SQLite stores as ISO-8601 text
    val = str(val)
    try:
        return datetime.fromisoformat(val)
    except (ValueError, TypeError):
        return None


def _model_from_row(cls, row):
    """Construct a dataclass instance from an aiosqlite.Row or dict."""
    if row is None:
        return None
    data = dict(row) if not isinstance(row, dict) else row
    json_fields = _JSON_FIELDS.get(cls.__name__, set())
    kwargs = {}
    dc_field_names = {f.name for f in fields(cls)}
    for key, val in data.items():
        if key not in dc_field_names:
            continue
        if key in json_fields and isinstance(val, str):
            try:
                val = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass
        # Detect datetime fields by annotation
        ft = cls.__dataclass_fields__[key].type
        if ft in ("datetime", "Optional[datetime]") and not isinstance(val, datetime):
            val = _parse_datetime(val)
        kwargs[key] = val
    return cls(**kwargs)


def _model_to_dict(self) -> dict:
    """Serialise a dataclass to a plain dict, converting datetimes to ISO strings
    and JSON-typed fields to JSON strings."""
    json_fields = _JSON_FIELDS.get(type(self).__name__, set())
    result = {}
    for f in fields(self):
        val = getattr(self, f.name)
        if isinstance(val, datetime):
            val = val.isoformat()
        elif f.name in json_fields and val is not None:
            val = json.dumps(val)
        result[f.name] = val
    return result


# ---------------------------------------------------------------------------
# Expert
# ---------------------------------------------------------------------------

@dataclass
class Expert(Base):
    id: str = field(default_factory=_full_uuid)
    name: str = ""
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    category: Optional[str] = None
    is_builtin: bool = False
    created_at: datetime = field(default_factory=_utcnow)

    to_dict = _model_to_dict

    @classmethod
    def from_row(cls, row) -> Optional[Expert]:
        return _model_from_row(cls, row)

    def __repr__(self) -> str:
        return f"<Expert {self.name!r}>"


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------

@dataclass
class Project(Base):
    id: str = field(default_factory=_full_uuid)
    name: str = ""
    description: Optional[str] = None
    working_dir: Optional[str] = None
    default_model: Optional[str] = None
    default_permission_mode: Optional[str] = None
    default_env_vars: Optional[dict] = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: Optional[datetime] = field(default_factory=_utcnow)

    to_dict = _model_to_dict

    @classmethod
    def from_row(cls, row) -> Optional[Project]:
        return _model_from_row(cls, row)

    def __repr__(self) -> str:
        return f"<Project {self.name!r}>"


# ---------------------------------------------------------------------------
# ProjectState (1:1 with Project)
# ---------------------------------------------------------------------------

@dataclass
class ProjectState(Base):
    id: str = field(default_factory=_full_uuid)
    project_id: str = ""
    current_phase_id: Optional[str] = None
    status: str = "idle"
    decisions: Optional[list] = field(default_factory=list)
    blockers: Optional[list] = field(default_factory=list)
    metrics: Optional[dict] = field(default_factory=dict)
    last_session_at: Optional[datetime] = None
    last_session_description: Optional[str] = None

    to_dict = _model_to_dict

    @classmethod
    def from_row(cls, row) -> Optional[ProjectState]:
        return _model_from_row(cls, row)

    def __repr__(self) -> str:
        return f"<ProjectState project_id={self.project_id!r} status={self.status!r}>"


# ---------------------------------------------------------------------------
# Phase
# ---------------------------------------------------------------------------

@dataclass
class Phase(Base):
    id: str = field(default_factory=_full_uuid)
    project_id: str = ""
    name: str = ""
    description: Optional[str] = None
    goal: Optional[str] = None
    success_criteria: Optional[list] = field(default_factory=list)
    phase_number: int = 0
    status: str = "not_started"
    depends_on_phase_id: Optional[str] = None
    current_wave: Optional[int] = None
    context_config: Optional[str] = None  # JSON: {"designer_phase": true, "context_files": [...]}
    created_at: datetime = field(default_factory=_utcnow)
    completed_at: Optional[datetime] = None

    to_dict = _model_to_dict

    @classmethod
    def from_row(cls, row) -> Optional[Phase]:
        return _model_from_row(cls, row)

    def __repr__(self) -> str:
        return f"<Phase {self.name!r} #{self.phase_number}>"


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------

@dataclass
class Task(Base):
    id: str = field(default_factory=_short_uuid)
    phase_id: Optional[str] = None
    project_id: str = ""
    name: str = ""
    prompt: Optional[str] = None
    status: str = "pending"
    wave: int = 1
    depends_on: Optional[list] = field(default_factory=list)
    model: Optional[str] = None
    permission_mode: Optional[str] = None
    env_vars: Optional[dict] = None
    system_prompt: Optional[str] = None
    expert_id: Optional[str] = None
    interactive: bool = False
    checkpoint_type: Optional[str] = None
    session_id: Optional[str] = None
    pid: Optional[int] = None
    cost_usd: float = 0.0
    result_summary: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=_utcnow)
    context_manifest: Optional[str] = None

    to_dict = _model_to_dict

    @classmethod
    def from_row(cls, row) -> Optional[Task]:
        return _model_from_row(cls, row)

    def __repr__(self) -> str:
        return f"<Task {self.id} {self.name!r} [{self.status}]>"


# ---------------------------------------------------------------------------
# TaskOutput
# ---------------------------------------------------------------------------

@dataclass
class TaskOutput(Base):
    id: Optional[int] = None  # autoincrement in DB
    task_id: str = ""
    timestamp: datetime = field(default_factory=_utcnow)
    event_type: str = ""
    content: Optional[dict] = None

    to_dict = _model_to_dict

    @classmethod
    def from_row(cls, row) -> Optional[TaskOutput]:
        return _model_from_row(cls, row)

    def __repr__(self) -> str:
        return f"<TaskOutput {self.id} [{self.event_type}]>"


# ---------------------------------------------------------------------------
# TaskTemplate
# ---------------------------------------------------------------------------

@dataclass
class TaskTemplate(Base):
    id: str = field(default_factory=_full_uuid)
    project_id: Optional[str] = None
    name: str = ""
    description: Optional[str] = None
    prompt: Optional[str] = None
    model: Optional[str] = None
    expert_id: Optional[str] = None
    interactive: bool = False
    env_vars: Optional[dict] = None

    to_dict = _model_to_dict

    @classmethod
    def from_row(cls, row) -> Optional[TaskTemplate]:
        return _model_from_row(cls, row)

    def __repr__(self) -> str:
        return f"<TaskTemplate {self.name!r}>"


# ---------------------------------------------------------------------------
# PipelineTemplate
# ---------------------------------------------------------------------------

@dataclass
class PipelineTemplate(Base):
    id: str = field(default_factory=_full_uuid)
    name: str = ""
    description: Optional[str] = None
    flow_json: str = "{}"       # Drawflow export JSON (full graph)
    is_default: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    to_dict = _model_to_dict

    @classmethod
    def from_row(cls, row) -> Optional[PipelineTemplate]:
        return _model_from_row(cls, row)

    def __repr__(self) -> str:
        return f"<PipelineTemplate {self.name!r}>"


