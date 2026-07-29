#!/usr/bin/env python3
"""Per-sleeve detail + risk for the live book, in one table.

Joins the reconstruction (Sharpe / maxDD / concentration / directionality /
per-year), the decay verdict, the portfolio weight and Kelly scale, the position
actually held, and whether the sleeve is also live on the FIX prop account.

Risk flags are the prop-firm lens (see DECISIONS.md: 3% daily / 10% static
total DD): a sleeve whose own reconstructed drawdown breaches the account limit,
or whose edge is concentrated in one or two years, is beta wearing a strategy's
clothes.

    ./venv/bin/python sleeve_report.py [--csv out.csv]
"""
import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import evaluate_strategy as E

DD_LIMIT = 0.10          # The5ers/FTMO static total DD
CONC_BETA = 0.60         # top-2-year share above this = regime beta, not edge
LONG_ONLY = 0.95         # >= this fraction long = one-sided


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv')
    a = ap.parse_args()

    state = json.load(open(ROOT / 'portfolio_state.json'))
    W, N = state['weights'], state['n_strategies']
    decay, kelly = state['decay_status'], state.get('decay_kelly_scale', {})
    note = state.get('decay_note', {})
    peers = {}
    for p in state.get('correlated_pairs', []):
        peers.setdefault(p['a'], []).append(p['b'])
        peers.setdefault(p['b'], []).append(p['a'])

    try:
        fix = {k for k, v in json.loads((ROOT / 'fix_runner_state.json').read_text()).items()
               if v.get('pos_id')}
    except Exception:
        fix = set()

    conn = E._conn()
    units = {r['sleeve_id']: r['units'] for r in
             conn.execute('SELECT sleeve_id, units FROM sleeve_units')}
    ids = [r['id'] for r in conn.execute(
        "SELECT id FROM strategies WHERE status IN ('paper_trading','incubating') "
        "ORDER BY id")]

    rows = []
    for i, sid in enumerate(ids, 1):
        try:
            st = E.load(sid, conn)
            df = E.build_data(st)
            sig = E.signal(st, df)
            net = E.net_returns(st, df, sig)
            m = E.metrics(sig, net)
            d = E.recent_entry_decay(sig, net, st['wf'])
            flags = []
            if abs(m['maxdd']) > DD_LIMIT:
                flags.append(f"DD{abs(m['maxdd'])*100:.0f}%")
            if m['conc'] == m['conc'] and m['conc'] > CONC_BETA:
                flags.append(f"conc{m['conc']*100:.0f}%")
            if m['longpct'] >= LONG_ONLY:
                flags.append('long-only')
            elif m['longpct'] <= 1 - LONG_ONLY:
                flags.append('short-only')
            if decay.get(sid) == 'DECAYED':
                flags.append('DECAYED')
            if m['r12'] < 0:
                flags.append('12mo-')
            if peers.get(sid):
                flags.append(f'corr x{len(peers[sid])}')
            rows.append(dict(
                sid=sid, inst=st['inst'], arch=st['arch'],
                wf=st['wf'], ho=st['ho'],
                sharpe=m['sharpe'], maxdd=m['maxdd'], conc=m['conc'],
                posyr=m['posyr'], nyr=m['nyr'], inmkt=m['inmkt'], longpct=m['longpct'],
                r12=m['r12'], r26=m['r26'], tot=m['tot'],
                decay=decay.get(sid, '?'), kelly=kelly.get(sid, 1.0),
                weight=W.get(sid, 0.0), wscale=W.get(sid, 0.0) * N,
                units=units.get(sid), fix=sid in fix,
                note=note.get(sid, ''), flags=' '.join(flags)))
            print(f'  [{i}/{len(ids)}] {sid}', flush=True)
        except Exception as exc:
            print(f'  [{i}/{len(ids)}] {sid} ERROR {exc}', flush=True)

    rows.sort(key=lambda r: -r['weight'])
    pct = lambda x: 'n/a' if x is None or x != x else f'{x*100:+.1f}%'
    print('\n' + '=' * 165)
    print(f"{'instrument':11}{'sleeve':22}{'wt%':>6}{'wtx':>6}{'Kel':>5} "
          f"{'WF':>5}{'HO':>5} {'Shrp':>5}{'maxDD':>7}{'conc':>6}{'+yrs':>6}"
          f"{'inmkt':>6}{'long':>6} {'12mo':>8}{'2026':>8} {'decay':<13}{'pos':>10}  flags")
    print('=' * 165)
    for r in rows:
        sleeve = r['sid'].split('_auto_')[1] if '_auto_' in r['sid'] else r['sid']
        pos = '—' if not r['units'] else f"{r['units']:+.4g}"
        print(f"{r['inst']:11}{sleeve:22}{r['weight']*100:5.2f}%{r['wscale']:6.2f}"
              f"{r['kelly']:5.1f} {r['wf'] or 0:5.2f}{r['ho'] or 0:5.2f} "
              f"{r['sharpe']:5.2f}{r['maxdd']*100:6.0f}%{r['conc']*100:5.0f}%"
              f"{r['posyr']:4d}/{r['nyr']:<2d}{r['inmkt']*100:5.0f}%{r['longpct']*100:5.0f}% "
              f"{pct(r['r12']):>8}{pct(r['r26']):>8} {r['decay']:<13}{pos:>10}"
              f"  {'FIX ' if r['fix'] else ''}{r['flags']}")
    print('=' * 165)
    print(f"{len(rows)} sleeves | total weight {sum(r['weight'] for r in rows)*100:.1f}%"
          f" | on FIX prop: {sum(1 for r in rows if r['fix'])}"
          f" | holding a position: {sum(1 for r in rows if r['units'])}")

    if a.csv:
        with open(a.csv, 'w', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
        print(f'wrote {a.csv}')


if __name__ == '__main__':
    main()
