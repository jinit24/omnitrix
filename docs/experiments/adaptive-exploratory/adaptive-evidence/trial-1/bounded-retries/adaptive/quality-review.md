# Manager acceptance review

Completed: the network job now retries transient timeouts within the configured attempt bound and preserves permanent-error behavior.

Evidence table:
| Requirement | Result | Evidence |
|---|---|---|
| Retry transient timeouts and return the eventual result | PASS | verification artifact d018f206be47498fb26058f8339ada34.log |
| Bound retry attempts and do not retry permanent errors | PASS | verification artifact 94241d1b2d454ba1b1cd5b739d140d02.log |

Mechanism:
```mermaid
flowchart TD
  A[process(call, attempts)] --> B{remaining attempts}
  B --> C[call()]
  C -->|success| D[return result]
  C -->|TimeoutError| E{last allowed attempt?}
  E -->|no| B
  E -->|yes| F[re-raise TimeoutError]
  C -->|other exception| G[propagate immediately]
```
Takeaway: only transient timeout failures are retried; permanent failures are not retried.

Practice/tradeoff: this is the smallest fixture-aligned change in `service.py`, avoiding unrequested policies like backoff or jitter.

Validation/limits: independent local acceptance checks passed against candidate revision 59b427ce497661e7f91826292009ea641e30db71. No production telemetry was in scope, so no live impact claim is made.

Follow-through: if broader behavior is later specified for non-positive `attempts`, add an explicit test and guard; current fixture acceptance is satisfied now.

# Implementation worker evidence

### Outcome
Updated `process` in `service.py:1` to retry only `TimeoutError` up to the `attempts` bound, returning immediately on success and re-raising the final timeout when exhausted.

### Why it works
- **Root cause:** previous implementation called `call()` once with no retry path.
- **Fix mechanism:** loop over bounded attempts; catch only `TimeoutError`; rethrow only on final attempt.
- **Failure semantics preserved:** non-timeout exceptions (e.g., `ValueError`) are never caught, so they fail immediately.

**Before/After example**
- Before: `call` timing out twice then succeeding would fail on first timeout.
- After: same `call` with `attempts=3` succeeds on third invocation and returns result.

### Evidence / Visual
```mermaid
flowchart TD
  A[process(call, attempts)] --> B{attempts loop}
  B --> C[call()]
  C -->|returns| D[return result]
  C -->|TimeoutError| E{last attempt?}
  E -->|no| B
  E -->|yes| F[re-raise TimeoutError]
  C -->|other Exception| G[propagate immediately]
```
Takeaway: only transient timeout path is retried; all other failures preserve one-shot behavior.

### Practice and tradeoffs
Matches fixture contract in `system.md` exactly: retry transient timeouts, cap retries, and avoid retrying permanent errors. Chosen minimal change avoids introducing new policy (e.g., delay/backoff) outside scope.

### Validation and limitations
- Ran both authoritative executable checks; both passed.
- Limitation: behavior for non-positive `attempts` is unspecified by contract and remains an open edge case.

### Follow-through
- **Owner:** independent reviewer/harness.
- **Next action:** run the same two acceptance checks in candidate verification pipeline.
- **Rollback:** revert `service.py` to previous single-call behavior if regressions appear in broader suite.
- **Monitoring:** N/A for non-deployed local artifact; adoption signal is repeatable check pass.

### Gate outcomes
| Gate | Status | Evidence |
|---|---|---|
| Finish line defined | PASS | Objective + two fixture criteria mapped to checks |
| Mechanism explained | PASS | Timeout-only retry loop and propagation behavior documented |
| Evidence readability | PASS | Mermaid flow + concrete before/after case |
| Established practice | PASS | Direct conformance to `system.md` contract |
| Validation | PASS | `check_0.py` and `check_1.py` both passed |
| Follow-through | PASS | Verification/rollback/adoption plan provided |
