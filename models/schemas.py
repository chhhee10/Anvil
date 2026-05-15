"""
Pydantic models for all agent state, pipeline runs, and inter-agent messages.
"""
from __future__ import annotations
from enum import Enum
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field
import uuid


# ─── Enums ────────────────────────────────────────────────────────────────────

class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class AgentName(str, Enum):
    ORCHESTRATOR = "orchestrator"
    RESEARCHER = "researcher"
    CODE_ANALYST = "code_analyst"
    WRITER = "writer"
    CRITIC = "critic"
    SYSTEM = "system"

class StepStatus(str, Enum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"

class EventType(str, Enum):
    PUSH = "push"
    PULL_REQUEST = "pull_request"
    ISSUES = "issues"
    MANUAL = "manual"
    SCHEDULED = "scheduled"


# ─── Webhook / Trigger Payloads ────────────────────────────────────────────────

class GitHubPushPayload(BaseModel):
    ref: str
    repository: dict
    commits: List[dict] = []
    pusher: dict = {}

class GitHubPRPayload(BaseModel):
    action: str
    pull_request: dict
    repository: dict

class ManualTriggerRequest(BaseModel):
    topic: str = Field(..., description="Topic or question to research")
    repo: Optional[str] = Field(None, description="Optional GitHub repo (owner/name)")
    context: Optional[str] = Field(None, description="Additional context")


# ─── Research Plan ─────────────────────────────────────────────────────────────

class ResearchTask(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    query: str
    priority: int = Field(1, ge=1, le=3)
    agent: AgentName

class ResearchPlan(BaseModel):
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    event_type: EventType
    main_topic: str
    summary: str
    research_tasks: List[ResearchTask]
    code_tasks: List[ResearchTask]
    repo: Optional[str] = None
    pr_number: Optional[int] = None
    commit_sha: Optional[str] = None


# ─── Agent Outputs ─────────────────────────────────────────────────────────────

class SearchResult(BaseModel):
    query: str
    url: str
    title: str
    content: str
    score: float = 0.0

class ResearchFindings(BaseModel):
    topic: str
    searches_performed: int = 0
    key_findings: List[str] = []
    security_advisories: List[str] = []
    ecosystem_trends: List[str] = []
    related_projects: List[str] = []
    raw_results: List[SearchResult] = []
    synthesis: str = ""

class CodeChange(BaseModel):
    filename: str
    status: str  # added, modified, removed
    additions: int = 0
    deletions: int = 0
    patch: Optional[str] = None

class CodeAnalysis(BaseModel):
    repo: Optional[str] = None
    commit_sha: Optional[str] = None
    files_changed: List[CodeChange] = []
    breaking_changes: List[str] = []
    security_issues: List[str] = []
    performance_notes: List[str] = []
    summary: str = ""

class CriticScore(BaseModel):
    accuracy: int = Field(..., ge=1, le=10)
    completeness: int = Field(..., ge=1, le=10)
    actionability: int = Field(..., ge=1, le=10)
    overall: float = 0.0
    approved: bool = False
    revision_requests: List[str] = []
    feedback: str = ""

class FinalReport(BaseModel):
    run_id: str
    topic: str
    event_type: EventType
    report_markdown: str
    critic_score: Optional[CriticScore] = None
    revision_count: int = 0
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    report_path: Optional[str] = None


# ─── Pipeline Run State ────────────────────────────────────────────────────────

class AgentStep(BaseModel):
    step_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    run_id: str
    agent: AgentName
    status: StepStatus
    message: str = ""
    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    metadata: dict = {}

class PipelineRun(BaseModel):
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    status: RunStatus = RunStatus.PENDING
    event_type: EventType
    topic: str
    repo: Optional[str] = None
    trigger_payload: dict = {}
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    steps: List[AgentStep] = []
    final_report: Optional[FinalReport] = None
    error: Optional[str] = None


# ─── API Response Models ───────────────────────────────────────────────────────

class TriggerResponse(BaseModel):
    run_id: str
    status: str
    message: str
    stream_url: str

class StatusResponse(BaseModel):
    run_id: str
    status: RunStatus
    topic: str
    event_type: EventType
    repo: Optional[str]
    created_at: datetime
    updated_at: datetime
    steps: List[AgentStep]
    has_report: bool
    error: Optional[str]
