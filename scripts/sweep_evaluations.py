#!/usr/bin/env python3
"""Run evaluate_strategy across every strategy that cleared validation.

evaluations only gets a row when someone runs the tool, unlike strategy_events
and sleeve_equity which write themselves. Its whole purpose is making DECAY a
queryable time series rather than a number someone recomputes — and a time series
needs REPEATED runs of the same strategy across moving windows. One sweep gives
the baseline; the value appears on the second.

Read-only with respect to trading: evaluate_strategy reconstructs and reports, it
never places an order or changes a status. The only write is one evaluations row
per strategy.

Each run is subprocessed with a timeout so one hanging reconstruction cannot
stall the sweep, and failures are recorded rather than fatal — a strategy whose
data cannot be fetched should not cost you the other 71.

Usage:
    python3 scripts/sweep_evaluations.py                    # passed + deployed + fragile
    python3 scripts/sweep_evaluations.py --statuses paper_trading
    python3 scripts/sweep_evaluations.py --timeout 300
"""
import argparse
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "pipeline.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--statuses", default="paper_trading,passed,passed_but_fragile")
    ap.add_argument("--timeout", type=int, default=240, help="per-strategy seconds")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()

    statuses = [s.strip() for s in a.statuses.split(",") if s.strip()]
    conn = sqlite3.connect(str(DB), timeout=30)
    q = ",".join("?" * len(statuses))
    sids = [r[0] for r in conn.execute(
        f"SELECT id FROM strategies WHERE status IN ({q}) ORDER BY status, id", statuses)]
    before = conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]
    conn.close()
    if a.limit:
        sids = sids[:a.limit]

    print(f"[sweep] {len(sids)} strategies ({'/'.join(statuses)}), "
          f"timeout {a.timeout}s each, evaluations before={before}", flush=True)

    ok = fail = timeout = 0
    t0 = time.time()
    for i, sid in enumerate(sids, 1):
        started = time.time()
        try:
            p = subprocess.run(
                [sys.executable, str(ROOT / "evaluate_strategy.py"), sid],
                cwd=str(ROOT), capture_output=True, text=True, timeout=a.timeout)
            if p.returncode == 0:
                ok += 1
                # surface the two lines that actually carry the verdict
                tail = [l for l in p.stdout.splitlines()
                        if "RECENT" in l or l.startswith("CAND") or "LOOKAHEAD" in l]
                note = tail[0][:110] if tail else "(no verdict line)"
                status = "ok"
            else:
                fail += 1
                note = (p.stderr.strip().splitlines() or ["(no stderr)"])[-1][:110]
                status = "FAIL"
        except subprocess.TimeoutExpired:
            timeout += 1
            status, note = "TIMEOUT", f"exceeded {a.timeout}s"
        print(f"[{i:3d}/{len(sids)}] {status:7} {time.time()-started:5.1f}s  {sid[:38]:38} {note}",
              flush=True)

    conn = sqlite3.connect(str(DB), timeout=30)
    after = conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]
    conn.close()
    print(f"\n[sweep] done in {(time.time()-t0)/60:.1f} min — "
          f"ok={ok} fail={fail} timeout={timeout}", flush=True)
    print(f"[sweep] evaluations {before} -> {after} (+{after-before})", flush=True)
    print(f"[sweep] finished {datetime.now(timezone.utc).isoformat()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
