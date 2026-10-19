# Omnitrix

A persistent, conversational engineering manager built with Python, the OpenAI
Agents SDK, SQLite, Markdown, and real Codex workers. It plans bounded research,
shares revision-aware knowledge, accepts steering while work runs, and can implement
explicitly authorized changes with independent executable verification and repairs.

## Install

Use a source checkout with Python 3.11+, Node 20+, Git, and the Codex CLI available
on PATH for interactive/implementation workers. Linux/macOS are supported (POSIX locks
and process groups). The Node dependency installs the official Codex SDK/CLI for the
original SDK adapter; its binary can also be selected using `OMNITRIX_CODEX_PATH`.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
npm ci
pytest -q
```

Set `OPENAI_API_KEY` securely: the manager, research roles, Codex SDK adapter and
Codex App Server all use it. No separate Codex login is needed when that variable
is present. API-backed Codex defaults to `gpt-5.3-codex`; override it with
`OMNITRIX_CODEX_MODEL`. `OMNITRIX_MODEL` defaults to `gpt-4.1` for SDK agents.
Codex sessions use `.omnitrix/codex-api` (override `OMNITRIX_CODEX_HOME`); credentials
are referenced by environment variable name and never written to its configuration.
Without the key, existing Codex login/configuration is retained. Never put credentials
in goal text, Markdown, or the database.
Model calls incur usage costs. Source/editable installation is required for the role
Markdown and Node bridge; this iteration is not a standalone distributed wheel.

## Start and steer a persistent manager

```sh
omnitrix create 'Improve performance, reliability and cost of the engineering system' \
  --criterion 'Identify and verify one bounded improvement' --require-implementation --workspace .
# Use the printed goal ID:
omnitrix chat GOAL_ID
```

Natural language supports status questions, priority explanations, task steering and
proposed policy/requirement changes. Durable changes need explicit acceptance; task
steering alone does not alter permanent policy. Terminal commands include:

```text
/status
/run 10
/policy Preserve public interfaces and be thorough about correctness.
/accept PROPOSAL_ID
/requirements {"constraint":"Stop researching service A","add_requirements":["Check service B"],"affected_tasks":["TASK_ID"]}
/send TASK_ID Examine the contradictory measurement before continuing.
/pause TASK_ID
/cancel TASK_ID
/answer QUESTION_MESSAGE_ID {"question_id":["the answer"]}
/reconcile TASK_ID
/resolve MESSAGE_ID delivered Evidence observed in the saved native thread.
/events
/quit
```

`/policy` proposes complete replacement policy text. Natural-language policy proposals
are instructed to retain existing expectations; review the proposal before accepting.
`/requirements` adds constraints/criteria; it cannot remove original mandatory criteria.
Omitting `affected_tasks` conservatively invalidates all active tasks. A cancelled task
can retain partial work. Pause interrupts and retains context; resume a saved Codex
thread using a manager-selected `follow_up` rather than replaying a stopped action.

For unattended runs and interaction from another terminal:

```sh
omnitrix run GOAL_ID --steps 50 --hours 4
omnitrix status GOAL_ID
omnitrix send GOAL_ID 'What are you working on?' --id unique-message-id
omnitrix send GOAL_ID --command status --id another-unique-id
omnitrix send GOAL_ID 'TASK_ID Change the bounded approach.' --command steer
omnitrix messages GOAL_ID
```

`send` uses the existing manager's durable inbox, not a second manager process.
`--command` also supports `policy`, `requirements`, `accept`, `pause`, `cancel`,
`continue`, and `reconcile`. Once a decision budget is spent the unattended process
can wait for new commands without model polling. `resume GOAL_ID --steps 5 --message
'Focus on correctness'` runs a bounded, restartable foreground session using the same
nonblocking supervisor. One manager owns a database; use `send` to contact it while
`run` or `chat` already owns it.

The database defaults to `.omnitrix/state.db` relative to the invocation directory.
Use `omnitrix --db /absolute/path/state.db ...` when changing directories. `create`,
`status`, `knowledge`, `export`, and inbox writes can work offline. Inspect replies
with `messages`. `/status` works without calling a model.

## Research and knowledge

The manager uses `discover` for bounded reconnaissance and `research` for one explicit
hypothesis and falsification condition. Supply small registries, manifests, documents,
and exported metrics/logs in the goal workspace. It does not blindly crawl every service
or acquire live observability access. Sources must be explicit local UTF-8 files of at
most 128 KiB; split/export large resources. Research and evaluation have separate tool
and wall-time limits and share a persisted goal budget. Scope and attempts are bounded.

```sh
omnitrix knowledge GOAL_ID 'retry'
omnitrix export GOAL_ID > research.json
```

Findings distinguish verified claims, observations, assumptions, rejected hypotheses
and invalidated knowledge. Changed source hashes, missing files and observations older
than 24 hours are flagged stale. Exact repeated investigations are detected. The manager
can retrieve older findings in a fresh session rather than repeating worker history.

## Authorize an implementation

First obtain accepted research for the goal. Review a JSON specification such as:

```json
{
  "objective": "Fix the bounded retry contract without changing permanent errors",
  "research_job": "ACCEPTED_RESEARCH_JOB_ID",
  "allowed_paths": ["src/retry.py"],
  "checks": [
    {
      "requirement": "EXACT CURRENT MANDATORY CRITERION TEXT",
      "argv": ["/absolute/path/python", "/absolute/path/trusted_acceptance_check.py"],
      "timeout": 60,
      "failure_kind": "local"
    }
  ],
  "max_attempts": 2,
  "seconds": 300
}
```

`--require-implementation` prevents a code goal from completing without an independent
verified implementation and current audit, even before an implementation is assigned.
Every mandatory criterion needs a check. Use `failure_kind: "assumption"` for a
check whose failure means the architecture/assumption needs research. External oracle
files are hashed at authorization. Checks execute in a separate candidate worktree;
`OMNITRIX_CANDIDATE` points to it. Use trusted independent checks outside the editable
candidate when testing adversarial or easily gamed acceptance conditions.

```sh
omnitrix authorize GOAL_ID implementation.json --allow-code-modifications
# The returned implementation ID is visible to the manager:
omnitrix resume GOAL_ID --steps 5
```

This authorizes bounded code changes in an isolated worktree, not a production
deployment, destructive operation, merge, or push. The goal workspace needs a Git
commit. Candidate worktrees, logs, research and approvals remain available for review.
Requirement/policy revisions revoke authorization; reauthorize after reviewing changes.
Check commands are trusted local processes, not a hostile-code sandbox. Provider
credentials are omitted from their environment. No automatic deployment exists.

## Demonstrations and experiments

```sh
# Actual SQLite, Git worktrees and test processes; mocked model decisions/coding:
python scripts/demo.py --output .omnitrix/demo-run
python scripts/faults.py --output .omnitrix/fault-run
python scripts/benchmark.py --output .omnitrix/benchmark-run

# Real paid model runs, with explicit authorization for generated fixture repositories:
python scripts/benchmark.py --live --allow-code-modifications --hours 4 \
  --output .omnitrix/live-experiment
```

Use new output paths. Completed benchmark arms are reusable on a rerun. An incomplete
arm requires inspection/reconciliation rather than blind replay. Mock benchmark numbers
validate the harness. The recorded live runs did not outperform a single Codex worker;
see the separate fixture/model profiles and remaining planning limits in the report. See [experiment results and protocol](docs/EXPERIMENTS.md).

See [architecture](docs/ARCHITECTURE.md), [status and limitations](docs/STATUS.md), and
[recovery](docs/RECOVERY.md). Keep the SQLite DB, `.sessions.db`, Codex home, worktrees
and artifacts together when moving/restoring an environment. Authentication and live
processes must be reestablished after restoration.

## Custom research context and quality gates

Provide a per-goal JSON brief and rubric. The file is read by the CLI and its
contents are stored in SQLite; every manager decision, discovery/research attempt
and independent evaluation receives the current version directly.

```json
{
  "brief": "Prioritize checkout reliability. Preserve idempotency. Our belief that retries amplify overload is unverified. Compare alternatives against failure rate, duplicate charges, latency and cost.",
  "rubric": [
    "Support factual claims with current source evidence",
    "Address contrary evidence and distinguish measured results from assumptions"
  ]
}
```

```sh
omnitrix context GOAL_ID --file research-context.json
# Review the proposed brief/rubric, then in omnitrix chat GOAL_ID:
# /accept PROPOSAL_ID
omnitrix context GOAL_ID
```

`/context` displays the active brief in chat. `context --file` proposes a complete
replacement; it does not change active instructions until accepted. Updates keep
all earlier versions, advance the goal revision, invalidate pending/active research, mark earlier accepted research stale,
invalidate its verified findings and revoke implementation authorizations. Existing candidates and evidence remain
available. A brief supplies context, not verified facts or code-change permission.

With context configured, each new discovery/research plan must state `decision`,
`expected_value` and `stop_condition`. Evaluation must assess each exact rubric
criterion once. Supported results are rejected when applicable criteria are unmet,
met criteria lack evidence, or citations are stale/unknown. A justified
`not_applicable` judgment remains the evaluator's responsibility. These checks
validate structured review and evidence integrity; semantic correctness still
requires judgment, and executable acceptance checks remain separate.

Longer investigations can record bounded `checkpoint` tool calls with evidence,
remaining uncertainty and next action. Checkpoints consume the tool budget and do
not reset deadlines or count as independently verified progress. Status includes
recent checkpoints and a research agenda summary grouped by stage/status. The
manager can retrieve older jobs through `recall_history(section='research')` to
review coverage, repeated approaches and priorities. This is deliberative oversight
between bounded attempts, not continuous independent semantic supervision.

## Whole-repository manager trials

See [public-repository audit protocol](docs/PUBLIC_RESEARCH.md) for pinned full
source snapshots, persistent area/sub-area agendas, read-only live runs, $25
per-goal request guards, crash/restart experiments and standalone reports.
`triaged` and `deferred` never mean fully audited; source sampling and exclusions
are reported separately from inventory. Reopened requirements resume the same
budget. No PRs, pushes, public-repository changes or deployment occur.

Independent review receives the actual researcher checkpoint ledger and bounded
portfolio context. To enforce a pre-report evidence checkpoint for custom goals,
include `"require_checkpoint": true` in the context JSON. This is a Python gate,
not a reviewer inference. `reevaluate` can spend one additional bounded evaluation
on a retained report without rerunning research or resetting attempts. Original
reports/reviews remain intact; supported observations do not satisfy failed
workflow gates or authorize implementation.

For the durable campaign queue, evidence obligations, protected evaluation budgets,
compact manager context and automatic local recovery, see
[workflow controls](docs/WORKFLOW_CONTROL.md). Run the offline integration benchmark
without provider calls:

```sh
python scripts/workflow_benchmark.py --output work/workflow-benchmark
```

Use a new output directory. Its JSON results and SQLite databases preserve both
successful scenarios and injected failures. Scripted fixture results validate the
controls; they do not measure live research quality or dollar savings.
