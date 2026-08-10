# Runbook — verify the `.t` swap-free listings before switching the book

**Status: not started.** This is the last thing standing between the measured
`.t` result and a switch.

## Why this exists

Measured 2026-08-10 on the 22-sleeve book, `--venue ctrader`, risk 0.005,
2024-01-01..2026-08-08, swap AND spread charged, guard armed, decay pinned:

| arm | return | Sharpe | maxDD | step 1 | step 2 |
|---|---|---|---|---|---|
| hold, plain listings | +5.03% | 0.391 | -7.47% | never | never |
| selective weekend-flat, no re-entry (today's capability) | +5.88% | 0.757 | -4.08% | never | never |
| selective weekend-flat + Monday re-entry (counterfactual) | +10.65% | 0.935 | -5.55% | day 666 | never |
| **`.t` listings, weekends held normally** | **+30.13%** | **2.019** | **-5.42%** | **day 294** | **day 386** |

Reproduce any row with:

    ./venv/bin/python scripts/risk_model_sim.py --sleeves <pkl> \
        --start 2024-01-01 --end 2026-08-08 --venue ctrader --risk 0.005 \
        --neutralise-decay --charge-swap --charge-spread --guard on \
        --tee-swap-free

`.t` is the only arm that reaches funded, and the only one needing no code
change. **The entire result rests on one unverified assumption: that these
listings are actually swap-free.** No `.t` position has ever been held, so
`broker_swap` has nothing to say about it.

## What is already verified (2026-08-10, live, account 48171893)

1. **The listings exist** — 7 of the account's 132 symbols end in `.t`:
   `EURUSD.t(110) WTI.t(111) XAUUSD.t(112) XAGUSD.t(113) NAS100.t(114)
   BRENT.t(115) DAX40.t(116)`.
2. **Specs match the plain listings** for all five that cover book instruments
   (EURUSD, XAUUSD, XAGUSD, NAS100, DAX40) — identical `min_volume`,
   `step_volume`, `lot_size`, `digits`. So sizing and the min-lot skip do not
   change, which is why the simulated arm could reuse the plain specs.
3. **`WTI.t` is the exception and a trap.** `min_volume` 100 against the plain
   listing's 1, `lot_size` 10000 against 100 — 100x coarser. On a $100k account
   most WTI opens would be SKIPPED by `fix_runner`, so the swap saving would be
   paid for by not trading. WTI is not in the current book; do not reach for it
   first just because it is the biggest swap payer (-4.165%/weekend measured).

## What is NOT verified — the point of this runbook

**That swap is actually zero.** Swap-free listings commonly charge a fixed
administration fee after a grace period of a few nights INSTEAD of daily
rollover. A spread measurement cannot see that, and this book holds daily-trend
positions for long stretches, so a post-grace fee would land squarely on the
+30.13%.

Also unverified: **no order has ever been placed on a `.t` symbol.** The
2026-07-27 rule requires proving a venue end to end — open with a stop attached,
read it back, close by id — before it carries book risk.

## The procedure

One test settles both. Hold a small `.t` position across a weekend and read the
accrual.

**This places a real order on the funded account. It needs an explicit go.**

### 0. Preconditions

- Run it on a **Thursday**, so the position sits through the Friday 21:00
  rollover. OANDA daily bars are stamped with the bar's START and there is no
  Friday- or Saturday-stamped bar, so the Thursday-stamped bar IS the Friday
  session.
- Do NOT touch the interlock or the pod. This is a manual position outside the
  runner's state; `fix_runner` will not manage it and must not be restarted into
  believing it owns it.
- Note the user hand-trades this account, so a stray position is not
  automatically an orphan — record the position id here the moment it opens.

### 1. Baseline the swap log

    ./venv/bin/python scripts/swap_log.py --dry-run

Confirms the session works and shows what is already open. Nothing is written.

### 2. Open the smallest possible `.t` position, stop attached

Use `NAS100.t` (id 114): `min_volume` 1, the same as plain NAS100, and NAS100 is
the book's largest swap payer at $12,263 of the $28,603 hold-arm bill.

Open at minimum volume with a server-side stop attached — the 2026-07-27 rule
requires the stop be proven, not just the fill. Record:

- the `positionId` returned
- the entry price
- that `stopLoss` is non-zero when read back

### 3. Read it back

    ./venv/bin/python scripts/swap_log.py

Appends an observation. Verify a row exists for the new `positionId` with
`swap_usd` = 0.0 (it should be zero at open regardless).

### 4. Let it sit through the weekend

`com.lich.swaplog` samples every 3h on its own, so no action is needed. The
Friday 21:00 UTC rollover is the event being measured.

### 5. Read the accrual on Monday

    ./venv/bin/python scripts/swap_log.py --report

Find the rows for the `.t` `positionId`. The charge is the DELTA between
consecutive observations of the same position — a row is a running total, not a
charge.

**Interpretation:**

- **delta == 0 across the Friday rollover** — swap-free CONFIRMED for the
  observed holding period. Proceed, but see step 7.
- **delta != 0** — NOT swap-free. The +30.13% arm is dead as measured; re-run it
  with the observed rate instead of zero before drawing any conclusion.

### 6. Close by positionId

Close it explicitly by `positionId`, which completes the end-to-end venue proof
(open with stop → read back → close by id). Confirm the account is flat of the
test position and that no standalone stop order is left behind.

### 7. The grace-period question this does NOT answer

A single weekend proves there is no swap in the first few nights. It does NOT
prove there is no **administration fee after N nights**, which is the common
shape of a swap-free listing and the one that would hurt this book most.

Either hold the test position for ~2 weeks and re-read, or accept the residual
risk knowingly and re-check `broker_swap` after the first month of live `.t`
trading. Do not switch and assume.

## After a PASS

Switching means changing `ctrader_symbols.json` to the `.t` ids for the five
covered book instruments (EUR_USD, XAU_USD, XAG_USD, NAS100_USD, DE30_EUR).
Because the specs are identical, nothing else moves — no strategy change, no
sizing change, no re-validation.

It is still a change to what the live book trades, so it goes through the normal
deploy path: `./scripts/zeabur_interlock.sh on` → confirm 0 pods → push → `off`.
**A `git push` to `feature/ctrader-adapter` is a trading action.**

Leave `SPX500_USD` (no `.t` exists), `BTC_USD`/`ETH_USD` (seven-day accrual, no
Friday triple, no `.t` needed) and `WTICO_USD` (the min-volume trap in §3) alone.
