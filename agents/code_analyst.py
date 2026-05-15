"""
Code Analyst Agent — Groq Llama 3.3 70B
Fetches GitHub diffs and analyzes code changes for:
- Breaking changes
- Security issues
- Performance implications
"""
from __future__ import annotations
import os
import json
import logging
import omium
from typing import Optional

from groq import Groq

from models.schemas import (
    PipelineRun, ResearchPlan, CodeAnalysis, CodeChange,
    AgentStep, AgentName, StepStatus
)
from tools import github_api
import db.database as db_ops

logger = logging.getLogger("newsroom.agents.code_analyst")

_client: Optional[Groq] = None


def get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=os.environ["GROQ_API_KEY"])
    return _client


@omium.trace("run_code_analyst")
async def run_code_analyst(run: PipelineRun, plan: ResearchPlan) -> CodeAnalysis:
    """
    Fetch code changes from GitHub and analyze them with Groq.
    Returns empty analysis if no repo/commit is present.
    """
    step = AgentStep(
        run_id=run.run_id,
        agent=AgentName.CODE_ANALYST,
        status=StepStatus.STARTED,
        message="Fetching and analyzing code changes...",
    )
    await db_ops.add_step(step)

    # ── Skip if no code context ───────────────────────────────────────────────
    if not plan.repo:
        await db_ops.complete_step(
            step.step_id, StepStatus.SKIPPED,
            "No repo provided — skipping code analysis"
        )
        return CodeAnalysis(summary="No repository context provided.")

    # ── Fetch diffs ───────────────────────────────────────────────────────────
    files_changed: list[CodeChange] = []

    if plan.commit_sha:
        files_changed = github_api.get_commit_diff(plan.repo, plan.commit_sha)
    elif plan.pr_number:
        files_changed = github_api.get_pr_files(plan.repo, plan.pr_number)

    if not files_changed:
        await db_ops.complete_step(
            step.step_id, StepStatus.COMPLETED,
            "No file changes found to analyze"
        )
        return CodeAnalysis(repo=plan.repo, summary="No file changes found.")

    # ── Prepare diff summary for LLM ─────────────────────────────────────────
    diff_text = ""
    for f in files_changed[:10]:
        diff_text += f"\n### {f.status.upper()}: {f.filename}\n"
        diff_text += f"+{f.additions} additions, -{f.deletions} deletions\n"
        if f.patch:
            diff_text += f"```diff\n{f.patch[:800]}\n```\n"

    messages = [
        {
            "role": "system",
            "content": (
                "You are a senior code reviewer and security analyst. "
                "Analyze code diffs and identify issues. Return JSON only. Be specific and concise."
            )
        },
        {
            "role": "user",
            "content": f"""Repository: {plan.repo}
Context: {plan.summary}

Code Changes:
{diff_text}

Analyze and return JSON:
{{
  "breaking_changes": ["..."],     // API breaks, removed exports, signature changes (empty if none)
  "security_issues": ["..."],       // SQL injection, XSS, secrets, auth bypasses (empty if none)
  "performance_notes": ["..."],     // N+1 queries, memory leaks, blocking I/O (empty if none)
  "summary": "..."                  // 2-3 paragraph analysis of what changed and implications
}}"""
        }
    ]

    try:
        client = get_client()
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.2,
            response_format={"type": "json_object"},
            max_tokens=1500,
        )
        data = json.loads(response.choices[0].message.content)

        analysis = CodeAnalysis(
            repo=plan.repo,
            commit_sha=plan.commit_sha,
            files_changed=files_changed,
            breaking_changes=data.get("breaking_changes", []),
            security_issues=data.get("security_issues", []),
            performance_notes=data.get("performance_notes", []),
            summary=data.get("summary", ""),
        )
        await db_ops.complete_step(
            step.step_id, StepStatus.COMPLETED,
            f"Analyzed {len(files_changed)} files: {len(analysis.security_issues)} security issues found",
            {
                "files": len(files_changed),
                "security_issues": len(analysis.security_issues),
                "breaking_changes": len(analysis.breaking_changes),
            },
        )
        return analysis

    except Exception as e:
        logger.error("Code analyst synthesis failed: %s", e)
        await db_ops.complete_step(step.step_id, StepStatus.FAILED, str(e))
        return CodeAnalysis(
            repo=plan.repo,
            files_changed=files_changed,
            summary=f"Code fetched ({len(files_changed)} files) but analysis failed: {e}",
        )
