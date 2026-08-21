"""Replay recorded theses through a second critique head — same text, two judges.

Answers the question the reject-rate alone cannot: when the self-critique rate
moved, was the CRITIC stricter or were the THESES worse? Both changed in one
commit, so only identical-input replay separates them.

    ./venv/bin/python scripts/critique_replay.py [--n 60] [--model byteplus:glm-5.2]

Also re-runs the ORIGINAL head on the same sample. That arm is not optional:
critique_control.py documents verdicts flipping between runs minutes apart on
the same model at temperature 0, so a raw disagreement rate is meaningless
without a same-model self-consistency baseline to compare it against.
"""
import argparse, json, os, random, sys, collections
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_research as ar

CORPUS = os.getenv('CRITIQUE_LOG', '.auto-research-logs/critique_theses.jsonl')


def judge(model, rec):
    ar.SELF_CRITIQUE_MODELS = [model]
    r = ar.self_critique_thesis(rec['thesis'], rec['instrument'])
    return {'verdict': r['verdict'], 'reason': r.get('reason', ''),
            'failopen': 'fail-open' in r.get('reason', '') or 'exception' in r.get('reason', '')}


def run_arm(model, sample, workers):
    # SELF_CRITIQUE_MODELS is module-global, so one arm at a time; threads
    # inside an arm all use the same model and cannot race it.
    ar.SELF_CRITIQUE_MODELS = [model]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(lambda r: judge(model, r), sample))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=60, help='per verdict class')
    ap.add_argument('--model', default='byteplus:glm-5.2', help='challenger head')
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--out', default='/tmp/critique_replay.json')
    a = ap.parse_args()

    recs = [json.loads(l) for l in open(CORPUS)]
    incumbent = recs[0]['critique_head']
    rng = random.Random(a.seed)
    sample = []
    for v in ('reject', 'pass'):
        pool = [r for r in recs if r['verdict'] == v]
        sample += rng.sample(pool, min(a.n, len(pool)))
    print(f"corpus={len(recs)}  sample={len(sample)}  "
          f"incumbent={incumbent}  challenger={a.model}", flush=True)

    chal = run_arm(a.model, sample, a.workers)
    print(f"challenger arm done ({a.model})", flush=True)
    self_ = run_arm(incumbent, sample, a.workers)
    print(f"self-consistency arm done ({incumbent})", flush=True)

    rows = []
    for rec, c, s in zip(sample, chal, self_):
        rows.append({'instrument': rec['instrument'], 'orig': rec['verdict'],
                     'chal': c['verdict'], 'chal_fo': c['failopen'],
                     'self': s['verdict'], 'self_fo': s['failopen'],
                     'orig_reason': rec['reason'], 'chal_reason': c['reason'],
                     'thesis': rec['thesis']})
    json.dump({'incumbent': incumbent, 'challenger': a.model, 'rows': rows},
              open(a.out, 'w'), indent=1)

    def table(key, label):
        m = collections.Counter((r['orig'], r[key]) for r in rows)
        agree = sum(v for (o, x), v in m.items() if o == x)
        fo = sum(1 for r in rows if r[key + '_fo'])
        print(f"\n-- {label} vs original ({agree}/{len(rows)} agree, "
              f"{100*agree/len(rows):.0f}%; fail-open {fo}) --")
        print(f"{'orig':>8} {'->pass':>7} {'->reject':>9}")
        for o in ('reject', 'pass'):
            print(f"{o:>8} {m[(o,'pass')]:>7} {m[(o,'reject')]:>9}")

    table('self', f'SAME head re-run ({incumbent})')
    table('chal', f'CHALLENGER ({a.model})')
    print(f"\nfull rows -> {a.out}")


if __name__ == '__main__':
    main()
