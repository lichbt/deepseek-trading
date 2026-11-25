#!/usr/bin/env python3
"""Prove that a venue symbol we CALL untradeable can actually be routed.

WHY (2026-11-25). `ctrader_symbols.json` derives `unroutable` from a name guess
(`UK100_GBP` -> `UK100GBP`) and filed UK100 that way while the venue listed `UK100`
all along. Presence in the symbol list is still not permission to trade, so this
script does the only test that counts: ONE min-size round trip, opened WITH its stop
attached, read back from the book, then closed by positionId.

Run it where the tokens live and NOTHING else is trading — a second cTrader session
cannot refresh tokens while `fix_runner` holds them, and an order placed while a
runner is live can collide with a pass.

    # pods=0, runner stopped (interlock on), then inside a one-shot container:
    python scripts/probe_uk100_route.py UK100_GBP            # read-only: price + specs
    python scripts/probe_uk100_route.py UK100_GBP --order    # the round trip

Refuses to send when the venue has no price for the symbol (market closed) — a market
order that parks and fills at the open would leave a position nobody is watching.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ctrader_exec import adapter_for          # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('instrument', help='OANDA-style name, e.g. UK100_GBP')
    ap.add_argument('--order', action='store_true', help='actually place the round trip')
    ap.add_argument('--units', type=float, default=0.01,
                    help='units to trade (x100 -> wire volume); 0.01 = venue minimum')
    ap.add_argument('--stop-pct', type=float, default=3.0, help='stop distance, percent')
    ap.add_argument('--symbols', default=None, help='path to ctrader_symbols.json')
    a = ap.parse_args(argv)

    path = a.symbols or os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'ctrader_symbols.json')
    syms = json.load(open(path))
    spec = syms.get('instruments', {}).get(a.instrument)
    if not spec:
        print(f"NO SPEC for {a.instrument} in {path} — add it first")
        return 2

    ad = adapter_for(a.instrument, syms)
    print(f"{a.instrument} -> venue id {ad.symbol_id} min_vol {ad.min_volume} "
          f"step {ad.step_volume} digits {ad.digits}")

    px = ad.get_current_price()
    print('now_utc', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M'), '| price', px)
    if px is None:
        print('ABORT: no price (market closed or symbol halted) — refusing to park an order')
        return 4
    if not a.order:
        print('READ-ONLY: no order sent')
        return 0

    vol_check = ad._to_volume(a.units)
    stop = round(px * (1 - a.stop_pct / 100.0), ad.digits)
    print(f"placing MARKET {'BUY' if a.units > 0 else 'SELL'} volume={vol_check} "
          f"stop={stop} ({a.stop_pct}% away)")
    pos = ad.execute_order(a.units, 'route-probe ' + a.instrument, stop_loss=stop)
    print('positionId', pos)
    if not pos:
        print('NO FILL — nothing appeared in the book; treat routing as UNPROVEN')
        return 3

    time.sleep(1)
    sl = ad.position_sl(pos)
    entry = ad.position_entry(pos)
    print(f"broker reads back: entry {entry} stopLoss {sl} "
          f"({'STOP ATTACHED' if sl else 'NO STOP — ATTACH FAILED'})")
    print('open_ids_before_close', ad.open_pos_ids())

    res = ad.close_position(pos, a.units)
    print('close_result', res)
    time.sleep(1)
    left = ad.open_pos_ids()
    print('open_ids_after_close', left)
    ok = str(pos) not in left and res and res.get('ord_status') == '2'
    print('ROUTE PROVEN' if ok else 'PROBE INCOMPLETE — CHECK THE BOOK BY HAND')
    return 0 if ok else 5


if __name__ == '__main__':
    sys.exit(main())
