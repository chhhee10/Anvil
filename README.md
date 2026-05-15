# QualityEngine AI

**Multi-agent autonomy that ships real work, end-to-end.**

QualityEngine AI is a webhook-driven, autonomous pull-request quality pipeline. When a PR opens on GitHub, seven specialized agents review the diff, scan for security issues, generate and run tests, attempt self-healing when tests fail, and **merge, reject, or file a bug issue on GitHub** — without human intervention.

> **North star:** A demo that runs. A product someone would actually use. End-to-end work, completed without a human in the loop.

---

## Problem

Engineering teams spend hours on repetitive PR review: style checks, security smells, missing tests, and merge decisions. QualityEngine automates that workflow as an always-on **Research → Action** pipeline:

1. **Trigger** — GitHub webhook or manual API call  
2. **Investigate** — multi-agent review, security scan, generated pytest run  
3. **Act** — auto-merge, merge-after-heal, reject/close, or open a GitHub Issue  

This maps to the hackathon brief’s *Personal/Team Agent on GitHub* and *Research → Action* problem spaces.

---

## What “ships real work” means here

The pipeline does not stop at a report. It produces **verifiable side-effects**:

| Verdict | GitHub actions |
|---------|----------------|
| `MERGE` | Approval review → **PR merged** |
| `MERGE_WITH_FIX` | Comment + approval → **PR merged** (after self-healer fixed tests) |
| `REJECT` | Rejection comment → **PR closed** |
| `BUG_REPORT` | Comment → **PR closed** → **GitHub Issue** created |

Commit status checks (`pending` / `success` / `failure`) are set on the PR head SHA.

---

## Architecture

```
GitHub Webhook / POST /trigger
        │
        ▼
  FastAPI (202 immediately → background task)
        │
        ▼
  ORCHESTRATOR — QualityPlan (change type, risk, files to test)
        │
   ┌────┴────────────────────┐
   ▼                         ▼
PR REVIEWER            SECURITY SCANNER
(code quality)         (secrets, injection, unsafe APIs)
   └────┬────────────────────┘
        ▼
  TEST GENERATOR — pytest in sandbox (fetches PR branch files)
        │
        ▼ (if tests fail)
  SELF-HEALER — up to 3 fix attempts on generated tests
        │
        ▼
  DECISION AGENT — MERGE | MERGE_WITH_FIX | REJECT | BUG_REPORT
        │
        ▼
  GitHub API — merge / close / comment / issue / status
```

### Agents

| Agent | Model (via router) | Role |
|-------|-------------------|------|
| Orchestrator | Gemini / Groq fallback | Parse diff → structured `QualityPlan` |
| PR Reviewer | Groq Llama 3.3 70B | Code quality, bugs, style, logic |
| Security Scanner | Groq Llama 3.3 70B | Secrets, injection, unsafe patterns |
| Test Generator | Groq Llama 3.3 70B | Generate + run pytest against PR files |
| Self-Healer | Groq Llama 3.3 70B | Fix failing generated tests (max 3 loops) |
| Decision Agent | Gemini / Groq fallback | Final verdict + scores |

Parallel fan-out: **PR Reviewer** and **Security Scanner** run concurrently (`asyncio.gather`).

---

## Problem statement alignment

| Required capability | How QualityEngine satisfies it |
|--------------------|--------------------------------|
| **Multi-Agent** | Seven specialized agents with distinct prompts and structured outputs — not one LLM in a retry loop |
| **Autonomy** | Merge / reject / issue without human approval |
| **Long-Running** | Background tasks, self-heal loops, SQLite persistence, crash recovery on restart |
| **Deep Reasoning** | Orchestrator planning + specialist reports + decision synthesis with rule overrides |
| **Tool Calling** | GitHub API, branch file fetch, pytest subprocess sandbox |
| **Web Search** | Tavily client in `tools/tavily_search.py` (set `TAVILY_API_KEY` for live CVE/advisory enrichment) |
| **Webhooks** | `POST /webhook/github` — PR opened/sync/reopened, `/re-review`, push to main |
| **Async Orchestration** | 202 response, parallel agents, SSE progress stream |

| Annex A demo contract | |
|-----------------------|--|
| Trigger | Webhook or `/trigger` |
| Multi-agent handoff | Orchestrator → parallel specialists → tests → healer → decision |
| Tool + side-effect | GitHub merge, close, comments, issues |
| Async / long-running | Background pipeline (minutes with self-heal) |
| Completion | PR disposition is the finished unit of work |

**Omium bonus:** `@omium.trace` on pipeline and agents — submit dashboard URL with your trace.

---

## Quick start

### 1. Prerequisites

- Python 3.11+
- A GitHub repo you can open PRs against
- Free API keys (see below)

```bash
python3 -m pip install -r requirements.txt
```

### 2. Environment

```bash
cp .env.example .env
```

| Variable | Required | Purpose |
|----------|----------|---------|
| `GITHUB_TOKEN` | **Yes** | Merge, close, comments, issues (`repo` scope) |
| `GROQ_API_KEY` | **Yes** | PR Reviewer, Security, Tests, Self-Healer |
| `GEMINI_API_KEY` | Recommended | Orchestrator, Decision Agent |
| `TOGETHER_API_KEY` | Optional | Fallback when Groq rate-limits |
| `GITHUB_WEBHOOK_SECRET` | For webhooks | HMAC validation |
| `OMIUM_API_KEY` | Optional | Tracing bonus |
| `TAVILY_API_KEY` | Optional | Live web search / CVE enrichment |

### 3. Run the server

```bash
python3 main.py
```

- API: http://localhost:8000  
- Dashboard: http://localhost:8000/dashboard/  
- Health: http://localhost:8000/health  

### 4. Trigger on a pull request

Open a PR on your repo, then:

```bash
curl -X POST http://localhost:8000/trigger \
  -H "Content-Type: application/json" \
  -d '{
    "repo": "YOUR_USER/YOUR_REPO",
    "pr_number": 1,
    "topic": "Manual quality review"
  }'
```

Response includes `run_id`. Watch progress:

```bash
# Poll status
curl http://localhost:8000/status/RUN_ID

# Or open the live dashboard
open http://localhost:8000/dashboard/
```

### 5. Three-PR demo script (recommended)

Automates clean merge, heal-then-merge, and security reject scenarios:

```bash
# Edit REPO in scripts/run_three_pr_demo.py if needed
python3 scripts/run_three_pr_demo.py
```

Expect: `MERGE` → `MERGE_WITH_FIX` → `REJECT` (exit code 0 if all match).

---

## GitHub webhook setup

### 1. Expose localhost (free tunnel)

```bash
# Example: ngrok
ngrok http 8000

# Or Pinggy SSH tunnel
ssh -p 443 -R0:localhost:8000 a.pinggy.io
```

### 2. Add webhook on your repo

1. **Settings → Webhooks → Add webhook**
2. **Payload URL:** `https://YOUR_TUNNEL/webhook/github`
3. **Content type:** `application/json`
4. **Secret:** same as `GITHUB_WEBHOOK_SECRET` in `.env`
5. **Events:** Pull requests, Issue comments, Pushes (optional)

Opening or updating a PR triggers the pipeline automatically. Comment `/re-review` on a PR to re-run.

---

## API reference

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/webhook/github` | GitHub events (returns 202) |
| `POST` | `/trigger` | Manual run: `{"repo", "pr_number", "topic?"}` |
| `GET` | `/status` | List recent runs |
| `GET` | `/status/{run_id}` | Verdict, scores, agent steps |
| `GET` | `/stream/{run_id}` | SSE live updates |
| `GET` | `/health` | Health check |
| `GET` | `/dashboard/` | Live UI |

---

## Decision logic (summary)

| Condition | Verdict | GitHub outcome |
|-----------|---------|----------------|
| Review ≥ 7, security ≥ 7, tests pass, no critical findings | `MERGE` | Merge + approve |
| Tests failed → self-healer succeeded, scores OK | `MERGE_WITH_FIX` | Merge + heal log comment |
| Critical security or very low review score | `REJECT` | Close + rejection comment |
| Tests still fail after 3 heal attempts | `BUG_REPORT` | Close + GitHub Issue |

---

## Observability

- **Logs:** `logs/qualityengine.log` and `server.log`
- **Database:** `qualityengine.db` (SQLite) — run state and agent steps survive restarts
- **Live dashboard:** SSE agent timeline + verdict
- **Omium:** instrumented traces for hackathon bonus — open [Omium dashboard](https://app.omium.ai) and match `run_id` from `/status`

---

## Project structure

```
agents/
  pipeline.py          # Master orchestrator
  orchestrator.py      # QualityPlan
  pr_reviewer.py
  security_scanner.py
  test_generator.py
  self_healer.py
  decision_agent.py
  model_router.py      # Groq / Gemini / Together fallback chain
tools/
  github_api.py        # Merge, close, issues, diff, status
  tavily_search.py     # Web search (optional)
db/
  database.py          # Async SQLite persistence
models/
  schemas.py           # Pydantic models for all agent I/O
dashboard/             # Live SSE UI
scripts/
  run_three_pr_demo.py # End-to-end 3-PR demo
main.py                # FastAPI entrypoint
```

---

## Free API stack

| Service | Role | Free tier |
|---------|------|-----------|
| Groq | Review, security, tests, healer | ~14k req/day |
| Google Gemini | Orchestrator, decision | ~1500 req/day |
| Together AI | Rate-limit fallback | $1 credit |
| GitHub API | Webhooks + merge/reject/issue | Free |
| Tavily | Web search (optional) | 1000 searches/month |
| Omium | Trace bonus | Sponsor access |

---

## Dependencies

Disclosed per submission requirements:

- **Framework:** FastAPI, Uvicorn, LangChain (LCEL chains)
- **LLMs:** Google Gemini, Groq, Together AI (fallback)
- **Tools:** PyGithub/httpx (GitHub), Tavily, pytest (subprocess)
- **Data:** aiosqlite, Pydantic
- **Observability:** Omium SDK, SSE (sse-starlette)

See `requirements.txt` for pinned versions.

---

## Submission checklist

| Artifact | Location |
|----------|----------|
| Product (this repo) | ✓ |
| Quickstart (above) | ✓ |
| 5-min demo video | Record: webhook → dashboard → GitHub merge/reject |
| 3-page PDF writeup | Problem, architecture, autonomy story |
| Omium trace URL (bonus) | From dashboard after a full run |

---

## License

MIT — hackathon submission.
