"""
PR Reviewer Agent — Groq Llama 3.3 70B via LangChain
Performs structured code review of the PR diff.
Runs in parallel with the Security Scanner via RunnableParallel.
"""
from __future__ import annotations
import logging
import os
import omium
from typing import Optional

from langchain_core.prompts import ChatPromptTemplate
from agents.model_router import get_llm

from models.schemas import (
    AgentName, AgentStep, QualityPlan, ReviewReport,
    PipelineRun, StepStatus, CodeIssue, IssueSeverity
)
import db.database as db_ops

logger = logging.getLogger("qualityengine.pr_reviewer")


def _build_chain():
    structured_llm = get_llm(temperature=0.2, structured_output=ReviewReport)

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a senior software engineer doing code review. Be fair and calibrated.

SCORING RUBRIC (use this exactly):
- 9-10: Production-ready. Proper error handling, validation, no security issues, clean code.
- 7-8:  Good code with minor issues. Would approve with optional suggestions.
- 5-6:  Needs changes. Has real bugs, missing validation, or style issues that matter.
- 3-4:  Major problems. Logic errors, missing error handling, bad patterns.
- 1-2:  Critical failures. Security vulnerabilities, broken logic, data loss risk.

RECOMMENDATION RULES:
- APPROVE if score >= 7 and no CRITICAL or HIGH severity issues
- REQUEST_CHANGES if score 5-6 or has HIGH severity issues
- REJECT if score <= 4 or has CRITICAL security issues

Do not under-score clean, well-written code. If code has proper validation, error handling,
and no obvious bugs, it should score 8-9."""),
        ("human", """Review this pull request diff.

Repository: {repo}
PR Title: {pr_title}
Change Type: {change_type}
Risk Level: {risk_level}

Review Focus Areas:
{review_focus}

Unified Diff:
{diff_text}

Produce a ReviewReport with:
- score: 1-10 (use the rubric above — do not under-score clean code)
- issues: list of CodeIssue objects (only real issues, not nitpicks)
- strengths: list of things done well
- summary: 2-3 sentence assessment
- recommendation: APPROVE / REQUEST_CHANGES / REJECT""")
    ])

    return prompt | structured_llm


_chain = None


def get_chain():
    global _chain
    if _chain is None:
        _chain = _build_chain()
    return _chain


@omium.trace("pr_reviewer")
async def run_pr_reviewer(run: PipelineRun, plan: QualityPlan) -> ReviewReport:
    """Perform structured code review of the PR diff."""
    step = AgentStep(
        run_id=run.run_id,
        agent=AgentName.PR_REVIEWER,
        status=StepStatus.STARTED,
        message="Reviewing code changes...",
    )
    await db_ops.add_step(step)

    try:
        chain = get_chain()
        report: ReviewReport = await chain.ainvoke({
            "repo":         run.repo,
            "pr_title":     run.pr_title or "Unknown",
            "change_type":  plan.change_type.value,
            "risk_level":   plan.risk_level.value,
            "review_focus": "\n".join(f"- {f}" for f in plan.review_focus),
            "diff_text":    (run.diff_text or "No diff available")[:6000],
        })

        critical = sum(1 for i in report.issues if i.severity == IssueSeverity.CRITICAL)
        high = sum(1 for i in report.issues if i.severity == IssueSeverity.HIGH)

        await db_ops.complete_step(
            step.step_id, StepStatus.COMPLETED,
            f"Review score: {report.score}/10 | {len(report.issues)} issues "
            f"({critical} critical, {high} high) | {report.recommendation}",
            {
                "score":          report.score,
                "issues":         len(report.issues),
                "critical":       critical,
                "recommendation": report.recommendation,
            }
        )
        logger.info("PR Review: score=%d/10 | %s", report.score, report.recommendation)
        return report

    except Exception as e:
        logger.error("PR Reviewer failed: %s", e)
        await db_ops.complete_step(step.step_id, StepStatus.FAILED, str(e))
        return ReviewReport(
            score=5,
            issues=[],
            strengths=[],
            summary=f"Review failed: {e}",
            recommendation="REQUEST_CHANGES",
        )
