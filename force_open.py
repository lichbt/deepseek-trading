#!/usr/bin/env python
"""Force-open paper_trading sleeves at the CURRENT bar, accepting price drift,
via the netting path — a one-shot test of the netting cutover without waiting
for the 21:00 UTC daily close.

  --dry   read-only: compute each sleeve's current signal + size, group by
          instrument, show which same-instrument groups would stack. No orders.
  (real)  place each sleeve's netting delta and persist own_units + live_status.
          ASSUMES THE LIVE TRADERS ARE STOPPED — otherwise they still hold
          in-memory own_units=0 and will double-open on their next bar.

ponytail: throwaway cutover-test harness, not part of the trading loop.
"""
import sys
import sqlite3
import pandas as pd

import live_test as L
from live_test import LiveTrader
from portfolio import _infer_instrument

DRY = '--dry' in sys.argv


def sleeves():
    c = sqlite3.connect('pipeline.db')
    rows = [r[0] for r in c.execute(
        "SELECT id FROM strategies WHERE status='paper_trading'").fetchall()]
    c.close()
    return rows


def evaluate(sid):
    """Mirror run_loop's signal block exactly: fetch candles, ATR, inject
    supplementary if non-standard archetype, run strategy_func, take last bar."""
    lt = LiveTrader(sid, _infer_instrument(sid))
    candles = lt._fetch_candles()
    if len(candles) == 0:
        return None
    bar_time = candles['date'].iloc[-1]
    atr = None
    if len(candles) >= 2:
        tr = pd.concat([
            candles['high'] - candles['low'],
            (candles['high'] - candles['close'].shift(1)).abs(),
            (candles['low'] - candles['close'].shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(lt.best_params.get('atr_window', 14)).mean().iloc[-1]
    signal_df = candles
    if lt.archetype and lt.archetype != 'standard':
        from supplementary_data import inject_supplementary_data
        signal_df = inject_supplementary_data(
            candles, lt.archetype, lt.instrument, lt.instrument2,
            None, None, lt.timeframe)
    signals = lt.strategy_func(signal_df, lt.best_params)
    sig = int(signals.iloc[-1]) if len(signals) else 0
    prev = int(signals.iloc[-2]) if len(signals) >= 2 else 0
    entry = float(candles['close'].iloc[-1])
    units = lt._compute_position_size(atr) if (sig != 0 and atr) else 0
    return lt, bar_time, atr, sig, prev, entry, units


def main():
    print(f"{'DRY RUN (no orders)' if DRY else 'REAL — placing netting deltas'}\n")
    by_inst = {}
    for sid in sleeves():
        try:
            r = evaluate(sid)
        except Exception as e:
            print(f"  ERROR {sid}: {e}")
            continue
        if r is None:
            print(f"  {sid}: no candles")
            continue
        lt, bar_time, atr, sig, prev, entry, units = r
        tag = f"{sid.split('_auto_')[0]}_{sid.split('_')[-1]}"
        print(f"  {lt.instrument:12} {tag:20} signal={sig:+d} units={units:>10} @ {entry}")
        by_inst.setdefault(lt.instrument, []).append((tag, sig, units))
        if not DRY and sig != 0 and atr:
            lt.own_units = 0.0
            lt._place_order_netting(sig, entry, atr)
            lt.prev_signal = prev
            L.save_live_state(sid, lt.current_position, lt.entry_price,
                              bar_time, sig, lt.oanda_trade_id)

    print("\n=== same-instrument groups (≥2 sleeves) ===")
    for inst, lst in sorted(by_inst.items()):
        if len(lst) >= 2:
            signing = [s for s in lst if s[1] != 0]
            net = sum(s[1] * s[2] for s in lst)
            print(f"  {inst:12} {len(lst)} sleeves, {len(signing)} signaling "
                  f"-> net would stack to {net} units  {[ (t,s) for t,s,_ in lst ]}")


if __name__ == '__main__':
    main()
