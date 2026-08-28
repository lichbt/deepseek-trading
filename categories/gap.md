<!-- gap.md — single source for the gap generation category. Loaded by auto_research._category_constraint('gap'). Forced slot (i%15==14, ~6%), daily-pinned — NOT i%15==8, which was superseded 2026-08-27 because its only hit inside 1..20 is i=8, always taken by wild. The gap event IS the entry trigger; the filter must be a SEPARATE regime gate. Every number in GUIDANCE was measured 2026-08-27 on OANDA daily bars 2015-01-01..2026-08-25, 31 instruments — re-measure before editing them. -->
# Overnight / weekend gap category

## CONSTRAINT

GAP MODE: the edge is the market's REACTION to a price discontinuity, not the gap's direction. The gap is ALWAYS `gap = df['open'] - df['close'].shift(1)`; `close - open` is the bar's body, not a gap, and is OFF-SPEC. Normalise as `gap_atr = gap / atr14` and name it in entry_condition.

STATE DIRECTION AND MAGNITUDE — "trade the gap" is not implementable and is DISCARDED. GAP-UP and GAP-DOWN must BOTH trade; long-only on a drifting instrument (XAU, BTC, indices) is a beta harvest in costume.

SIZE IS A PERCENTILE OF THE INSTRUMENT'S OWN |gap_atr|, NEVER A FIXED ATR MULTIPLE: median |gap_atr| is 0.02-0.12 and p90 only 0.08-0.37, so `|gap_atr| > 1.5` selects FOUR BARS IN ELEVEN YEARS. Write `gap_atr > gap_atr.rolling(N).quantile(0.8)`; any absolute floor stays under 0.5.

EXECUTION FACT: the gap bar's own open-to-close fill is NOT capturable — entry is at the CLOSE of the signal bar, so a gap seen at bar t's open is entered after that fill and earns bar t+1. "Price returns to the prior close during the gap session" is an unreachable leg and is REJECTED. Exiting AT the prior close is fine — a target for the position you hold.

No unconditional fade or continuation: both measure ~0.02 ATR on the tradeable leg, under round-trip cost, and the SIGN SPLITS BY MECHANISM — a weekend gap on a continuously-traded market CONTINUES, a nightly session gap on a cash index FADES. Name the mechanism, then earn the edge from a stated CONDITION: gap size, whether it is still unfilled at the signal bar's close, the vol regime, or agreement with the trend. SIGNAL STARVATION is how this category fails: the gap event is ALREADY selective (7-56% of bars by instrument, and a percentile cut takes a fifth of that), so pick ONE conditioning axis and keep the filter a BROAD regime state — three selective conditions multiplied together leaves single-digit signals over a decade.

FILTER_CONDITION (mandatory): a regime or liquidity state INDEPENDENT of the gap, never a restatement of its threshold — realized vol vs its 60-bar median, a trend-strength or efficiency-ratio gate, ADX(14) < 20 for range fades, or `close > SMA(200)` for continuation. That last one plus a continuation entry on a drifting instrument is a long-bias trap: the validator REJECTS any strategy long more than 60% of its bars or structurally one-sided. `spread` exists ONLY under archetype "spread"; the frame is date/open/high/low/close and macro columns (rates, yields, CPI, DXY) are NOT available in gap mode — any column you did not request fails at signal-check.

EXIT_CONDITION must reference the gap's level or the prior close, not a bare bar count: return to the prior close (full fill), 50% filled, break beyond the first bar's high/low, or an opposite gap. Compound is fine — "after 5 bars OR when price touches the prior close".

Declare strategy_family "flow-proxy", or "speed-based" if the session boundary is the point. Do NOT write "gap" — that field is a closed set and an unknown value discards the thesis.

Daily bars only, so the boundary is the 21:00/22:00 UTC roll. Compute the gap from OHLC (archetype "standard"); never invent a `gap` column. To gate the weekend bar use `dow == 6` with archetype "calendar", NEVER `dow == 4` or `dow == 0` — the bar is stamped at its OPEN, so those mean something other than what they read. HARD LIMITS: <=4 tunable parameters, <=200 grid combinations, never .rolling(...).apply(). ENTRY and FILTER must be vectorized. EXIT STATE IS ALLOWED — these gap exits need it: use ONE stateful single pass over all bars carrying (in_position, dir), as codegen.md prescribes, with a position opened at t fixed at t and nothing reaching back. Do NOT re-implement the ATR STOP; the validator owns it via compute_returns_with_stop.

## GUIDANCE

## Overnight / weekend gaps — the two facts that decide the design

A **gap** is `open - close.shift(1)`, normalised by ATR. No injected column —
compute it. Two things about it are measured, not folklore (2015–2026, 31
instruments, daily bars):

1. **The gap bar's own open→close fill is UNREACHABLE.** Entry is at the CLOSE of
   the bar that produced the signal, so a gap seen at bar t's open is entered
   after the fill and earns bar t+1. Every "the gap gets filled during the day"
   thesis describes a leg this pipeline cannot trade.
2. **On bar t+1 the sign splits by mechanism, and folklore is backwards.**
   Weekend gaps (one continuous market — FX, metals, energy) CONTINUE: fading
   them returns −0.018 ATR (n=5,434, t=−1.94). Session gaps (cash indices with a
   nightly exchange close) FADE: +0.020 ATR (n=7,310, t=+2.27). Both sit at or
   under round-trip cost, so neither is a strategy unconditionally — the edge has
   to come from a condition on gap size, on whether it is still unfilled at the
   close, on the vol regime, or on trend agreement.

**⚠ DATE-STAMP TRAP — applies to EVERY calendar thesis, not just gaps.** OANDA
stamps a daily bar at its OPEN (21:00/22:00 UTC), so weekday labels sit one
session earlier than they read: `dow == 6` is **Monday's session** (the
weekend-gap bar), `dow == 0` is Tuesday's, and Friday's is `dow == 3`.
`dow == 4` **BARELY EXISTS** — 4 bars of 3,010 on EUR_USD — and `dow == 5` is
empty, so a "Friday effect" written as `dow == 4` fails at IS=0. (Crypto trades
the weekend and is the exception.) Prefer gating on gap SIZE over a weekday: for
FX and metals, "above the 80th percentile" already selects the weekend bar 92%
of the time.

**Why daily only, measured 2023-2026.** Intraday bars gap LESS, not more — the
feed is continuous, so the session discontinuity people picture is already folded
into the daily bar:

| | H1 | H4 | D |
|---|---|---|---|
| % bars gapping >0.1 ATR | 2.8-3.1% | 5.9-7.3% | **8.4-12.5%** |
| median gap in ATR | 0.003-0.009 | 0.002-0.005 | **0.025-0.030** |

On EUR_USD H1, 27% of bars have `open` EXACTLY equal to the previous close. An
intraday "gap" here is ~0.005 ATR, far under spread — there is no London-open gap
in this H1 series to trade. H1/H4 also cannot reach the prop book
(`fix_runner.py:433` skips any timeframe != 'D'), and live steering runs 19 D to
1 H4. Do not re-propose an intraday gap slot without a different data source.

**Prior:** ~1,980 free-form gap theses have been generated for ZERO passes. The
unconditioned gap fade has been tried at scale and does not survive.
