"""
QualityEngine AI — Pydantic Data Models
All structured outputs for the 7-agent PR quality pipeline.
"""
from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field
import uuid


# ─── Enums ────────────────────────────────────────────────────────────────────

class RunStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"

class StepStatus(str, Enum):
    STARTED   = "started"
    COMPLETED = "completed"
    FAILED    = "failed"
    SKIPPED   = "skipped"

class AgentName(str, Enum):
    ORCHESTRATOR     = "orchestrator"
    RESEARCHER       = "researcher"
    PR_REVIEWER      = "pr_reviewer"
    SECURITY_SCANNER = "security_scanner"
    TEST_GENERATOR   = "test_generator"
    SELF_HEALER      = "self_healer"
    DECISION_AGENT   = "decision_agent"
    REPORT_WRITER    = "report_writer"
    SYSTEM           = "system"

class ChangeType(str, Enum):
    FEATURE    = "feature"
    BUG_FIX    = "bug_fix"
    REFACTOR   = "refactor"
    CONFIG     = "config"
    DEPENDENCY = "dependency"
    DOCS       = "docs"
    UNKNOWN    = "unknown"

class RiskLevel(str, Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"

class Verdict(str, Enum):
    MERGE          = "MERGE"
    REJECT         = "REJECT"
    MERGE_WITH_FIX = "MERGE_WITH_FIX"
    BUG_REPORT     = "BUG_REPORT"

class IssueSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"

class EventType(str, Enum):
    PULL_REQUEST = "pull_request"
    PUSH         = "push"
    MANUAL       = "manual"


# ─── Agent Step (shared across all agents) ────────────────────────────────────

class AgentStep(BaseModel):
    step_id:      str      = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id:       str
    agent:        AgentName
    status:       StepStatus
    message:      str
    started_at:   datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    metadata:     dict     = Field(default_factory=dict)


# ─── Orchestrator Output ──────────────────────────────────────────────────────

class QualityPlan(BaseModel):
    """Orchestrator's structured plan for the quality pipeline."""
    change_type:     ChangeType
    risk_level:      RiskLevel
    summary:         str              # What this PR does in plain English
    files_to_test:   List[str]        # File paths that need test generation
    review_focus:    List[str]        # Specific areas for PR Reviewer to focus on
    security_focus:  List[str]        # Specific security aspects to check
    test_functions:  List[str]        # Function signatures to generate tests for
    skip_reason:     Optional[str] = None  # If pipeline should be skipped (e.g. docs only)
    research_plan:   Optional[ResearchPlan] = None


# ─── Researcher Output ───────────────────────────────────────────────────────

class SearchResult(BaseModel):
    url: str
    title: str
    content: str

class ResearchTask(BaseModel):
    query: str
    purpose: str

class ResearchPlan(BaseModel):
    """Orchestrator's plan for web research."""
    main_topic: str
    summary: str
    research_tasks: List[ResearchTask]

class ResearchFindings(BaseModel):
    """Output from the Researcher Agent via Tavily search."""
    topic: str
    searches_performed: int
    key_findings: List[str] = Field(default_factory=list)
    security_advisories: List[str] = Field(default_factory=list)
    ecosystem_trends: List[str] = Field(default_factory=list)
    related_projects: List[str] = Field(default_factory=list)
    raw_results: List[SearchResult] = Field(default_factory=list)
    synthesis: str


# ─── PR Reviewer Output ───────────────────────────────────────────────────────

class CodeIssue(BaseModel):
    file:        str
    line:        Optional[int] = None
    issue_type:  str           # bug / security / performance / style / logic
    severity:    IssueSeverity
    description: str
    suggestion:  str

class ReviewReport(BaseModel):
    """Structured output from PR Reviewer agent."""
    score:          int           # 1-10 overall code quality score
    issues:         List[CodeIssue]
    strengths:      List[str]     # What the PR does well
    summary:        str           # 2-3 sentence review summary
    recommendation: str           # APPROVE / REQUEST_CHANGES / REJECT


# ─── Security Scanner Output ──────────────────────────────────────────────────

class SecurityFinding(BaseModel):
    finding_type: str            # hardcoded_secret / sql_injection / xss / etc.
    severity:     IssueSeverity
    file:         str
    description:  str
    line_hint:    Optional[str] = None

class SecurityReport(BaseModel):
    """Structured output from Security Scanner agent."""
    score:          int                    # 1-10 (10 = perfectly secure)
    findings:       List[SecurityFinding]
    critical_count: int
    high_count:     int
    summary:        str
    recommendation: str                    # PASS / REVIEW / BLOCK


# ─── Test Generator Output ───────────────────────────────────────────────────

class TestResult(BaseModel):
    """Result of generating + running tests in sandbox."""
    test_code:  str    # The generated pytest code
    passed:     int
    failed:     int
    errors:     int
    stdout:     str
    stderr:     str
    success:    bool   # True if passed > 0 and failed == 0 and errors == 0
    timed_out:  bool = False


# ─── Self-Healer Output ───────────────────────────────────────────────────────

class HealAttempt(BaseModel):
    """One iteration of the self-healing loop."""
    attempt:         int        # 1, 2, or 3
    fix_description: str        # What the healer changed
    fixed_code:      str        # The patched code
    test_result:     TestResult # Result after re-running tests


# ─── Decision Agent Output ───────────────────────────────────────────────────

class ScoreBreakdown(BaseModel):
    correctness:   int    # 1-10
    security:      int    # 1-10
    test_coverage: int    # 1-10
    code_quality:  int    # 1-10
    risk:          int    # 1-10 (10 = lowest risk)
    overall:       float  # weighted average

class PRDecision(BaseModel):
    """Final verdict from Decision Agent."""
    verdict:          Verdict
    scores:           ScoreBreakdown
    reasoning:        str
    merge_message:    Optional[str] = None   # Posted when merging
    rejection_reason: Optional[str] = None   # Posted when rejecting
    bug_title:        Optional[str] = None   # GitHub Issue title if BUG_REPORT
    bug_body:         Optional[str] = None   # GitHub Issue body if BUG_REPORT


# ─── Pipeline Run (top-level state) ──────────────────────────────────────────

class PipelineRun(BaseModel):
    run_id:       str      = Field(default_factory=lambda: str(uuid.uuid4()))
    repo:         str
    pr_number:    Optional[int]   = None
    pr_title:     Optional[str]   = None
    pr_author:    Optional[str]   = None
    branch:       Optional[str]   = None
    commit_sha:   Optional[str]   = None
    diff_text:    Optional[str]   = None   # raw unified diff
    event_type:   EventType       = EventType.PULL_REQUEST
    status:       RunStatus       = RunStatus.PENDING
    created_at:   datetime        = Field(default_factory=datetime.utcnow)
    updated_at:   datetime        = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    # Agent outputs
    quality_plan:      Optional[QualityPlan]      = None
    research_plan:     Optional[ResearchPlan]     = None
    research_findings: Optional[ResearchFindings] = None
    review_report:     Optional[ReviewReport]     = None
    security_report:   Optional[SecurityReport]   = None
    test_result:     Optional[TestResult]     = None
    heal_attempts:   List[HealAttempt]        = Field(default_factory=list)
    decision:        Optional[PRDecision]     = None
    # GitHub result
    github_comment_url: Optional[str] = None
    github_issue_url:   Optional[str] = None
    # Pipeline metadata
    steps: List[AgentStep] = Field(default_factory=list)
    error: Optional[str]   = None
    topic: str             = ""   # human readable label for dashboard
    metadata: dict         = Field(default_factory=dict)


# ─── API Request / Response Models ───────────────────────────────────────────

class ManualTriggerRequest(BaseModel):
    repo:      str
    pr_number: int
    topic:     Optional[str] = None

class TriggerResponse(BaseModel):
    run_id:     str
    status:     str
    message:    str
    stream_url: str

class WebhookResponse(BaseModel):
    run_id:     str
    status:     str
    stream_url: str
