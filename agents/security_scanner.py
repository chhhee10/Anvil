"""
Security Scanner Agent — Groq Llama 3.3 70B via LangChain
Runs in PARALLEL with PR Reviewer via RunnableParallel.
Scans the diff for secrets, vulnerabilities, and unsafe patterns.
"""
from __future__ import annotations
import logging
import os
import omium

from langchain_core.prompts import ChatPromptTemplate
from agents.model_router import get_llm

from models.schemas import (
    AgentName, AgentStep, QualityPlan, SecurityReport,
    PipelineRun, StepStatus, SecurityFinding, IssueSeverity
)
import db.database as db_ops

logger = logging.getLogger("qualityengine.security_scanner")


def _build_chain():
    structured_llm = get_llm(temperature=0.1, structured_output=SecurityReport)

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are an application security engineer (AppSec) performing a security audit of a code diff.
Be thorough. Look for:
- Hardcoded secrets, API keys, passwords, tokens
- SQL injection, NoSQL injection
- XSS / template injection
- Insecure deserialization
- Path traversal
- Auth bypasses / missing authentication checks
- Unsafe use of eval(), exec(), subprocess with shell=True
- Vulnerable or outdated dependency imports
- Missing input validation
- Sensitive data in logs

Score 10 if no issues found. Be precise — cite actual code from the diff."""),
        ("human", """Perform a security audit of this pull request diff.

Repository: {repo}
Security Focus Areas (from orchestrator):
{security_focus}

Live Web Research Context (Use this to inform your security audit if relevant):
{live_web_context}

Unified Diff:
{diff_text}

Produce a SecurityReport with:
- score: 1-10 (10 = perfectly secure, no issues)
- findings: list of SecurityFinding objects (finding_type, severity, file, description, line_hint)
- critical_count: number of critical severity findings
- high_count: number of high severity findings
- summary: 2-3 sentence security assessment
- recommendation: PASS / REVIEW / BLOCK""")
    ])

    return prompt | structured_llm


_chain = None


def get_chain():
    global _chain
    if _chain is None:
        _chain = _build_chain()
    return _chain


@omium.trace("security_scanner")
async def run_security_scanner(run: PipelineRun, plan: QualityPlan) -> SecurityReport:
    """Scan the PR diff for security vulnerabilities."""
    step = AgentStep(
        run_id=run.run_id,
        agent=AgentName.SECURITY_SCANNER,
        status=StepStatus.STARTED,
        message="Scanning for security vulnerabilities...",
    )
    await db_ops.add_step(step)

    try:
        chain = get_chain()
        research_context = run.research_findings.synthesis if run.research_findings else "No additional web research conducted."
        
        report: SecurityReport = await chain.ainvoke({
            "repo":           run.repo,
            "security_focus": "\n".join(f"- {f}" for f in plan.security_focus),
            "live_web_context": research_context,
            "diff_text":      (run.diff_text or "No diff available")[:6000],
        })

        await db_ops.complete_step(
            step.step_id, StepStatus.COMPLETED,
            f"Security score: {report.score}/10 | {len(report.findings)} findings "
            f"({report.critical_count} critical, {report.high_count} high) | {report.recommendation}",
            {
                "score":          report.score,
                "findings":       len(report.findings),
                "critical":       report.critical_count,
                "recommendation": report.recommendation,
            }
        )
        logger.info("Security scan: score=%d/10 | %s", report.score, report.recommendation)
        return report

    except Exception as e:
        logger.error("Security Scanner failed: %s", e)
        await db_ops.complete_step(step.step_id, StepStatus.FAILED, str(e))
        return SecurityReport(
            score=5,
            findings=[],
            critical_count=0,
            high_count=0,
            summary=f"Security scan failed: {e}",
            recommendation="REVIEW",
        )
