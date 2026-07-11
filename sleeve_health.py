#!/usr/bin/env python3
"""
sleeve_health.py — rolling health monitor for live sleeves + candidate ranking.

Two modes:

  1. HEALTH CHECK (default):
     For every deployed (paper_trading) sleeve, compute rolling metrics over
     a configurable window (default 6 months) and flag sleeves whose edge
     has decayed. Outputs a ranked table from healthiest to sickest.

  2. CANDIDATE RANKING (--rank):
     Score passed-but-not-deployed strategies against the existing book on
     marginal Sharpe improvement and decorrelation, so you deploy the best
     addition — not just "good enough."

Usage:
    ./venv/bin/python sleeve_health.py                  # health check
    ./venv/bin/python sleeve_health.py --window 90      # 90-day rolling window
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

# ── Decay thresholds ──
# Each metric has a REVIEW and RETIRE threshold.
# REVIEW = "watch closely, reduce conviction next rebalance"
# RETIRE = "remove from book unless you have a strong reason to keep"
# NOTE: rolling_sharpe and rolling_return are measured over the WINDOW only.
# trailing_loss_mo is the CURRENT losing streak at the tail (not all-time worst).
# live_vs_backtest = rolling_sharpe / full_sharpe (edge decay ratio).
THRESHOLDS = {
    "rolling_sharpe":       {"review": -0.3,  "retire": -1.0},
    "rolling_return":       {"review": -0.03, "retire": -0.08},
    "trailing_loss_mo":     {"review": 3,     "retire": 5},
    "live_vs_backtest":     {"review": 0.0,   "retire": -0.5},
}


def _reconstruct_sleeve(row, start, end):
    """Reconstruct a single sleeve's daily returns over [start, end]."""
    sid = row["id"]
    tf = row["timeframe"] or "D"
    inst = P._infer_instrument(sid)
    try:
        df = get_candles_date_range(inst, start, end, granularity=tf).reset_index(drop=True)
        df["date"] = pd.to_datetime(df["date"])
        archetype = P._infer_archetype(row["code"], row.get("archetype") or "standard")
        if archetype != "standard":
            df = inject_supplementary_data(df, archetype, inst, row.get("instrument2"), start, end, tf)
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


def _consecutive_losing_months(rets):
    """Longest streak of consecutive negative months (calendar months)."""
    if len(rets) < 20:
        return 0
    monthly = rets.resample("ME").sum()
    streak = 0
    max_streak = 0
    for r in monthly.values:
        if r < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return max_streak


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


def _grade(metrics):
    """Grade a sleeve: HEALTHY / REVIEW / RETIRE based on thresholds."""
    flags = []
    for metric, thresholds in THRESHOLDS.items():
        val = metrics.get(metric)
        if val is None or (isinstance(val, float) and np.isnan(val)):
            continue
        if metric == "trailing_loss_mo":
            if val >= thresholds["retire"]:
                flags.append(("RETIRE", metric, val))
            elif val >= thresholds["review"]:
                flags.append(("REVIEW", metric, val))
        elif metric in ("rolling_sharpe", "rolling_return", "live_vs_backtest"):
            if val <= thresholds["retire"]:
                flags.append(("RETIRE", metric, val))
            elif val <= thresholds["review"]:
                flags.append(("REVIEW", metric, val))
    if any(g == "RETIRE" for g, _, _ in flags):
        return "RETIRE", flags
    if any(g == "REVIEW" for g, _, _ in flags):
        return "REVIEW", flags
    return "HEALTHY", flags


def health_check(window_days=180):
    """Run health check on all deployed sleeves."""
    rows = {r["id"]: r for r in P.load_strategies()}
    weights_data = {}
    if os.path.exists(STATE_FILE):
        try:
            weights_data = json.load(open(STATE_FILE)).get("weights", {})
        except Exception:
            pass

    now = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    full_start = "2015-01-01"
    window_start = (datetime.now() - timedelta(days=window_days + 30)).strftime("%Y-%m-%d")

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

        r_sharpe = _rolling_sharpe(rets, window_days)
        r_return = _rolling_return(rets, window_days)
        full_sharpe = _rolling_sharpe(rets, len(rets))
        live_vs_bt = r_sharpe / full_sharpe if (full_sharpe and full_sharpe > 0 and not np.isnan(full_sharpe)) else np.nan
        mdd = _max_drawdown(rets)
        consec = _consecutive_losing_months(rets)
        trail_consec = _trailing_consecutive_loss_months(rets)
        active = _active_days(sig, window_days)
        weight = weights_data.get(sid, 0.0)

        metrics = {
            "rolling_sharpe": r_sharpe,
            "rolling_return": r_return,
            "full_sharpe": full_sharpe,
            "live_vs_backtest": live_vs_bt,
            "max_dd_sleeve": mdd,
            "consecutive_loss_mo": consec,
            "trailing_loss_mo": trail_consec,
            "active_days": active,
            "weight": weight,
            "instrument": inst,
        }
        grade, flags = _grade(metrics)
        metrics["grade"] = grade
        metrics["flags"] = flags
        metrics["id"] = sid
        results.append(metrics)

    results.sort(key=lambda r: (
        {"HEALTHY": 0, "REVIEW": 1, "RETIRE": 2, "ERROR": 3}.get(r.get("grade", "ERROR"), 3),
        -(r.get("rolling_sharpe") or -999)
    ))
    return results, all_rets


def print_health_report(results, window_days):
    """Print the health check results."""
    print(f"\n{'='*100}")
    print(f"  SLEEVE HEALTH CHECK — {window_days}-day rolling window")
    print(f"{'='*100}")
    print(f"{'Sleeve':<36} {'Inst':<12} {'Grade':<8} {'RollSharpe':>10} {'RollRet%':>9} "
          f"{'FullSharpe':>10} {'Live/BT':>7} {'MaxDD%':>7} {'LossMo':>6} {'Active':>6} {'Wt%':>5}")
    print(f"{'─'*100}")

    healthy = review = retire = error = 0
    for r in results:
        if r.get("grade") == "ERROR":
            print(f"{r['id'][:36]:<36} {'???':<12} {'ERROR':<8} {'—':>10} {'—':>9} "
                  f"{'—':>10} {'—':>7} {'—':>7} {'—':>6} {'—':>6} {'—':>5}")
            error += 1
            continue

        sid = r["id"]
        inst = r["instrument"]
        grade = r["grade"]
        rs = r["rolling_sharpe"]
        rr = r["rolling_return"]
        fs = r["full_sharpe"]
        lvb = r["live_vs_backtest"]
        mdd = r["max_dd_sleeve"]
        clm = r["trailing_loss_mo"]
        act = r["active_days"]
        wt = r["weight"]

        grade_marker = {"HEALTHY": " ", "REVIEW": "*", "RETIRE": "!"}[grade]
        print(f"{sid[:36]:<36} {inst:<12} {grade_marker}{grade:<7} "
              f"{rs:>10.2f} {rr*100:>8.2f}% "
              f"{fs:>10.2f} {lvb:>6.1f}x {mdd*100:>6.1f}% {clm:>6} {act:>6} {wt*100:>4.1f}%")

        if grade == "HEALTHY": healthy += 1
        elif grade == "REVIEW": review += 1
        elif grade == "RETIRE": retire += 1

    print(f"{'─'*100}")
    print(f"Summary: {healthy} HEALTHY  |  {review} REVIEW  |  {retire} RETIRE  |  {error} ERROR")

    if review + retire > 0:
        print(f"\n{'─'*60}")
        print("FLAGGED SLEEVES — details:")
        print(f"{'─'*60}")
        for r in results:
            if r.get("grade") in ("REVIEW", "RETIRE"):
                print(f"\n  {r['id']}")
                for g, metric, val in r.get("flags", []):
                    thresh = THRESHOLDS[metric]
                    if isinstance(val, float):
                        print(f"    [{g}] {metric}: {val:.3f}  (review<={thresh['review']}, retire<={thresh['retire']})")
                    else:
                        print(f"    [{g}] {metric}: {val}  (review>={thresh['review']}, retire>={thresh['retire']})")

    print(f"\n{'='*100}")
    print("DECISION GUIDE:")
    print("  HEALTHY  → keep running, no action needed")
    print("  REVIEW   → reduce conviction at next rebalance, watch closely")
    print("  RETIRE   → remove from book unless you have a strong thesis for keeping it")
    print(f"{'='*100}")


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
        avg_corr = np.mean(corrs) if corrs else 0.0

        sleeve_sharpe = _rolling_sharpe(rets, len(rets))
        ho_sharpe = _rolling_sharpe(rets, 180)
        wf = row["walk_forward_gt_score"] or 0
        ho = row.get("holdout_gt_score") or 0
        mdd = _max_drawdown(rets)
        active = _active_days(sig, len(sig))
        inst = P._infer_instrument(sid)

        robustness = min(ho / wf, 1.5) if wf > 0 else 0.0
        diversity_score = max(0, 1 - max_corr)
        composite = (marginal_sharpe * 2) + (diversity_score * 0.5) + (robustness * 0.3)

        scored.append({
            "id": sid,
            "instrument": inst,
            "wf": wf,
            "ho": ho,
            "robustness": robustness,
            "sleeve_sharpe": sleeve_sharpe,
            "ho_sharpe": ho_sharpe,
            "marginal_sharpe": marginal_sharpe,
            "max_corr": max_corr,
            "avg_corr": avg_corr,
            "diversity": diversity_score,
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

    print(f"\n{'='*110}")
    print(f"  CANDIDATE RANKING — marginal book improvement")
    print(f"{'='*110}")
    print(f"{'#':>2} {'Sleeve':<36} {'Inst':<12} {'Composite':>9} {'MargSharpe':>10} "
          f"{'Diversity':>9} {'Robust':>7} {'MaxCorr':>7} {'WF':>5} {'HO':>5} {'MaxDD%':>7}")
    print(f"{'─'*110}")

    for i, r in enumerate(scored, 1):
        print(f"{i:>2} {r['id'][:36]:<36} {r['instrument']:<12} {r['composite']:>9.3f} "
              f"{r['marginal_sharpe']:>+9.3f} "
              f"{r['diversity']:>9.2f} {r['robustness']:>7.2f} {r['max_corr']:>7.2f} "
              f"{r['wf']:>5.2f} {r['ho']:>5.2f} {r['max_dd']*100:>6.1f}%")

    print(f"{'─'*110}")
    print("SCORING: Composite = 2×MarginalSharpe + 0.5×Diversity + 0.3×Robustness")
    print("  MarginalSharpe = how much book Sharpe improves by adding this sleeve")
    print("  Diversity      = 1 - max|corr| with any existing sleeve (0=clone, 1=uncorrelated)")
    print("  Robustness     = min(HO/WF, 1.5) — holdout decay ratio")
    print(f"{'='*110}")


def main():
    parser = argparse.ArgumentParser(description="Sleeve health monitor and candidate ranker")
    parser.add_argument("--window", type=int, default=180, help="Rolling window in days (default: 180)")
    parser.add_argument("--rank", action="store_true", help="Rank passed candidates instead of health check")
    parser.add_argument("--top", type=int, default=10, help="Show top N candidates (default: 10)")
    args = parser.parse_args()

    if args.rank:
        results, all_rets = health_check(args.window)
        print_health_report(results, args.window)
        scored = rank_candidates(all_rets, args.top)
        print_candidate_ranking(scored)
    else:
        results, _ = health_check(args.window)
        print_health_report(results, args.window)


if __name__ == "__main__":
    main()
