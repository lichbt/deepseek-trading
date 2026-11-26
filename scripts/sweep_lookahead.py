#!/usr/bin/env python3
"""Run the truncation look-ahead gate over strategies that were validated
BEFORE the gate existed (commit 69971a2, 2026-07-08).

Those rows cleared validation without any causality check, so a positive flip
rate there is not a regression — it is a gate that never ran. Read-only with
respect to strategy status: this REPORTS, it does not retire anything.

  python3 scripts/sweep_lookahead.py                 # pre-gate survivors
  python3 scripts/sweep_lookahead.py --all           # every live/passed row
  python3 scripts/sweep_lookahead.py --out x.json
"""
import argparse
import json
import sys
import traceback
from datetime import datetime, timezone

sys.path.insert(0, '.')

import evaluate_strategy as E
from validator import (create_strategy_function, truncation_lookahead_flip_rate,
                       LOOKAHEAD_MAX_FLIP_RATE)

GATE_ADDED = '2026-07-08'   # 69971a2 Add truncation look-ahead gate to the validator


def targets(conn, all_rows: bool):
    q = """
        select s.id, s.status, s.timeframe, v.tested_at
        from validation_results v join strategies s on s.id = v.strategy_id
        where v.final_status like 'PASS%'
          and s.status in ('passed', 'passed_but_fragile', 'paper_trading')
    """
    if not all_rows:
        q += f" and v.tested_at < '{GATE_ADDED}'"
    return conn.execute(q + " order by s.status, v.tested_at").fetchall()


def check(sid, conn):
    """(flip_rate, n_checked) on the dev window — exactly as evaluate_strategy runs it."""
    st = E.load(sid, conn)
    ddf = E.build_data(st, *E.get_dev_window(st['inst']))
    return truncation_lookahead_flip_rate(
        create_strategy_function(st['code']), ddf, st['params'])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--all', action='store_true', help='include post-gate rows too')
    ap.add_argument('--out', default='lookahead_sweep.json')
    a = ap.parse_args()

    conn = E.connect() if hasattr(E, 'connect') else __import__('sqlite3').connect('pipeline.db')
    conn.row_factory = __import__('sqlite3').Row

    rows = targets(conn, a.all)
    print(f'sweeping {len(rows)} strategies (gate threshold {LOOKAHEAD_MAX_FLIP_RATE:.0%})', flush=True)

    out, fails, untestable = [], 0, 0
    for i, r in enumerate(rows, 1):
        rec = {'id': r['id'], 'status': r['status'], 'tf': r['timeframe'],
               'tested_at': r['tested_at']}
        try:
            rate, n = check(r['id'], conn)
            rec.update(flip_rate=rate, n_checked=n)
            if rate is None:
                rec['verdict'] = 'UNTESTABLE'
                untestable += 1
            elif rate > LOOKAHEAD_MAX_FLIP_RATE:
                rec['verdict'] = 'FAIL'
                fails += 1
            else:
                rec['verdict'] = 'PASS'
        except Exception as e:
            rec.update(verdict='ERROR', error=f'{type(e).__name__}: {e}')
            rec['trace'] = traceback.format_exc()[-400:]
        out.append(rec)
        flip = 'n/a' if rec.get('flip_rate') is None else f"{rec['flip_rate']:.0%}"
        print(f"[{i}/{len(rows)}] {rec['verdict']:10s} flip={flip:>4s} "
              f"{r['status']:18s} {r['id']}", flush=True)

    payload = {'run_at': datetime.now(timezone.utc).isoformat(),
               'gate_added': GATE_ADDED, 'threshold': LOOKAHEAD_MAX_FLIP_RATE,
               'scope': 'all' if a.all else 'pre-gate',
               'n': len(out), 'fails': fails, 'untestable': untestable,
               'results': out}
    with open(a.out, 'w') as f:
        json.dump(payload, f, indent=2)

    print(f'\n=== {fails} FAIL / {untestable} untestable / {len(out)} checked -> {a.out}')
    for r in out:
        if r['verdict'] == 'FAIL':
            print(f"  FAIL {r['flip_rate']:.0%}  {r['status']:18s} {r['id']}")


if __name__ == '__main__':
    main()
