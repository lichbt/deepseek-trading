#!/usr/bin/env python3
"""CAUSAL-RETURN-COLLAPSE audit — the check the validator's flip-rate gate misses.

validator.truncation_lookahead_flip_rate divides flipped bars by ALL sampled
bars, so on a selective sleeve it structurally under-reads: xptusd trades 4% of
bars, flipped 4% of 120, PASSED the 5% gate — and 100% of its edge vanished
under causal replay (+57.7% -> -0.1% over 26 trades, 2026-07-25).

This rebuilds the signal the way live execution must — sig[t] = fn(df[:t+1])[-1],
no future visible — and compares the resulting return to the full-sample signal.

    ~100% collapse -> the edge IS the look-ahead (retire)
    ~33%           -> partial real edge (borderline)
    ~0%            -> causal, clean

The window is sized PER SLEEVE to contain at least MIN_TRADES entries rather
than a fixed bar count, because a fixed window gives a 4%-in-market sleeve only
a handful of trades to judge (the first xptusd run used 150 bars = 3 trades,
which was far too thin to conclude anything).

    ./venv/bin/python causal_audit.py                 # whole live book
    ./venv/bin/python causal_audit.py --max-inmkt 0.20  # only selective sleeves
    ./venv/bin/python causal_audit.py --sids a,b,c
"""
import argparse
import csv
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import evaluate_strategy as E
from validator import create_strategy_function

MIN_TRADES = 25       # entries the window must contain to be conclusive
MAX_BARS = 900        # runtime ceiling: one strategy recompute per bar
RETIRE_AT = 0.80      # collapse >= this = edge is the look-ahead
PARTIAL_AT = 0.25


def window_for(sig, min_trades=MIN_TRADES, max_bars=MAX_BARS):
    """Smallest window (from the end) holding >= min_trades entries."""
    s = pd.Series(np.asarray(sig)).fillna(0)
    entries = np.flatnonzero((s != 0) & (s != s.shift(1).fillna(0)))
    if len(entries) == 0:
        return None, 0
    if len(entries) <= min_trades:
        start = int(entries[0])
    else:
        start = int(entries[-min_trades])
    n = min(len(s) - start, max_bars)
    n = max(n, 60)
    return n, int((entries >= len(s) - n).sum())


def audit(sid, conn):
    st = E.load(sid, conn)
    df = E.build_data(st)
    fn = create_strategy_function(st['code'])
    full = E.signal(st, df)
    n, trades = window_for(full)
    if not n:
        return dict(sid=sid, trades=0, verdict='NO-TRADES')

    causal = full.copy()
    for t in range(len(df) - n, len(df)):
        s = fn(df.iloc[:t + 1].copy(), st['params'])
        if isinstance(s, tuple):
            s = s[0]
        causal.iloc[t] = float(np.asarray(s)[-1])

    nf = E.net_returns(st, df, full).iloc[-n:]
    nc = E.net_returns(st, df, causal).iloc[-n:]
    rf = float((1 + nf).prod() - 1)
    rc = float((1 + nc).prod() - 1)
    collapse = (1 - rc / rf) if rf else float('nan')
    disagree = float((np.sign(full.iloc[-n:]) != np.sign(causal.iloc[-n:])).mean())
    verdict = ('CLEAN' if collapse < PARTIAL_AT else
               'PARTIAL' if collapse < RETIRE_AT else 'LOOK-AHEAD')
    if rf <= 0:
        verdict += '?'          # can't rank collapse against a losing baseline
    return dict(sid=sid, inst=st['inst'], bars=n, trades=trades,
                full_ret=rf, causal_ret=rc, collapse=collapse,
                disagree=disagree, verdict=verdict)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sids')
    ap.add_argument('--max-inmkt', type=float,
                    help='only sleeves in-market below this fraction (weakest gate coverage)')
    ap.add_argument('--csv', default='causal_audit.csv')
    a = ap.parse_args()

    conn = E._conn()
    ids = [r['id'] for r in conn.execute(
        "SELECT id FROM strategies WHERE status='paper_trading' ORDER BY id")]
    if a.sids:
        want = set(a.sids.split(','))
        ids = [i for i in ids if i in want]

    rows, t0 = [], time.time()
    fh = open(a.csv, 'w', newline='')
    w = None
    for i, sid in enumerate(ids, 1):
        try:
            r = audit(sid, conn)
        except Exception as exc:
            r = dict(sid=sid, verdict=f'ERROR {type(exc).__name__}: {exc}')
        rows.append(r)
        if w is None:
            w = csv.DictWriter(fh, fieldnames=list(r.keys()) if len(r) > 4 else
                               ['sid', 'inst', 'bars', 'trades', 'full_ret',
                                'causal_ret', 'collapse', 'disagree', 'verdict'])
            w.writeheader()
        w.writerow({k: r.get(k) for k in w.fieldnames})
        fh.flush()
        pct = lambda x: 'n/a' if x is None else f'{x*100:+.1f}%'
        coll = r.get('collapse')
        coll_s = 'n/a' if coll is None else f'{coll*100:.0f}%'
        print(f"[{i}/{len(ids)}] {sid:38} {r.get('verdict','?'):12} "
              f"trades={r.get('trades','?'):>3} "
              f"full={pct(r.get('full_ret'))} causal={pct(r.get('causal_ret'))} "
              f"collapse={coll_s}  ({time.time()-t0:.0f}s)", flush=True)
    fh.close()
    print(f"\nwrote {a.csv}")


if __name__ == '__main__':
    main()
