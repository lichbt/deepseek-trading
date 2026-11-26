#!/usr/bin/env python3
"""Realized P&L per sleeve on the cTrader PROP account, from broker deal history.

WHY THIS EXISTS. sleeve_report.py and sleeve_equity are the PAPER book: scale-free
reconstruction and simulated units. Neither can answer "what did THIS sleeve actually
make on the prop account" -- only the venue can, and until now the venue was never
asked. ctrader_client.get_deals / get_orders are the read-only half; this is the
report on top.

ATTRIBUTION. Deals carry no sleeve. They carry orderId, and ProtoOAOrder carries
clientOrderId, which ctrader_exec.execute_order stamps with fix_ + sleeve_id. The
`label` field cannot be used for this: it exists on the request only and has no
counterpart on ProtoOAOrder, so it never comes back. Orders placed BEFORE that stamp
shipped are attributed only by symbol and land in an unattributed/SYMBOL row -- a
data limit, not a bug in this report.

    ./venv/bin/python sleeve_prop_pnl.py            # last 90 days
    ./venv/bin/python sleeve_prop_pnl.py --days 400
    ./venv/bin/python sleeve_prop_pnl.py --json out.json
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import ctrader_client

WINDOW_DAYS = 6                 # documented deal-list ceiling is one week
DAY_MS = 86400000

def fetch(client, since_ms):
    """(deals, order_owner) over [since_ms, now], chunked so no window is dropped.

    A window that reports hasMore is split in half and re-fetched; that flag is the
    only signal that rows were silently dropped for being outside the range."""
    now_ms = int(time.time() * 1000)
    owner, deals = {}, []
    start = since_ms
    while start < now_ms:
        end = min(start + WINDOW_DAYS * DAY_MS, now_ms)
        try:
            for o in client.get_orders(start, end):
                if o['client_order_id']:
                    owner[o['order_id']] = o['client_order_id']
            chunk, more = client.get_deals(start, end)
        except Exception as exc:      # one bad window must not kill the whole report
            print('  window %s..%s FAILED: %s' % (start, end, exc), file=sys.stderr)
            start = end
            continue
        if more and end - start > DAY_MS:
            mid = start + (end - start) // 2
            tail, _ = client.get_deals(mid, end)
            seen = set(d['deal_id'] for d in tail)
            chunk = [d for d in chunk if d['deal_id'] not in seen] + tail
        deals.extend(chunk)
        start = end
    return deals, owner

def book_labels(root=None):
    """ctrader symbol name -> label for the LIVE BOOK, e.g. XAGUSD -> 'XAG_USD',
    DAX40 -> 'DE30_EUR +2'. Symbol-level is the most the broker can give us for a
    historical deal: `label` on the order request never comes back on ProtoOAOrder,
    and the broker's own clientOrderId is server-generated numbers. A symbol with
    more than one sleeve on the book is genuinely ambiguous until orders start
    carrying our own clientOrderId."""
    root = root or os.path.dirname(os.path.abspath(__file__))
    try:
        instruments = json.load(open(os.path.join(root, 'ctrader_symbols.json')))['instruments']
        conn = sqlite3.connect(os.path.join(root, 'pipeline.db'))
        book = {}
        for sid, inst in conn.execute(
                "SELECT id, instrument FROM strategies "
                "WHERE status IN ('paper_trading', 'incubating')"):
            book.setdefault(inst, []).append(sid)
        out = {}
        for inst, meta in instruments.items():
            n = len(book.get(inst, []))
            if not n:
                continue
            out[meta['ctrader_name']] = inst if n == 1 else '%s +%d' % (inst, n - 1)
        return out
    except Exception:
        return {}

def net_of(d):
    """Deposit-currency net for one CLOSE deal. Positive is profit."""
    return (d['gross_profit'] + d['swap'] + d['commission']
            + d['close_commission'] + d['pnl_conversion_fee'])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=90)
    ap.add_argument('--json')
    a = ap.parse_args()

    since_ms = int((time.time() - a.days * 86400) * 1000)
    client = ctrader_client.get_client().start()
    trader = client.get_trader()
    names = client.get_symbols()
    labels = book_labels()
    deals, owner = fetch(client, since_ms)

    print('account %s | balance %.2f | %s orders with a broker-assigned clientOrderId, '
          '%s carrying a fix_ label | %s deals (%s closes) | since %s'
          % (trader['account_id'], trader['balance'], len(owner),
             sum(1 for v in owner.values() if v.startswith('fix_')), len(deals),
             sum(1 for d in deals if d['closed']),
             datetime.fromtimestamp(since_ms / 1000, timezone.utc).strftime('%Y-%m-%d')))

    per = defaultdict(lambda: defaultdict(float))
    for d in deals:
        if not d['closed']:
            continue
        cid = owner.get(d['order_id'])
        if cid and cid.startswith('fix_'):
            key = 'sleeve ' + cid[4:]
        else:
            sym = names.get(d['symbol_id'], d['symbol_id'])
            key = 'unattributed/' + str(sym) + '(' + labels.get(str(sym), '?') + ')'
        t = d['close_ts'] or d['exec_ts']
        r = per[key]
        r['trade'] += 1
        r['gross'] += d['gross_profit']
        r['swap'] += d['swap']
        r['comm'] += d['commission'] + d['close_commission']
        r['fee'] += d['pnl_conversion_fee']
        r['net'] += net_of(d)
        r['first'] = min(r.get('first', t), t)
        r['last'] = max(r.get('last', t), t)

    rows = sorted(per.items(), key=lambda kv: -kv[1]['net'])
    head = ('%-34s%4s%11s%10s%10s%11s  %-10s%-10s'
            % ('sleeve/symbol', 'trd', 'gross', 'swap', 'comm', 'net', 'first', 'last'))
    print('')
    print(head)
    print('-' * len(head))
    for key, r in rows:
        d1 = datetime.fromtimestamp(r['first'] / 1000, timezone.utc).strftime('%Y-%m-%d')
        d2 = datetime.fromtimestamp(r['last'] / 1000, timezone.utc).strftime('%Y-%m-%d')
        print('%-34s%4d%+11.2f%+10.2f%+10.2f%+11.2f  %-10s%-10s'
              % (key[:33], int(r['trade']), r['gross'], r['swap'], r['comm'],
                 r['net'], d1, d2))
    print('-' * len(head))
    net = sum(r['net'] for _, r in rows)
    print('%s rows | total net P&L %+.2f | balance %.2f | gross %+.2f | cost %.2f'
          % (len(rows), net, trader['balance'],
             sum(r['gross'] for _, r in rows),
             sum(r['swap'] + r['comm'] + r['fee'] for _, r in rows)))
    print('attributed %d rows, unattributed %d rows (deals with no fix_ order label)'
          % (sum(1 for k, _ in rows if k.startswith('sleeve')),
             sum(1 for k, _ in rows if k.startswith('unattributed'))))

    check = sum(net_of(d) for d in deals if d['closed'])
    if abs(check - net) >= 0.01:
        raise SystemExit('RECONCILE FAIL: per-row %+.2f vs deals %+.2f' % (net, check))

    if a.json:
        json.dump({k: dict(v) for k, v in rows}, open(a.json, 'w'), indent=1)
        print('wrote ' + a.json)

if __name__ == '__main__':
    main()
