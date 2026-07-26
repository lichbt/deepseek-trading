#!/usr/bin/env python3
"""
incubation.py — live-vs-validation tracking for deployed sleeves.

The 2026-06-09/10 look-ahead audit showed that validation evidence can be
corrupted in ways no backtest gate anticipates (the same-day macro join
survived every gate for weeks). The generic defense is to compare what each
live sleeve ACTUALLY did with what its own strategy code says it SHOULD have
done over the same live window:

    live bar returns       (parsed from the trader's own log)
        vs
    reconstructed returns  (build_strategy_returns over the same dates,
                            through the honest data path)

If evidence and execution are sound, the two series are near-identical —
same signals on the same bars — regardless of market noise. So divergence
is statistically meaningful within days, not months: a data leak, a cost
mis-model, a stale-cache feed or an execution bug all surface as low
correlation or a widening return gap. Market noise does not.

Statuses:
    incubating  — not enough overlapping active days to judge yet
    tracking    — live matches reconstruction
    diverging   — correlation soft or live lagging reconstruction; watch
    mismatch    — live is NOT doing what validation simulated; recommend
                  retire (or investigate) regardless of P&L

Usage:
    python incubation.py          # print the report section
    incubation.report_section()   # wired into hourly_report
"""
import os
import sys
import sqlite3
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

from portfolio import (DB_PATH, build_strategy_returns,
                       parse_log_returns, _short)

# Judgment thresholds. Correlation is computed over overlapping ACTIVE days
# (either side nonzero) — flat stretches carry no information about whether
# the trader follows its validated signals.
MIN_OVERLAP_ACTIVE = 5      # need >= this many active overlapping days to judge
WARN_CORR          = 0.60   # below this: diverging
FAIL_CORR          = 0.30   # below this: mismatch
GAP_WARN           = 0.02   # live cum return lags reconstruction by > 2%: diverging
GAP_FAIL           = 0.05   # ... by > 5%: mismatch
WARMUP_DAYS        = 365    # reconstruction lookback before deploy date (indicator warmup)
# Skip the first N active overlapping days from judgment. On deploy the sleeve
# opens a FRESH startup-alignment position to match validation's current signal,
# but the reconstruction assumes the position was already held (entered at an
# earlier bar/price) — so days 1-2 diverge by construction and drag correlation
# negative (de30 i1: 2 opposite settle days made corr=-0.37 on a profitable,
# otherwise-tracking sleeve). These are execution transients, not leaks.
SETTLE_ACTIVE_DAYS = 2

_ICON = {'incubating': '🧪', 'tracking': '✅', 'diverging': '⚠️', 'mismatch': '🚨'}


def load_strategies():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT s.id, s.timeframe, s.code, s.status,
               s.instrument, s.archetype, s.instrument2,
               vr.best_params, vr.walk_forward_gt_score,
               vr.is_gt_score, vr.torture_flags
        FROM strategies s
        JOIN validation_results vr ON s.id = vr.strategy_id
        WHERE s.status = 'paper_trading'
        ORDER BY s.id
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def sleeve_tracking(row: dict, start_date: str, end_date: str = None) -> dict:
    """Compare a sleeve's live returns with its reconstruction since deploy.

    Returns {status, n_active, corr, live_cum, expected_cum, gap, note}.
    Never raises — any data problem degrades to 'incubating' with a note.
    """
    sid = row["id"]
    out = {'status': 'incubating', 'n_active': 0, 'corr': None,
           'live_cum': None, 'expected_cum': None, 'gap': None, 'note': ''}
    try:
        # End at the last CLOSED day — an end date of "today" makes the candle
        # fetcher ask OANDA for a window ending tomorrow, which 400s.
        end_date = end_date or (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')
        start_day = pd.to_datetime(start_date).normalize().tz_localize(None)

        live = parse_log_returns(sid)
        if live is None or live.empty:
            out['note'] = 'no live bar returns logged yet'
            return out
        live = live[live.index >= start_day]

        # Reconstruct with a warmup buffer before the deploy date — rolling
        # indicators need history, and a deploy-date-only window also trips
        # build_strategy_returns' MIN_BARS floor. Judged bars are then sliced
        # back to >= deploy date.
        fetch_start = (start_day - pd.Timedelta(days=WARMUP_DAYS)).strftime('%Y-%m-%d')
        built = build_strategy_returns(row, fetch_start, end_date)
        if built is None:
            out['note'] = 'reconstruction unavailable'
            return out
        expected, _ = built
        if expected.empty:
            out['note'] = 'reconstruction unavailable'
            return out
        expected = expected[expected.index >= start_day]

        idx = live.index.intersection(expected.index)
        if len(idx) == 0:
            out['note'] = 'no overlapping days yet'
            return out
        lv, ex = live.reindex(idx).fillna(0.0), expected.reindex(idx).fillna(0.0)

        # Drop the first SETTLE_ACTIVE_DAYS active overlapping days — the
        # startup-alignment transient (see constant). Judge on what remains.
        active_all = ((lv != 0) | (ex != 0))
        active_dates = list(lv.index[active_all])
        if len(active_dates) <= SETTLE_ACTIVE_DAYS:
            out['note'] = f'settling ({len(active_dates)} active day(s), skip first {SETTLE_ACTIVE_DAYS})'
            return out
        judged_dates = active_dates[SETTLE_ACTIVE_DAYS:]
        lv, ex = lv.loc[judged_dates], ex.loc[judged_dates]

        active = (lv != 0) | (ex != 0)
        n_active = int(active.sum())
        out['n_active'] = n_active
        out['live_cum'] = float(lv.sum())
        out['expected_cum'] = float(ex.sum())
        out['gap'] = float(lv.sum() - ex.sum())

        if n_active < MIN_OVERLAP_ACTIVE:
            out['note'] = f'{n_active} active days (< {MIN_OVERLAP_ACTIVE})'
            return out

        la, ea = lv[active], ex[active]
        la_std, ea_std = float(la.std()), float(ea.std())
        if la_std == 0.0 and ea_std == 0.0:
            # Both constant on active days (e.g. identical fixed-size wins):
            # correlation is undefined, not zero — judge by the gap alone.
            corr = None
        elif la_std == 0.0 or ea_std == 0.0:
            # One side dead while the other varies IS a mismatch signal.
            corr = 0.0
            out['note'] = 'one side flat while the other trades'
        else:
            corr = float(np.corrcoef(la.values, ea.values)[0, 1])
        out['corr'] = corr

        ceff = 1.0 if corr is None else corr   # undefined corr → gap decides
        if ceff < FAIL_CORR or out['gap'] < -GAP_FAIL:
            out['status'] = 'mismatch'
        elif ceff < WARN_CORR or out['gap'] < -GAP_WARN:
            out['status'] = 'diverging'
        else:
            out['status'] = 'tracking'
        return out
    except Exception as e:
        out['note'] = f'tracking error: {e}'
        return out


def _deploy_dates() -> dict:
    """strategy_id -> live_status.start_date for the current book."""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute('SELECT strategy_id, start_date FROM live_status').fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows if r[1]}
    except Exception:
        return {}


def _live_positions() -> dict:
    """strategy_id -> current_position (int; 0 = flat) from live_status."""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute('SELECT strategy_id, current_position FROM live_status').fetchall()
        conn.close()
        return {r[0]: (r[1] or 0) for r in rows}
    except Exception:
        return {}


def report_section() -> str:
    """Formatted Telegram/console section. Never raises."""
    try:
        strategies = load_strategies()
        if not strategies:
            return ''
        starts = _deploy_dates()
        positions = _live_positions()
        # Only report sleeves currently HOLDING a live position — flat sleeves
        # add no actionable line (a flat sleeve isn't executing anything to
        # compare right now). Cuts the report to what's actually in-market.
        strategies = [r for r in strategies if positions.get(r['id'], 0) != 0]
        if not strategies:
            return ''
        lines = ['🧪 <b>Incubation</b> (live vs validation reconstruction) — in-market sleeves']
        now = datetime.now(timezone.utc)
        for row in strategies:
            sid = row['id']
            start = starts.get(sid)
            if not start:
                lines.append(f"  🧪 {_short(sid, 34):<34} no deploy date")
                continue
            age_d = max(0, (now - pd.to_datetime(start, utc=True)).days)
            t = sleeve_tracking(row, start)
            icon = _ICON[t['status']]
            if t['status'] == 'incubating':
                detail = t['note'] or 'gathering data'
                lines.append(f"  {icon} {_short(sid, 34):<34} {age_d:>3}d  incubating — {detail}")
            else:
                corr_s = f"{t['corr']:+.2f}" if t['corr'] is not None else ' n/a'
                lines.append(
                    f"  {icon} {_short(sid, 34):<34} {age_d:>3}d  corr={corr_s} "
                    f"live={t['live_cum']*100:+.1f}% vs exp={t['expected_cum']*100:+.1f}% "
                    f"({t['n_active']}d)"
                )
                if t['status'] == 'mismatch':
                    lines.append(f"      ↳ 🚨 live is not doing what validation simulated — RECOMMEND RETIRE/INVESTIGATE")
        return '\n'.join(lines)
    except Exception as e:
        print(f'[incubation] report failed: {e}', file=sys.stderr)
        return ''


if __name__ == '__main__':
    print(report_section().replace('<b>', '').replace('</b>', ''))
