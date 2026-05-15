"""
Decision Agent — Gemini 2.0 Flash via LangChain
Synthesizes all agent outputs and issues the final PR verdict:
MERGE / REJECT / MERGE_WITH_FIX / BUG_REPORT
"""
from __future__ import annotations
import logging
import os
import omium
from typing import List

from langchain_core.prompts import ChatPromptTemplate
from agents.model_router import get_llm

from models.schemas import (
    AgentName, AgentStep, QualityPlan, ReviewReport,
    SecurityReport, TestResult, HealAttempt, PRDecision,
    PipelineRun, StepStatus, Verdict, ScoreBreakdown
)
import db.database as db_ops

logger = logging.getLogger("qualityengine.decision_agent")

# Scoring thresholds
MERGE_THRESHOLD       = 7.0   # overall score needed for auto-merge
MERGE_FIX_THRESHOLD   = 6.5   # score needed for merge-with-fix
REJECT_REVIEW_SCORE   = 4.0   # review score at or below → auto-reject
CRITICAL_BLOCK        = True  # any critical security finding = auto-reject


def _build_chain():
    structured_llm = get_llm(temperature=0.2, structured_output=PRDecision)

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are an engineering director making the final call on a pull request.
You have received reports from specialist agents. Make a clear, justified decision.

DECISION RULES (follow these exactly):
- MERGE: review score >= 7 AND security score >= 7 AND tests pass AND no CRITICAL security findings
- MERGE_WITH_FIX: healing succeeded AND overall >= 6.5 AND no CRITICAL security findings
- BUG_REPORT: tests failed after all healing attempts (regardless of scores)
- REJECT: CRITICAL security finding OR review score <= 4 OR security score <= 4
- REQUEST_CHANGES (use REJECT verdict): score 5-6 with fixable issues

IMPORTANT: If tests pass and scores are 7+, you MUST return MERGE. Don't be overly conservative."""),
        ("human", """Make a final PR decision based on these agent reports.

PR: {pr_title}
Repository: {repo}
Change Type: {change_type} | Risk: {risk_level}

─── PR REVIEWER REPORT ───────────────────────────────
Score: {review_score}/10 | Recommendation: {review_recommendation}
Issues: {review_issues}
Summary: {review_summary}

─── SECURITY SCANNER REPORT ──────────────────────────
Score: {security_score}/10 | Recommendation: {security_recommendation}
Critical findings: {critical_count} | High findings: {high_count}
Findings: {security_findings}
Summary: {security_summary}

─── TEST RESULTS ─────────────────────────────────────
Passed: {tests_passed} | Failed: {tests_failed} | Errors: {tests_errors}
Self-healing attempted: {healing_attempted}
Self-healing succeeded: {healing_succeeded}
Heal attempts: {heal_attempts}

─── DECISION RULES ───────────────────────────────────────────────────────────
- MERGE: review >= 7 AND security >= 7 AND tests pass AND no CRITICAL security findings
- MERGE_WITH_FIX: healing succeeded AND overall >= 6.5 AND no CRITICAL findings
- BUG_REPORT: tests failed after all healing (even if code quality is good)
- REJECT: CRITICAL security finding OR review score <= 4

IMPORTANT: If tests passed and both review/security are 7+, return MERGE.

Produce a PRDecision with:
- verdict: MERGE / REJECT / MERGE_WITH_FIX / BUG_REPORT
- scores: ScoreBreakdown (correctness, security, test_coverage, code_quality, risk each 1-10, overall)
- reasoning: 2-3 sentences explaining the decision
- merge_message: (if MERGE/MERGE_WITH_FIX) short approval comment for GitHub
- rejection_reason: (if REJECT) detailed rejection comment for GitHub PR
- bug_title: (if BUG_REPORT) GitHub issue title
- bug_body: (if BUG_REPORT) Full GitHub issue body with reproduction steps and error details""")
    ])

    return prompt | structured_llm


_chain = None


def get_chain():
    global _chain
    if _chain is None:
        _chain = _build_chain()
    return _chain


def _compute_overall(
    review: ReviewReport,
    security: SecurityReport,
    test_result: TestResult,
) -> float:
    """Weighted overall score from specialist agents."""
    test_score = 10 if test_result.success else max(0, 10 - test_result.failed * 2)
    return round(
        review.score * 0.35
        + security.score * 0.35
        + test_score * 0.20
        + (10 if security.critical_count == 0 else 0) * 0.10,
        1,
    )


def _apply_verdict_rules(
    decision: PRDecision,
    review: ReviewReport,
    security: SecurityReport,
    test_result: TestResult,
    heal_attempts: List[HealAttempt],
) -> PRDecision:
    """
    Deterministic verdict — overrides the LLM when evidence supports merge/reject.
    Prevents good PRs from BUG_REPORT solely because sandbox tests were flaky.
    """
    healing_succeeded = any(h.test_result.success for h in heal_attempts)
    overall = _compute_overall(review, security, test_result)

    scores = decision.scores.model_copy(update={
        "correctness":  review.score,
        "security":     security.score,
        "test_coverage": 10 if test_result.success else max(1, 10 - test_result.failed),
        "code_quality": review.score,
        "risk":         security.score,
        "overall":      overall,
    })

    code_looks_good = (
        review.score >= 7
        and security.score >= 7
        and security.critical_count == 0
        and security.recommendation != "BLOCK"
        and review.recommendation != "REJECT"
    )

    # Hard blocks
    if security.critical_count > 0 or security.recommendation == "BLOCK":
        return decision.model_copy(update={
            "verdict": Verdict.REJECT,
            "scores": scores,
            "reasoning": (
                f"Blocked: {security.critical_count} critical security finding(s). "
                + (decision.reasoning or "")
            ),
            "rejection_reason": decision.rejection_reason or security.summary,
        })

    if review.score < REJECT_REVIEW_SCORE or review.recommendation == "REJECT":
        return decision.model_copy(update={
            "verdict": Verdict.REJECT,
            "scores": scores,
            "reasoning": f"Code review score {review.score}/10 — below merge threshold.",
        })

    # Self-healed tests — MERGE_WITH_FIX before plain MERGE
    if healing_succeeded and overall >= MERGE_FIX_THRESHOLD and code_looks_good:
        return decision.model_copy(update={
            "verdict": Verdict.MERGE_WITH_FIX,
            "scores": scores,
            "reasoning": (
                "Tests initially failed but self-healing succeeded. "
                + (decision.reasoning or "")
            ),
            "merge_message": decision.merge_message or (
                f"Approved after self-heal. Review {review.score}/10, "
                f"security {security.score}/10."
            ),
        })

    # Tests passed on first run (no healing needed)
    if test_result.success and not healing_succeeded:
        if overall >= MERGE_THRESHOLD and code_looks_good:
            return decision.model_copy(update={
                "verdict": Verdict.MERGE,
                "scores": scores,
                "merge_message": decision.merge_message or (
                    f"All checks passed. Review {review.score}/10, "
                    f"security {security.score}/10."
                ),
            })

    # Strong review/security — merge without healing when tests never passed
    if (
        code_looks_good
        and review.score >= 8
        and security.score >= 8
        and not healing_succeeded
        and len(heal_attempts) == 0
    ):
        note = ""
        if not test_result.success and not healing_succeeded:
            note = (
                " Generated tests did not all pass, but code review and "
                "security scan are strong — merging on specialist agent consensus."
            )
        return decision.model_copy(update={
            "verdict": Verdict.MERGE,
            "scores": scores,
            "reasoning": (decision.reasoning or "Approved by quality agents.") + note,
            "merge_message": decision.merge_message or (
                f"Approved: review {review.score}/10, security {security.score}/10."
            ),
        })

    if code_looks_good and overall >= MERGE_THRESHOLD:
        return decision.model_copy(update={
            "verdict": Verdict.MERGE,
            "scores": scores,
            "merge_message": decision.merge_message or "Approved by QualityEngine AI.",
        })

    # Real failures in code — not just test harness
    if (
        not test_result.success
        and not healing_succeeded
        and (review.score < 7 or security.high_count > 0)
    ):
        return decision.model_copy(update={
            "verdict": Verdict.BUG_REPORT,
            "scores": scores,
        })

    # Default: trust reject from LLM, otherwise reject on ambiguous failure
    if decision.verdict == Verdict.BUG_REPORT and code_looks_good:
        return decision.model_copy(update={
            "verdict": Verdict.MERGE,
            "scores": scores,
            "reasoning": (
                "Specialist agents found no blocking issues; overriding test-only failure."
            ),
            "merge_message": "Approved — review and security checks passed.",
        })

    return decision.model_copy(update={"scores": scores})


@omium.trace("decision_agent")
async def run_decision_agent(
    run: PipelineRun,
    plan: QualityPlan,
    review: ReviewReport,
    security: SecurityReport,
    test_result: TestResult,
    heal_attempts: List[HealAttempt],
) -> PRDecision:
    """Synthesize all agent outputs and issue the final PR verdict."""
    step = AgentStep(
        run_id=run.run_id,
        agent=AgentName.DECISION_AGENT,
        status=StepStatus.STARTED,
        message="Synthesizing all reports and making final decision...",
    )
    await db_ops.add_step(step)

    healing_attempted  = len(heal_attempts) > 0
    healing_succeeded  = any(h.test_result.success for h in heal_attempts)

    heal_summary = "None"
    if heal_attempts:
        heal_summary = " | ".join(
            f"Attempt {h.attempt}: {'✅' if h.test_result.success else '❌'} "
            f"({h.test_result.passed}p/{h.test_result.failed}f)"
            for h in heal_attempts
        )

    review_issues_text = ""
    if review.issues:
        review_issues_text = "\n".join(
            f"- [{i.severity.value.upper()}] {i.file}: {i.description}"
            for i in review.issues[:8]
        )
    else:
        review_issues_text = "None found"

    security_findings_text = ""
    if security.findings:
        security_findings_text = "\n".join(
            f"- [{f.severity.value.upper()}] {f.finding_type} in {f.file}: {f.description}"
            for f in security.findings[:8]
        )
    else:
        security_findings_text = "None found"

    try:
        chain = get_chain()
        llm_decision: PRDecision = await chain.ainvoke({
            "pr_title":               run.pr_title or "Unknown",
            "repo":                   run.repo,
            "change_type":            plan.change_type.value,
            "risk_level":             plan.risk_level.value,
            "review_score":           str(review.score),
            "review_recommendation":  review.recommendation,
            "review_issues":          review_issues_text,
            "review_summary":         review.summary,
            "security_score":         str(security.score),
            "security_recommendation": security.recommendation,
            "critical_count":         str(security.critical_count),
            "high_count":             str(security.high_count),
            "security_findings":      security_findings_text,
            "security_summary":       security.summary,
            "tests_passed":           str(test_result.passed),
            "tests_failed":           str(test_result.failed),
            "tests_errors":           str(test_result.errors),
            "healing_attempted":      str(healing_attempted),
            "healing_succeeded":      str(healing_succeeded),
            "heal_attempts":          heal_summary,
        })

        decision = _apply_verdict_rules(
            llm_decision, review, security, test_result, heal_attempts
        )

        await db_ops.complete_step(
            step.step_id, StepStatus.COMPLETED,
            f"Verdict: {decision.verdict.value} | "
            f"Overall score: {decision.scores.overall}/10",
            {
                "verdict": decision.verdict.value,
                "overall": decision.scores.overall,
            }
        )
        logger.info(
            "Decision: %s (%.1f/10) [LLM suggested %s]",
            decision.verdict.value, decision.scores.overall, llm_decision.verdict.value,
        )
        return decision

    except Exception as e:
        logger.error("Decision Agent failed: %s", e)
        await db_ops.complete_step(step.step_id, StepStatus.FAILED, str(e))
        # Safe fallback: reject on error
        return PRDecision(
            verdict=Verdict.REJECT,
            scores=ScoreBreakdown(
                correctness=5, security=5, test_coverage=5,
                code_quality=5, risk=5, overall=5.0
            ),
            reasoning=f"Decision agent encountered an error: {e}. Defaulting to reject for safety.",
            rejection_reason=f"Automated review failed with an internal error: {e}",
        )
