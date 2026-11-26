#!/usr/bin/env python3
"""Score whether a research directive actually moved what gets generated.

WHY THIS EXISTS
The directive was measured INERT: nine nights of "generate more cross-market,
less volatility" moved cross-market 5.5% -> 6.2% against a night-to-night sigma
of 0.8pp, because the prose is spliced into a prompt whose slot was already
chosen. The DIRECTED slot (auto_research._directed_slots) is the fix. This script
is the instrument that says whether the fix worked — and it must stay the SAME
instrument that produced the baseline, or before/after means nothing.

THE TWO CONTROLS THAT MAKE IT COMPARABLE
1. Every night is measured over the SAME UTC clock window (default 16:00-19:00 =
   local 23:00-02:00). The research window was 7h before 2026-09-06 and 3h after,
   so an unwindowed night-over-night comparison measures the schedule change, not
   the steer.
2. SHARES, not counts, for the same reason.

BASELINE SENSITIVITY — read before quoting a number from this
The baseline is whatever nights you ask for, and it is not robust to one outlier.
`--nights 9` (8 baseline nights, 2026-08-29..09-05) gives cross-market mean 5.5%
sd 0.8; `--nights 10` pulls in 2026-08-28, whose cross-market ran 10.3%, and the
same family reads mean 6.0% sd 1.7 — a band twice as wide, which would hide the
very effect this script exists to detect. Quote the night span with the number,
and keep the span fixed across a before/after comparison.

The classifier is imported from meta_review, never reimplemented: a second copy of
the keyword buckets is exactly how this measurement would stop being comparable to
the baseline it is scored against.
"""
import argparse
import json
import os
import sqlite3
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from meta_review import _mechanism_of, _MECH_FAMILIES   # noqa: E402

FAMILIES = list(_MECH_FAMILIES) + ['other']


def load_nights(db, nights, lo_h, hi_h, limit_dates=None):
    """{date: {family: count}} over the fixed UTC hour window, newest `nights` dates."""
    conn = sqlite3.connect('file:%s?mode=ro' % db, uri=True)
    try:
        rows = conn.execute(
            "SELECT substr(created_at,1,10) AS d, substr(created_at,12,2) AS h, rationale "
            "FROM strategies WHERE rationale IS NOT NULL AND created_at IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    per = defaultdict(lambda: defaultdict(int))
    for d, h, rat in rows:
        try:
            hh = int(h)
        except (TypeError, ValueError):
            continue
        if not (lo_h <= hh < hi_h):
            continue
        per[d][_mechanism_of(rat)] += 1
    dates = sorted(per)[-nights:] if nights else sorted(per)
    return {d: dict(per[d]) for d in dates}


def shares(counts):
    n = sum(counts.values())
    if not n:
        return {}, 0
    return {f: 100.0 * counts.get(f, 0) / n for f in FAMILIES}, n


def score(per_night, recent, min_n):
    dates = sorted(per_night)
    if len(dates) <= recent:
        raise SystemExit('need more than --recent nights of history (have %d)' % len(dates))
    recent_dates, prior_dates = dates[-recent:], dates[:-recent]

    prior = []
    for d in prior_dates:
        sh, n = shares(per_night[d])
        if n >= min_n:
            prior.append((d, sh, n))
    if not prior:
        raise SystemExit('no baseline night reached --min-n %d' % min_n)

    rec = [(d,) + shares(per_night[d]) for d in recent_dates]
    rec = [(d, sh, n) for d, sh, n in rec if n]

    out = []
    for f in FAMILIES:
        base = [sh[f] for _, sh, _ in prior]
        mu = statistics.mean(base)
        sd = statistics.pstdev(base) if len(base) > 1 else 0.0
        cur = statistics.mean([sh[f] for _, sh, _ in rec]) if rec else 0.0
        out.append({
            'family': f, 'recent': cur, 'baseline_mean': mu, 'baseline_sd': sd,
            'baseline_min': min(base), 'baseline_max': max(base),
            'shift_sd': ((cur - mu) / sd) if sd else None,
        })
    meta = {
        'recent_nights': [d for d, _, _ in rec],
        'baseline_nights': [d for d, _, _ in prior],
        'recent_n': [n for _, _, n in rec],
        'baseline_n': [n for _, _, n in prior],
        'excluded_below_min_n': [d for d in prior_dates
                                 if d not in [x[0] for x in prior]],
    }
    return out, meta


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', default='pipeline.db')
    ap.add_argument('--nights', type=int, default=10)
    ap.add_argument('--recent', type=int, default=1)
    ap.add_argument('--hours', default='16-19', help='UTC window, e.g. 16-19')
    ap.add_argument('--min-n', type=int, default=30)
    ap.add_argument('--family', action='append', default=None)
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)

    lo_h, hi_h = (int(x) for x in a.hours.split('-'))
    per_night = load_nights(a.db, a.nights, lo_h, hi_h)
    rows, meta = score(per_night, a.recent, a.min_n)
    if a.family:
        rows = [r for r in rows if r['family'] in a.family]
    rows.sort(key=lambda r: abs(r['shift_sd']) if r['shift_sd'] is not None else -1,
              reverse=True)

    if a.json:
        print(json.dumps({'meta': meta, 'families': rows}, indent=2))
        return 0

    print('recent:   %s  (n=%s)' % (', '.join(meta['recent_nights']), meta['recent_n']))
    print('baseline: %d nights %s..%s  (n=%s)' % (
        len(meta['baseline_nights']), meta['baseline_nights'][0],
        meta['baseline_nights'][-1], meta['baseline_n']))
    if meta['excluded_below_min_n']:
        print('excluded (<--min-n): %s' % ', '.join(meta['excluded_below_min_n']))
    print('UTC hours %02d:00-%02d:00 — same window every night\n' % (lo_h, hi_h))
    print('%-16s %9s %9s %7s %14s %9s' %
          ('family', 'recent', 'base mean', 'sd', 'base range', 'shift'))
    print('-' * 70)
    for r in rows:
        sh = 'n/a' if r['shift_sd'] is None else '%+.1f sd' % r['shift_sd']
        mark = ' **' if r['shift_sd'] is not None and abs(r['shift_sd']) >= 2 else ''
        print('%-16s %8.1f%% %8.1f%% %7.1f %6.1f-%-7.1f %9s%s' % (
            r['family'], r['recent'], r['baseline_mean'], r['baseline_sd'],
            r['baseline_min'], r['baseline_max'], sh, mark))
    return 0


if __name__ == '__main__':
    sys.exit(main())
