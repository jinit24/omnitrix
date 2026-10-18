# Omnitrix

A persistent engineering manager using the OpenAI Agents SDK, real Codex workers,
and SQLite. It plans bounded research, accepts steering during work, and verifies
explicitly authorized code changes. The manager has **not outperformed a single Codex
worker** in the recorded trials.

## Install

Requires Python 3.11+, Node 20+, Git, and Codex CLI on PATH. Linux/macOS only.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
npm ci
pytest -q
```

Set `OPENAI_API_KEY` securely; all agents and Codex adapters use it. Never put keys in
goals, files, or the database. Model calls cost money. Without the key, Codex keeps its
existing login configuration. `OMNITRIX_CODEX_PATH` can select the installed CLI.

| Setting | Default |
| --- | --- |
| `OMNITRIX_MODEL` | `gpt-4.1` |
| `OMNITRIX_CODEX_MODEL` | `gpt-5.3-codex` |
| `OMNITRIX_CODEX_HOME` | `.omnitrix/codex-api` |
| Database (`--db PATH` before command) | `.omnitrix/state.db` |

Use a source/editable install: instructions and Node bridge are repository resources.

## Run and observe

```sh
omnitrix create 'Improve service reliability' \
  --criterion 'Verify one bounded improvement' --require-implementation --workspace .
omnitrix chat GOAL_ID
# Another terminal; read-only app and HTML documentation:
omnitrix --db .omnitrix/state.db dashboard
```

The dashboard binds loopback port 8765. It has no authentication; keep access trusted.
Use `--require-implementation` for code goals: recommendations alone cannot complete them.

| Chat command | Purpose |
| --- | --- |
| `/status`, `/events`, `/run 10` | Inspect or run bounded decisions |
| `/policy TEXT`, `/accept PROPOSAL_ID` | Propose and explicitly accept permanent policy |
| `/requirements JSON` | Add constraints/criteria; originals cannot be removed |
| `/send TASK_ID TEXT` | Steer a worker |
| `/pause TASK_ID`, `/cancel TASK_ID` | Interrupt work, retaining state |
| `/answer MESSAGE_ID JSON` | Answer a worker question |
| `/reconcile TASK_ID` | Inspect an uncertain native turn |
| `/resolve MESSAGE_ID delivered EVIDENCE` | Record delivery reconciliation |
| `/quit` | Save and shut down |

`/policy` replaces the full policy; review before accepting. Requirement JSON accepts
`constraint`, `add_requirements`, and `affected_tasks`; omitted task IDs affect all active work.
Changes revoke implementation authorization. Pause interrupts; it is not an OS freeze.

```sh
omnitrix run GOAL_ID --steps 50 --hours 4
omnitrix send GOAL_ID 'What are you working on?' --id unique-message-id
omnitrix send GOAL_ID 'TASK_ID Check contradictory evidence' --command steer
omnitrix status GOAL_ID
omnitrix messages GOAL_ID
omnitrix knowledge GOAL_ID 'retry'
omnitrix export GOAL_ID > research.json
omnitrix report GOAL_ID --output research.html
```

One manager owns each DB. `send` queues to that owner; it never starts another manager.
`resume GOAL_ID --steps 5 --message TEXT` restarts bounded work when no owner is running.

## Authorize code changes

Accepted research and a committed Git workspace are required. Review `implementation.json`:

```json
{
  "objective": "Fix the bounded retry contract",
  "research_job": "ACCEPTED_RESEARCH_JOB_ID",
  "allowed_paths": ["src/retry.py"],
  "checks": [{
    "requirement": "EXACT CURRENT MANDATORY CRITERION TEXT",
    "argv": ["/absolute/path/python", "/absolute/path/trusted_check.py"],
    "timeout": 60,
    "failure_kind": "local"
  }],
  "max_attempts": 2,
  "seconds": 300
}
```

```sh
omnitrix authorize GOAL_ID implementation.json --allow-code-modifications
omnitrix resume GOAL_ID --steps 5
```

Every mandatory criterion needs an executable check. `failure_kind: "assumption"`
returns failures to research. External oracles are hashed; `OMNITRIX_CANDIDATE` identifies
the clean candidate worktree. Commands are trusted local processes, not a hostile-code
sandbox; provider credentials are removed. Authorization permits bounded worktree edits,
never deployments, destructive operations, merges or pushes.

## GitHub Pages

Enable **Settings → Pages → GitHub Actions**. The included workflow publishes a labeled
static demo and HTML docs. Live manager state stays local.

Research sources up to 32 MiB support bounded reads, search, and CSV aggregation.
Partition larger exports; observed coverage is not exhaustive system coverage.

## Durable research campaigns

Create a fresh goal for a pinned local clone, then start a read-only investigation
with persistent time, decision, role-run and dollar limits:

```sh
omnitrix create 'Find system inefficiencies and compare falsifiable explanations' \
  --criterion 'Distinguish code mechanisms, measured impact and missing data' \
  --workspace /absolute/path/to/pinned-repository
omnitrix --db .omnitrix/state.db campaign GOAL_ID \
  --days 3 --decisions 1000 --research-runs 2000 --dollars 25
# After a process restart: reuse the same DB and goal, without new limits.
omnitrix --db .omnitrix/state.db campaign GOAL_ID
# Inspect or pause from another terminal.
omnitrix --db .omnitrix/state.db status GOAL_ID
omnitrix --db .omnitrix/state.db campaign GOAL_ID --pause
```

Each campaign is capped at $25. Standard-tier GPT-4.1 requests reserve maximum
cost before inference; lost responses retain that reservation. Native workers,
hosted tools, code changes and external PRs are disabled for these campaigns.
The HTML checkpoint includes evidence, decisions, spending and remaining coverage.
Register local exported logs/CSV with repeatable `--evidence workspace/relative/path`;
record workload and measurement-window context alongside them. See the
[campaign and tracking guide](docs/campaigns.html) for pricing, recovery, explicit
continuation after a pause, and the limits of the current Kafka experiment.

## Learn more

The app's **Documentation** section includes searchable HTML guides, flowcharts, tables,
and raw experiment records. README is the only Markdown documentation page; edit other guides directly as HTML.

- [Architecture](docs/architecture.html)
- [Dashboard](docs/dashboard.html)
- [Recovery](docs/recovery.html)
- [Experiments and commands](docs/experiments.html)
- [Status and limitations](docs/status.html)

## Research quality evaluation and self-audit

The [HTML findings and repair log](docs/research-quality.html) records baseline
failures, changes, actual live outcomes, and unresolved limitations. Its source is
`docs/research-quality.json`; per-goal HTML includes decisions, evidence, queries,
evaluations and measured model usage. Source/model text is escaped, never executed.
Each issue retains dated observations, fixes, tests and regressions. Latest run
summaries coexist with an append-only experiment history, including model costs
and coverage where available. A fixed guard is distinguished from improved live
research behavior; unresolved failures remain visible.

```sh
# Scripted tool/harness checks: these do not measure model reasoning.
python scripts/research_eval.py --harness-only --output .omnitrix/eval-harness-NEW
# Paid autonomous discovery: competing clues and a negative control, scored outside the workspace.
python scripts/research_eval.py --live --output .omnitrix/eval-live-NEW --log docs/research-quality.json
# Real Omnitrix source, seeded failing probes, autonomous triage and authorized Codex repair.
python scripts/self_audit.py --allow-code-modifications --output .omnitrix/self-audit-NEW --log docs/research-quality.json
```

Use new output directories. `--live` needs API credentials and API network access.
The self-audit clones the selected committed revision locally, retains every
candidate, and limits worker changes to `omnitrix/research.py`; independent oracles
live outside the candidate. It performs no merge, push or PR publication. Running
the script through an outer sandbox that masks Codex's daemon socket can block
worker shells; keep Codex's own sandbox enabled when using a supported execution route.

Synthetic scoring measures observable research behavior, with explicitly reported
lexical target matching. It is not proof of causal correctness or generalization
to large public repositories. The self-audit probes are human-authored known
failures: successful triage/repair is separate from autonomous issue discovery.
The research evaluator exits 0 only when every checkpoint passes, 1 for behavioral
failures (or no trials), and 2 for blocked/error runs. A completed agent conversation
does not by itself pass the evaluation.
