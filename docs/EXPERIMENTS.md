# Experiment protocol and measured limitations

## Measured harness smoke run

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

Provide secure manager API access, authenticated Codex, writable session storage, network
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

No live multi-hour run was performed here. The manager API key is absent. The previous
real Codex smoke created a session but model access failed with proxy HTTP CONNECT 403;
its initial session-storage write also required permission. There are no live comparative
quality, token-efficiency, intervention-rate or recovery results. Architectural superiority
is **not established**.

Next: paid live end-to-end validation, larger multi-service tasks, held-out oracles,
repeated randomized trials and uncertainty intervals, equal-resolution baseline tool
telemetry, and process-kill/requirement-change faults during live work. Add infrastructure
only after measured contention, throughput or recovery limitations justify it.
