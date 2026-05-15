"""
SQLite-backed state management for pipeline runs and agent steps.
Uses aiosqlite for fully async access.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime
from typing import Optional, List
import aiosqlite
from models.schemas import (
    PipelineRun, AgentStep, FinalReport, RunStatus,
    AgentName, StepStatus, EventType, CriticScore
)

logger = logging.getLogger("newsroom.db")

DB_PATH = "newsroom.db"


async def init_db():
    """Create tables if they don't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                event_type TEXT NOT NULL,
                topic TEXT NOT NULL,
                repo TEXT,
                trigger_payload TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                error TEXT,
                final_report TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS agent_steps (
                step_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                agent TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT DEFAULT '',
                started_at TEXT NOT NULL,
                completed_at TEXT,
                metadata TEXT DEFAULT '{}',
                FOREIGN KEY (run_id) REFERENCES pipeline_runs(run_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_steps_run ON agent_steps(run_id)")
        await db.commit()
    logger.info("Database initialized at %s", DB_PATH)


async def create_run(run: PipelineRun) -> PipelineRun:
    """Persist a new pipeline run."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO pipeline_runs
              (run_id, status, event_type, topic, repo, trigger_payload, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run.run_id,
            run.status.value,
            run.event_type.value,
            run.topic,
            run.repo,
            json.dumps(run.trigger_payload),
            run.created_at.isoformat(),
            run.updated_at.isoformat(),
        ))
        await db.commit()
    return run


async def update_run_status(run_id: str, status: RunStatus, error: str = None):
    """Update pipeline run status."""
    now = datetime.utcnow().isoformat()
    completed_at = now if status in (RunStatus.COMPLETED, RunStatus.FAILED) else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE pipeline_runs
            SET status = ?, updated_at = ?, completed_at = ?, error = ?
            WHERE run_id = ?
        """, (status.value, now, completed_at, error, run_id))
        await db.commit()


async def save_final_report(run_id: str, report: FinalReport):
    """Save the final report JSON to the run row."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE pipeline_runs
            SET final_report = ?, updated_at = ?
            WHERE run_id = ?
        """, (report.model_dump_json(), datetime.utcnow().isoformat(), run_id))
        await db.commit()


async def add_step(step: AgentStep) -> AgentStep:
    """Persist an agent step."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO agent_steps
              (step_id, run_id, agent, status, message, started_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            step.step_id,
            step.run_id,
            step.agent.value,
            step.status.value,
            step.message,
            step.started_at.isoformat(),
            json.dumps(step.metadata),
        ))
        await db.commit()
    return step


async def complete_step(step_id: str, status: StepStatus, message: str = "", metadata: dict = {}):
    """Mark an agent step as completed or failed."""
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE agent_steps
            SET status = ?, completed_at = ?, message = ?, metadata = ?
            WHERE step_id = ?
        """, (status.value, now, message, json.dumps(metadata), step_id))
        await db.commit()


async def get_run(run_id: str) -> Optional[PipelineRun]:
    """Load a full pipeline run with its steps."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        run = _row_to_run(row)
        run.steps = await _get_steps(db, run_id)
        return run


async def get_all_runs(limit: int = 50) -> List[PipelineRun]:
    """Load recent pipeline runs (without steps for speed)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM pipeline_runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_run(r) for r in rows]


async def get_pending_runs() -> List[PipelineRun]:
    """Load runs that were interrupted (pending/running) for resume on restart."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM pipeline_runs WHERE status IN ('pending', 'running')"
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_run(r) for r in rows]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _row_to_run(row) -> PipelineRun:
    report = None
    if row["final_report"]:
        try:
            report = FinalReport.model_validate_json(row["final_report"])
        except Exception:
            pass
    return PipelineRun(
        run_id=row["run_id"],
        status=RunStatus(row["status"]),
        event_type=EventType(row["event_type"]),
        topic=row["topic"],
        repo=row["repo"],
        trigger_payload=json.loads(row["trigger_payload"] or "{}"),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
        error=row["error"],
        final_report=report,
    )


async def _get_steps(db: aiosqlite.Connection, run_id: str) -> List[AgentStep]:
    async with db.execute(
        "SELECT * FROM agent_steps WHERE run_id = ? ORDER BY started_at", (run_id,)
    ) as cur:
        rows = await cur.fetchall()
    return [
        AgentStep(
            step_id=r["step_id"],
            run_id=r["run_id"],
            agent=AgentName(r["agent"]),
            status=StepStatus(r["status"]),
            message=r["message"] or "",
            started_at=datetime.fromisoformat(r["started_at"]),
            completed_at=datetime.fromisoformat(r["completed_at"]) if r["completed_at"] else None,
            metadata=json.loads(r["metadata"] or "{}"),
        )
        for r in rows
    ]
