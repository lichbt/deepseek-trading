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
| Daily scope, revisited | **Indices now; XAU+XAG after 07 measures the real roll spread** (decided 2026-08-10). Adding the metals to the DAILY set measures +21.60% / SR 1.796 against indices-only's +19.33% / 1.619, with **entries IDENTICAL at 2340** — daily roll-flat closes and reopens at the same bar edge, so it surrenders no exposure and is a pure cost swap. That is why the carry-vs-round-trip ratio IS sufficient here and was not for the weekend rule. Held back only because the metals' headroom is 1.43–1.48× against a STATIC mid-session spread (NAS100 is 17.94×), thinner than the 2.48× that got Arm D rejected: if the real roll spread exceeds ~1.45× the table, the whole +2.27pp inverts. XCU is retired and WTI has no sleeve, so rows including them are unbankable; crypto (+0.51pp more) is the thinnest headroom in the book and rolls 7 days a week |
| Roll spread | **Not measured; 5.44× headroom accepted** at charting. The model charges the static mid-session spread table; break-even is 5.44× that. Arm D (everything) was rejected partly because its margin is only 2.48× |
| Swap rates | MEASURED off `broker_swap` (374 obs), never `pipeline_utils.DAILY_SWAP_RATE` (no WTICO/XAG/XCU entry → charges zero on the biggest payers) |
| Carry symmetry | **No instrument pays positive carry on either side** — `swapLong` and `swapShort` both negative on all 16. Symmetric charging is near-exact, not conservative |
| Published rates | Usable for the RATIO and `swapRollover3Days` only. Absolute values do NOT convert per-unit by lotSize (1.00× for NAS100/XAU/ETH/BTC but 8–50× for FX and XAG) |
| `.t` ruled out | The swap-free listings read `swapLong = swapShort = 0.0`, but the account **cannot trade them** — `TRADING_DISABLED` on a live minimum order. Not an option |
| Hard constraint | A deliberate flat must never be confusable with a stopped-out flat — a stop-out that re-enters on an unchanged signal is the divergence commit `58c1a6f` removed. **Satisfied structurally** (see 01): `fix_runner` holds both in one `st['signal']` field, `FLAT(signal)` vs `FLAT(0)`, so they cannot both be set. The `order_decision`/`stopped_signal` framing in the original charting was `live_test`'s, i.e. the paper book — out of scope here |

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

### Eliminated — do not re-derive

- **A collapsing entry count at higher risk is NOT harness non-monotonicity.** It looked
  like one (hold: 1659 entries at risk 0.005 → 1185 at 0.00675, with the return flipping
  sign). The cause is `halted_total` firing and `run()` breaking out of the loop — those
  runs are **dead accounts**, not bad ones, and report only the bars they survived. Always
  read `bars` and `halted_total` before comparing two risk levels.
- **Widening the weekend set does NOT pay, and the carry-vs-round-trip ratio is why you
  might wrongly think it does** (2026-08-10). Screening on carry ÷ round-trip cost — the
  right denominator, unlike `SELECTIVE_FLAT`'s carry ÷ notional — ranks XAU (4.45x) ABOVE
  XAG (4.28x), which is in the set, and puts five FX pairs over the 2.48x that got Arm D
  rejected. Re-run, it does not survive: shipped +19.33% / SR 1.619, **+XAU +18.30%**
  (gold's weekend exposure is worth more than its carry), +XAU+FX +19.44% (+0.11pp — noise
  on one path, for 781 extra entries), everything +16.90%. The ratio prices the COST side
  exactly (swap falls monotonically −10,888 → −8,574) and is blind to the exposure
  surrendered. **Necessary, not sufficient — never scope a flat rule on it alone.**
  Counter-note: maxDD improves monotonically with scope (−4.94% → −4.22%) while
  worst_day_intraday is flat at ~−1.45%, so wider flattening is a DRAWDOWN trade. Revisit
  only if the book ever becomes DD-binding.
- **The measured swap table is confirmed against the live board** (2026-08-10). Six open
  positions, broker's own swap column vs `SWAP_PER_UNIT_DAY`: −29.38 actual against −29.40
  modelled, **+0.08%**, worst single instrument 0.35%. Independent of the 374-observation
  fit — different week, different positions.
- **The two harnesses do not charge the same round trip** (05). `oanda_book_simulator`
  charges spread on EXITS only and never on an entry; `risk_model_sim` charges the entry
  half-spread plus commission. So **every `--charge-spread` figure on this map is
  optimistic by ~1.5pp of total return** (chosen arm 20.86% → 19.33%; hold 6.14% → 5.03%).
  The ranking is unaffected — the chosen arm takes MORE entries, so charging it properly
  only widens the gap in the direction already chosen. Quote `risk_model_sim` from now on.
- **A pre-roll flatten cannot hurt the guard** (05). `daily_base = max(balance, equity)`,
  and closing collapses `balance` onto `equity`, so the latched base only ever falls. Do
  not re-derive this as a risk.
- **Every arm above risk ~0.00675 blows the 10% total wall** once swap AND spread are
  charged. Hold dies at bar 148 of 675. This is why the chosen arm is sized at 0.005.

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

- [A policy-flat state in order_decision](issues/01-policy-flat-state.md) — **no new state
  needed; the ticket's premise was wrong.** `order_decision`/`stopped_signal` are
  `live_test` (the paper book); `fix_runner` decides on `sig != st['signal']` and that ONE
  field already separates the two flats in opposite directions — `FLAT(st['signal'])` after
  a stop (stays flat) vs `FLAT(0)` after a deliberate close (re-establishes next pass). The
  mutual exclusion is structural: one slot, so both can never be set. **A roll-flat close is
  just: close, write `FLAT(0)`.** Delivered `fix_runner.acts_on_signal()` naming the
  invariant plus `tests/test_roll_flat_state.py` (8 tests) pinning it as one contract;
  suite 1167 green. Re-scoped 02, made 04 likely a no-op, moved instrument scoping to 03.
- [Simulator parity, and what the guard sees when the book is
  flat](issues/05-simulator-and-guard-parity.md) — **both halves clear, and the schedule
  cannot be written in UTC.** `--roll-flat` ported to `scripts/risk_model_sim.py`;
  `--check-baseline` still 2.9e-11 and the swap-only arm reproduces `simulate()` to **0.0
  exactly**. On the INTRADAY measure the chosen arm is −1.44% worst day vs hold's −2.07%,
  **0 days past the halt line and 0 past the wall** for both — the firm's own metric does
  not change the verdict. A pre-roll flatten can only LOWER the latched `daily_base` (it
  collapses `balance` onto `equity`): a losing book into the roll goes from 2.00% to 3.00%
  of daily room, a winning one is unchanged. No guard change needed. Delivered
  `tests/test_risk_model_sim_roll_flat.py` (5 tests); suite **1172 green**.

## Not yet specified

- **Partial-policy state.** If the 20:50 close fills but the 21:05 reopen fails (or vice
  versa), the sleeve is left in a state neither the runner nor the validated return stream
  models. The repair depends on how the close/reopen passes actually behave — unspecifiable
  until 03 and 04 exist. 01 narrows it: a failed close leaves `FLAT(signal)` and the sleeve
  simply carries swap for a night, but a close that fills while the reopen never runs leaves
  `FLAT(0)` — armed to re-enter at an arbitrary later time.
- **Monte Carlo on the chosen arm.** Every figure on this map is ONE historical path. A
  distribution is worth having before sizing changes, but the arm's exact live behaviour
  has to be settled first.
- **True round-trip cost.** The 5.44× headroom was accepted rather than measured. Real
  fills at the roll will reveal the actual spread — this graduates into a real question
  once 07 has data, and could reopen the scope choice.
- **Cron cadence and the host clock.** Sharpened by 05, and the framing here was wrong on
  two counts. (a) **There is no 21:05 trigger** — since 2026-07-28 the pod fires HOURLY at
  `:15` and acts only in the **00:00 UTC hour** (`zeabur_interlock.sh cron-install`),
  because 21:05 UTC sat inside the index session close and every index order was rejected.
  Since a policy close is just `FLAT(0)`, that existing pass is already a working REOPEN,
  so 03 may only need the pre-roll CLOSE. (b) **The times cannot be UTC constants.** The
  roll is 21:00 UTC in summer and 22:00 UTC in winter (server clock = New York + 7h), so a
  fixed 20:50/21:05 pair pays the FULL carry for ~4.5 months a year. Express the schedule
  on the broker clock, as `prop_guard._trading_day` already does.
- **The reopen gap nothing prices.** The simulator models close-and-reopen at the same bar
  edge, i.e. zero lost exposure. Reopening on the 00:15 UTC pass instead leaves ~2h of
  index session unheld. Small, but it is charged at zero today.

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
