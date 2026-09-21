"""Resolve NEEDS_RERUN rows whose zero-reason payload is already on the record.

WHY. refine_codes.classify stores NEEDS_RERUN whenever the failure string
carries the exact-zero sentinel and no zero_reason is supplied, because a bare
"0.0000 <" cannot distinguish a coverage guard from a clamped negative score
(the 2026-08-03 binding correction). But for an IS-gate zero the validator
ALREADY re-ran the strategy and appended gt_score_zero_reason's own verdict as a
" [payload]" suffix (validator.py:457-466). Rows written before that payload was
threaded through pipeline_utils.record_validation therefore read NEEDS_RERUN on
a question that was answered at validation time.

This is a pure TEXT reclassification: it re-runs nothing, reads only
validation_results.final_status, and writes only failure_cause. It cannot
resolve a WF or holdout zero — the diagnosis block is gated on is_score, so
those carry no suffix and still need scripts/revalidate_failed.py.

SAFETY. Every changed row's previous value is written to a revert file BEFORE
the transaction commits, and the reclassification is recomputed through
refine_codes.classify rather than mapped by hand, so this script cannot invent
a partition the live path would not produce.

Usage:
    python3 scripts/backfill_failure_cause.py                 # report only
    python3 scripts/backfill_failure_cause.py --write         # apply
    python3 scripts/backfill_failure_cause.py --revert F.csv  # undo
"""
import argparse
import collections
import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import refine_codes as R          # noqa: E402
from pipeline_utils import DB_PATH  # noqa: E402


def _plan(con):
    """Rows whose stored NEEDS_RERUN would now classify to something else."""
    rows = con.execute(
        'SELECT v.strategy_id, s.status, v.final_status, v.failure_cause '
        'FROM validation_results v JOIN strategies s ON s.id = v.strategy_id '
        "WHERE v.failure_cause = ?", (R.NEEDS_RERUN,)).fetchall()
    changes, unchanged = [], 0
    for sid, status, final_status, old in rows:
        new = R.classify(status, final_status, R.zero_reason_from(final_status))
        if new == old:
            unchanged += 1
        else:
            changes.append((sid, old, new))
    return rows, changes, unchanged


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=str(DB_PATH))
    ap.add_argument('--write', action='store_true',
                    help='apply the reclassification (default is a dry run)')
    ap.add_argument('--revert', metavar='CSV',
                    help='restore failure_cause from a revert file and exit')
    a = ap.parse_args(argv)

    con = sqlite3.connect(a.db)

    if a.revert:
        with open(a.revert) as fh:
            back = [(r['old'], r['strategy_id']) for r in csv.DictReader(fh)]
        con.executemany(
            'UPDATE validation_results SET failure_cause = ? WHERE strategy_id = ?', back)
        con.commit()
        print(f'reverted {len(back)} rows from {a.revert}')
        return 0

    rows, changes, unchanged = _plan(con)
    tally = collections.Counter(new for _, _, new in changes)

    print(f'NEEDS_RERUN rows: {len(rows)}')
    print(f'  resolvable:   {len(changes)}')
    print(f'  still opaque: {unchanged}  (no payload — WF/holdout zeros)')
    for k, v in tally.most_common():
        print(f'    -> {k:14} {v}')

    # A resolution must never land on UNKNOWN: zero_reason_from whitelists the
    # payload heads precisely so an unrecognised suffix stays NEEDS_RERUN. An
    # UNKNOWN here means the whitelist and the partition have drifted apart.
    if tally.get(R.UNKNOWN):
        print('\nUNKNOWN in the plan — whitelist and partition disagree; refusing to write')
        return 1

    if not a.write:
        print('\ndry run — pass --write to apply')
        return 0

    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    revert_path = Path(a.db).resolve().parent / f'failure_cause_revert_{stamp}.csv'
    with open(revert_path, 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['strategy_id', 'old', 'new'])
        w.writerows(changes)
    print(f'\nrevert file: {revert_path}')

    con.executemany(
        'UPDATE validation_results SET failure_cause = ? WHERE strategy_id = ?',
        [(new, sid) for sid, _, new in changes])
    con.commit()

    left = con.execute('SELECT COUNT(*) FROM validation_results WHERE failure_cause = ?',
                       (R.NEEDS_RERUN,)).fetchone()[0]
    print(f'wrote {len(changes)} rows; {left} still NEEDS_RERUN')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
