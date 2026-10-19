# Exploratory run: retained evidence, unequal scope enforcement

This frozen checkpoint (`c674313`) ran two repetitions of three fixtures. The
adaptive route passed 6/6 runs, the research-first route passed 5/6, and the single
worker passed all executable criteria. Retrospective inspection found a stray `=`
file in the trial-1 single-worker retry fixture, so only 5/6 single runs also met
write scope. The research-first route had correctly blocked the same scope mistake.
The original recorded outcomes are retained alongside `post_run_scope_audit`; they
are not silently relabeled as measurements made at runtime.

The final harness applies the same write-scope gate to every arm. Confirmed completed
worker turns may perform a bounded cleanup repair in their existing thread; unknown
turns still require reconciliation. The affected fixture was rerun in paired trials
under those final rules. Use the [final comparison](../adaptive-comparison.md).

[All 18 original runs, stage timings, diagnostics and retrospective audit](adaptive-comparison.json)
include relative links to the independent check logs, candidate patches and quality
reviews in `adaptive-evidence/`. These are exploratory observations, not production
or architecture-superiority evidence.
