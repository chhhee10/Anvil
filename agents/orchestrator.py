"""
Orchestrator Agent — Gemini 2.0 Flash via LangChain
Parses the PR diff, classifies the change type and risk level,
and produces a structured QualityPlan for the downstream agents.
"""
from __future__ import annotations
import logging
import os
import omium
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from langchain_core.prompts import ChatPromptTemplate
from agents.model_router import get_llm

from models.schemas import (
    AgentName, AgentStep, QualityPlan, PipelineRun, StepStatus,
    ChangeType, RiskLevel
)
import db.database as db_ops

logger = logging.getLogger("qualityengine.orchestrator")


def _build_chain():
    structured_llm = get_llm(temperature=0.2, structured_output=QualityPlan)

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a senior engineering lead analyzing a GitHub pull request.
Your job is to create a structured quality plan for the review pipeline.
Classify the change accurately and identify what needs to be tested and reviewed.
Be specific — list actual file paths and function names from the diff."""),
        ("human", """Analyze this pull request and create a QualityPlan.

PR Title: {pr_title}
PR Author: {pr_author}
Repository: {repo}
Branch: {branch}

Unified Diff:
{diff_text}

Produce a QualityPlan with:
- change_type: feature / bug_fix / refactor / config / dependency / docs / unknown
- risk_level: low / medium / high / critical
- summary: 1-2 sentences describing what this PR does
- files_to_test: list of file paths that have logic changes needing tests
- review_focus: 3-5 specific things the PR Reviewer should focus on
- security_focus: 2-4 specific security aspects to check (auth, input validation, secrets, etc.)
- test_functions: list of function/method names visible in the diff that need unit tests
- skip_reason: null unless this is a docs-only or trivial change that should skip testing""")
    ])

    return prompt | structured_llm


_chain = None


def get_chain():
    global _chain
    if _chain is None:
        _chain = _build_chain()
    return _chain


@omium.trace("orchestrator_plan")
async def create_quality_plan(run: PipelineRun) -> QualityPlan:
    """Parse the PR diff and create a structured QualityPlan."""
    step = AgentStep(
        run_id=run.run_id,
        agent=AgentName.ORCHESTRATOR,
        status=StepStatus.STARTED,
        message="Analyzing PR diff and creating quality plan...",
    )
    await db_ops.add_step(step)

    try:
        chain = get_chain()
        plan: QualityPlan = await chain.ainvoke({
            "pr_title":  run.pr_title or "Unknown",
            "pr_author": run.pr_author or "Unknown",
            "repo":      run.repo,
            "branch":    run.branch or "unknown",
            "diff_text": (run.diff_text or "No diff available")[:8000],
        })

        await db_ops.complete_step(
            step.step_id, StepStatus.COMPLETED,
            f"Plan: {plan.change_type.value} | risk={plan.risk_level.value} | "
            f"{len(plan.files_to_test)} files to test | {len(plan.test_functions)} functions",
            {
                "change_type": plan.change_type.value,
                "risk_level":  plan.risk_level.value,
                "files":       len(plan.files_to_test),
                "functions":   len(plan.test_functions),
            }
        )
        logger.info("QualityPlan: %s | risk=%s", plan.change_type.value, plan.risk_level.value)
        return plan

    except Exception as e:
        logger.error("Orchestrator failed: %s", e)
        await db_ops.complete_step(step.step_id, StepStatus.FAILED, str(e))
        # Fallback plan — still lets pipeline run
        return QualityPlan(
            change_type=ChangeType.UNKNOWN,
            risk_level=RiskLevel.MEDIUM,
            summary=f"PR analysis failed ({e}). Running generic review.",
            files_to_test=[],
            review_focus=["General code correctness", "Error handling", "Code style"],
            security_focus=["Input validation", "Authentication checks"],
            test_functions=[],
        )
