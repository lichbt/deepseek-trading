"""Hand-label a blinded sample of critiqued theses, then score the gate against it.

critique_replay.py proved the critique head CHANGED (qwen3.7-plus self-agrees
94%, glm-5.2 passes what it rejects). It could not say whether the new ~49%
reject rate is CORRECT — agreement is not accuracy. This supplies the missing
ground truth.

    ./venv/bin/python scripts/critique_label.py sheet  --n-reject 30 --n-pass 10
    # ...fill in labels.json: {"<key>": {"verdict":"pass|reject","cat":"...","why":"..."}}
    ./venv/bin/python scripts/critique_label.py score --labels labels.json

The sheet is BLINDED and shuffled on purpose: it omits the gate's verdict and
reason, because a labeller who sees "reject: contradicts the mechanism" will
find the contradiction. The key is a hash, not an index, so ordering leaks
nothing either. Sample is deliberately reject-heavy — a false REJECT silently
starves generation and is the failure mode worth measuring; passes are included
so the labeller cannot score well by rubber-stamping one verdict.

Label against _SELF_CRITIQUE_SYSTEM's four categories and its DEFAULT-TO-PASS
instruction, not against whether the strategy would make money.
"""
import argparse, hashlib, json, os, random, sys, collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CORPUS = os.getenv('CRITIQUE_LOG', '.auto-research-logs/critique_theses.jsonl')
CATS = ('mechanism', 'fidelity', 'regime', 'lookahead', 'none')


def key_of(rec):
    t = rec['thesis']
    raw = f"{rec['instrument']}|{t.get('rationale','')}|{t.get('entry_condition','')}"
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


def load(n_reject, n_pass, seed):
    recs = [json.loads(l) for l in open(CORPUS)]
    rng = random.Random(seed)
    out = []
    for v, n in (('reject', n_reject), ('pass', n_pass)):
        pool = [r for r in recs if r['verdict'] == v]
        out += rng.sample(pool, min(n, len(pool)))
    rng.shuffle(out)          # order must not encode the verdict
    return recs, out


def cmd_sheet(a):
    _, sample = load(a.n_reject, a.n_pass, a.seed)
    lines = ["# Blinded critique labelling sheet",
             "",
             f"{len(sample)} theses. For each: verdict pass|reject, category "
             f"({'/'.join(CATS)}), one sentence.",
             "Rubric is _SELF_CRITIQUE_SYSTEM: reject ONLY on a nameable structural",
             "defect in mechanism / fidelity / regime-independence / look-ahead.",
             "DEFAULT TO PASS. Not-obviously-profitable is NOT a reject.",
             ""]
    for rec in sample:
        t = rec['thesis']
        lines += [f"## {key_of(rec)}",
                  f"- instrument: {rec['instrument']}",
                  f"- family/timeframe: {t.get('strategy_family','')} / {t.get('timeframe','')}",
                  f"- rationale: {t.get('rationale','')}",
                  f"- entry: {t.get('entry_condition','')}",
                  f"- filter: {t.get('filter_condition','')}",
                  f"- exit: {t.get('exit_condition','')}",
                  ""]
    open(a.out, 'w').write('\n'.join(lines))
    print(f"wrote {a.out}  ({len(sample)} theses, blinded, seed={a.seed})")


def cmd_score(a):
    _, sample = load(a.n_reject, a.n_pass, a.seed)
    labels = json.load(open(a.labels))
    by_key = {key_of(r): r for r in sample}
    missing = [k for k in by_key if k not in labels]
    if missing:
        print(f"WARNING: {len(missing)} unlabelled, excluded: {missing[:5]}")
    m = collections.Counter()
    disagree = []
    for k, rec in by_key.items():
        if k not in labels:
            continue
        truth = labels[k]['verdict']
        gate = rec['verdict']
        m[(truth, gate)] += 1
        if truth != gate:
            disagree.append((k, rec, labels[k]))
    n = sum(m.values())
    tp, fp = m[('reject', 'reject')], m[('pass', 'reject')]
    fn, tn = m[('reject', 'pass')], m[('pass', 'pass')]
    print(f"\nn={n}  (gate = alibaba critique head, truth = hand label)")
    print(f"{'':>14}{'gate reject':>13}{'gate pass':>11}")
    print(f"{'truth reject':>14}{tp:>13}{fn:>11}")
    print(f"{'truth pass':>14}{fp:>13}{tn:>11}")
    if tp + fp:
        print(f"\nprecision (of gate rejects, share truly bad): {tp}/{tp+fp} = {tp/(tp+fp):.0%}")
    if tp + fn:
        print(f"recall    (of truly bad, share caught):        {tp}/{tp+fn} = {tp/(tp+fn):.0%}")
    print(f"accuracy: {(tp+tn)}/{n} = {(tp+tn)/n:.0%}")
    print(f"\nFALSE REJECTS (the starvation failure mode): {fp}")
    for k, rec, lab in disagree:
        if lab['verdict'] == 'pass':
            print(f"  {k} {rec['instrument']:9} gate: {rec['reason'][:80]}")
            print(f"             {'':9} me:   {lab.get('why','')[:80]}")
    print(f"\nFALSE PASSES: {fn}")
    for k, rec, lab in disagree:
        if lab['verdict'] == 'reject':
            print(f"  {k} {rec['instrument']:9} me: [{lab.get('cat','')}] {lab.get('why','')[:80]}")
    cats = collections.Counter(l.get('cat') for l in labels.values() if l['verdict'] == 'reject')
    print(f"\nmy reject categories: {dict(cats)}")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['sheet', 'score'])
    ap.add_argument('--n-reject', type=int, default=30)
    ap.add_argument('--n-pass', type=int, default=10)
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--out', default='/tmp/critique_sheet.md')
    ap.add_argument('--labels', default='/tmp/critique_labels.json')
    a = ap.parse_args()
    (cmd_sheet if a.cmd == 'sheet' else cmd_score)(a)
