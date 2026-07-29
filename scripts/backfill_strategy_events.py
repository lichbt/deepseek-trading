#!/usr/bin/env python3
"""Backfill strategy_events from status_history.

Usage:
    python3 scripts/backfill_strategy_events.py           # real insert
    python3 scripts/backfill_strategy_events.py --dry-run   # classify only
    python3 scripts/backfill_strategy_events.py --limit N   # process N rows
"""

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

# Make the repo root importable for reason_codes.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reason_codes import classify


DB_PATH = REPO_ROOT / "pipeline.db"
BATCH_SIZE = 1000


def parse_args():
    parser = argparse.ArgumentParser(description="Backfill strategy_events from status_history")
    parser.add_argument("--dry-run", action="store_true", help="Classify and report, do not insert")
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N rows")
    return parser.parse_args()


def fetch_rows(conn, limit=None):
    sql = "SELECT id, strategy_id, old_status, new_status, reason, changed_at FROM status_history"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql)


def build_event(row):
    history_id, strategy_id, old_status, new_status, reason, changed_at = row
    reason_code = classify(new_status, reason)
    return (
        strategy_id,
        changed_at,
        old_status,
        new_status,
        reason_code,
        reason,
        "backfill",
        history_id,
    )


def print_report(total, code_counts, unclassified_pairs):
    print(f"\nTotal rows processed: {total}")
    print("\nReason-code distribution:")
    for code, count in code_counts.most_common():
        pct = (count / total) * 100 if total else 0.0
        print(f"  {code:30s} {count:>8d}  {pct:6.2f}%")

    unclassified_total = code_counts.get("UNCLASSIFIED", 0)
    print(f"\nUNCLASSIFIED total: {unclassified_total}")

    if unclassified_pairs:
        print(f"\nDistinct (new_status, reason) pairs in UNCLASSIFIED ({len(unclassified_pairs)} total, showing up to 30):")
        for (ns, reason), count in unclassified_pairs.most_common(30):
            print(f"  {count:>6d} | {ns} | {reason[:120]}")


def main():
    args = parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA busy_timeout=30000")
    # foreign_keys stays OFF (SQLite's default, and what the rest of this DB runs with).
    # status_history holds 1 row whose strategy_id is not in strategies; with FKs ON,
    # INSERT OR IGNORE swallows the FK violation exactly like a duplicate and would
    # silently DROP that historical event while still reporting success.
    conn.execute("PRAGMA foreign_keys=OFF")

    cursor = conn.cursor()
    rows = cursor.execute(
        "SELECT id, strategy_id, old_status, new_status, reason, changed_at FROM status_history"
        + (f" LIMIT {int(args.limit)}" if args.limit else "")
    )

    code_counts = Counter()
    unclassified_pairs = Counter()
    batch = []
    total = 0

    insert_sql = """
        INSERT OR IGNORE INTO strategy_events
            (strategy_id, occurred_at, old_status, new_status, reason_code, reason_prose, source, history_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """

    while True:
        chunk = rows.fetchmany(BATCH_SIZE)
        if not chunk:
            break

        for row in chunk:
            total += 1
            event = build_event(row)
            reason_code = event[4]
            code_counts[reason_code] += 1

            if reason_code == "UNCLASSIFIED":
                unclassified_pairs[(row[3], row[4])] += 1

            if not args.dry_run:
                batch.append(event)
                if len(batch) >= BATCH_SIZE:
                    conn.executemany(insert_sql, batch)
                    conn.commit()
                    batch.clear()

    if not args.dry_run and batch:
        conn.executemany(insert_sql, batch)
        conn.commit()

    print_report(total, code_counts, unclassified_pairs)

    if args.dry_run:
        print("\n(DRY RUN — no rows inserted)")
    else:
        print(f"\nInserted/ignored rows into {DB_PATH}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
