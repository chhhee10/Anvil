# QualityEngine Dashboard — Partner integration guide

Skeleton UI for your design pass. **Do not remove element IDs** — `app.js` depends on them.

## Files

| File | Your job |
|------|----------|
| `style.css` | Full visual design (colors, typography, motion) |
| `index.html` | Optional layout tweaks; keep `id` and `data-region` hooks |
| `app.js` | Logic only if adding features; styling via CSS |

## Run locally

```bash
# From repo root, with API server up:
python3 main.py
# Open http://localhost:8000/dashboard/
```

## REST API (used by dashboard)

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Header status dot |
| `GET /status` | Run list (sidebar) |
| `GET /status/{run_id}` | Full run detail |
| `POST /trigger` | Body: `{ "repo", "pr_number", "topic?" }` |
| `GET /stream/{run_id}` | SSE live updates |

### `GET /status` item shape

```json
{
  "run_id": "uuid",
  "status": "pending|running|completed|failed",
  "topic": "PR #12: …",
  "repo": "owner/repo",
  "pr_number": 12,
  "verdict": "MERGE",
  "score": 9.7,
  "created_at": "ISO8601"
}
```

### `GET /status/{run_id}` extra fields

`pr_title`, `pr_author`, `scores`, `reasoning`, `github_comment`, `github_issue`, `steps[]`, `error`

## SSE events (live run)

Connect: `new EventSource('/stream/{run_id}')`

| Event | Updates panel |
|-------|----------------|
| `init` | Initial steps in timeline |
| `step` | Agent log + stepper (`data.agent`, `data.message`) |
| `plan` | Quality Plan panel |
| `review` | PR Review panel |
| `security` | Security panel |
| `tests` | Tests panel; may show Self-Healer |
| `heal` | Self-Healer list (one per attempt) |
| `verdict` | Scores + Final Decision |
| `complete` | Refresh from REST |
| `status` | `completed` / `failed` → refresh |
| `ping` | Keep-alive (ignore) |

## DOM regions (`data-region`)

- `header` — logo, health, trigger button
- `sidebar` — run list
- `main` — detail or placeholder
- `run-header` — title, meta, status/verdict badges
- `agent-stepper` — 7-agent progress row
- `quality-plan` — orchestrator output
- `agent-timeline` — step log from DB + live
- `scores` — 6 score cards
- `review`, `security`, `tests`, `heal`, `verdict`, `github-links`

## Required element IDs

`runList`, `runCount`, `detailPlaceholder`, `detailView`, `detailTopic`, `detailMeta`, `detailStatus`, `detailVerdict`, `agentStepper`, `planContent`, `agentTimeline`, `scoreGrid`, `reviewContent`, `securityContent`, `testsContent`, `panelHeal`, `healList`, `verdictBadge`, `verdictReasoning`, `githubLinks`, `triggerModal`, `inputRepo`, `inputPrNumber`, `inputTopic`, `healthDot`, `healthLabel`

## Agents (stepper order)

1. orchestrator  
2. pr_reviewer + security_scanner (parallel)  
3. test_generator  
4. self_healer (optional)  
5. decision_agent  
6. system (GitHub actions)

## Verdict CSS classes

- `verdict-merge` — MERGE  
- `verdict-merge-fix` — MERGE_WITH_FIX  
- `verdict-reject` — REJECT  
- `verdict-bug` — BUG_REPORT  

## CSS variables (override in `:root`)

`--qe-bg`, `--qe-surface`, `--qe-border`, `--qe-text`, `--qe-text-muted`, `--qe-accent`, `--qe-merge`, `--qe-merge-fix`, `--qe-reject`, `--qe-bug`
