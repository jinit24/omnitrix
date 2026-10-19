# Public repository research experiments

The harness in `scripts/public_research.py` runs the normal manager, typed research
specialists, and independent evaluator against pinned public source snapshots.
It retains one SQLite database and one immutable **$25 total API budget per
repository experiment**. Continuations, model/tool turns, failed requests with
uncertain billing, and process restarts share that budget. No pull requests,
pushes, code modifications, public-repository commands, or test execution are
available to the model in this research-only arm.

The full-repository mode inventories every tracked path, exports all supported
UTF-8 text, and initializes a durable audit agenda for every top-level area. The
manager chooses what to inspect, ranks alternatives, and selects hypotheses.
The harness does not plant a suspected defect.

## Prepare without model calls

Use local clean checkouts at the desired pinned revisions. For example:

```sh
.venv/bin/python scripts/public_research.py prepare kubernetes \
  --repo work/public-repos/kubernetes --output work/public-runs/kubernetes
.venv/bin/python scripts/public_research.py upgrade-full \
  work/public-runs/kubernetes --repo work/public-repos/kubernetes

.venv/bin/python scripts/public_research.py prepare httpd \
  --repo work/public-repos/httpd --output work/public-runs/httpd
.venv/bin/python scripts/public_research.py upgrade-full \
  work/public-runs/httpd --repo work/public-repos/httpd
```

`prepare` retains the original bounded-subsystem fixture for reproducibility;
`upgrade-full` expands the **same goal and budget** to the whole checkout before
live work. Existing directories cannot be silently overwritten, and the upgrade
cannot reset spend. It records accepted policy, requirements, and research-context
revisions. The original bounded copies and old manifest remain available for
provenance; current research should use `sources/full/` and `catalog/full/`.

The snapshot records repository URL, exact commit, hashes, line offsets, and
explicit exclusions for every tracked path. Symlinks, missing files/submodules,
binary/NUL content, invalid UTF-8, and individual lines larger than 128 KiB are
inventory gaps. They are not silently called audited text. Every exported source
chunk and catalog page is at most 128 KiB. Source lines are unchanged; mapping is:

```text
original source line = line read in chunk + original_start_line - 1
```

The compact `MANIFEST.json` and `catalog/full/INDEX.md` describe all top-level
areas. Per-area JSONL catalog pages map exact chunk paths to original files and
line ranges. Manager navigation supports paginated path searches and bounded
literal searches across the exported tree. Navigation results select research
scopes; formal source reads capture citable evidence.

## Run and continue the same experiment

```sh
OMNITRIX_REASONING_EFFORT=low .venv/bin/python scripts/public_research.py run \
  work/public-runs/kubernetes --steps 72 --seconds 5400 --restart-every 2

OMNITRIX_REASONING_EFFORT=low .venv/bin/python scripts/public_research.py resume \
  work/public-runs/kubernetes --steps 24 --seconds 1800 --restart-every 2

.venv/bin/python scripts/public_research.py export work/public-runs/kubernetes
```

The default model is `gpt-6.1-sol`. Its budget prices are explicit conservative
bounds covering the configured context tier and surcharge, not a provider invoice.
The model availability and current pricing must be checked before changing these
bounds. Dollar-budgeted requests reserve their worst-case cost **before** network
transmission and settle against provider token usage. Unknown outcomes retain
their reservation; retries cannot refill the account. Research role count and
wall-clock limits can stop work before the dollar limit.

The harness rejects `delegate`, `follow_up`, `implement`, and `audit` dispatches in
Python. Its runtime cannot launch Codex or shell commands. It does not rely only
on prompt instructions to keep public checkouts unchanged.

Normal restarts occur only after workers have finished. The harness closes the
supervisor and database, reopens persisted state, and compares table counts,
including the manager conversation database. Uncertain active tasks are fenced
by normal supervisor recovery instead of automatically replayed. Explicit
`run`/`resume` activates a paused goal before assigning more work.

Completion requires observed discovery, independent evaluations of at least two
distinct focused hypotheses, and independently evaluated current source sampling
in every registered top-level audit area. Each
frontier area needs a justified priority, next question, and honest state:
triaged, investigating, sampled, or deferred. A deferred area remains a gap.
Sampling requires current cited research evidence from that area; the manager
cannot label an area sampled merely by assigning the status. Completion rechecks
all cited sample sources and exact goal, policy and context revisions. Historical
sample updates remain intact when they become ineligible; `completion-check.json`
records the final gate result and reasons separately from agenda history.

## Durable artifacts

Each experiment directory contains:

| Artifact | Contents |
|---|---|
| `experiment.json` | Goal ID, model, pinned revision, budget, execution state, restarts |
| `state.db` | Decisions, agenda revisions, research jobs/runs, source excerpts, knowledge, budgets, messages |
| `state.db.sessions.db` | Durable manager conversation |
| `source-snapshot.json` | Complete inventory, source hashes, text chunk mappings, exclusions |
| `completion-check.json` | Current coverage eligibility and reasons, separate from historical agenda states |
| `workspace/` | Read-only research inputs, manifests, catalogs, source copies |
| `history/` | Inputs retained from before the whole-repository scope upgrade |
| `events.jsonl` | Incremental preparation, decision, worker, restart, and exception records |
| `export/state.sqlite` | Standalone SQLite backup, checked after reopening |
| `export/sessions.sqlite` | Standalone conversation backup when a session exists |
| `export/tables.json` | Every database table, including full research reports and evaluations |
| `export/knowledge.json` | Knowledge records, citations, opportunities, jobs, query history |
| `export/report.json` | Counts, cost ledger summary, source integrity, limitations |
| `export/artifact-manifest.json` | Export file hashes and sizes |

Exports are refreshed between completed worker batches and on stop or exception.
SQLite's backup API produces standalone backups without depending on a copied
WAL. Exceptions and their tracebacks remain in the event log and experiment
metadata. Export checks compare both copied chunks and the public checkout to
the recorded source hashes.

## What this experiment establishes

It can establish whether management selected useful questions, used contradictory
evidence, avoided repeated work, retained its frontier, survived normal restarts,
and respected its budget and research permissions. Review the actual reports,
source excerpts, rejected hypotheses, and unresolved questions to judge quality.

Inventory coverage is different from semantic code coverage. Triaging the entire
frontier and independently reviewing two hypotheses do not establish a complete
repository audit. Static research and another model's agreement do not prove an
exploitable defect, production impact, executable regression, or candidate fix.
Those require executable validation beyond this static-research experiment. No live outcome or
superiority claim follows merely from a successful harness run.

Provider request and response JSON are retained in `dollar_payloads` before SDK
structured-output parsing, alongside reservation hashes, token usage and response
IDs. This makes malformed output inspectable without reissuing a paid request.
HTTP headers and provider credentials are never stored. Raw payload retention was
added after the initial live parse failure; earlier requests retain their usage
and response IDs, session/tool history and role contexts, but not a raw-response
copy. Encrypted reasoning remains opaque and is only replayed with same-goal
provenance and a conservative prior-output token allowance.

The live Kubernetes fault experiment sends SIGKILL during source research, records
its exact task and captured observations, reconciles local SDK work without paid
calls, and retains the unresolved cost reservation. An exhausted one-attempt plan
stays exhausted. Operator recovery guidance is explicitly recorded; it is not
reported as autonomous manager success.

The initial whole-repository trial exposed premature closure after top-level
triage and two evaluated hypotheses. That completion remains in the event history.
The corrected guard requires independent current source sampling in every
registered top-level area before completion; deferral and triage leave coverage
incomplete. A budget stop produces a partial audit with durable unanswered work.
Even sampling every area is a coverage floor, not proof of semantic correctness.
A completed metadata record no longer prevents resume when approved requirements
have reopened the underlying goal.

A later live restart exposed a truncated history page beginning with tool outputs
whose call records fell outside the SDK's 12-item window. Those provider 400 errors
and reservations remain visible. Manager input normalization now preserves complete
call/output pairs and omits orphaned pairs from model replay, retaining all original
session rows in SQLite. Old state snapshots are also omitted from replay because
fresh durable state is supplied explicitly.
