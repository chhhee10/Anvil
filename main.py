"""
NewsRoom AI — FastAPI Server
Handles webhook ingestion, manual triggers, pipeline orchestration,
SSE streaming for live dashboard updates, and report delivery.
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import logging
import os
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator, Dict, List, Optional

from dotenv import load_dotenv
load_dotenv()

import omium
from omium import OmiumConfig

# Initialize Omium Observability
# omium.init(project="NewsRoom AI")

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from agents import code_analyst, critic, orchestrator, researcher, writer
from db import database as db_ops
from models.schemas import (
    AgentName, AgentStep, EventType, FinalReport, ManualTriggerRequest,
    PipelineRun, RunStatus, StatusResponse, StepStatus, TriggerResponse
)
from tools import file_writer

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/newsroom.log"),
    ],
)
logger = logging.getLogger("newsroom.main")

# ─── SSE Event Bus ────────────────────────────────────────────────────────────
# Maps run_id → list of pending SSE event dicts
_sse_queues: Dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)


def emit_sse(run_id: str, event: str, data: dict):
    """Push an event into the SSE queue for a given run."""
    try:
        _sse_queues[run_id].put_nowait({"event": event, "data": data})
    except asyncio.QueueFull:
        pass


# ─── App Lifecycle ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    os.makedirs("logs", exist_ok=True)
    os.makedirs("reports", exist_ok=True)
    await db_ops.init_db()
    logger.info("NewsRoom AI started ✅")

    # Resume any runs that were interrupted
    pending = await db_ops.get_pending_runs()
    if pending:
        logger.warning("Found %d interrupted runs — marking as failed", len(pending))
        for run in pending:
            await db_ops.update_run_status(run.run_id, RunStatus.FAILED, "Server restarted")

    yield
    logger.info("NewsRoom AI shutting down")


app = FastAPI(
    title="NewsRoom AI",
    description="Multi-agent autonomous tech intelligence pipeline",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve dashboard
app.mount("/dashboard", StaticFiles(directory="dashboard", html=True), name="dashboard")


# ─── Pipeline Runner ──────────────────────────────────────────────────────────

async def run_pipeline(run: PipelineRun):
    """The full multi-agent pipeline. Runs in a background task."""
    logger.info("Pipeline starting: %s [%s]", run.run_id, run.topic)
    await db_ops.update_run_status(run.run_id, RunStatus.RUNNING)
    emit_sse(run.run_id, "status", {"status": "running", "run_id": run.run_id})

    async def deliver(run: PipelineRun, report_text: str, score, revision_count: int):
        """Save report to disk and DB, update run status."""
        path = file_writer.write_report(run.run_id, run.topic, report_text)
        final = FinalReport(
            run_id=run.run_id,
            topic=run.topic,
            event_type=run.event_type,
            report_markdown=report_text,
            critic_score=score,
            revision_count=revision_count,
            report_path=path,
        )
        await db_ops.save_final_report(run.run_id, final)
        await db_ops.update_run_status(run.run_id, RunStatus.COMPLETED)
        step = AgentStep(
            run_id=run.run_id, agent=AgentName.SYSTEM, status=StepStatus.COMPLETED,
            message=f"Report saved to {path}",
            metadata={"path": path, "chars": len(report_text)},
        )
        await db_ops.add_step(step)
        emit_sse(run.run_id, "status", {"status": "completed", "report_path": path})
        logger.info("Pipeline complete: %s → %s", run.run_id, path)

    try:
        await orchestrator.orchestrate(
            run=run,
            researcher_fn=researcher.run_researcher,
            code_analyst_fn=code_analyst.run_code_analyst,
            writer_fn=writer.run_writer,
            critic_fn=critic.run_critic,
            deliver_fn=deliver,
            sse_emit=emit_sse,
        )
    except Exception as e:
        logger.exception("Pipeline failed for %s: %s", run.run_id, e)
        await db_ops.update_run_status(run.run_id, RunStatus.FAILED, str(e))
        emit_sse(run.run_id, "status", {"status": "failed", "error": str(e)})


# ─── Webhook: GitHub ──────────────────────────────────────────────────────────

@app.post("/webhook/github", status_code=202)
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Receives GitHub webhook events (push, pull_request, issues).
    Validates HMAC signature, returns 202 immediately, runs pipeline in background.
    """
    body = await request.body()

    # Validate signature if secret is set
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if secret:
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        # Note: Python 3 hmac.new() — correct usage
        if not hmac.compare_digest(sig_header, expected):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event_type_raw = request.headers.get("X-GitHub-Event", "push")
    payload = json.loads(body)

    # Map GitHub event to our EventType
    event_map = {
        "push": EventType.PUSH,
        "pull_request": EventType.PULL_REQUEST,
        "issues": EventType.ISSUES,
    }
    event_type = event_map.get(event_type_raw, EventType.PUSH)

    # Extract topic + repo
    repo = payload.get("repository", {}).get("full_name", "unknown/repo")

    if event_type == EventType.PUSH:
        commits = payload.get("commits", [])
        msg = commits[0]["message"] if commits else "Push event"
        topic = f"{repo}: {msg[:80]}"
    elif event_type == EventType.PULL_REQUEST:
        pr = payload.get("pull_request", {})
        topic = f"{repo} PR #{pr.get('number', '?')}: {pr.get('title', '')[:60]}"
    else:
        issue = payload.get("issue", {})
        topic = f"{repo} Issue #{issue.get('number', '?')}: {issue.get('title', '')[:60]}"

    run = PipelineRun(
        run_id=str(uuid.uuid4()),
        event_type=event_type,
        topic=topic,
        repo=repo,
        trigger_payload=payload,
    )
    await db_ops.create_run(run)
    background_tasks.add_task(run_pipeline, run)

    logger.info("GitHub webhook received: %s → run %s", event_type.value, run.run_id)
    return {
        "run_id": run.run_id,
        "status": "accepted",
        "stream_url": f"/stream/{run.run_id}",
    }


# ─── Manual Trigger ───────────────────────────────────────────────────────────

@app.post("/trigger", response_model=TriggerResponse)
async def manual_trigger(req: ManualTriggerRequest, background_tasks: BackgroundTasks):
    """
    Manually trigger the pipeline with a custom topic.
    """
    run = PipelineRun(
        run_id=str(uuid.uuid4()),
        event_type=EventType.MANUAL,
        topic=req.topic,
        repo=req.repo,
        trigger_payload={"topic": req.topic, "context": req.context or ""},
    )
    await db_ops.create_run(run)
    background_tasks.add_task(run_pipeline, run)

    logger.info("Manual trigger: '%s' → run %s", req.topic, run.run_id)
    return TriggerResponse(
        run_id=run.run_id,
        status="accepted",
        message=f"Pipeline started for: {req.topic}",
        stream_url=f"/stream/{run.run_id}",
    )


# ─── SSE Stream ───────────────────────────────────────────────────────────────

@app.get("/stream/{run_id}")
async def stream_run(run_id: str, request: Request):
    """
    Server-Sent Events stream for live pipeline updates.
    Dashboard connects here to get real-time agent step events.
    """
    async def event_generator():
        # Send current state first
        run = await db_ops.get_run(run_id)
        if run:
            yield {"event": "init", "data": json.dumps({
                "run_id": run_id,
                "status": run.status.value,
                "topic": run.topic,
                "steps": [s.model_dump(mode="json") for s in run.steps],
            })}

        queue = _sse_queues[run_id]
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield {"event": event["event"], "data": json.dumps(event["data"])}
                # Stop streaming if pipeline is done
                if event["event"] in ("complete",) or (
                    event["event"] == "status" and event["data"].get("status") in ("completed", "failed")
                ):
                    break
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": json.dumps({"ts": datetime.utcnow().isoformat()})}

    return EventSourceResponse(event_generator())


# ─── Status Endpoints ─────────────────────────────────────────────────────────

@app.get("/status")
async def list_runs():
    """List all pipeline runs (recent 50)."""
    runs = await db_ops.get_all_runs(limit=50)
    return [
        {
            "run_id": r.run_id,
            "status": r.status.value,
            "topic": r.topic,
            "event_type": r.event_type.value,
            "repo": r.repo,
            "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat(),
            "has_report": r.final_report is not None,
        }
        for r in runs
    ]


@app.get("/status/{run_id}")
async def get_run_status(run_id: str):
    """Get detailed status for a specific run."""
    run = await db_ops.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "topic": run.topic,
        "event_type": run.event_type.value,
        "repo": run.repo,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "steps": [
            {
                "step_id": s.step_id,
                "agent": s.agent.value,
                "status": s.status.value,
                "message": s.message,
                "started_at": s.started_at.isoformat(),
                "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                "metadata": s.metadata,
            }
            for s in run.steps
        ],
        "has_report": run.final_report is not None,
        "critic_score": run.final_report.critic_score.model_dump() if run.final_report and run.final_report.critic_score else None,
        "error": run.error,
    }


@app.get("/report/{run_id}")
async def get_report(run_id: str):
    """Fetch the final markdown report for a run."""
    run = await db_ops.get_run(run_id)
    if not run or not run.final_report:
        raise HTTPException(status_code=404, detail="Report not found")
    return {
        "run_id": run_id,
        "topic": run.final_report.topic,
        "report_markdown": run.final_report.report_markdown,
        "critic_score": run.final_report.critic_score.model_dump() if run.final_report.critic_score else None,
        "revision_count": run.final_report.revision_count,
        "generated_at": run.final_report.generated_at.isoformat(),
        "report_path": run.final_report.report_path,
    }


# ─── Root ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    """Redirect to dashboard."""
    return HTMLResponse("""
<!DOCTYPE html>
<html>
<head>
  <meta http-equiv="refresh" content="0;url=/dashboard/">
  <title>NewsRoom AI</title>
</head>
<body>Redirecting to dashboard...</body>
</html>
""")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "NewsRoom AI", "version": "1.0.0"}


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", 8000)),
        reload=False,
        log_level="info",
    )
