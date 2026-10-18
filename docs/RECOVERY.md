# Recovery and unattended operation

Run one manager owner per local SQLite database. Use `chat` for a terminal session,
or `run --hours ...` with `send`, `status`, and `messages` from another terminal.
A second process can enqueue/read messages but cannot become a conflicting manager.

Retain the WAL-consistent database, adjacent `.sessions.db`, role Markdown, Codex
home, repositories/Git metadata, worktrees and artifacts together. Use SQLite's backup
API or stop writes before copying; copying only the main DB with active WAL writes is
unsafe. Preserve paths or repair linked worktrees with Git after relocation.
Authentication and processes are not restored by copying files.

## Restart and reconcile

`/quit`, EOF and normal shutdown interrupt owned work and retain paused/uncertain
state. SIGKILL cannot clean up. On restart, running/waiting work and in-flight sends
become uncertain. Evidence, consumed budgets and pending messages remain durable.
The OS lock establishes ownership, not a stale PID/heartbeat.

```sh
omnitrix status GOAL_ID
omnitrix messages GOAL_ID
omnitrix chat GOAL_ID
```

Use `/reconcile TASK_ID` to inspect the existing native thread and exact turn. A
completed implementation can be independently verified without coding again. A known
failed/interrupted turn can become a bounded repair. Active turns must first finish
or be interrupted. Missing external history/session IDs remain a manual blocker:
inspect retained work and artifacts before authorizing replacement. A lost response
never establishes that the action did not happen.

Uncertain deliveries are not automatically resent. Inspect native history, then use
`/resolve MESSAGE_ID delivered EVIDENCE` or `/resolve MESSAGE_ID not_delivered EVIDENCE`.
This records explicit operator reconciliation. It is not an exactly-once guarantee;
the latter permits a later explicit resend. The CLI requires a concrete evidence note.
Native answers have no RPC acknowledgment and remain `sent_no_ack` in the ledger.

Native question IDs belong to a runtime connection. After reconnect, reconcile/resume
with the actual answer rather than replaying an old numeric RPC response. SDK research
roles use cancellation and a fresh bounded attempt with current constraints and prior
knowledge; they do not pretend to support live steering. Pause is an interrupt plus
durable state, not an OS freeze.

## Changes, progress and budgets

Permanent policy/requirements changes need explicit proposal acceptance. Original
mandatory criteria cannot disappear. Affected task generations reject late results;
unaffected task work and all old evidence are retained. Policy/goal revisions revoke
implementation authorization and completion approval. Reauthorize and verify current
criteria before completing a candidate. No merge or deployment follows automatically.

The activity ledger records decisions, source observations, concrete command/file
outcomes, verification and reconciliation. Tokens and repeated identical observations
do not reset progress timers. These signals are progress proxies, not proof of useful
improvement. Waiting questions do not poll an LLM. Wall-time limits still apply; expired
turns require reconciliation. Research goal/attempt budgets and implementation repair
budgets survive restart. Repeated failures escalate rather than reset budgets.

Inspect `activity`, `messages`, `research_queries`, `research_runs`, `verifications`,
`external_actions`, and `completion_audits`, or use CLI status/knowledge/export commands.
No monitoring service is required. See the status document for remaining live-validation
limits and the experiment document for interruption/fault commands.
