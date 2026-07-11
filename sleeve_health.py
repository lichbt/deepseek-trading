#!/usr/bin/env python3
"""
sleeve_health.py — rolling health monitor for live sleeves + candidate ranking.

Two modes:

  1. HEALTH CHECK (default):
     For every deployed (paper_trading) sleeve, compute rolling metrics over
     MULTIPLE windows (3mo, 6mo, 12mo) and grade by cross-window consensus:

       HEALTHY  = 6mo Sharpe > 0
       REVIEW   = 6mo Sharpe < 0 BUT 12mo Sharpe > 0  (temporary drawdown)
       RETIRE   = BOTH 6mo AND 12mo Sharpe < 0         (structural decay)

  2. CANDIDATE RANKING (--rank):
     Score passed-but-not-deployed strategies against the existing book on
     marginal Sharpe improvement and decorrelation, so you deploy the best
     addition — not just "good enough."

Usage:
    ./venv/bin/python sleeve_health.py                  # health check (multi-window)
    ./venv/bin/python sleeve_health.py --rank            # rank candidates
    ./venv/bin/python sleeve_health.py --rank --top 5    # top 5 candidates
"""
import os, sys, json, argparse, warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import portfolio as P
from validator import create_strategy_function, get_candles_date_range, get_dev_window
from supplementary_data import inject_supplementary_data
import pipeline_utils

STATE_FILE = os.path.join(os.path.dirname(__file__), "portfolio_state.json")

WINDOWS = {
    "3mo":  63,
    "6mo":  126,
    "12mo": 252,
}


def _reconstruct_sleeve(row, start, end):
    """Reconstruct a single sleeve's daily returns over [start, end]."""
    sid = row["id"]
    tf = row["timeframe"] or "D"
    inst = P._infer_instrument(sid)
    try:
        ds, _ = get_dev_window(inst)
        actual_start = max(start, ds)
        df = get_candles_date_range(inst, actual_start, end, granularity=tf).reset_index(drop=True)
        df["date"] = pd.to_datetime(df["date"])
        archetype = P._infer_archetype(row["code"], row.get("archetype") or "standard")
        if archetype != "standard":
            df = inject_supplementary_data(df, archetype, inst, row.get("instrument2"), actual_start, end, tf)
        f = create_strategy_function(row["code"])
        bp = json.loads(row["best_params"] or "{}")
        sig = np.asarray(f(df, bp)).astype(int)
        rr = np.asarray(pipeline_utils.compute_net_strategy_returns(
            df, pd.Series(sig, index=df.index), inst, tf))
        idx = pd.to_datetime(df["date"].iloc[:len(rr)])
        return pd.Series(rr, index=idx, name=sid), sig[:len(rr)]
    except Exception as e:
        return None, None


def _rolling_sharpe(rets, window_days):
    """Annualised Sharpe over a trailing window."""
    if len(rets) < 20:
        return np.nan
    tail = rets.iloc[-window_days:]
    tail = tail[tail != 0]
    if len(tail) < 10:
        return np.nan
    mu = tail.mean() * 252
    sigma = tail.std() * np.sqrt(252)
    return mu / sigma if sigma > 0 else 0.0


def _rolling_return(rets, window_days):
    """Cumulative return over a trailing window."""
    tail = rets.iloc[-window_days:]
    return float((1 + tail).prod() - 1)


def _max_drawdown(rets):
    """Max drawdown of the full return series."""
    eq = (1 + rets).cumprod()
    return float((eq / eq.cummax() - 1).min())


def _trailing_consecutive_loss_months(rets):
    """Current streak of consecutive losing months at the tail end."""
    if len(rets) < 20:
        return 0
    monthly = rets.resample("ME").sum()
    streak = 0
    for r in reversed(monthly.values):
        if r < 0:
            streak += 1
        else:
            break
    return streak


def _active_days(sig, window_days):
    """Number of days the strategy had a position in the window."""
    tail = sig[-window_days:]
    return int(np.sum(tail != 0))


MIN_TRADES_12MO = 30   # need ≥30 active days in 12mo for stats to mean anything

def _grade_multiwindow(sharpe_3mo, sharpe_6mo, sharpe_12mo, trail_loss_mo, active_12mo):
    """Grade using cross-window consensus + minimum trade count.

    LOW_DATA = fewer than 30 active days in 12mo — can't evaluate
    RETIRE   = 6mo AND 12mo both negative (structural edge decay)
    REVIEW   = 6mo negative but 12mo positive (temporary drawdown)
    HEALTHY  = 6mo positive (recent edge intact)

    When a window has insufficient data (nan), we skip it — a sleeve
    with only 4 months of history can't be judged on 12mo.
    """
    if active_12mo < MIN_TRADES_12MO:
        return "LOW_DATA", [f"only {active_12mo} active days in 12mo (need {MIN_TRADES_12MO}+)"]

    flags = []

    s6_neg = (not np.isnan(sharpe_6mo)) and sharpe_6mo < 0
    s12_neg = (not np.isnan(sharpe_12mo)) and sharpe_12mo < 0
    s6_nan = np.isnan(sharpe_6mo)
    s12_nan = np.isnan(sharpe_12mo)

    if s6_neg and (s12_neg or s12_nan):
        flags.append(f"6mo Sharpe {sharpe_6mo:+.2f}")
        if not s12_nan:
            flags.append(f"12mo Sharpe {sharpe_12mo:+.2f}")
        if trail_loss_mo >= 3:
            flags.append(f"{trail_loss_mo} trailing loss months")
        return "RETIRE", flags

    if s6_neg and not s12_neg:
        flags.append(f"6mo Sharpe {sharpe_6mo:+.2f} but 12mo {sharpe_12mo:+.2f} (temporary?)")
        return "REVIEW", flags

    if trail_loss_mo >= 5:
        flags.append(f"{trail_loss_mo} trailing loss months despite 6mo Sharpe {sharpe_6mo:+.2f}")
        return "REVIEW", flags

    return "HEALTHY", flags


def health_check():
    """Run health check on all deployed sleeves with multi-window grading."""
    rows = {r["id"]: r for r in P.load_strategies()}
    weights_data = {}
    if os.path.exists(STATE_FILE):
        try:
            weights_data = json.load(open(STATE_FILE)).get("weights", {})
        except Exception:
            pass

    now = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    full_start = "2015-01-01"

    results = []
    all_rets = {}
    print(f"Reconstructing {len(rows)} sleeves...", flush=True)

    for sid, row in rows.items():
        tf = row["timeframe"] or "D"
        if tf != "D":
            continue

        rets, sig = _reconstruct_sleeve(row, full_start, now)
        if rets is None:
            results.append({"id": sid, "grade": "ERROR", "reason": "reconstruction failed"})
            continue

        all_rets[sid] = rets
        inst = P._infer_instrument(sid)

        s3 = _rolling_sharpe(rets, WINDOWS["3mo"])
        s6 = _rolling_sharpe(rets, WINDOWS["6mo"])
        s12 = _rolling_sharpe(rets, WINDOWS["12mo"])
        full_sharpe = _rolling_sharpe(rets, len(rets))

        r3 = _rolling_return(rets, WINDOWS["3mo"])
        r6 = _rolling_return(rets, WINDOWS["6mo"])
        r12 = _rolling_return(rets, WINDOWS["12mo"])

        mdd = _max_drawdown(rets)
        trail_loss = _trailing_consecutive_loss_months(rets)
        active_6mo = _active_days(sig, WINDOWS["6mo"])
        active_12mo = _active_days(sig, WINDOWS["12mo"])
        weight = weights_data.get(sid, 0.0)

        grade, flags = _grade_multiwindow(s3, s6, s12, trail_loss, active_12mo)

        results.append({
            "id": sid,
            "instrument": inst,
            "grade": grade,
            "flags": flags,
            "sharpe_3mo": s3,
            "sharpe_6mo": s6,
            "sharpe_12mo": s12,
            "full_sharpe": full_sharpe,
            "return_3mo": r3,
            "return_6mo": r6,
            "return_12mo": r12,
            "max_dd": mdd,
            "trailing_loss_mo": trail_loss,
            "active_6mo": active_6mo,
            "active_12mo": active_12mo,
            "weight": weight,
        })

    results.sort(key=lambda r: (
        {"HEALTHY": 0, "LOW_DATA": 1, "REVIEW": 2, "RETIRE": 3, "ERROR": 4}.get(r.get("grade", "ERROR"), 4),
        -(r.get("sharpe_6mo") if r.get("sharpe_6mo") and not np.isnan(r.get("sharpe_6mo", np.nan)) else -999)
    ))
    return results, all_rets


def _fmt_sharpe(v):
    return f"{v:+.2f}" if (v is not None and not np.isnan(v)) else "  n/a"


def print_health_report(results):
    """Print the multi-window health check results."""
    print(f"\n{'='*120}")
    print(f"  SLEEVE HEALTH CHECK — multi-window (3mo / 6mo / 12mo)")
    print(f"{'='*120}")
    print(f"{'Sleeve':<36} {'Inst':<12} {'Grade':<10} "
          f"{'Sh3m':>6} {'Sh6m':>6} {'Sh12m':>6} {'Full':>6} "
          f"{'Ret6m%':>7} {'Ret12m%':>8} {'MaxDD%':>7} {'LsMo':>4} {'A6m':>4} {'A12m':>4} {'Wt%':>5}")
    print(f"{'─'*120}")

    healthy = low_data = review = retire = error = 0
    for r in results:
        if r.get("grade") == "ERROR":
            print(f"{r['id'][:36]:<36} {'???':<12} {'ERROR':<10}")
            error += 1
            continue

        grade = r["grade"]
        marker = {"HEALTHY": " ", "LOW_DATA": "~", "REVIEW": "*", "RETIRE": "!"}[grade]

        print(f"{r['id'][:36]:<36} {r['instrument']:<12} {marker}{grade:<9} "
              f"{_fmt_sharpe(r['sharpe_3mo']):>6} {_fmt_sharpe(r['sharpe_6mo']):>6} "
              f"{_fmt_sharpe(r['sharpe_12mo']):>6} {_fmt_sharpe(r['full_sharpe']):>6} "
              f"{r['return_6mo']*100:>6.1f}% {r['return_12mo']*100:>7.1f}% "
              f"{r['max_dd']*100:>6.1f}% {r['trailing_loss_mo']:>4} "
              f"{r['active_6mo']:>4} {r['active_12mo']:>4} {r['weight']*100:>4.1f}%")

        if grade == "HEALTHY": healthy += 1
        elif grade == "LOW_DATA": low_data += 1
        elif grade == "REVIEW": review += 1
        elif grade == "RETIRE": retire += 1

    print(f"{'─'*120}")
    print(f"Summary: {healthy} HEALTHY  |  {low_data} LOW_DATA  |  {review} REVIEW  |  {retire} RETIRE  |  {error} ERROR")

    flagged = [r for r in results if r.get("grade") in ("LOW_DATA", "REVIEW", "RETIRE")]
    if flagged:
        print(f"\n{'─'*70}")
        print("FLAGGED SLEEVES — reasoning:")
        print(f"{'─'*70}")
        for r in flagged:
            print(f"\n  [{r['grade']}] {r['id']}")
            for f in r.get("flags", []):
                print(f"    → {f}")

    print(f"\n{'='*120}")
    print("GRADING LOGIC (cross-window consensus + trade count):")
    print(f"  LOW_DATA = fewer than {MIN_TRADES_12MO} active days in 12mo (not enough trades to evaluate)")
    print("  HEALTHY  = 6mo Sharpe > 0 (recent edge intact)")
    print("  REVIEW   = 6mo Sharpe < 0 BUT 12mo Sharpe > 0 (temporary drawdown, may recover)")
    print("  RETIRE   = 6mo AND 12mo Sharpe BOTH < 0 (structural decay — edge is gone)")
    print(f"{'='*120}")


def rank_candidates(existing_rets, top_n=10):
    """Rank passed-but-not-deployed strategies by marginal book improvement."""
    import sqlite3
    conn = sqlite3.connect(P.DB_PATH)
    conn.row_factory = sqlite3.Row
    candidates = conn.execute("""
        SELECT s.id, s.timeframe, s.code, s.status,
               s.instrument, s.archetype, s.instrument2,
               vr.best_params, vr.walk_forward_gt_score,
               vr.is_gt_score, vr.holdout_gt_score, vr.torture_flags
        FROM strategies s
        JOIN validation_results vr ON s.id = vr.strategy_id
        WHERE s.status IN ('passed', 'passed_but_fragile')
          AND vr.walk_forward_gt_score > 0
        ORDER BY vr.walk_forward_gt_score DESC
    """).fetchall()
    conn.close()
    candidates = [dict(r) for r in candidates]

    if not candidates:
        print("No passed candidates found.")
        return []

    now = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    full_start = "2015-01-01"

    book_df = pd.DataFrame(existing_rets).fillna(0)
    book_daily = book_df.sum(axis=1)
    book_sharpe = _rolling_sharpe(book_daily, len(book_daily))

    print(f"\nScoring {len(candidates)} candidates against the {len(existing_rets)}-sleeve book "
          f"(book Sharpe: {book_sharpe:.2f})...\n")

    scored = []
    for row in candidates:
        sid = row["id"]
        tf = row["timeframe"] or "D"
        if tf != "D":
            continue

        rets, sig = _reconstruct_sleeve(row, full_start, now)
        if rets is None:
            continue

        aligned = pd.DataFrame(existing_rets).fillna(0)
        aligned[sid] = rets
        aligned = aligned.fillna(0)
        new_book = aligned.sum(axis=1)
        new_sharpe = _rolling_sharpe(new_book, len(new_book))
        marginal_sharpe = new_sharpe - book_sharpe

        corrs = []
        for esid, erets in existing_rets.items():
            common = pd.concat([rets, erets], axis=1).dropna()
            if len(common) > 20:
                corrs.append(abs(common.iloc[:, 0].corr(common.iloc[:, 1])))
        max_corr = max(corrs) if corrs else 0.0

        s6 = _rolling_sharpe(rets, WINDOWS["6mo"])
        s12 = _rolling_sharpe(rets, WINDOWS["12mo"])
        full_sharpe = _rolling_sharpe(rets, len(rets))
        wf = row["walk_forward_gt_score"] or 0
        ho = row.get("holdout_gt_score") or 0
        mdd = _max_drawdown(rets)
        active = _active_days(sig, len(sig))
        inst = P._infer_instrument(sid)

        robustness = min(ho / wf, 1.5) if wf > 0 else 0.0
        diversity_score = max(0, 1 - max_corr)
        recent_ok = 1.0 if (not np.isnan(s6) and s6 > 0) else 0.0
        composite = (marginal_sharpe * 2) + (diversity_score * 0.5) + (robustness * 0.3) + (recent_ok * 0.3)

        scored.append({
            "id": sid,
            "instrument": inst,
            "wf": wf,
            "ho": ho,
            "robustness": robustness,
            "sharpe_6mo": s6,
            "sharpe_12mo": s12,
            "full_sharpe": full_sharpe,
            "marginal_sharpe": marginal_sharpe,
            "max_corr": max_corr,
            "diversity": diversity_score,
            "recent_ok": recent_ok,
            "max_dd": mdd,
            "active_days": active,
            "composite": composite,
        })

    scored.sort(key=lambda x: x["composite"], reverse=True)
    return scored[:top_n]


def print_candidate_ranking(scored):
    """Print the candidate ranking table."""
    if not scored:
        print("No candidates to rank.")
        return

    print(f"\n{'='*130}")
    print(f"  CANDIDATE RANKING — marginal book improvement")
    print(f"{'='*130}")
    print(f"{'#':>2} {'Sleeve':<36} {'Inst':<12} {'Comp':>6} {'MargSh':>7} "
          f"{'Divers':>6} {'Robust':>6} {'6moOK':>5} {'MaxCorr':>7} "
          f"{'Sh6m':>6} {'Sh12m':>6} {'WF':>5} {'HO':>5} {'MaxDD%':>7}")
    print(f"{'─'*130}")

    for i, r in enumerate(scored, 1):
        print(f"{i:>2} {r['id'][:36]:<36} {r['instrument']:<12} {r['composite']:>6.2f} "
              f"{r['marginal_sharpe']:>+6.3f} "
              f"{r['diversity']:>6.2f} {r['robustness']:>6.2f} "
              f"{'YES' if r['recent_ok'] else ' NO':>5} {r['max_corr']:>7.2f} "
              f"{_fmt_sharpe(r['sharpe_6mo']):>6} {_fmt_sharpe(r['sharpe_12mo']):>6} "
              f"{r['wf']:>5.2f} {r['ho']:>5.2f} {r['max_dd']*100:>6.1f}%")

    print(f"{'─'*130}")
    print("SCORING: Composite = 2×MarginalSharpe + 0.5×Diversity + 0.3×Robustness + 0.3×Recent6moPositive")
    print("  MarginalSharpe = how much book Sharpe improves by adding this sleeve")
    print("  Diversity      = 1 - max|corr| with any existing sleeve")
    print("  Robustness     = min(HO/WF, 1.5) — holdout decay ratio")
    print("  6moOK          = bonus if the candidate's own 6mo Sharpe is positive (recent edge alive)")
    print(f"{'='*130}")


def main():
    parser = argparse.ArgumentParser(description="Sleeve health monitor and candidate ranker")
    parser.add_argument("--rank", action="store_true", help="Also rank passed candidates")
    parser.add_argument("--top", type=int, default=10, help="Show top N candidates (default: 10)")
    args = parser.parse_args()

    results, all_rets = health_check()
    print_health_report(results)

    if args.rank:
        scored = rank_candidates(all_rets, args.top)
        print_candidate_ranking(scored)


if __name__ == "__main__":
    main()
