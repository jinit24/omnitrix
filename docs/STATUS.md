# Current status

Implemented research, interactive control, authorized implementation and lightweight
reliability while preserving the original manager, Codex SDK adapter and SQLite design.
No dashboard, broker, workflow framework, deployment, automatic merge or distributed
scheduler was added.

## Verified functionality

- Conversational Agents SDK manager, durable SDK session identity and manager-only
  conversation; versioned policies, immutable original criteria and revised constraints.
- Nonblocking workers, interactive CLI and cross-process durable inbox; serialized
  manager decisions; explicit policy/requirement approval, affected-task generation
  fences, native Codex steering/interrupt/questions, progress/blockers, deduplicated
  messages and acknowledgment/uncertainty tracking.
- Bounded discovery, system maps, ranked opportunities, falsifiable research, shared
  knowledge, source hashes/timestamps/staleness, query/evidence ledgers, rejected
  hypotheses, independent evaluation, targeted feedback and durable budgets.
- Explicit authorization after accepted research, real Codex implementation path in
  isolated worktrees, candidate revisions and handoffs; separate executable verification,
  oracle/artifact/revision binding, repairs, assumption escalation, bounded retries and
  exact-criterion goal audits.
- Local process restart recovery, uncertain ownership/delivery handling, native thread
  reconciliation, lost Git acknowledgment reconciliation, completed implementation
  verification without recoding after a lost response, stale-result rejection, progress
  deadlines, no model polling for waiting questions and safe retained shutdown state.

The offline suite uses scripted model reasoning/coding with real SQLite, SDK session
storage, Git worktrees, subprocess verification, cross-process messaging, SIGKILL
and worker-process termination. The optional live SDK start/resume test requires
`OMNITRIX_LIVE_CODEX=1` and `OPENAI_API_KEY`; it is skipped during offline checks.
The historical 55-test offline/56-test live measurements below predate the wider
research controls. No scripted result is presented as a live model result.

## Wide research controls

New whole-repository campaigns persist approved strategy batches, atomic queue
claims, area fairness, source/revision fences, primary-area sample attribution and
remaining questions. Typed evidence obligations are preflighted against real ranges
and read/checkpoint budgets; authorized dependency grants remain within the same
attempt and resource limits. Report-time proof snapshots and independent evaluation
prevent later reads from repairing missing researcher evidence.

Manager model input uses bounded cards and paged retrieval while preserving current
mandatory contracts. New dollar budgets cap manager/chat allocation and requests,
and protect explicit evaluation allowances before research. Structured/history
failures get one durable corrective decision; local research/review interruptions
reconcile without replay or refunding uncertain provider charges.

The offline `scripts/workflow_benchmark.py` covers six scenarios with real persisted
state and frozen code/prompt hashes. It checks approved batch dispatch against a
per-task scripted baseline, same-attempt dependency repair, atomic preflight,
bounded manager correction, restart reconciliation, staleness/scope/completion
fences, and context/history limits. This validates mechanics rather than live
research effectiveness. See [workflow controls](WORKFLOW_CONTROL.md).

Final offline validation on 2026-10-19: **234 passed, 1 skipped**. All six workflow
benchmark scenarios passed with zero provider calls; the existing full-loop demo
also completed both acceptance criteria after one executable repair. The frozen
workflow benchmark result is [retained as JSON](experiments/workflow-control-20261019.json).

The earlier Kubernetes and Apache HTTP Server trials remain incomplete,
operator-assisted audits. No new paid public-repository run, full semantic coverage,
measured cost improvement or architectural superiority is established by these fixes.

`scripts/demo.py` demonstrates discovery → research → evaluation → authorization boundary
→ failed check → bounded repair → executable success → manager goal audit.
`scripts/faults.py` passes nine fault scenarios and emits JUnit/JSON. `scripts/benchmark.py`
compares three paired single-worker/manager fixtures, with live and harness-only modes.
The smoke run satisfied 6/6 criteria per arm with zero incorrect completion claims;
it established harness function, **not architectural superiority**. See [experiments](EXPERIMENTS.md).

## APIs and live limitations

Inspected `openai-agents==0.24.0`: Agent, Runner, function tools, typed output, SQLiteSession
and usage; `@openai/codex-sdk==0.163.0`: start/resume/streamed turns; and Codex CLI 0.159.2
App Server generated JSON schemas: thread start/resume/read, turn start/steer/interrupt,
and native questions/answers. Direct Python dependencies and the Node dependency tree
are pinned. The repository was empty before the first stage.

References: https://github.com/openai/openai-agents-python and
https://github.com/openai/codex/tree/main/sdk/typescript, plus installed CLI schema output.

`OPENAI_API_KEY` is now available and used by both Python agents and both Codex
adapters through an explicit environment-key API provider. It is never copied into
arguments or saved configuration. API-backed Codex uses a writable local session home;
the previous ChatGPT-route 403 and protected-home failures are no longer blockers.
Native worker commands required the approved execution route outside an outer sandbox
that masks Codex's daemon socket directory; Codex's own sandbox remained enabled.

Live validation now includes SDK start/resume; native worker execution; a manager reply
in 2.52 seconds while a real delayed worker ran; approved policy delivery/interruption;
conversation/pending-message restoration; and native reconciliation of the interrupted
turn. Live manager tasks completed discovery, accepted research, authorized Codex
implementation, executable verification and final audit.

The clean paired comparison passed **6/6 criteria (3/3 tasks) for a single Codex worker**
and **3/6 criteria (1/3 tasks) for the manager**. Times were 62.6 s versus 112.4 s total.
The manager paused the retry and batching tasks over callback details it could not
establish from the supplied sources. No application exception or false completion claim
occurred in that clean trial. This is evidence of current limitations, not superiority.
See [fixture-v1 results](experiments/live-api.json).

A final separate fixture-v2 run supplied the existing callback acceptance contracts to
both arms, keeping all executable assertions unchanged. With GPT-5.4 as coordinator,
the manager completed **2/3 tasks and 4/6 criteria**, versus **3/3 and 6/6** for the
single worker. Times were 176.2 s versus 59.0 s; observed input/output tokens were
461,690/12,328 versus 232,753/4,404. The manager paused retries after independent review
demanded more edge-case evidence. It had no authorization to implement that task and
its final explanation did not clearly distinguish that from missing user permission.
This planning/evaluation limitation remains; no architectural advantage is established.
See [v2 results](experiments/live-api-v2-gpt54.json) and [protocol](EXPERIMENTS.md).

Live experiments exposed and led to fixes for:

- API credentials not being selected for Codex's default login route.
- SDK synchronous tools executing SQLite operations on the wrong thread; database tools
  are now async, with tests invoking the actual SDK dispatch wrapper.
- Invalid research scopes crashing an arm instead of returning bounded corrective feedback.
- Hidden read-range limits and opaque SDK tool errors; line bounds are now advertised
  in the schema and argument failures give corrective feedback.
- Manager SDK turn exhaustion escaping the supervisor; it now records a durable pause
  and blocker without automatically retrying.
- Mandatory knowledge prefetch consuming a one-call research budget before any source read.
- Structured research reports being omitted from manager handoffs.
- Authorized executable checks being hidden behind opaque authorization IDs in manager state.
- Premature completion of code goals before any verified implementation existed; explicit
  code-goal completion gates are now persisted and enforced.
- Benchmark scoring mutable worker files and crashing on unstarted worktrees; scoring now
  uses a separate checkout at an immutable candidate revision with regression tests.

Earlier diagnostic runs and their failures are preserved, not pooled with the clean
comparison. No multi-hour experiment, statistical quality advantage, production benefit,
or new-cloud-machine restoration is claimed. Environment publication remains separate.

## Remaining limits and next improvements

- Single-owner local execution, POSIX locks/process groups and editable source install.
  Preserve the DB, SDK sessions DB, Codex home, repositories/worktrees and artifacts together.
- Unknown external outcomes can require operator reconciliation. No exactly-once delivery
  or blind replay; old native question IDs cannot be reused after reconnect. Pause is an
  interrupt/resume, not a process freeze. Incomplete benchmark arms require inspection.
- Research reads bounded local files/exports, not live remote collectors. Exact/canonical
  duplicate detection does not detect all semantic paraphrases. Hashes do not prove metric
  freshness. Independent LLM evaluation can err: deterministic gates prove citation integrity,
  not universal semantic truth.
- Verification depends on trusted authorized executable oracles. Passing them does not prove
  all global properties. Test processes omit provider credentials but are not a hostile-code
  sandbox; use external isolation for untrusted repositories.
- Native APIs are version-sensitive. Failed provider calls can omit usage; missing baseline
  research/correction/recovery measurements are not invented.

Prioritize reducing unnecessary research/clarification and improving callback-contract
reasoning without weakening evidence gates, then paired multi-hour trials with identical
instrumentation and held-out oracles. Measure failure rates and contention before adding
infrastructure. Improve oracle coverage, semantic retrieval and external source adapters
when measurements establish value. See [architecture](ARCHITECTURE.md) and [recovery](RECOVERY.md).
