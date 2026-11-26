#!/usr/bin/env python3
"""Flatten sleeve_units owned by sleeves that no longer run.

A retired sleeve has no live_test process, so the units it still owns under
NETTING are unmanaged and unstopped (netted positions carry no broker stop — the
stop is evaluated per-bar inside the loop that is no longer running). This
closes that exposure.

Idempotent and safe to run on a timer: it exits 0 with "nothing to do" once no
orphan rows remain, and it never touches a sleeve that is still paper_trading.
A close rejected by the broker (MARKET_HALTED at the weekend, API down) leaves
the row intact so the next run retries.

The close is REDUCE_ONLY, so under netting it can never open exposure — but that
also means it is rejected NO_POSITION_TO_REDUCE when the instrument's net has
already absorbed the orphan's share. That rejection is terminal, not transient:
retrying can never succeed, so the row is cleared and reported as a bookkeeping
correction rather than an exit. Without that distinction this job reported
"STILL EXPOSED" forever against an instrument the account did not hold, which is
worse than useless — it is a permanent false alarm in the channel that is
supposed to warn about a real stranded position.

    ./venv/bin/python flatten_orphans.py           # flatten every orphan
    ./venv/bin/python flatten_orphans.py --dry-run # report only
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# Credentials come from the shell under launchd; fall back to .env like the
# other services do.
if not os.getenv('OANDA_API_TOKEN'):
    env = ROOT / '.env'
    if env.exists():
        for line in env.read_text().splitlines():
            if '=' in line and not line.strip().startswith('#'):
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))

import pipeline_utils as pu


def find_orphans():
    """Sleeves holding non-zero units whose status is not paper_trading."""
    conn = sqlite3.connect(str(pu.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS sleeve_units'
                     '(sleeve_id TEXT PRIMARY KEY, units REAL, stop REAL)')
        return conn.execute(
            "SELECT su.sleeve_id, su.units, s.status FROM sleeve_units su "
            "JOIN strategies s ON s.id = su.sleeve_id "
            # 'incubating' is NOT an orphan: an incubating sleeve is deliberately
            # trading on the paper book while withheld from the prop account, so it
            # legitimately holds units while not being 'paper_trading'. Without this
            # exclusion the orphan sweeper would flatten exactly the positions
            # incubation exists to observe.
            "WHERE ABS(COALESCE(su.units, 0)) > 0 "
            "  AND s.status NOT IN ('paper_trading','incubating') "
            "ORDER BY su.sleeve_id").fetchall()
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    stamp = datetime.now().isoformat(timespec='seconds')
    orphans = find_orphans()
    if not orphans:
        print(f'[{stamp}] nothing to do — no orphaned sleeve_units')
        return 0

    failed = 0
    lines = []
    for row in orphans:
        sid, units, status = row['sleeve_id'], row['units'], row['status']
        if args.dry_run:
            print(f'[{stamp}] would flatten {sid} ({status}) units={units:+.4f}')
            continue
        try:
            res = pu.flatten_sleeve(sid)
            if res.get('skipped') == 'NO_POSITION_TO_REDUCE':
                # Not a close and not a failure: the broker held nothing on the
                # reducible side, so the sleeve's share was already netted away.
                # Report it as a bookkeeping correction, never as an exit.
                print(f"[{stamp}] cleared stale row {sid} ({status}) "
                      f"units={res['requested']:+.4f} — broker had no position to reduce")
                lines.append(f"🧹 cleared stale row {sid} ({status})\n"
                             f"   {res['requested']:+.4f} units on the books, "
                             f"nothing at the broker — no order placed")
            else:
                print(f"[{stamp}] flattened {sid} ({status}) units={res['units']:+.4f} "
                      f"@ {res['price']} pl={res['pl']}")
                lines.append(f"✅ closed {sid} ({status})\n"
                             f"   {res['units']:+.4f} @ {res['price']}  pl={res['pl']}")
        except Exception as exc:
            failed += 1
            print(f'[{stamp}] RETRY LATER {sid} ({status}) units={units:+.4f}: {exc}')
            lines.append(f'⚠️ STILL EXPOSED {sid} ({status})\n'
                         f'   {units:+.4f} units — close FAILED: {exc}\n'
                         f'   unmanaged and unstopped until the next run succeeds')

    # This job closes broker positions with no human in the loop, so it must never
    # do so silently — a flatten you never hear about is indistinguishable from the
    # stranding it exists to fix.
    if lines and not args.dry_run:
        try:
            from telegram_bot import notify
            notify(f'ORPHAN SLEEVE SWEEP  {stamp}\n\n' + '\n'.join(lines))
        except Exception as exc:
            print(f'[{stamp}] telegram notify failed: {exc}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
