# Manager acceptance review

Completed: candidate `f5333f2c6ae8a70803d150331c5c2175adcb8720` satisfies the goal in scoped files.

| Requirement | Result | Evidence |
|---|---|---|
| Preserve duplicate keys, order, and missing values | PASS | verification artifact `9c9400c5a6eb49a0812ee537fd7b7e07.log`; code path in `service.py`; baseline research `411a65ebb09b4e3e82e1cc37dbc3281e` |
| One database request per nonempty batch and none for empty input | PASS | verification artifact `7f5d24ddc93c479190cddf41d867d0f1.log`; code path in `service.py`; baseline research `411a65ebb09b4e3e82e1cc37dbc3281e` |

Before/after mechanism:
- Before: nonempty input triggered one `fetch_many([key])` call per key.
- After: empty input returns `[]`; nonempty input performs one batched fetch, then rebuilds results in original key order using mapping lookups.

Text flow diagram:
```text
keys -> empty?
  yes -> return []
  no  -> single batched fetch -> mapping.get(key) for each original key -> ordered output
```

Validation status:
- PASS: exact candidate audited matches verified implementation `bc47d2e877f04d5bac13afc12a1cf067`
- PASS: both required local oracle checks succeeded
- N/A: live deployment/production monitoring not part of this task

Limitations:
- No production telemetry or benchmark in scope; acceptance is based on deterministic local checks.
- Broader `fetch_many` adapter variants outside scoped contract were not audited.

Follow-through:
- Owner: unassigned deployment owner
- Next step if shipping: monitor request count per batch and functional regressions around missing-key handling; rollback by reverting `service.py` if regressions appear.

# Implementation worker evidence

### Outcome
Implemented only the authorized change in `service.py:1`. The prior failure was per-key fetching (`N` DB calls for `N` input keys). The fix now performs zero calls for empty input and exactly one call for nonempty input, while reconstructing output as one element per original key.

### Why this works
Before: `process([1,2,1], fetch)` called `fetch_many([1])`, `fetch_many([2])`, `fetch_many([1])`.
After: it calls `fetch_many([1,2])` once, then returns `[fetched.get(k) for k in keys]` so order/duplicates/missing values are preserved.

### Evidence / visual
```mermaid
flowchart LR
A[keys input] --> B{empty?}
B -- yes --> C[return []]
B -- no --> D[unique keys via dict.fromkeys]
D --> E[one fetch_many(unique_keys)]
E --> F[reconstruct per original key with mapping.get]
F --> G[ordered output with duplicates and None for misses]
```
Takeaway: call-count optimization is isolated to fetch stage; reconstruction stage preserves original semantics.

### Practice and tradeoffs
Aligned with local acceptance contract in `system.md` (single request per nonempty batch; preserve order/dup/missing). Tradeoff: deduplicating keys changes fetch payload from original input multiplicity to first-seen unique keys; this is required by the provided acceptance example (`calls == [[1,2]]`). Alternative (single call with full keys including duplicates) would still reduce calls but fail that contract.

### Validation and limitations
Executed both required oracle checks and both passed. Validation scope is local harness checks only; no production metrics were collected.

### Follow-through
For deployment-context adoption: monitor DB request count per processed batch and error rate around `fetch_many` mapping assumptions. Rollback is straightforward: revert `service.py` to previous implementation if functional regressions appear.

### Gate outcomes
- Finish line: **PASS** (objective met in allowed path)
- Mechanism explanation: **PASS**
- Evidence readability: **PASS** (tests + flow diagram)
- Practice comparison: **PASS** (matches `system.md` contract)
- Validation: **PASS** (both required checks)
- Monitoring/rollback: **PASS** for proposal; **N/A** for live confirmation (not deployed in this task).
