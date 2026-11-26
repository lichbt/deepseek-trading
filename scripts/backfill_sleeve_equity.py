#!/usr/bin/env python3
"""Backfill sleeve_equity from the paper-trading logs.

live_test has recorded every bar it processed to its own log since deployment,
but nothing persisted it — sleeve_equity only starts collecting from the moment
the writer shipped (2026-07-29). This recovers the ~616 live bars already sitting
in the logs so per-sleeve history starts today rather than in six weeks.

These are REAL LIVE BARS, not a reconstruction: each line is what the trader
actually saw and did at the time. They are marked source='log_backfill' anyway,
because a log is a weaker record than a database write — see the caveats below.

WHY NOT REUSE portfolio.parse_log_returns: it keeps only the P&L field and
NORMALISES the timestamp to midnight. Both matter here. We want bar_return and
position as well, and a normalised timestamp would NOT match the live writer's
str(current_bar_time), so UNIQUE(sleeve_id, bar_time) would fail to dedupe and
the table would end up holding two rows per bar in two different formats.

WHAT CANNOT BE RECOVERED, and is therefore left NULL:
    own_units, price, sleeve_pnl
The log records the bar return and the position (-1/0/+1) but not the units held
or the price, so currency P&L is unreconstructable. position_return — the
scale-free column, and the one actually comparable to a reconstruction — IS
recoverable, which is the more useful half.

THE LOGS HAVE GAPS. xcuusd_i27 was deployed 2026-06-06 (53 days) yet has 38 bar
lines; gbpjpy_i8 from 06-11 (48 days) has 45. Restarts and rotation have already
eaten some. This backfill is real but INCOMPLETE, and a missing bar is
indistinguishable from a bar that never happened. Treat gaps as unknown, not flat.

Idempotent: INSERT OR IGNORE against UNIQUE(sleeve_id, bar_time), so re-running
is a no-op. It also means a row already written by the LIVE writer always wins —
the backfill can never overwrite a richer live row with a sparser one.
"""
import argparse
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "pipeline.db"
LOG_DIR = ROOT / ".paper-trading-logs"

# [2026-07-27 21:00:00+00:00] [D] Bar return: -0.0084, Position: -1, P&L: +0.0084
LINE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s*\[(?P<tf>[^\]]+)\]\s*Bar return:\s*(?P<br>[-+0-9.eE]+),"
    r"\s*Position:\s*(?P<pos>[-+0-9]+),\s*P&L:\s*(?P<pnl>[-+0-9.eE]+)"
)


def parse_log(path):
    """Yield (bar_time_raw, position, bar_return, position_return) per bar line.

    bar_time is taken VERBATIM from the log prefix, because live_test writes
    str(current_bar_time) and the log prefix is that same value — so the two
    sources produce byte-identical keys and dedupe correctly.
    """
    out = []
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                m = LINE.match(line.strip())
                if not m:
                    continue
                try:
                    out.append((m.group("ts").strip(), int(m.group("pos")),
                                float(m.group("br")), float(m.group("pnl"))))
                except ValueError:
                    continue
    except FileNotFoundError:
        return []
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="parse and report, insert nothing")
    ap.add_argument("--statuses", default="paper_trading,incubating",
                    help="comma-separated statuses to backfill")
    a = ap.parse_args()

    statuses = [s.strip() for s in a.statuses.split(",") if s.strip()]
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    qmarks = ",".join("?" * len(statuses))
    sids = [r[0] for r in conn.execute(
        f"SELECT id FROM strategies WHERE status IN ({qmarks}) ORDER BY id", statuses)]

    sql = ("INSERT OR IGNORE INTO sleeve_equity "
           "(sleeve_id, bar_time, position, bar_return, position_return, source) "
           "VALUES (?,?,?,?,?, 'log_backfill')")

    before = conn.execute("SELECT COUNT(*) FROM sleeve_equity").fetchone()[0]
    parsed, per_sleeve, no_log = 0, Counter(), []

    for sid in sids:
        path = LOG_DIR / f"{sid}.log"
        if not path.exists():
            no_log.append(sid)
            continue
        rows = parse_log(path)
        per_sleeve[sid] = len(rows)
        parsed += len(rows)
        if not a.dry_run and rows:
            conn.executemany(sql, [(sid, ts, pos, br, pnl) for ts, pos, br, pnl in rows])
    if not a.dry_run:
        conn.commit()

    after = conn.execute("SELECT COUNT(*) FROM sleeve_equity").fetchone()[0]

    print(f"\nsleeves considered      : {len(sids)} ({'/'.join(statuses)})")
    print(f"bar lines parsed        : {parsed}")
    print(f"sleeves with no log     : {len(no_log)}")
    if no_log:
        for s in no_log[:10]:
            print(f"    (no log) {s}")
    print(f"rows before             : {before}")
    print(f"rows after              : {after}   (+{after - before})")
    if not a.dry_run:
        dupes = parsed - (after - before)
        print(f"skipped as duplicates   : {dupes}  "
              f"(re-runs, restart-replayed bars, or rows the LIVE writer already owns)")
    print("\nper sleeve (top 12):")
    for sid, n in per_sleeve.most_common(12):
        print(f"    {n:4d}  {sid}")
    if a.dry_run:
        print("\n(DRY RUN — nothing inserted)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
