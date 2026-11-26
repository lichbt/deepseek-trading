# Cohort read for the Jev shadow decisions: which route/repair/regen arms actually
# produce theses that clear the IS gate.
#
# Usage:
#   python scripts/jev_cohort_report.py                # pooled cohorts
#   python scripts/jev_cohort_report.py --days         # add per-day rows
#   python scripts/jev_cohort_report.py --since 2026-10-09
#
# Cohorts are assigned from jev_outcomes.jsonl and scored from validation_results.
# repaired/regen are the treated arms; repair-control/regen-control are the
# randomized holds, so treated-vs-control is the causal comparison.
import argparse
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def cohort_of(row):
    if row.get('defect_repaired'):
        return 'repaired'
    if row.get('repair_holdout'):
        return 'repair-control'
    if row.get('regenerated'):
        return 'regen'
    if row.get('novelty_holdout'):
        return 'regen-control'
    if row.get('route') == 'block':
        return 'block-no-regen'
    return row.get('route') or 'none'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--log', default=str(REPO / '.auto-research-logs' / 'jev_outcomes.jsonl'))
    ap.add_argument('--db', default=str(REPO / 'pipeline.db'))
    ap.add_argument('--since', default='')
    ap.add_argument('--days', action='store_true')
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    pooled = defaultdict(list)
    per_day = defaultdict(lambda: defaultdict(list))
    missing = 0
    with open(args.log) as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if args.since and row.get('at', '')[:10] < args.since:
                continue
            got = conn.execute(
                'SELECT is_gt_score FROM validation_results WHERE strategy_id = ?',
                (row.get('strategy_id'),)).fetchone()
            if not got or got[0] is None or not math.isfinite(got[0]):
                missing += 1
                continue
            k = cohort_of(row)
            pooled[k].append(got[0])
            per_day[row.get('at', '')[:10]][k].append(got[0])

    def line(name, xs):
        n = len(xs)
        ge = 100 * sum(1 for v in xs if v >= 0.3) // n
        zero = 100 * sum(1 for v in xs if v == 0) // n
        med = sorted(xs)[n // 2]
        return '%-15s n=%5d  IS>=0.3=%3d%%  IS=0=%3d%%  median=%+.3f' % (name, n, ge, zero, med)

    print('wrote rows with no finite IS yet: %d' % missing)
    for k in sorted(pooled, key=lambda k: -len(pooled[k])):
        print(line(k, pooled[k]))
    if args.days:
        print()
        for d in sorted(per_day):
            for k in sorted(per_day[d], key=lambda k: -len(per_day[d][k])):
                print(d, line(k, per_day[d][k]))


if __name__ == '__main__':
    main()
