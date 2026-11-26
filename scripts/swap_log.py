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
    _reconcile(by_pos)


# A consecutive-position jump at least this large is read as the broker changing
# the rate, not as noise. Ordinary scatter between positions of the same
# instrument runs a few percent (the residual is where the Friday triple falls);
# a real change is a clean multiple — NAS100 moved 10x. 1.5 sits far above the
# noise and far below any rate change worth reporting.
BREAK_RATIO = 1.5


def _split_at_break(recs: list) -> tuple:
    """Split one instrument's positions into (prior, recent) at a RATE CHANGE.

    A CALENDAR window cannot do this job, and the first version of this code
    tried: with a 30-day recent window, NAS100's cut — which landed ~28 days
    before this was written — sat INSIDE the window, so "recent" still blended
    -35.75 and -3.58 into -8.14 and reported obs/model 2.27. The window has to be
    found in the data, not assumed.

    So: order positions by when they were last seen, take each one's own implied
    rate, and cut at the largest consecutive jump if that jump clears BREAK_RATIO.
    No break found means no split — the whole history is one regime, which is the
    normal case and needs no window at all.
    """
    if len(recs) < 2:
        return [], recs
    rates = [r['charge'] / r['ud'] if r['ud'] else None for r in recs]
    at, biggest = None, 1.0
    for i in range(len(recs) - 1):
        a, b = rates[i], rates[i + 1]
        if not a or not b:
            continue
        ratio = max(a / b, b / a)     # same sign, so this is a clean multiple
        if ratio > biggest:
            biggest, at = ratio, i
    if at is None or biggest < BREAK_RATIO:
        return [], recs
    return recs[:at + 1], recs[at + 1:]


def _agg(recs: list) -> dict | None:
    """Fold per-position records into one implied rate. None if there is nothing."""
    if not recs:
        return None
    charge = sum(r['charge'] for r in recs)
    ud = sum(r['ud'] for r in recs)
    if not ud:
        return None
    return {'rate': charge / ud, 'n': len(recs),
            'days': sum(r['days'] for r in recs),
            'px': recs[0]['px'],
            'first': min(r['first'] for r in recs),
            'last': max(r['last'] for r in recs)}


def _reconcile(by_pos: dict) -> None:
    """Compare what the broker CHARGED against what the simulator MODELS.

    The point of the whole table above. A rate in oanda_book_simulator is either
    MEASURED (it came from these deltas) or DERIVED (it came from the published
    card via swapLong / 10**pipPosition). A derived rate has never been checked
    against money actually leaving the account. The rule behind it is validated at
    pipPosition 0, 2, 4 and 5, and PROVISIONAL at 1 — NATGAS is the only symbol
    sitting at 1 and it is itself an output of the rule, so it cannot validate it.

    SPLIT BY REGIME, NOT AVERAGED (2026-09-07). Broker swap rates are not
    constants. NAS100 was charged -35.7/unit/day through 2026-08-10 and -3.58 from
    2026-09-04 — the broker cut it ~10x — and folding a position's whole history
    into one number turned that into -8.86/u/day at an obs/model of 0.25, which
    describes neither regime and looks like a modelling error rather than a rate
    change. So the headline rate is the rate SINCE THE LAST BREAK (see
    _split_at_break) and the break itself is reported separately instead of
    averaged away. The same blending is why SPX500 read 0.95 over 5.3 days when
    its clean single-roll delta is 1.004.

    Implied rate is charge / (units x calendar days), measured across each
    position's WHOLE observed life — first observation to last — never per window.
    That distinction is the whole correctness of this block, and it still holds
    WITHIN a regime. Swap lands as one discrete charge at the daily roll, but
    observations are sampled every ~3h, so the entire day's charge falls inside one
    3h window: dividing by that window's own length reports the rate ~8x (24/3) too
    high, and the first version of this code did exactly that and flagged all ten
    measured rates as wrong.

    Over a multi-day span the arithmetic comes out: an ordinary instrument is
    charged on weekdays only but takes a 3x Friday roll, and the triple exactly
    compensates the two uncharged weekend days, so charge-days equals calendar-days
    over any whole number of weeks. Short spans still read high or low depending on
    where the Friday falls, which is what the `days` column is for.
    """
    try:
        import oanda_book_simulator as S
    except Exception as exc:                      # pragma: no cover - import guard
        print('\n(model reconciliation skipped: %s)' % exc)
        return

    per_inst: dict = {}
    newest = None
    for obs in by_pos.values():
        first, last = obs[0], obs[-1]
        charge = last['swap_usd'] - first['swap_usd']
        if charge == 0 or not first['instrument'] or not first['units']:
            continue
        t0 = dt.datetime.strptime(first['observed_at'], '%Y-%m-%dT%H:%M:%SZ')
        t1 = dt.datetime.strptime(last['observed_at'], '%Y-%m-%dT%H:%M:%SZ')
        days = (t1 - t0).total_seconds() / 86400
        if days <= 0:
            continue
        per_inst.setdefault(first['instrument'], []).append(
            {'charge': charge, 'ud': abs(first['units']) * days, 'days': days,
             'px': first['entry_price'], 'first': t0, 'last': t1})
        newest = t1 if newest is None or t1 > newest else newest

    if not per_inst:
        print('\nno non-zero deltas yet — nothing to reconcile.')
        return

    breaks = []

    print('\nMODEL RECONCILIATION — observed charge vs the rate the simulator uses')
    print(f'{"instrument":<13}{"pos":>5}{"days":>7}{"obs/u/day (current)":>21}'
          f'{"model/u/day":>14}{"obs/model":>11}  source')
    print('-' * 100)
    for inst, recs in sorted(per_inst.items()):
        pri, rec = _split_at_break(sorted(recs, key=lambda r: r['last']))
        recent, prior = _agg(rec), _agg(pri)
        head = recent or _agg(recs)
        implied = head['rate']
        model = S.SWAP_PER_UNIT_DAY.get(inst)
        src = 'measured'
        if model is None:
            pctv = S.SWAP_PCT_NOTIONAL_DAY.get(inst)
            model = pctv * head['px'] if pctv is not None and head['px'] else None
            src = 'proxy (pct x price)' if model is not None else 'NO RATE — charged 0'
        elif inst in getattr(S, 'SWAP_DERIVED', ()):
            src = 'DERIVED from card — UNCONFIRMED'
        ratio = (implied / model) if model else None
        flag = ''
        if ratio is not None and (ratio > 1.5 or ratio < 0.67):
            flag = '   <<< MODEL DISAGREES'
        if recent and prior and prior['rate']:
            rr = recent['rate'] / prior['rate']
            if rr > 1.5 or rr < 0.67:
                breaks.append((inst, recent, prior, rr))
        print(f'{inst:<13}{head["n"]:>5}{head["days"]:>7.1f}{implied:>21.6g}'
              f'{(("%.6g" % model) if model else "--"):>14}'
              f'{(("%.2f" % ratio) if ratio else "-"):>11}  {src}{flag}')
    print('-' * 100)

    if breaks:
        print('\nREGIME BREAK — the broker changed the rate; these are NOT model errors')
        for inst, recent, prior, rr in breaks:
            print(f'  {inst}')
            print(f'    prior   {prior["rate"]:>12.6g}/u/day  '
                  f'{prior["first"]:%Y-%m-%d}..{prior["last"]:%Y-%m-%d}  '
                  f'{prior["n"]} pos')
            print(f'    recent  {recent["rate"]:>12.6g}/u/day  '
                  f'{recent["first"]:%Y-%m-%d}..{recent["last"]:%Y-%m-%d}  '
                  f'{recent["n"]} pos')
            print(f'    recent/prior {rr:.3f}  — the model must track the RECENT rate')

    missing = sorted(getattr(S, 'SWAP_DERIVED', ()) - set(per_inst))
    if missing:
        print('\nSTILL UNCONFIRMED (no observed accrual yet): %s' % ', '.join(missing))
    print('Rate = charge / (units x calendar days) over each position\'s whole')
    print('observed life, aggregated over the CURRENT regime only — everything since')
    print('the last detected rate break. Short spans read high or low depending on')
    print('where the Friday triple falls — read the days column before trusting one.')


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
