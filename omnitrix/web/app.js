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
  const t =
    typeof v === "number"
      ? v * 1000
      : Date.parse(String(v).replace(" ", "T") + "Z");
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
let lastGood = null;
const titles = {
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
      type: research?.stage || t.adapter || "worker",
      title:
        research?.spec?.hypothesis ||
        research?.spec?.question ||
        (implementation ? "Implement authorized changes" : t.instructions),
      status: ["uncertain", "paused", "cancelled", "waiting", "failed"].includes(runtime?.state)
        ? runtime.state
        : implementation?.status || research?.status || t.status,
      research_runs: research ? d.research_runs.filter(r => r.job_id === research.id) : undefined,
      runtime,
      research,
      implementation,
    };
  });
  for (const r of d.research_jobs)
    if (!linkedJobs.has(r.id))
      result.push({
        ...r,
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
  return typeof v === "number"
    ? v * 1000
    : Date.parse(String(v).replace(" ", "T") + "Z") || 0;
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
  return `<div class="overview-grid"><div class="stack"><section class="panel">${heading("Manager overview", g ? badge(g.status) : "")}<div class="manager-summary"><div class="manager-identity"><span class="agent-icon">◈</span><div><h3>Engineering manager</h3><small>Conversational agent · persistent context</small></div>${g ? `<button class="text-button" ${recordButton({ id: `manager:${g.id}`, goal_revision: g.revision || 1, policy: d.policy_versions[0] || null, goal: g.objective, status: g.status }, "Manager session")}>Inspect ↗</button>` : ""}</div><div class="objective">${esc(g?.objective || "Your next engineering goal starts here.")}</div><div class="manager-meta"><span>${g ? "Revision " + (g.revision || 1) : "No active goal"}</span><span>Policy v${d?.policy_versions[0]?.version || "—"}</span><span>${g ? g.requirements.length : 0} acceptance criteria</span></div><div class="decision"><div class="decision-label">⌁ &nbsp; LATEST MANAGER DECISION</div><p>${esc(latest ? short(summary(latest.body), 220) : "No decisions recorded.")}</p></div></div>${
    g
      ? `<div class="criteria">${g.requirements
          .slice(0, 4)
          .map((c) => `<div class="criterion">${esc(c)}</div>`)
          .join(
            "",
          )}${g.requirements.length > 4 ? `<button class="text-button" ${recordButton({ requirements: g.requirements }, "Acceptance criteria")}>View all ${g.requirements.length} criteria ↗</button>` : ""}</div>`
      : ""
  }</section><section class="panel">${heading("Ongoing tasks", `<button class="text-button" data-view="tasks">View all tasks ↗</button>`, String(pending.length))}${taskRows(pending.slice(0, 5))}</section><section class="panel">${heading("Research budget")}<div class="progress-panel"><div class="progress-line"><span>Bounded role executions</span><span>${budget ? `${budget.spent} / ${budget.max_runs} used` : "Not configured"}</span></div><div class="segments">${Array.from({ length: Math.min(budget?.max_runs || 12, 36) }, (_, i) => `<span class="${budget && i < budget.spent ? "spent" : ""}"></span>`).join("")}</div><div class="budget-note">${budget ? "Includes discovery, research, and evaluation. Exhaustion pauses work for escalation." : "Budgets are recorded when research is initialized. No estimated usage is shown."}</div></div></section></div><div class="stack">${tree(g, list)}<section class="panel">${heading("Recent activity", `<button class="text-button" data-view="activity">View all ↗</button>`)}${eventRows(events(g).slice(0, 5))}</section><section class="panel">${heading("Verification")}<div class="progress-panel"><div class="progress-line"><span>Executable checks passed</span><span>${d?.verifications.filter((v) => v.passed).length || 0} / ${d?.verifications.length || 0}</span></div><div class="progress-track"><span id="verification-progress"></span></div><div class="budget-note">Checks are bound to candidate revisions. A passing check alone does not establish goal completion.</div></div></section></div></div>`;
}
function render() {
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
  const progress = $("#verification-progress");
  if (progress) {
    const checks = g?.data.verifications || [];
    const pct = checks.length
      ? checks.filter((c) => c.passed).length / checks.length
      : 0;
    progress.style.width = `${pct * 100}%`;
  }
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
function openRecord(index, trigger) {
  const rec = records[index];
  if (!rec) return;
  drawerTrigger = trigger;
  $("#drawer-title").textContent = short(rec.title, 180);
  $("#drawer-body").innerHTML = `<dl>${Object.entries(rec.row)
    .filter(([, v]) => v != null)
    .map(
      ([k, v]) =>
        `<div class="detail-field"><dt>${esc(human(k))}</dt><dd>${typeof v === "object" ? `<pre>${esc(text(v))}</pre>` : esc(v)}</dd></div>`,
    )
    .join("")}</dl>`;
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
  if (e.key === "Escape") closeDrawer();
  if (e.key === "Tab" && !$("#drawer").hidden) {
    e.preventDefault();
    $(".close-drawer").focus();
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
    const response = await fetch(document.body.dataset.state || "/api/state", {
      cache: "no-store",
      signal: AbortSignal.timeout(7000),
    });
    if (!response.ok)
      throw Error(
        "State could not be read. Keeping the last successful snapshot.",
      );
    const next = await response.json();
    const changed = JSON.stringify(next.goals) !== JSON.stringify(state.goals);
    state = next;
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
    $("#connection").textContent = state.demo ? "Static demo" : "Connected";
    $("#connection").classList.remove("error");
    $("#updated").textContent =
      state.demo ? "Sample data · no live workers" : `Updated ${lastGood.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
    $("#notice").hidden = Boolean(!state.demo && state.available && state.goals.length);
    $("#notice").textContent = state.demo ? state.reason : !state.available
      ? state.reason
      : state.goals.length
        ? ""
        : "No goals recorded yet. Create a goal using the CLI; this dashboard will update automatically.";
    $("#source-note").textContent =
      state.demo ? "Static sample · no live connection" : `SQLite · Latest ${state.limit || 200} records per category`;
  } catch (err) {
    $("#connection").textContent = "Disconnected";
    $("#connection").classList.add("error");
    $("#notice").hidden = false;
    $("#notice").textContent =
      `${err.message} ${lastGood ? "Last success: " + lastGood.toLocaleTimeString() + "." : ""} Retrying every 5 seconds.`;
  } finally {
    fetching = false;
  }
}
$("#refresh").addEventListener("click", refresh);
render();
refresh();
if (document.body.dataset.static) $(".auto-refresh").hidden = true;
else setInterval(() => {
  if (!document.hidden) refresh();
}, 5000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
