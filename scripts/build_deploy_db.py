#!/usr/bin/env python3
"""Build the COMPACT deployment pipeline.db that ships to Zeabur.

The local research DB is ~273 MB (73k research_failed rows); the deployed one is
~200 KB. This copies the schema verbatim and carries only the rows the FIX runtime
actually reads:

    strategies          status='paper_trading' only
    validation_results  matching rows (best_params — live sizing needs them)
    live_status         matching rows
    sleeve_units        matching rows (see --no-units caveat below)
    status_history      schema only, empty (audit trail; nothing reads it at runtime)

It writes to a TEMP path and never touches the working DB. Staging into the index
is the caller's job (`git hash-object` + `git update-index --cacheinfo`) so the
273 MB working file is never added — see references/deploy.md.

    python scripts/build_deploy_db.py --out /tmp/deploy.db

--no-units omits sleeve_units rows. Use it when the target volume is FRESH: those
rows are the persisted broker-share truth under netting, and shipping local units
onto an empty volume makes the runtime believe it owns positions it does not.
On an existing Zeabur volume the mounted /data/pipeline.db keeps its own rows
anyway, so the flag is usually moot — but it matters on a first deploy.
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DEFAULT = ROOT / "pipeline.db"

# Tables copied filtered on the paper_trading id set, keyed by their id column.
FILTERED = {
    "validation_results": "strategy_id",
    "live_status": "strategy_id",
    "sleeve_units": "sleeve_id",
}


def build(src_path: Path, out_path: Path, include_units: bool) -> dict:
    if out_path.exists():
        out_path.unlink()

    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    out = sqlite3.connect(out_path)

    # 1. Schema verbatim — every table/index, so nothing the runtime writes fails.
    for (stmt,) in src.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ):
        out.execute(stmt)

    # 2. The sleeve set.
    sids = [r[0] for r in src.execute(
        "SELECT id FROM strategies WHERE status = 'paper_trading'")]
    if not sids:
        raise SystemExit("refusing to build: no paper_trading strategies in source DB")

    ph = ",".join("?" * len(sids))
    counts = {}

    cols = [r[1] for r in src.execute("PRAGMA table_info(strategies)")]
    rows = src.execute(
        f"SELECT * FROM strategies WHERE id IN ({ph})", sids).fetchall()
    out.executemany(
        f"INSERT INTO strategies VALUES ({','.join('?' * len(cols))})", rows)
    counts["strategies"] = len(rows)

    for table, key in FILTERED.items():
        if table == "sleeve_units" and not include_units:
            counts[table] = 0
            continue
        ncols = len(list(src.execute(f"PRAGMA table_info({table})")))
        rows = src.execute(
            f"SELECT * FROM {table} WHERE {key} IN ({ph})", sids).fetchall()
        out.executemany(
            f"INSERT INTO {table} VALUES ({','.join('?' * ncols)})", rows)
        counts[table] = len(rows)

    out.commit()
    out.execute("VACUUM")
    out.close()
    src.close()

    counts["_bytes"] = out_path.stat().st_size
    return counts


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--src", type=Path, default=SRC_DEFAULT)
    p.add_argument("--out", type=Path, required=True,
                   help="temp path, e.g. /tmp/deploy.db — NOT the working pipeline.db")
    p.add_argument("--no-units", action="store_true",
                   help="omit sleeve_units (use on a fresh volume; see module docstring)")
    a = p.parse_args()

    if a.out.resolve() == a.src.resolve():
        raise SystemExit("refusing to overwrite the working DB — pick a temp --out")

    counts = build(a.src, a.out, include_units=not a.no_units)
    kb = counts.pop("_bytes") / 1024
    for t, n in counts.items():
        print(f"  {t:<20} {n:>5}")
    print(f"  {'size':<20} {kb:>5.0f} KB  -> {a.out}")

    if kb > 5000:
        print("\nWARNING: >5 MB. The deployed DB is normally ~200 KB — "
              "check the status filter before committing.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
