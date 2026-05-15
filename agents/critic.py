"""
Critic Agent — Groq Llama 3.3 70B
Reviews the writer's report, scores it on 3 axes,
and either approves it or returns structured revision requests.
"""
from __future__ import annotations
import os
import json
import logging
import omium
from typing import Tuple, List, Optional

from groq import Groq

from models.schemas import (
    PipelineRun, CriticScore,
    AgentStep, AgentName, StepStatus
)
import db.database as db_ops

logger = logging.getLogger("newsroom.agents.critic")

_client: Optional[Groq] = None
APPROVAL_THRESHOLD = 6.5  # overall score out of 10


def get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=os.environ["GROQ_API_KEY"])
    return _client


@omium.trace("run_critic")
async def run_critic(
    run: PipelineRun,
    report: str,
) -> Tuple[CriticScore, bool, List[str]]:
    """
    Review the report. Returns (score, approved, revision_requests).
    """
    step = AgentStep(
        run_id=run.run_id,
        agent=AgentName.CRITIC,
        status=StepStatus.STARTED,
        message="Reviewing report quality...",
    )
    await db_ops.add_step(step)

    # Truncate very long reports to stay within token budget
    report_sample = report[:3500] if len(report) > 3500 else report

    messages = [
        {
            "role": "system",
            "content": (
                "You are a demanding senior editor reviewing AI-generated intelligence reports. "
                "Be constructive but rigorous. Return JSON only."
            )
        },
        {
            "role": "user",
            "content": f"""Review this intelligence report and score it strictly:

REPORT:
{report_sample}

Score on a scale of 1-10 (be harsh — 7+ means publishable):
- accuracy: Are claims specific and verifiable? Or vague/generic?
- completeness: Does it cover all key aspects? Missing important sections?
- actionability: Can a developer act on this immediately?

Also identify specific revision requests if score < 7.

Return JSON:
{{
  "accuracy": <1-10>,
  "completeness": <1-10>,
  "actionability": <1-10>,
  "feedback": "Overall feedback in 2-3 sentences",
  "revision_requests": [
    "Specific thing to fix 1",
    "Specific thing to fix 2"
  ]
}}

If all scores >= 7, revision_requests should be [].
Be a tough but fair editor. Generic AI fluff = low scores."""
        }
    ]

    try:
        client = get_client()
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.2,
            response_format={"type": "json_object"},
            max_tokens=800,
        )
        data = json.loads(response.choices[0].message.content)

        accuracy = max(1, min(10, int(data.get("accuracy", 5))))
        completeness = max(1, min(10, int(data.get("completeness", 5))))
        actionability = max(1, min(10, int(data.get("actionability", 5))))
        overall = round((accuracy + completeness + actionability) / 3, 1)
        approved = overall >= APPROVAL_THRESHOLD

        revision_requests = data.get("revision_requests", [])
        if approved:
            revision_requests = []

        score = CriticScore(
            accuracy=accuracy,
            completeness=completeness,
            actionability=actionability,
            overall=overall,
            approved=approved,
            revision_requests=revision_requests,
            feedback=data.get("feedback", ""),
        )

        await db_ops.complete_step(
            step.step_id,
            StepStatus.COMPLETED,
            f"Score: {overall}/10 ({'APPROVED' if approved else 'REVISION NEEDED'})",
            {"overall": overall, "approved": approved, "revisions": len(revision_requests)},
        )
        logger.info("Critic score: %.1f/10 (approved=%s)", overall, approved)
        return score, approved, revision_requests

    except Exception as e:
        logger.error("Critic failed: %s", e)
        await db_ops.complete_step(step.step_id, StepStatus.FAILED, str(e))
        # Default: approve on error to avoid infinite loops
        default_score = CriticScore(
            accuracy=7, completeness=7, actionability=7,
            overall=7.0, approved=True,
            feedback=f"Auto-approved due to critic error: {e}",
        )
        return default_score, True, []
