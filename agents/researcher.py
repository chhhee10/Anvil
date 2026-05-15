"""
Researcher Agent — Groq Llama 3.3 70B
Performs multi-hop web searches via Tavily and synthesizes findings
into a structured ResearchFindings object.
"""
from __future__ import annotations
import os
import json
import logging
import omium
from typing import List, Optional

from groq import Groq

from models.schemas import (
    PipelineRun, ResearchPlan, ResearchFindings, SearchResult,
    AgentStep, AgentName, StepStatus
)
from tools import tavily_search
import db.database as db_ops

logger = logging.getLogger("newsroom.agents.researcher")

_client: Optional[Groq] = None


def get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=os.environ["GROQ_API_KEY"])
    return _client


def _groq_json(messages: list, temperature: float = 0.3) -> dict:
    """Call Groq and parse JSON response."""
    client = get_client()
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=messages,
        temperature=temperature,
        response_format={"type": "json_object"},
        max_tokens=2048,
    )
    return json.loads(response.choices[0].message.content)


def _generate_followup_query(results: List[SearchResult], hop: int, original_topic: str) -> str:
    """Use Groq to generate a smarter follow-up search query from results."""
    if not results:
        return ""
    snippets = "\n".join(f"- {r.title}: {r.content[:200]}" for r in results[:3])
    messages = [
        {"role": "system", "content": "You generate precise follow-up search queries for tech research. Return JSON only."},
        {"role": "user", "content": f"""Original topic: {original_topic}
Search hop {hop} found:
{snippets}

Generate ONE specific follow-up search query that digs deeper into the most important aspect found.
Return JSON: {{"query": "..."}}"""}
    ]
    try:
        data = _groq_json(messages, temperature=0.4)
        return data.get("query", "")
    except Exception:
        return ""


@omium.trace("run_researcher")
async def run_researcher(run: PipelineRun, plan: ResearchPlan) -> ResearchFindings:
    """
    Execute research tasks:
    1. Multi-hop Tavily searches for each research query
    2. Synthesize all findings with Groq
    """
    step = AgentStep(
        run_id=run.run_id,
        agent=AgentName.RESEARCHER,
        status=StepStatus.STARTED,
        message=f"Starting research on: {plan.main_topic}",
    )
    await db_ops.add_step(step)

    all_results: List[SearchResult] = []
    searches_done = 0

    # ── Multi-hop searches ────────────────────────────────────────────────────
    for task in plan.research_tasks[:4]:
        logger.info("Research task: %s", task.query)

        def make_followup(topic=plan.main_topic):
            def followup(results, hop):
                return _generate_followup_query(results, hop, topic)
            return followup

        hop_results = tavily_search.multi_hop_search(
            initial_query=task.query,
            follow_up_fn=make_followup(),
            max_hops=2,
        )
        all_results.extend(hop_results)
        searches_done += 1

    if not all_results:
        await db_ops.complete_step(step.step_id, StepStatus.FAILED, "No search results returned")
        return ResearchFindings(topic=plan.main_topic, synthesis="No research results available.")

    # ── Synthesis ─────────────────────────────────────────────────────────────
    results_text = ""
    for i, r in enumerate(all_results[:15]):  # cap at 15 for token budget
        results_text += f"\n[{i+1}] {r.title}\nURL: {r.url}\n{r.content[:600]}\n"

    messages = [
        {
            "role": "system",
            "content": (
                "You are a senior tech researcher. Synthesize search results into structured intelligence. "
                "Return JSON only. Be specific, factual, and actionable."
            )
        },
        {
            "role": "user",
            "content": f"""Topic: {plan.main_topic}
Context: {plan.summary}

Search Results:
{results_text}

Synthesize findings into JSON:
{{
  "key_findings": ["..."],          // 4-6 most important discoveries
  "security_advisories": ["..."],   // any CVEs, vulnerabilities, security issues (can be empty [])
  "ecosystem_trends": ["..."],      // broader trends in the ecosystem
  "related_projects": ["..."],      // relevant tools, libraries, projects mentioned
  "synthesis": "..."                // 3-4 paragraph executive synthesis
}}"""
        }
    ]

    try:
        data = _groq_json(messages)
        findings = ResearchFindings(
            topic=plan.main_topic,
            searches_performed=searches_done,
            key_findings=data.get("key_findings", []),
            security_advisories=data.get("security_advisories", []),
            ecosystem_trends=data.get("ecosystem_trends", []),
            related_projects=data.get("related_projects", []),
            raw_results=all_results[:10],
            synthesis=data.get("synthesis", ""),
        )
        await db_ops.complete_step(
            step.step_id, StepStatus.COMPLETED,
            f"Research complete: {len(findings.key_findings)} findings, {searches_done} searches",
            {"searches": searches_done, "results_count": len(all_results)},
        )
        return findings

    except Exception as e:
        logger.error("Researcher synthesis failed: %s", e)
        await db_ops.complete_step(step.step_id, StepStatus.FAILED, str(e))
        return ResearchFindings(
            topic=plan.main_topic,
            searches_performed=searches_done,
            raw_results=all_results[:5],
            synthesis=f"Research gathered {len(all_results)} results but synthesis failed: {e}",
        )
