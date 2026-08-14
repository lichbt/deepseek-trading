#!/usr/bin/env python3
"""Read-only current-book counterfactual using live-like Oanda risk sizing."""
import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import portfolio
import kelly_policy
from data_fetcher import get_candles_date_range
from supplementary_data import inject_supplementary_data
from validator import create_strategy_function

RISK = 0.005
MAX_RISK = 0.02
# Kelly comes from kelly_policy, the same object both live books use. This file
# held a THIRD copy of the constants and formula until 2026-07-31; it was
# behaviourally equivalent but recomputed every 21 bars against the books' every
# bar, and — worse — had no ENABLED switch, so disabling the overlay for live
# trading would have left this simulator still modelling 2x and reporting risk
# figures for a book that was no longer being traded.
KELLY_WINDOW = kelly_policy.ACTIVE_WINDOW
KELLY_MIN_TRADES = kelly_policy.MIN_TRADES
KELLY_RECOMPUTE = kelly_policy.RECOMPUTE_EVERY
DECAY_ENTRIES = 30
DECAY_RECHECK_DAYS = 21

# ---------------------------------------------------------------------------
# SWAP / ROLLOVER (opt-in via --charge-swap; off by default so every existing
# figure this file has produced stays reproducible).
#
# USD charged per UNIT per swap-day, MEASURED on the live The5ers cTrader
# account rather than taken from a published rate card. Derived from the
# broker_swap table as the delta between consecutive observations of the same
# position_id, divided by units (scripts/swap_log.py --report). Span
# 2026-07-31 -> 2026-08-10, 374 observations, 32 positions.
#
# NOT pipeline_utils.DAILY_SWAP_RATE, deliberately: that table is a rough
# rate-card approximation, has no entry for WTICO/XAG/XCU (so get_daily_swap
# returns 0.0 for the single biggest payer in the book's history), and was never
# checked against an accrual. The measured values reproduce the independently
# published per-weekend costs to 1-9% (NAS100 -0.376%/wk measured vs -0.386%
# published, XAU -0.062% vs -0.066%, XAG -0.206% vs -0.225%), which is the
# cross-check that makes them usable.
#
# CHARGED SYMMETRICALLY — both sides pay, and on this broker that is very nearly
# exact rather than a simplification. Read from ProtoOASymbolByIdReq 2026-08-10:
# NOT ONE of the 16 book symbols pays positive carry on either side — swapLong and
# swapShort are both negative everywhere, so there is no carry credit to miss.
# short/long ratios: XAU/DE30/ETH/WTI 1.000, EUR_USD 0.990, NAS100 0.962,
# XAG 0.923, BTC 0.872, AUD 0.837, EUR_GBP 0.806, SPX500 0.571, XCU 0.580 —
# and USD_CHF 1.133, EUR_JPY 1.117, GBP_JPY 1.098 where the SHORT pays MORE.
# So symmetric charging is within 4% for NAS100 and errs in both directions
# elsewhere; it is not a systematic bias toward either arm.
#
# THE PUBLISHED->PER-UNIT CONVERSION IS SOLVED (2026-08-14), and this supersedes the
# note that used to sit here saying it was unresolved and the magnitude had to stay
# measured. That note tried lotSize, which is the wrong divisor. The right one is
# the DECIMAL EXPONENT:
#
#     per unit per day, in the QUOTE currency = swapLong / 10**pipPosition
#
# Checked against every rate below (ProtoOASymbolByIdReq, account 48171893):
# WTICO/NAS100/XAU/XAG all 1.00x, ETH 0.98, EUR_USD 0.98, and the DE30/SPX500
# proxies further down 1.01. Two misses, both explained rather than tolerated:
# BTC reads 0.87 because the -60.0 here equals swapSHORT exactly (it was measured
# off a short), and USD_CHF reads 1.24 because its quote is CHF, so the published
# figure still needs the FX leg that a USD-quoted symbol does not.
# swapCalculationType is 0 on all 12 symbols and discriminates nothing.
#
# ⚠ THE VALIDATED EXPONENTS ARE 0, 2 AND 4 — ALL EVEN. Every symbol that confirmed
# the rule sits at one of those three. The two rates derived from it (SWAP_DERIVED
# below) sit at pipPosition 1 and 5, which NOTHING has validated, and an off-by-one
# in that exponent is a 10x error — the same size as the XCU correction the rule
# produced. NATGAS is the weaker of the two: digits-pipPosition is 0, 1 or 2 on
# every other symbol in the book and 3 on NATGAS alone. Treat both as provisional
# until an accrual confirms them; scripts/swap_log.py --report reconciles observed
# charges against these numbers and marks the derived ones.
#
# Consequence: an instrument with no accrual is no longer un-costable. Prefer a
# measurement when one exists — it is the account's own truth and it caught the
# BTC long/short asymmetry — but a derived rate beats the 0.0 that swap_charge
# returns for an absent key, which is not a small error but a silent one.
SWAP_PER_UNIT_DAY = {
    'AUD_USD':    -0.000082,   # from the Friday triple / 3; no weekday pair yet
    'BTC_USD':   -60.0,
    'ETH_USD':    -4.0,
    'EUR_GBP':    -0.000090,
    'EUR_USD':    -0.000097,
    'NAS100_USD': -35.875,
    'USD_CHF':    -0.000140,
    'WTICO_USD':  -0.70,
    'XAG_USD':    -0.042800,
    'XAU_USD':    -0.890,
    # DERIVED from the published card by the rule above, NOT measured from an
    # accrual — no position in either has ever been held on this account. Both are
    # USD-quoted, so they need no FX leg and belong in this table rather than the
    # percent-of-notional one.
    'NATGAS_USD': -0.005200,   # swapLong -0.052, pipPosition 1. ~63%/yr at $3 —
                               # this instrument was charged ZERO until 2026-08-14.
    'XCU_USD':    -0.0004341,  # swapLong -43.41, pipPosition 5. REPLACES an
                               # XAG-derived proxy that was 10.4x too high
                               # (-0.00452/unit/day, i.e. 25%/yr against a real
                               # 2.4%/yr), on an instrument live on the weekend leg.
}

# Rates that came off the published card rather than an accrual. One source of
# truth for "provisional": scripts/swap_log.py --report marks these so an observed
# charge that contradicts one is visible instead of averaging away.
SWAP_DERIVED = {'NATGAS_USD', 'XCU_USD'}

# Instruments in the book with NO measured accrual, priced as a fraction of
# notional per day and converted at the bar close. Indices are anchored on the
# MEASURED NAS100 rate scaled by the published relative (DE30 0.071%/wk and
# SPX500 0.058%/wk against NAS100's 0.386%/wk); FX uses the mean of the four
# measured FX pairs; XCU uses XAG as the nearest measured metal. WHEAT has
# neither a measurement nor a published figure and is charged at the FX mean as
# a placeholder — it is one sleeve and its own sensitivity is reported.
SWAP_PCT_NOTIONAL_DAY = {
    'DE30_EUR':   -0.0002306,   # 0.184x the measured NAS100 0.1254%/day
    'SPX500_USD': -0.0001881,   # 0.150x
    'EUR_JPY':    -0.000120, 'GBP_JPY': -0.000120, 'GBP_USD': -0.000120,
    # XCU_USD used to sit here at -0.000687 as an "XAG proxy". Removed 2026-08-14:
    # the broker's own card puts it 10.4x lower, and it is now a per-unit entry above.
    'WHEAT_USD':  -0.000120,    # placeholder, no source — and unroutable, so inert
}

# Accrue every calendar day and take NO Friday multiple (swapRollover3Days=0),
# measured: BTC charged an identical -0.60 across Friday and non-Friday windows.
SWAP_SEVEN_DAY_NO_TRIPLE = {'BTC_USD', 'ETH_USD'}
# Accrue seven days AND take the Friday triple on top — ~5 days per weekend.
# Measured on WTI: -1.40/unit on both Saturday and Sunday plus a -4.20 Friday.
SWAP_SEVEN_DAY_PLUS_TRIPLE = {'WTICO_USD'}

# Weekend-flat scoped to the instruments whose weekend carry is material.
# >= 0.15% of notional per weekend, measured: WTI 4.165%, ETH 0.429%, NAS100
# 0.376%, XAG 0.206%, BTC 0.186%. FX is 0.025-0.052% and is held.
SELECTIVE_FLAT = {'WTICO_USD', 'NAS100_USD', 'XAG_USD', 'BTC_USD', 'ETH_USD'}

# Cash-index CFDs — the instruments that pay daily financing on full notional and
# whose session boundary IS the 21:00 roll.
INDICES = {'NAS100_USD', 'DE30_EUR', 'SPX500_USD', 'US30_USD', 'JP225_USD',
           'AU200_AUD', 'HK33_HKD', 'UK100_GBP', 'CN50_USD'}


def roll_flat_scope(spec):
    """Parse a --roll-flat scope into a predicate-ready set, or None for 'all'.

    Accepts 'off', 'all', 'indices', or a comma-separated list whose tokens are
    instrument ids and/or the literal 'indices' (which expands to INDICES). So
    'indices,XAU_USD' is the index set plus gold. 'indices' alone is byte-identical
    to the old behaviour, which every prior roll-flat figure was produced under.

    WHY A SET AND NOT A FLAG. Roll-flat pays a full round trip in place of one
    day's carry, and both legs are LINEAR in units — so whether it wins is a
    per-instrument ratio (carry/day ÷ round trip), not a book-wide policy. Measured
    2026-08-11 on the broker's real commission card: NAS100 17.94x, DE30 2.78x,
    XAU 2.43x, SPX500 1.44x, XAG 1.39x, XCU 1.28x, and BELOW ONE for ETH (0.85x),
    BTC (0.65x) and every FX pair — for those the round trip costs MORE than the
    swap it avoids, so applying it there is a loss, not a saving.
    """
    if spec in (None, "off"):
        return set()
    if spec == "all":
        return None                      # None means "every instrument"
    out = set()
    for tok in str(spec).split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok == "indices":
            out |= INDICES
        else:
            out.add(tok)
    return out


def weekend_flat_scope(spec):
    """Same grammar as roll_flat_scope, with 'selective' expanding to SELECTIVE_FLAT.

    'off' | 'all' | 'selective' | comma-separated instruments (and/or the literal
    'selective'). 'selective' alone reproduces the old behaviour exactly.

    NOTE THE COST HERE IS NOT THE ROUND TRIP. Weekend-flat surrenders the position
    until the signal genuinely CHANGES — order_decision only fires on a change, so
    there is no Monday re-entry. Its economics are therefore dominated by FOREGONE
    EDGE, not by the spread, and it cannot be screened with a carry/round-trip
    ratio the way roll-flat can. It has to be simulated.
    """
    if spec in (None, "off"):
        return set()
    if spec == "all":
        return None
    out = set()
    for tok in str(spec).split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok == "selective":
            out |= SELECTIVE_FLAT
        else:
            out.add(tok)
    return out


def _weekend_flat_applies(spec, instrument):
    scope = spec if isinstance(spec, (set, frozenset)) or spec is None \
        else weekend_flat_scope(spec)
    return scope is None or instrument in scope


def _roll_flat_applies(spec, instrument):
    scope = spec if isinstance(spec, (set, frozenset)) or spec is None \
        else roll_flat_scope(spec)
    return scope is None or instrument in scope


def _half_spread(instrument, units, price, quote_to_usd):
    """Cost in USD of ONE side of a round trip, using the repo's own spread model.

    pipeline_utils.apply_costs is the convention every validated return stream is
    scored under: HALF the spread on an entry from flat, HALF on an exit to flat,
    so a reversal pays a full spread and a flatten-plus-re-entry pays one too.
    Reusing get_spread_pips/get_pip_value rather than re-deriving them is
    deliberate — a hand-rolled conversion once charged ETH a 15% spread.
    """
    from pipeline_utils import get_pip_value, get_spread_pips
    return abs(units) * get_spread_pips(instrument) * get_pip_value(instrument) \
        * 0.5 * quote_to_usd


# USD per SIDE. From the broker's own card (ProtoOASymbolByIdReq, 2026-08-11),
# saved at .scratch/costed/card.json. Two kinds:
#   'per_base_unit' -> USD per 1 base unit  (type 2, price-independent)
#   'pct_notional'  -> fraction of USD notional (type 3 / type 1)
#
# commissionType semantics, with the raw `commission` field scaled by 100 so
# raw/100 is USD:
#   type 2 USD_PER_LOT     -> raw/100 USD per lot, lot = lot_size/100 base units
#   type 3 PCT_OF_VALUE    -> raw/100 USD per $100,000 of USD notional
#   type 1 USD_PER_MILLION -> raw/100 USD per $1,000,000 of USD notional
# Both type-3 rows were solved against real fills and agree to 2%:
#   XAG raw 100, 50 oz @ ~$62.5 = $3,125 notional -> $0.031 (broker showed 0.03)
#   BTC raw 3000, 0.01 BTC @ ~$64,700 = $647 notional -> $0.194 (broker showed 0.19)
# FX type-2 is exact on three fills: 0.05 lot -> $0.10, 0.02 -> $0.04, 0.14 -> $0.28.
# minCommission is 0 on every symbol, so there is NO per-order floor to model.
# WHEAT_USD is a live sleeve but is NOT on the broker's card (absent from
# ctrader_symbols.json) — it appears in the UNCOSTED line at report time.
CTRADER_COMMISSION = {
    # FX, type 2, $2 per 100k base units per side
    'AUD_USD': ('per_base_unit', 2e-5), 'EUR_GBP': ('per_base_unit', 2e-5),
    'EUR_JPY': ('per_base_unit', 2e-5), 'EUR_USD': ('per_base_unit', 2e-5),
    'GBP_JPY': ('per_base_unit', 2e-5), 'GBP_USD': ('per_base_unit', 2e-5),
    'USD_CHF': ('per_base_unit', 2e-5), 'USD_JPY': ('per_base_unit', 2e-5),
    # metals, type 3, $1 per 100k notional per side
    'XAU_USD': ('pct_notional', 1e-5), 'XAG_USD': ('pct_notional', 1e-5),
    'XCU_USD': ('pct_notional', 1e-5), 'XPT_USD': ('pct_notional', 1e-5),
    'XPD_USD': ('pct_notional', 1e-5),
    # crypto AND energy, type 3, $30 per 100k notional per side
    'BTC_USD': ('pct_notional', 3e-4), 'ETH_USD': ('pct_notional', 3e-4),
    'WTICO_USD': ('pct_notional', 3e-4), 'NATGAS_USD': ('pct_notional', 3e-4),
    # cash indices, commission-free (raw 0)
    'NAS100_USD': ('pct_notional', 0.0), 'DE30_EUR': ('pct_notional', 0.0),
    'SPX500_USD': ('pct_notional', 0.0), 'AU200_AUD': ('pct_notional', 0.0),
    'HK33_HKD': ('pct_notional', 0.0),
}


def _commission(instrument, units, price=1.0, quote_to_usd=1.0,
                venue="oanda", sides=1):
    """Per-SIDE commission in USD. `sides` counts the sides transacted (1 for an
    entry or an exit, 2 for an in-bar roll-flat round trip).

    `venue == "oanda"` returns the OANDA rate card unchanged: abs(units) *
    get_commission(instrument). price/quote_to_usd/sides are ignored — the OANDA
    path must not move.

    `venue == "ctrader"` returns the cTrader broker card (CTRADER_COMMISSION):
      per_base_unit -> abs(units) * rate * sides
      pct_notional  -> abs(units) * price * quote_to_usd * rate * sides
    Instruments absent from the table return 0.0; the reporting layer names them
    so an uncosted sleeve is visible rather than silently free.
    """
    if venue == "oanda":
        from pipeline_utils import get_commission
        return abs(units) * get_commission(instrument)
    entry = CTRADER_COMMISSION.get(instrument)
    if entry is None:
        return 0.0
    kind, rate = entry
    if kind == "per_base_unit":
        return abs(units) * rate * sides
    return abs(units) * price * quote_to_usd * rate * sides


# ⚠ NOT EXECUTABLE — DO NOT PLAN AROUND THIS ARM. The seven .t listings are visible
# on the account and size identically, but an ORDER ON A .t SYMBOL IS REJECTED
# (user-confirmed 2026-08-11, already tested — do not re-test). So --tee-swap-free is
# a COUNTERFACTUAL like --monday-reentry: it prices what swap-free WOULD be worth, it
# does not describe anything reachable. The reachable substitute is the carry policy
# (roll_flat / weekend_flat scopes) plus modest scaling.
#
# The seven listings, for reference only: BRENT.t(115) DAX40.t(116) EURUSD.t(110)
# NAS100.t(114) WTI.t(111) XAGUSD.t(113) XAUUSD.t(112). Mapped to the book's
# instrument ids. SPX500 has no .t, and BTC/ETH would need none (they trade seven
# days and take no Friday triple).
TEE_SWAP_FREE = {'BCO_USD', 'DE30_EUR', 'EUR_USD', 'NAS100_USD', 'WTICO_USD',
                 'XAG_USD', 'XAU_USD'}


def swap_charge(instrument, units, price, quote_to_usd, gap_days, crosses_rollover,
                tee_swap_free=False, roll_flat="off"):
    """USD cost of carrying `units` from this bar's close to the next bar's.

    `gap_days` is the CALENDAR gap to the sleeve's own next bar, which is what
    makes the arithmetic come out without a weekday calendar: an ordinary
    instrument is charged on weekdays only but takes a 3x Friday roll, and the
    triple exactly compensates the two uncharged weekend days — so charge-days
    equals gap-days on every bar. Only the seven-day instruments deviate.
    """
    if tee_swap_free and instrument in TEE_SWAP_FREE:
        return 0.0
    rate = SWAP_PER_UNIT_DAY.get(instrument)
    if rate is not None:
        # Measured rates come off broker_swap.swap_usd, already in the ACCOUNT
        # currency — converting again would double-apply the FX leg.
        fx = 1.0
    else:
        pct = SWAP_PCT_NOTIONAL_DAY.get(instrument)
        if pct is None:
            return 0.0
        rate = pct * price      # quote currency per unit
        fx = quote_to_usd
    days = gap_days
    if crosses_rollover and instrument in SWAP_SEVEN_DAY_PLUS_TRIPLE:
        days += 2
    return units * rate * days * fx


def atr(data, window):
    tr = pd.concat([
        data.high - data.low,
        (data.high - data.close.shift(1)).abs(),
        (data.low - data.close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window).mean()


def kelly_multiplier(active_returns):
    """Delegates to kelly_policy so this simulator, fix_runner and live_test cannot
    report different sizing for the same book.

    NOTE the input is deliberately NOT the books' reconstruction. `active_returns`
    holds the REALISED in-position return per bar, computed to the exit price when
    a stop triggers, so a stopped bar contributes the stop loss rather than the
    full close-to-close move. That is the more faithful series and is kept — the
    duplication being removed here is the FORMULA, not the input."""
    return kelly_policy.kelly_multiplier(active_returns)


_INSTRUMENT_SIZING = {
    "BTC_USD": (0.001, 1.0, 3), "ETH_USD": (0.001, 10.0, 3),
    "LTC_USD": (0.1, 100.0, 1), "WTICO_USD": (1.0, 5000.0, 0),
}
_DEFAULT_SIZING = (1.0, 50000.0, 0)

# VENUE MATTERS, AND IT IS NOT A DETAIL. The table above is OANDA's, which is
# correct for the paper book — but the PROP book executes on cTrader, whose
# minimums differ by up to three orders of magnitude in BOTH directions:
# indices are 0.01 there against 1.0 here (100x FINER), while every FX pair is
# 1000 against 1.0 (1000x COARSER). Because _clamp_units FLOORS UP to the
# minimum, scoring the prop book with the OANDA table fabricates index positions
# ~100x too large. Measured 2026-08-04 at $100k, RISK 0.005: OANDA specs report
# worst day -2.39% / maxDD -5.49%, cTrader specs -1.51% / -3.86%, i.e. the
# sanctioned montecarlo path was reporting the live prop book as roughly twice
# as risky (and twice as profitable) as it is. At $10k the error was far worse.
# So: any PROP risk number must be run with --venue ctrader.
_CT_SPEC = None


def _ct_spec():
    """{instrument: (min_units, step_units)} from the venue's own symbol dump.

    Wire volume is centi-units, exactly as fix_runner._CT_VOL_SPEC reads it.
    Loaded lazily so the OANDA path never needs the file to exist."""
    global _CT_SPEC
    if _CT_SPEC is None:
        path = Path(__file__).with_name("ctrader_symbols.json")
        data = json.loads(path.read_text())["instruments"]
        _CT_SPEC = {k: (v["min_volume"] / 100.0, v["step_volume"] / 100.0)
                    for k, v in data.items()}
    return _CT_SPEC


def _sizing(instrument):
    return _INSTRUMENT_SIZING.get(instrument, _DEFAULT_SIZING)


def _clamp_units(units, instrument, venue="oanda"):
    if venue == "ctrader":
        spec = _ct_spec()
        if instrument not in spec:
            return 0.0            # not offered on cTrader -> the sleeve cannot trade
        minimum, step = spec[instrument]
        # Mirrors fix_runner.round_vol exactly: round to the step, then floor to
        # the minimum. Deliberately NOT the OANDA precision-truncation above.
        return max(round(float(units) / step) * step, minimum)
    minimum, maximum, precision = _sizing(instrument)
    units = min(max(float(units), minimum), maximum)
    scale = 10 ** precision
    return np.floor(units * scale) / scale


def min_lot_implied_risk(instrument, stop_mult, atr_value, equity, quote_to_usd=1.0):
    """Risk fraction implied by ONE minimum lot — the quantity fix_runner gates on."""
    spec = _ct_spec()
    if instrument not in spec:
        return float("inf")
    minimum = spec[instrument][0]
    return minimum * stop_mult * atr_value * quote_to_usd / max(equity, 1e-9)


def risk_units(equity, atr_value, stop_mult, weight_scale, corr_scale, kelly, decay,
               risk=RISK, max_risk=MAX_RISK, quote_to_usd=1.0, instrument=None,
               venue="oanda", skip_min_lot=False):
    if not np.isfinite(atr_value) or atr_value <= 0 or quote_to_usd <= 0:
        return 0.0
    fraction = min(risk * weight_scale * corr_scale * kelly * decay, max_risk)
    units = equity * fraction / (stop_mult * atr_value * quote_to_usd)
    if instrument is None:
        return units
    # THE TWO BOOKS BEHAVE DIFFERENTLY AT THE FLOOR and it is easy to model the
    # wrong one. live_test FLOORS to the minimum (np.clip) and therefore always
    # trades; fix_runner REFUSES the open when the minimum lot alone implies more
    # than MAXRISK. Modelling the prop book with floor semantics silently trades
    # sleeves the live runner declines every single pass.
    if skip_min_lot and venue == "ctrader":
        if min_lot_implied_risk(instrument, stop_mult, atr_value, equity, quote_to_usd) > max_risk:
            return 0.0
    return _clamp_units(units, instrument, venue)


def _quote_to_usd_pair(instrument):
    quote = instrument.split("_", 1)[-1] if "_" in instrument else "USD"
    if quote == "USD":
        return None, False
    direct = {"EUR": "EUR_USD", "GBP": "GBP_USD", "AUD": "AUD_USD", "NZD": "NZD_USD"}
    inverse = {"JPY": "USD_JPY", "CHF": "USD_CHF", "CAD": "USD_CAD", "HKD": "USD_HKD", "SGD": "USD_SGD"}
    if quote in direct:
        return direct[quote], False
    if quote in inverse:
        return inverse[quote], True
    return None, False


def _add_quote_to_usd(data, instrument, start, end, granularity):
    pair, inverse = _quote_to_usd_pair(instrument)
    if not pair:
        data["quote_to_usd"] = 1.0
        return data
    fx = get_candles_date_range(pair, start, end, granularity=granularity)[["date", "close"]].copy()
    fx["date"] = pd.to_datetime(fx["date"])
    fx["quote_to_usd"] = 1.0 / fx["close"] if inverse else fx["close"]
    return data.merge(fx[["date", "quote_to_usd"]], on="date", how="left").assign(
        quote_to_usd=lambda x: x.quote_to_usd.ffill().bfill())


def decay_multiplier(closed_returns, current, last_check, current_scale):
    if last_check is not None and current < last_check + pd.Timedelta(days=DECAY_RECHECK_DAYS):
        return current_scale, last_check
    if len(closed_returns) < DECAY_ENTRIES:
        return 1.0, current
    recent = closed_returns[-DECAY_ENTRIES:]
    return (0.5 if np.mean(recent) <= 0 else 1.0), current


@dataclass
class Sleeve:
    sid: str
    instrument: str
    frame: pd.DataFrame
    signal: pd.Series
    atr: pd.Series
    weight_scale: float
    peers: set
    stop_mult: float
    quote_to_usd: pd.Series = None
    units: float = 0.0
    direction: int = 0
    prev_target: object = None
    entry: float = 0.0
    stop: float = 0.0
    active_returns: list = field(default_factory=list)
    closed_returns: list = field(default_factory=list)
    kelly: float = 0.5
    decay: float = 1.0
    last_decay_check: object = None
    pnl: float = 0.0
    # sleeve.pnl accumulates over the WARMUP too (only `equity` is gated by
    # in_evaluation), so it is not comparable with swap_paid/spread_paid, which
    # are. pnl_eval is the in-window figure — the one to use for attribution.
    pnl_eval: float = 0.0
    swap_paid: float = 0.0
    spread_paid: float = 0.0
    comm_paid: float = 0.0
    bars_held: int = 0
    bars_seen: int = 0
    entries: int = 0
    kelly_checks: int = 0
    decay_events: int = 0
    # Last observed close / quote rate, carried so book exposure can be marked on
    # a bar this sleeve did not trade (instruments do not share a calendar).
    mark: float = 0.0
    markq: float = 1.0


def _state(path):
    raw = json.loads(Path(path).read_text())
    weights = raw.get("weights", {})
    n = raw.get("n_strategies", len(weights))
    peers = {}
    for pair in raw.get("correlated_pairs", []):
        peers.setdefault(pair["a"], set()).add(pair["b"])
        peers.setdefault(pair["b"], set()).add(pair["a"])
    return weights, n, peers


def load_sleeves(start, end, warmup_days, state_path, allow_partial=False):
    weights, n, peer_map = _state(state_path)
    rows = {r["id"]: r for r in portfolio.load_strategies()}
    missing = set(weights) - set(rows)
    if missing and not allow_partial:
        raise RuntimeError(f"state sleeves missing from paper_trading DB: {sorted(missing)}")
    fetch_start = (pd.Timestamp(start) - pd.Timedelta(days=warmup_days)).strftime("%Y-%m-%d")
    latest_closed_day = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    fetch_end = min(end, latest_closed_day)
    sleeves = []
    for sid, weight in weights.items():
        row = rows.get(sid)
        if row is None:
            continue
        try:
            inst = row.get("instrument") or portfolio._infer_instrument(sid)
            tf = row.get("timeframe") or "D"
            data = get_candles_date_range(inst, fetch_start, fetch_end, granularity=tf).reset_index(drop=True)
            data["date"] = pd.to_datetime(data["date"])
            archetype = portfolio._infer_archetype(row["code"], row.get("archetype") or "standard")
            if archetype != "standard":
                data = inject_supplementary_data(data, archetype, inst, row.get("instrument2"), fetch_start, fetch_end, tf)
            data = _add_quote_to_usd(data, inst, fetch_start, fetch_end, tf)
            params = json.loads(row.get("best_params") or "{}")
            signal = pd.Series(np.asarray(create_strategy_function(row["code"])(data, params)), index=data.index).fillna(0).astype(int)
            if len(signal) != len(data):
                raise ValueError("signal length mismatch")
            sleeves.append(Sleeve(
                sid=sid, instrument=inst, frame=data, signal=signal,
                atr=atr(data, params.get("atr_window", 14)),
                weight_scale=min(float(weight) * n, 3.0), peers=peer_map.get(sid, set()),
                stop_mult=float(params.get("stop_mult", 2.0)),
                quote_to_usd=data["quote_to_usd"]))
        except Exception as exc:
            if not allow_partial:
                raise RuntimeError(f"{sid} reconstruction failed: {exc}") from exc
            print(f"SKIP {sid}: {exc}")
    if not sleeves:
        raise RuntimeError("no sleeves reconstructed")
    return sleeves


def simulate(sleeves, start, end, initial_equity=100000, risk=RISK, max_risk=MAX_RISK,
             venue="oanda", skip_min_lot=False, charge_swap=False, weekend_flat="off",
             neutralise_decay=False, monday_reentry=False, charge_spread=False,
             tee_swap_free=False, roll_flat="off"):
    timestamps = sorted(set().union(*(set(s.frame.date) for s in sleeves)))
    equity = float(initial_equity)
    daily = []
    for ts in timestamps:
        if ts > pd.Timestamp(end):
            continue
        in_evaluation = ts >= pd.Timestamp(start)
        bar_sleeves = [s for s in sleeves if ts in set(s.frame.date)]
        pre_equity = equity
        pnl = 0.0
        directions = {s.sid: s.direction for s in sleeves}
        for sleeve in sorted(bar_sleeves, key=lambda s: s.sid):
            i = int(sleeve.frame.index[sleeve.frame.date == ts][0])
            if i == 0:
                continue
            row, prev = sleeve.frame.iloc[i], sleeve.frame.iloc[i - 1]
            sleeve.mark = float(row.close)
            sleeve.markq = (float(sleeve.quote_to_usd.iloc[i])
                            if sleeve.quote_to_usd is not None else 1.0)
            if sleeve.direction:
                exit_price = row.close
                if sleeve.direction > 0 and row.low <= sleeve.stop:
                    exit_price = sleeve.stop
                elif sleeve.direction < 0 and row.high >= sleeve.stop:
                    exit_price = sleeve.stop
                quote_to_usd = float(sleeve.quote_to_usd.iloc[i]) if sleeve.quote_to_usd is not None else 1.0
                move = sleeve.direction * (exit_price - prev.close) * sleeve.units * quote_to_usd
                pnl += move
                sleeve.pnl += move
                if in_evaluation:
                    sleeve.pnl_eval += move
                active_return = sleeve.direction * (exit_price - prev.close) / prev.close
                sleeve.active_returns.append(active_return)
                if exit_price == sleeve.stop:
                    sleeve.closed_returns.append(sleeve.direction * (exit_price - sleeve.entry) / sleeve.entry)
                    if charge_spread:
                        sp = -_half_spread(sleeve.instrument, sleeve.units,
                                           exit_price, quote_to_usd)
                        cm = -_commission(sleeve.instrument, sleeve.units,
                                          exit_price, quote_to_usd, venue, 1)
                        c = sp + cm
                        pnl += c; sleeve.pnl += c
                        if in_evaluation:
                            sleeve.spread_paid += sp; sleeve.comm_paid += cm
                            sleeve.pnl_eval += c
                    sleeve.units = sleeve.direction = 0
            if i % KELLY_RECOMPUTE == 0:
                sleeve.kelly = kelly_multiplier(sleeve.active_returns)
                sleeve.kelly_checks += 1
            if neutralise_decay:
                # decay_multiplier reads this simulator's OWN closed_returns, so a
                # policy that changes which trades close feeds back into its own
                # sizing — a loop that does NOT exist live, where decay reaches the
                # pod as a value baked into portfolio_state.json by portfolio.py's
                # OANDA reconstruction. Any overlay comparison must pin it or it
                # scores the feedback rather than the overlay.
                sleeve.decay = 1.0
            else:
                sleeve.decay, sleeve.last_decay_check = decay_multiplier(
                    sleeve.closed_returns, ts, sleeve.last_decay_check, sleeve.decay)
            target = int(sleeve.signal.iloc[i - 1])
            # Trade on a signal CHANGE, not on signal-vs-position. Live only
            # acts on a flip (live_test.order_decision) and validation's
            # compute_returns_with_stop models a fired stop as flat until the
            # signal VALUE changes. Comparing target to sleeve.direction alone
            # re-entered on an UNCHANGED signal the moment a stop zeroed the
            # position — an entry neither live nor the validated return stream
            # ever takes, filled at the open of the very bar that stopped out.
            # prev_target is None only on a sleeve's first evaluated bar, which
            # is the startup 'align' live does take.
            flipped = sleeve.prev_target is None or target != sleeve.prev_target
            sleeve.prev_target = target
            if flipped and target != sleeve.direction:
                if sleeve.direction:
                    sleeve.closed_returns.append(sleeve.direction * (prev.close - sleeve.entry) / sleeve.entry)
                    if charge_spread:
                        sp = -_half_spread(sleeve.instrument, sleeve.units,
                                           prev.close, sleeve.markq)
                        cm = -_commission(sleeve.instrument, sleeve.units,
                                          prev.close, sleeve.markq, venue, 1)
                        c = sp + cm
                        pnl += c; sleeve.pnl += c
                        if in_evaluation:
                            sleeve.spread_paid += sp; sleeve.comm_paid += cm
                            sleeve.pnl_eval += c
                sleeve.units = sleeve.direction = 0
                if target:
                    corr = 0.5 if any(directions.get(peer) == target for peer in sleeve.peers) else 1.0
                    units = risk_units(pre_equity, sleeve.atr.iloc[i - 1], sleeve.stop_mult,
                                       sleeve.weight_scale, corr, sleeve.kelly, sleeve.decay, risk, max_risk,
                                       float(sleeve.quote_to_usd.iloc[i - 1]), sleeve.instrument,
                                       venue, skip_min_lot)
                    if units:
                        sleeve.direction, sleeve.units, sleeve.entry = target, units, row.open
                        sleeve.stop = sleeve.entry - target * sleeve.stop_mult * sleeve.atr.iloc[i - 1]
                        sleeve.entries += 1
                        if sleeve.decay == 0.5:
                            sleeve.decay_events += 1

            if in_evaluation:
                sleeve.bars_seen += 1
                sleeve.bars_held += 1 if sleeve.direction else 0

            # Calendar gap to THIS sleeve's next bar. Instruments do not share a
            # calendar, so it has to be per-sleeve rather than book-level.
            nxt = (sleeve.frame.date.iloc[i + 1]
                   if i + 1 < len(sleeve.frame) else None)
            gap_days = int((nxt - ts).days) if nxt is not None else 0

            # WEEKEND FLAT. OANDA daily bars are stamped with the bar's START and
            # there is no Friday- or Saturday-stamped bar (verified: the daily
            # index runs Sunday..Thursday), so the THURSDAY-stamped bar IS the
            # Friday session and its close is the Friday close — the last moment
            # before the 21:00 rollover.
            #
            # THE POSITION IS NOT RE-OPENED ON MONDAY, and that is the whole point
            # of this arm. prev_target is deliberately left untouched, so the
            # Sunday bar sees target == prev_target, `flipped` is False, and no
            # entry is taken — exactly what live does, where order_decision
            # returns None whenever latest_signal == prev_signal. The sleeve stays
            # flat until the signal genuinely CHANGES. Re-entering here would
            # credit the book an entry neither the runner nor the validated return
            # stream ever takes (the defect commit 58c1a6f fixed).
            if (weekend_flat != "off" and sleeve.direction and ts.weekday() == 3
                    and _weekend_flat_applies(weekend_flat,
                                                 sleeve.instrument)):
                sleeve.closed_returns.append(
                    sleeve.direction * (row.close - sleeve.entry) / sleeve.entry)
                if charge_spread:
                    sp = -_half_spread(sleeve.instrument, sleeve.units,
                                       float(row.close), sleeve.markq)
                    cm = -_commission(sleeve.instrument, sleeve.units,
                                      float(row.close), sleeve.markq, venue, 1)
                    c = sp + cm
                    pnl += c; sleeve.pnl += c
                    if in_evaluation:
                        sleeve.spread_paid += sp; sleeve.comm_paid += cm
                        sleeve.pnl_eval += c
                sleeve.units = sleeve.direction = 0
                if monday_reentry:
                    # THE COUNTERFACTUAL ARM. Neither the runner nor this
                    # simulator can do this today — it prices what building the
                    # re-entry would be WORTH, it does not describe current
                    # behaviour. Resetting prev_target to FLAT(0) is how a
                    # deliberate flatten is expressed live: the next bar then
                    # reads target != prev_target, flips, and opens at that bar's
                    # OPEN — which for the Sunday-stamped bar is the Sunday-evening
                    # weekly reopen, the intended re-entry moment. Exactly the
                    # mechanism risk_model_sim already uses for a guard halt.
                    sleeve.prev_target = 0

            # ROLL-FLAT: close before the 21:00 roll and reopen after, so the
            # position is never ON the books at the rollover instant and no swap
            # is charged. The daily bar boundary IS 21:00 UTC, so this is a
            # round trip at the bar edge — modelled as its cost (a full spread
            # plus commission) in place of the day's carry. The POSITION is
            # deliberately left intact: it is closed and reopened within the same
            # bar edge, so signal state, stop and entry are unchanged, and no
            # re-entry rule is involved. That is what distinguishes this from
            # --weekend-flat, which surrenders exposure until the next flip.
            if (roll_flat != "off" and charge_swap and sleeve.direction
                    and sleeve.units
                    and _roll_flat_applies(roll_flat, sleeve.instrument)):
                sp = -(2.0 * _half_spread(sleeve.instrument, sleeve.units,
                                          float(row.close), sleeve.markq))
                cm = -_commission(sleeve.instrument, sleeve.units,
                                  float(row.close), sleeve.markq, venue, 2)
                c = sp + cm
                pnl += c; sleeve.pnl += c
                if in_evaluation:
                    sleeve.spread_paid += sp; sleeve.comm_paid += cm
                    sleeve.pnl_eval += c
            # SWAP on whatever is still open, carried into the next bar.
            elif charge_swap and sleeve.direction and sleeve.units:
                cost = swap_charge(sleeve.instrument, sleeve.units, float(row.close),
                                   sleeve.markq, gap_days, gap_days >= 3,
                                   tee_swap_free)
                pnl += cost
                sleeve.pnl += cost
                if in_evaluation:
                    sleeve.pnl_eval += cost
                    # pnl from the warmup span is discarded (`equity` only moves
                    # under in_evaluation), so the reported swap total has to be
                    # gated the same way or it prints five years of warmup carry
                    # the book never actually paid.
                    sleeve.swap_paid += cost
        if in_evaluation:
            equity += pnl
            # BOOK-LEVEL EXPOSURE carried into the next bar. Recorded, never acted
            # on: nothing in this repo gates on aggregate risk. CLUSTER_CAP is
            # per-cluster (RISK x CLUSTER_CAP = 1.00% each) and MAXRISK is
            # per-trade, so open positions stack toward the 3% daily wall with no
            # ceiling. The arithmetic max across today's 6 clusters is 4.25%,
            # above the wall — and the block bootstrap in prop_realsim_mc CANNOT
            # price that day, because it can only resample days that occurred.
            # These two columns are how you find out whether it is reachable.
            #   open_risk_initial — sum of the risk each open position was SIZED for
            #   open_risk_to_stop — what the book actually loses if every open
            #                       position stops from here (the daily-DD number)
            r_init = r_stop = 0.0
            for s in sleeves:
                if not s.direction or not s.units or not s.stop:
                    continue
                r_init += s.units * abs(s.entry - s.stop) * s.markq
                r_stop += max(0.0, s.direction * (s.mark - s.stop)) * s.units * s.markq
            daily.append((ts, equity, pnl, r_init / equity, r_stop / equity))
    result = pd.DataFrame(
        daily, columns=["date", "equity", "pnl", "open_risk_initial", "open_risk_to_stop"]
    ).set_index("date")
    return result


def report(result, sleeves, initial_equity, venue="oanda", skip_min_lot=False,
           charge_swap=False, weekend_flat="off", neutralise_decay=False,
           monday_reentry=False, charge_spread=False, tee_swap_free=False,
           roll_flat="off"):
    returns = result.pnl / result.equity.shift(1).fillna(initial_equity)
    vol = returns.std() * np.sqrt(252)
    sharpe = returns.mean() * 252 / vol if vol else np.nan
    dd = result.equity / result.equity.cummax() - 1
    print(f"Start equity: ${initial_equity:,.0f}")
    print(f"End equity:   ${result.equity.iloc[-1]:,.0f}")
    print(f"Return:       {(result.equity.iloc[-1] / initial_equity - 1)*100:+.2f}%")
    print(f"Sharpe:       {sharpe:.3f}")
    print(f"Max drawdown: {dd.min()*100:.2f}%")
    print(f"Worst day:    {returns.min()*100:.2f}%")
    print(f"Sleeves:      {len(sleeves)}")
    print(f"Entries:      {sum(s.entries for s in sleeves)}")
    seen = sum(s.bars_seen for s in sleeves)
    print(f"In market:    {sum(s.bars_held for s in sleeves) / seen * 100:.1f}% of "
          f"sleeve-bars" if seen else "In market:    n/a")
    if charge_swap:
        paid = sum(s.swap_paid for s in sleeves)
        print(f"Swap charged: ${paid:,.0f} ({paid / initial_equity * 100:+.2f}% of "
              f"start equity) — MEASURED rates, both sides pay")
        uncosted = sorted({s.instrument for s in sleeves
                           if s.instrument not in SWAP_PER_UNIT_DAY
                           and s.instrument not in SWAP_PCT_NOTIONAL_DAY})
        proxied = sorted({s.instrument for s in sleeves
                          if s.instrument in SWAP_PCT_NOTIONAL_DAY})
        per_inst = {}
        for s in sleeves:
            per_inst[s.instrument] = per_inst.get(s.instrument, 0.0) + s.swap_paid
        for inst, amt in sorted(per_inst.items(), key=lambda kv: kv[1]):
            if amt:
                print(f"    {inst:<12} ${amt:>12,.0f}")
        if proxied:
            print(f"  proxied (no accrual measured): {', '.join(proxied)}")
        if uncosted:
            print(f"  UNCOSTED (charged 0): {', '.join(uncosted)}")
    else:
        print("Swap:         NOT CHARGED — this return is GROSS of rollover")
    if charge_spread:
        sp = sum(s.spread_paid for s in sleeves)
        cm = sum(s.comm_paid for s in sleeves)
        print(f"Spread:       ${sp:,.0f} ({sp / initial_equity * 100:+.2f}%)")
        print(f"Commission:   ${cm:,.0f} ({cm / initial_equity * 100:+.2f}%) — "
              f"{venue} card, charged PER SIDE")
        per_comm = {}
        for s in sleeves:
            per_comm[s.instrument] = per_comm.get(s.instrument, 0.0) + s.comm_paid
        for inst, amt in sorted(per_comm.items(), key=lambda kv: kv[1]):
            if amt:
                print(f"    {inst:<12} ${amt:>12,.0f}")
        if venue == "ctrader":
            spec, held = _ct_spec(), {s.instrument for s in sleeves}
            gone = sorted(i for i in held if i not in spec)
            absent = sorted(i for i in held
                            if i in spec and i not in CTRADER_COMMISSION)
            if gone:
                print(f"  NOT OFFERED on cTrader — 0 entries, weight allocated "
                      f"but dead: {', '.join(gone)}")
            if absent:
                print(f"  UNCOSTED (traded, but NOT on the broker card): "
                      f"{', '.join(absent)}")
    else:
        print("Spread:       NOT CHARGED — entries and exits are free here")
    print(f"Weekend flat: {weekend_flat}"
          + ("" if weekend_flat == "off"
             else (" — flat at the Friday close, "
                   + ("RE-OPENED at the Sunday reopen (COUNTERFACTUAL: needs a "
                      "re-entry state order_decision does not have)"
                      if monday_reentry else
                      "NO Monday re-entry (waits for a real signal flip)"))))
    print(f"Roll flat:    {roll_flat}"
          + ("" if roll_flat == "off" or not charge_swap
             else " — closed and reopened at the bar edge, a round trip charged "
                  "in place of the day's carry")
          + ("" if roll_flat == "off" or charge_swap
             else " — INERT: roll-flat needs --charge-swap"))
    if tee_swap_free:
        print("Swap-free .t: COUNTERFACTUAL — orders on .t symbols are REJECTED")
    if neutralise_decay:
        print("Decay:        PINNED at 1.0 (overlay comparison)")
    # kelly_checks counts CALLS, not effect — the multiplier still runs (and returns
    # 1.0) when the overlay is off, so a bare count reads as "Kelly is active" on a
    # run where no position was levered. State first, count second.
    if kelly_policy.ENABLED:
        print(f"Kelly: {kelly_policy.UP}x/{kelly_policy.FLOOR}x every "
              f"{kelly_policy.RECOMPUTE_EVERY} bar(s) — "
              f"{sum(s.kelly_checks for s in sleeves)} recomputes")
    else:
        print(f"Kelly: DISABLED (kelly_policy.ENABLED=False) — every bar at "
              f"{kelly_policy.NEUTRAL}x, {sum(s.kelly_checks for s in sleeves)} "
              f"no-op calls")
    print(f"Decay entries:{sum(s.decay_events for s in sleeves)}")
    # Printed unconditionally: a run's venue is not recoverable from the numbers,
    # and quoting an OANDA-sized figure for the prop book is the exact defect this
    # flag was added to close.
    print(f"Venue: {venue} volume specs"
          + (" — min-lot opens SKIPPED (fix_runner semantics)" if skip_min_lot
             else " — min-lot opens FLOORED (live_test semantics)"))


def main():
    parser = argparse.ArgumentParser(description="Read-only real-sized current-book Oanda counterfactual")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--initial-equity", type=float, default=100000)
    parser.add_argument("--warmup-days", type=int, default=1825)
    parser.add_argument("--risk", type=float, default=RISK)
    parser.add_argument("--max-risk", type=float, default=MAX_RISK)
    parser.add_argument("--state", default="portfolio_state.json")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--csv")
    parser.add_argument("--venue", choices=("oanda", "ctrader"), default="oanda",
                        help="volume minimums to size against. 'oanda' = the paper book; "
                             "'ctrader' = the PROP book (required for any prop risk number)")
    parser.add_argument("--charge-swap", action="store_true",
                        help="charge measured rollover swap (default OFF — every "
                             "figure this file has ever printed is GROSS of swap)")
    parser.add_argument("--weekend-flat", default="off",
                        help="close positions at the Friday close. The sleeve is "
                             "NOT re-opened on Monday — live only enters on a "
                             "signal CHANGE, so it stays flat until the next flip")
    parser.add_argument("--roll-flat", default="off",
                        help="close before the 21:00 roll and reopen after, paying a "
                             "full spread per held day INSTEAD of that day's swap")
    parser.add_argument("--tee-swap-free", action="store_true",
                        help="COUNTERFACTUAL, NOT EXECUTABLE: orders on .t symbols "
                             "are REJECTED (tested 2026-08-11). Prices what "
                             "swap-free would be worth; nothing more. "
                             "Zeroes the swap on the seven .t listings the account "
                             "carries. Spread is deliberately left at the PLAIN "
                             "listing's, which is conservative — .t measured "
                             "tighter on 4 of 5 instruments")
    parser.add_argument("--charge-spread", action="store_true",
                        help="charge spread+commission on every entry and exit, at "
                             "pipeline_utils.apply_costs' half/half convention. "
                             "REQUIRED to price --monday-reentry honestly: the "
                             "weekly round trip is otherwise free")
    parser.add_argument("--monday-reentry", action="store_true",
                        help="COUNTERFACTUAL: re-open at the Sunday reopen instead "
                             "of waiting for a flip. Requires a re-entry state that "
                             "does NOT exist in order_decision — this prices the "
                             "code change, it does not model today's book")
    parser.add_argument("--neutralise-decay", action="store_true",
                        help="pin decay at 1.0. REQUIRED for any overlay comparison: "
                             "decay reads this simulator's own closed trades, so an "
                             "overlay otherwise scores its own feedback loop")
    parser.add_argument("--no-skip-min-lot", action="store_true",
                        help="under --venue ctrader, FLOOR to the minimum lot (live_test "
                             "semantics) instead of SKIPPING the open the way fix_runner does")
    args = parser.parse_args()
    # fix_runner ALWAYS skips, so skipping is the default for the prop venue and
    # has to be opted out of, not into.
    skip = args.venue == "ctrader" and not args.no_skip_min_lot
    sleeves = load_sleeves(args.start, args.end, args.warmup_days, args.state, args.allow_partial)
    result = simulate(sleeves, args.start, args.end, args.initial_equity, args.risk, args.max_risk,
                      args.venue, skip, args.charge_swap, args.weekend_flat,
                      args.neutralise_decay, args.monday_reentry, args.charge_spread,
                      args.tee_swap_free, args.roll_flat)
    if result.empty:
        raise RuntimeError("no evaluation bars")
    report(result, sleeves, args.initial_equity, args.venue, skip,
           args.charge_swap, args.weekend_flat, args.neutralise_decay,
           args.monday_reentry, args.charge_spread,
                      args.tee_swap_free, args.roll_flat)
    if args.csv:
        result.to_csv(args.csv)


if __name__ == "__main__":
    main()
