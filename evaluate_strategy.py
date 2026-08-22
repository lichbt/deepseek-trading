#!/usr/bin/env python
"""Deploy-review a strategy in ONE command — the whole manual lens, compact output.

    ./venv/bin/python evaluate_strategy.py <strategy_id> [--split] [--book-corr]

Given a strategy id it prints: the DB record, the LOOK-AHEAD gate verdict, the
real-sized reconstruction (directionality / Sharpe / concentration / per-year /
recent / maxDD), and a CURATION block — same-instrument incumbents compared
head-to-head with correlation, or (for a new instrument) max |corr| vs the book.
Both curation paths report FULL-SAMPLE and BOTH-IN-MARKET correlation plus the
same-direction %. Read the both-in-market pair: full-sample is diluted by the
in-market fraction and understates real overlap on selective sleeves.

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
    # Both are RETURNS, not drawdowns — maxdd below is the only drawdown here, and
    # the three sit side by side in _fmt where a reader can easily read a negative
    # 12mo as a drawdown figure. The labels say "ret" for that reason.
    r12 = (1 + net[net.index >= net.index.max() - pd.Timedelta(days=365)]).prod() - 1
    # Year-to-date, derived from the data's own last year. This was hardcoded to
    # '2026-01-01', which reads correctly only during 2026 — in 2027 it would have
    # silently become "return since 2 years ago" under a column still labelled for
    # one year. Same failure as the pinned FULL_END the sleeve-ops skill warns about.
    ytd_year = int(net.index.max().year)
    rytd = (1 + net[net.index.year == ytd_year]).prod() - 1
    return dict(inmkt=(sig != 0).mean(), longpct=(longs / (longs + shorts) if longs + shorts else 0),
                longs=longs, shorts=shorts, sharpe=(ann / vol if vol else 0), tot=tot,
                conc=(top2 / tot if tot > 0 else float('nan')), posyr=int((yr > 0).sum()),
                nyr=len(yr), r12=r12, rytd=rytd, ytd_year=ytd_year,
                maxdd=_maxdd(net), vol=vol, yr=yr)


def _fmt(k, m):
    return (f"{k:6} in-mkt {m['inmkt']*100:3.0f}%  long {m['longpct']*100:3.0f}%  "
            f"Sharpe {m['sharpe']:5.2f}  totRet {m['tot']*100:6.0f}%  conc {m['conc']*100:3.0f}%  "
            f"+yrs {m['posyr']:2d}/{m['nyr']}  12mo ret {m['r12']*100:+5.1f}%  "
            f"{str(m['ytd_year'])[-2:]}ytd ret {m['rytd']*100:+5.1f}%  "
            f"maxDD {m['maxdd']*100:4.0f}%")


def distinct_entry_indices(sig):
    s = pd.Series(np.asarray(sig)).fillna(0)
    return s.index[(s != 0) & (s != s.shift(1).fillna(0))].to_list()


# Params that a generated strategy uses to mean "hold at most this many bars".
# 'timeout' is the same idea under a different name — audusd_auto_20260806_110126_i15
# calls it that, and missing the alias hid the defect below on a DEPLOYED sleeve.
_HOLD_CAP_PARAMS = ('max_hold', 'timeout', 'hold_bars', 'max_bars')


def hold_cap_check(sig, params):
    """Does a declared max-hold parameter actually BIND on the reconstructed signal?

    Found 2026-08-22. The common generated shape is a loop over entry indices that
    slice-assigns the position array:

        for i in np.flatnonzero(raw):
            end = min(i + max_hold, len(df))
            ...
            pos[i:end] = direction

    Consecutive entries CHAIN — each new entry extends the run by up to max_hold
    more bars and overwrites past the cross-back exit — so the effective behaviour
    is "hold while the entry condition persists, plus a max_hold tail", not a cap.
    Measured overshoots: usdjpy_auto_20260822_023306_i18 ran 80 bars against a
    declared 15, xagusd_auto_20260719_072203_i16 52 against 15, and
    audusd_auto_20260806_110126_i15 20 against a 'timeout' of 5.

    This is NOT a bug to fix. Measured across all four affected LIVE sleeves
    2026-08-22, against two arms — "hard cap" (exit at k, stay flat for the rest of
    the directional episode) and "cap+reenter" (exit at k, sit out one bar, resume
    while the signal holds, which is the faithful reading of enforcing the param):

        sleeve            cap   as-validated      hard cap       cap+reenter
        gbpusd_..._i3      8    1.03 / -9.0%    0.85 / -7.3%    0.93 / -9.8%
        xauusd_..._i5      3    0.66 / -7.9%    0.49 / -5.4%    0.55 / -7.0%
        audusd_..._i15     5    0.82 / -9.3%    0.60 / -9.1%    0.69 / -8.5%
        xagusd_..._i16    15    0.77 / -25.3%   0.84 / -16.8%   0.64 / -28.9%

    Three of the four are worse capped under BOTH arms — the optimiser searched the
    parameter with the chaining in place, so those extended holds ARE the edge.

    The xagusd row is the trap. It looks like a win under "hard cap" and is WORSE
    under "cap+reenter", so the gain is not from honouring max_hold at all — it is
    from being forced FLAT for the rest of the episode. That is a different rule
    (one entry per directional episode), not this parameter, and it would need its
    own walk-forward and holdout before it went anywhere near a live sleeve.

    So this reports, it does not judge: the point is only that the sleeve being
    validated is not the sleeve the parameter describes. Never re-cap a DEPLOYED
    sleeve — it is a trading change — and if you measure, measure BOTH arms, because
    one arm alone pointed the wrong way on the only sleeve that looked fixable.

    Caveat on all of the above: net_returns does not charge swap, and capping cuts
    xagusd's in-market bars 1827 -> 1367. XAG carries heavily enough to sit on the
    weekend-flat leg, so its capped arm is understated here by an uncounted amount.

    Returns None when no cap parameter is present.
    """
    key = next((k for k in _HOLD_CAP_PARAMS if k in (params or {})), None)
    if key is None:
        return None
    try:
        cap = int(params[key])
    except (TypeError, ValueError):
        return None
    if cap <= 0:
        return None

    p = np.asarray(sig).astype(int)
    if not len(p):
        return None
    runs, cur, n = [], p[0], 1
    for x in p[1:]:
        if x == cur:
            n += 1
        else:
            runs.append((cur, n)); cur, n = x, 1
    runs.append((cur, n))
    held = [n for v, n in runs if v != 0]
    if not held:
        return None
    over = sum(1 for n in held if n > cap)
    return {'param': key, 'cap': cap, 'max_run': max(held), 'runs': len(held),
            'over': over,
            'verdict': 'BINDS' if over == 0 else 'DOES NOT BIND (entries chain)'}


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


def pair_correlation(sig_a, net_a, sig_b, net_b, min_both=30):
    """Correlation between two sleeves, measured BOTH ways.

    Returns {'full', 'both_in_mkt', 'same_dir', 'n_both'}.

    `full` is the ordinary full-sample correlation of the two net-return series.
    It is the WRONG measure for a selective sleeve: a flat sleeve returns 0 and
    contributes nothing to covariance, so a pair that moves together whenever it
    is exposed reads uncorrelated over a history where it is mostly flat.

    `both_in_mkt` restricts to bars where BOTH sleeves hold a position, and
    `same_dir` is the fraction of those bars where they hold the SAME sign. That
    pair is what the curation decision rests on — it is the condition
    `_corr_scale` acts on live, and it is what settled the 2026-08-01 GBP_USD
    swap (full +0.11/+0.28 vs both-in-market +0.52/+0.64 at 96-97% same-dir).

    `both_in_mkt` is NaN below `min_both` overlapping bars rather than a
    small-sample number that would sort noise to the top of a ranking. Callers
    use 10 for a head-to-head against a named incumbent (where the pair is
    already chosen and any signal is informative) and 30 when RANKING a whole
    book. `same_dir` is still reported below the floor — it is a proportion, not
    a correlation, and stays readable on few bars.
    """
    al = pd.DataFrame({'a': net_a, 'b': net_b}).dropna()
    if al.empty:
        return {'full': float('nan'), 'both_in_mkt': float('nan'),
                'same_dir': 0.0, 'n_both': 0}
    sig_b_al = sig_b.reindex(sig_a.index).fillna(0)
    live = (sig_a != 0) & (sig_b_al != 0)
    mask = live.reindex(al.index).fillna(False)
    n_both = int(mask.sum())
    both = (al['a'][mask].corr(al['b'][mask])
            if n_both >= min_both else float('nan'))
    same = float(((np.sign(sig_a) == np.sign(sig_b_al)) & live).sum() / n_both) \
        if n_both else 0.0
    return {'full': float(al['a'].corr(al['b'])), 'both_in_mkt': float(both),
            'same_dir': same, 'n_both': n_both}


def record_evaluation(conn, strategy_id, m, decay, verdict, start, end, notes=None):
    """Append one evaluation row.

    `verdict` is machine-generated and records what the gates measured; `notes`
    is the human conclusion (why it was rejected, what it duplicates, whether the
    instrument is even routable). The table is sealed against UPDATE, so a note
    is fixed at insert — a later opinion is recorded as a NEW evaluation.
    """
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute('''
            INSERT INTO evaluations (
                strategy_id, run_at, window_start, window_end,
                recent_gt, gt_floor, decay_status, near_miss,
                entries_in_window, entries_lifetime, capped_by,
                r12, sharpe, maxdd, inmkt, tot_return,
                verdict, notes, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'live')
        ''', (
            strategy_id,
            datetime.now(timezone.utc).isoformat(),
            start, end,
            decay.get('recent_gt'), decay.get('threshold'),
            decay.get('status'),
            1 if decay.get('near_miss') else 0,
            decay.get('in_window'), decay.get('entries'),
            decay.get('capped_by'),
            m.get('r12'), m.get('sharpe'), m.get('maxdd'),
            m.get('inmkt'), m.get('tot'),
            verdict, notes,
        ))
        conn.commit()
    except Exception as e:
        print(f"WARNING: failed to record evaluation: {e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('sid')
    ap.add_argument('--split', action='store_true', help='split long vs short P&L')
    ap.add_argument('--book-corr', action='store_true', help='force full-book correlation')
    ap.add_argument('--no-record', action='store_true', help='suppress writing to evaluations table')
    ap.add_argument('--note', default=None,
                    help='free-text conclusion stored on the evaluation row '
                         '(why rejected / what it duplicates / venue blocker)')
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

    hc = hold_cap_check(sig, st['params'])
    if hc:
        print(f">>> HOLD CAP: {hc['param']}={hc['cap']} but longest single-direction "
              f"run is {hc['max_run']} ({hc['over']}/{hc['runs']} runs over) -> {hc['verdict']}")

    print("\n-- reconstruction (full-history, at best_params + live stop) --")
    m = metrics(sig, net)
    decay = recent_entry_decay(sig, net, st['wf'])
    print(_fmt('CAND', m))
    print(_fmt_decay(decay))
    print("per-year:", {int(y): round(x * 100) for y, x in m['yr'].items()})

    lookahead_summary = f"LOOKAHEAD={verd} DECAY={decay['status']}"
    if not a.no_record:
        record_evaluation(c, a.sid, m, decay, lookahead_summary, FULL_START, FULL_END,
                          notes=a.note)

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
            pc = pair_correlation(sig, net, isig, inet, min_both=10)
            print(f"       corr vs CAND: full {pc['full']:+.2f}  "
                  f"both-in-mkt {pc['both_in_mkt']:+.2f}  same-dir {pc['same_dir']:.0%}")
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
        # Rank on BOTH-IN-MARKET correlation, not full-sample. Full-sample is diluted
        # by the in-market fraction — a flat sleeve contributes 0 to covariance — so a
        # selective pair reads uncorrelated over the full history while going the same
        # way whenever it is actually exposed. Measured 2026-08-04 on the live book:
        # 0 pairs exceed 0.5 full-sample, 12 do both-in-market (worst +0.85 at 100%
        # same-direction). This branch previously reported full-sample ONLY, so the
        # measure that decided the 2026-08-01 GBP_USD swap had to be recalled by hand.
        # The same-instrument branch above has always printed it; this is parity.
        scored = []
        for cr, iid, bst, bsig, bnet in cors:
            # min_both=30, not the incumbent branch's 10: this RANKS a whole book,
            # and a 10-bar correlation would sort noise to the top.
            pc = pair_correlation(sig, net, bsig, bnet, min_both=30)
            scored.append((pc['full'], pc['both_in_mkt'], pc['same_dir'],
                           pc['n_both'], iid, bst, bsig, bnet))
        # NaN both-in-market (too few overlapping bars) sorts last, never first.
        scored.sort(key=lambda x: -(abs(x[1]) if x[1] == x[1] else -1))
        finite = [s[1] for s in scored if s[1] == s[1]]
        print("  top |corr| both-in-mkt:",
              [(round(float(b), 2), _infer_instrument(i)) for _, b, _, _, i, *_ in scored[:5]
               if b == b])
        print("  max |corr|: full %s  both-in-mkt %s" % (
            round(float(max(abs(cr) for cr, *_ in cors)), 2) if cors else 'n/a',
            round(float(max(abs(b) for b in finite)), 2) if finite else 'n/a'))
        for cr, both, sd, n_both, iid, bst, bsig, bnet in scored[:5]:
            bstr = f"{both:+.2f}" if both == both else " n/a"
            print(f"       {_infer_instrument(iid)}/{iid[-3:]} full={cr:+.2f} "
                  f"both-in-mkt={bstr} same-dir={sd:.0%} n={n_both}  " +
                  _fmt_decay(recent_entry_decay(bsig, bnet, bst['wf'])))


if __name__ == '__main__':
    main()
