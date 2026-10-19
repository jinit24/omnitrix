# Research workflow controls

The manager chooses valuable uncertainties, specialists gather evidence, an
independent evaluator assesses the result, and Python enforces budgets and proof
contracts. These controls address failures observed during the Kubernetes and
Apache HTTP Server trials. Those trials remain historical evidence; their budgets
and incomplete coverage have not been reset or relabeled as successful audits.

| Observed problem | Implemented control | What it establishes |
| --- | --- | --- |
| A huge search space repeatedly narrowed to a few topics | Durable approved batches, priority and aging, area fairness, and an append-only audit agenda | Work and remaining questions survive restarts; accepted samples credit one declared area |
| Missing callers, helpers or final behavior made investigations inconclusive | Typed evidence obligations, file/range preflight, reserved checkpoint calls, and bounded dependency grants | Declared proof is feasible and captured before the report; a reviewer still judges semantic completeness |
| Manager context consumed too much budget | Bounded cards/history, paged retrieval, manager spending/request ceilings, and protected review allowances | Enforced cost allocation and bounded request size; live efficiency gains remain unmeasured |
| Operators repaired history, restarted work, and rejected premature completion | One durable corrective manager attempt, automatic local reconciliation, atomic queue claims, and current-evidence completion fences | Known local failures recover without blind replay or budget/attempt resets |

## Context and proof contracts

Provide a custom brief and exact review criteria using `omnitrix context GOAL_ID
--file research-context.json`, then accept the resulting proposal through the
existing control flow. For new wide audits, use:

```json
{
  "brief": "Audit the whole repository. Prioritize reachable behavior and practical user impact. Track excluded and unexamined areas explicitly.",
  "rubric": [
    "Trace behavior from callers through helpers to the final result",
    "Check relevant tests, contrary evidence and documented intent",
    "Separate static observations from demonstrated defects and measured impact"
  ],
  "require_checkpoint": true,
  "require_evidence_plan": true
}
```

The manager and specialists receive the current approved brief/rubric directly.
Context changes advance the goal revision and invalidate affected work. Mandatory
goal/criteria/context/policy are preserved in model input; excessive mandatory
context produces a recorded limit error rather than silent omission.

A strict plan declares a registered `primary_area`, a decision, expected value and
stop condition, explicit source files, and typed `evidence_obligations`. Each
obligation identifies its kind (`implementation`, `caller`, `callee`, `contract`,
`test`, or `final_outcome`), file, range and required status. Required unresolved
dependencies reject admission. Optional prerequisites need an explicit reason.
Preflight checks source existence, boundaries, current evidence and minimum read
calls plus a mandatory checkpoint allowance. It does not infer a complete call graph.

When a newly discovered prerequisite is relevant, a plan with
`allow_scope_expansion: true` and explicit `dependency_roots` permits
`request_dependency(callsite_evidence_ids, reason, requests)`. Each request names
an actual file and line range. Grants require current captured callsite evidence,
stay within approved roots, and allow at most two grants of two new files each.
Focused work still has at most eight sources. Grants consume the same tool pool
and extend no deadlines, attempts or dollar limits. They become required proof
obligations and are visible to the evaluator.

A report's proof state is saved before evaluation. An evaluator's later reads
cannot repair a missing researcher obligation or mandatory checkpoint. Strict
contexts also require a typed `stop_reason`; incomplete scope and exhausted tools
must not be confused with unavailable access. Checkpoints renew semantic progress
only when they close a new declared obligation. They do not prove correctness or value.

## Durable scheduling and recovery

The `schedule_research` decision accepts one to six typed discovery/research plans.
The entire batch is preflighted before jobs are inserted. Ready queue entries bind
the approved goal, policy, context, plan and source versions. Atomic claims choose
work by priority, aging and prior area claims. Dispatch needs no extra manager call
for each approved worker. Stale ready entries are invalidated rather than executed.

Opening a supervisor reconciles interrupted local SDK research/evaluation without
replaying it. Captured evidence, reports and consumed attempts remain durable;
claimed interrupted work is not silently returned to the queue. External worker or
provider uncertainty retains its fence and conservative dollar charge. The system
does not promise automatic recovery of unknown external effects.

A structured-output error or known broken tool-history error permits one corrective
decision for the current goal/policy/context revision. The repair ticket and outcome
are stored. A failed or interrupted repair pauses; timeouts, request exhaustion and
dollar exhaustion do not start an unbounded corrective loop.

Accepted research records one sample against its declared primary area. Parent and
sibling areas receive no inferred credit. Existing uncertainty, reviewer feedback
and earlier next questions remain in the ledger. A stopped run or empty queue is
not a completed audit; the campaign summary explicitly keeps semantic completeness
false. The separately checked reviewed-coverage floor is a sampling contract.

## Cost and context controls

`Budget.configure` creates an immutable goal cap of at most $25 and explicit model
prices. Its new-goal defaults allocate at most 25% of that cap to manager/chat,
three HTTP requests per manager invocation, a 48 KiB full serialized request, and
2,000 maximum output tokens. The model input uses a 16 KiB dynamic state packet,
4 KiB tool results and 12 KiB retained paired history, with an additional assembled
input guard. Goal, policy and required context are never trimmed to meet these limits.
All limits include tool turns; retries, resumes and scope grants cannot reset them.
Existing budget rows retain their historical policies.

Before research begins, `review_budget_usd` protects an explicit independent-review
allowance (default $1.50) within the same cap. Evaluation requests must fit its
remaining conservative reservation. Unused allowance is released on terminal local
outcomes; charged or uncertain requests remain charged. An allowance never refills
or reopens under the same package ID. This default is a tuning starting point, not
evidence that $1.50 suffices for every review. Changed plans and additional review
packages must still fit the original goal cap and role-run limits.

Complete records remain in SQLite. Bounded manager cards retain unresolved and
contradictory signals and IDs for retrieval; `read_report` and `retrieve_record`
provide bounded pages rather than copying entire histories into every request.

## Verification and limits

```sh
pytest -q
python scripts/workflow_benchmark.py --output work/workflow-benchmark
```

The offline benchmark uses real SQLite, supervisors and research tools with scripted
decisions and specialists. It compares batch dispatch with a per-task strategy arm
and injects scope, preflight, repair, restart, staleness and completion failures.
Results include code/prompt/fixture hashes and durable databases. No provider calls
or external workers are used. A reused output directory is rejected.

The [recorded offline result](experiments/workflow-control-20261019.json) passes all
six scenarios. Its batch arm uses one scripted strategy call versus three in the
per-task arm, with the same three supported fixture contracts. This is a dispatch
measurement with scripted answers, not a live effectiveness comparison.

This verifies enforceable mechanics. It does not establish repository-wide semantic
coverage, live model research accuracy, lower actual spend, or superiority over a
single worker. Those require fresh paired live trials, held-out acceptance oracles,
the same $25 goal cap, and recorded operator interventions. The previous public runs
remain incomplete operator-assisted trials.

Dependency grants pin their admitted file hashes. Initial scoped files are checked
at preflight and queued dispatch; current-evidence gates detect stale citations.
This is not an immutable filesystem for an entire attempt: prevent concurrent edits
when a consistent multi-file source snapshot is required.
