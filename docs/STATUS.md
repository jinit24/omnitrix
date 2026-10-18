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

The deterministic suite passes **40 tests**, with **one live Codex test skipped**.
It uses mocked model reasoning/coding with real SQLite, SDK session storage, Git worktrees,
subprocess verification, cross-process messaging, SIGKILL and worker-process termination.
No mocked result is presented as a live model result.

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

The earlier real Codex SDK smoke returned a thread ID, then failed model connectivity
with proxy HTTP CONNECT 403. Codex authentication was present; its read-only home needed
session-write permission for that attempt. `OPENAI_API_KEY` is absent. No successful live
manager/research/implementation cycle, live native steering/recovery, multi-hour run or
comparative model-quality result is claimed. Native protocol tests use a subprocess double.

Supply credentials securely, permit configured provider destinations and provide writable
Codex session storage before live validation. Start with:

```sh
OMNITRIX_LIVE_CODEX=1 .venv/bin/python -m pytest tests/test_codex_live.py -q
```

Then exercise chat/run and the explicitly authorized live benchmark. Environment
publication and restoration on a new cloud machine are separate, unverified operations.

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

Prioritize paid live end-to-end validation, then paired multi-hour trials with identical
instrumentation and held-out oracles. Measure failure rates and contention before adding
infrastructure. Improve oracle coverage, semantic retrieval and external source adapters
when measurements establish value. See [architecture](ARCHITECTURE.md) and [recovery](RECOVERY.md).
