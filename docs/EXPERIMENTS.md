# Experiment protocol and measured limitations

## Final tool-contract run: fixture v2 and GPT-5.4 coordinator

A separate paired run exposed the unchanged executable callback contracts in `system.md`
for both arms, so source-only research could read the same expectations as the coding
worker. Assertions and task objectives were unchanged. The coordinator/research model
was GPT-5.4; Codex remained GPT-5.3-Codex with medium reasoning. The read tool now
advertises its existing 100-line cap in its SDK schema and returns actionable errors.

| Arm | Criteria satisfied | Tasks completed | Total wall time | Observed input tokens | Output tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| Single real Codex worker | 6/6 | 3/3 | 59.0 s | 232,753 | 4,404 |
| Real GPT-5.4 manager/specialists | 4/6 | 2/3 | 176.2 s | 461,690 | 12,328 |

Both deduplication and batching completed through research, authorization, code changes,
independent tests and final audit. Retry work stopped because the independent evaluator
wanted additional edge-case contract evidence; accepted research/authorization was not
granted. That unfinished task remains in the denominator. There were no application
exceptions, false completion claims or recorded human interventions in this run.

The manager did not outperform the baseline: it completed fewer tasks, took about three
times as long, and consumed more observed tokens. This is one small paired trial, not a
production or statistical claim. Code/tool metadata and fixture context changed between
profiles, so this does not establish a causal ranking of coordinator models either.
[Full v2 results](experiments/live-api-v2-gpt54.json) include candidates, source, checks,
manager actions, independent reviews and related failed profiles. Raw artifacts are in
`.omnitrix/live-api-trial-9-gpt54-bounds`.

The model-sensitivity run exposed another concrete API defect: models requested 200–220
lines, but the read cap of 100 was absent from the tool schema, and generic errors were
mistaken for inaccessible sources. The cap/error contract was corrected and tested through
real SDK dispatch. Manager SDK turn exhaustion also now pauses durably instead of escaping
as an uncaught orchestration error. Earlier failed profiles are retained, not rewritten.

## Earlier clean fixture-v1 run (GPT-4.1)

Using the environment `OPENAI_API_KEY`, the corrected system ran one clean paired
trial with `gpt-5.3-codex` workers (medium reasoning) and `gpt-4.1` manager/research
agents. Each arm used the same fixture tree, acceptance checks and 420-second cap.

| Arm | Criteria satisfied | Tasks completed | Total wall time | Observed input tokens | Output tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| Single real Codex worker | 6/6 | 3/3 | 62.6 s | 266,871 | 4,364 |
| Real manager and specialists | 3/6 | 1/3 | 112.4 s | 287,409 | 9,309 |

There were zero incorrect completion claims, zero recorded human interventions in the
clean trial, and no application exceptions. The manager completed deduplication through
research, authorized code, independent tests and final audit. It paused retries while
asking whether the injected callback owned retry behavior, and batching while seeking
callback guarantees irrelevant to preserving order from a returned mapping. The single
worker completed all tasks against the same executable oracles. The unfinished manager
tasks are retained in the result, not discarded or counted as completed.

This trial **does not show an advantage for the manager**: it satisfied fewer criteria
and took longer. Input/output counts are across different models, can include cached
input, and are not equivalent to dollar costs. One small trial supports no statistical
or production-wide conclusion. Multi-hour trials and comparative recovery rates remain
unmeasured. Baseline internal corrections/research are unknown, not assumed zero.

Live validation separately confirmed real SDK start/resume and native steering: a manager
replied in 2.52 seconds while a real worker ran, delivered an approved policy update,
interrupted it, restored conversation and a pending message after reopening, and reconciled
the interrupted native turn. This is a functional check, not a measured fault-rate study.

[Machine-readable live results](experiments/live-api.json) include exact candidates,
candidate source, criterion outcomes, actions, audits and diagnostic-run summaries. Full
local artifacts are under `.omnitrix/live-api-trial-6`. Five earlier exploratory runs
exposed SDK/SQLite thread affinity, scope validation, budget starvation, missing report/
authorization context and scoring defects. Their failures are preserved separately;
one diagnostic steering intervention is recorded. They used evolving code/policy and
must not be pooled as repeated trials of the final implementation.

## Historical harness smoke run

Three generated tasks cover order-preserving deduplication with a complexity bound,
bounded transient retries/permanent errors, and database batching with duplicate/missing
key semantics. Both arms receive identical source/observations and independent executable
acceptance oracles outside their candidate worktrees.

| Arm | Tasks | Criteria satisfied | Gain over initial fixture | Wall time | Model tokens |
| --- | ---: | ---: | ---: | ---: | --- |
| Single scripted worker | 3 | 6/6 | +4 criteria | 0.234 s | Unmeasured; no models |
| Scripted manager/specialists | 3 | 6/6 | +4 criteria | 0.869 s | Unmeasured; no models |

Both had zero incorrect completion claims. The manager had zero repairs in this paired
run; unobservable baseline internal research/corrections are null, not assumed zero.
A separate scripted demo followed discovery → research → independent evaluation →
manager authorization boundary → implementation → audit → completion. Its first
candidate passed correctness and failed complexity; one repair produced a new candidate
passing both criteria. Actual SQLite, Git worktrees and test subprocesses executed.

Nine deterministic fault scenarios passed: killed worker process, SIGKILLed manager,
updated requirements/late stale result, repeated verification failures, misleading
research, lost message acknowledgment, lost Git acknowledgment, external question waiting,
and token/activity without meaningful progress. These exercise recovery invariants;
they are not live Codex recovery rates.

This is **harness validation only**: decisions and patches were scripted. It establishes
no real-model quality, productivity, dollar-cost or architectural advantage. The manager
added local orchestration time. These small realistic contracts are not a production
portfolio. A machine-readable snapshot is in [benchmark-smoke.json](benchmark-smoke.json).
Full local artifacts are under `.omnitrix/benchmark-final` and `.omnitrix/demo-final`;
regenerate rather than rely on those machine-local paths.

## Repeat

```sh
.venv/bin/python scripts/demo.py --output .omnitrix/demo-new
.venv/bin/python scripts/faults.py --output .omnitrix/faults-new
.venv/bin/python scripts/benchmark.py --output .omnitrix/benchmark-new
```

The fault runner preserves pytest's exit status and emits JUnit XML/JSON. The benchmark
emits per-arm/aggregate JSON, exact candidate revisions, initial tree identities,
goal/database IDs, independent criterion logs, measured time, available usage, repeated
queries, repairs and completion-claim checks. Missing measurements are null. Quality is
only the declared criterion fraction/gain; richer business utility is unmeasured.

## Paid multi-hour comparison

Provide `OPENAI_API_KEY` securely for the manager and Codex API provider, writable
session storage, network
access to configured providers, Git and the installed dependencies. Use an isolated
development machine. Explicit authorization permits changes only in generated fixtures;
there is no deploy, merge or push in the experiment.

```sh
export OMNITRIX_MODEL=gpt-4.1
# Optionally set OMNITRIX_CODEX_MODEL to the same available model for both arms.
.venv/bin/python scripts/benchmark.py --live --allow-code-modifications \
  --hours 4 --steps 12 --arm-seconds 1200 --output .omnitrix/live-4h
```

The live arms are a single real Codex worker with the broad goal, and the actual
model-driven manager with discovery/research, evaluation, preauthorized implementation,
verification and repairs. No scripted mock plan is supplied to the live manager.
The harness authorizes implementation only after research is accepted; the experiment
flag is the user's explicit permission, not manager self-authorization.

Use identical fixtures/oracles, model/runtime settings and per-arm wall deadlines.
Initial tree identities are saved and arm order alternates by trial to reduce warm-up
bias. Include coordinator/research costs. Hours repeats whole trials and may overrun to
finish the last trial; each arm has a wall deadline. Independent scoring runs afterward.
A turn completing is not a success claim: explicit structured worker claims are compared
with executable criterion results.

Successful SDK calls report usage; native cumulative tokens are counted once per thread.
Interrupted/provider-failed calls may omit usage: check completeness before comparing
costs. Baseline internal research/corrections are unknown unless instrumented/reported;
do not compare unequal-resolution telemetry as if it were complete. Recovery is measured
in the separate fault report, not invented per-arm. Dollar prices are not inferred.

The manager is steerable through its SQLite inbox while the benchmark runs: use its goal
ID and DB path with `omnitrix --db ... status`, `send`, and `messages`. Record interventions;
do not secretly give an arm extra help. Completed arms are reused on rerun. An incomplete
arm requires inspection of native threads, journal and worktrees; it is never blindly
replayed. Automatic resumption of ambiguous experiment arms is not claimed.

## Not measured and next experiments

A bounded live run was performed, as reported above; no multi-hour run was performed.
The earlier credential-route failures were resolved with environment-key authentication.
Codex shell execution inside this environment required an approved outer execution route
while retaining Codex sandboxing; see RECOVERY.md. Statistical superiority, production
benefit and comparative live recovery rates remain **not established**.

Next: improve requirement-aligned evaluation and unnecessary-pause behavior, then
larger multi-service tasks, held-out oracles,
repeated randomized trials and uncertainty intervals, equal-resolution baseline tool
telemetry, and process-kill/requirement-change faults during live work. Add infrastructure
only after measured contention, throughput or recovery limitations justify it.
