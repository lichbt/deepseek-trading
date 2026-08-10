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
# CHARGED SYMMETRICALLY — both sides pay. Real swap is side-dependent and one
# side of an FX pair usually RECEIVES carry, so this is conservative for the
# holding book and therefore biases AGAINST the hold arm, i.e. in FAVOUR of
# weekend-flat. Every observation behind the table happens to be the side the
# book actually held. Same convention pipeline_utils already uses for indices.
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
}

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
    'XCU_USD':    -0.000687,    # XAG proxy
    'WHEAT_USD':  -0.000120,    # placeholder, no source
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


def _commission(instrument, units):
    """Per-round-trip commission, charged in full at the open."""
    from pipeline_utils import get_commission
    return abs(units) * get_commission(instrument)


def swap_charge(instrument, units, price, quote_to_usd, gap_days, crosses_rollover):
    """USD cost of carrying `units` from this bar's close to the next bar's.

    `gap_days` is the CALENDAR gap to the sleeve's own next bar, which is what
    makes the arithmetic come out without a weekday calendar: an ordinary
    instrument is charged on weekdays only but takes a 3x Friday roll, and the
    triple exactly compensates the two uncharged weekend days — so charge-days
    equals gap-days on every bar. Only the seven-day instruments deviate.
    """
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
    swap_paid: float = 0.0
    spread_paid: float = 0.0
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
             neutralise_decay=False, monday_reentry=False, charge_spread=False):
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
                active_return = sleeve.direction * (exit_price - prev.close) / prev.close
                sleeve.active_returns.append(active_return)
                if exit_price == sleeve.stop:
                    sleeve.closed_returns.append(sleeve.direction * (exit_price - sleeve.entry) / sleeve.entry)
                    if charge_spread:
                        c = -_half_spread(sleeve.instrument, sleeve.units,
                                          exit_price, quote_to_usd)
                        pnl += c; sleeve.pnl += c
                        if in_evaluation:
                            sleeve.spread_paid += c
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
                        c = -_half_spread(sleeve.instrument, sleeve.units,
                                          prev.close, sleeve.markq)
                        pnl += c; sleeve.pnl += c
                        if in_evaluation:
                            sleeve.spread_paid += c
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
                    and (weekend_flat == "all"
                         or sleeve.instrument in SELECTIVE_FLAT)):
                sleeve.closed_returns.append(
                    sleeve.direction * (row.close - sleeve.entry) / sleeve.entry)
                if charge_spread:
                    c = -_half_spread(sleeve.instrument, sleeve.units,
                                      float(row.close), sleeve.markq)
                    pnl += c; sleeve.pnl += c
                    if in_evaluation:
                        sleeve.spread_paid += c
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

            # SWAP on whatever is still open, carried into the next bar.
            if charge_swap and sleeve.direction and sleeve.units:
                cost = swap_charge(sleeve.instrument, sleeve.units, float(row.close),
                                   sleeve.markq, gap_days, gap_days >= 3)
                pnl += cost
                sleeve.pnl += cost
                if in_evaluation:
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
           monday_reentry=False, charge_spread=False):
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
        print(f"Spread+comm:  ${sp:,.0f} ({sp / initial_equity * 100:+.2f}%)")
    else:
        print("Spread:       NOT CHARGED — entries and exits are free here")
    print(f"Weekend flat: {weekend_flat}"
          + ("" if weekend_flat == "off"
             else (" — flat at the Friday close, "
                   + ("RE-OPENED at the Sunday reopen (COUNTERFACTUAL: needs a "
                      "re-entry state order_decision does not have)"
                      if monday_reentry else
                      "NO Monday re-entry (waits for a real signal flip)"))))
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
    parser.add_argument("--weekend-flat", choices=("off", "all", "selective"),
                        default="off",
                        help="close positions at the Friday close. The sleeve is "
                             "NOT re-opened on Monday — live only enters on a "
                             "signal CHANGE, so it stays flat until the next flip")
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
                      args.neutralise_decay, args.monday_reentry, args.charge_spread)
    if result.empty:
        raise RuntimeError("no evaluation bars")
    report(result, sleeves, args.initial_equity, args.venue, skip,
           args.charge_swap, args.weekend_flat, args.neutralise_decay,
           args.monday_reentry, args.charge_spread)
    if args.csv:
        result.to_csv(args.csv)


if __name__ == "__main__":
    main()
