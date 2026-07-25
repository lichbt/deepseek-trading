#!/usr/bin/env python3
"""RECENT30 decay scan across the whole book — every live and retired sleeve.

Reuses evaluate_strategy's reconstruction and recent_entry_decay so the verdicts
are identical to a one-off `evaluate_strategy.py <id>` review, just batched and
sorted. The point is the two mismatch classes:

    live + DECAYED   → candidate to retire (flatten first, see retire_strategy)
    retired + OK     → candidate to restore

Window ends at evaluate_strategy.FULL_END (last completed session), NOT a pinned
date — a stale end date silently changes these verdicts.

    ./venv/bin/python decay_scan.py                  # live + retired
    ./venv/bin/python decay_scan.py --status live    # one cohort
    ./venv/bin/python decay_scan.py --csv out.csv
"""
import argparse
import csv
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import evaluate_strategy as E

STATUS = {'live': 'paper_trading', 'retired': 'retired'}


def scan_one(sid, conn):
    st = E.load(sid, conn)
    if not st:
        raise ValueError('not found in strategies')
    df = E.build_data(st)
    sig = E.signal(st, df)
    net = E.net_returns(st, df, sig)
    m = E.metrics(sig, net)
    d = E.recent_entry_decay(sig, net, st['wf'])
    return {
        'id': sid,
        'status': st['status'],
        'wf': st['wf'],
        'ho': st['ho'],
        'decay': d['status'],
        'entries': d['entries'],
        'in_window': d.get('in_window'),
        'capped_by': d.get('capped_by'),
        'since': str(d['start'].date()) if d.get('start') is not None else '',
        'bars': d.get('bars') or 0,
        'recent_ret': d.get('recent_ret'),
        'recent_gt': d.get('recent_gt'),
        'min_gt': d.get('threshold'),
        'sharpe': m.get('sharpe'),
        'ret_12mo': m.get('r12'),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--status', choices=['live', 'retired', 'both'], default='both')
    ap.add_argument('--csv', help='also write rows to this path')
    a = ap.parse_args()

    wanted = ([STATUS['live'], STATUS['retired']] if a.status == 'both'
              else [STATUS[a.status]])
    conn = E._conn()
    ids = [r['id'] for r in conn.execute(
        'SELECT id FROM strategies WHERE status IN (%s) ORDER BY id'
        % ','.join('?' * len(wanted)), wanted).fetchall()]

    print(f'RECENT30 scan — {len(ids)} sleeve(s), window ends {E.FULL_END}', flush=True)
    rows, errors = [], []
    for i, sid in enumerate(ids, 1):
        try:
            rows.append(scan_one(sid, conn))
            print(f'  [{i}/{len(ids)}] {sid} -> {rows[-1]["decay"]}', flush=True)
        except Exception as exc:
            errors.append((sid, f'{type(exc).__name__}: {exc}'))
            print(f'  [{i}/{len(ids)}] {sid} -> ERROR {exc}', flush=True)
            traceback.print_exc(file=sys.stderr)

    def fmt(r):
        pct = lambda x: 'n/a' if x is None else f'{x*100:+.1f}%'
        num = lambda x: 'n/a' if x is None else f'{x:.2f}'
        tag = 'live   ' if r['status'] == 'paper_trading' else 'retired'
        return (f"{r['decay']:<12}{tag} {r['id']:<38} WF={num(r['wf'])} "
                f"ent={r['entries']:>3} since={r['since']:<10} "
                f"ret={pct(r['recent_ret']):>7} GT={num(r['recent_gt']):>5} "
                f"min={num(r['min_gt']):>5} 12mo={pct(r['ret_12mo']):>7}")

    live_bad = [r for r in rows if r['status'] == 'paper_trading' and r['decay'] == 'DECAYED']
    retired_ok = [r for r in rows if r['status'] == 'retired' and r['decay'] == 'OK']

    print('\n' + '=' * 100)
    print(f'MISMATCH: live but DECAYED  ({len(live_bad)}) — candidates to retire')
    print('=' * 100)
    for r in sorted(live_bad, key=lambda x: (x['recent_gt'] or 0)):
        print(fmt(r))

    print('\n' + '=' * 100)
    print(f'MISMATCH: retired but OK  ({len(retired_ok)}) — candidates to restore')
    print('=' * 100)
    for r in sorted(retired_ok, key=lambda x: -(x['recent_gt'] or 0)):
        print(fmt(r))

    print('\n' + '=' * 100)
    print('FULL BOOK')
    print('=' * 100)
    order = {'DECAYED': 0, 'INSUFFICIENT': 1, 'OK': 2}
    for r in sorted(rows, key=lambda x: (order.get(x['decay'], 9), x['status'], x['id'])):
        print(fmt(r))

    counts = {}
    for r in rows:
        counts[(r['status'], r['decay'])] = counts.get((r['status'], r['decay']), 0) + 1
    print('\nsummary:', {f'{k[0]}/{k[1]}': v for k, v in sorted(counts.items())})
    if errors:
        print(f'\n{len(errors)} sleeve(s) failed to reconstruct:')
        for sid, err in errors:
            print(f'  {sid}: {err}')

    if a.csv:
        with open(a.csv, 'w', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f'\nwrote {a.csv}')


if __name__ == '__main__':
    main()
