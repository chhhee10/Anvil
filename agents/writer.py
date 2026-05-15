"""
Writer Agent — Groq Llama 3.3 70B (mixtral for longer context)
Synthesizes research findings and code analysis into
a structured, professional intelligence report.
Supports revision mode for critic-driven rewrites.
"""
from __future__ import annotations
import os
import json
import logging
import omium
from datetime import datetime
from typing import List, Optional

from groq import Groq

from models.schemas import (
    PipelineRun, ResearchPlan, ResearchFindings, CodeAnalysis,
    AgentStep, AgentName, StepStatus
)
import db.database as db_ops

logger = logging.getLogger("newsroom.agents.writer")

_client: Optional[Groq] = None


def get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=os.environ["GROQ_API_KEY"])
    return _client


@omium.trace("run_writer")
async def run_writer(
    run: PipelineRun,
    plan: ResearchPlan,
    findings: ResearchFindings,
    code_analysis: CodeAnalysis,
    revision_requests: Optional[List[str]] = None,
    previous_draft: Optional[str] = None,
) -> str:
    """
    Generate or revise an intelligence report in markdown.
    """
    is_revision = bool(revision_requests and previous_draft)
    step = AgentStep(
        run_id=run.run_id,
        agent=AgentName.WRITER,
        status=StepStatus.STARTED,
        message="Revising report..." if is_revision else "Writing intelligence report...",
    )
    await db_ops.add_step(step)

    today = datetime.utcnow().strftime("%B %d, %Y")

    # ── Build context ──────────────────────────────────────────────────────────
    research_block = f"""RESEARCH FINDINGS:
Topic: {findings.topic}
Searches performed: {findings.searches_performed}

Key Findings:
{chr(10).join(f"- {f}" for f in findings.key_findings) or "- None identified"}

Security Advisories:
{chr(10).join(f"- {s}" for s in findings.security_advisories) or "- None identified"}

Ecosystem Trends:
{chr(10).join(f"- {t}" for t in findings.ecosystem_trends) or "- None identified"}

Related Projects:
{chr(10).join(f"- {p}" for p in findings.related_projects) or "- None"}

Research Synthesis:
{findings.synthesis or "No synthesis available."}"""

    code_block = ""
    if code_analysis.files_changed:
        code_block = f"""
CODE ANALYSIS:
Repository: {code_analysis.repo or "N/A"}
Files changed: {len(code_analysis.files_changed)}

Breaking Changes:
{chr(10).join(f"- {c}" for c in code_analysis.breaking_changes) or "- None identified"}

Security Issues:
{chr(10).join(f"- {s}" for s in code_analysis.security_issues) or "- None identified"}

Performance Notes:
{chr(10).join(f"- {p}" for p in code_analysis.performance_notes) or "- None identified"}

Code Summary:
{code_analysis.summary or "No code summary available."}"""

    # Sources
    sources_block = ""
    if findings.raw_results:
        sources_block = "\nSOURCES FOUND:\n" + "\n".join(
            f"- {r.title}: {r.url}" for r in findings.raw_results[:6]
        )

    if is_revision:
        revision_text = "\n".join(f"- {r}" for r in revision_requests)
        system_msg = "You are a senior tech writer revising an intelligence report. Improve it based on feedback. Return ONLY the improved markdown report."
        user_msg = f"""REVISION REQUESTS FROM CRITIC:
{revision_text}

PREVIOUS DRAFT:
{previous_draft[:2500]}

SUPPORTING DATA:
{research_block}
{code_block}

Write an improved version addressing ALL revision requests. Keep the markdown structure. Be specific and data-driven."""
    else:
        system_msg = "You are a senior tech intelligence analyst. Write comprehensive, actionable intelligence reports in markdown. Be specific — use actual data from research, not generic statements."
        user_msg = f"""Write a professional intelligence report for:

Event: {plan.event_type.value.upper()}
Topic: {plan.main_topic}
Summary: {plan.summary}
Repo: {plan.repo or "N/A"}
Date: {today}

{research_block}
{code_block}
{sources_block}

Write the report using this EXACT structure:

---
# 🔍 Intelligence Report: {plan.main_topic[:60]}

**Date:** {today} | **Event:** {plan.event_type.value.upper()} | **Repo:** {plan.repo or "N/A"}

---

## 📋 Executive Summary
[2-3 concrete paragraphs. Use specific facts from research. No generic filler.]

## 🔑 Key Findings
[5-7 specific, numbered findings with real data points from research]

## 🌐 Ecosystem Context
[2 paragraphs: trends, related projects, competitive landscape. Use actual names from research.]

## ⚠️ Risk Assessment
| Risk | Severity | Impact |
|------|----------|--------|
[4-5 rows with real risks from findings. Severity: 🔴 High / 🟡 Medium / 🟢 Low]

## 💻 Code Impact Analysis
{"[Based on " + str(len(code_analysis.files_changed)) + " files analyzed]" if code_analysis.files_changed else "No code changes in this event."}

## ✅ Recommended Actions
1. [Specific, immediately actionable step]
2. [Specific, immediately actionable step]
3. [Specific, immediately actionable step]
4. [Specific, immediately actionable step]

## 🔗 Sources
{sources_block or "[List sources from research]"}

---
*Generated by NewsRoom AI Multi-Agent Pipeline | Run {run.run_id[:8]}*

---

IMPORTANT: Be specific. Use real data. Avoid generic statements like "it is important to..." or "developers should consider..."."""

    # ── Generate ──────────────────────────────────────────────────────────────
    try:
        client = get_client()
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.5,
            max_tokens=3000,
        )
        report = response.choices[0].message.content.strip()

        await db_ops.complete_step(
            step.step_id, StepStatus.COMPLETED,
            f"Report written: {len(report)} chars {'(revision)' if is_revision else ''}",
            {"chars": len(report), "is_revision": is_revision},
        )
        logger.info("Writer produced %d char report", len(report))
        return report

    except Exception as e:
        logger.error("Writer failed: %s", e)
        await db_ops.complete_step(step.step_id, StepStatus.FAILED, str(e))
        return f"""# Intelligence Report: {plan.main_topic}

## Executive Summary
{plan.summary}

## Research Findings
{findings.synthesis}

## Error
Writer agent encountered an error: {e}

*Run ID: {run.run_id}*
"""
