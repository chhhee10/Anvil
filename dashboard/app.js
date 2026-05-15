"use strict";

// ── State ──────────────────────────────────────────────────────────
let runs = [], filteredRuns = [], activeRunId = null, activeSSE = null;

const AGENT_CONFIG = {
  orchestrator:     { icon: "🧠", label: "Orchestrator",      color: "rgba(201,74,46,.12)" },
  pr_reviewer:      { icon: "🔍", label: "PR Reviewer",        color: "rgba(45,95,160,.12)" },
  security_scanner: { icon: "🔒", label: "Security Scanner",   color: "rgba(184,134,42,.12)" },
  test_generator:   { icon: "🧪", label: "Test Generator",     color: "rgba(58,125,82,.12)" },
  self_healer:      { icon: "🔧", label: "Self Healer",        color: "rgba(100,80,200,.12)" },
  decision_agent:   { icon: "⚖️",  label: "Decision Agent",     color: "rgba(201,74,46,.12)" },
  researcher:       { icon: "📡", label: "Researcher",         color: "rgba(45,95,160,.12)" },
  code_analyst:     { icon: "💻", label: "Code Analyst",       color: "rgba(58,125,82,.12)" },
  writer:           { icon: "✍️",  label: "Writer",             color: "rgba(100,80,200,.12)" },
  critic:           { icon: "🎯", label: "Critic",             color: "rgba(184,134,42,.12)" },
  system:           { icon: "⚙️",  label: "System",             color: "rgba(68,68,68,.1)" },
};
const EVENT_LABELS = { push:"PUSH", pull_request:"PR", issues:"ISSUE", manual:"MANUAL", scheduled:"SCHED" };
const STATUS_LABELS = { pending:"PENDING", running:"RUNNING", completed:"DONE", failed:"FAILED" };

// ── Init ────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  initClock();
  initScrollReveal();
  initNavScroll();
  fetchRuns();
  setInterval(fetchRuns, 5000);
  checkHealth();
});

// ── Clock ───────────────────────────────────────────────────────────
function initClock() {
  const el = document.getElementById("heroClock");
  if (!el) return;
  const tick = () => {
    const now = new Date();
    el.textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  };
  tick(); setInterval(tick, 1000);
}

// ── Nav scroll shadow ────────────────────────────────────────────────
function initNavScroll() {
  const nav = document.getElementById("nav");
  if (!nav) return;
  window.addEventListener("scroll", () => {
    nav.classList.toggle("scrolled", window.scrollY > 20);
  }, { passive: true });
}

// ── Scroll-reveal text ───────────────────────────────────────────────
function initScrollReveal() {
  const el = document.getElementById("revealText");
  if (!el) return;
  const text = el.textContent;
  const words = text.split(" ");
  el.innerHTML = words.map(w => `<span class="word">${escHtml(w)} </span>`).join("");
  const wordEls = el.querySelectorAll(".word");
  const total = wordEls.length;

  function update() {
    const rect = el.parentElement.getBoundingClientRect();
    const wh = window.innerHeight;
    // progress: 0 when section enters bottom of viewport, 1 when section exits top
    const progress = Math.max(0, Math.min(1, (wh - rect.top) / (rect.height + wh * 0.4)));
    const revealed = Math.floor(progress * total);
    wordEls.forEach((w, i) => {
      w.style.color = i < revealed ? "var(--ink)" : "var(--ink-faint)";
    });
  }
  window.addEventListener("scroll", update, { passive: true });
  update();
}

// ── Health check ─────────────────────────────────────────────────────
async function checkHealth() {
  const el = document.getElementById("serverStatus");
  const txt = document.getElementById("serverText");
  const dot = el?.querySelector(".blink-dot");
  try {
    const res = await fetch("/health");
    if (res.ok) {
      if (txt) txt.textContent = "ONLINE";
      if (dot) { dot.style.background = "#4caf50"; dot.style.boxShadow = "0 0 6px #4caf50"; }
    } else throw new Error();
  } catch {
    if (txt) txt.textContent = "OFFLINE";
    if (dot) { dot.style.background = "#e05030"; dot.style.boxShadow = "0 0 6px #e05030"; }
  }
}

// ── Navigate to dashboard ─────────────────────────────────────────────
function scrollToDashboard() {
  document.getElementById("dashboard")?.scrollIntoView({ behavior: "smooth" });
}

// ── Fetch & render runs ───────────────────────────────────────────────
async function fetchRuns() {
  try {
    const res = await fetch("/status");
    if (!res.ok) return;
    runs = await res.json();
    filterRuns();
    updateDashStats();
    if (activeRunId) {
      const r = runs.find(r => r.run_id === activeRunId);
      if (r && r.status === "running") fetchRunDetail(activeRunId);
    }
  } catch {}
}

function filterRuns() {
  const q = (document.getElementById("searchInput")?.value || "").toLowerCase().trim();
  filteredRuns = q ? runs.filter(r =>
    r.topic.toLowerCase().includes(q) ||
    (r.repo && r.repo.toLowerCase().includes(q)) ||
    r.status.toLowerCase().includes(q)
  ) : [...runs];
  renderRunList();
}

function renderRunList() {
  const list = document.getElementById("runList");
  const cnt  = document.getElementById("runCount");
  if (cnt) cnt.textContent = runs.length;
  if (!list) return;
  if (!filteredRuns.length) {
    list.innerHTML = runs.length
      ? `<div class="dash-empty"><span class="dash-empty-icon">🔍</span><p>No matches found.</p></div>`
      : `<div class="dash-empty"><span class="dash-empty-icon">⚙</span><p>No runs yet.<br>Click NEW RUN to start.</p></div>`;
    return;
  }
  list.innerHTML = filteredRuns.map(r => `
    <div class="run-card${activeRunId === r.run_id ? " active" : ""}" onclick="selectRun('${r.run_id}')">
      <div class="run-card-top">
        <span class="run-event-badge">${EVENT_LABELS[r.event_type] || r.event_type}</span>
        <span class="run-status-dot status-dot-${r.status}"></span>
      </div>
      <div class="run-topic">${escHtml(r.topic)}</div>
      <div class="run-meta">
        <span class="run-time">${relativeTime(r.created_at)}</span>
        ${r.repo ? `<span class="run-repo">· ${escHtml(r.repo)}</span>` : ""}
        <span class="run-status-label label-${r.status}">${STATUS_LABELS[r.status] || r.status}</span>
      </div>
    </div>
  `).join("");
}

function updateDashStats() {
  setText("dTotal",   runs.length);
  setText("dDone",    runs.filter(r => r.status === "completed").length);
  setText("dRunning", runs.filter(r => r.status === "running").length);
  setText("dFailed",  runs.filter(r => r.status === "failed").length);
}

// ── Select run ────────────────────────────────────────────────────────
async function selectRun(runId) {
  activeRunId = runId;
  renderRunList();
  if (activeSSE) { activeSSE.close(); activeSSE = null; }
  const run = runs.find(r => r.run_id === runId);
  if (run) renderDetailSkeleton(run);
  await fetchRunDetail(runId);
  if (run && run.status === "running") connectSSE(runId);
}

async function fetchRunDetail(runId) {
  try {
    const res = await fetch(`/status/${runId}`);
    if (!res.ok) return;
    renderDetail(await res.json());
  } catch {}
}

// ── SSE ───────────────────────────────────────────────────────────────
function connectSSE(runId) {
  if (activeSSE) activeSSE.close();
  activeSSE = new EventSource(`/stream/${runId}`);
  activeSSE.addEventListener("step", e => appendLiveStep(JSON.parse(e.data)));
  activeSSE.addEventListener("status", e => {
    const d = JSON.parse(e.data);
    if (d.status === "completed" || d.status === "failed") {
      setTimeout(() => { fetchRuns(); fetchRunDetail(runId); }, 600);
    }
  });
  activeSSE.addEventListener("complete", () => {
    setTimeout(() => { fetchRuns(); fetchRunDetail(runId); }, 1000);
    if (activeSSE) { activeSSE.close(); activeSSE = null; }
  });
  activeSSE.onerror = () => { if (activeSSE) { activeSSE.close(); activeSSE = null; } };
}

function appendLiveStep(data) {
  const tl = document.getElementById("timeline");
  if (!tl) return;
  const cfg = AGENT_CONFIG[data.agent] || { icon: "⚙️", label: data.agent, color: "rgba(68,68,68,.1)" };
  const el = document.createElement("div");
  el.className = "timeline-step";
  el.innerHTML = `
    <div class="step-icon" style="background:${cfg.color};border-color:${cfg.color}">${cfg.icon}</div>
    <div class="step-body">
      <div class="step-agent">${cfg.label}</div>
      <div class="step-message">${escHtml(data.message || "")}</div>
    </div>
    <div class="step-right"><span class="step-spinner">↻</span></div>`;
  tl.appendChild(el);
  el.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ── Render Detail ─────────────────────────────────────────────────────
function renderDetailSkeleton(run) {
  const pane = document.getElementById("detailPane");
  if (!pane) return;
  pane.innerHTML = `<div class="run-detail-wrap">
    <div class="run-detail-title">${escHtml(run.topic)}</div>
    <div class="run-detail-meta">
      <span class="run-event-badge">${EVENT_LABELS[run.event_type] || run.event_type}</span>
      <span class="meta-status ms-${run.status}">${STATUS_LABELS[run.status] || run.status}</span>
    </div>
    <div class="section-title">AGENT TIMELINE</div>
    <div class="timeline" id="timeline"><div class="timeline-loading">Loading…</div></div>
  </div>`;
}

function renderDetail(d) {
  if (activeRunId !== d.run_id) return;
  const pane = document.getElementById("detailPane");
  if (!pane) return;
  const scoreHtml = d.critic_score ? renderScores(d.critic_score) : "";
  pane.innerHTML = `<div class="run-detail-wrap">
    <div class="run-detail-title">${escHtml(d.topic)}</div>
    <div class="run-detail-meta">
      <span class="run-event-badge">${EVENT_LABELS[d.event_type] || d.event_type}</span>
      <span class="meta-status ms-${d.status}">${STATUS_LABELS[d.status] || d.status}</span>
      ${d.repo ? `<span class="meta-chip">${escHtml(d.repo)}</span>` : ""}
      <span class="meta-chip">${d.run_id.substring(0,8)}</span>
      <span class="meta-chip">${relativeTime(d.created_at)}</span>
    </div>
    ${scoreHtml}
    ${d.has_report ? `<div class="report-preview-card">
      <span class="report-preview-title">📄 REPORT READY</span>
      <button class="btn-cta btn-sm" onclick="openReport('${d.run_id}')">VIEW →</button>
    </div>` : ""}
    ${d.error ? `<div class="error-box">❌ ${escHtml(d.error)}</div>` : ""}
    <div class="section-title">AGENT TIMELINE</div>
    <div class="timeline" id="timeline">${renderTimeline(d.steps)}</div>
  </div>`;
  if (d.status === "running" && !activeSSE) connectSSE(d.run_id);
}

function renderScores(s) {
  const cls = v => v >= 7 ? "score-high" : v >= 5 ? "score-med" : "score-low";
  const fill = v => v >= 7 ? "fill-high" : v >= 5 ? "fill-med" : "fill-low";
  const pct = v => Math.round(v * 10);
  return `<div class="score-section">
    <div class="section-title">CRITIC SCORES</div>
    <div class="score-grid">
      <div class="score-card"><div class="score-val ${cls(s.overall)}">${s.overall.toFixed(1)}</div><div class="score-bar"><div class="score-bar-fill ${fill(s.overall)}" style="width:${pct(s.overall)}%"></div></div><div class="score-label">OVERALL</div></div>
      <div class="score-card"><div class="score-val ${cls(s.accuracy)}">${s.accuracy}<span class="score-denom">/10</span></div><div class="score-bar"><div class="score-bar-fill ${fill(s.accuracy)}" style="width:${pct(s.accuracy)}%"></div></div><div class="score-label">ACCURACY</div></div>
      <div class="score-card"><div class="score-val ${cls(s.completeness)}">${s.completeness}<span class="score-denom">/10</span></div><div class="score-bar"><div class="score-bar-fill ${fill(s.completeness)}" style="width:${pct(s.completeness)}%"></div></div><div class="score-label">COMPLETENESS</div></div>
      <div class="score-card"><div class="score-val ${cls(s.actionability)}">${s.actionability}<span class="score-denom">/10</span></div><div class="score-bar"><div class="score-bar-fill ${fill(s.actionability)}" style="width:${pct(s.actionability)}%"></div></div><div class="score-label">ACTIONABILITY</div></div>
    </div>
    ${s.feedback ? `<div class="critic-feedback">"${escHtml(s.feedback)}"</div>` : ""}
    ${s.revision_requests?.length ? `<div class="revision-chips">${s.revision_requests.map(r => `<span class="revision-chip">⚠ ${escHtml(r)}</span>`).join("")}</div>` : ""}
  </div>`;
}

function renderTimeline(steps) {
  if (!steps?.length) return `<div class="timeline-empty">No steps yet…</div>`;
  return steps.map(s => {
    const cfg = AGENT_CONFIG[s.agent] || { icon: "⚙️", label: s.agent, color: "rgba(68,68,68,.1)" };
    const dur = s.completed_at && s.started_at
      ? `${((new Date(s.completed_at) - new Date(s.started_at)) / 1000).toFixed(1)}s`
      : "";
    const isRunning = s.status === "started";
    const statusIcons = { completed: "✅", failed: "❌", skipped: "⏭️" };

    // Build meta line
    const meta = [];
    if (s.metadata?.searches) meta.push(`${s.metadata.searches} searches`);
    if (s.metadata?.chars)    meta.push(`${(s.metadata.chars / 1000).toFixed(1)}k chars`);
    if (s.metadata?.files)    meta.push(`${s.metadata.files} files`);

    return `<div class="timeline-step">
      <div class="step-icon" style="background:${cfg.color};border-color:${cfg.color}">${cfg.icon}</div>
      <div class="step-body">
        <div class="step-agent">${cfg.label}</div>
        <div class="step-message">${escHtml(s.message || "")}</div>
        ${meta.length ? `<div class="step-meta">${meta.join(" · ")}</div>` : ""}
      </div>
      <div class="step-right">
        ${dur ? `<span class="step-dur">${dur}</span>` : ""}
        ${isRunning
          ? `<span class="step-spinner">↻</span>`
          : `<span class="step-status-icon">${statusIcons[s.status] || ""}</span>`}
      </div>
    </div>`;
  }).join("");
}

// ── Report Modal ───────────────────────────────────────────────────────
let currentReportMd = "";

async function openReport(runId) {
  document.getElementById("reportModal").classList.add("open");
  document.getElementById("reportModalBody").innerHTML = `<div class="report-loading">⏳ LOADING REPORT…</div>`;
  try {
    const res = await fetch(`/report/${runId}`);
    if (!res.ok) throw new Error("Not found");
    const data = await res.json();
    currentReportMd = data.report_markdown;
    document.getElementById("reportModalTitle").textContent = `📄 ${data.topic}`;
    document.getElementById("reportModalBody").innerHTML = `<div class="report-md">${simpleMarkdown(data.report_markdown)}</div>`;
  } catch (e) {
    document.getElementById("reportModalBody").innerHTML = `<div class="report-loading" style="color:#b84040">Failed: ${escHtml(e.message)}</div>`;
  }
}

function closeReportModal() { document.getElementById("reportModal").classList.remove("open"); }
function closeReportModalIfOutside(e) { if (e.target === document.getElementById("reportModal")) closeReportModal(); }

async function copyReport() {
  if (!currentReportMd) return;
  try {
    await navigator.clipboard.writeText(currentReportMd);
    const btn = document.querySelector("#reportModal .btn-ghost");
    if (btn) { btn.textContent = "✅ COPIED!"; setTimeout(() => btn.textContent = "📋 COPY", 2000); }
  } catch {}
}

// ── Trigger Modal ──────────────────────────────────────────────────────
function openTriggerModal() {
  document.getElementById("triggerModal").classList.add("open");
  setTimeout(() => document.getElementById("topicInput").focus(), 100);
}
function closeTriggerModal() {
  document.getElementById("triggerModal").classList.remove("open");
  document.getElementById("submitLabel").textContent = "▶ LAUNCH";
  document.getElementById("submitTrigger").disabled = false;
}
function closeTriggerModalIfOutside(e) { if (e.target === document.getElementById("triggerModal")) closeTriggerModal(); }
function setTopic(t) { document.getElementById("topicInput").value = t; document.getElementById("topicInput").focus(); }

async function submitTrigger() {
  const topic = document.getElementById("topicInput").value.trim();
  if (!topic) {
    const inp = document.getElementById("topicInput");
    inp.style.borderColor = "#b84040";
    setTimeout(() => inp.style.borderColor = "", 1500);
    return;
  }
  const btn = document.getElementById("submitTrigger");
  const lbl = document.getElementById("submitLabel");
  btn.disabled = true; lbl.textContent = "⏳ LAUNCHING…";
  try {
    const res = await fetch("/trigger", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic, repo: document.getElementById("repoInput").value.trim() || null, context: document.getElementById("contextInput").value.trim() || null }),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    closeTriggerModal();
    ["topicInput","repoInput","contextInput"].forEach(id => document.getElementById(id).value = "");
    setTimeout(() => scrollToDashboard(), 150);
    await fetchRuns();
    setTimeout(() => selectRun(data.run_id), 400);
  } catch (e) {
    lbl.textContent = "❌ ERROR";
    setTimeout(() => { lbl.textContent = "▶ LAUNCH"; btn.disabled = false; }, 3000);
  }
}

document.addEventListener("keydown", e => {
  if (e.key === "Escape") { closeTriggerModal(); closeReportModal(); }
  if (e.key === "Enter" && document.getElementById("triggerModal").classList.contains("open") && document.activeElement?.tagName !== "TEXTAREA") submitTrigger();
});

// ── Utilities ──────────────────────────────────────────────────────────
function escHtml(s) { const d = document.createElement("div"); d.textContent = s || ""; return d.innerHTML; }
function setText(id, v) { const el = document.getElementById(id); if (el) el.textContent = v; }
function relativeTime(iso) {
  if (!iso) return "—";
  const d = (Date.now() - new Date(iso)) / 1000;
  if (d < 5) return "just now";
  if (d < 60) return `${Math.floor(d)}s ago`;
  if (d < 3600) return `${Math.floor(d/60)}m ago`;
  if (d < 86400) return `${Math.floor(d/3600)}h ago`;
  return `${Math.floor(d/86400)}d ago`;
}

function simpleMarkdown(md) {
  if (!md) return "";
  return md
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/```[\s\S]*?```/g, b => `<pre class="report-code-block"><code>${b.replace(/^```[^\n]*\n?/,"").replace(/\n?```$/,"")}</code></pre>`)
    .replace(/^### (.+)$/gm,"<h3>$1</h3>")
    .replace(/^## (.+)$/gm,"<h2>$1</h2>")
    .replace(/^# (.+)$/gm,"<h1>$1</h1>")
    .replace(/\*\*(.+?)\*\*/g,"<strong>$1</strong>")
    .replace(/\*(.+?)\*/g,"<em>$1</em>")
    .replace(/`([^`]+)`/g,"<code>$1</code>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/^---+$/gm,"<hr>")
    .replace(/^- (.+)$/gm,"<li>$1</li>")
    .replace(/(<li>.*<\/li>\n?)+/g, s => `<ul>${s}</ul>`)
    .replace(/\n\n/g,"</p><p>")
    .replace(/^(?!<[hupoli])(.+)$/gm,"$1");
}
