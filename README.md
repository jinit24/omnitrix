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

The dashboard defaults to read-only on loopback port 8765. Authenticated control is opt-in.
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

## Agent quality gates

Every model entry point receives `instructions/QUALITY_GATES.md` plus its own role
prompt through `omnitrix/prompts.py`. The master manager reviews acceptance; the
`AgentBrain` layer maintains evidence and decision quality; discovery maps scope;
research tests hypotheses; evaluation challenges findings; repository workers inspect;
implementation workers explain, validate and prepare operational handoff. Chat reports
these distinctions without turning status questions into new work.

New worker/research/evaluation handoffs include a Markdown `quality_report`: why the
answer works, sourced charts/diagrams or evidence tables, established-practice fit,
validation and limitations, role-appropriate follow-through, and PASS/RETURN/BLOCKED/N/A
gate outcomes. Research uses validation/invalidation triggers; implementation uses
rollout, monitoring and rollback plans. A PR alone never satisfies acceptance.

These are model instructions and review gates, backed by the existing deterministic
source, scope, authorization, revision and executable-check gates. They are not an
automatic semantic grader or a production monitoring service. Old stored handoffs
still load; an absent quality report is unreviewed. New/resumed model invocations load
current prompts; an already-running turn is not retroactively changed. See
[role contracts and review examples](docs/QUALITY_GATES.md).
See the [measured comparison and candidate/check artifacts](docs/experiments/adaptive-comparison.md)
and [execution design, monitoring and rollback](docs/ADAPTIVE_EXECUTION.md).

## Authorize an implementation

A committed Git workspace, explicit authorization and executable checks for every
mandatory criterion are required. Unknown defects/designs use accepted research;
a well-specified bounded change can use the direct contract below. Review
`implementation.json`:

```json
{
  "objective": "Fix the bounded retry contract",
  "basis": "direct",
  "contract": "Retry only the documented transient error up to the supplied positive attempt limit; preserve all executable acceptance contracts.",
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

For research-backed changes, use `"basis": "research"` (the backward-compatible
default) and `"research_job": "ACCEPTED_RESEARCH_JOB_ID"` instead of `contract`.
The supervisor starts a ready direct authorization without a planning round trip,
resumes the native thread for bounded local repairs, independently verifies the
candidate, runs the deterministic audit, and returns once to the manager for the
final evidence review. This route also serves as a bounded, explicitly authorized
implementation experiment; research-only campaigns cannot use it.

Manager decisions receive one current snapshot with complete criteria, recent
history and durable dialogue. Older original reports remain available through
`recall_history`/`recall_artifact`; state snapshots are not replayed as conversation.
Research/evaluation receives the current acceptance criteria and separates blocking
questions from nonblocking uncertainties. Independent read-only investigations can
use `research_batch` with 2..3 plans and an `independence_reason`; all saved scope,
revision, role-run, dollar and cancellation limits still apply.

Every mandatory criterion needs an executable check. `failure_kind: "assumption"`
returns failures to research. External oracles are hashed; `OMNITRIX_CANDIDATE` identifies
the clean candidate worktree. Commands are trusted local processes, not a hostile-code
sandbox; provider credentials are removed. Authorization permits bounded worktree edits,
never deployments, destructive operations, merges or pushes.

Compare the single worker, research-first manager and adaptive route on identical
fixture contracts with `python scripts/benchmark.py --trials 2 --arms single manager
adaptive --output .omnitrix/comparison-NEW`. Add `--live --allow-code-modifications`
for real model usage. Reports separate coordinator, research, coding and verification
timings, model/token metadata and independently scored outcomes. These small fixtures
test orchestration; they cannot establish production or architecture superiority.

## GitHub Pages

Enable **Settings → Pages → GitHub Actions**. The included workflow publishes three recorded real-model experiment runs and HTML docs.
Live manager state stays local; archived results are labeled as snapshots.

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

## UI control

```sh
omnitrix dashboard-user operator --users .omnitrix/auth/users.json
omnitrix --db .omnitrix/state.db dashboard --allow-control \
  --auth-users .omnitrix/auth/users.json --workspace-root .
```

Sign in locally. The UI shows manager liveness, delivery stages, task evidence, approval
changes and the requirement checklist. Create goals inside the configured workspace root;
start their single manager owner separately. Code permission remains explicit.

[Deployment](docs/deployment.html) provides systemd and Caddy templates for a persistent
HTTPS host. No host is configured here; Pages remains a recorded experiment view.

Browser checks: `pip install -e '.[browser]'`, then `python scripts/check_ui.py`.

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
