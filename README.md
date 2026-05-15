# 🚀 NewsRoom AI

**Autonomous Multi-Agent Tech Intelligence Pipeline**

NewsRoom AI is a webhook-driven, multi-agent autonomous system that watches GitHub repositories, autonomously researches every significant event on the web, writes a structured intelligence report, critiques and refines it — all without human intervention.

---

## Architecture

```
GitHub Webhook / Manual Trigger
        │
        ▼
  FastAPI Server (returns 202 immediately)
        │
        ▼
  ORCHESTRATOR (Gemini Flash)
  Plans research → fans out tasks
        │
   ┌────┴────┐
   ▼         ▼
RESEARCHER  CODE ANALYST
(Groq)      (Groq)
Tavily      GitHub API
multi-hop   diff analysis
   └────┬────┘
        ▼
    WRITER (Gemini Flash)
    Structured report
        ▼
    CRITIC (Groq)
    Score + revision loop (max 2x)
        ▼
    DELIVER
    Save to /reports/ + DB + SSE
```

### 5 Specialized Agents
| Agent | Model | Role |
|-------|-------|------|
| Orchestrator | Gemini 2.0 Flash | Event analysis, research planning, fan-out coordination |
| Researcher | Groq Llama 3.3 70B | Multi-hop Tavily web search + synthesis |
| Code Analyst | Groq Llama 3.3 70B | GitHub diff fetch + security/breaking change analysis |
| Writer | Gemini 2.0 Flash | Structured intelligence report generation |
| Critic | Groq Llama 3.3 70B | Quality scoring (accuracy/completeness/actionability) + revision requests |

---

## Quick Start

### 1. Prerequisites
```bash
python3 -m pip install -r requirements.txt
```

### 2. API Keys (all free)
Sign up for free keys at:
- **Gemini**: https://aistudio.google.com/apikey
- **Groq**: https://console.groq.com
- **Tavily**: https://tavily.com

```bash
cp .env.example .env
# Edit .env with your keys
```

### 3. Run the Server
```bash
python main.py
```

Server starts at `http://localhost:8000`  
Dashboard at `http://localhost:8000/dashboard/`

### 4. Test a Manual Run
```bash
curl -X POST http://localhost:8000/trigger \
  -H "Content-Type: application/json" \
  -d '{"topic": "Python 3.13 performance improvements"}'
```

Open the dashboard and watch the pipeline run live!

---

## GitHub Webhook Setup

### 1. Start a public tunnel (free)
```bash
# Install pinggy or use SSH tunnel:
ssh -p 443 -R0:localhost:8000 a.pinggy.io
# Or use ngrok: ngrok http 8000
```

### 2. Add GitHub Webhook
1. Go to your repo → Settings → Webhooks → Add webhook
2. Payload URL: `https://YOUR_TUNNEL_URL/webhook/github`
3. Content type: `application/json`
4. Secret: same as `GITHUB_WEBHOOK_SECRET` in `.env`
5. Events: Push, Pull Requests, Issues

### 3. Push a commit → watch the pipeline fire automatically!

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/trigger` | Manual pipeline trigger |
| `POST` | `/webhook/github` | GitHub webhook receiver |
| `GET`  | `/status` | List all pipeline runs |
| `GET`  | `/status/{run_id}` | Detailed run status + agent steps |
| `GET`  | `/report/{run_id}` | Fetch final markdown report |
| `GET`  | `/stream/{run_id}` | SSE stream for live updates |
| `GET`  | `/health` | Health check |

---

## Features

### Multi-Agent Collaboration
Five specialized agents with clear separation of concerns. The Orchestrator fans out research and code analysis tasks in **parallel** using `asyncio.gather()`.

### Autonomous Execution
Full pipeline runs without human input:
- Webhook → 202 response (immediate)
- Background pipeline: plan → research (parallel) → write → critique → deliver
- Writer/Critic loop with structured revision requests (max 2 iterations)

### Deep Reasoning
- Orchestrator decomposes events into structured `ResearchPlan`
- Researcher performs **multi-hop** searches (result of hop 1 informs hop 2)
- Critic scores on 3 axes with specific revision requests (not just yes/no)
- Writer revises based on structured feedback

### Crash Safety
- All state persisted to SQLite before each step
- On restart: interrupted runs detected and marked failed
- Each step writes to DB before proceeding

### Live Dashboard
- SSE streaming for real-time agent step updates
- Agent timeline with color-coded steps
- Critic score cards (accuracy/completeness/actionability)
- Full markdown report viewer with copy button

---

## Free API Stack
| Service | Purpose | Free Tier |
|---------|---------|-----------|
| Google Gemini 2.0 Flash | Orchestrator + Writer | 1500 req/day |
| Groq Llama 3.3 70B | Researcher + Critic | ~14,400 req/day |
| Tavily Search | Web search tool | 1,000/month |
| GitHub Webhooks | Event ingestion | Free |
| Pinggy/ngrok | HTTPS tunnel | Free |

---

## Dependencies
- `fastapi` + `uvicorn` — web server
- `google-generativeai` — Gemini Flash
- `groq` — Groq LLM SDK
- `tavily-python` — web search
- `aiosqlite` — async SQLite
- `sse-starlette` — Server-Sent Events
- `httpx` — GitHub API calls
- `pydantic` — data validation
- `python-dotenv` — env management
