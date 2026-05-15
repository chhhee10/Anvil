"""
Orchestrator Agent — Groq Llama 3.3 70B
Analyzes incoming events, creates a structured research plan,
fans out tasks to Researcher and Code Analyst, and hands off to Writer.
"""
from __future__ import annotations
import os
import json
import logging
import asyncio
import omium
from datetime import datetime
from typing import Optional, Callable, Awaitable

from groq import Groq

from models.schemas import (
    PipelineRun, ResearchPlan, ResearchTask, ResearchFindings,
    CodeAnalysis, AgentStep, AgentName, StepStatus, EventType
)
import db.database as db_ops

logger = logging.getLogger("newsroom.agents.orchestrator")

_client: Optional[Groq] = None


def get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=os.environ["GROQ_API_KEY"])
    return _client


async def _emit(run_id: str, agent: AgentName, message: str, metadata: dict = {}) -> AgentStep:
    """Persist and return an agent step."""
    step = AgentStep(
        run_id=run_id,
        agent=agent,
        status=StepStatus.STARTED,
        message=message,
        metadata=metadata,
    )
    await db_ops.add_step(step)
    return step


@omium.trace("create_research_plan")
async def create_research_plan(run: PipelineRun) -> ResearchPlan:
    """
    Step 1: Orchestrator analyzes the trigger and creates a structured plan.
    """
    step = await _emit(
        run.run_id, AgentName.ORCHESTRATOR,
        "Analyzing trigger and creating research plan..."
    )

    payload_summary = json.dumps(run.trigger_payload, indent=2)[:1200]

    messages = [
        {
            "role": "system",
            "content": "You are a tech intelligence orchestrator. Create structured research plans. Return JSON only."
        },
        {
            "role": "user",
            "content": f"""Analyze this event and create a research plan.

EVENT TYPE: {run.event_type.value}
TOPIC: {run.topic}
REPO: {run.repo or 'N/A'}
PAYLOAD:
{payload_summary}

Return JSON:
{{
  "main_topic": "clear 1-sentence research topic",
  "summary": "2-3 sentences: what happened and why it matters",
  "research_tasks": [
    {{"query": "specific search query 1", "priority": 1}},
    {{"query": "specific search query 2", "priority": 2}},
    {{"query": "specific search query 3", "priority": 2}}
  ],
  "code_tasks": []
}}

Make queries specific and targeted. Return ONLY the JSON."""
        }
    ]

    try:
        client = get_client()
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.3,
            response_format={"type": "json_object"},
            max_tokens=1000,
        )
        data = json.loads(response.choices[0].message.content)

        research_tasks = [
            ResearchTask(
                query=t["query"],
                priority=t.get("priority", 1),
                agent=AgentName.RESEARCHER,
            )
            for t in data.get("research_tasks", [])[:4]
        ]
        code_tasks = [
            ResearchTask(
                query=t["query"],
                priority=t.get("priority", 1),
                agent=AgentName.CODE_ANALYST,
            )
            for t in data.get("code_tasks", [])[:2]
        ]

        plan = ResearchPlan(
            event_type=run.event_type,
            main_topic=data.get("main_topic", run.topic),
            summary=data.get("summary", ""),
            research_tasks=research_tasks,
            code_tasks=code_tasks,
            repo=run.repo,
            commit_sha=run.trigger_payload.get("after"),
            pr_number=run.trigger_payload.get("number"),
        )
        await db_ops.complete_step(
            step.step_id, StepStatus.COMPLETED,
            f"Plan created: {len(research_tasks)} research tasks, {len(code_tasks)} code tasks",
            {"plan_id": plan.plan_id, "topic": plan.main_topic},
        )
        logger.info("Research plan created: %s", plan.main_topic)
        return plan

    except Exception as e:
        await db_ops.complete_step(step.step_id, StepStatus.FAILED, str(e))
        logger.error("Orchestrator planning failed: %s", e)
        # Fallback plan
        return ResearchPlan(
            event_type=run.event_type,
            main_topic=run.topic,
            summary=f"Researching: {run.topic}",
            research_tasks=[
                ResearchTask(query=run.topic, priority=1, agent=AgentName.RESEARCHER),
                ResearchTask(query=f"{run.topic} best practices 2024", priority=2, agent=AgentName.RESEARCHER),
                ResearchTask(query=f"{run.topic} security implications", priority=2, agent=AgentName.RESEARCHER),
            ],
            code_tasks=[],
            repo=run.repo,
        )


@omium.trace("orchestrate_pipeline")
async def orchestrate(
    run: PipelineRun,
    researcher_fn: Callable[[PipelineRun, ResearchPlan], Awaitable[ResearchFindings]],
    code_analyst_fn: Callable[[PipelineRun, ResearchPlan], Awaitable[CodeAnalysis]],
    writer_fn: Callable[[PipelineRun, ResearchPlan, ResearchFindings, CodeAnalysis], Awaitable[str]],
    critic_fn: Callable[[PipelineRun, str], Awaitable[tuple]],
    deliver_fn: Callable[[PipelineRun, str, object], Awaitable[None]],
    sse_emit: Optional[Callable] = None,
) -> None:
    """
    Main orchestration loop:
    1. Plan → 2. Parallel fan-out → 3. Write → 4. Critique loop → 5. Deliver
    """
    def emit_sse(event: str, data: dict):
        if sse_emit:
            try:
                sse_emit(run.run_id, event, data)
            except Exception:
                pass

    # ── Step 1: Plan ──────────────────────────────────────────────────────────
    emit_sse("step", {"agent": "orchestrator", "message": "Creating research plan..."})
    plan = await create_research_plan(run)
    emit_sse("plan", {"topic": plan.main_topic, "summary": plan.summary})

    # ── Step 2: Parallel fan-out ───────────────────────────────────────────────
    emit_sse("step", {"agent": "orchestrator", "message": "Dispatching Researcher + Code Analyst in parallel..."})

    async def safe_research():
        try:
            return await researcher_fn(run, plan)
        except Exception as e:
            logger.error("Researcher failed: %s", e)
            return ResearchFindings(topic=plan.main_topic, synthesis=f"Research unavailable: {e}")

    async def safe_code():
        try:
            return await code_analyst_fn(run, plan)
        except Exception as e:
            logger.error("Code analyst failed: %s", e)
            return CodeAnalysis(summary=f"Code analysis unavailable: {e}")

    research_findings, code_analysis = await asyncio.gather(
        safe_research(),
        safe_code(),
    )

    emit_sse("step", {
        "agent": "orchestrator",
        "message": f"✅ Agents returned: {len(research_findings.key_findings)} findings, {len(code_analysis.files_changed)} files analyzed"
    })

    # ── Step 3: Writer ─────────────────────────────────────────────────────────
    emit_sse("step", {"agent": "writer", "message": "Drafting intelligence report..."})
    report_draft = await writer_fn(run, plan, research_findings, code_analysis)

    # ── Step 4: Critic → Revision Loop ─────────────────────────────────────────
    revision_count = 0
    max_revisions = 2
    final_report_text = report_draft
    final_score = None

    while revision_count <= max_revisions:
        emit_sse("step", {"agent": "critic", "message": f"Review round {revision_count + 1}..."})
        score, approved, revision_requests = await critic_fn(run, final_report_text)
        final_score = score
        if approved or revision_count >= max_revisions:
            emit_sse("step", {
                "agent": "critic",
                "message": f"✅ Report approved (score: {score.overall:.1f}/10)"
            })
            break
        emit_sse("step", {
            "agent": "writer",
            "message": f"Revising based on critic feedback (pass {revision_count + 1})..."
        })
        final_report_text = await writer_fn(
            run, plan, research_findings, code_analysis,
            revision_requests=revision_requests,
            previous_draft=final_report_text,
        )
        revision_count += 1

    # ── Step 5: Deliver ────────────────────────────────────────────────────────
    emit_sse("step", {"agent": "system", "message": "Delivering final report..."})
    await deliver_fn(run, final_report_text, final_score, revision_count)
    emit_sse("complete", {"run_id": run.run_id, "revisions": revision_count})
