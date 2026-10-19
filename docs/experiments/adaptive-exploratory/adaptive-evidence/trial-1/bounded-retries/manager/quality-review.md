# Implementation worker evidence

### Outcome
Updated `process` in `service.py:1` to use bounded retries for transient timeouts:
- Retries only `TimeoutError` up to `attempts` total invocations.
- Returns immediately on first success.
- Re-raises the final `TimeoutError` when retries are exhausted.
- Preserves failure semantics for permanent errors (e.g., `ValueError`) by not catching them.

### Why it works
Root cause was a single direct call (`return call()`) with unused `attempts`. The new loop consumes `attempts` in control flow and narrows retries to transient timeout failures.

Before: one invocation only.\
After: up to `attempts` invocations, but only for `TimeoutError`.

```text
process(call, attempts)
  -> validate attempts >= 1
  -> for each attempt in [1..attempts]
       -> call()
          -> success: return result
          -> TimeoutError: retry unless last attempt
          -> other Exception: propagate immediately
```

### Evidence / validation
| Criterion | Command evidence | Result |
|---|---|---|
| Retry transient timeouts, return eventual result | `check_0.py` | PASS |
| Bound retries; do not retry permanent errors | `check_1.py` | PASS |

Both required checks passed in this worktree after the change.

### Practice and tradeoffs
- Aligned to local authoritative contract in `system.md:1` (retry transient `TimeoutError`, bounded attempts, no permanent-error retry).
- Alternative considered: catch broad `Exception` and classify; rejected because it risks retrying permanent failures and violating semantics.
- Compatibility tradeoff: explicit `attempts < 1` guard improves correctness clarity but is not contract-specified.

### Follow-through (non-deployed artifact)
No deployment performed (required). Rollout/monitoring in production is N/A for this local patch. Adoption check: run the same two manager checks in CI/candidate verification before merge.

### Gate outcomes
- Finish line: **PASS** (both requirements implemented and validated).
- Mechanism explanation: **PASS**.
- Evidence readability (table + flow): **PASS**.
- Established practice: **PASS** (matched `system.md` contract).
- Validation: **PASS** (executable checks run and passed).
- Follow-through: **N/A** for production monitoring (no deploy in scope); CI acceptance check identified.
