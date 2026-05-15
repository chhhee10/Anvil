"""
QualityEngine AI — Master Pipeline Orchestrator
Wires all 7 agents together. This is the heart of the system.

Flow:
  Orchestrator → [PR Reviewer ∥ Security Scanner] → Test Generator
  → [Self-Healer if needed] → Decision Agent → GitHub Action → SSE
"""
from __future__ import annotations
import asyncio
import logging
import os
from datetime import datetime
from typing import Callable, List

import omium

from models.schemas import (
    AgentName, AgentStep, HealAttempt, PipelineRun,
    PRDecision, RunStatus, StepStatus, TestResult, Verdict
)
import db.database as db_ops
from tools import github_api

from agents import orchestrator, pr_reviewer, security_scanner
from agents import test_generator, self_healer, decision_agent

logger = logging.getLogger("qualityengine.pipeline")


@omium.trace("full_pipeline")
async def run_pipeline(run: PipelineRun, sse_emit: Callable) -> None:
    """
    Full autonomous pipeline. Called as a FastAPI background task.
    sse_emit(run_id, event, data) pushes updates to the live dashboard.
    """
    logger.info("Pipeline starting: %s | PR #%s | %s", run.run_id, run.pr_number, run.repo)
    await db_ops.update_run_status(run.run_id, RunStatus.RUNNING)
    sse_emit(run.run_id, "status", {"status": "running", "run_id": run.run_id})

    try:
        # ── Step 0: Set GitHub commit status → pending ─────────────────────────
        if run.commit_sha:
            github_api.set_commit_status(
                run.repo, run.commit_sha,
                "pending", "QualityEngine AI is analyzing this PR..."
            )

        # ── Step 1: Orchestrator creates QualityPlan ───────────────────────────
        _emit_agent(sse_emit, run.run_id, "orchestrator", "Planning quality review...")
        plan = await orchestrator.create_quality_plan(run)
        run.quality_plan = plan

        sse_emit(run.run_id, "plan", {
            "change_type": plan.change_type.value,
            "risk_level":  plan.risk_level.value,
            "summary":     plan.summary,
            "files":       plan.files_to_test,
        })

        # Skip pipeline if orchestrator says it's trivial (e.g. docs only)
        if plan.skip_reason:
            logger.info("Skipping pipeline: %s", plan.skip_reason)
            await _handle_skip(run, plan.skip_reason, sse_emit)
            return

        # ── Step 2: Parallel fan-out — PR Reviewer + Security Scanner ──────────
        _emit_agent(sse_emit, run.run_id, "pr_reviewer",
                    "Reviewing code changes...")
        _emit_agent(sse_emit, run.run_id, "security_scanner",
                    "Scanning for security vulnerabilities...")

        review, security = await asyncio.gather(
            pr_reviewer.run_pr_reviewer(run, plan),
            security_scanner.run_security_scanner(run, plan),
        )
        run.review_report   = review
        run.security_report = security

        sse_emit(run.run_id, "review", {
            "score":          review.score,
            "issues":         len(review.issues),
            "recommendation": review.recommendation,
            "summary":        review.summary,
        })
        sse_emit(run.run_id, "security", {
            "score":    security.score,
            "critical": security.critical_count,
            "high":     security.high_count,
            "verdict":  security.recommendation,
        })

        # ── Step 3: Test Generator ─────────────────────────────────────────────
        _emit_agent(sse_emit, run.run_id, "test_generator",
                    f"Generating tests for {len(plan.test_functions)} functions...")

        test_result, source_files = await test_generator.run_test_generator(run, plan)
        run.test_result = test_result

        sse_emit(run.run_id, "tests", {
            "passed":  test_result.passed,
            "failed":  test_result.failed,
            "errors":  test_result.errors,
            "success": test_result.success,
            "stdout":  test_result.stdout[:1000],
        })

        # ── Step 4: Self-Healer (only if tests actually failed) ────────────────
        heal_attempts: List[HealAttempt] = []

        if (
            not test_result.success
            and not test_result.timed_out
            and (test_result.failed > 0 or test_result.errors > 0)
        ):
            _emit_agent(sse_emit, run.run_id, "self_healer",
                        f"Tests failed — attempting self-heal (max 3 attempts)...")

            test_result, heal_attempts = await self_healer.run_self_healer(
                run, plan, test_result, source_files=source_files
            )
            run.test_result    = test_result
            run.heal_attempts  = heal_attempts

            for attempt in heal_attempts:
                sse_emit(run.run_id, "heal", {
                    "attempt":         attempt.attempt,
                    "fix_description": attempt.fix_description,
                    "success":         attempt.test_result.success,
                    "passed":          attempt.test_result.passed,
                    "failed":          attempt.test_result.failed,
                })

        # ── Step 5: Decision Agent ─────────────────────────────────────────────
        _emit_agent(sse_emit, run.run_id, "decision_agent",
                    "Making final PR decision...")

        decision = await decision_agent.run_decision_agent(
            run, plan, review, security, test_result, heal_attempts
        )
        run.decision = decision

        sse_emit(run.run_id, "verdict", {
            "verdict":      decision.verdict.value,
            "overall":      decision.scores.overall,
            "correctness":  decision.scores.correctness,
            "security":     decision.scores.security,
            "test_coverage":decision.scores.test_coverage,
            "code_quality": decision.scores.code_quality,
            "risk":         decision.scores.risk,
            "reasoning":    decision.reasoning,
        })

        # ── Step 6: Execute GitHub Action ──────────────────────────────────────
        _emit_agent(sse_emit, run.run_id, "system",
                    f"Executing GitHub action: {decision.verdict.value}...")

        comment_url, issue_url = await _execute_github_action(run, decision)
        run.github_comment_url = comment_url
        run.github_issue_url   = issue_url

        # ── Step 7: Persist + Complete ──────────────────────────────────────────
        await db_ops.save_decision(run.run_id, decision, comment_url, issue_url)
        await db_ops.update_run_status(run.run_id, RunStatus.COMPLETED)

        # Set final commit status
        if run.commit_sha:
            gh_state = "success" if decision.verdict in (
                Verdict.MERGE, Verdict.MERGE_WITH_FIX
            ) else "failure"
            github_api.set_commit_status(
                run.repo, run.commit_sha, gh_state,
                f"QualityEngine: {decision.verdict.value} (score {decision.scores.overall}/10)"
            )

        sse_emit(run.run_id, "complete", {
            "run_id":           run.run_id,
            "verdict":          decision.verdict.value,
            "score":            decision.scores.overall,
            "github_comment":   comment_url,
            "github_issue":     issue_url,
        })
        logger.info("Pipeline complete: %s → %s (%.1f/10)",
                    run.run_id, decision.verdict.value, decision.scores.overall)

    except Exception as e:
        logger.exception("Pipeline failed for %s: %s", run.run_id, e)
        await db_ops.update_run_status(run.run_id, RunStatus.FAILED, str(e))
        if run.commit_sha:
            github_api.set_commit_status(
                run.repo, run.commit_sha, "error",
                "QualityEngine encountered an internal error"
            )
        sse_emit(run.run_id, "status", {"status": "failed", "error": str(e)})


# ─── GitHub Action Dispatcher ─────────────────────────────────────────────────

async def _execute_github_action(
    run: PipelineRun, decision: PRDecision
) -> tuple[str | None, str | None]:
    """
    Take the appropriate GitHub action based on the verdict.
    Returns (comment_url, issue_url).
    """
    repo   = run.repo
    pr_num = run.pr_number
    sha    = run.commit_sha or ""
    comment_url = None
    issue_url   = None

    if decision.verdict == Verdict.MERGE:
        # Post approval review + merge
        body = _format_approval_comment(decision)
        comment_url = github_api.post_pr_review(repo, pr_num, sha, body, "APPROVE")
        merged = github_api.merge_pr(
            repo, pr_num,
            commit_title=f"Auto-merge PR #{pr_num}: {run.pr_title or 'via QualityEngine'}",
            commit_message=decision.merge_message or "",
        )
        if not merged:
            # Fallback: just comment if merge fails (e.g. conflicts)
            github_api.post_pr_comment(repo, pr_num,
                "⚠️ QualityEngine approved this PR but auto-merge failed. Please merge manually.")

    elif decision.verdict == Verdict.MERGE_WITH_FIX:
        # Post comment explaining the fix + merge
        body = _format_merge_with_fix_comment(decision, run.heal_attempts)
        comment_url = github_api.post_pr_comment(repo, pr_num, body)
        github_api.post_pr_review(repo, pr_num, sha,
            "QualityEngine Auto-approved after self-healing.", "APPROVE")
        github_api.merge_pr(
            repo, pr_num,
            commit_title=f"Auto-merge (self-healed) PR #{pr_num}: {run.pr_title or ''}",
        )

    elif decision.verdict == Verdict.REJECT:
        # Post rejection comment first, then close
        body = _format_rejection_comment(decision)
        comment_url = github_api.post_pr_comment(repo, pr_num, body)
        github_api.close_pr(repo, pr_num)

    elif decision.verdict == Verdict.BUG_REPORT:
        # Post comment + close PR + open bug issue
        comment_body = _format_bug_comment(decision)
        comment_url = github_api.post_pr_comment(repo, pr_num, comment_body)
        github_api.close_pr(repo, pr_num)
        if decision.bug_title and decision.bug_body:
            issue_url = github_api.create_bug_issue(
                repo,
                title=decision.bug_title,
                body=decision.bug_body,
                labels=["bug", "auto-generated", "qualityengine", f"from-pr-{pr_num}"],
            )

    return comment_url, issue_url


# ─── Comment Formatters ────────────────────────────────────────────────────────

def _format_approval_comment(decision: PRDecision) -> str:
    s = decision.scores
    return f"""## ✅ QualityEngine AI — APPROVED

**Overall Score: {s.overall}/10**

| Metric | Score |
|--------|-------|
| Correctness | {s.correctness}/10 |
| Security | {s.security}/10 |
| Test Coverage | {s.test_coverage}/10 |
| Code Quality | {s.code_quality}/10 |
| Risk | {s.risk}/10 |

**Reasoning:** {decision.reasoning}

{decision.merge_message or ''}

---
*🤖 Reviewed autonomously by [QualityEngine AI](https://github.com/chhhee10/ANVIL)*"""


def _format_merge_with_fix_comment(decision: PRDecision, heals: list) -> str:
    s = decision.scores
    heal_log = "\n".join(
        f"- Attempt {h.attempt}: {'✅ Fixed' if h.test_result.success else '❌ Still failing'}"
        for h in heals
    )
    return f"""## 🔧 QualityEngine AI — APPROVED (with self-healing)

**Overall Score: {s.overall}/10**

Tests initially failed but were automatically fixed by the Self-Healer agent.

**Healing Log:**
{heal_log}

**Reasoning:** {decision.reasoning}

---
*🤖 Reviewed autonomously by [QualityEngine AI](https://github.com/chhhee10/ANVIL)*"""


def _format_rejection_comment(decision: PRDecision) -> str:
    s = decision.scores
    return f"""## ❌ QualityEngine AI — REJECTED

**Overall Score: {s.overall}/10**

| Metric | Score |
|--------|-------|
| Correctness | {s.correctness}/10 |
| Security | {s.security}/10 |
| Test Coverage | {s.test_coverage}/10 |
| Code Quality | {s.code_quality}/10 |
| Risk | {s.risk}/10 |

**Issues Found:**
{decision.rejection_reason or 'See scores above.'}

**Required Actions:** Please address the issues above and re-open this PR.

---
*🤖 Reviewed autonomously by [QualityEngine AI](https://github.com/chhhee10/ANVIL)*"""


def _format_bug_comment(decision: PRDecision) -> str:
    return f"""## 🐛 QualityEngine AI — BUG DETECTED

This PR introduces code that **could not be automatically fixed** after 3 self-healing attempts.

A detailed bug report has been filed: see the linked issue.

**Reasoning:** {decision.reasoning}

This PR has been closed. Please fix the underlying issue and open a new PR.

---
*🤖 Reviewed autonomously by [QualityEngine AI](https://github.com/chhhee10/ANVIL)*"""


# ─── SSE Helper ───────────────────────────────────────────────────────────────

def _emit_agent(sse_emit: Callable, run_id: str, agent: str, message: str):
    sse_emit(run_id, "step", {"agent": agent, "message": message, "status": "started"})


async def _handle_skip(run: PipelineRun, reason: str, sse_emit: Callable):
    """Handle docs-only or trivial PRs by posting a quick comment and skipping."""
    if run.pr_number:
        github_api.post_pr_comment(
            run.repo, run.pr_number,
            f"## ✅ QualityEngine AI — Skipped\n\n{reason}\n\n"
            f"*🤖 QualityEngine AI — no quality checks needed for this change type.*"
        )
        if run.commit_sha:
            github_api.set_commit_status(run.repo, run.commit_sha, "success", reason)
    await db_ops.update_run_status(run.run_id, RunStatus.COMPLETED)
    sse_emit(run.run_id, "complete", {"verdict": "SKIPPED", "reason": reason})
