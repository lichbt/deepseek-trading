#!/usr/bin/env python3
"""Record the swap the broker has ACTUALLY charged on the live prop positions.

READ-ONLY at the broker: issues ProtoOAReconcileReq and nothing else. Places no
order, amends no position, touches no runner state. Safe to run at any time,
including while the pod holds positions and while a trading pass is in flight.

WHY IT RUNS ON THE MAC, NOT THE POD: reading accrued swap needs only a broker
session, so there is no reason to make it a deploy. Running it here means no push,
no interlock, no trading action, and nothing for reset-db to destroy -- reset-db
deletes /data/pipeline.db, so a pod-side log would be wiped by every deploy that
ships a new book.

WHAT A ROW MEANS: position.swap is the running total accrued since the position
opened, NOT the charge for one period. The charge is the DELTA between two
observations of the same position_id -- which is why this appends and never
updates, and why --report reads consecutive pairs rather than single rows.

A position that CLOSES between two runs takes its final swap with it: the last
row recorded is the last observation, not the settled total. Run before a close
(or often enough) if the full lifetime charge matters.

Usage:
    python3 scripts/swap_log.py                 # observe once and append
    python3 scripts/swap_log.py --report        # per-instrument charge from deltas
    python3 scripts/swap_log.py --dry-run       # observe and print, write nothing
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(REPO, 'pipeline.db')
SYMS = os.path.join(REPO, 'ctrader_symbols.json')


def _utc_iso(ms: int) -> str | None:
    if not ms:
        return None
    return dt.datetime.utcfromtimestamp(ms / 1000).strftime('%Y-%m-%dT%H:%M:%SZ')


def observe() -> list[dict]:
    """Read every open position's accrued swap. Read-only."""
    from ctrader_client import get_client
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAReconcileReq

    with open(SYMS) as fh:
        by_id = {v['symbol_id']: k
                 for k, v in json.load(fh)['instruments'].items()}

    cli = get_client().start()
    req = ProtoOAReconcileReq()
    req.ctidTraderAccountId = cli.account_id
    res = cli.send(req)

    now = dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    rows = []
    for pos in res.position:
        td = pos.tradeData
        # moneyDigits governs swap/commission scaling and is NOT the price digits
        mdig = getattr(pos, 'moneyDigits', 2) or 2
        rows.append({
            'observed_at': now,
            'position_id': str(pos.positionId),
            'instrument': by_id.get(td.symbolId),
            'symbol_id': td.symbolId,
            'side': 'BUY' if td.tradeSide == 1 else 'SELL',
            'volume': td.volume,
            'units': td.volume / 100.0,
            'entry_price': pos.price,
            'swap_raw': pos.swap,
            'money_digits': mdig,
            'swap_usd': pos.swap / (10 ** mdig),
            'commission_usd': getattr(pos, 'commission', 0) / (10 ** mdig),
            'opened_at': _utc_iso(getattr(td, 'openTimestamp', 0)),
        })
    return rows


def record(rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = list(rows[0].keys())
    sql = (f'INSERT OR IGNORE INTO broker_swap ({",".join(cols)}) '
           f'VALUES ({",".join("?" * len(cols))})')
    con = sqlite3.connect(DB)
    try:
        # INSERT OR IGNORE, not REPLACE: the table is sealed against DELETE and
        # REPLACE is DELETE+INSERT, so REPLACE would raise. Re-running in the same
        # second is a silent no-op via UNIQUE(position_id, observed_at).
        cur = con.executemany(sql, [[r[c] for c in cols] for r in rows])
        con.commit()
        return cur.rowcount
    finally:
        con.close()


def report() -> None:
    """Per-instrument charge, derived from consecutive observations."""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        'SELECT * FROM broker_swap ORDER BY position_id, observed_at').fetchall()
    con.close()
    if not rows:
        print('no observations yet — run without --report first')
        return

    by_pos: dict[str, list] = {}
    for r in rows:
        by_pos.setdefault(r['position_id'], []).append(r)

    print(f'{"instrument":<13}{"pos_id":<10}{"units":>9}  {"from":<21}{"to":<21}'
          f'{"hrs":>7}{"charge$":>10}{"fri":>5}')
    print('-' * 96)
    for pid, obs in sorted(by_pos.items(), key=lambda kv: kv[1][0]['instrument'] or ''):
        for a, b in zip(obs, obs[1:]):
            delta = b['swap_usd'] - a['swap_usd']
            if delta == 0:
                continue
            t0 = dt.datetime.strptime(a['observed_at'], '%Y-%m-%dT%H:%M:%SZ')
            t1 = dt.datetime.strptime(b['observed_at'], '%Y-%m-%dT%H:%M:%SZ')
            hrs = (t1 - t0).total_seconds() / 3600
            # did a Friday ~21:00 UTC rollover (the 3x charge) fall in the window?
            fri = 'yes' if any(
                (t0 + dt.timedelta(hours=h)).weekday() == 4
                and (t0 + dt.timedelta(hours=h)).hour >= 21
                for h in range(int(hrs) + 1)) else ''
            print(f'{a["instrument"] or "?":<13}{pid:<10}{a["units"]:>9.2f}  '
                  f'{a["observed_at"]:<21}{b["observed_at"]:<21}'
                  f'{hrs:>7.1f}{delta:>10.3f}{fri:>5}')
    print('-' * 96)
    print('charge = delta between consecutive observations of the SAME position.')
    print('"fri" marks a window containing a Friday 21:00 UTC rollover (3x charge).')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--report', action='store_true',
                    help='derive per-period charges from recorded observations')
    ap.add_argument('--dry-run', action='store_true',
                    help='observe and print; write nothing')
    a = ap.parse_args()

    if a.report:
        report()
        return 0

    rows = observe()
    for r in rows:
        print(f'  {r["instrument"] or "sym%s" % r["symbol_id"]:<13} '
              f'{r["position_id"]:<10} {r["side"]:<5} units={r["units"]:<9.2f} '
              f'swap={r["swap_usd"]:>8.2f}  opened={r["opened_at"]}')
    if a.dry_run:
        print(f'[dry-run] {len(rows)} position(s) observed, nothing written')
        return 0
    n = record(rows)
    print(f'recorded {n} new observation(s) of {len(rows)} open position(s)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
