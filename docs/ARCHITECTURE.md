# Architecture

Omnitrix is a Python OpenAI Agents SDK manager, SQLite state, Markdown instructions,
and real Codex workers. There is no workflow framework, broker, web server, or
scheduler service. The original serial `Manager`/`Execution` API remains available;
the CLI now runs the nonblocking `Supervisor` for interactive and unattended work.

## Manager identity and control

`AgentBrain` is a conversational `Agent`, run through `Runner.run`. It owns the
objective and current mandatory criteria, chooses priorities, calls durable-history
and knowledge retrieval tools, and emits executable decisions. It repeatedly asks
which high-value uncertainty to resolve next; Python enforces scopes, authorizations,
budgets, revision fences and executable checks, rather than supplying an LLM task
classification pipeline. Discovery, research, and verification do not own the goal.

`SQLiteSession('manager:<goal-id>')` preserves actual manager conversation across
restarts in a separate sessions database. Worker tool histories stay out of that
session. The latest 12 SDK session items and recent task summaries bound the context;
original criteria, policy, durable decisions and shared knowledge remain in SQLite
and can be recalled after older conversation falls outside the context window.
Markdown in `instructions/` defines each role. `policy_versions` stores approved
behavior separately from goal/task instructions and preserves every earlier version.

The supervisor holds a process-level database ownership lock. Its async actor lock
serializes manager calls, policy updates and decisions, but **not worker execution**.
Background asyncio tasks run workers while terminal input, status reads, native
worker messages and progress events remain available. SQLite is also the cross-process
control inbox: `send` queues commands to an existing manager; it never starts a second
manager. `status` and `messages` can read state while a worker runs. `run` waits for
external/user events without polling an LLM. `chat` provides natural-language control
and explicit terminal commands. No live dashboard is needed.

Policy/requirement updates are reviewable proposals. Only an explicit `/accept`
(or equivalent queued command) applies permanent changes. Natural-language inference
alone cannot rewrite policy. Requirement revisions append constraints and mandatory
criteria, retaining original criteria. Affected task IDs can be explicit; omitted
IDs conservatively affect all active tasks. Affected generations are invalidated,
new constraints are delivered, and execution is stopped/replanned. Unaffected task
results and all old evidence remain stored. Implementation authorizations/approvals
are conservatively revoked on policy or goal revision changes; candidate work is
retained for reauthorization and verification.

## Two research levels and shared knowledge

`ResearchPlan` specifies explicit source files, a question and per-role tool/time
limits plus an attempt cap. Discovery may inspect at most 20 sources; focused research
at most eight and must name one hypothesis and its falsification condition. Each
role is a fresh tool-using Agents SDK agent. It can only read scoped source excerpts
and retrieve existing knowledge: no shell, arbitrary network, or recursive search.
Services, jobs, dependencies, repositories, documents and observability resources
are mapped from supplied catalogs/manifests and exported logs/metrics/documents.
There are no live observability collectors in this iteration. An unavailable system
is recorded as a gap, never silently claimed to have been examined.

Mandatory knowledge retrieval precedes new investigation and is recorded separately
from the agent's discretionary tool-call budget, so a one-read budget still permits
one source read. Reads capture bounded
UTF-8 excerpts, line numbers, content SHA-256 revisions, source modification times,
and observation timestamps. The 1–100 line bound is explicit in the SDK tool schema; argument errors return
actionable feedback instead of implying source permissions failed. Files are limited
to 128 KiB; large resources need a
bounded export. Every read/search query, accessed evidence ID, and spent tool call
is durable. Search has bounded pagination and is workspace-scoped. JSON export includes
maps, findings, rejected hypotheses, queries, job/attempt histories and source snapshots.
No HTML view adds value beyond these small CLI/JSON navigation paths.

Discovery produces a map, gaps and an opportunity backlog. Entries remain `observed`
or `assumption`, never verified merely because discovery says so. Backlog scores use
`impact * evidence confidence / (risk * effort)` with explicit ordinal inputs and
reasons. Unsupported opportunities get confidence zero. Scores aid the manager;
they are not calibrated economic predictions. Repeated discovery updates the backlog.

Focused research consults earlier work, gathers support and contradictions, and
returns findings, assumptions, uncertainty and a next action. Structured discovery
and research reports are delivered directly to the manager; historical retrieval is
additional context, not a substitute for the worker handoff. Invalid plans return
targeted rejection feedback; three identical rejections escalate instead of crashing
the application or launching an unbounded retry. Database-backed SDK tools are async: SDK 0.24 dispatches synchronous tools in
threads, which would violate SQLite connection ownership. A separate evaluator
gets the original hypothesis, falsification condition, report, and **actual captured
source excerpts**, and can independently read scoped files. It produces supported,
insufficient, falsified or blocked findings with evidence IDs and targeted feedback.
A deterministic gate rejects invented/stale citations, uncited claims and blocked
support. Only claims independently cited by a supporting evaluation become verified
knowledge. A falsified hypothesis is recorded as rejected, not deleted.

Equivalent normalized hypothesis/question, scope, source versions, and goal/policy
revisions yield a detectable duplicate. This is exact/canonical duplication detection,
not a guarantee of detecting semantic paraphrases. Changed source versions permit a
fresh investigation. Retrieval flags knowledge stale when sources change/disappear or
an observation is over 24 hours old; semantic freshness of exported metrics still
needs evaluator judgment. Rejected and invalidated knowledge remains searchable.

Each specialist role has a timeout, tool-call cap and SDK turn cap. The goal has a
persisted total role-run budget (12 by default; evaluation spends a run too). Manager reasoning turn/time exhaustion also produces a durable pause/blocker rather
than an uncaught crash. A retry
requires targeted feedback and cannot reset the attempt budget. Exhaustion escalates.
There is no retry loop that invents fresh budgets after every failure.

## Codex execution and communication

When `OPENAI_API_KEY` is present, both Codex adapters use an explicit Responses API
provider with `env_key = "OPENAI_API_KEY"` and a writable local session home. No key
value is stored in arguments/configuration or copied from another credential file.
Without it, the existing Codex configuration is preserved.

The original `CodexWorker`/Node bridge uses the official Codex SDK for structured
start/resume handoffs. Interactive workers use `CodexRuntime`, a Python JSONL
client of the supported Codex App Server protocol: `initialize`, `thread/start`,
`thread/resume`, `turn/start`, `turn/steer`, `turn/interrupt`, `thread/read`, and native
user-input request responses. Installed CLI JSON schemas were inspected when building
this adapter. Steering uses native expected-turn preconditions and message identities,
not fabricated conversation entries. Thread IDs are saved before starting a turn;
turn IDs, questions, findings, blockers and usage/progress events are retained separately.

The outbox tracks pending, sending, acknowledged and uncertain delivery. An RPC response
confirms acceptance, not completion of the requested engineering work. Native question
responses have no acknowledgment; they are recorded as `sent_no_ack`. Requests from a
previous connection cannot be answered using a recycled RPC ID. Pending questions and
messages survive restart; reconnect/reconcile and start a supported new turn if an old
request can no longer be answered. SDK research roles use cancellation and a fresh
bounded attempt with revised context, rather than claiming live steering is supported.
Pause is interrupt plus durable state; it is not an OS process freeze.

## Authorized implementation and independent verification

`authorize --allow-code-modifications` binds a reviewed `ImplementationSpec` to a
current goal/policy revision, accepted research, allowed paths, executable acceptance
checks, and bounded attempts. The manager cannot grant this permission itself. Every
mandatory criterion must have an authorized check. Explicit external oracle scripts
are hashed; changing them requires reauthorization. The goal workspace must be a Git
repository with a commit before implementation can start.

A real Codex App Server worker receives the objective, accepted findings, constraints,
checks and repair feedback in an isolated detached Git worktree. It has workspace-write
sandbox access there, no sandbox network access, and no automatic approval escalation.
Deployments/destructive operations are prohibited. The harness checks changed paths
against authorization, commits a candidate and retains its exact SHA. Worker handoffs,
session references, test artifacts, logs and unresolved final-message concerns are
stored. Original checkouts are not modified or merged automatically.

Independent verification uses another clean detached worktree at that candidate SHA,
running the user-authorized executable commands in separate OS processes with provider
credentials removed from the environment. Outputs and digests, exit codes, criterion
coverage and goal revision are saved. Timeouts terminate process groups. Verification
rejects tracked source/HEAD changes. These are trusted local test commands, not a general
hostile-code sandbox; run untrusted repositories in a separately isolated environment.
The quality of the authorized test oracle bounds what a passing result establishes.

Local failures return concrete artifacts to a bounded repair attempt. A check classified
as an architectural/assumption failure returns `needs_research`, requiring manager
reprioritization. Exhausted attempts escalate. Success is only `ready_for_audit`.
Code goals can require verified implementation explicitly (`--require-implementation`);
they cannot complete on recommendations even if no implementation has been assigned.
The manager sees the authorized objective, paths and executable checks, not just an
opaque authorization ID. A separate manager-requested goal audit checks current criteria, current authorization,
exact candidate revision and immutable verification artifacts. Missing criterion coverage
fails even if all executed tests passed. Only an audit can complete implementation work;
no merge, production deployment or destructive action follows automatically.

## Recovery and meaningful progress

WAL SQLite stores goals, immutable original requirements, revisions, decisions,
assignments, ownership/generations, sessions, messages, research, approvals, candidate
revisions and artifacts. Snapshot the database, SDK sessions DB, Codex home, repositories,
worktrees and logs together. No process is assumed to survive restoration.

On restart, previously running work and in-flight deliveries become uncertain. No start,
steer, code operation or external action is blindly replayed. Reconcile native threads
and turns before retrying. Worktree creation has a deterministic journaled target and
checks actual Git metadata after a lost response. A completed implementation whose
response was lost can be reconciled and independently verified without coding again.
A failed/interrupted turn can become a bounded repair after its outcome is known. Missing
external history remains a manual reconciliation blocker; exactly-once execution is not
promised. Explicit delivery reconciliation requires recorded evidence.

Generations fence off late results. Goal/policy changes during research prevent publishing
old findings as verified. Verification and final audits are revision-bound. Progress uses
new source observations, distinct concrete command/file-change outcomes and verification
artifacts; tokens and process activity do not reset the progress clock. Exact repeated
observations are deduplicated. These signals are conservative progress proxies, not proof
that an engineering improvement is useful. Stalls/deadlines pause and report blockers;
waiting questions do not spend manager tokens. Repeated failed approaches and bounded
research/repair exhaustion escalate instead of looping indefinitely.

Activity/decision/message tables provide local observability. Successful SDK calls and
Codex token usage snapshots are recorded where available. Shutdown interrupts owned
workers, preserves pending messages and stops the local runtime. Lost termination
acknowledgments remain uncertain until reconciliation. There is no distributed lease
service; this is a single-owner, local-filesystem system.
