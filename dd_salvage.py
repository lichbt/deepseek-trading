#!/usr/bin/env python
"""DD-blocked "real edge vs beta" screener — READ-ONLY.

The hard DD gate (validator.MAX_DRAWDOWN_HARD=0.30) rejects strategies whose
1x-notional drawdown exceeds 30%. Many are high-vol crypto/energy sleeves with
genuine-looking WF/HO scores — but a high score on a volatile instrument is
usually DIRECTIONAL BETA in a favorable regime, not a risk-controllable edge.
Vol-targeting / Calmar ALONE cannot tell them apart: a bull-market beta sleeve
has a great Calmar too (2026-06-21: it flagged 5 crypto beta sleeves as
"salvageable"; the deploy-review showed all 5 were one-sided, rally-concentrated,
or had broken exits).

So a candidate passes only if it clears ALL of:
  1. Calmar / holdout   — return-per-drawdown is real AND held into 2024+ (HO>0).
  2. Directionality      — not mostly-long / mostly-short the instrument (beta).
  3. Concentration       — returns not dominated by 1-2 favorable regime years.
  4. Exit integrity      — exits properly; not stuck in the market (broken exit).

THE HOLDOUT LEG USUALLY CANNOT BE APPLIED HERE, and pretending otherwise was a
defect (fixed 2026-08-14). validator.py returns at the DD gate with
`'ho_score': None` BEFORE Step 7 runs, and record_validation then stores 0.0. So
every row this script selects — `final_status LIKE '%drawdown%'` — carries an
UNMEASURED holdout. Screening those on `ho <= 0` rejected 651 of 663 candidates
on a field that was never written. See holdout_was_measured(): the leg is now
skipped, and such candidates report "holdout UNTESTED" rather than "REAL EDGE".

That distinction is not cosmetic: of 8 untested candidates given a real holdout
on 2026-08-14, 4 cleared their threshold. What this script can still say is
"beta on the applicable screens"; what it cannot say is "no out-of-sample edge".
It also cannot say a survivor is deployable — all 8 of those looks produced 0
deployable sleeves, every one dying in the evaluate lens on a dead short leg,
one-sided crypto exposure, or an incumbent that already dominates it.

READ-ONLY: reads strategies + stored best_params and replays the gate's exact
continuous series. No DB writes, no deploys.

Run:  source ~/.zshrc && ./venv/bin/python dd_salvage.py [--target-vol 0.15]
"""
import argparse
import json
import sqlite3
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

from validator import (get_dev_window, get_candles_date_range,
                       inject_supplementary_data, create_strategy_function,
                       HOLDOUT_START, MAX_DRAWDOWN_HARD)
from pipeline_utils import compute_net_strategy_returns
from risk import compute_calmar_ratio
from portfolio import _infer_instrument, _infer_archetype

PROP_STATIC_DD = 0.10
BARS_PER_YEAR = {'D': 252, 'W': 52, 'H4': 252 * 6, 'H1': 252 * 24, 'M30': 252 * 48}

# --- screen thresholds ---
CALMAR_MIN = 0.50    # return-per-drawdown floor (sizing-invariant)
DIR_MAX    = 0.60    # one direction > this share of ALL bars -> directional beta
TOP2YR_MAX = 0.55    # > this share of positive return from 2 years -> regime beta
FLAT_MIN   = 0.10    # < this flat share -> always-in-market (broken exit / beta)


def reconstruct(code, best_params, inst, tf, arch):
    """Return (oos, sig, ret) — the exact continuous series the DD gate builds."""
    func = create_strategy_function(code)
    dev_start, _ = get_dev_window(inst)
    full = get_candles_date_range(inst, dev_start, HOLDOUT_START, granularity=tf)
    if arch != 'standard':
        full = inject_supplementary_data(full, arch, inst, None, dev_start, HOLDOUT_START, tf)
    ho_end = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    try:
        ho = get_candles_date_range(inst, HOLDOUT_START, ho_end, granularity=tf)
        if arch != 'standard':
            ho = inject_supplementary_data(ho, arch, inst, None, HOLDOUT_START, ho_end, tf)
    except Exception:
        ho = None
    if ho is not None and len(ho):
        oos = pd.concat([full, ho], ignore_index=True)
        if 'date' in oos.columns:
            oos = oos.drop_duplicates(subset='date').reset_index(drop=True)
    else:
        oos = full
    sig = func(oos, best_params).reset_index(drop=True)
    ret = compute_net_strategy_returns(oos, sig, inst, tf, params=best_params)
    return oos, sig, ret


def vol_target(ret, tf, target_ann_vol, vol_win=20, max_lev=3.0):
    """Scale each bar's return by target_vol / trailing realized vol (lagged, no
    look-ahead), capped at max_lev (matches live MAX_WEIGHT_SCALE=3.0)."""
    ann = BARS_PER_YEAR.get(tf, 252)
    target_per_bar = target_ann_vol / np.sqrt(ann)
    vol_est = ret.rolling(vol_win).std().shift(1)
    scale = (target_per_bar / vol_est).clip(upper=max_lev)
    scale = scale.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return scale * ret


# ---- pure screen primitives (unit-tested in test_dd_salvage.py) ----

def directionality(sig):
    """Fraction of bars long / short / flat."""
    n = len(sig)
    if n == 0:
        return {'long': 0.0, 'short': 0.0, 'flat': 1.0}
    vc = pd.Series(sig).value_counts()
    return {'long': vc.get(1, 0) / n, 'short': vc.get(-1, 0) / n, 'flat': vc.get(0, 0) / n}


def holding(sig):
    """entries = transitions into a new non-zero state; max_hold = longest
    consecutive in-market run (a broken time-exit shows up as a huge max_hold)."""
    entries = mx = hold = 0
    prev = 0
    for x in sig:
        if x != 0 and x != prev:
            entries += 1
        if x != 0:
            hold += 1
            mx = max(mx, hold)
        else:
            hold = 0
        prev = x
    return {'entries': entries, 'max_hold': mx}


def top2year_share(rr):
    """Share of total POSITIVE annual log-return coming from the top 2 years
    (0..1). High = the edge is one or two favorable regimes, i.e. beta. Returns
    1.0 when there is no net positive year (degenerate -> treat as concentrated)."""
    ann = (1 + rr).groupby(pd.DatetimeIndex(rr.index).year).prod() - 1
    logs = sorted([np.log1p(x) for x in ann if x > 0], reverse=True)
    return float(sum(logs[:2]) / sum(logs)) if sum(logs) > 0 else 1.0


def holdout_was_measured(final_status: str) -> bool:
    """False when the stored verdict shows validation stopped AT the DD gate, so
    the holdout never ran and holdout_gt_score is a default 0.0, not a result.
    Keyed on the gate's own reason text (validator.py: 'Max drawdown ... >
    ... (full reconstructed equity) — prop-disqualifying')."""
    s = (final_status or '').lower()
    return not ('max drawdown' in s and 'prop-disqualifying' in s)


def screen(dirn, hold_stats, top2, n_bars, tf, calmar, ho):
    """PURE: real edge vs beta. Returns (is_edge: bool, reasons: list[str]).
    A candidate is a real edge only when it clears EVERY screen.

    `ho=None` means the holdout was NEVER RUN — see holdout_was_measured(). The
    HO leg is then SKIPPED rather than treated as a failure. This screener's
    whole population is DD-gate failures, and validator.py returns at the DD
    gate (line ~593, `'ho_score': None`) BEFORE the holdout at Step 7, so
    record_validation stores 0.0 for every one of them. Reading that unwritten
    0.0 as "no 2024+ edge" rejected 651 of 663 candidates on a field that was
    never measured, and made this script's "all beta" summary unsupported.
    Measured 2026-08-14: of 8 such candidates given a real holdout, 4 cleared
    their threshold — so the field is not merely unwritten, it is wrong."""
    reasons = []
    if calmar < CALMAR_MIN:
        reasons.append(f"Calmar {calmar:.2f}<{CALMAR_MIN}")
    if ho is not None and ho <= 0:
        reasons.append("HO=0 (no 2024+ edge)")
    one_dir = max(dirn['long'], dirn['short'])
    if one_dir > DIR_MAX:
        side = 'long' if dirn['long'] >= dirn['short'] else 'short'
        reasons.append(f"{side} {one_dir:.0%} of bars (beta)")
    if top2 > TOP2YR_MAX:
        reasons.append(f"top2yr {top2:.0%}>{TOP2YR_MAX:.0%} (regime beta)")
    marathon = min(BARS_PER_YEAR.get(tf, 252), int(0.30 * n_bars))
    if dirn['flat'] < FLAT_MIN:
        reasons.append(f"flat {dirn['flat']:.0%}<{FLAT_MIN:.0%} (always-in)")
    elif hold_stats['max_hold'] > marathon:
        reasons.append(f"max-hold {hold_stats['max_hold']}>{marathon} (broken exit)")
    return (len(reasons) == 0, reasons)


def dsr_for(strategy_id):
    """Deflated Sharpe for a candidate vs the same (instrument, timeframe) trial
    pool — for annotating the deploy-review. Returns (dsr, pool_n) or (None, 0).
    NOTE: meaningless until trials.db has same-(inst,tf) trials (fills from the
    first batch after the honesty wiring went live)."""
    import sqlite3
    import strategy_honesty as H
    import validator as V
    c = sqlite3.connect("pipeline.db"); c.row_factory = sqlite3.Row
    r = c.execute("SELECT code, timeframe FROM strategies WHERE id=?", (strategy_id,)).fetchone()
    v = c.execute("SELECT best_params FROM validation_results WHERE strategy_id=? "
                  "ORDER BY tested_at DESC LIMIT 1", (strategy_id,)).fetchone()
    c.close()
    if not r or not v or not v["best_params"]:
        return None, 0
    try:
        bp = json.loads(v["best_params"]); inst = _infer_instrument(strategy_id)
        tf = r["timeframe"]; arch = _infer_archetype(r["code"])
        _, _, ret = reconstruct(r["code"], bp, inst, tf, arch)
        pool = V._matching_trial_sharpes(strategy_id, tf)
        return float(H.deflated_sharpe_ratio(ret, pool)), len(pool)
    except Exception:
        return None, 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-vol", type=float, default=0.15,
                    help="Annualized vol target for the Calmar estimate (default 0.15)")
    tgt = ap.parse_args().target_vol

    conn = sqlite3.connect("pipeline.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT v.strategy_id sid, v.is_gt_score i, v.walk_forward_gt_score w,
                  v.holdout_gt_score h, v.final_status fs, v.best_params bp,
                  s.code code, s.timeframe tf
           FROM validation_results v JOIN strategies s ON s.id = v.strategy_id
           WHERE lower(v.final_status) LIKE '%drawdown%' AND v.walk_forward_gt_score >= 0.5
             AND (v.torture_flags IS NULL OR v.torture_flags = '[]' OR v.torture_flags = '')
           ORDER BY v.walk_forward_gt_score DESC""").fetchall()

    seen, cands = set(), []
    for r in rows:
        if r['sid'] in seen:
            continue
        seen.add(r['sid'])
        cands.append(r)

    print(f"DD-blocked-with-edge candidates: {len(cands)}   (gate={MAX_DRAWDOWN_HARD:.0%})")
    print(f"screens: Calmar>={CALMAR_MIN} & HO>0 & one-dir<={DIR_MAX:.0%} & "
          f"top2yr<={TOP2YR_MAX:.0%} & flat>={FLAT_MIN:.0%} & exit-ok\n")
    hdr = (f"{'instrument':11} {'tf':3} {'WF':>4} {'HO':>4} {'Calmar':>6} "
           f"{'L/S/F':>14} {'top2yr':>6} {'maxHold':>7}  verdict")
    print(hdr); print("-" * len(hdr))

    edges = []
    for r in cands:
        sid = r['sid']; inst = _infer_instrument(sid)
        arch = _infer_archetype(r['code'] or ''); tf = r['tf'] or 'D'
        try:
            bp = json.loads(r['bp']) if r['bp'] else {}
        except Exception:
            bp = {}
        if not bp:
            print(f"  {inst:11} {tf:3} {r['w']:4.2f} {r['h'] or 0:4.2f}  SKIP (no best_params)")
            continue
        try:
            oos, sig, ret = reconstruct(r['code'], bp, inst, tf, arch)
            if ret is None or len(ret) < 50:
                raise ValueError("too few returns")
            dates = pd.to_datetime(oos['date'])
            rr = pd.Series(np.asarray(ret), index=dates.iloc[len(oos) - len(ret):].values)
            calmar = compute_calmar_ratio(vol_target(ret, tf, tgt))
            dirn = directionality(sig); hs = holding(sig); top2 = top2year_share(rr)
            ho_meas = holdout_was_measured(r['fs'])
            is_edge, reasons = screen(dirn, hs, top2, len(sig), tf, calmar,
                                      (r['h'] or 0.0) if ho_meas else None)
        except Exception as e:
            print(f"  {inst:11} {tf:3} {r['w']:4.2f} {r['h'] or 0:4.2f}  ERROR {str(e)[:42]}")
            continue
        lsf = f"{dirn['long']:.0%}/{dirn['short']:.0%}/{dirn['flat']:.0%}"
        # Print EVERY reason. This was reasons[:2], which silently truncated the
        # rejection list — the printed counts under-reported directionality and
        # concentration, so the output could not be tallied.
        if is_edge:
            verdict = "★ CLEARS ALL (holdout UNTESTED)" if not ho_meas else "★ REAL EDGE"
        else:
            verdict = "beta: " + "; ".join(reasons)
        ho_txt = f"{r['h'] or 0:4.2f}" if ho_meas else " n/a"
        print(f"  {inst:11} {tf:3} {r['w']:4.2f} {ho_txt} {calmar:6.2f} "
              f"{lsf:>14} {top2:6.0%} {hs['max_hold']:7d}  {verdict}  {sid}")
        if is_edge:
            edges.append(dict(sid=sid, inst=inst, tf=tf, wf=r['w'], ho=r['h'] or 0.0,
                              calmar=calmar, ho_measured=ho_meas))

    conn.close()
    untested = sum(1 for o in edges if not o['ho_measured'])
    print(f"\n===== SUMMARY =====")
    print(f"  candidates: {len(cands)} | cleared every APPLICABLE screen: {len(edges)}"
          f"  (of which holdout UNTESTED: {untested})")
    if edges:
        print(f"  shortlist — NOT a deploy list. Each row still needs (a) a real holdout")
        print(f"  and (b) the sleeve-ops evaluate lens; measured 2026-08-14, 8 such")
        print(f"  candidates cost 8 holdout looks and produced 0 deployable sleeves.")
        for o in sorted(edges, key=lambda x: -x['calmar']):
            ho_txt = f"HO={o['ho']:.2f}" if o['ho_measured'] else "HO=untested"
            print(f"    {o['inst']:11} {o['tf']:3} WF={o['wf']:.2f} {ho_txt} "
                  f"Calmar={o['calmar']:.2f}  {o['sid']}")
    else:
        print("  None — every DD-blocked candidate is directional beta / regime-concentrated /")
        print("  broken-exit on the screens that could be APPLIED. Note this does not by")
        print("  itself prove absence of edge: for rows that stopped at the DD gate the")
        print("  holdout never ran, so the HO leg is skipped rather than failed.")
    # The Calmar -> return projection that used to print here was removed: it read
    # `calmar * PROP_STATIC_DD` as an expected return "at 10% DD", which invites
    # sizing a book off a screener. Calmar is scale-invariant, so that number is
    # not a forecast, and the vol_target path it comes from LEVERS UP (max_lev
    # 3.0) — measured 2026-08-14, a sleeve losing -0.41%/yr in its holdout showed
    # a vol-targeted +17.8%/yr. No figure from this script is evidence of edge.


if __name__ == "__main__":
    main()
