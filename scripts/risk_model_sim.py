#!/usr/bin/env python3
"""Replay the current prop book with a PLUGGABLE risk model.

WHY THIS EXISTS. oanda_book_simulator.simulate() hard-codes the sizing decision as
`min(risk * ws * corr * kelly * decay, max_risk)` — one scalar, no account state.
Scoring an account-aware policy (today's budget, cushion above the static floor,
distance to the step target) needs that expression to become a callback. This file
is that simulator with exactly one thing changed, plus two things recorded that
nothing in the repo records today.

WHY IT IS NOT A LINEAR RE-WEIGHTING OF A BOOK CSV. cTrader minimums are coarse for FX
(1000 units) and fine for indices (0.01), and fix_runner SKIPS an open outright when
one minimum lot already implies more than MAXRISK. Sizing is therefore a step
function of the risk fraction, not a scale of it — halving the fraction can change
nothing, or can remove a sleeve from the book entirely. Any harness that rescales an
equity curve instead of re-running the sizing path gets that wrong exactly where it
matters.

WHAT IS NEW HERE.

1. INTRADAY FLOATING LOW. Every existing script in this repo is close-to-close;
   stress_book.py says so about itself. The firm measures the daily loss on the
   floating low "at any point during the day", so a close-to-close worst day
   understates the number the account is actually judged on. Two columns are
   emitted because the truth is between them and neither alone is honest:
     intraday_low_cotimed  every open sleeve hits its worst tick simultaneously.
                           A BOUND, not a path. Conservative by construction.
     intraday_low_worst1   only the single worst sleeve dips. A floor on the
                           estimate.
   Quote both or neither.

2. THE GUARD'S REAL COST. When the breaker fires it flattens, and because entries
   fire on a signal CHANGE, live resets sleeves to FLAT(0) — so the book RE-ENTERS
   on the next bar. Modelling the halt as a free truncation (which is what
   prop_guarded_mc necessarily does on bootstrapped days) makes it look strictly
   beneficial. Here the flatten sets prev_target = 0 as well, so the re-entries are
   paid. montecarlo.md notes this cost "is not modelled anywhere"; it is modelled
   here, on the real historical path.

REPRODUCTION IS THE ACCEPTANCE TEST. With every component disabled and guard off,
this must reproduce oanda_book_simulator.simulate() to 1e-6 on every bar. Run
`--check-baseline`. If that fails, nothing else this file prints is worth reading.
"""
import argparse
import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import oanda_book_simulator as S
import prop_risk_model as M

# REPO-RELATIVE, deliberately. These pointed at a per-session scratchpad
# (/private/tmp/claude-501/.../fee33f56-.../scratchpad) that stopped existing when
# that session ended, so `--check-baseline` died on FileNotFoundError for anyone who
# ran it bare. An acceptance test the docstring calls load-bearing has to work
# without remembering which session built its inputs.
SCRATCH = Path(__file__).resolve().parent.parent / ".scratch" / "costed"
DEFAULT_SLEEVES = SCRATCH / "sleeves_ctrader.pkl"
DEFAULT_BASELINE = SCRATCH / "baseline_005.csv"

COMPONENTS = ("throttle", "budget_gate", "ramp", "endgame", "consistency")


def config_from(base_risk=0.005, max_risk=0.02, halt_fraction=0.80,
                components=(), **kw):
    """RiskConfig with the named components enabled and nothing else."""
    flags = {"%s_enabled" % c: (c in components) for c in COMPONENTS}
    return M.RiskConfig(base_risk=base_risk, max_risk=max_risk,
                        halt_fraction=halt_fraction, **flags, **kw)


def _units_from_fraction(fraction, equity, atr_value, stop_mult, quote_to_usd,
                         instrument, cfg, venue, skip_min_lot):
    """The tail of oanda_book_simulator.risk_units, with the fraction supplied.

    The min-lot skip gates on cfg.max_risk and NOT on `fraction`, because that is
    what fix_runner does: it asks whether one minimum lot exceeds the per-trade
    ceiling, not whether it exceeds this particular trade's sized risk.
    """
    if not np.isfinite(atr_value) or atr_value <= 0 or quote_to_usd <= 0:
        return 0.0
    units = equity * fraction / (stop_mult * atr_value * quote_to_usd)
    if instrument is None:
        return units
    if skip_min_lot and venue == "ctrader":
        if S.min_lot_implied_risk(instrument, stop_mult, atr_value, equity,
                                  quote_to_usd) > cfg.max_risk:
            return 0.0
    return S._clamp_units(units, instrument, venue)


def run(cfg, sleeves_blob, start="2024-01-01", end="2026-08-07",
        initial_equity=100000.0, venue="ctrader", skip_min_lot=True,
        guard=True, weight_scale_override=None, record_sleeve_pnl=False,
        charge_swap=False, weekend_flat="off", neutralise_decay=False,
        monday_reentry=False, charge_spread=False, tee_swap_free=False,
        roll_flat="off", halt_resume="reopen"):
    """-> (DataFrame, summary dict). Mirrors oanda_book_simulator.simulate().

    `sleeves_blob` is PICKLED BYTES, not a list: simulate() mutates Sleeve objects,
    so each run must start from its own copy or configs silently contaminate each
    other through carried positions, Kelly windows and decay state.
    """
    sleeves = pickle.loads(sleeves_blob)
    if weight_scale_override:
        for s in sleeves:
            if s.sid in weight_scale_override:
                s.weight_scale = weight_scale_override[s.sid]

    timestamps = sorted(set().union(*(set(s.frame.date) for s in sleeves)))
    equity = float(initial_equity)
    step_start = float(initial_equity)
    step = 1
    best_day_profit = 0.0
    step_days = {}
    daily = []
    halts_daily = 0
    halted_total = False
    day_index = 0

    for ts in timestamps:
        if ts > pd.Timestamp(end):
            continue
        in_evaluation = ts >= pd.Timestamp(start)
        bar_sleeves = [s for s in sleeves if ts in set(s.frame.date)]
        pre_equity = equity
        pnl = 0.0
        adverse = []
        bar_pnl = {}
        directions = {s.sid: s.direction for s in sleeves}

        # The account view used for SIZING. day_base is pre_equity because the firm
        # anchors on max(balance, equity) at the day roll and, at a daily bar's
        # close, balance == equity. day_low is pre_equity too: positions are opened
        # at THIS bar's open, when none of this day's loss has happened yet.
        state = M.AccountState(
            equity=pre_equity, initial_balance=float(initial_equity),
            step_start_balance=step_start, day_base=pre_equity,
            day_low=pre_equity, step=step, best_day_profit=best_day_profit)

        for sleeve in sorted(bar_sleeves, key=lambda s: s.sid):
            i = int(sleeve.frame.index[sleeve.frame.date == ts][0])
            if i == 0:
                continue
            row, prev = sleeve.frame.iloc[i], sleeve.frame.iloc[i - 1]
            sleeve.mark = float(row.close)
            sleeve.markq = (float(sleeve.quote_to_usd.iloc[i])
                            if sleeve.quote_to_usd is not None else 1.0)
            if sleeve.direction:
                quote_to_usd = (float(sleeve.quote_to_usd.iloc[i])
                                if sleeve.quote_to_usd is not None else 1.0)
                # Adverse excursion, captured BEFORE the exit zeroes the position.
                # Bounded by the stop: past it the stop fills, so the loss stops.
                if sleeve.direction > 0:
                    worst_px = max(float(row.low), float(sleeve.stop))
                    adv = min(0.0, worst_px - float(prev.close))
                else:
                    worst_px = min(float(row.high), float(sleeve.stop))
                    adv = min(0.0, float(prev.close) - worst_px)
                adverse.append(adv * sleeve.units * quote_to_usd)

                exit_price = row.close
                if sleeve.direction > 0 and row.low <= sleeve.stop:
                    exit_price = sleeve.stop
                elif sleeve.direction < 0 and row.high >= sleeve.stop:
                    exit_price = sleeve.stop
                move = sleeve.direction * (exit_price - prev.close) * sleeve.units * quote_to_usd
                pnl += move
                sleeve.pnl += move
                bar_pnl[sleeve.sid] = bar_pnl.get(sleeve.sid, 0.0) + move
                sleeve.active_returns.append(
                    sleeve.direction * (exit_price - prev.close) / prev.close)
                if exit_price == sleeve.stop:
                    sleeve.closed_returns.append(
                        sleeve.direction * (exit_price - sleeve.entry) / sleeve.entry)
                    if charge_spread:
                        sp = -S._half_spread(sleeve.instrument, sleeve.units,
                                             exit_price, quote_to_usd)
                        cm = -S._commission(sleeve.instrument, sleeve.units,
                                            exit_price, quote_to_usd, venue, 1)
                        c = sp + cm
                        pnl += c; sleeve.pnl += c
                        bar_pnl[sleeve.sid] = bar_pnl.get(sleeve.sid, 0.0) + c
                        if in_evaluation:
                            sleeve.spread_paid += sp; sleeve.comm_paid += cm
                    sleeve.units = sleeve.direction = 0

            if i % S.KELLY_RECOMPUTE == 0:
                sleeve.kelly = S.kelly_multiplier(sleeve.active_returns)
                sleeve.kelly_checks += 1
            if neutralise_decay:
                # Pinned for overlay comparisons: decay reads this run's OWN
                # closed trades, a feedback loop that does not exist live.
                sleeve.decay = 1.0
            else:
                sleeve.decay, sleeve.last_decay_check = S.decay_multiplier(
                    sleeve.closed_returns, ts, sleeve.last_decay_check, sleeve.decay)

            target = int(sleeve.signal.iloc[i - 1])
            flipped = sleeve.prev_target is None or target != sleeve.prev_target
            sleeve.prev_target = target
            if flipped and target != sleeve.direction:
                if sleeve.direction:
                    sleeve.closed_returns.append(
                        sleeve.direction * (prev.close - sleeve.entry) / sleeve.entry)
                    if charge_spread:
                        sp = -S._half_spread(sleeve.instrument, sleeve.units,
                                             prev.close, sleeve.markq)
                        cm = -S._commission(sleeve.instrument, sleeve.units,
                                            prev.close, sleeve.markq, venue, 1)
                        c = sp + cm
                        pnl += c; sleeve.pnl += c
                        bar_pnl[sleeve.sid] = bar_pnl.get(sleeve.sid, 0.0) + c
                        if in_evaluation:
                            sleeve.spread_paid += sp; sleeve.comm_paid += cm
                sleeve.units = sleeve.direction = 0
                if target:
                    corr = 0.5 if any(directions.get(p) == target
                                      for p in sleeve.peers) else 1.0
                    atr_value = sleeve.atr.iloc[i - 1]
                    q2u = float(sleeve.quote_to_usd.iloc[i - 1])
                    fraction = M.size_fraction(
                        state, cfg, weight_scale=sleeve.weight_scale,
                        corr_scale=corr, kelly=sleeve.kelly, decay=sleeve.decay)
                    stop_risk = fraction  # loss-to-stop as a fraction of equity
                    if not M.admit_open(stop_risk, state, cfg):
                        continue
                    units = _units_from_fraction(
                        fraction, pre_equity, atr_value, sleeve.stop_mult, q2u,
                        sleeve.instrument, cfg, venue, skip_min_lot)
                    if units:
                        sleeve.direction, sleeve.units = target, units
                        sleeve.entry = row.open
                        sleeve.stop = sleeve.entry - target * sleeve.stop_mult * atr_value
                        sleeve.entries += 1
                        if charge_spread:
                            sp = -S._half_spread(sleeve.instrument, units,
                                                 row.open, sleeve.markq)
                            cm = -S._commission(sleeve.instrument, units,
                                                row.open, sleeve.markq, venue, 1)
                            c = sp + cm
                            pnl += c; sleeve.pnl += c
                            bar_pnl[sleeve.sid] = bar_pnl.get(sleeve.sid, 0.0) + c
                            if in_evaluation:
                                sleeve.spread_paid += sp; sleeve.comm_paid += cm
                        if sleeve.decay == 0.5:
                            sleeve.decay_events += 1

            # WEEKEND FLAT and SWAP — same semantics as oanda_book_simulator, and
            # they have to stay identical or --check-baseline stops meaning
            # anything. The THURSDAY-stamped bar is the Friday session (OANDA
            # stamps a daily bar with its START and there is no Friday- or
            # Saturday-stamped bar), and prev_target is deliberately NOT touched,
            # so no Monday re-entry is taken on an unchanged signal.
            nxt = (sleeve.frame.date.iloc[i + 1]
                   if i + 1 < len(sleeve.frame) else None)
            gap_days = int((nxt - ts).days) if nxt is not None else 0
            if (weekend_flat != "off" and sleeve.direction and ts.weekday() == 3
                    and S._weekend_flat_applies(weekend_flat,
                                                 sleeve.instrument)):
                sleeve.closed_returns.append(
                    sleeve.direction * (row.close - sleeve.entry) / sleeve.entry)
                if charge_spread:
                    sp = -S._half_spread(sleeve.instrument, sleeve.units,
                                         float(row.close), sleeve.markq)
                    cm = -S._commission(sleeve.instrument, sleeve.units,
                                        float(row.close), sleeve.markq, venue, 1)
                    c = sp + cm
                    pnl += c; sleeve.pnl += c
                    bar_pnl[sleeve.sid] = bar_pnl.get(sleeve.sid, 0.0) + c
                    if in_evaluation:
                        sleeve.spread_paid += sp; sleeve.comm_paid += cm
                sleeve.units = sleeve.direction = 0
                if monday_reentry:
                    # THE DEPLOYED ARM since 2026-08-17 (fix_runner's
                    # WEEKEND_FLAT_REENTRY, default on). Fills at the Sunday-stamped
                    # bar's open — the 21:00 UTC weekly open — while the pod's first
                    # pass of the week is 00:15 UTC Monday, so this is an UPPER
                    # BOUND by about 3.25h. Same mechanism as the guard halt: reset to
                    # FLAT(0) so the next bar flips and opens at its OPEN, which
                    # for the Sunday-stamped bar is the weekly reopen.
                    sleeve.prev_target = 0
            # ROLL-FLAT — identical semantics to oanda_book_simulator: close
            # before the 21:00 roll and reopen after, so nothing is on the books
            # at the rollover instant and no swap is charged. Priced as its cost
            # (a full spread plus commission) INSTEAD of the day's carry, hence
            # the elif. The position is deliberately left intact — same bar edge,
            # so signal state, stop and entry are unchanged and no re-entry rule
            # is involved. Like swap, it is a cash adjustment at the bar close and
            # so does NOT enter `adverse`: the intraday low is unaffected.
            if (roll_flat != "off" and charge_swap and sleeve.direction
                    and sleeve.units
                    and S._roll_flat_applies(roll_flat, sleeve.instrument)):
                sp = -(2.0 * S._half_spread(sleeve.instrument, sleeve.units,
                                            float(row.close), sleeve.markq))
                cm = -S._commission(sleeve.instrument, sleeve.units,
                                    float(row.close), sleeve.markq, venue, 2)
                c = sp + cm
                pnl += c; sleeve.pnl += c
                bar_pnl[sleeve.sid] = bar_pnl.get(sleeve.sid, 0.0) + c
                if in_evaluation:
                    sleeve.spread_paid += sp; sleeve.comm_paid += cm
            elif charge_swap and sleeve.direction and sleeve.units:
                # Charged at the bar CLOSE, so it moves equity but deliberately
                # not the intraday low — the roll is a cash adjustment at 21:00,
                # not an excursion the floating equity passes through.
                cost = S.swap_charge(sleeve.instrument, sleeve.units,
                                     float(row.close), sleeve.markq,
                                     gap_days, gap_days >= 3, tee_swap_free)
                pnl += cost
                sleeve.pnl += cost
                bar_pnl[sleeve.sid] = bar_pnl.get(sleeve.sid, 0.0) + cost
                if in_evaluation:
                    sleeve.swap_paid += cost

        if not in_evaluation:
            continue

        equity += pnl
        low_cotimed = pre_equity + sum(adverse)
        low_worst1 = pre_equity + (min(adverse) if adverse else 0.0)
        halted = ""

        if guard:
            verdict = M.halt_decision(equity, pre_equity, step_start, cfg,
                                      day_low=low_cotimed)
            if verdict == "total":
                halted, halted_total = "total", True
            elif verdict == "daily":
                # Once the breaker fires, trading STOPS for the day, so the session
                # ends at the halt level (plus flatten slippage) regardless of where
                # it would otherwise have closed. That cuts BOTH ways and both are
                # real: a -5% day is stopped at -2.8%, and a day that dipped past the
                # line and would have recovered green is LOCKED at -2.8%.
                #
                # An earlier version applied this only when it worsened the day,
                # which modelled a breaker that can lose money but never save any —
                # it made the guard look purely costly at every risk level.
                halt_level = pre_equity * (1.0 - cfg.daily_limit * cfg.halt_fraction)
                halt_level -= cfg.guard_lag_pp * pre_equity
                equity = halt_level
                pnl = equity - pre_equity
                halts_daily += 1
                halted = "daily"
                for s in sleeves:
                    # WHICH KIND OF FLAT the halt leaves behind — fix_runner's
                    # flatten_all(preserve_signal=...) contract, modelled:
                    #   "reopen"  prev_target = 0 -> a PAUSE. The next bar reads
                    #             0 -> sig as a change, so the book re-establishes
                    #             and pays the spread on every sleeve. This is what
                    #             LIVE does today (fix_runner.py:872 takes the
                    #             preserve_signal=False default).
                    #   "wait"    prev_target untouched -> a SURRENDER. Nothing
                    #             re-enters until the strategy genuinely says
                    #             something new, so the book sits flat for however
                    #             long that takes. The counterfactual arm.
                    s.units = s.direction = 0
                    if halt_resume == "reopen":
                        s.prev_target = 0

        r_init = r_stop = 0.0
        for s in sleeves:
            if not s.direction or not s.units or not s.stop:
                continue
            r_init += s.units * abs(s.entry - s.stop) * s.markq
            r_stop += max(0.0, s.direction * (s.mark - s.stop)) * s.units * s.markq

        best_day_profit = max(best_day_profit, pnl)
        daily.append((ts, equity, pnl, r_init / equity, r_stop / equity,
                      low_cotimed, low_worst1, pre_equity, halted, step,
                      bar_pnl))
        day_index += 1

        if halted_total:
            break

        post = M.AccountState(equity=equity, initial_balance=float(initial_equity),
                              step_start_balance=step_start, day_base=pre_equity,
                              day_low=low_cotimed, step=step,
                              best_day_profit=best_day_profit)
        if step <= len(cfg.step_targets) and M.step_passed(post, cfg):
            step_days["step_%d_day" % step] = day_index
            nxt = M.begin_step(post, cfg)
            step, step_start, best_day_profit = nxt.step, nxt.step_start_balance, 0.0

    result = pd.DataFrame(daily, columns=[
        "date", "equity", "pnl", "open_risk_initial", "open_risk_to_stop",
        "intraday_low_cotimed", "intraday_low_worst1", "day_base", "halted", "step",
        "_sleeve_pnl",
    ]).set_index("date")

    if record_sleeve_pnl:
        # One column per sleeve, in UNITS OF EQUITY AT THE BAR OPEN, so the columns
        # are directly summable into a book return under any weighting.
        sids = sorted({k for d in result._sleeve_pnl for k in d})
        for sid in sids:
            result["r__" + sid] = [d.get(sid, 0.0) for d in result._sleeve_pnl] / result.day_base
    result = result.drop(columns=["_sleeve_pnl"])

    summary = _summarise(result, sleeves, initial_equity, cfg)
    summary.update(step_days)
    summary["n_halts_daily"] = halts_daily
    summary["halted_total"] = halted_total
    summary["step_1_day"] = step_days.get("step_1_day")
    summary["step_2_day"] = step_days.get("step_2_day")
    summary["swap_paid"] = float(sum(s.swap_paid for s in sleeves))
    summary["spread_paid"] = float(sum(s.spread_paid for s in sleeves))
    summary["comm_paid"] = float(sum(s.comm_paid for s in sleeves))
    # Per-instrument attribution. The reconciliation gate for any per-sleeve
    # verdict: these three dicts MUST sum to the three totals above, or a cost is
    # leaking somewhere the attribution cannot see.
    per_inst = {}
    for s in sleeves:
        # NOT sleeve.pnl_eval: this harness never accumulates the price move into
        # it (only oanda_book_simulator.simulate does), so it is identically zero
        # here and would read as "this sleeve made nothing". Per-sleeve P&L comes
        # from record_sleeve_pnl=True, which is the sanctioned path.
        d = per_inst.setdefault(s.instrument, {"swap": 0.0, "spread": 0.0,
                                               "comm": 0.0, "entries": 0,
                                               "sleeves": 0})
        d["swap"] += s.swap_paid
        d["spread"] += s.spread_paid
        d["comm"] += s.comm_paid
        d["entries"] += s.entries
        d["sleeves"] += 1
    summary["per_instrument"] = per_inst
    # TWO DIFFERENT THINGS, and conflating them reads as a modelling gap when it is
    # a venue fact. `not_offered` is not on the venue at all — ctrader_symbols.json
    # lists CORN/SOYBN/WHEAT under "unroutable", and _clamp_units returns 0.0 for
    # anything absent from the spec, so such a sleeve takes ZERO entries and has
    # nothing to cost. Its portfolio weight is still allocated, so it is dead risk
    # on the prop book (freed risk is dropped, never redistributed).
    # `uncosted_commission` is the real gap: offered and traded, but with no entry
    # on the broker's card, so its commission is silently 0.
    #
    # `uncosted_swap` is the SAME gap on the other leg, and it was invisible here
    # until 2026-08-14: swap_charge() returns 0.0 for any instrument in neither
    # SWAP_PER_UNIT_DAY nor SWAP_PCT_NOTIONAL_DAY, so such a sleeve is backtested
    # CARRY-FREE BY OMISSION rather than by measurement. NATGAS_USD is the live
    # case — routable on The5ers (symbol_id 132), 2,876 candidates generated, one
    # already deployed and retired, and no swap rate anywhere in the repo. Only
    # oanda_book_simulator.report() named these; this harness did not, so a
    # --charge-swap run here read as fully costed when one leg was empty.
    #
    # Filtered to instruments actually ON the venue, exactly as the commission set
    # is: an unroutable sleeve takes zero entries, so it has no carry to miss and
    # naming it would bury the real gap in noise.
    held = {s.instrument for s in sleeves}
    swap_costed = set(S.SWAP_PER_UNIT_DAY) | set(S.SWAP_PCT_NOTIONAL_DAY)
    if venue == "ctrader":
        spec = S._ct_spec()
        summary["not_offered"] = sorted(i for i in held if i not in spec)
        summary["uncosted_commission"] = sorted(
            i for i in held if i in spec and i not in S.CTRADER_COMMISSION)
        summary["uncosted_swap"] = sorted(
            i for i in held if i in spec and i not in swap_costed)
    else:
        summary["not_offered"] = []
        summary["uncosted_commission"] = []
        summary["uncosted_swap"] = sorted(i for i in held if i not in swap_costed)
    return result, summary


def _summarise(result, sleeves, initial_equity, cfg):
    if result.empty:
        return {"bars": 0}
    prev_eq = result.equity.shift(1).fillna(initial_equity)
    returns = result.pnl / prev_eq
    vol = returns.std() * np.sqrt(252)
    dd = result.equity / result.equity.cummax() - 1
    # The intraday low is measured against the DAY BASE, which is what the firm
    # compares it to — not against the running equity peak.
    intraday_ret = result.intraday_low_cotimed / result.day_base - 1.0
    intraday_ret1 = result.intraday_low_worst1 / result.day_base - 1.0
    return {
        "bars": int(len(result)),
        "base_risk": cfg.base_risk,
        "max_risk": cfg.max_risk,
        "halt_fraction": cfg.halt_fraction,
        "end_equity": float(result.equity.iloc[-1]),
        "total_return": float(result.equity.iloc[-1] / initial_equity - 1.0),
        "sharpe": float(returns.mean() * 252 / vol) if vol else float("nan"),
        "max_dd": float(dd.min()),
        "worst_day_close": float(returns.min()),
        "worst_day_intraday": float(intraday_ret.min()),
        "worst_day_intraday_worst1": float(intraday_ret1.min()),
        "days_past_halt_intraday": int((intraday_ret <= -cfg.daily_limit * cfg.halt_fraction).sum()),
        "days_past_wall_intraday": int((intraday_ret <= -cfg.daily_limit).sum()),
        "entries": int(sum(s.entries for s in sleeves)),
    }


def check_baseline(blob, baseline_path, start, end, venue, skip_min_lot, tol=1e-6):
    """Components off + guard off must reproduce the sanctioned simulator exactly.

    THE WINDOW COMES FROM THE BASELINE, not from --start/--end. This test asks one
    question — "do I still reproduce THIS csv" — so running it over any other window
    cannot answer it, and comparing a differently-windowed run produced only a bar
    count mismatch. The old default `--end 2026-08-07` against a baseline built
    through 2026-08-10 made `--check-baseline` report FAIL for a config reason, on
    code that reproduced to 3e-11 the moment the dates lined up. A load-bearing
    acceptance test that cries wolf is worse than none: it trains you to skip it.
    """
    base = pd.read_csv(baseline_path, parse_dates=["date"]).set_index("date")
    start = str(base.index[0].date())
    # +1 DAY, and this is the off-by-one that caused the original failure. Bars are
    # stamped at their 21:00/22:00 CLOSE, and the fetch window's `end` is exclusive
    # of that date — so a baseline whose last bar reads 2026-08-09 21:00 was built
    # with --end 2026-08-10. Passing the bare last date back drops that bar and the
    # count comes up one short (675 vs 676).
    end = str((base.index[-1] + pd.Timedelta(days=1)).date())
    print("window taken FROM THE BASELINE: %s -> %s (--start/--end ignored here)"
          % (start, end))
    cfg = config_from(0.005, 0.02, 0.80, components=())
    result, summary = run(cfg, blob, start, end, 100000.0, venue, skip_min_lot,
                          guard=False)
    if len(base) != len(result):
        print("FAIL bar count: baseline %d vs harness %d" % (len(base), len(result)))
        return False
    diff = (result.equity.values - base.equity.values)
    worst = float(np.max(np.abs(diff)))
    print("baseline bars      %d" % len(base))
    print("max abs equity diff %.6e  (tolerance %.0e)" % (worst, tol))
    print("worst day  baseline %.4f%%   harness %.4f%%"
          % (100 * (base.pnl / base.equity.shift(1).fillna(100000)).min(),
             100 * summary["worst_day_close"]))
    print("end equity baseline %.2f   harness %.2f"
          % (base.equity.iloc[-1], summary["end_equity"]))
    ok = worst <= tol
    print("REPRODUCTION: %s" % ("PASS" if ok else "FAIL"))
    return ok


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sleeves", default=str(DEFAULT_SLEEVES))
    p.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default="2026-08-07")
    p.add_argument("--risk", type=float, default=0.005)
    p.add_argument("--max-risk", type=float, default=0.02)
    p.add_argument("--halt-fraction", type=float, default=0.80)
    p.add_argument("--venue", choices=("oanda", "ctrader"), default="ctrader")
    p.add_argument("--no-skip-min-lot", action="store_true")
    p.add_argument("--components", default="",
                   help="comma-separated subset of: " + ",".join(COMPONENTS))
    p.add_argument("--guard", choices=("on", "off"), default="on")
    p.add_argument("--charge-swap", action="store_true",
                   help="charge measured rollover swap (default OFF)")
    p.add_argument("--weekend-flat", default="off",
                   help="flat at the Friday close, NO Monday re-entry")
    p.add_argument("--tee-swap-free", action="store_true",
                   help="COUNTERFACTUAL, NOT EXECUTABLE — orders on .t symbols are "
                        "REJECTED (tested 2026-08-11). Zeroes swap on the seven .t "
                        "listings to price what swap-free WOULD be worth")
    p.add_argument("--charge-spread", action="store_true",
                   help="charge spread+commission on every entry and exit")
    p.add_argument("--roll-flat", default="off",
                   help="close before the 21:00 roll and reopen after: pay a "
                   "'off' | 'all' | 'indices' | a comma-separated instrument "
                   "set, where 'indices' expands (e.g. 'indices,XAU_USD'). "
                   "Roll-flat only WINS where carry/day exceeds one round trip: "
                   "measured NAS100 17.94x, DE30 2.78x, XAU 2.43x, SPX500 1.44x, "
                   "XAG 1.39x, XCU 1.28x, but ETH 0.85x, BTC 0.65x and all FX "
                   "BELOW 1.0 — applying it there LOSES money. "
                        "round trip instead of the day's carry (needs "
                        "--charge-swap)")
    p.add_argument("--monday-reentry", action="store_true",
                   help="re-open at the Sunday reopen — models the DEPLOYED "
                        "WEEKEND_FLAT_REENTRY=1 runner (default off here, so the "
                        "baseline is unchanged); omit it to model REENTRY=0")
    p.add_argument("--neutralise-decay", action="store_true",
                   help="pin decay at 1.0 — required for overlay comparisons")
    p.add_argument("--csv")
    p.add_argument("--json")
    p.add_argument("--halt-resume", choices=("reopen", "wait"), default="reopen",
                   help="what a daily halt leaves behind. reopen = FLAT(0), the "
                        "book re-establishes next bar (what live does today); "
                        "wait = signal preserved, flat until it genuinely changes")
    p.add_argument("--check-baseline", action="store_true")
    args = p.parse_args()

    blob = Path(args.sleeves).read_bytes()
    skip = args.venue == "ctrader" and not args.no_skip_min_lot

    if args.check_baseline:
        ok = check_baseline(blob, args.baseline, args.start, args.end,
                            args.venue, skip)
        raise SystemExit(0 if ok else 1)

    comps = tuple(c.strip() for c in args.components.split(",") if c.strip())
    bad = set(comps) - set(COMPONENTS)
    if bad:
        raise SystemExit("unknown component(s): %s" % ", ".join(sorted(bad)))
    cfg = config_from(args.risk, args.max_risk, args.halt_fraction, comps)
    result, summary = run(cfg, blob, args.start, args.end, 100000.0, args.venue,
                          skip, guard=(args.guard == "on"),
                          charge_swap=args.charge_swap,
                          weekend_flat=args.weekend_flat,
                          neutralise_decay=args.neutralise_decay,
                          monday_reentry=args.monday_reentry,
                          charge_spread=args.charge_spread,
                          tee_swap_free=args.tee_swap_free,
                          roll_flat=args.roll_flat,
                          halt_resume=args.halt_resume)

    print("venue %s   components %s   guard %s   swap %s   weekend-flat %s"
          "   roll-flat %s%s"
          % (args.venue, ",".join(comps) or "none", args.guard,
             "CHARGED" if args.charge_swap else "not charged",
             args.weekend_flat + ("+monday-reentry" if args.monday_reentry else ""),
             args.roll_flat,
             "   decay PINNED" if args.neutralise_decay else ""))
    for k in ("bars", "end_equity", "total_return", "sharpe", "max_dd",
              "worst_day_close", "worst_day_intraday", "worst_day_intraday_worst1",
              "days_past_halt_intraday", "days_past_wall_intraday",
              "n_halts_daily", "halted_total", "step_1_day", "step_2_day",
              "entries", "swap_paid", "spread_paid", "comm_paid"):
        v = summary.get(k)
        if isinstance(v, float):
            print("  %-26s %s" % (k, ("%.4f" % v) if abs(v) < 100 else ("%.2f" % v)))
        else:
            print("  %-26s %s" % (k, v))
    if summary.get("not_offered"):
        print("  NOT OFFERED on %s — 0 entries, weight allocated but dead: %s"
              % (args.venue, ", ".join(summary["not_offered"])))
    if summary.get("uncosted_commission"):
        print("  UNCOSTED commission (traded, but NOT on the broker card): %s"
              % ", ".join(summary["uncosted_commission"]))
    if args.charge_swap and summary.get("uncosted_swap"):
        # Only under --charge-swap: without it NOTHING is costed and the banner
        # above already says the return is gross of rollover.
        print("  UNCOSTED swap (traded, but NO measured or proxied rate — carry "
              "charged 0, so this arm is GROSS of their rollover): %s"
              % ", ".join(summary["uncosted_swap"]))
    if args.charge_swap or args.charge_spread:
        print("  per instrument      sleeves  entries          swap"
              "        spread          comm")
        for inst, d in sorted(summary["per_instrument"].items(),
                              key=lambda kv: kv[1]["swap"] + kv[1]["comm"]):
            print("    %-16s %5d %8d %13.0f %13.0f %13.0f"
                  % (inst, d["sleeves"], d["entries"], d["swap"], d["spread"],
                     d["comm"]))
    if args.csv:
        result.to_csv(args.csv)
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
