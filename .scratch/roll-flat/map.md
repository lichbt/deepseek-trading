# Stop paying carry — flat at the 21:00 roll

Labels: `wayfinder:map`

## Destination

The **roll-flat policy is live on the prop pod** — index positions closed before the
21:00 UTC rollover and reopened after, plus the selective weekend rule on the remaining
sleeves — and `broker_swap` **proves the carry actually fell** across a real roll.

Done when the pod runs the policy, the book reconciles clean against the broker, and the
measured accrual on the index positions across the next roll is zero (or is explained).

## Notes

**Domain:** prop book execution — `fix_runner.py` / `live_test.py` / `ctrader_exec.py`,
deployed as a Zeabur pod over the cTrader Open API. Read `CLAUDE.md`, the boot-loaded
`DECISIONS.md`, and `.claude/skills/sleeve-ops/references/deploy.md` before any session
that touches the pod.

**This map carries EXECUTION, not just decisions** (overrides Wayfinder's plan-only
default, chosen at charting). `task` tickets build, deploy and verify.

**Skills:** `/grilling` + `/domain-modeling` for any ticket that reopens a design choice.
`sleeve-ops` deploy mode is MANDATORY for the deploy ticket — do not improvise it.

**Scope chosen at charting:** prop pod only (the paper book pays no real swap), indices
only for the daily rule (`NAS100_USD`, `DE30_EUR`, `SPX500_USD`).

**Companion artifact** (the same plan, rendered):
<https://claude.ai/code/artifact/21fb16b5-6f59-4dd3-85d7-63c7c9930ca1>. The map is
canonical; if they disagree, the map wins.

### Settled during charting — do not relitigate

| # | Decision |
|---|---|
| Mechanism | Swap is charged only on a position held THROUGH the roll. The daily bar boundary IS 21:00 UTC, so close before / reopen after replaces a day of carry with one round trip |
| Bar stamping | OANDA daily bars run Sunday–Thursday. The **Thursday-stamped bar IS the Friday session** — verified empirically, there is no Friday- or Saturday-stamped bar |
| Overlap | Daily roll-flat **subsumes** weekend-flat for the instruments it covers. The weekend rule earns its place only on the NON-index sleeves — that is what buys maxDD −6.33% → −4.83% |
| Chosen arm | Indices daily + weekend selective w/ re-entry: **+20.86% · SR 1.736 · maxDD −4.83%** (risk 0.005, ctrader specs, 2024-01-01→2026-08-08, swap AND spread charged, decay pinned) |
| Baseline | Holding as today: +6.14% · SR 0.469 · maxDD −7.40%, paying $28,548 carry |
| Roll spread | **Not measured; 5.44× headroom accepted** at charting. The model charges the static mid-session spread table; break-even is 5.44× that. Arm D (everything) was rejected partly because its margin is only 2.48× |
| Swap rates | MEASURED off `broker_swap` (374 obs), never `pipeline_utils.DAILY_SWAP_RATE` (no WTICO/XAG/XCU entry → charges zero on the biggest payers) |
| Carry symmetry | **No instrument pays positive carry on either side** — `swapLong` and `swapShort` both negative on all 16. Symmetric charging is near-exact, not conservative |
| Published rates | Usable for the RATIO and `swapRollover3Days` only. Absolute values do NOT convert per-unit by lotSize (1.00× for NAS100/XAU/ETH/BTC but 8–50× for FX and XAG) |
| `.t` ruled out | The swap-free listings read `swapLong = swapShort = 0.0`, but the account **cannot trade them** — `TRADING_DISABLED` on a live minimum order. Not an option |
| Hard constraint | `policy_flat` must be **mutually exclusive with `stopped_signal` by construction**. If it ever applies to a stop-out, the sleeve re-enters on an unchanged signal — the divergence commit `58c1a6f` removed |

### Facts this design rests on (measured 2026-08-10)

- Live book carry is **47% of gross** right now: −$29.38 against +$62.51 across 6 open
  positions. NAS100 alone surrenders **38%** of its gross.
- NAS100 measured carry **−35.875/unit/day** = 0.121%/day of notional; published
  `swapLong` −35.75 confirms it to 0.35%.
- `swapRollover3Days = 5` (Friday triple) on everything except BTC/ETH at 0 — reproducing
  the measured 2.98–3.01× and BTC's 1.00×.
- Simulator flags exist and are committed: `--charge-swap`, `--charge-spread`,
  `--roll-flat {all,indices}`, `--weekend-flat`, `--monday-reentry`, `--tee-swap-free`,
  `--neutralise-decay`. All default OFF; default output is byte-identical to before.
- `risk_model_sim --check-baseline` reproduces `simulate()` to 2.9e−11.

### Known hazards

- **21:00 UTC is a documented failure window.** Daily orders at the bar close previously
  hit broker maintenance and returned `MARKET_HALTED` — the root cause of a dormant book,
  fixed only by a pending-entry retry. This policy routes every index round trip through
  that minute.
- **`git push` to `feature/ctrader-adapter` is a TRADING ACTION.** Always
  `zeabur_interlock.sh on` → confirm 0 pods → push → `off`.
- **The guard already resets `prev_target = 0`** on a daily halt, so a halt and a policy
  flatten can both be in flight. They must not fight.

## Decisions so far

<!-- one line per closed ticket -->

_None yet — charted 2026-08-10._

## Not yet specified

- **Partial-policy state.** If the 20:50 close fills but the 21:05 reopen fails (or vice
  versa), the sleeve is left in a state neither the runner nor the validated return stream
  models. The repair depends on how the close/reopen passes actually behave — unspecifiable
  until 03 and 04 exist.
- **Monte Carlo on the chosen arm.** Every figure on this map is ONE historical path. A
  distribution is worth having before sizing changes, but the arm's exact live behaviour
  has to be settled first.
- **True round-trip cost.** The 5.44× headroom was accepted rather than measured. Real
  fills at the roll will reveal the actual spread — this graduates into a real question
  once 07 has data, and could reopen the scope choice.
- **Cron cadence and the host clock.** A new ~20:50 UTC pass has to coexist with the
  existing 21:05 trigger on a host running +08 with a cron that has no `CRON_TZ`. Shape
  depends on how 03 lands.

## Out of scope

- **The `.t` swap-free switch.** Confirmed genuinely swap-free from broker data, but the
  account gets `TRADING_DISABLED` on any `.t` order. Revisit only if The5ers enables
  swap-free on the account — a fresh effort, not a resumption. Runbook preserved at
  `TEE_VERIFICATION.md`.
- **The OANDA paper book.** Chosen at charting: it pays no real swap, so the policy buys
  nothing there. Porting it later is a separate effort.
- **Carry-aware sleeve grading / the NAS100 retire question.** Measured and real — the
  three NAS100 sleeves are gross +$4,055, swap −$12,237, **net −$8,216** against a
  whole-book net of $6,142, and `sleeve_health` grades all three HEALTHY because it charges
  no carry. But that is a **book-composition** decision, not this policy, and the two
  interact: roll-flat removes most of the carry that makes them negative. Separate effort,
  and it should run AFTER this one so it grades against the real cost.
- **Arm D (roll-flat on everything).** Higher raw return (+27.56%) but 2.48× break-even and
  every sleeve through the 21:00 window nightly. Rejected on robustness at charting.
