"""
QualityEngine AI — FastAPI Server
Handles GitHub webhook ingestion, manual triggers, SSE streaming, and dashboard serving.
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
from typing import AsyncIterator, Dict

from dotenv import load_dotenv
load_dotenv()

import omium
omium.init(project="QualityEngine AI")

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from agents.pipeline import run_pipeline
from db import database as db_ops
from models.schemas import (
    EventType, ManualTriggerRequest, PipelineRun,
    RunStatus, TriggerResponse
)
from tools.github_api import get_pr_diff, get_pr_metadata

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/qualityengine.log"),
    ],
)
logger = logging.getLogger("qualityengine.main")

# ─── SSE Event Bus ────────────────────────────────────────────────────────────
_sse_queues: Dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)


def emit_sse(run_id: str, event: str, data: dict):
    """Push SSE event to the live dashboard queue for a run."""
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
    logger.info("QualityEngine AI started ✅")

    # Mark any interrupted runs as failed
    pending = await db_ops.get_pending_runs()
    if pending:
        logger.warning("Found %d interrupted runs — marking as failed", len(pending))
        for run in pending:
            await db_ops.update_run_status(run.run_id, RunStatus.FAILED, "Server restarted")

    yield
    logger.info("QualityEngine AI shutting down")


app = FastAPI(
    title="QualityEngine AI",
    description="Autonomous PR Quality Engineering Pipeline",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

app.mount("/dashboard", StaticFiles(directory="dashboard", html=True), name="dashboard")


# ─── GitHub Webhook ───────────────────────────────────────────────────────────
@app.post("/webhook/github", status_code=202)
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Receives GitHub webhook events.
    Handles: pull_request (opened/synchronize/reopened) and issue_comment (/re-review).
    Returns 202 immediately; pipeline runs in background.
    """
    body = await request.body()

    # HMAC validation
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if secret:
        sig = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event_type_raw = request.headers.get("X-GitHub-Event", "")
    payload = json.loads(body)
    repo = payload.get("repository", {}).get("full_name", "unknown/repo")

    # ── Pull Request event ─────────────────────────────────────────────────────
    if event_type_raw == "pull_request":
        action = payload.get("action", "")
        if action not in ("opened", "synchronize", "reopened"):
            return {"status": "ignored", "reason": f"PR action '{action}' not monitored"}

        pr     = payload.get("pull_request", {})
        pr_num = pr.get("number")
        title  = pr.get("title", "")
        author = pr.get("user", {}).get("login", "")
        branch = pr.get("head", {}).get("ref", "")
        sha    = pr.get("head", {}).get("sha", "")

        # Fetch the actual diff
        diff_text = get_pr_diff(repo, pr_num) if pr_num else ""

        run = PipelineRun(
            run_id=str(uuid.uuid4()),
            repo=repo,
            pr_number=pr_num,
            pr_title=title,
            pr_author=author,
            branch=branch,
            commit_sha=sha,
            diff_text=diff_text,
            event_type=EventType.PULL_REQUEST,
            topic=f"PR #{pr_num}: {title[:60]}",
        )
        await db_ops.create_run(run)
        background_tasks.add_task(run_pipeline, run, emit_sse)

        logger.info("PR webhook: #%d '%s' by %s → run %s", pr_num, title, author, run.run_id)
        return {"run_id": run.run_id, "status": "accepted",
                "stream_url": f"/stream/{run.run_id}"}

    # ── Issue comment — /re-review command ────────────────────────────────────
    elif event_type_raw == "issue_comment":
        comment_body = payload.get("comment", {}).get("body", "")
        if "/re-review" not in comment_body:
            return {"status": "ignored"}

        pr_num = payload.get("issue", {}).get("number")
        if not pr_num:
            return {"status": "ignored", "reason": "Not a PR comment"}

        meta     = get_pr_metadata(repo, pr_num)
        diff_text = get_pr_diff(repo, pr_num)

        run = PipelineRun(
            run_id=str(uuid.uuid4()),
            repo=repo,
            pr_number=pr_num,
            pr_title=meta.get("title", f"PR #{pr_num}"),
            pr_author=meta.get("author", ""),
            branch=meta.get("branch", ""),
            commit_sha=meta.get("commit_sha", ""),
            diff_text=diff_text,
            event_type=EventType.PULL_REQUEST,
            topic=f"Re-review PR #{pr_num}: {meta.get('title', '')[:50]}",
        )
        await db_ops.create_run(run)
        background_tasks.add_task(run_pipeline, run, emit_sse)

        logger.info("/re-review triggered on PR #%d → run %s", pr_num, run.run_id)
        return {"run_id": run.run_id, "status": "accepted",
                "stream_url": f"/stream/{run.run_id}"}

    # ── push to main — regression check ──────────────────────────────────────
    elif event_type_raw == "push":
        ref = payload.get("ref", "")
        if not any(ref.endswith(b) for b in ("/main", "/master")):
            return {"status": "ignored", "reason": "Not a main branch push"}

        commits  = payload.get("commits", [])
        msg      = commits[0]["message"] if commits else "Push"
        sha      = payload.get("after", "")

        run = PipelineRun(
            run_id=str(uuid.uuid4()),
            repo=repo,
            commit_sha=sha,
            diff_text="",   # No PR diff for direct push
            event_type=EventType.PUSH,
            topic=f"{repo}: {msg[:60]}",
        )
        await db_ops.create_run(run)
        background_tasks.add_task(run_pipeline, run, emit_sse)

        return {"run_id": run.run_id, "status": "accepted"}

    return {"status": "ignored", "reason": f"Event '{event_type_raw}' not handled"}


# ─── Manual Trigger ───────────────────────────────────────────────────────────
@app.post("/trigger", response_model=TriggerResponse)
async def manual_trigger(req: ManualTriggerRequest, background_tasks: BackgroundTasks):
    """Manually trigger the pipeline on any PR (for testing / demo)."""
    meta      = get_pr_metadata(req.repo, req.pr_number)
    diff_text = get_pr_diff(req.repo, req.pr_number)

    run = PipelineRun(
        run_id=str(uuid.uuid4()),
        repo=req.repo,
        pr_number=req.pr_number,
        pr_title=meta.get("title") or req.topic or f"PR #{req.pr_number}",
        pr_author=meta.get("author", ""),
        branch=meta.get("branch", ""),
        commit_sha=meta.get("commit_sha", ""),
        diff_text=diff_text,
        event_type=EventType.PULL_REQUEST,
        topic=req.topic or f"PR #{req.pr_number}: {meta.get('title', '')[:50]}",
    )
    await db_ops.create_run(run)
    background_tasks.add_task(run_pipeline, run, emit_sse)

    logger.info("Manual trigger: %s PR #%d → run %s", req.repo, req.pr_number, run.run_id)
    return TriggerResponse(
        run_id=run.run_id,
        status="accepted",
        message=f"Pipeline started for PR #{req.pr_number}",
        stream_url=f"/stream/{run.run_id}",
    )


# ─── SSE Stream ───────────────────────────────────────────────────────────────
@app.get("/stream/{run_id}")
async def stream_run(run_id: str, request: Request):
    """SSE stream for live pipeline updates. Dashboard connects here."""
    async def event_generator():
        run = await db_ops.get_run(run_id)
        if run:
            yield {"event": "init", "data": json.dumps({
                "run_id":    run_id,
                "status":    run.status.value,
                "topic":     run.topic,
                "pr_number": run.pr_number,
                "repo":      run.repo,
                "steps":     [s.model_dump(mode="json") for s in run.steps],
            })}

        queue = _sse_queues[run_id]
        while True:
            if await request.is_disconnected():
                break
            try:
                evt = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield {"event": evt["event"], "data": json.dumps(evt["data"])}
                if evt["event"] in ("complete",) or (
                    evt["event"] == "status" and
                    evt["data"].get("status") in ("completed", "failed")
                ):
                    break
            except asyncio.TimeoutError:
                yield {"event": "ping",
                       "data": json.dumps({"ts": datetime.utcnow().isoformat()})}

    return EventSourceResponse(event_generator())


# ─── Status Endpoints ─────────────────────────────────────────────────────────
@app.get("/status")
async def list_runs():
    runs = await db_ops.get_all_runs(limit=50)
    return [
        {
            "run_id":      r.run_id,
            "status":      r.status.value,
            "topic":       r.topic,
            "repo":        r.repo,
            "pr_number":   r.pr_number,
            "verdict":     r.decision.verdict.value if r.decision else None,
            "score":       r.decision.scores.overall if r.decision else None,
            "created_at":  r.created_at.isoformat(),
            "updated_at":  r.updated_at.isoformat(),
        }
        for r in runs
    ]


@app.get("/status/{run_id}")
async def get_run_status(run_id: str):
    run = await db_ops.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return {
        "run_id":      run.run_id,
        "status":      run.status.value,
        "topic":       run.topic,
        "repo":        run.repo,
        "pr_number":   run.pr_number,
        "pr_title":    run.pr_title,
        "pr_author":   run.pr_author,
        "verdict":     run.decision.verdict.value if run.decision else None,
        "scores":      run.decision.scores.model_dump() if run.decision else None,
        "reasoning":   run.decision.reasoning if run.decision else None,
        "github_comment": run.github_comment_url,
        "github_issue":   run.github_issue_url,
        "created_at":  run.created_at.isoformat(),
        "steps": [
            {
                "agent":     s.agent.value,
                "status":    s.status.value,
                "message":   s.message,
                "metadata":  s.metadata,
            }
            for s in run.steps
        ],
        "error": run.error,
    }


# ─── Health + Root ────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "QualityEngine AI", "version": "2.0.0"}


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse("""<!DOCTYPE html><html><head>
  <meta http-equiv="refresh" content="0;url=/dashboard/">
  <title>QualityEngine AI</title>
</head><body>Redirecting...</body></html>""")


# ─── Entry Point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", 8000)),
                reload=False, log_level="info")
