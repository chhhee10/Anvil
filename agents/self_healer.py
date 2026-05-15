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
    llm = get_str_llm(temperature=0.3)

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a senior Python engineer debugging failing tests.
Your job is to fix the bug in either the test code OR the source code.

RULES:
1. If the test logic is wrong, fix the test code.
2. If the source code is genuinely broken (e.g. throws error, bad math, missing logic), fix the source code!
3. Output the FULL contents of any file you change. Do not omit code.

FORMAT INSTRUCTIONS:
For each file you change, you MUST use this exact format:
### FILE: test_generated.py
```python
<full test code>
```
### FILE: math_utils.py
```python
<full source code>
```
Output NOTHING else except these blocks."""),
        ("human", """Fix these failing pytest tests.

Attempt: {attempt} of {max_attempts}

FAILING TEST CODE:
{test_code}

PYTEST ERROR OUTPUT:
{error_output}

SOURCE FILES AVAILABLE (for reference or modification):
{source_list}

DIFF CONTEXT:
{diff_text}

Analyze the error. Output the fixed files using the exact format requested.""")
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
            # We need to pass the ACTUAL contents of source_files so the LLM can modify them
            source_list_verbose = "\n\n".join(f"--- {name} ---\n{content}" for name, content in source_files.items())

            output_text: str = await chain.ainvoke({
                "attempt":      str(attempt),
                "max_attempts": str(MAX_HEAL_ATTEMPTS),
                "test_code":    current_result.test_code,
                "error_output": error_output,
                "source_list":  source_list_verbose,
                "diff_text":    (run.diff_text or "")[:2000],
            })

            # Custom parser
            import re
            blocks = re.split(r'### FILE:\s*([^\n]+)', output_text)
            fixed_test_code = current_result.test_code
            found_source_fixes = {}

            for i in range(1, len(blocks), 2):
                fname = blocks[i].strip()
                code_block = blocks[i+1]
                code = _strip_markdown(code_block)
                if fname == "test_generated.py":
                    fixed_test_code = code
                else:
                    found_source_fixes[fname] = code
            
            # Apply any source code fixes to our sandbox memory
            if found_source_fixes:
                for fname, fcontent in found_source_fixes.items():
                    if fname in source_files:
                        source_files[fname] = fcontent
                        logger.info("Healer modified source file: %s", fname)
                        
                        run.metadata = run.metadata if hasattr(run, "metadata") else {}
                        run.metadata[f"healed_source_{fname}"] = fcontent

            # Re-run sandbox WITH the source files so imports work
            new_result = await loop.run_in_executor(
                None, lambda fc=fixed_test_code: _run_in_sandbox(fc, source_files)
            )

            heal_attempts.append(HealAttempt(
                attempt=attempt,
                fix_description=f"Patched test_generated.py and {list(found_source_fixes.keys())}",
                fixed_code=fixed_test_code,
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
