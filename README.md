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

Set `OPENAI_API_KEY` securely for the manager and research roles. Authenticate Codex
separately (`codex login`) with writable Codex session storage. `OMNITRIX_MODEL`
defaults to `gpt-4.1`. Never put credentials in goal text, Markdown, or the database.
Model calls incur usage costs. Source/editable installation is required for the role
Markdown and Node bridge; this iteration is not a standalone distributed wheel.

## Start and steer a persistent manager

```sh
omnitrix create 'Improve performance, reliability and cost of the engineering system' \
  --criterion 'Identify and verify one bounded improvement' --workspace .
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
validate the harness; they say nothing about whether multiple real agents outperform
one real Codex worker. See [experiment results and protocol](docs/EXPERIMENTS.md).

See [architecture](docs/ARCHITECTURE.md), [status and limitations](docs/STATUS.md), and
[recovery](docs/RECOVERY.md). Keep the SQLite DB, `.sessions.db`, Codex home, worktrees
and artifacts together when moving/restoring an environment. Authentication and live
processes must be reestablished after restoration.
