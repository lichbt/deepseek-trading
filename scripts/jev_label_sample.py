# Build a stratified hand-label sample from the Jev shadow log.
# Label each row in the verdict column:
#   D = duplicate  N = near  R = related  X = unrelated (Jev false positive)
# precision(block) = count(D,N) / labeled block rows.
import argparse, csv, json, random, sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def matched_rationale(conn, match_id):
    if not match_id:
        return ''
    try:
        row = conn.execute('SELECT rationale, status FROM strategies WHERE id = ?',
                           (match_id,)).fetchone()
    except Exception:
        return ''
    return '[%s] %s' % (row[1], (row[0] or '')[:900]) if row else '(match id not in DB)'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--log', default=str(REPO / '.auto-research-logs' / 'jev_triage.jsonl'))
    ap.add_argument('--db', default=str(REPO / 'pipeline.db'))
    ap.add_argument('--out', default=str(REPO / 'jev_label_sample.csv'))
    ap.add_argument('--n-block', type=int, default=40)
    ap.add_argument('--n-review', type=int, default=15)
    ap.add_argument('--n-proceed', type=int, default=15)
    ap.add_argument('--seed', type=int, default=0)
    args = ap.parse_args()

    rows = []
    for line in open(args.log):
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    by_route = {}
    for r in rows:
        if r.get('closest') in (None, 'unclear'):
            continue
        by_route.setdefault(r.get('route'), []).append(r)

    rng = random.Random(args.seed)
    picked = []
    for route, n in (('block', args.n_block), ('review', args.n_review),
                     ('proceed', args.n_proceed)):
        pool = list(by_route.get(route, []))
        rng.shuffle(pool)
        picked.extend(pool[:n])

    conn = sqlite3.connect(args.db)
    with open(args.out, 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['i', 'at', 'instrument', 'route', 'probability', 'match_id',
                    'dominant_defect', 'verdict', 'new_thesis', 'matched_thesis'])
        for i, r in enumerate(picked, 1):
            th = r.get('thesis') or {}
            new = ' | '.join(str(th.get(k, '') or '') for k in
                             ('rationale', 'entry_condition', 'filter_condition',
                              'exit_condition'))[:900]
            w.writerow([i, r.get('at'), r.get('instrument'), r.get('route'),
                        r.get('probability'), r.get('match_id'),
                        r.get('dominant_defect'), '', new,
                        matched_rationale(conn, r.get('match_id'))])
    print('routes:', {k: len(v) for k, v in by_route.items()})
    print('wrote %d rows -> %s' % (len(picked), args.out))
    print('fill verdict (D/N/R/X); precision(block) = D+N over labeled block rows')


if __name__ == '__main__':
    main()
