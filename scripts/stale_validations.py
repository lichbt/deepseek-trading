"""Report stored verdicts produced by a DIFFERENT validator than the current one.

WHY. A pass/fail is only true for the code that produced it, and `tested_at`
cannot say which code that was. The 2026-07-31 data_fetcher dedup fix changed
the candles under ~31% of the failed pool without touching a single row. Every
validation now stamps `validation_results.validator_hash` (pipeline_utils), so
this reads the backlog off the table instead of inferring it from timestamps.

Scope defaults to the statuses that can still reach a live book (passed,
passed_but_fragile, incubating, paper_trading, live). Rows written before
stamping are NULL and count as stale — unknown provenance is not fresh.

READ-ONLY. Clearing a row means running it through validator.py again; the
revalidation sweeps deliberately stop before the holdout and cannot clear it.

Usage:  python3 scripts/stale_validations.py [--all] [--quiet]
Exit 0 = nothing stale in scope, 1 = stale rows found.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline_utils as pu  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument('--all', action='store_true',
                help='every status, incl. research_failed (22k+ rows)')
ap.add_argument('--quiet', action='store_true', help='exit code only')
a = ap.parse_args()

pu.init_db()

if a.all:
    with pu.get_db_connection() as conn:
        statuses = tuple(r['status'] for r in conn.execute(
            'SELECT DISTINCT status FROM strategies ORDER BY status'))
else:
    statuses = pu.STALE_SCOPE_STATUSES

stale = pu.stale_validations(statuses)
current = pu.validator_hash()

if not a.quiet:
    print(f'current validator_hash {current}; scope {", ".join(statuses)}')
    by_status = {}
    for r in stale:
        by_status.setdefault(r['status'], []).append(r)
    for status in sorted(by_status):
        rows = by_status[status]
        print(f'\n{status}: {len(rows)}')
        for r in rows[:20]:
            print(f"  {r['id']}  tested {r['tested_at']}  "
                  f"stamped {r['validator_hash'] or 'NULL(pre-stamp)'}")
        if len(rows) > 20:
            print(f'  ... {len(rows) - 20} more')
    print(f'\n{len(stale)} stale in scope')

sys.exit(1 if stale else 0)
