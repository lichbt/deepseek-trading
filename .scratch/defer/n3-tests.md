# N3 — defer/drain test suite

VERDICT: PASS — 21 new tests, full suite 1315 passed (was 1294), exit 0.

## The first attempt FAILED and this is why

The original single spec (15 cases + build-your-own fake broker) STALLED and was
killed at the 600s hard timeout with nothing written. Classic "spec bundles too
much". No `review` escalation was run: a stall with zero output gives a cold
reviewer nothing to inspect, and the cause was legible from the spec.

Remedy: split in two, and stop making workers rebuild a fake broker that already
existed. The verified harness from N2's smoke test was extracted to
`.scratch/defer/n3-fakes.txt` (74 lines) and pasted verbatim into both specs,
including the two traps that cost real time:
  * `_stop_ok` accepts ONLY `{'ord_status':'0',...}` — a bare string reads as
    FAILURE and triggers a retry, so a naive fake shows 3 calls instead of 2.
  * `_SESSION_CACHE` is per-process and leaks between tests unless cleared.
Both halves then finished in ~4 minutes each.

| | tests | file |
|---|---|---|
| N3a | 6 | `tests/test_deferred_queue.py` |
| N3b | 9 | `tests/test_deferred_drain.py` |
| N4  | 6 | `tests/test_defer_sim_parity.py` |

## Verified by Opus, not taken on trust

`route opencode` echoes only the `VERDICT:` line, so no worker's pytest output ever
reached this session. Every file was re-run here, and the assertions were read
rather than counted.

**Mutation test on the load-bearing clause.** The fail-closed halt check in
`deferred_drain` was deliberately broken to fail OPEN (drain when the guard cannot
be read) and the suite was re-run:

```
FAILED tests/test_deferred_drain.py::test_halt_check_raising_holds_the_queue
1 failed, 8 passed
```

Source restored byte-identical afterwards (`diff -q` clean), suite back to 9 passed.
So the most important test in the file provably bites rather than passing vacuously.

Tests 1 and 2 also assert OPPOSITE queue lengths on the halt path (0 dropped vs 1
held), so neither can be satisfied by a no-op drain.

Test 8 asserts more than a call count: `all(c[0] != 'close' for c in ad.sent)`,
position still in state, intent still queued — i.e. the close leg genuinely never
started when the stop cancel was unconfirmed.

## Nothing found wrong in fix_runner.py

Both workers were instructed to report rather than fix, and neither reported a
defect. No source file was modified by either.
