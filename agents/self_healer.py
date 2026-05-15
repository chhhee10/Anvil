"""
Self-Healer Agent — Groq Llama 3.3 70B via LangChain
When tests fail, reads the error output and attempts to fix the generated test code.
Runs up to 3 iterations. Re-executes sandbox after each fix attempt.
Source files from the PR branch are written to sandbox so imports resolve correctly.
"""
from __future__ import annotations
import asyncio
import logging
import omium
from typing import Dict, List, Optional

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from agents.model_router import get_str_llm

from models.schemas import (
    AgentName, AgentStep, QualityPlan, TestResult,
    HealAttempt, PipelineRun, StepStatus
)
from agents.test_generator import _run_in_sandbox, _strip_markdown
import db.database as db_ops

logger = logging.getLogger("qualityengine.self_healer")

MAX_HEAL_ATTEMPTS = 3


def _build_chain():
    llm = get_str_llm(temperature=0.4)

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a senior Python engineer debugging failing pytest tests.
You will be given failing test code and the pytest error output.
The source files are physically in the sandbox directory — the imports are correct.
Your job is to fix ONLY the test code so the assertions and test logic are correct.

RULES:
1. Output ONLY valid Python code — no markdown, no explanation
2. Keep the same imports (the source files exist in sandbox)
3. Fix incorrect assertions, wrong expected values, wrong argument usage
4. Do NOT redefine the functions being tested
5. Runnable with: python3 -m pytest test_generated.py -v"""),
        ("human", """Fix these failing pytest tests.

Attempt: {attempt} of {max_attempts}

FAILING TEST CODE:
{test_code}

PYTEST ERROR OUTPUT:
{error_output}

SOURCE FILES AVAILABLE (for reference — already imported):
{source_list}

DIFF CONTEXT:
{diff_text}

Common fixes needed:
- Wrong expected values in assert statements → check actual function behavior from diff
- Wrong argument types or order → match function signatures from diff
- Missing pytest.raises context → add if testing exceptions

Output ONLY the corrected Python test code.""")
    ])

    return prompt | llm | StrOutputParser()


_chain = None


def get_chain():
    global _chain
    if _chain is None:
        _chain = _build_chain()
    return _chain


@omium.trace("self_healer")
async def run_self_healer(
    run: PipelineRun,
    plan: QualityPlan,
    initial_test_result: TestResult,
    source_files: Optional[Dict[str, str]] = None,
) -> tuple[TestResult, List[HealAttempt]]:
    """
    Attempt to fix failing tests. Returns (final_test_result, heal_attempts).
    source_files is written to the sandbox so imports resolve correctly.
    """
    source_files = source_files or {}
    step = AgentStep(
        run_id=run.run_id,
        agent=AgentName.SELF_HEALER,
        status=StepStatus.STARTED,
        message=(
            f"Tests failed ({initial_test_result.failed} failed, "
            f"{initial_test_result.errors} errors). Attempting self-heal..."
        ),
    )
    await db_ops.add_step(step)

    heal_attempts: List[HealAttempt] = []
    current_result = initial_test_result
    chain = get_chain()
    loop = asyncio.get_event_loop()
    source_list = "\n".join(f"- {p}" for p in source_files) or "- (none)"

    for attempt in range(1, MAX_HEAL_ATTEMPTS + 1):
        logger.info("Self-heal attempt %d/%d", attempt, MAX_HEAL_ATTEMPTS)

        # Build error summary: stdout + stderr, truncated
        error_output = (
            f"STDOUT:\n{current_result.stdout}\n"
            f"STDERR:\n{current_result.stderr}"
        )[:3000]

        try:
            fixed_code: str = await chain.ainvoke({
                "attempt":      str(attempt),
                "max_attempts": str(MAX_HEAL_ATTEMPTS),
                "test_code":    current_result.test_code,
                "error_output": error_output,
                "source_list":  source_list,
                "diff_text":    (run.diff_text or "")[:2000],
            })

            fixed_code = _strip_markdown(fixed_code)

            # Re-run sandbox WITH the source files so imports work
            new_result = await loop.run_in_executor(
                None, lambda fc=fixed_code: _run_in_sandbox(fc, source_files)
            )

            heal_attempts.append(HealAttempt(
                attempt=attempt,
                fix_description=_summarize_fix(fixed_code, current_result.test_code),
                fixed_code=fixed_code,
                test_result=new_result,
            ))

            current_result = new_result
            logger.info(
                "Heal attempt %d: %d passed, %d failed, %d errors",
                attempt, new_result.passed, new_result.failed, new_result.errors
            )

            if new_result.success:
                logger.info("✅ Self-healing succeeded on attempt %d", attempt)
                break

        except Exception as e:
            logger.error("Heal attempt %d failed: %s", attempt, e)
            heal_attempts.append(HealAttempt(
                attempt=attempt,
                fix_description=f"Attempt {attempt} errored: {e}",
                fixed_code=current_result.test_code,
                test_result=current_result,
            ))

    success = current_result.success
    await db_ops.complete_step(
        step.step_id,
        StepStatus.COMPLETED if success else StepStatus.FAILED,
        f"Self-healing {'succeeded' if success else 'failed'} after "
        f"{len(heal_attempts)} attempt(s). "
        f"Final: {current_result.passed}p / {current_result.failed}f",
        {"attempts": len(heal_attempts), "healed": success}
    )
    return current_result, heal_attempts


def _summarize_fix(new_code: str, old_code: str) -> str:
    new_lines = set(new_code.splitlines())
    old_lines = set(old_code.splitlines())
    added   = len(new_lines - old_lines)
    removed = len(old_lines - new_lines)
    return f"+{added} lines changed, -{removed} lines removed"
