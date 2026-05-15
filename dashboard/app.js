/* ═══════════════════════════════════════════════════════════════════
   NewsRoom AI — Dashboard JS
   Handles: run list polling, SSE live updates, trigger modal,
            report modal with markdown rendering, agent timeline
   ═══════════════════════════════════════════════════════════════════ */

"use strict";

// ─── State ──────────────────────────────────────────────────────────
let runs = [];
let activeRunId = null;
let activeSSE = null;
let pollingTimer = null;

// ─── Agent config ────────────────────────────────────────────────────
const AGENT_CONFIG = {
  orchestrator: { icon: "🧠", label: "Orchestrator" },
  researcher:   { icon: "🔍", label: "Researcher"   },
  code_analyst: { icon: "💻", label: "Code Analyst" },
  writer:       { icon: "✍️",  label: "Writer"       },
  critic:       { icon: "🎯", label: "Critic"       },
  system:       { icon: "⚙️",  label: "System"       },
};

const EVENT_LABELS = {
  push: "Push", pull_request: "Pull Request",
  issues: "Issues", manual: "Manual", scheduled: "Scheduled",
};

// ─── Init ────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  fetchRuns();
  startPolling();
});

function startPolling() {
  pollingTimer = setInterval(() => {
    fetchRuns();
    if (activeRunId) {
      const run = runs.find(r => r.run_id === activeRunId);
      if (run && run.status === "running") {
        fetchRunDetail(activeRunId);
      }
    }
  }, 4000);
}

// ─── Fetch runs ───────────────────────────────────────────────────────
async function fetchRuns() {
  try {
    const res = await fetch("/status");
    if (!res.ok) return;
    runs = await res.json();
    renderRunList();
  } catch (e) {
    console.warn("Fetch runs failed:", e);
  }
}

function renderRunList() {
  const list = document.getElementById("runList");
  const count = document.getElementById("runCount");
  count.textContent = `${runs.length} run${runs.length !== 1 ? "s" : ""}`;

  if (!runs.length) {
    list.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">🤖</div>
        <p>No runs yet.<br/>Click <strong>+ New Run</strong> to trigger the pipeline.</p>
      </div>`;
    return;
  }

  list.innerHTML = runs.map(r => `
    <div class="run-card${activeRunId === r.run_id ? " active" : ""}"
         id="card-${r.run_id}"
         onclick="selectRun('${r.run_id}')">
      <div class="run-card-top">
        <span class="run-event-badge event-${r.event_type}">${EVENT_LABELS[r.event_type] || r.event_type}</span>
        <span class="run-status-dot status-${r.status}"></span>
      </div>
      <div class="run-topic">${escHtml(r.topic)}</div>
      <div class="run-meta">
        <span class="run-time">${relativeTime(r.created_at)}</span>
        ${r.repo ? `<span class="run-repo">· ${escHtml(r.repo)}</span>` : ""}
      </div>
    </div>
  `).join("");
}

// ─── Select Run ───────────────────────────────────────────────────────
async function selectRun(runId) {
  activeRunId = runId;
  renderRunList();

  // Kill existing SSE
  if (activeSSE) { activeSSE.close(); activeSSE = null; }

  const run = runs.find(r => r.run_id === runId);
  if (run) renderDetailSkeleton(run);

  await fetchRunDetail(runId);

  // If running, open SSE for live updates
  if (run && run.status === "running") {
    connectSSE(runId);
  }
}

async function fetchRunDetail(runId) {
  try {
    const res = await fetch(`/status/${runId}`);
    if (!res.ok) return;
    const detail = await res.json();
    renderDetail(detail);
  } catch (e) {
    console.warn("Fetch detail failed:", e);
  }
}

// ─── SSE Live Updates ─────────────────────────────────────────────────
function connectSSE(runId) {
  if (activeSSE) activeSSE.close();
  activeSSE = new EventSource(`/stream/${runId}`);

  activeSSE.addEventListener("step", (e) => {
    const data = JSON.parse(e.data);
    appendLiveStep(data);
  });

  activeSSE.addEventListener("status", (e) => {
    const data = JSON.parse(e.data);
    if (data.status === "completed" || data.status === "failed") {
      setTimeout(() => {
        fetchRuns();
        fetchRunDetail(runId);
      }, 500);
    }
  });

  activeSSE.addEventListener("complete", (e) => {
    setTimeout(() => {
      fetchRuns();
      fetchRunDetail(runId);
    }, 1000);
    if (activeSSE) { activeSSE.close(); activeSSE = null; }
  });

  activeSSE.onerror = () => {
    if (activeSSE) { activeSSE.close(); activeSSE = null; }
  };
}

let liveStepBuffer = [];
function appendLiveStep(data) {
  const timeline = document.getElementById("timeline");
  if (!timeline) return;

  const agent = data.agent || "system";
  const cfg = AGENT_CONFIG[agent] || { icon: "⚙️", label: agent };
  const stepEl = document.createElement("div");
  stepEl.className = "timeline-step step-running";
  stepEl.id = `live-step-${Date.now()}`;
  stepEl.innerHTML = `
    <div class="step-icon icon-${agent}">${cfg.icon}</div>
    <div class="step-body">
      <div class="step-agent agent-${agent}">${cfg.label}</div>
      <div class="step-message">${escHtml(data.message || "")}</div>
    </div>
    <span class="step-spinner">⟳</span>
  `;
  timeline.appendChild(stepEl);
  stepEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ─── Render Detail ────────────────────────────────────────────────────
function renderDetailSkeleton(run) {
  const pane = document.getElementById("detailPane");
  pane.innerHTML = `
    <div class="run-detail">
      <div class="run-detail-header">
        <div>
          <div class="run-detail-title">${escHtml(run.topic)}</div>
          <div class="run-detail-meta">
            <span class="run-event-badge event-${run.event_type}">${EVENT_LABELS[run.event_type] || run.event_type}</span>
            <span class="meta-status ms-${run.status}">${run.status.toUpperCase()}</span>
            ${run.repo ? `<span class="meta-chip">${escHtml(run.repo)}</span>` : ""}
            <span class="meta-chip">${run.run_id.substring(0, 8)}</span>
          </div>
        </div>
      </div>
      <div class="section-title">Agent Timeline</div>
      <div class="timeline" id="timeline">
        <div style="color:var(--text-3);font-size:13px;padding:12px;">Loading steps...</div>
      </div>
    </div>
  `;
}

function renderDetail(detail) {
  if (activeRunId !== detail.run_id) return;

  const pane = document.getElementById("detailPane");

  const scoreHtml = detail.critic_score ? renderScores(detail.critic_score) : "";
  const reportHtml = detail.has_report ? `
    <div class="report-preview-card">
      <div class="report-preview-header">
        <div class="report-preview-title">📄 Intelligence Report</div>
        <button class="btn btn-ghost btn-sm" onclick="openReport('${detail.run_id}')">
          View Full Report →
        </button>
      </div>
      <div class="report-preview-snippet" id="reportSnippet-${detail.run_id}">Loading preview...</div>
    </div>
  ` : "";

  pane.innerHTML = `
    <div class="run-detail">
      <div class="run-detail-header">
        <div style="flex:1">
          <div class="run-detail-title">${escHtml(detail.topic)}</div>
          <div class="run-detail-meta">
            <span class="run-event-badge event-${detail.event_type}">${EVENT_LABELS[detail.event_type] || detail.event_type}</span>
            <span class="meta-status ms-${detail.status}">${detail.status.toUpperCase()}</span>
            ${detail.repo ? `<span class="meta-chip">${escHtml(detail.repo)}</span>` : ""}
            <span class="meta-chip">${detail.run_id.substring(0, 8)}</span>
            <span class="meta-chip">${relativeTime(detail.created_at)}</span>
          </div>
        </div>
      </div>

      ${scoreHtml}

      ${detail.has_report ? `
        <div class="report-preview-card">
          <div class="report-preview-header">
            <div class="report-preview-title">📄 Intelligence Report Generated</div>
            <button class="btn btn-primary btn-sm" onclick="openReport('${detail.run_id}')">
              View Full Report →
            </button>
          </div>
        </div>
      ` : ""}

      ${detail.error ? `
        <div style="padding:14px;border-radius:8px;background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);color:#ef4444;font-size:13px;margin-bottom:20px;">
          ❌ Error: ${escHtml(detail.error)}
        </div>
      ` : ""}

      <div class="section-title">Agent Timeline</div>
      <div class="timeline" id="timeline">
        ${renderTimeline(detail.steps)}
      </div>
    </div>
  `;

  // Reconnect SSE if still running
  if (detail.status === "running" && !activeSSE) {
    connectSSE(detail.run_id);
  }
}

function renderScores(score) {
  const scoreColor = (v) => v >= 7 ? "score-high" : v >= 5 ? "score-med" : "score-low";
  return `
    <div class="score-grid">
      <div class="score-card">
        <div class="score-val ${scoreColor(score.overall)}">${score.overall.toFixed(1)}</div>
        <div class="score-label">Overall</div>
      </div>
      <div class="score-card">
        <div class="score-val ${scoreColor(score.accuracy)}">${score.accuracy}/10</div>
        <div class="score-label">Accuracy</div>
      </div>
      <div class="score-card">
        <div class="score-val ${scoreColor(score.completeness)}">${score.completeness}/10</div>
        <div class="score-label">Completeness</div>
      </div>
      <div class="score-card">
        <div class="score-val ${scoreColor(score.actionability)}">${score.actionability}/10</div>
        <div class="score-label">Actionability</div>
      </div>
    </div>
  `;
}

function renderTimeline(steps) {
  if (!steps || !steps.length) return `<div style="color:var(--text-3);font-size:13px;padding:12px;">No steps yet...</div>`;
  return steps.map(s => {
    const cfg = AGENT_CONFIG[s.agent] || { icon: "⚙️", label: s.agent };
    const dur = s.completed_at
      ? `${((new Date(s.completed_at) - new Date(s.started_at)) / 1000).toFixed(1)}s`
      : "";
    const isRunning = s.status === "started";
    const metaParts = [];
    if (dur) metaParts.push(dur);
    if (s.metadata && Object.keys(s.metadata).length) {
      const meta = s.metadata;
      if (meta.searches) metaParts.push(`${meta.searches} searches`);
      if (meta.files) metaParts.push(`${meta.files} files`);
      if (meta.overall) metaParts.push(`score: ${meta.overall}`);
      if (meta.chars) metaParts.push(`${(meta.chars/1000).toFixed(1)}k chars`);
    }
    return `
      <div class="timeline-step step-${s.status}">
        <div class="step-icon icon-${s.agent}">${cfg.icon}</div>
        <div class="step-body">
          <div class="step-agent agent-${s.agent}">${cfg.label}</div>
          <div class="step-message">${escHtml(s.message || "")}</div>
          ${metaParts.length ? `<div class="step-meta">${metaParts.join(" · ")}</div>` : ""}
        </div>
        ${isRunning ? `<span class="step-spinner">⟳</span>` : statusIcon(s.status)}
      </div>
    `;
  }).join("");
}

function statusIcon(status) {
  const icons = { completed: "✅", failed: "❌", skipped: "⏭️", started: "⟳" };
  return `<span style="font-size:14px">${icons[status] || ""}</span>`;
}

// ─── Report Modal ─────────────────────────────────────────────────────
let currentReportMd = "";

async function openReport(runId) {
  document.getElementById("reportModal").classList.add("open");
  document.getElementById("reportModalBody").innerHTML = `<div class="report-loading">⏳ Loading report...</div>`;

  try {
    const res = await fetch(`/report/${runId}`);
    if (!res.ok) throw new Error("Report not found");
    const data = await res.json();
    currentReportMd = data.report_markdown;
    document.getElementById("reportModalTitle").textContent = `📄 ${data.topic}`;
    document.getElementById("reportModalBody").innerHTML = `<div class="report-md">${simpleMarkdown(data.report_markdown)}</div>`;
  } catch (e) {
    document.getElementById("reportModalBody").innerHTML = `<div class="report-loading" style="color:var(--red)">Failed to load report: ${e.message}</div>`;
  }
}

function closeReportModal() {
  document.getElementById("reportModal").classList.remove("open");
}

function closeReportModalIfOutside(e) {
  if (e.target === document.getElementById("reportModal")) closeReportModal();
}

async function copyReport() {
  if (!currentReportMd) return;
  try {
    await navigator.clipboard.writeText(currentReportMd);
    const btn = document.querySelector(".report-header-actions .btn-ghost");
    if (btn) { btn.textContent = "✅ Copied!"; setTimeout(() => btn.textContent = "📋 Copy", 2000); }
  } catch (e) {}
}

// ─── Trigger Modal ────────────────────────────────────────────────────
function openTriggerModal() {
  document.getElementById("triggerModal").classList.add("open");
  setTimeout(() => document.getElementById("topicInput").focus(), 100);
}

function closeTriggerModal() {
  document.getElementById("triggerModal").classList.remove("open");
}

function closeTriggerModalIfOutside(e) {
  if (e.target === document.getElementById("triggerModal")) closeTriggerModal();
}

function setTopic(topic) {
  document.getElementById("topicInput").value = topic;
  document.getElementById("topicInput").focus();
}

async function submitTrigger() {
  const topic = document.getElementById("topicInput").value.trim();
  if (!topic) {
    document.getElementById("topicInput").style.borderColor = "var(--red)";
    setTimeout(() => document.getElementById("topicInput").style.borderColor = "", 1500);
    return;
  }

  const btn = document.getElementById("submitTrigger");
  const label = document.getElementById("submitLabel");
  btn.disabled = true;
  label.textContent = "⏳ Launching...";

  try {
    const body = {
      topic,
      repo: document.getElementById("repoInput").value.trim() || null,
      context: document.getElementById("contextInput").value.trim() || null,
    };
    const res = await fetch("/trigger", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();

    closeTriggerModal();
    document.getElementById("topicInput").value = "";
    document.getElementById("repoInput").value = "";
    document.getElementById("contextInput").value = "";

    // Refresh and select new run
    await fetchRuns();
    setTimeout(() => selectRun(data.run_id), 300);
  } catch (e) {
    label.textContent = `❌ Error: ${e.message}`;
    setTimeout(() => label.textContent = "▶ Launch Pipeline", 3000);
  } finally {
    btn.disabled = false;
    label.textContent = "▶ Launch Pipeline";
  }
}

// Enter key submit
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeTriggerModal();
    closeReportModal();
  }
  if (e.key === "Enter" && document.getElementById("triggerModal").classList.contains("open")) {
    submitTrigger();
  }
});

// ─── Utilities ────────────────────────────────────────────────────────
function escHtml(str) {
  const div = document.createElement("div");
  div.textContent = str || "";
  return div.innerHTML;
}

function relativeTime(isoStr) {
  const diff = (Date.now() - new Date(isoStr)) / 1000;
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

// Minimal markdown renderer (no external lib needed)
function simpleMarkdown(md) {
  if (!md) return "";
  return md
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    // Headers
    .replace(/^### (.+)$/gm, "<h3>$1</h3>")
    .replace(/^## (.+)$/gm, "<h2>$1</h2>")
    .replace(/^# (.+)$/gm, "<h1>$1</h1>")
    // Bold/italic
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    // Code
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    // Links
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    // Table rows (basic)
    .replace(/^\|(.+)\|$/gm, (line) => {
      const cells = line.split("|").filter(Boolean).map(c => c.trim());
      const isHeader = cells.some(c => /^[-:]+$/.test(c));
      if (isHeader) return "";
      const tag = line.includes("---") ? "th" : "td";
      return "<tr>" + cells.map(c => `<${tag}>${c}</${tag}>`).join("") + "</tr>";
    })
    // HR
    .replace(/^---+$/gm, "<hr>")
    // Bullet lists
    .replace(/^- (.+)$/gm, "<li>$1</li>")
    .replace(/(<li>.*<\/li>\n?)+/g, s => `<ul>${s}</ul>`)
    // Numbered lists
    .replace(/^\d+\. (.+)$/gm, "<li>$1</li>")
    // Paragraphs
    .replace(/\n\n/g, "</p><p>")
    .replace(/^(?!<[huplti])(.+)$/gm, "$1")
    .replace(/^(.+)$/, "<p>$1</p>");
}
