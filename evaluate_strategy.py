#!/usr/bin/env python
"""Deploy-review a strategy in ONE command — the whole manual lens, compact output.

    ./venv/bin/python evaluate_strategy.py <strategy_id> [--split] [--book-corr]

Given a strategy id it prints: the DB record, the LOOK-AHEAD gate verdict, the
real-sized reconstruction (directionality / Sharpe / concentration / per-year /
recent / maxDD), and a CURATION block — same-instrument incumbents compared
head-to-head with correlation, or (for a new instrument) max |corr| vs the book.

Consolidates the review logic that used to live in throwaway inline bash so the
command text stops re-entering context every review (see CLAUDE.md bash rule).

Flags:
  --split      also split long-leg vs short-leg P&L (for two-sided strategies)
  --book-corr  force full-book correlation even when same-instrument incumbents exist
"""
import argparse
import json
import sqlite3
import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from validator import (create_strategy_function, get_dev_window,
                       truncation_lookahead_flip_rate, LOOKAHEAD_MAX_FLIP_RATE)
from data_fetcher import get_candles_date_range
from supplementary_data import inject_supplementary_data
from pipeline_utils import compute_net_strategy_returns, compute_gt_score
from auto_research import _infer_archetype
# One source of truth for the decay window: portfolio.py owns it because its
# verdict drives live weights/Kelly, and this tool must agree with it exactly.
from portfolio import (_infer_instrument, recent_decay_window,
                       RECENT_DECAY_ENTRIES, RECENT_DECAY_GT_FRACTION,
                       RECENT_DECAY_MAX_MONTHS, RECENT_DECAY_MIN_ENTRIES,
                       RECENT_DECAY_NEAR_MISS_FRACTION)

DB = ROOT / 'pipeline.db'
FULL_START = '2015-01-01'
# End at the last completed session, NOT a hard-coded date. A pinned FULL_END
# silently scores every later run on stale data: the 2026-07-07 pin was still in
# place on 2026-07-25, so a RECENT30 decay check missed 18 days and read a copper
# sleeve at GT 0.26 (marginal pass) when the current window put it at 0.18 (fail).
# Yesterday, not today — OANDA rejects a `to` timestamp in the future.
FULL_END = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')


def _conn():
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    return c


def load(sid, c):
    """Return dict with everything needed to reconstruct, or None if not found."""
    s = c.execute('SELECT code, timeframe, rationale, status, archetype, instrument2 '
                  'FROM strategies WHERE id=?', (sid,)).fetchone()
    if not s:
        return None
    v = c.execute('SELECT is_gt_score, walk_forward_gt_score, holdout_gt_score, '
                  'final_status, best_params FROM validation_results WHERE strategy_id=?',
                  (sid,)).fetchone()
    bp = json.loads(v['best_params']) if v and v['best_params'] else {}
    return dict(id=sid, code=s['code'], tf=s['timeframe'] or 'D', rationale=s['rationale'],
                status=s['status'], instrument2=s['instrument2'],
                arch=_infer_archetype(s['code'] or '', s['archetype'] or 'standard'),
                inst=_infer_instrument(sid), params=bp,
                is_s=(v['is_gt_score'] if v else None),
                wf=(v['walk_forward_gt_score'] if v else None),
                ho=(v['holdout_gt_score'] if v else None),
                final=(v['final_status'] if v else None))


_CANDLE_CACHE = {}


def build_data(st, start=FULL_START, end=FULL_END):
    """OHLC (+ injected archetype columns) with a proper DatetimeIndex."""
    key = (st['inst'], st['arch'], st['instrument2'], start, end, st['tf'])
    if key in _CANDLE_CACHE:
        return _CANDLE_CACHE[key]
    df = get_candles_date_range(st['inst'], start, end, st['tf'])
    if 'date' in df.columns:
        df = df.set_index(pd.DatetimeIndex(pd.to_datetime(df['date'])))
    if st['arch'] != 'standard':
        df = inject_supplementary_data(df, st['arch'], st['inst'], st['instrument2'],
                                       start, end, st['tf'])
        if not isinstance(df.index, pd.DatetimeIndex) and 'date' in df.columns:
            df = df.set_index(pd.DatetimeIndex(pd.to_datetime(df['date'])))
    _CANDLE_CACHE[key] = df
    return df


def signal(st, df):
    s = create_strategy_function(st['code'])(df.copy(), st['params'])
    if isinstance(s, tuple):
        s = s[0]
    return pd.Series(np.asarray(s), index=df.index).fillna(0)


def net_returns(st, df, sig):
    """Net returns aligned to df.index — handles the stop-path positional RangeIndex."""
    net = compute_net_strategy_returns(df, sig, st['inst'], st['tf'],
                                       st['params'] if st['params'] else None)
    if not isinstance(net.index, pd.DatetimeIndex):
        net = pd.Series(np.asarray(net), index=df.index[-len(net):])
    return net.reindex(df.index).fillna(0)


def _maxdd(r):
    eq = (1 + r).cumprod()
    return float((eq / eq.cummax() - 1).min())


def metrics(sig, net):
    longs, shorts = int((sig > 0).sum()), int((sig < 0).sum())
    ann, vol = net.mean() * 252, net.std() * np.sqrt(252)
    yr = (1 + net).groupby(net.index.year).prod() - 1
    tot = (1 + net).prod() - 1
    top2 = yr.sort_values(ascending=False).iloc[:2].sum()
    r12 = (1 + net[net.index >= net.index.max() - pd.Timedelta(days=365)]).prod() - 1
    r26 = (1 + net[net.index >= '2026-01-01']).prod() - 1
    return dict(inmkt=(sig != 0).mean(), longpct=(longs / (longs + shorts) if longs + shorts else 0),
                longs=longs, shorts=shorts, sharpe=(ann / vol if vol else 0), tot=tot,
                conc=(top2 / tot if tot > 0 else float('nan')), posyr=int((yr > 0).sum()),
                nyr=len(yr), r12=r12, r26=r26, maxdd=_maxdd(net), vol=vol, yr=yr)


def _fmt(k, m):
    return (f"{k:6} in-mkt {m['inmkt']*100:3.0f}%  long {m['longpct']*100:3.0f}%  "
            f"Sharpe {m['sharpe']:5.2f}  totRet {m['tot']*100:6.0f}%  conc {m['conc']*100:3.0f}%  "
            f"+yrs {m['posyr']:2d}/{m['nyr']}  12mo {m['r12']*100:+5.1f}%  26 {m['r26']*100:+5.1f}%  "
            f"maxDD {m['maxdd']*100:4.0f}%")


def distinct_entry_indices(sig):
    s = pd.Series(np.asarray(sig)).fillna(0)
    return s.index[(s != 0) & (s != s.shift(1).fillna(0))].to_list()


def recent_entry_decay(sig, net, baseline_gt):
    w = recent_decay_window(sig)
    if w['start'] is None or w['in_window'] < RECENT_DECAY_MIN_ENTRIES:
        return dict(status='INSUFFICIENT', entries=w['entries'],
                    in_window=w['in_window'], capped_by=w['capped_by'], threshold=None)
    start = w['start']
    recent = net.iloc[start:].dropna()
    recent_gt = compute_gt_score(recent)
    recent_ret = float((1 + recent).prod() - 1) if len(recent) else 0.0
    threshold = max(0.0, (baseline_gt or 0.0) * RECENT_DECAY_GT_FRACTION)
    decayed = recent_ret <= 0 or recent_gt < threshold
    status, near_miss = ('DECAYED' if decayed else 'OK'), False
    if decayed and recent_ret > 0 and threshold > 0 \
            and recent_gt >= threshold * RECENT_DECAY_NEAR_MISS_FRACTION:
        status, near_miss = 'INSUFFICIENT', True
    return dict(status=status, near_miss=near_miss, entries=w['entries'],
                in_window=w['in_window'], capped_by=w['capped_by'],
                threshold=threshold, recent_gt=recent_gt, recent_ret=recent_ret,
                start=net.index[start], bars=len(recent))


def _fmt_decay(d):
    tag = f'RECENT{RECENT_DECAY_ENTRIES}/{RECENT_DECAY_MAX_MONTHS}mo'
    if d['status'] == 'INSUFFICIENT' and not d.get('near_miss'):
        return (f"{tag} {d['status']} in-window={d['in_window']}/"
                f"{RECENT_DECAY_MIN_ENTRIES} (of {d['entries']} lifetime)")
    # A near miss keeps every number — it was scored, it just landed in the noise.
    if d.get('near_miss'):
        tag += ' NEAR-MISS'
    return (f"{tag} {d['status']} entries={d['in_window']}/{d['entries']} "
            f"since={d['start'].date()} [{d['capped_by']}] bars={d['bars']} "
            f"ret={d['recent_ret']*100:+.1f}% "
            f"GT={d['recent_gt']:.2f} minGT={d['threshold']:.2f}")


def incumbents(inst, sid, c):
    """Same-instrument sleeves currently in the paper book (excluding this one)."""
    out = []
    for r in c.execute("SELECT id FROM strategies WHERE status='paper_trading'").fetchall():
        if r['id'] != sid and _infer_instrument(r['id']) == inst:
            out.append(r['id'])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('sid')
    ap.add_argument('--split', action='store_true', help='split long vs short P&L')
    ap.add_argument('--book-corr', action='store_true', help='force full-book correlation')
    a = ap.parse_args()

    c = _conn()
    st = load(a.sid, c)
    if not st:
        print(f'NOT FOUND: {a.sid}'); return
    print(f"=== {a.sid} ===")
    print(f"status={st['status']}  tf={st['tf']}  archetype={st['arch']}"
          + (f"  instrument2={st['instrument2']}" if st['instrument2'] else ""))
    print(f"rationale: {st['rationale']}")
    sc = lambda x: f'{x:.2f}' if x is not None else 'n/a'
    print(f"IS/WF/HO: {sc(st['is_s'])}/{sc(st['wf'])}/{sc(st['ho'])}   {st['final']}")

    df = build_data(st)
    sig = signal(st, df)
    net = net_returns(st, df, sig)

    # LOOK-AHEAD gate (dev-window data, as the validator runs it)
    ddf = build_data(st, *get_dev_window(st['inst']))
    rate, n = truncation_lookahead_flip_rate(create_strategy_function(st['code']), ddf, st['params'])
    verd = ('n/a (untestable)' if rate is None
            else ('FAIL' if rate > LOOKAHEAD_MAX_FLIP_RATE else 'PASS'))
    print(f">>> LOOK-AHEAD: flip={'n/a' if rate is None else f'{rate:.0%}'} of {n} -> {verd}")

    print("\n-- reconstruction (full-history, at best_params + live stop) --")
    m = metrics(sig, net)
    print(_fmt('CAND', m))
    print(_fmt_decay(recent_entry_decay(sig, net, st['wf'])))
    print("per-year:", {int(y): round(x * 100) for y, x in m['yr'].items()})

    if a.split:
        print("\n-- long/short split --")
        for name, ss in [('LONG', sig.where(sig > 0, 0)), ('SHORT', sig.where(sig < 0, 0))]:
            print(_fmt(name, metrics(ss, net_returns(st, df, ss))))

    inc = incumbents(st['inst'], a.sid, c)
    if inc and not a.book_corr:
        print(f"\n-- CURATION: {len(inc)} same-instrument incumbent(s) --")
        for iid in inc:
            ist = load(iid, c)
            idf = build_data(ist); isig = signal(ist, idf); inet = net_returns(ist, idf, isig)
            print(_fmt(iid.split('_auto_')[0] + '/' + iid[-3:], metrics(isig, inet)))
            print("       " + _fmt_decay(recent_entry_decay(isig, inet, ist['wf'])))
            al = pd.DataFrame({'a': net, 'b': inet}).dropna()
            isig_aligned = isig.reindex(sig.index).fillna(0)
            mask = (sig != 0) & (isig_aligned != 0)
            both = al['a'][mask].corr(al['b'][mask]) if mask.sum() > 10 else float('nan')
            sd = ((np.sign(sig) == np.sign(isig_aligned)) & mask).sum() / mask.sum() if mask.sum() else 0
            print(f"       corr vs CAND: full {al['a'].corr(al['b']):+.2f}  both-in-mkt {both:+.2f}  same-dir {sd:.0%}")
    else:
        print("\n-- CURATION: new instrument — correlation vs whole book --")
        cors = []
        for r in c.execute("SELECT id FROM strategies WHERE status='paper_trading'").fetchall():
            if r['id'] == a.sid:
                continue
            try:
                bst = load(r['id'], c); bdf = build_data(bst)
                bsig = signal(bst, bdf)
                bnet = net_returns(bst, bdf, bsig)
                al = pd.DataFrame({'a': net, 'b': bnet}).dropna()
                if len(al) > 50:
                    cors.append((al['a'].corr(al['b']), r['id'], bst, bsig, bnet))
            except Exception:
                pass
        cors.sort(key=lambda x: -abs(x[0]))
        print("  top |corr|:", [(round(float(cr), 2), _infer_instrument(iid)) for cr, iid, *_ in cors[:5]])
        print("  max |corr|:", round(float(max(abs(cr) for cr, *_ in cors)), 2) if cors else 'n/a')
        for cr, iid, bst, bsig, bnet in cors[:5]:
            print(f"       {_infer_instrument(iid)}/{iid[-3:]} corr={cr:+.2f}  " +
                  _fmt_decay(recent_entry_decay(bsig, bnet, bst['wf'])))


if __name__ == '__main__':
    main()
