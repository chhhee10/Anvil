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

from pydantic import BaseModel, Field
from langchain_core.prompts import ChatPromptTemplate
from agents.model_router import get_llm

from models.schemas import (
    PipelineRun, ResearchPlan, ResearchFindings, SearchResult,
    AgentStep, AgentName, StepStatus
)
from tools import tavily_search
import db.database as db_ops

logger = logging.getLogger("newsroom.agents.researcher")

class FollowupQuery(BaseModel):
    query: str = Field(description="The specific follow-up search query to dig deeper.")

class SynthesisResult(BaseModel):
    key_findings: List[str] = Field(description="4-6 most important discoveries")
    security_advisories: List[str] = Field(description="any CVEs, vulnerabilities, security issues")
    ecosystem_trends: List[str] = Field(description="broader trends in the ecosystem")
    related_projects: List[str] = Field(description="relevant tools, libraries, projects mentioned")
    synthesis: str = Field(description="3-4 paragraph executive synthesis")


def _generate_followup_query(results: List[SearchResult], hop: int, original_topic: str) -> str:
    """Use Groq to generate a smarter follow-up search query from results."""
    if not results:
        return ""
    snippets = "\n".join(f"- {r.title}: {r.content[:200]}" for r in results[:3])
    prompt = ChatPromptTemplate.from_messages([
        ("system", "You generate precise follow-up search queries for tech research."),
        ("human", f"Original topic: {original_topic}\nSearch hop {hop} found:\n{snippets}\n\nGenerate ONE specific follow-up search query that digs deeper into the most important aspect found.")
    ])
    try:
        chain = prompt | get_llm(temperature=0.4, structured_output=FollowupQuery)
        res = chain.invoke({})
        return res.query
    except Exception as e:
        logger.error("Failed to generate followup query: %s", e)
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

    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are a senior tech researcher. Synthesize search results into structured intelligence. Be specific, factual, and actionable."),
        ("human", f"Topic: {plan.main_topic}\nContext: {plan.summary}\n\nSearch Results:\n{results_text}\n\nSynthesize findings.")
    ])

    try:
        chain = prompt | get_llm(temperature=0.3, structured_output=SynthesisResult)
        data: SynthesisResult = await chain.ainvoke({})
        
        findings = ResearchFindings(
            topic=plan.main_topic,
            searches_performed=searches_done,
            key_findings=data.key_findings,
            security_advisories=data.security_advisories,
            ecosystem_trends=data.ecosystem_trends,
            related_projects=data.related_projects,
            raw_results=all_results[:10],
            synthesis=data.synthesis,
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
