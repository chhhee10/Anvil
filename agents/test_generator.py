"""
Test Generator Agent — Groq Llama 3.3 70B via LangChain
1. Generates pytest unit tests for the changed functions in the diff
2. Runs them in an isolated subprocess sandbox (timeout=30s)
3. Returns structured TestResult with pass/fail counts and stdout
"""
from __future__ import annotations
import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import omium
from typing import Dict, List, Optional

from langchain_core.prompts import ChatPromptTemplate
from tools.github_api import fetch_pr_source_files, get_pr_changed_files
from langchain_core.output_parsers import StrOutputParser
from agents.model_router import get_str_llm

from models.schemas import (
    AgentName, AgentStep, QualityPlan, TestResult,
    PipelineRun, StepStatus
)
import db.database as db_ops

logger = logging.getLogger("qualityengine.test_generator")

SANDBOX_TIMEOUT = 30
MAX_OUTPUT_LEN  = 3000


def _build_chain():
    llm = get_str_llm(temperature=0.3)

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a senior Python engineer writing pytest unit tests.
CRITICAL RULES:
1. Output ONLY valid Python code — zero markdown, zero explanations, zero code fences
2. The source files listed below ARE physically present on disk in the test directory
3. You MUST import from them directly (e.g. `from utils import safe_divide`)
4. Do NOT redefine or stub the functions — test the real implementations
5. Each test function name must start with test_
6. Use pytest style — no unittest.TestCase"""),
        ("human", """Write pytest unit tests for this pull request.

Repository: {repo}
Functions to test: {test_functions}

THESE SOURCE FILES ARE IN THE SANDBOX — IMPORT THEM:
{source_files}

Diff of the changes:
{diff_text}

Write 4-6 tests covering:
- Happy path with normal inputs
- Edge cases: empty input, zero, None, boundary values
- Error cases: invalid inputs that should raise exceptions

Import from the real module. Example: if utils.py is in the sandbox, write:
  from utils import safe_divide, calculate_discount

Output ONLY the Python test code. No markdown. No explanation.""")
    ])

    return prompt | llm | StrOutputParser()



_chain = None


def get_chain():
    global _chain
    if _chain is None:
        _chain = _build_chain()
    return _chain


def _write_source_files(tmpdir: str, source_files: Dict[str, str]) -> List[str]:
    """Write PR source files into the sandbox preserving relative paths."""
    written: List[str] = []
    for rel_path, content in source_files.items():
        dest = os.path.join(tmpdir, rel_path)
        dir_path = os.path.dirname(dest)
        os.makedirs(dir_path, exist_ok=True)
        
        # Create __init__.py in all parent directories to ensure importability
        current_dir = tmpdir
        for part in rel_path.split("/")[:-1]:
            current_dir = os.path.join(current_dir, part)
            init_file = os.path.join(current_dir, "__init__.py")
            if not os.path.exists(init_file):
                with open(init_file, "w") as f:
                    pass

        with open(dest, "w") as f:
            f.write(content)
        written.append(rel_path)
    return written


def _files_from_diff(diff_text: str) -> List[str]:
    paths: List[str] = []
    for line in (diff_text or "").splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path != "/dev/null" and path.endswith(".py"):
                paths.append(path)
    return paths


def _collect_source_files(run: PipelineRun, plan: QualityPlan) -> Dict[str, str]:
    """Fetch changed Python files from the PR head branch for sandbox execution."""
    if not run.repo or not run.pr_number:
        return {}

    paths = list(plan.files_to_test or [])
    if not paths:
        paths = _files_from_diff(run.diff_text or "")
    if not paths and run.pr_number:
        paths = get_pr_changed_files(run.repo, run.pr_number)

    ref = run.branch or "main"
    return fetch_pr_source_files(run.repo, paths, ref)


def _run_in_sandbox(
    test_code: str, source_files: Optional[Dict[str, str]] = None
) -> TestResult:
    """
    Execute the generated test code in an isolated subprocess.
    - Writes PR source files and test file into a temp directory
    - Runs `python3 -m pytest` with timeout
    - Captures stdout/stderr
    - Returns structured TestResult
    """
    source_files = source_files or {}

    with tempfile.TemporaryDirectory() as tmpdir:
        if source_files:
            _write_source_files(tmpdir, source_files)

        test_file = os.path.join(tmpdir, "test_generated.py")
        with open(test_file, "w") as f:
            f.write(test_code)

        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", test_file, "-v",
                 "--tb=short", "--no-header", "-q"],
                capture_output=True,
                text=True,
                timeout=SANDBOX_TIMEOUT,
                cwd=tmpdir,
                env={**os.environ, "PYTHONPATH": tmpdir},
            )

            stdout = result.stdout[:MAX_OUTPUT_LEN]
            stderr = result.stderr[:MAX_OUTPUT_LEN]

            # Parse pytest summary line: "X passed, Y failed, Z error"
            passed = _parse_count(stdout, "passed")
            failed = _parse_count(stdout, "failed")
            errors = _parse_count(stdout, "error")

            success = (failed == 0 and errors == 0 and passed > 0)

            return TestResult(
                test_code=test_code,
                passed=passed,
                failed=failed,
                errors=errors,
                stdout=stdout,
                stderr=stderr,
                success=success,
                timed_out=False,
            )

        except subprocess.TimeoutExpired:
            logger.warning("Test sandbox timed out after %ds", SANDBOX_TIMEOUT)
            return TestResult(
                test_code=test_code,
                passed=0, failed=0, errors=1,
                stdout="", stderr=f"Timed out after {SANDBOX_TIMEOUT}s",
                success=False, timed_out=True,
            )
        except Exception as e:
            logger.error("Sandbox execution error: %s", e)
            return TestResult(
                test_code=test_code,
                passed=0, failed=0, errors=1,
                stdout="", stderr=str(e),
                success=False,
            )


def _parse_count(output: str, keyword: str) -> int:
    """Extract number from pytest summary: '3 passed' → 3"""
    import re
    match = re.search(rf"(\d+)\s+{keyword}", output)
    return int(match.group(1)) if match else 0


@omium.trace("test_generator")
async def run_test_generator(
    run: PipelineRun, plan: QualityPlan
) -> tuple[TestResult, Dict[str, str]]:
    """Generate pytest tests for the PR and execute them in a sandbox."""
    step = AgentStep(
        run_id=run.run_id,
        agent=AgentName.TEST_GENERATOR,
        status=StepStatus.STARTED,
        message=f"Generating tests for {len(plan.test_functions)} functions...",
    )
    await db_ops.add_step(step)
    source_files: Dict[str, str] = {}

    # Skip if nothing to test
    if not plan.test_functions and not plan.files_to_test:
        await db_ops.complete_step(
            step.step_id, StepStatus.SKIPPED,
            "No testable functions identified by orchestrator"
        )
        return TestResult(
            test_code="# No testable functions found",
            passed=0, failed=0, errors=0,
            stdout="Skipped", stderr="",
            success=True,  # Don't block pipeline on docs-only PRs
        ), {}

    try:
        source_files = _collect_source_files(run, plan) or {}
        source_list = (
            "\n".join(f"- {p}" for p in source_files)
            or "- (none — infer imports from diff)"
        )
        logger.info("Sandbox source files: %s", list(source_files))

        # Step 1: Generate test code
        chain = get_chain()
        test_code: str = await chain.ainvoke({
            "repo":           run.repo,
            "test_functions": "\n".join(f"- {fn}" for fn in plan.test_functions) or "- (infer from diff)",
            "source_files":   source_list,
            "diff_text":      (run.diff_text or "No diff available")[:5000],
        })

        # Strip any accidental markdown fences
        test_code = _strip_markdown(test_code)
        logger.info("Generated %d chars of test code", len(test_code))

        # Step 2: Run in sandbox (blocking — run in executor to not block event loop)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: _run_in_sandbox(test_code, source_files)
        )

        status = StepStatus.COMPLETED if not result.timed_out else StepStatus.FAILED
        msg = (
            f"Tests: {result.passed} passed, {result.failed} failed, {result.errors} errors"
            + (" [TIMEOUT]" if result.timed_out else "")
        )
        await db_ops.complete_step(
            step.step_id, status, msg,
            {"passed": result.passed, "failed": result.failed,
             "errors": result.errors, "success": result.success}
        )
        logger.info("Test result: %s", msg)
        return result, source_files

    except Exception as e:
        logger.error("Test Generator failed: %s", e)
        await db_ops.complete_step(step.step_id, StepStatus.FAILED, str(e))
        return TestResult(
            test_code=f"# Generation failed: {e}",
            passed=0, failed=1, errors=0,
            stdout="", stderr=str(e),
            success=False,
        ), source_files


def _strip_markdown(code: str) -> str:
    """Remove ```python or ``` fences if the LLM added them."""
    lines = code.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)
