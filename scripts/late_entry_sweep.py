#!/usr/bin/env python3
"""Does refusing a LATE entry ever beat taking it? Sweep k and answer with numbers.

Run this before re-proposing a stale-entry / signal-age filter. The idea has now
been raised twice (2026-07-31, 2026-08-06) and measured twice, both times NO —
so the standing decision is "there is no k". This script is what produces that
answer, so the third person to have the idea gets a sweep instead of a re-derivation.

    python3 scripts/late_entry_sweep.py

THE TRAP THE QUESTION SETS. The intuition ("late entries do worse") is CORRECT and
the data confirms it: the paired difference is significantly negative, and the
stop-out rate climbs as entry slips, because a re-anchored full-width stop sits
inside a move that already happened. But the decision does not turn on that.
Skipping banks exactly 0R, so a filter pays only where the late trade is NEGATIVE.
Worse-than-on-time is not worse-than-nothing, and every version of this proposal so
far has quietly substituted the first test for the second.

METHOD, matched to the 2026-07-31 study so runs stay comparable:
  * R MULTIPLES, not %. A stop-out is exactly -1R, so trades of differing ATR are
    risk-comparable; mean % return is not.
  * The stop is RE-ANCHORED at the late entry — a late trade gets a fresh
    full-width stop, which is what fix_runner actually does.
  * PAIRED on the same entry event, so trades that end before k bars dropping out
    of the sample cannot masquerade as an effect.

2026-08-06 result: 1151 entries, 22 sleeves, 2021-2026. Late R positive at EVERY
k in 0..21 with a 95% CI never touching zero; weakest k=12 at +0.114R
[+0.023,+0.205], t=2.46, n=459. Reproduces 2026-07-31 (k=0: 0.176R/t=6.47 vs
0.213R/t=5.95) on a different book.
"""
import os
import sys
import warnings
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fix_runner as fr                                          # noqa: E402
from data_fetcher import get_candles_date_range                  # noqa: E402

KS = list(range(0, 22))
END = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')
START = (datetime.utcnow() - timedelta(days=1825)).strftime('%Y-%m-%d')

# A stop narrower than 0.1% of price is a degenerate ATR, not a tradeable setup.
# R is (move / stop width), so a near-zero denominator manufactures an enormous R
# from an ordinary move. This is not hypothetical: on 2026-08-06 exactly ONE entry
# of 1151 breached it, scored R=442, and single-handedly moved the k=0 mean from
# 0.176 to 0.559 while crushing t from 6.47 to 1.45 — i.e. it inverted the read on
# the whole table. Excluded from BOTH legs so the pairing stays honest.
MIN_STOP_FRAC = 0.001


def atr_series(df, n=14):
    """Rolling ATR. Same true-range definition as fix_runner._atr, which returns
    only the last value; the sweep needs one per bar."""
    tr = pd.concat([(df['high'] - df['low']),
                    (df['high'] - df['close'].shift(1)).abs(),
                    (df['low'] - df['close'].shift(1)).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def simulate(sig, high, low, close, atr, idx, side, stop_mult):
    """Enter at close[idx]; exit on the stop (intrabar) or when the signal leaves
    `side`. Returns (R, stopped) or None when the entry is unusable."""
    risk = stop_mult * atr[idx]
    if not np.isfinite(risk) or risk <= 0:
        return None
    if risk / close[idx] < MIN_STOP_FRAC:
        return None
    entry = close[idx]
    stop = entry - side * risk
    for j in range(idx + 1, len(close)):
        if (side > 0 and low[j] <= stop) or (side < 0 and high[j] >= stop):
            return -1.0, True                      # a stop-out is exactly -1R
        if sig[j] != side:
            return side * (close[j] - entry) / risk, False
    return side * (close[-1] - entry) / risk, False


def main():
    rows = {k: [] for k in KS}                     # k -> (ontime_R, late_R, late_stopped)
    sleeves, _ = fr.load_sleeves()
    print(f'sleeves: {len(sleeves)}   window: {START} -> {END}\n')

    for s in sleeves:
        try:
            df = get_candles_date_range(s['inst'], START, END,
                                        granularity='D').reset_index(drop=True)
            df['date'] = pd.to_datetime(df['date'])
            if s['arch'] != 'standard':
                df = fr.inject_supplementary_data(df, s['arch'], s['inst'],
                                                  s['instrument2'], START, END, 'D')
            sig = np.sign(np.asarray(s['fn'](df, s['params'])).astype(float)).astype(int)
            atr = atr_series(df).values
            high, low, close = df['high'].values, df['low'].values, df['close'].values
            sm = s['params'].get('stop_mult', fr.DEFAULT_STOP_MULT)
            n_entry = 0
            for i in range(1, len(df)):
                if sig[i] == 0 or sig[i] == sig[i - 1]:
                    continue                       # not a fresh entry
                side = int(sig[i])
                base = simulate(sig, high, low, close, atr, i, side, sm)
                if base is None:
                    continue
                n_entry += 1
                for k in KS:
                    j = i + k
                    if j >= len(close) or sig[j] != side:
                        continue                   # trade is over; nothing to be late for
                    late = simulate(sig, high, low, close, atr, j, side, sm)
                    if late is not None:
                        rows[k].append((base[0], late[0], late[1]))
            print(f'  {s["sid"][:40]:42} bars={len(df):5d} entries={n_entry:3d}')
        except Exception as exc:
            print(f'  {s["sid"][:40]:42} SKIPPED: {type(exc).__name__}: {exc}')

    print(f'\n{"k":>3} {"n":>5} {"onTimeR":>9} {"lateR":>8} {"medLate":>8} {"diff":>8} '
          f'{"t(diff)":>8} {"t(lateR)":>9} {"lateR 95% CI":>18} {"stopout%":>9}')
    print('-' * 96)
    verdict = []
    for k in KS:
        r = rows[k]
        if len(r) < 20:
            continue
        a = np.array([x[0] for x in r]); b = np.array([x[1] for x in r])
        st = np.array([x[2] for x in r]); d = b - a
        n = len(r)
        se_b = b.std(ddof=1) / np.sqrt(n)
        t_b = b.mean() / se_b if se_b else float('nan')
        t_d = d.mean() / (d.std(ddof=1) / np.sqrt(n)) if d.std(ddof=1) else float('nan')
        lo, hi = b.mean() - 1.96 * se_b, b.mean() + 1.96 * se_b
        print(f'{k:>3} {n:>5} {a.mean():>9.3f} {b.mean():>8.3f} {np.median(b):>8.3f} '
              f'{d.mean():>8.3f} {t_d:>8.2f} {t_b:>9.2f} '
              f'{f"[{lo:+.3f},{hi:+.3f}]":>18} {st.mean() * 100:>8.1f}%')
        verdict.append((k, b.mean(), lo, hi, t_b, n))

    print('\nDECISION RULE: a filter at k pays only if taking the late trade LOSES money,')
    print('i.e. lateR < 0. Skipping banks exactly 0R — "worse than on-time" is NOT the bar.')
    bad = [v[0] for v in verdict if v[1] < 0]
    spans_zero = [v[0] for v in verdict if v[2] < 0 < v[3]]
    print(f'  k where mean lateR < 0           : {bad or "NONE"}')
    print(f'  k where the 95% CI includes zero : {spans_zero or "NONE"}')
    if verdict:
        w = min(verdict, key=lambda v: v[1])
        print(f'  weakest k = {w[0]}: lateR {w[1]:+.3f}  CI [{w[2]:+.3f},{w[3]:+.3f}]  '
              f't={w[4]:.2f}  n={w[5]}')
    if not bad and not spans_zero:
        print('  -> NO threshold pays. The decision stands: do not build the filter.')


if __name__ == '__main__':
    main()
