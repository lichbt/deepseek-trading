# N6 — integration

VERDICT: PASS — 1315 tests, exit 0. Nothing committed, nothing pushed.

## Cross-node consistency

**Schema.** `defer_action` writes 13 keys; `deferred_drain` reads 8. Every key read
is written — no read-but-never-written key exists. The 5 unread keys (`pos_id`,
`stop_ref`, `side`, `held_units`, `created`) are deliberate: the drain takes the
position identity from `state`, not from the queued snapshot, because a reconcile
can retire a pos_id between the pass and the fill. That precedence was IMPLICIT and
is now written into `defer_action`'s docstring — it is exactly the kind of thing a
later maintainer reads the wrong way round.

**Default is off.** `DEFER_SHUT_MARKET` resolves to `False` with no env var set, so
both the gate and the drain are inert. Rollback is unsetting it, never a revert.

**Node agreement.** N4 and N5 measured the same-broker-day property with two
independent implementations (5-minute stepping vs direct interval arithmetic) and
returned identical AU200 counts: 784 weekdays, 450 open, 334 same-day, 0 later.

## Regression: the existing book is untouched

The 22-sleeve curve was regenerated from the pre-existing pickle after BOTH swap
additions and byte-compared against the run made before any of this work:

```
MD5 (/tmp/m_22.csv)     = 294813bdb3b181c487787ccba3958020
MD5 (/tmp/regress22.csv) = 294813bdb3b181c487787ccba3958020
IDENTICAL
```

total_return 0.3024, sharpe 1.6679, worst_day_close -1.32% — unchanged. Neither
AU200 nor HK33 is in that book, so a changed curve would have meant a leak.

## Final diff

| file | change |
|---|---|
| `fix_runner.py` | +351 — config, `market_shut`, queue helpers, the gate, `deferred_drain`, boot banner |
| `oanda_book_simulator.py` | +27/-1 — two DERIVED swap rates, `SWAP_DERIVED` updated |
| `tests/test_deferred_queue.py` | new, 6 tests |
| `tests/test_deferred_drain.py` | new, 9 tests |
| `tests/test_defer_sim_parity.py` | new, 6 tests |

No other source file touched. No test was weakened or skipped.

## Evidence quality

Every delegated PASS was re-verified here, because `route opencode` echoes only the
`VERDICT:` line and no worker's pytest output reaches this session:
- N3a/N3b/N4 test files re-run locally.
- N4's counts re-derived independently by Opus.
- N3b MUTATION-TESTED: the fail-closed halt check was broken to fail OPEN and
  `test_halt_check_raising_holds_the_queue` failed as it must; source restored
  byte-identical afterwards.

## What is NOT done, and must not be assumed

1. **Nothing is deployed.** `DEFER_SHUT_MARKET` is not set on the pod, and setting
   it requires a real build (Zeabur applies env only on a manifest apply).
2. **The push is a separate decision.** It is a trading action: `interlock on` ->
   confirm 0 pods -> push -> `off`.
3. **AU200 is paper-only.** The sleeve is `paper_trading` locally; the prop book
   has never seen it.
4. **HK33's candidate is NOT cleared for deploy.** It needs an evaluate run against
   a costed backtest, which did not exist until today.
5. **The winter schedule is a projection.** The broker publishes fixed Bucharest
   wall-times whose Sydney mapping drifts across DST, suggesting they re-publish it.
   The mechanism is schedule-driven so it is correct either way, but the 26 Oct date
   is not a promise.
