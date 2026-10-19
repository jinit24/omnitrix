"use strict";
const $ = (s) => document.querySelector(s);
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const text = (v) => (typeof v === "string" ? v : JSON.stringify(v, null, 2));
const summary = (v) => {
  if (typeof v === "string") return v;
  if (!v) return "";
  if (v.rationale) return `${human(v.action)} · ${v.rationale}`;
  if (v.result) return summary(v.result);
  if (v.error) return v.error;
  if (v.instruction || v.text || v.feedback)
    return text(v.instruction || v.text || v.feedback);
  if (v.passed != null)
    return `${v.passed ? "Passed" : "Failed"}${v.candidate ? " · candidate " + v.candidate.slice(0, 8) : ""}`;
  if (v.status || v.state) return human(v.status || v.state);
  if (v.kind) return human(v.kind);
  return Object.keys(v)
    .filter((k) => v[k] != null)
    .slice(0, 3)
    .map(human)
    .join(" · ");
};
const human = (v) => String(v || "pending").replaceAll("_", " ");
const short = (v, n = 130) => {
  const s = text(v) || "";
  return s.length > n ? s.slice(0, n) + "…" : s;
};
const age = (v) => {
  if (!v) return "Time not recorded";
  const t = stamp(v);
  if (!Number.isFinite(t)) return "Time not recorded";
  const s = Math.max(0, (Date.now() - t) / 1000);
  return s < 60
    ? "Just now"
    : s < 3600
      ? `${Math.floor(s / 60)}m ago`
      : s < 86400
        ? `${Math.floor(s / 3600)}h ago`
        : `${Math.floor(s / 86400)}d ago`;
};
const active = (s) =>
  [
    "running",
    "active",
    "started",
    "implementing",
    "verifying",
    "researching",
  ].includes(s);
const done = (s) =>
  [
    "completed",
    "complete",
    "supported",
    "verified",
    "ready_for_audit",
    "succeeded",
    "discovered",
  ].includes(s);
const blocked = (s) =>
  /block|fail|uncertain|escalat|needs_research|paused|waiting/.test(s || "");
const badge = (s) =>
  `<span class="badge ${active(s) ? "green" : done(s) ? "purple" : blocked(s) ? "orange" : ""}">${esc(human(s))}</span>`;
let state = { goals: [] },
  goalId = null,
  view = "overview",
  filter = "",
  query = "",
  records = [],
  fetching = false,
  drawerTrigger = null;
let lastGood = null, disconnected = false, composing = false, sending = false, proposalId = null, creating = null;
let backend = null, localSession = { login_available: false, authenticated: false };
const compose = {
  command: "chat",
  text: "",
  task: "",
  note: "",
  pending: null,
};
const endpoint = (path) =>
  backend ? new URL(path, backend.url).href : "/" + path;
const auth = () => backend ? { Authorization: "Bearer " + backend.token } :
  localSession.csrf_token || state.auth?.csrf_token ? { "X-CSRF-Token": localSession.csrf_token || state.auth.csrf_token } : {};
function pendingKey() {
  return "omnitrix:pending:" + (backend?.url || new URL("/", location.href).href);
}
function savePending() {
  try {
    if (compose.pending) sessionStorage.setItem(pendingKey(), JSON.stringify(compose.pending));
    else sessionStorage.removeItem(pendingKey());
  } catch (_) { /* Storage may be unavailable; request identity still survives in this tab. */ }
}
function restorePending() {
  compose.pending = null;
  try { compose.pending = JSON.parse(sessionStorage.getItem(pendingKey())); } catch (_) { /* Ignore unavailable storage. */ }
  const payload = compose.pending?.payload;
  if (!payload || typeof payload.text !== "string" || typeof payload.request_id !== "string") {
    compose.pending = null;
    return;
  }
  goalId = payload.goal_id;
  compose.command = payload.command === "accept" ? "chat" : payload.command;
  if (["pause", "cancel", "reconcile", "steer"].includes(payload.command)) {
    const [task, ...instruction] = payload.text.split(" ");
    compose.task = task;
    compose.text = instruction.join(" ");
  } else if (payload.command !== "accept") compose.text = payload.text;
  compose.note = "Earlier delivery is unconfirmed. Retry unchanged to retrieve its receipt.";
}

const titles = {
  chat: ["Manager chat", "Talk to the manager and steer bounded work."],
  overview: [
    "Mission control",
    "A clear view of what your manager is doing, and why.",
  ],
  tasks: [
    "Task workspace",
    "Bounded work, independent checks, and every outcome in one place.",
  ],
  hierarchy: [
    "Agent hierarchy",
    "Understand ownership and see how work flows through your system.",
  ],
  knowledge: [
    "Shared knowledge",
    "Evidence that survives sessions. Findings, assumptions, and opportunities.",
  ],
  activity: [
    "Activity & decisions",
    "Follow the reasoning, handoffs, and events behind the work.",
  ],
};
function recordButton(row, title) {
  const i = records.push({ row, title }) - 1;
  return `data-record="${i}"`;
}
function goal() {
  return state.goals.find((g) => g.id === goalId) || null;
}
function tasks(g) {
  if (!g) return [];
  const d = g.data,
    runtimes = new Map(d.runtime.map((r) => [r.task_id, r])),
    jobs = new Map(d.research_jobs.map((r) => [r.id, r])),
    implementations = new Map(d.implementations.map((r) => [r.id, r]));
  const linkedJobs = new Set(),
    linkedImpl = new Set();
  const result = d.tasks.map((t) => {
    const runtime = runtimes.get(t.id),
      spec = runtime?.spec || {};
    const research = jobs.get(spec.job_id),
      implementation = implementations.get(spec.implementation_id);
    if (research) linkedJobs.add(research.id);
    if (implementation) linkedImpl.add(implementation.id);
    return {
      ...t,
      _task: true,
      type: research?.stage || t.adapter || "worker",
      title:
        research?.spec?.hypothesis ||
        research?.spec?.question ||
        (implementation ? "Implement authorized changes" : t.instructions),
      status: [
        "uncertain",
        "paused",
        "cancelled",
        "waiting",
        "failed",
      ].includes(runtime?.state)
        ? runtime.state
        : implementation?.status || research?.status || t.status,
      research_runs: research
        ? d.research_runs.filter((r) => r.job_id === research.id)
        : undefined,
      runtime,
      research,
      implementation,
    };
  });
  for (const r of d.research_jobs)
    if (!linkedJobs.has(r.id))
      result.push({
        ...r,
        _task: true,
        research: r,
        research_runs: d.research_runs.filter((run) => run.job_id === r.id),
        type: r.stage,
        title:
          r.spec?.hypothesis ||
          r.spec?.question ||
          `${human(r.stage)} investigation`,
      });
  for (const i of d.implementations)
    if (!linkedImpl.has(i.id))
      result.push({
        ...i,
        _task: true,
        implementation: i,
        type: "implementation",
        title: "Implement authorized changes",
        instructions:
          i.feedback ||
          "Isolated worktree · independent executable verification",
      });
  return result;
}
function events(g) {
  if (!g) return [];
  return [
    ...g.data.activity.map((e) => ({
      ...e,
      title: human(e.kind),
      summary: short(summary(e.detail), 180),
    })),
    ...g.data.decisions.map((e) => ({
      ...e,
      created: e.created_at,
      title: "Manager decision",
      summary: short(summary(e.body), 180),
    })),
    ...g.data.messages.map((e) => ({
      ...e,
      title: human(e.kind),
      summary: short(summary(e.body), 180),
    })),
  ].sort((a, b) => stamp(b.created) - stamp(a.created));
}
function stamp(v) {
  if (!v) return 0;
  if (typeof v === "number") return v * 1000;
  const value = String(v).replace(" ", "T");
  return Date.parse(/(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : value + "Z") || 0;
}
function editable() {
  return Boolean(state.control_enabled && !state.snapshot && !disconnected);
}
function updateHealth() {
  const manager = state.manager || {};
  const selected = goal();
  const owns = manager.goal_id === selected?.id;
  const live = !state.snapshot && !disconnected;
  const health = owns ? manager.state || "unknown" : "offline";
  const signIn = !backend && localSession.login_available && !localSession.authenticated;
  const label = state.snapshot ? "Recorded experiment" : signIn ? "Sign in required" : disconnected ? "Connection lost" : lastGood ? "Live state" : "Connecting";
  const detail = state.snapshot ? "Archived run · controls disabled" : signIn ? "Sign in to read and control your manager" : disconnected ? "Showing the last successful state · writes disabled" : !lastGood ? "Waiting for workspace state" : !selected ? "Create a goal to begin" : owns ? `Manager ${health} · heartbeat ${age(manager.heartbeat_at)}` : "No active manager for this goal · commands wait in the inbox";
  $("#system-status").innerHTML = `<span class="status-light ${live && health === "online" ? "healthy" : ""}"></span><strong>${esc(label)}</strong><span>${esc(detail)}</span>`;
  $("#connection").textContent = label;
  $("#connection").classList.toggle("error", disconnected);
  $("#goal-create-open").disabled = !editable();
}
function checklist(g) {
  if (!g) return "";
  const rows = g.checklist || g.requirements.map((criterion) => {
    const evidence = (g.data.verifications || []).filter((check) => check.requirement === criterion && check.goal_revision === (g.revision || 1));
    return { criterion, state: evidence.some((check) => check.passed) ? "verified" : evidence.length ? "failed" : "pending", mandatory: true, evidence };
  });
  return `<section class="panel completion-panel">${heading("Completion criteria", `<span class="muted">${rows.filter((r) => r.state === "verified").length} / ${rows.length} verified</span>`)}<div class="requirement-list">${rows.map((r) => `<article class="requirement"><div><strong>${esc(r.criterion)}</strong><div class="task-meta">Mandatory${r.original ? " · original criterion" : ""} · goal revision ${g.revision || 1}${state.snapshot ? " · recorded checks" : ""}</div>${r.evidence?.length ? `<details><summary>Inspect ${r.evidence.length} check record${r.evidence.length === 1 ? "" : "s"}</summary><pre>${esc(text(r.evidence))}</pre></details>` : `<small class="muted">No current verification evidence.</small>`}</div>${badge(r.state)}</article>`).join("")}</div></section>`;
}

function empty(title, description) {
  return `<div class="empty"><span class="empty-symbol">◇</span><strong>${esc(title)}</strong>${esc(description)}</div>`;
}
function heading(title, extra = "", subtitle = "") {
  return `<div class="panel-heading"><h2>${title}${subtitle ? `<span class="subtitle">${subtitle}</span>` : ""}</h2>${extra}</div>`;
}
function taskRows(list) {
  return list.length
    ? list
        .map(
          (t) =>
            `<button class="task-row" ${recordButton(t, t.title)}><span class="task-icon ${t.type === "implementation" ? "code" : ""}">${t.type === "implementation" ? "⌥" : "⌕"}</span><div><div class="task-title">${esc(short(t.title, 180))}</div><div class="task-meta"><span>${esc(human(t.type))}</span><span>·</span><span>${esc(t.id.slice(0, 8))}</span>${t.priority != null ? `<span>P${esc(t.priority)}</span>` : ""}</div></div><div class="task-status">${badge(t.status)}<small>${t.runtime ? `${t.runtime.progress_count} progress events` : esc(age(t.updated || t.created))}</small></div></button>`,
        )
        .join("")
    : empty(
        "No matching tasks",
        "Assigned work will appear here as the manager delegates.",
      );
}
function tree(g, list, full = false) {
  return `<section class="panel ${full ? "full-hierarchy" : ""}">${heading("Agent hierarchy", full ? "" : `<button class="text-button" data-view="hierarchy">Explore ↗</button>`)}<div class="mini-tree"><div class="tree-manager"><span class="agent-icon">◈</span><div><div class="tree-title">Engineering manager</div><div class="tree-sub">${g ? "Goal owner · durable session" : "Waiting for a goal"}</div></div></div><div class="tree-children">${
    list.length
      ? list
          .slice(0, full ? 200 : 4)
          .map(
            (t) =>
              `<button class="tree-node" ${recordButton(t, t.title)}><span class="task-icon ${t.type === "implementation" ? "code" : ""}">${t.type === "implementation" ? "⌥" : "⌕"}</span><div><div class="tree-title">${esc(short(t.title, 80))}</div><div class="tree-sub">${esc(human(t.type))} · ${esc(t.id.slice(0, 8))}</div></div>${badge(t.status)}</button>`,
          )
          .join("")
      : empty(
          "No workers assigned",
          "The manager will delegate bounded work from here.",
        )
  }</div></div><div class="tree-note">Manager → assignments. Evaluators and tests run separately.</div></section>`;
}
function eventRows(items) {
  return items.length
    ? items
        .map(
          (e) =>
            `<button class="event" ${recordButton(e, e.title)}><span class="event-dot"></span><div class="event-content"><div class="event-title"><strong>${esc(e.title)}</strong> · ${esc(e.summary)}</div><div class="event-time">${esc(age(e.created))}${e.task_id ? ` · Task ${esc(e.task_id.slice(0, 8))}` : ""}</div></div></button>`,
        )
        .join("")
    : empty(
        "A quiet workspace",
        "Activity and decisions will appear as the manager works.",
      );
}
function overview(g, list) {
  const d = g?.data;
  const latest = d?.decisions[0];
  const pending = list.filter((t) => !done(t.status));
  const budget = d?.research_limits[0];
  return `<div class="overview-grid"><div class="stack"><section class="panel">${heading("Manager overview", g ? badge(g.status) : "")}<div class="manager-summary"><div class="manager-identity"><span class="agent-icon">◈</span><div><h3>Engineering manager</h3><small>Conversational agent · persistent context</small></div>${g ? `<button class="text-button" ${recordButton({ id: `manager:${g.id}`, goal_revision: g.revision || 1, policy: d.policy_versions[0] || null, goal: g.objective, status: g.status }, "Manager session")}>Inspect ↗</button>` : ""}</div><div class="objective">${esc(g?.objective || "Your next engineering goal starts here.")}</div><div class="manager-meta"><span>${g ? "Revision " + (g.revision || 1) : "No active goal"}</span><span>Policy v${d?.policy_versions[0]?.version || "—"}</span><span>${g ? g.requirements.length : 0} acceptance criteria</span></div><div class="decision"><div class="decision-label">⌁ &nbsp; LATEST MANAGER DECISION</div><p>${esc(latest ? short(summary(latest.body), 220) : "No decisions recorded.")}</p></div></div></section>${checklist(g)}<section class="panel">${heading("Ongoing tasks", `<button class="text-button" data-view="tasks">View all tasks ↗</button>`, String(pending.length))}${taskRows(pending.slice(0, 5))}</section><section class="panel">${heading("Research budget")}<div class="progress-panel"><div class="progress-line"><span>Bounded role executions</span><span>${budget ? `${budget.spent} / ${budget.max_runs} used` : "Not configured"}</span></div><div class="segments">${Array.from({ length: Math.min(budget?.max_runs || 12, 36) }, (_, i) => `<span class="${budget && i < budget.spent ? "spent" : ""}"></span>`).join("")}</div><div class="budget-note">${budget ? "Includes discovery, research, and evaluation. Exhaustion pauses work for escalation." : "Budgets are recorded when research is initialized. No estimated usage is shown."}</div></div></section></div><div class="stack">${tree(g, list)}<section class="panel">${heading("Recent activity", `<button class="text-button" data-view="activity">View all ↗</button>`)}${eventRows(events(g).slice(0, 5))}</section><section class="panel">${heading("Verification")}<div class="progress-panel"><div class="progress-line"><span>Executable checks passed</span><span>${d?.verifications.filter((v) => v.passed).length || 0} / ${d?.verifications.length || 0}</span></div><div class="progress-track"><span id="verification-progress"></span></div><div class="budget-note">Checks are bound to candidate revisions. A passing check alone does not establish goal completion.</div></div></section></div></div>`;
}
function render() {
  if (composing) return;
  const focusedId = document.activeElement?.id;
  const focused = focusedId === "manager-text";
  const selection = focused
    ? [
        document.activeElement.selectionStart,
        document.activeElement.selectionEnd,
      ]
    : null;
  const g = goal(),
    list = tasks(g);
  records = [];
  $("#breadcrumb").textContent =
    view === "overview" ? "Overview" : titles[view][0];
  $("#page-title").textContent = titles[view][0];
  $("#page-description").textContent = titles[view][1];
  document
    .querySelectorAll("[data-view]")
    .forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $("#nav-count").textContent = list.length;
  const metrics = [
    [
      "Active tasks",
      list.filter((t) => active(t.status)).length,
      "Currently executing",
      "⌁",
    ],
    [
      "Completed tasks",
      list.filter((t) => done(t.status)).length,
      "Recorded task outcomes",
      "✓",
    ],
    [
      "Needs attention",
      list.filter((t) => blocked(t.status)).length +
        (g?.data.proposals.filter((p) => p.state === "pending").length || 0),
      "Blocked, paused, or awaiting review",
      "◷",
    ],
    [
      "Shared knowledge",
      g?.data.knowledge.length || 0,
      `${g?.data.knowledge.filter((k) => k.classification === "verified").length || 0} verified · others retain classification`,
      "▤",
    ],
  ];
  $("#metrics").innerHTML = metrics
    .map(
      ([label, value, note, icon]) =>
        `<article class="metric"><div class="metric-label">${label}<span class="metric-icon">${icon}</span></div><div class="metric-value">${value}</div><div class="metric-note">${esc(note)}</div></article>`,
    )
    .join("");
  if (view === "overview") $("#content").innerHTML = overview(g, list);
  if (view === "tasks") {
    $("#content").innerHTML =
      `<section class="panel">${heading("All assignments", "", `${list.length} total`)}<div class="task-toolbar"><input class="search" id="task-search" placeholder="Search task, role, or ID…" aria-label="Search tasks" value="${esc(query)}"><select id="task-filter" aria-label="Filter tasks"><option value="">All statuses</option value="active">Active</option><option value="done">Completed</option><option value="blocked">Needs attention</option></select></div><div id="task-list"></div></section>`;
    $("#task-filter").value = filter;
    renderTaskList();
  }
  if (view === "hierarchy") $("#content").innerHTML = tree(g, list, true);
  if (view === "activity")
    $("#content").innerHTML =
      `<section class="panel">${heading("Activity & decision log", "", `${events(g).length} recent records`)}${eventRows(events(g))}</section>`;
  if (view === "knowledge") {
    const items = [
      ...(g?.data.knowledge || []),
      ...(g?.data.opportunities || []).map((o) => ({
        ...o,
        classification: "opportunity",
        body: o.spec,
      })),
    ];
    $("#content").innerHTML = items.length
      ? `<div class="knowledge-grid">${items.map((k) => `<button class="knowledge-card" ${recordButton(k, k.title)}>${badge(k.classification)}<h3>${esc(k.title)}</h3><p>${esc(short(k.body, 240))}</p><div class="card-bottom">${esc(age(k.created))} · ${k.citations?.length || 0} citations${k.score != null ? ` · Score ${esc(k.score)}` : ""}</div></button>`).join("")}</div>`
      : `<section class="panel">${empty("Knowledge grows with each investigation", "Findings and opportunities appear here with their recorded classifications. Source freshness must be checked before reuse.")}</section>`;
  }
  if (view === "chat") renderChat(g);
  const canWrite = editable();
  $(".read-badge").textContent = canWrite
    ? "Authenticated control"
    : "Read-only";
  $("#mode-title").textContent = canWrite
    ? "Control connected"
    : "Observe mode";
  $("#mode-description").textContent = canWrite
    ? "Authenticated manager inbox"
    : "This workspace is read-only";
  $("#workspace-mode").textContent = canWrite
    ? "Authenticated control"
    : "Read-only workspace";
  $("#connect-open").textContent = backend ? "Connection settings" : localSession.authenticated ? "Account" : localSession.login_available ? "Sign in" : "Connect manager";
  if (["manager-text", "manager-command", "manager-task", "task-filter"].includes(focusedId)) {
    const target = document.getElementById(focusedId);
    target?.focus();
    if (selection && target) target.setSelectionRange(...selection);
  }
  updateHealth();
  const progress = $("#verification-progress");
  if (progress) {
    const checks = g?.data.verifications || [];
    const pct = checks.length
      ? checks.filter((c) => c.passed).length / checks.length
      : 0;
    progress.style.width = `${pct * 100}%`;
  }
}
function delivery(message, messages) {
  const reply = messages.find((m) => m.id === "reply:" + message.id);
  if (reply) return { state: reply.body?.error ? "error" : "answered", note: reply.body?.error || "Manager response recorded", step: 3 };
  const states = {
    pending: ["queued", "Durably queued · waiting for the manager", 0],
    sending: ["processing", "Manager is processing this request", 1],
    acknowledged: ["acknowledged", "Receipt acknowledged · no response recorded yet", 2],
    uncertain: ["uncertain", "Delivery is uncertain · reconcile before retrying", 1],
    sent_no_ack: ["uncertain", "Sent without acknowledgment · outcome is unknown", 1],
    failed: ["error", "Request failed · inspect details", 1],
  };
  const [status, note, step] = states[message.state] || [message.state || "pending", "Inspect the persisted receipt", 0];
  return { state: status, note, step };
}
function renderChat(g) {
  const enabled = Boolean(g && editable());
  const messages = (g?.data.messages || []).filter((m) => m.kind === "control" || m.direction === "manager_to_user").slice().reverse();
  const proposals = (g?.data.proposals || []).filter((p) => p.state === "pending");
  const commands = { chat: "Message", steer: "Steer task", pause: "Pause task", cancel: "Cancel task", continue: "Run decisions", policy: "Propose policy", requirements: "Propose requirements", reconcile: "Reconcile task" };
  const hints = {
    chat: "Ask about progress, priorities, or the current approach.",
    steer: "Instructions are delivered to the selected worker using its supported runtime.",
    pause: "Pause the selected task; retained work stays available.",
    cancel: "Cancel the selected task; retained work stays available.",
    continue: "Choose 1–50 manager decisions. Workers remain bounded by their own budgets.",
    policy: "Propose the complete replacement policy. Review and accept before it becomes permanent.",
    requirements: 'Use JSON: {"constraint":"New constraint","add_requirements":["New criterion"]}. Original criteria remain mandatory.',
    reconcile: "Check the runtime before retrying work with an uncertain outcome.",
  };
  $("#content").innerHTML = `<section class="panel chat-panel">${heading("Conversation", enabled ? '<span class="badge green">Control enabled</span>' : '<span class="badge">Read-only</span>')}
  <div class="conversation">${messages.length ? messages.map((m) => {
    const receipt = m.kind === "control" ? delivery(m, messages) : null;
    return `<article class="chat-message"><div class="chat-author">${m.kind === "control" ? "You · " + esc(human(m.body.command)) : "Manager"}<span>${esc(age(m.created))}</span></div><p>${esc(m.kind === "control" ? m.body.text : m.body.response || summary(m.body))}</p>${receipt ? `<div class="delivery-state" data-delivery="${esc(receipt.state)}">${badge(receipt.state)}<span>${esc(receipt.note)}</span></div><ol class="delivery-timeline" aria-label="Message delivery">${["Queued", "Processing", "Acknowledged", "Answered"].map((label, i) => `<li class="delivery-step ${i < receipt.step ? "complete" : i === receipt.step ? "current" : ""}">${label}</li>`).join("")}</ol>` : ""}<button class="text-button" ${recordButton(m, "Message details")}>Inspect record ↗</button></article>`;
  }).join("") : empty("Start a conversation", enabled ? "Messages enter the durable inbox. The manager replies when running." : "Connect to an authenticated backend to send messages.")}</div>
  ${proposals.map((p) => `<article class="proposal"><div><strong>Proposed ${esc(p.kind)} change</strong><p>Review the full change, affected tasks and evidence before accepting.</p></div><button class="connect-button" data-review="${esc(p.id)}" ${enabled ? "" : "disabled"}>Review change</button></article>`).join("")}
  <form id="manager-form" class="composer"><div class="composer-options"><label>Action<select id="manager-command" ${enabled ? "" : "disabled"}>${Object.entries(commands).map(([value, label]) => `<option value="${value}" ${compose.command === value ? "selected" : ""}>${label}</option>`).join("")}</select></label><label>Task<select id="manager-task" ${enabled ? "" : "disabled"}><option value="">Select for task actions</option>${(g?.data.tasks || []).map((t) => `<option value="${esc(t.id)}" ${compose.task === t.id ? "selected" : ""}>${esc(t.id.slice(0, 8))} · ${esc(short(t.instructions, 70))}</option>`).join("")}</select></label></div>
  <label class="composer-label" for="manager-text">Message or instruction</label><textarea id="manager-text" aria-describedby="composer-hint" rows="4" maxlength="12000" placeholder="Ask for progress, steer work, or propose a change…" ${enabled ? "" : "disabled"}>${esc(compose.text)}</textarea>
  <div class="composer-bottom"><span id="composer-hint">${esc(hints[compose.command])}</span><button class="connect-button" type="submit" ${enabled && !sending ? "" : "disabled"}>${sending ? "Sending…" : compose.pending ? "Retry delivery" : "Send"}</button></div><p id="send-note" role="status">${esc(compose.note)}</p></form></section>`;
}

async function sendCommand(command, text) {
  const g = goal();
  if (!g || !editable() || sending) return false;
  const body = {
    goal_id: g.id,
    command,
    text,
    goal_revision: g.revision || 1,
    policy_revision: g.data.policy_versions[0]?.version || 1,
  };
  const signature = JSON.stringify({ goal_id: g.id, command, text });
  if (compose.pending && compose.pending.signature !== signature) {
    compose.note =
      "Previous delivery is unconfirmed. Retry the unchanged request first and inspect its receipt before sending a different command.";
    render();
    return;
  }
  if (!compose.pending)
    compose.pending = {
      signature,
      payload: { ...body, request_id: crypto.randomUUID() },
    };
  savePending();
  sending = true;
  compose.note = "Sending…";
  render();
  let queued = false;
  try {
    const response = await fetch(endpoint("api/commands"), {
      method: "POST",
      headers: { ...auth(), "Content-Type": "application/json" },
      body: JSON.stringify(compose.pending.payload),
      signal: AbortSignal.timeout(10000),
      redirect: "error",
    });
    const result = await response.json();
    if (!response.ok) {
      if (response.status < 500) compose.pending = null;
      throw Error(result.error || "Command rejected");
    }
    compose.note =
      "Queued. The running manager will acknowledge and reply here.";
    compose.pending = null;
    if (command !== "accept") compose.text = "";
    queued = true;
    await refresh();
  } catch (error) {
    compose.note = compose.pending
      ? "Delivery unconfirmed. Retry unchanged to reuse the request ID; inspect messages before changing the request."
      : error.message;
  }
  savePending();
  sending = false;
  render();
  return queued;
}
document.addEventListener("submit", async (event) => {
  if (event.target.id !== "manager-form") return;
  event.preventDefault();
  let text = compose.text.trim();
  if (["pause", "cancel", "reconcile", "steer"].includes(compose.command)) {
    if (!compose.task) {
      compose.note = "Select a task first.";
      render();
      return;
    }
    text = compose.task + (compose.command === "steer" ? " " + text : "");
  }
  if (
    compose.command === "cancel" &&
    !confirm("Cancel the selected task? Retained work will be preserved.")
  )
    return;
  await sendCommand(
    compose.command,
    text || (compose.command === "continue" ? "5" : ""),
  );
});
document.addEventListener("input", (e) => {
  if (e.target.id === "manager-text") compose.text = e.target.value;
});
document.addEventListener("compositionstart", () => { composing = true; });
document.addEventListener("compositionend", () => { composing = false; });
document.addEventListener("change", (e) => {
  if (e.target.id === "manager-command") {
    compose.command = e.target.value;
    render();
  }
  if (e.target.id === "manager-task") compose.task = e.target.value;
});
function openProposal(id) {
  const proposal = goal()?.data.proposals.find((p) => p.id === id && p.state === "pending");
  if (!proposal) return;
  proposalId = id;
  const review = proposal.review;
  $("#proposal-title").textContent = `Review ${proposal.kind} change`;
  $("#proposal-confirm").checked = false;
  $("#proposal-confirm").disabled = !review || review.stale;
  $("#proposal-apply").disabled = true;
  $("#proposal-error").textContent = !review ? "Refresh live state to load the complete impact review." : review.stale ? "This proposal is based on an older revision. Ask the manager for a new proposal." : "";
  const requirements = proposal.kind === "requirements";
  const oldText = requirements ? (review?.old_requirements || goal().requirements).join("\n") : review?.old_instructions;
  const newText = requirements ? (review?.new_requirements || []).join("\n") : review?.new_instructions;
  $("#proposal-review").innerHTML = review ? `<p>Based on goal revision ${esc(review.base_goal_revision)} and policy v${esc(review.base_policy_revision)}. Earlier versions remain available.</p><div class="instruction-diff"><section class="diff-before"><h3>Current ${requirements ? "criteria" : "policy"}</h3><pre>${esc(text(oldText))}</pre></section><section class="diff-after"><h3>Proposed ${requirements ? "criteria" : "policy"}</h3><pre>${esc(text(newText))}</pre></section></div>${review.constraint ? `<h3>New task constraint</h3><p>${esc(review.constraint)}</p>` : ""}<h3>Impact</h3><div class="impact-list">${[
    ["Affected active tasks", review.affected_task_ids],
    ["Authorizations requiring renewal", review.invalidated_approvals],
    ["Evidence records invalidated", review.affected_evidence_ids],
    ["Verification records becoming stale", review.stale_verification_ids || []],
  ].map(([label, ids]) => `<details><summary>${esc(label)} · ${ids.length}</summary>${listItems(ids)}</details>`).join("")}</div><p class="muted">Original mandatory criteria remain. Unaffected research is preserved. Acceptance is queued and revision checked by the manager.</p>` : `<pre>${esc(text(proposal.body))}</pre>`;
  $("#proposal-dialog").showModal();
}
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-review]");
  if (button) openProposal(button.dataset.review);
});
$("#proposal-close").addEventListener("click", () => $("#proposal-dialog").close());
$("#proposal-confirm").addEventListener("change", (event) => {
  $("#proposal-apply").disabled = !event.target.checked || !editable();
});
$("#proposal-apply").addEventListener("click", async () => {
  if (!$("#proposal-confirm").checked || !proposalId || !editable() || goal()?.data.proposals.find((p) => p.id === proposalId)?.review?.stale) return;
  $("#proposal-apply").disabled = true;
  if (await sendCommand("accept", proposalId)) $("#proposal-dialog").close();
  else $("#proposal-error").textContent = compose.note;
});
$("#goal-create-open").addEventListener("click", () => {
  if (!editable()) return;
  $("#goal-error").textContent = "";
  $("#goal-dialog").showModal();
});
$("#goal-close").addEventListener("click", () => $("#goal-dialog").close());
$("#goal-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!editable()) return;
  const body = {
    objective: $("#goal-objective").value.trim(),
    requirements: $("#goal-requirements").value.split("\n").map((v) => v.trim()).filter(Boolean),
    workspace: $("#goal-workspace").value.trim(),
    require_implementation: $("#goal-implementation").checked,
  };
  const signature = JSON.stringify(body);
  if (!body.requirements.length) {
    $("#goal-error").textContent = "Add at least one mandatory criterion.";
    return;
  }
  if (creating && signature !== creating.signature) {
    $("#goal-error").textContent = "Previous creation is unconfirmed. Retry unchanged to retrieve its original receipt.";
    return;
  }
  if (!creating) creating = { signature, payload: { ...body, request_id: crypto.randomUUID() } };
  $("#goal-submit").disabled = true;
  try {
    const response = await fetch(endpoint("api/goals"), {
      method: "POST", headers: { ...auth(), "Content-Type": "application/json" },
      body: JSON.stringify(creating.payload), redirect: "error", signal: AbortSignal.timeout(10000),
    });
    const result = await response.json();
    if (!response.ok) {
      if (response.status < 500) creating = null;
      throw Error(result.error || "Goal rejected");
    }
    creating = null;
    goalId = result.goal_id;
    view = "overview";
    $("#goal-dialog").close();
    $("#goal-form").reset();
    await refresh();
    render();
  } catch (error) {
    $("#goal-error").textContent = creating ? "Creation unconfirmed. Retry unchanged to reuse the same request ID." : error.message;
  } finally {
    $("#goal-submit").disabled = false;
  }
});

function openConnect() {
  $("#backend-url").value =
    backend?.url || (document.body.dataset.static ? "" : location.origin + "/");
  $("#backend-token").value = "";
  $("#connect-error").textContent = "";
  $("#connect-dialog").showModal();
}
$("#connect-open").addEventListener("click", () => {
  if (!backend && localSession.login_available) openLogin();
  else openConnect();
});
$("#connect-close").addEventListener("click", () =>
  $("#connect-dialog").close(),
);
$("#disconnect").addEventListener("click", () => {
  backend = null;
  disconnected = false;
  state = { goals: [] };
  restorePending();
  $("#backend-token").value = "";
  $("#connect-dialog").close();
  render();
  refresh();
});
$("#connect-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const url = new URL($("#backend-url").value);
    if (
      url.username ||
      url.password ||
      url.search ||
      url.hash ||
      (url.protocol !== "https:" &&
        !(
          url.protocol === "http:" &&
          ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
        ))
    )
      throw Error(
        "Use HTTPS, or HTTP on localhost. Do not put credentials in the URL.",
      );
    url.pathname = url.pathname.replace(/\/?$/, "/");
    const token = $("#backend-token").value;
    const response = await fetch(new URL("api/state", url), {
      headers: { Authorization: "Bearer " + token },
      signal: AbortSignal.timeout(10000),
      redirect: "error",
    });
    if (!response.ok)
      throw Error(
        "Connection rejected. Check the URL, token and allowed origin.",
      );
    const result = await response.json();
    if (!result.control_enabled)
      throw Error(
        "This backend has control disabled. Start it with --allow-control.",
      );
    backend = { url: url.href, token };
    $("#backend-token").value = "";
    $("#connect-dialog").close();
    goalId = null;
    compose.note = "";
    restorePending();
    view = "chat";
    await refresh();
    render();
  } catch (error) {
    $("#connect-error").textContent = error.message;
  }
});

function openLogin() {
  $("#login-title").textContent = localSession.authenticated ? "Workspace account" : "Sign in to your workspace";
  $("#session-description").textContent = localSession.authenticated ? `Signed in as ${localSession.username}.` : "Use your manager account. Your session is stored in a protected cookie.";
  $("#login-fields").hidden = localSession.authenticated;
  $("#login-submit").hidden = localSession.authenticated;
  $("#logout").hidden = !localSession.authenticated;
  $("#login-password").value = "";
  $("#login-error").textContent = "";
  $("#login-dialog").showModal();
}
$("#login-close").addEventListener("click", () => $("#login-dialog").close());
$("#login-token").addEventListener("click", () => { $("#login-dialog").close(); openConnect(); });
$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#login-submit").disabled = true;
  try {
    const response = await fetch("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin", redirect: "error",
      body: JSON.stringify({ username: $("#login-username").value, password: $("#login-password").value }),
      signal: AbortSignal.timeout(10000),
    });
    const result = await response.json();
    if (!response.ok) throw Error(result.error || "Sign in failed");
    localSession = { ...result, authenticated: true, login_available: true };
    $("#login-password").value = "";
    $("#login-dialog").close();
    await refresh();
    render();
  } catch (error) {
    $("#login-error").textContent = error.message;
  } finally { $("#login-submit").disabled = false; }
});
$("#logout").addEventListener("click", async () => {
  try {
    const response = await fetch("/api/logout", { method: "POST", headers: { ...auth(), "Content-Type": "application/json" }, body: "{}", credentials: "same-origin", redirect: "error" });
    if (!response.ok) throw Error("Could not sign out. Try again.");
    localSession = { login_available: true, authenticated: false };
    state = { goals: [] };
    compose.pending = null;
    savePending();
    compose.text = "";
    goalId = null;
    $("#login-dialog").close();
    render();
    await refresh();
  } catch (error) { $("#login-error").textContent = error.message; }
});
async function initialize() {
  restorePending();
  if (!document.body.dataset.static) {
    try {
      const response = await fetch("/api/session", { credentials: "same-origin", redirect: "error", signal: AbortSignal.timeout(7000) });
      if (response.ok) localSession = await response.json();
    } catch (_) { /* Read-only servers and static exports need no login endpoint. */ }
  }
  render();
  await refresh();
}

function renderTaskList() {
  const list = tasks(goal()).filter(
    (t) =>
      (!query ||
        `${t.title} ${t.type} ${t.id}`
          .toLowerCase()
          .includes(query.toLowerCase())) &&
      (!filter ||
        (filter === "active"
          ? active(t.status)
          : filter === "done"
            ? done(t.status)
            : blocked(t.status))),
  );
  $("#task-list").innerHTML = taskRows(list);
}
function listItems(items, fallback = "None recorded.") {
  return items?.length ? `<ul>${items.map((item) => `<li>${esc(typeof item === "string" ? item : summary(item))}</li>`).join("")}</ul>` : `<p class="muted">${esc(fallback)}</p>`;
}
function detailSection(title, body) {
  return `<section class="detail-section"><h3>${title}</h3>${body}</section>`;
}
function taskDetail(task) {
  const g = goal(), d = g.data;
  const research = task.research, implementation = task.implementation;
  const plan = research?.spec || {};
  const runs = task.research_runs || [];
  const latest = [...runs].sort((a, b) => stamp(b.created) - stamp(a.created))[0];
  const report = latest?.report || {};
  const evaluation = latest?.evaluation || {};
  const authorization = (d.implementation_authorizations || []).find((a) => a.id === implementation?.authorization);
  const spec = authorization?.spec || {};
  const checks = (d.verifications || []).filter((v) => v.implementation_id === implementation?.id);
  const runIds = new Set(runs.map((r) => r.id));
  const sourceIds = new Set((d.research_access || []).filter((a) => runIds.has(a.run_id)).map((a) => a.source_id));
  const sources = (d.research_sources || []).filter((e) => sourceIds.has(e.id));
  const messages = (d.messages || []).filter((m) => m.task_id === task.id);
  const result = task.result || {};
  const handoff = result.handoff || result;
  const budget = plan.budget;
  return `<div class="task-detail"><div class="task-detail-status">${badge(task.status)}<code>${esc(task.id)}</code></div>
  ${detailSection("Objective & scope", `<p>${esc(plan.question || spec.objective || task.instructions || task.title)}</p>${listItems(plan.scope || spec.allowed_paths, "No explicit source scope recorded.")}`)}
  ${plan.hypothesis ? detailSection("Hypothesis", `<p>${esc(plan.hypothesis)}</p><strong>Falsified if</strong><p>${esc(plan.falsification)}</p>`) : ""}
  ${detailSection("Budget & progress", `<dl class="detail-grid">${Object.entries({
    ...(budget ? { "Tool calls per attempt": budget.tool_calls, "Seconds per role": budget.seconds, "Attempts": `${research.attempts || 0} / ${budget.attempts}` } : {}),
    ...(implementation ? { "Implementation attempts": `${implementation.attempts || 0}${spec.max_attempts ? " / " + spec.max_attempts : ""}`, "Time limit": spec.seconds ? `${spec.seconds}s` : "Not recorded" } : {}),
    ...(task.runtime ? { "Meaningful progress": `${task.runtime.progress_count || 0} events`, "Last progress": age(task.runtime.last_progress), "Deadline": new Date(stamp(task.runtime.deadline)).toLocaleString(), "Goal revision": task.runtime.goal_revision, "Policy revision": task.runtime.policy_revision } : {}),
  }).map(([key, value]) => `<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join("") || '<div><dd>No execution budget recorded.</dd></div>'}</dl>`)}
  ${detailSection("Findings", report.findings?.length ? report.findings.map((finding) => `<article class="finding">${badge(finding.classification)}<p>${esc(finding.statement)}</p><small>${finding.evidence_ids?.length || 0} cited sources</small></article>`).join("") : report.opportunities?.length ? listItems(report.opportunities.map((opportunity) => opportunity.title)) : `<p class="muted">No findings returned yet.</p>`)}
  ${detailSection("Evidence & contradictions", `<div class="evidence-list">${sources.map((source) => `<details><summary>${esc(source.locator)} · ${esc(source.revision?.slice(0, 10))}</summary><p class="muted">Observed ${esc(age(source.observed))} · source ${esc(source.id)}</p><pre>${esc(source.excerpt)}</pre></details>`).join("") || '<p class="muted">No captured excerpts available in this view.</p>'}</div><h4>Supporting evidence</h4>${listItems(report.supporting_evidence)}<h4>Contradictory evidence</h4>${listItems(report.contradictory_evidence, "No contradictory evidence recorded. Absence is not proof.")}`)}
  ${evaluation.verdict ? detailSection("Independent evaluation", `${badge(evaluation.verdict)}<p>${esc(evaluation.reason)}</p><p>${esc(evaluation.feedback)}</p>${listItems(evaluation.evidence_ids)}`) : ""}
  ${detailSection("Blockers & remaining uncertainty", `${listItems(report.uncertainties || report.gaps || handoff.unresolved_concerns)}${latest?.feedback || implementation?.feedback ? `<p>${esc(text(latest?.feedback || implementation.feedback))}</p>` : ""}${report.recommendation ? `<h4>Recommended next action</h4><p>${esc(report.recommendation)}</p>` : ""}`)}
  ${implementation ? detailSection("Candidate & executable checks", `<p>Candidate <code>${esc(implementation.candidate || "Not returned")}</code></p>${checks.length ? checks.map((check) => `<article class="candidate-check">${badge(check.candidate !== implementation.candidate || check.goal_revision !== (g.revision || 1) || authorization?.revoked ? "stale" : check.passed ? "verified" : "failed")}<strong>${esc(check.requirement)}</strong><small>Candidate ${esc(check.candidate)} · goal revision ${check.goal_revision} · exit ${esc(check.exit_code)}</small><details><summary>Command and evidence</summary><pre>${esc(text({ argv: check.argv, artifact: check.artifact, digest: check.digest }))}</pre></details></article>`).join("") : '<p class="muted">No independent checks recorded.</p>'}`) : ""}
  ${messages.length ? detailSection("Worker communication", messages.map((m) => `<article class="worker-message"><strong>${esc(human(m.direction))} · ${esc(human(m.kind))}</strong>${badge(m.state)}<p>${esc(summary(m.body))}</p></article>`).join("")) : ""}
  </div>`;
}
function openRecord(index, trigger) {
  const rec = records[index];
  if (!rec) return;
  drawerTrigger = trigger;
  $("#drawer-title").textContent = short(rec.title, 180);
  const raw = `<dl>${Object.entries(rec.row).filter(([key, value]) => value != null && !key.startsWith("_")).map(([key, value]) => `<div class="detail-field"><dt>${esc(human(key))}</dt><dd>${typeof value === "object" ? `<pre>${esc(text(value))}</pre>` : esc(value)}</dd></div>`).join("")}</dl>`;
  $("#drawer-body").innerHTML = rec.row._task ? taskDetail(rec.row) + `<details class="raw-record"><summary>Raw persisted record</summary>${raw}</details>` : raw;
  $("#drawer").hidden = false;
  $("#drawer-backdrop").hidden = false;
  $(".close-drawer").focus();
}

function closeDrawer() {
  $("#drawer").hidden = true;
  $("#drawer-backdrop").hidden = true;
  if (drawerTrigger?.isConnected) drawerTrigger.focus();
  else $("#refresh").focus();
}
document.addEventListener("click", (e) => {
  const v = e.target.closest("[data-view]");
  if (v) {
    view = v.dataset.view;
    render();
  }
  const r = e.target.closest("[data-record]");
  if (r) openRecord(Number(r.dataset.record), r);
  if (e.target.closest(".close-drawer") || e.target.id === "drawer-backdrop")
    closeDrawer();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#drawer").hidden) closeDrawer();
  if (e.key === "Tab" && !$("#drawer").hidden) {
    const focusable = [...$("#drawer").querySelectorAll('button, a[href], summary, input, select, textarea, [tabindex="0"]')].filter((item) => !item.disabled);
    const first = focusable[0], last = focusable.at(-1);
    if (e.shiftKey && (document.activeElement === first || !$("#drawer").contains(document.activeElement))) {
      e.preventDefault(); last?.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault(); first?.focus();
    }
  }
});
document.addEventListener("input", (e) => {
  if (e.target.id === "task-search") {
    query = e.target.value;
    renderTaskList();
  }
});
document.addEventListener("change", (e) => {
  if (e.target.id === "goal-select") {
    goalId = e.target.value;
    query = "";
    filter = "";
    closeDrawer();
    render();
  }
  if (e.target.id === "task-filter") {
    filter = e.target.value;
    renderTaskList();
  }
});
async function refresh() {
  if (fetching) return;
  fetching = true;
  try {
    const response = await fetch(
      backend
        ? endpoint("api/state")
        : document.body.dataset.state || "/api/state",
      {
        headers: auth(),
        redirect: "error",
        cache: "no-store",
        signal: AbortSignal.timeout(7000),
      },
    );
    if (!response.ok) {
      if (response.status === 401 && !backend && localSession.login_available) {
        localSession.authenticated = false;
        throw Error("Sign in to view and control this workspace.");
      }
      throw Error("State could not be read. Keeping the last successful snapshot.");
    }
    const next = await response.json();
    const changed = JSON.stringify(next.goals) !== JSON.stringify(state.goals) || next.control_enabled !== state.control_enabled || disconnected;
    disconnected = false;
    state = next;
    if (compose.pending?.payload.command === "accept" && state.goals.some((g) => g.data.messages.some((m) => m.id === `web:${compose.pending.payload.goal_id}:${compose.pending.payload.request_id}`))) {
      compose.pending = null;
      savePending();
      compose.note = "Earlier acceptance was received. Its result appears in the conversation.";
    }
    lastGood = new Date();
    if (!state.goals.some((g) => g.id === goalId))
      goalId = state.goals[0]?.id || null;
    const selector = $("#goal-select");
    if (changed || !selector.dataset.ready) {
      selector.innerHTML = state.goals.length
        ? state.goals
            .map(
              (g) =>
                `<option value="${esc(g.id)}">${esc(short(g.objective, 85))}</option>`,
            )
            .join("")
        : "<option>No goals yet</option>";
      selector.value = goalId || "";
      selector.dataset.ready = "1";
      if (document.activeElement?.id !== "task-search") render();
    }
    updateHealth();
    $("#updated").textContent = state.snapshot
      ? `Recorded ${new Date(state.recorded_at).toLocaleString()} · not live`
      : `Updated ${lastGood.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
    $("#notice").hidden = Boolean(
      !state.snapshot && state.available && state.goals.length,
    );
    $("#notice").textContent = state.snapshot
      ? state.reason
      : !state.available
        ? state.reason
        : state.goals.length
          ? ""
          : editable() ? "No goals yet. Create your first engineering goal." : "No goals recorded yet. Connect to create a goal, or use the CLI.";
    if (state.snapshot) {
      const link = document.createElement("a");
      link.href = "./docs/experiments.html";
      link.textContent = " View results and limitations ↗";
      $("#notice").append(link);
    }
    $("#source-note").textContent = state.snapshot
      ? "Recorded experiment · read-only"
      : `SQLite · Latest ${state.limit || 200} records per category`;
  } catch (err) {
    disconnected = true;
    render();
    updateHealth();
    $("#notice").hidden = false;
    $("#notice").textContent =
      `${err.message} ${lastGood ? "Last success: " + lastGood.toLocaleTimeString() + "." : ""} Retrying every 5 seconds.`;
  } finally {
    fetching = false;
  }
}
$("#refresh").addEventListener("click", refresh);
render();
initialize();
if (document.body.dataset.static) $(".auto-refresh").hidden = true;
setInterval(() => {
  if (!document.hidden && (!document.body.dataset.static || backend)) refresh();
}, 5000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
