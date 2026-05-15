/**
 * QualityEngine AI — Dashboard skeleton
 * -------------------------------------
 * Partner: restyle via style.css. Logic + DOM hooks are stable.
 * See dashboard/README.md for SSE events and API contract.
 */
"use strict";

// ─── Agent registry (matches backend AgentName) ─────────────────────
const AGENTS = [
  { id: "orchestrator",      label: "Orchestrator",      parallel: false },
  { id: "pr_reviewer",       label: "PR Reviewer",       parallel: true,  group: "review" },
  { id: "security_scanner",  label: "Security Scanner",  parallel: true,  group: "review" },
  { id: "test_generator",    label: "Test Generator",    parallel: false },
  { id: "self_healer",       label: "Self-Healer",       parallel: false, optional: true },
  { id: "decision_agent",    label: "Decision Agent",    parallel: false },
  { id: "system",            label: "GitHub Actions",    parallel: false },
];

const AGENT_MAP = Object.fromEntries(AGENTS.map((a) => [a.id, a]));

const VERDICT_CLASS = {
  MERGE: "verdict-merge",
  MERGE_WITH_FIX: "verdict-merge-fix",
  REJECT: "verdict-reject",
  BUG_REPORT: "verdict-bug",
  SKIPPED: "verdict-skipped",
};

const SCORE_KEYS = [
  { key: "overall",        label: "Overall" },
  { key: "correctness",    label: "Correctness" },
  { key: "security",       label: "Security" },
  { key: "test_coverage",  label: "Tests" },
  { key: "code_quality",   label: "Quality" },
  { key: "risk",           label: "Risk" },
];

// ─── State ───────────────────────────────────────────────────────────
let runs = [];
let activeRunId = null;
let activeSSE = null;
let pollTimer = null;

/** Live SSE payload cache for the selected run */
const liveState = {
  plan: null,
  review: null,
  security: null,
  tests: null,
  heals: [],
  verdict: null,
  stepper: {}, // agentId -> pending | active | done | failed | skipped
};

// ─── DOM refs ────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);

// ─── Init ────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  buildAgentStepper();
  bindUI();
  checkHealth();
  fetchRuns();
  pollTimer = setInterval(() => {
    fetchRuns();
    if (activeRunId) {
      const r = runs.find((x) => x.run_id === activeRunId);
      if (r?.status === "running") fetchRunDetail(activeRunId);
    }
  }, 4000);
  setInterval(checkHealth, 30000);
});

function bindUI() {
  $("btnOpenTrigger").addEventListener("click", openTriggerModal);
  $("btnCloseTrigger").addEventListener("click", closeTriggerModal);
  $("btnCancelTrigger").addEventListener("click", closeTriggerModal);
  $("triggerForm").addEventListener("submit", (e) => {
    e.preventDefault();
    submitTrigger();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeTriggerModal();
  });
}

// ─── Health ──────────────────────────────────────────────────────────
async function checkHealth() {
  const dot = $("healthDot");
  const label = $("healthLabel");
  try {
    const res = await fetch("/health");
    if (!res.ok) throw new Error("unhealthy");
    const data = await res.json();
    dot.className = "qe-health__dot qe-health__dot--ok";
    label.textContent = data.service || "Live";
  } catch {
    dot.className = "qe-health__dot qe-health__dot--err";
    label.textContent = "Offline";
  }
}

// ─── Runs list ───────────────────────────────────────────────────────
async function fetchRuns() {
  try {
    const res = await fetch("/status");
    if (!res.ok) return;
    runs = await res.json();
    renderRunList();
  } catch (e) {
    console.warn("fetchRuns:", e);
  }
}

function renderRunList() {
  const list = $("runList");
  const count = $("runCount");
  count.textContent = `${runs.length} run${runs.length !== 1 ? "s" : ""}`;

  if (!runs.length) {
    list.innerHTML = `
      <div class="qe-empty" data-empty="run-list">
        <p>No runs yet.</p>
        <p class="qe-empty__hint">Open a PR webhook or click <strong>Review PR</strong>.</p>
      </div>`;
    return;
  }

  list.innerHTML = runs
    .map((r) => {
      const active = r.run_id === activeRunId ? " qe-run-card--active" : "";
      const verdict = r.verdict
        ? `<span class="qe-run-card__verdict ${VERDICT_CLASS[r.verdict] || ""}">${r.verdict}</span>`
        : "";
      const score =
        r.score != null ? `<span class="qe-run-card__score">${Number(r.score).toFixed(1)}</span>` : "";
      return `
        <button type="button" class="qe-run-card${active}" data-run-id="${r.run_id}"
                onclick="selectRun('${r.run_id}')">
          <div class="qe-run-card__top">
            <span class="qe-run-card__status qe-run-card__status--${r.status}">${r.status}</span>
            ${verdict}
            ${score}
          </div>
          <div class="qe-run-card__topic">${esc(r.topic || "Untitled run")}</div>
          <div class="qe-run-card__meta">
            ${r.repo ? esc(r.repo) : ""}
            ${r.pr_number ? ` · PR #${r.pr_number}` : ""}
            · ${relativeTime(r.created_at)}
          </div>
        </button>`;
    })
    .join("");
}

// ─── Select run ──────────────────────────────────────────────────────
function resetLiveState() {
  liveState.plan = null;
  liveState.review = null;
  liveState.security = null;
  liveState.tests = null;
  liveState.heals = [];
  liveState.verdict = null;
  liveState.stepper = {};
  AGENTS.forEach((a) => {
    liveState.stepper[a.id] = a.optional ? "skipped" : "pending";
  });
  updateStepperUI();
}

async function selectRun(runId) {
  activeRunId = runId;
  if (activeSSE) {
    activeSSE.close();
    activeSSE = null;
  }
  resetLiveState();

  $("detailPlaceholder").hidden = true;
  $("detailView").hidden = false;

  renderRunList();
  await fetchRunDetail(runId);

  const run = runs.find((r) => r.run_id === runId);
  if (run?.status === "running") connectSSE(runId);
}

async function fetchRunDetail(runId) {
  try {
    const res = await fetch(`/status/${runId}`);
    if (!res.ok) return;
    const detail = await res.json();
    if (activeRunId !== runId) return;
    renderRunDetail(detail);
    if (detail.status === "running" && !activeSSE) connectSSE(runId);
  } catch (e) {
    console.warn("fetchRunDetail:", e);
  }
}

// ─── Render full detail from REST ────────────────────────────────────
function renderRunDetail(d) {
  $("detailTopic").textContent = d.topic || d.pr_title || `PR #${d.pr_number || "?"}`;
  $("detailMeta").innerHTML = [
    d.repo && `<span>${esc(d.repo)}</span>`,
    d.pr_number && `<span>PR #${d.pr_number}</span>`,
    d.pr_author && `<span>@${esc(d.pr_author)}</span>`,
    d.pr_title && `<span title="${esc(d.pr_title)}">${esc(truncate(d.pr_title, 40))}</span>`,
    `<span class="qe-mono">${d.run_id.slice(0, 8)}</span>`,
    `<span>${relativeTime(d.created_at)}</span>`,
  ]
    .filter(Boolean)
    .join("");

  $("detailStatus").textContent = d.status;
  $("detailStatus").className = `qe-badge qe-badge--status qe-badge--${d.status}`;

  if (d.verdict) {
    $("detailVerdict").hidden = false;
    $("detailVerdict").textContent = d.verdict;
    $("detailVerdict").className = `qe-badge qe-badge--verdict ${VERDICT_CLASS[d.verdict] || ""}`;
  } else {
    $("detailVerdict").hidden = true;
  }

  if (d.scores) renderScores(d.scores);
  if (d.reasoning) {
    $("verdictReasoning").textContent = d.reasoning;
    $("verdictBadge").textContent = d.verdict || "—";
    $("verdictBadge").className = `qe-verdict__badge ${VERDICT_CLASS[d.verdict] || ""}`;
  }

  renderGithubLinks(d.github_comment, d.github_issue);

  if (d.steps?.length) {
    applyStepsToStepper(d.steps);
    $("agentTimeline").innerHTML = renderTimelineHtml(d.steps);
  }

  if (d.error) {
    $("agentTimeline").insertAdjacentHTML(
      "afterbegin",
      `<div class="qe-timeline__error">Error: ${esc(d.error)}</div>`
    );
  }
}

// ─── Agent stepper ───────────────────────────────────────────────────
function buildAgentStepper() {
  const ol = $("agentStepper");
  ol.innerHTML = AGENTS.map(
    (a) => `
    <li class="qe-stepper__item" data-agent="${a.id}" data-state="pending">
      <span class="qe-stepper__icon" aria-hidden="true">○</span>
      <span class="qe-stepper__label">${a.label}</span>
      ${a.parallel ? '<span class="qe-stepper__tag">parallel</span>' : ""}
    </li>`
  ).join("");
  resetLiveState();
}

function setStepperAgent(agentId, state) {
  if (!AGENT_MAP[agentId]) return;
  liveState.stepper[agentId] = state;
  if (agentId === "pr_reviewer" || agentId === "security_scanner") {
    if (state === "active") {
      liveState.stepper.pr_reviewer = "active";
      liveState.stepper.security_scanner = "active";
    }
  }
  updateStepperUI();
}

function applyStepsToStepper(steps) {
  const order = AGENTS.map((a) => a.id);
  steps.forEach((s) => {
    const st =
      s.status === "completed" ? "done" : s.status === "failed" ? "failed" : "active";
    setStepperAgent(s.agent, st);
  });
  order.forEach((id) => {
    if (liveState.stepper[id] === "active") liveState.stepper[id] = "done";
  });
}

function updateStepperUI() {
  document.querySelectorAll(".qe-stepper__item").forEach((el) => {
    const id = el.dataset.agent;
    const state = liveState.stepper[id] || "pending";
    el.dataset.state = state;
    const icons = { pending: "○", active: "◉", done: "✓", failed: "✕", skipped: "—" };
    el.querySelector(".qe-stepper__icon").textContent = icons[state] || "○";
  });
}

// ─── SSE ─────────────────────────────────────────────────────────────
function connectSSE(runId) {
  if (activeSSE) activeSSE.close();
  activeSSE = new EventSource(`/stream/${runId}`);

  activeSSE.addEventListener("init", (e) => {
    const data = JSON.parse(e.data);
    if (data.steps?.length) {
      $("agentTimeline").innerHTML = renderTimelineHtml(
        data.steps.map((s) => ({
          agent: s.agent,
          status: s.status,
          message: s.message,
          metadata: s.metadata || {},
        }))
      );
    }
  });

  activeSSE.addEventListener("step", (e) => handleStepEvent(JSON.parse(e.data)));
  activeSSE.addEventListener("plan", (e) => handlePlanEvent(JSON.parse(e.data)));
  activeSSE.addEventListener("review", (e) => handleReviewEvent(JSON.parse(e.data)));
  activeSSE.addEventListener("security", (e) => handleSecurityEvent(JSON.parse(e.data)));
  activeSSE.addEventListener("tests", (e) => handleTestsEvent(JSON.parse(e.data)));
  activeSSE.addEventListener("heal", (e) => handleHealEvent(JSON.parse(e.data)));
  activeSSE.addEventListener("verdict", (e) => handleVerdictEvent(JSON.parse(e.data)));

  activeSSE.addEventListener("complete", () => finishRun(runId));
  activeSSE.addEventListener("status", (e) => {
    const data = JSON.parse(e.data);
    if (data.status === "completed" || data.status === "failed") finishRun(runId);
  });

  activeSSE.onerror = () => {
    if (activeSSE) {
      activeSSE.close();
      activeSSE = null;
    }
  };
}

function finishRun(runId) {
  setTimeout(() => {
    fetchRuns();
    fetchRunDetail(runId);
  }, 600);
  if (activeSSE) {
    activeSSE.close();
    activeSSE = null;
  }
}

function handleStepEvent(data) {
  const agent = data.agent || "system";
  setStepperAgent(agent, "active");
  appendTimelineLive(agent, data.message || "", "started");
}

function handlePlanEvent(data) {
  liveState.plan = data;
  setStepperAgent("orchestrator", "done");
  $("planContent").innerHTML = `
    <dl class="qe-dl">
      <dt>Change</dt><dd>${esc(data.change_type)}</dd>
      <dt>Risk</dt><dd>${esc(data.risk_level)}</dd>
      <dt>Summary</dt><dd>${esc(data.summary)}</dd>
      <dt>Files to test</dt><dd>${(data.files || []).map(esc).join(", ") || "—"}</dd>
    </dl>`;
}

function handleReviewEvent(data) {
  liveState.review = data;
  setStepperAgent("pr_reviewer", "done");
  $("reviewContent").innerHTML = `
    <p><strong>Score:</strong> ${data.score}/10 · <strong>${esc(data.recommendation)}</strong></p>
    <p class="qe-muted">${esc(data.summary || "")}</p>
    <p class="qe-muted">${data.issues ?? 0} issue(s) flagged</p>`;
}

function handleSecurityEvent(data) {
  liveState.security = data;
  setStepperAgent("security_scanner", "done");
  $("securityContent").innerHTML = `
    <p><strong>Score:</strong> ${data.score}/10 · <strong>${esc(data.verdict || data.recommendation)}</strong></p>
    <p class="qe-muted">Critical: ${data.critical ?? 0} · High: ${data.high ?? 0}</p>
    <p class="qe-muted">${esc(data.summary || "")}</p>`;
}

function handleTestsEvent(data) {
  liveState.tests = data;
  setStepperAgent("test_generator", "done");
  const ok = data.success ? "qe-text--ok" : "qe-text--err";
  $("testsContent").innerHTML = `
    <p class="${ok}">
      <strong>${data.passed ?? 0}</strong> passed ·
      <strong>${data.failed ?? 0}</strong> failed ·
      <strong>${data.errors ?? 0}</strong> errors
    </p>
    ${data.stdout ? `<pre class="qe-pre">${esc(truncate(data.stdout, 800))}</pre>` : ""}`;
  if (!data.success && (data.failed > 0 || data.errors > 0)) {
    setStepperAgent("self_healer", "pending");
    liveState.stepper.self_healer = "pending";
    $("panelHeal").hidden = false;
  }
}

function handleHealEvent(data) {
  $("panelHeal").hidden = false;
  setStepperAgent("self_healer", "active");
  liveState.heals.push(data);
  const li = document.createElement("li");
  li.className = "qe-heal-list__item";
  li.innerHTML = `
    <span>Attempt ${data.attempt}</span>
    <span>${data.success ? "✓ fixed" : "✗ still failing"}</span>
    <span>${data.passed ?? 0}p / ${data.failed ?? 0}f</span>
    <p class="qe-muted">${esc(data.fix_description || "")}</p>`;
  $("healList").appendChild(li);
  if (data.success) setStepperAgent("self_healer", "done");
}

function handleVerdictEvent(data) {
  liveState.verdict = data;
  setStepperAgent("decision_agent", "done");
  setStepperAgent("system", "active");
  if (data.scores) renderScores(data.scores);
  $("verdictBadge").textContent = data.verdict;
  $("verdictBadge").className = `qe-verdict__badge ${VERDICT_CLASS[data.verdict] || ""}`;
  $("verdictReasoning").textContent = data.reasoning || "";
  $("detailVerdict").hidden = false;
  $("detailVerdict").textContent = data.verdict;
  $("detailVerdict").className = `qe-badge qe-badge--verdict ${VERDICT_CLASS[data.verdict] || ""}`;
}

function renderScores(scores) {
  $("scoreGrid").innerHTML = SCORE_KEYS.map(({ key, label }) => {
    const v = scores[key];
    if (v == null) return "";
    const num = typeof v === "number" ? v : parseFloat(v);
    return `
      <div class="qe-score-card" data-score-key="${key}">
        <span class="qe-score-card__value">${Number.isInteger(num) ? num : num.toFixed(1)}</span>
        <span class="qe-score-card__label">${label}</span>
      </div>`;
  }).join("");
}

function renderGithubLinks(commentUrl, issueUrl) {
  const items = [];
  if (commentUrl) items.push(`<li><a href="${commentUrl}" target="_blank" rel="noopener">PR comment</a></li>`);
  if (issueUrl) items.push(`<li><a href="${issueUrl}" target="_blank" rel="noopener">Bug issue</a></li>`);
  $("githubLinks").innerHTML = items.length
    ? items.join("")
    : `<li><span class="qe-muted">No links yet.</span></li>`;
}

// ─── Timeline ────────────────────────────────────────────────────────
function renderTimelineHtml(steps) {
  if (!steps?.length) return `<p class="qe-muted">No steps yet.</p>`;
  return steps
    .map((s) => {
      const label = AGENT_MAP[s.agent]?.label || s.agent;
      return `
        <article class="qe-timeline__item qe-timeline__item--${s.status}">
          <header class="qe-timeline__head">
            <span class="qe-timeline__agent">${esc(label)}</span>
            <span class="qe-timeline__status">${s.status}</span>
          </header>
          <p class="qe-timeline__msg">${esc(s.message || "")}</p>
        </article>`;
    })
    .join("");
}

function appendTimelineLive(agent, message, status) {
  const tl = $("agentTimeline");
  if (tl.querySelector(".qe-muted")) tl.innerHTML = "";
  const label = AGENT_MAP[agent]?.label || agent;
  const el = document.createElement("article");
  el.className = `qe-timeline__item qe-timeline__item--${status} qe-timeline__item--live`;
  el.innerHTML = `
    <header class="qe-timeline__head">
      <span class="qe-timeline__agent">${esc(label)}</span>
      <span class="qe-timeline__status">${status}</span>
    </header>
    <p class="qe-timeline__msg">${esc(message)}</p>`;
  tl.appendChild(el);
  el.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ─── Trigger modal ───────────────────────────────────────────────────
function openTriggerModal() {
  const modal = $("triggerModal");
  if (modal.showModal) modal.showModal();
  else modal.setAttribute("open", "");
  $("triggerError").hidden = true;
}

function closeTriggerModal() {
  const modal = $("triggerModal");
  if (modal.close) modal.close();
  else modal.removeAttribute("open");
}

async function submitTrigger() {
  const repo = $("inputRepo").value.trim();
  const prNumber = parseInt($("inputPrNumber").value, 10);
  const topic = $("inputTopic").value.trim();
  const errEl = $("triggerError");
  errEl.hidden = true;

  if (!repo || !prNumber) {
    errEl.textContent = "Repository and PR number are required.";
    errEl.hidden = false;
    return;
  }

  $("btnSubmitTrigger").disabled = true;
  try {
    const res = await fetch("/trigger", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo, pr_number: prNumber, topic: topic || undefined }),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    closeTriggerModal();
    $("triggerForm").reset();
    await fetchRuns();
    selectRun(data.run_id);
  } catch (e) {
    errEl.textContent = e.message || "Trigger failed";
    errEl.hidden = false;
  } finally {
    $("btnSubmitTrigger").disabled = false;
  }
}

// ─── Utils ───────────────────────────────────────────────────────────
function esc(str) {
  const d = document.createElement("div");
  d.textContent = str == null ? "" : String(str);
  return d.innerHTML;
}

function truncate(str, n) {
  const s = String(str || "");
  return s.length <= n ? s : s.slice(0, n) + "…";
}

function relativeTime(iso) {
  const diff = (Date.now() - new Date(iso)) / 1000;
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

// Expose for inline onclick on run cards
window.selectRun = selectRun;
