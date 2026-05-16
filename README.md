# PullSmith — Autonomous PR Quality Engineering Pipeline

> **A GitHub pull request lands. Seconds later, it has been read, researched, security-audited, tested, self-healed if broken, judged, and either merged, rejected, or filed as a bug — with no human in the loop.**

PullSmith is a production-grade, multi-agent autonomous pipeline that eliminates the bottleneck of human code review for qualifying pull requests. A GitHub webhook fires the moment a PR opens. Seven specialized AI agents collaborate — in parallel and in sequence — to deliver a complete quality verdict: review score, security audit, generated+executed test suite, up to three self-healing attempts on failing tests, and a final MERGE / REJECT / MERGE_WITH_FIX / BUG_REPORT decision posted directly back to GitHub. Every step is traced end-to-end in Omium, giving you a live, causal record of exactly what happened and why.

---

## Screenshots

### Hero Dashboard
![Hero Dashboard](https://raw.githubusercontent.com/Vijeta-Patel/ANVIL/main/dashboard/img/hero.png)

### Activity Log — Live Pipeline Run
![Activity Log](https://raw.githubusercontent.com/Vijeta-Patel/ANVIL/main/dashboard/img/activitylog.png)

### Omium Trace View
![Omium Trace](https://raw.githubusercontent.com/Vijeta-Patel/ANVIL/main/dashboard/img/omium.png)

---

## Table of Contents

1. [Why Omium](#why-omium)
2. [How PullSmith Solves Multi-Agent Autonomy End-to-End](#how-pullsmith-solves-multi-agent-autonomy-end-to-end)
3. [Architecture Overview](#architecture-overview)
4. [Agent Roster](#agent-roster)
5. [Workflow Deep-Dive](#workflow-deep-dive)
6. [Tool Surface](#tool-surface)
7. [Model Routing & Fallback Chain](#model-routing--fallback-chain)
8. [Data Flow](#data-flow)
9. [Webhook Contract](#webhook-contract)
10. [Quickstart](#quickstart)
11. [Environment Variables](#environment-variables)
12. [Project Structure](#project-structure)
13. [Dependencies](#dependencies)

---

## Why Omium

Every multi-agent system faces the same invisible problem: **you can see the output, but you cannot see the reasoning**. When PullSmith merges a PR, or rejects one, or files a bug — you need to know *why* the decision was made, *which agent* produced each signal, and *how* each tool call influenced the final verdict. Without that, the system is a black box that engineers will not trust.

Omium is PullSmith's observability spine. Every agent function is decorated with `@omium.trace(...)`, which means:

- **Every agent invocation** — Orchestrator, Researcher, PR Reviewer, Security Scanner, Test Generator, Self-Healer, Decision Agent — produces a named, timestamped span in the Omium dashboard.
- **Causal threading is preserved**: the parent `full_pipeline` trace links to each child agent trace, which links to its tool calls. A webhook-triggered run and its entire downstream agent tree are visually connected.
- **The dashboard matches the demo exactly**: every action that happened in the product appears in Omium — no untraced side-effects, no orphaned spans.
- **Decisions are inspectable**: when the Decision Agent overrides an LLM verdict with deterministic rule logic (e.g. upgrading a BUG_REPORT to MERGE because code quality was high), that override appears in the trace alongside the original LLM output, making the system's reasoning fully auditable.

Omium turns PullSmith from a powerful black box into a **verifiable, debuggable engineering system** — which is the difference between a hackathon demo and something a real team would run in production.

---

## How PullSmith Solves Multi-Agent Autonomy End-to-End

The brief demands autonomous, long-running, multi-agent work that ships real results without human steering. Here is precisely how PullSmith delivers each requirement:

**Multi-Agent, not retry-loop.** Seven agents with distinct roles, distinct models, and distinct tools operate on the same PR. The Orchestrator plans. The Researcher gathers live intelligence. The PR Reviewer and Security Scanner run in true `asyncio.gather` parallelism. The Test Generator writes and executes code. The Self-Healer iterates. The Decision Agent synthesizes. No single LLM does everything; each agent has one job and does it well.

**Autonomous execution.** The entire pipeline runs as a FastAPI `BackgroundTask`. GitHub fires the webhook; the server returns HTTP 202 immediately; the seven-agent chain runs to completion — writing GitHub comments, setting commit statuses, merging or closing PRs, creating bug issues — without a human touching anything. The only human input is the PR itself.

**Long-running and crash-safe.** All pipeline state (`PipelineRun`, `AgentStep`, `PRDecision`) is persisted to SQLite via `aiosqlite`. On server restart, any interrupted runs are detected and marked `FAILED` rather than left in a zombie pending state. The Self-Healer runs up to three full test→fix→re-execute cycles within a single pipeline run, each cycle executing a real subprocess sandbox.

**Deep reasoning.** The Orchestrator does not just classify the PR — it produces a full `QualityPlan`: change type, risk level, specific files to test, precise function names, security focus areas, and an optional multi-hop research plan. The Decision Agent receives all specialist reports and applies both LLM synthesis *and* deterministic override rules, preventing the LLM from making contradictory decisions on evidence that clearly supports one verdict.

**Tool calling.** Real tools with real side effects: GitHub API (post comments, set commit statuses, merge PRs, close PRs, create issues, push healed source files to branches), Tavily (live multi-hop web search), subprocess sandbox (execute generated pytest suites), file system (write source + test files to temp directories).

**Web search.** The Researcher runs multi-hop Tavily searches — the Orchestrator identifies the library or API the PR touches, generates targeted queries, and the Researcher performs up to 2 hops per query, using an LLM to generate smarter follow-up queries from the first results. Findings feed directly into the PR Reviewer and Security Scanner prompts as live intelligence.

**Webhooks.** GitHub sends `pull_request` events (opened, synchronize, reopened) and `issue_comment` events (`/re-review` trigger) to `/webhook/github`. HMAC-SHA256 signature verification on every request. The server returns 202 before the pipeline touches a single agent.

**Async orchestration.** PR Reviewer and Security Scanner run in `asyncio.gather` — true fan-out parallelism. The Self-Healer iterates asynchronously, re-executing the sandbox and re-invoking the LLM on each cycle. SSE (Server-Sent Events) push live progress to the dashboard throughout the entire run without polling.

---

## Architecture Overview

```
GitHub PR Event
      │
      ▼
┌─────────────────────────────────────────────────────┐
│              FastAPI Server (main.py)               │
│  HMAC-verified webhook → BackgroundTask             │
│  SSE event bus → live dashboard push                │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
            ┌──────────────────┐
            │   Orchestrator   │  Gemini 2.0 Flash
            │  (QualityPlan)   │  Plans the entire review
            └────────┬─────────┘
                     │
          ┌──────────┴──────────┐
          │   (conditional)     │
          ▼                     │
   ┌─────────────┐              │
   │  Researcher  │ Groq 70B    │
   │ (web search) │ Tavily      │
   └──────┬──────┘              │
          └──────────┬──────────┘
                     │
          ┌──────────┴──────────┐
          │   asyncio.gather    │   Fan-out parallelism
          │                     │
    ┌─────▼──────┐    ┌─────────▼───────┐
    │ PR Reviewer│    │ Security Scanner│
    │  Groq 70B  │    │   Groq 70B      │
    └─────┬──────┘    └─────────┬───────┘
          └──────────┬──────────┘
                     │
            ┌────────▼────────┐
            │ Test Generator  │  Groq 70B
            │  + Sandbox exec │  subprocess / pytest
            └────────┬────────┘
                     │
           ┌─────────▼──────────┐
           │    Self-Healer     │  Groq 70B (up to 3×)
           │  (only on failure) │
           └─────────┬──────────┘
                     │
            ┌────────▼────────┐
            │ Decision Agent  │  Groq 70B + deterministic
            │ MERGE/REJECT/   │  override rules
            │ FIX / BUG_RPT   │
            └────────┬────────┘
                     │
            ┌────────▼────────┐
            │  GitHub Action  │  Real side effects
            │ comment/merge/  │
            │ close/issue     │
            └─────────────────┘
                     │
            ┌────────▼────────┐
            │    Omium SDK    │  Full causal trace
            │  (every step)   │
            └─────────────────┘
```

---

## Agent Roster

### 1. Orchestrator (`agents/orchestrator.py`)
**Model:** Gemini 2.0 Flash via LangChain  
**Role:** Engineering Lead

The entry point of every pipeline run. Receives the raw PR diff and produces a structured `QualityPlan` — a typed Pydantic schema that drives every downstream agent. It classifies the change type (`feature`, `bug_fix`, `refactor`, `config`, `dependency`, `docs`), assigns a risk level (`low`, `medium`, `high`, `critical`), identifies exact file paths and function names that need testing, produces specific review and security focus areas, and — when the PR touches a third-party library or complex API — generates a targeted research plan for the Researcher. If the PR is docs-only or trivial, it sets `skip_reason` and the pipeline short-circuits with a GitHub comment.

**Why Gemini here:** Gemini 2.0 Flash handles structured output extraction from long diffs reliably and has a large enough context window to ingest full PR diffs without truncation concerns.

---

### 2. Researcher (`agents/researcher.py`)
**Model:** Groq Llama 3.3 70B · Tool: Tavily Search  
**Role:** Intelligence Analyst

Only invoked when the Orchestrator's `QualityPlan` contains a `research_plan`. Executes multi-hop web searches: for each research task query, it runs an initial Tavily search, then uses the LLM to generate a smarter follow-up query from the results, then runs a second hop. Up to 4 research tasks, 2 hops each. Synthesizes all raw results into a structured `ResearchFindings` object — key findings, security advisories, ecosystem trends, related projects, and a 3-4 paragraph executive synthesis. These findings are injected into both the PR Reviewer and Security Scanner prompts as live web intelligence.

---

### 3. PR Reviewer (`agents/pr_reviewer.py`)
**Model:** Groq Llama 3.3 70B  
**Role:** Senior Software Engineer

Performs structured code review against the diff, guided by the Orchestrator's `review_focus` areas and enriched with the Researcher's live web findings. Produces a `ReviewReport` with a calibrated 1-10 score, typed `CodeIssue` objects with severity levels, a list of strengths, a summary, and a recommendation (`APPROVE` / `REQUEST_CHANGES` / `REJECT`). Runs in **parallel** with the Security Scanner via `asyncio.gather`, cutting wall-clock review time in half.

The scoring rubric is explicit: 9-10 means production-ready, 7-8 means good with minor issues, 5-6 means real bugs present, 3-4 means major problems, 1-2 means critical failures. The prompt explicitly prohibits under-scoring clean code.

---

### 4. Security Scanner (`agents/security_scanner.py`)
**Model:** Groq Llama 3.3 70B  
**Role:** Application Security Engineer (AppSec)

Runs in parallel with the PR Reviewer. Audits the diff for hardcoded secrets, SQL/NoSQL injection, XSS, insecure deserialization, path traversal, auth bypasses, unsafe `eval()`/`exec()`/`subprocess(shell=True)` usage, missing input validation, and sensitive data in logs. Uses Omium tracing and enriches its analysis with live web research context from the Researcher. Produces a `SecurityReport` with a score, typed `SecurityFinding` objects, critical/high counts, and a recommendation (`PASS` / `REVIEW` / `BLOCK`). A single critical finding triggers an automatic `REJECT` verdict regardless of other scores.

---

### 5. Test Generator (`agents/test_generator.py`)
**Model:** Groq Llama 3.3 70B  
**Role:** QA Engineer + Sandbox Executor

Fetches the actual source files from the PR branch via the GitHub API, writes them to a temporary directory, generates a `pytest` test suite targeting the exact function names identified by the Orchestrator, writes the test file to the same temp directory, and executes it in an isolated subprocess with a 30-second timeout. Returns a structured `TestResult` with pass/fail/error counts, stdout, and a `timed_out` flag. The source files written to disk are passed forward to the Self-Healer so it can fix both the tests *and* the source code if needed.

---

### 6. Self-Healer (`agents/self_healer.py`)
**Model:** Groq Llama 3.3 70B  
**Role:** Debugging Engineer

Only invoked when the Test Generator reports failures. Receives the failing test code, the full pytest error output, and all source files available in the sandbox. Analyzes whether the bug is in the test logic or the source code, then outputs corrected full file contents for every file it changes using a strict `### FILE: filename.py` format. Re-executes the sandbox after each fix. Runs up to 3 attempts. If it succeeds, the fixed source files are pushed back to the PR branch via the GitHub API. Each attempt is recorded as a `HealAttempt` object passed to the Decision Agent.

---

### 7. Decision Agent (`agents/decision_agent.py`)
**Model:** Groq Llama 3.3 70B + deterministic override layer  
**Role:** Engineering Director

The final synthesizer. Receives all outputs — `QualityPlan`, `ReviewReport`, `SecurityReport`, `TestResult`, and `List[HealAttempt]` — and issues one of four verdicts:

| Verdict | Condition |
|---|---|
| `MERGE` | Review ≥ 6, Security ≥ 6, tests pass, no critical findings |
| `MERGE_WITH_FIX` | Self-healing succeeded, overall ≥ 6.0, no critical findings |
| `REJECT` | Critical security finding, or review score ≤ 4 |
| `BUG_REPORT` | Tests failed after all healing attempts |

Critically, the LLM verdict is run through `_apply_verdict_rules()` — a deterministic function that overrides the LLM when the evidence unambiguously supports a specific outcome. This prevents the LLM from issuing `BUG_REPORT` on a PR with 8/10 review and 8/10 security scores just because the sandbox was flaky. The Decision Agent also computes a weighted `ScoreBreakdown` (correctness 35%, security 35%, test coverage 20%, critical-finding penalty 10%).

---

## Workflow Deep-Dive

### Step 0: Webhook Ingestion
GitHub fires a `pull_request` event. PullSmith verifies the HMAC-SHA256 signature against `GITHUB_WEBHOOK_SECRET`, extracts the repo, PR number, branch, and commit SHA, fetches the full unified diff and PR metadata via the GitHub API, creates a `PipelineRun` record in SQLite, sets the GitHub commit status to `pending`, and spawns the pipeline as a `BackgroundTask`. HTTP 202 returned immediately.

### Step 1: Orchestration
The Orchestrator reads the diff (up to 8,000 characters) and produces a `QualityPlan`. The plan is emitted over SSE to the live dashboard. If `skip_reason` is set (docs-only, trivial change), PullSmith posts a GitHub comment and exits.

### Step 1.5: Web Research (conditional)
If `research_plan` is present in the `QualityPlan`, the Researcher executes multi-hop Tavily searches and synthesizes findings. Findings are stored on the `PipelineRun` and injected into downstream agent prompts.

### Step 2: Parallel Fan-Out
`asyncio.gather(pr_reviewer.run_pr_reviewer(...), security_scanner.run_security_scanner(...))` — both agents run concurrently. SSE events push their results to the dashboard as they complete.

### Step 3: Test Generation and Execution
The Test Generator fetches source files from GitHub, writes them to a temp sandbox, generates pytest tests targeting the Orchestrator-specified functions, and executes them. Results streamed over SSE.

### Step 4: Self-Healing (conditional)
If tests failed and did not time out, the Self-Healer runs up to 3 fix-and-execute cycles. Each cycle's outcome is streamed over SSE. If healing succeeds, fixed source files are committed back to the PR branch.

### Step 5: Final Decision
The Decision Agent synthesizes all reports, applies deterministic override rules, computes scores, and issues the verdict. Streamed over SSE.

### Step 6: GitHub Action
Based on the verdict:
- **MERGE**: Posts an approval review comment with score table, then merges the PR.
- **MERGE_WITH_FIX**: Posts a healing log comment, then merges the PR.
- **REJECT**: Posts a rejection comment with required actions, then closes the PR.
- **BUG_REPORT**: Posts a bug detection comment, closes the PR, opens a new GitHub Issue with full reproduction details.

The final commit status (`success` or `failure`) is set on the PR head SHA.

### Step 7: Completion
All results persisted to SQLite. SSE `complete` event fires with the final run summary.

---

## Tool Surface

| Tool | Purpose | Side Effect |
|---|---|---|
| `github_api.get_pr_diff()` | Fetch unified diff | Read-only |
| `github_api.get_pr_metadata()` | Fetch PR title, author, branch, SHA | Read-only |
| `github_api.set_commit_status()` | Set pending/success/failure on commit | ✅ GitHub status |
| `github_api.post_pr_review()` | Post review comment with score table | ✅ GitHub comment |
| `github_api.post_pr_comment()` | Post plain comment | ✅ GitHub comment |
| `github_api.merge_pr()` | Auto-merge PR | ✅ GitHub merge |
| `github_api.close_pr()` | Close rejected PR | ✅ GitHub close |
| `github_api.create_bug_issue()` | Open bug issue from failed PR | ✅ GitHub issue |
| `github_api.update_file_on_branch()` | Push healed source file to PR branch | ✅ GitHub commit |
| `github_api.fetch_pr_source_files()` | Download source files from branch | Read-only |
| `tavily_search.multi_hop_search()` | Live web search with LLM-generated follow-ups | Read-only |
| `subprocess` sandbox | Execute generated pytest suite | ✅ Disk + process |
| `aiosqlite` | Persist all pipeline state | ✅ Local DB |
| `omium.trace()` | Emit agent spans to Omium dashboard | ✅ Omium API |

---

## Model Routing & Fallback Chain

PullSmith never fails because one API key rate-limits. The `model_router.py` builds an automatic fallback chain across multiple Groq models and optionally OpenRouter:

```
Primary: Groq llama-3.3-70b-versatile   (key 1)
Fallback 1: Groq mixtral-8x7b-32768     (key 1)
Fallback 2: Groq llama3-70b-8192        (key 1)
Fallback 3: Groq gemma2-9b-it           (key 1)
Fallback 4: Groq llama-3.1-8b-instant   (key 1)
... (repeated for GROQ_API_KEY_2, _3, _4 if set)

Final: OpenRouter free tier (meta-llama/llama-3.3-70b)
```

LangChain's `.with_fallbacks()` handles the chain automatically. Structured output (Pydantic models) is applied per model in the chain. The Orchestrator and Decision Agent use Gemini 2.0 Flash directly for superior structured output fidelity on complex schemas.

---

## Data Flow

```
PipelineRun (SQLite)
├── run_id          UUID, primary key
├── repo            "owner/repo"
├── pr_number       Integer
├── pr_title        String
├── pr_author       String
├── branch          String
├── commit_sha      String (for commit status API)
├── diff_text       Full unified diff
├── quality_plan    QualityPlan (JSON)
├── research_findings  ResearchFindings (JSON, optional)
├── review_report   ReviewReport (JSON)
├── security_report SecurityReport (JSON)
├── test_result     TestResult (JSON)
├── heal_attempts   List[HealAttempt] (JSON)
├── decision        PRDecision (JSON)
├── github_comment_url  String
├── github_issue_url    String
└── status          pending → running → completed|failed

AgentStep (SQLite)
├── step_id         UUID
├── run_id          FK → PipelineRun
├── agent           AgentName enum
├── status          started → completed | failed
├── message         Human-readable step description
├── result_data     JSON metadata
└── timestamps
```

---

## Webhook Contract

**Endpoint:** `POST /webhook/github`  
**Auth:** HMAC-SHA256 on request body with `GITHUB_WEBHOOK_SECRET`  
**Triggers:**

| Event | Action | Pipeline? |
|---|---|---|
| `pull_request` | `opened` | ✅ Full pipeline |
| `pull_request` | `synchronize` | ✅ Full pipeline |
| `pull_request` | `reopened` | ✅ Full pipeline |
| `issue_comment` | body contains `/re-review` | ✅ Full pipeline |
| All others | — | Ignored, 200 OK |

**Manual trigger:** `POST /api/trigger` with JSON body:
```json
{
  "repo": "owner/repo",
  "pr_number": 42
}
```

**SSE stream:** `GET /api/runs/{run_id}/stream` — emits events `status`, `plan`, `research`, `review`, `security`, `tests`, `heal`, `verdict`, `complete`.

---

## Quickstart

### Prerequisites
- Python 3.12+
- Git
- A GitHub repo with webhook access
- API keys (see Environment Variables below)

### 1. Clone and install

```bash
git clone https://github.com/your-org/pullsmith
cd pullsmith
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your API keys
```

### 3. Start the server

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### 4. Expose via ngrok (for GitHub webhook)

```bash
ngrok http 8000
# Copy the https://xxx.ngrok.io URL
```

### 5. Register GitHub webhook

In your GitHub repo: **Settings → Webhooks → Add webhook**
- Payload URL: `https://xxx.ngrok.io/webhook/github`
- Content type: `application/json`
- Secret: value of `GITHUB_WEBHOOK_SECRET` from your `.env`
- Events: **Pull requests** + **Issue comments**

### 6. Open a PR

Open any pull request in the configured repo. Watch the live dashboard at `http://localhost:8000/dashboard`.

### 7. Run the three-PR demo script

```bash
python scripts/run_three_pr_demo.py
```

This simulates three distinct PR types (feature, security vulnerability, refactor) to demonstrate all four verdict paths.

---

## Environment Variables

```bash
# LLM Providers
GEMINI_API_KEY=           # Orchestrator + Decision Agent (Gemini 2.0 Flash)
GROQ_API_KEY=             # All other agents (Llama 3.3 70B)
GROQ_API_KEY_2=           # Optional: key rotation for higher throughput
TOGETHER_API_KEY=         # Fallback when Groq rate-limits
OPENROUTER_API_KEY=       # Final fallback (free tier)

# Tools
TAVILY_API_KEY=           # Live web search for Researcher agent
OMIUM_API_KEY=            # Observability and trace dashboard

# GitHub
GITHUB_TOKEN=             # PAT with scopes: repo, pull_requests, issues
GITHUB_WEBHOOK_SECRET=    # HMAC secret, match value in GitHub webhook settings

# Server
HOST=0.0.0.0
PORT=8000
```

**Free tiers available for everything:**
- Gemini: 1,500 req/day via [aistudio.google.com](https://aistudio.google.com/apikey)
- Groq: ~14,400 req/day via [console.groq.com](https://console.groq.com)
- Tavily: 1,000 searches/month via [tavily.com](https://tavily.com)
- Omium: Hackathon access via [app.omium.ai](https://app.omium.ai)

---

## Project Structure

```
pullsmith/
├── main.py                      # FastAPI server, webhook ingestion, SSE bus
├── requirements.txt
├── .env.example
│
├── agents/
│   ├── pipeline.py              # Master orchestrator — wires all 7 agents
│   ├── orchestrator.py          # Gemini: QualityPlan generation
│   ├── researcher.py            # Groq + Tavily: multi-hop web research
│   ├── pr_reviewer.py           # Groq: structured code review
│   ├── security_scanner.py      # Groq: AppSec vulnerability audit
│   ├── test_generator.py        # Groq: pytest generation + sandbox execution
│   ├── self_healer.py           # Groq: iterative test/source fix (3 attempts)
│   ├── decision_agent.py        # Groq + rules: final verdict synthesis
│   └── model_router.py          # Fallback chain across Groq models + OpenRouter
│
├── models/
│   └── schemas.py               # All Pydantic models: PipelineRun, QualityPlan,
│                                #   ReviewReport, SecurityReport, TestResult,
│                                #   HealAttempt, PRDecision, etc.
│
├── db/
│   └── database.py              # aiosqlite: persist runs, steps, decisions
│
├── tools/
│   ├── github_api.py            # All GitHub REST API calls
│   ├── tavily_search.py         # Multi-hop Tavily search wrapper
│   └── file_writer.py           # Sandbox file utilities
│
├── dashboard/
│   ├── index.html               # Live pipeline dashboard
│   ├── app.js                   # SSE consumer, real-time agent timeline
│   └── style.css
│
└── scripts/
    └── run_three_pr_demo.py     # Three-PR demo: feature / security / refactor
```

---

## Dependencies

| Package | Version | Role |
|---|---|---|
| `fastapi` | 0.115.5 | Web server + webhook + SSE |
| `uvicorn[standard]` | 0.32.1 | ASGI server |
| `langchain-groq` | latest | Groq LLM integration |
| `langchain-google-genai` | latest | Gemini integration |
| `google-generativeai` | 0.8.3 | Gemini SDK |
| `groq` | 0.11.0 | Groq SDK |
| `tavily-python` | 0.3.9 | Web search |
| `pydantic` | 2.10.1 | All data schemas |
| `aiosqlite` | 0.20.0 | Async SQLite persistence |
| `sse-starlette` | 2.1.3 | Server-Sent Events |
| `PyGithub` | 2.5.0 | GitHub API |
| `python-dotenv` | 1.0.1 | Environment config |
| `httpx` | 0.28.0 | Async HTTP |
| `omium` | latest | Agent tracing + observability |
| `python-multipart` | 0.0.12 | Form parsing |
| `markdown` | 3.7 | Report rendering |

---

## Evaluation Axis Coverage

| Axis | How PullSmith delivers |
|---|---|
| **Problem Relevance (20%)** | Code review is a universal engineering bottleneck. PullSmith eliminates it for qualifying PRs. Real target users: solo founders, small teams, open-source maintainers. |
| **Autonomous Execution (25%)** | Webhook → seven agents → GitHub merge/close/issue. Zero human steering. Retries via Self-Healer (3 attempts). Crash-safe via SQLite persistence. Deterministic Decision Agent overrides prevent LLM hallucination on verdicts. |
| **Multi-Agent Quality (20%)** | 7 agents with distinct roles, distinct models, distinct tools. Parallel fan-out (PR Reviewer ∥ Security Scanner). Sequential with state threading (Orchestrator → Researcher → parallel → Test Generator → Self-Healer → Decision Agent). |
| **Tooling & Integrations (15%)** | GitHub API (8 distinct operations), Tavily multi-hop search, subprocess sandbox, SQLite, SSE, Omium. Every verdict produces verifiable GitHub side-effects. |
| **Demo Video (10%)** | Three-PR demo script covers all four verdict paths. Live dashboard shows real-time agent progression. End-to-end from webhook to merged PR in under 60 seconds. |
| **Technical Architecture (10%)** | Typed Pydantic schemas throughout. Async-first with aiosqlite. Model fallback chain prevents rate-limit failures. Clean module separation. Omium tracing on every agent function. |
| **Omium Bonus (+10%)** | `@omium.trace(...)` on every agent: `full_pipeline`, `orchestrator_plan`, `run_researcher`, `pr_reviewer`, `security_scanner`, `test_generator`, `self_healer`, `decision_agent`. Causal parent-child linking. Dashboard matches demo exactly. |

---
<div align='center'>
PR in. Prod out.
</div>