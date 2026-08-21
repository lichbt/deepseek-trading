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

PIN THE CORPUS FIRST. The gate appends to critique_theses.jsonl while batches
run, so `load()` samples from a MOVING pool and the same seed does not return
the same theses an hour later. Point CRITIQUE_LOG at a snapshot before any
before/after comparison:

    cp .auto-research-logs/critique_theses.jsonl /tmp/snap.jsonl
    CRITIQUE_LOG=/tmp/snap.jsonl ./venv/bin/python scripts/critique_label.py ...

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
    if a.gate:
        # Same theses, same labels, different judge: overlay only the verdicts.
        newv = {key_of(json.loads(l)): json.loads(l) for l in open(a.gate)}
        miss = [k for k in by_key if k not in newv]
        if miss:
            print(f"WARNING: {len(miss)} sample theses absent from --gate file")
        by_key = {k: newv.get(k, v) for k, v in by_key.items()}
        print(f"scoring re-judged verdicts from {a.gate}")
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


def cmd_regate(a):
    """Re-judge the SAME sample with a replacement system prompt, score vs labels.

    The point is a before/after on identical text and identical labels, so the
    only thing that moved is the prompt. Writes a corpus-shaped JSONL so `score`
    can read the new verdicts exactly as it reads the gate's own.
    """
    from concurrent.futures import ThreadPoolExecutor
    import auto_research as ar
    _, sample = load(a.n_reject, a.n_pass, a.seed)
    if a.system:
        ar._SELF_CRITIQUE_SYSTEM = open(a.system).read()
    if a.model:
        ar.SELF_CRITIQUE_MODELS = [a.model]
    head = (ar.SELF_CRITIQUE_MODELS or [None])[0]
    print(f"regating {len(sample)} theses  head={head}  system={a.system or 'unchanged'}",
          flush=True)
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        out = list(ex.map(lambda r: ar.self_critique_thesis(r['thesis'], r['instrument']),
                          sample))
    fo = sum(1 for r in out if 'fail-open' in r.get('reason', ''))
    with open(a.out, 'w') as fh:
        for rec, r in zip(sample, out):
            fh.write(json.dumps({**rec, 'verdict': r['verdict'], 'reason': r['reason'],
                                 'critique_head': head}) + chr(10))
    rej = sum(1 for r in out if r['verdict'] == 'reject')
    print(f"reject rate {rej}/{len(out)} = {100*rej/len(out):.0f}%   fail-open {fo}")
    print(f"wrote {a.out} -- score it with:")
    print(f"  CRITIQUE_LOG={a.out} ./venv/bin/python scripts/critique_label.py score "
          f"--labels {a.labels}")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['sheet', 'score', 'regate'])
    ap.add_argument('--n-reject', type=int, default=30)
    ap.add_argument('--n-pass', type=int, default=10)
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--out', default='/tmp/critique_sheet.md')
    ap.add_argument('--labels', default='/tmp/critique_labels.json')
    ap.add_argument('--gate', help='score: JSONL of re-judged verdicts (from regate) to score '
                                   'INSTEAD of the corpus verdicts; the sample and the labels '
                                   'stay identical so only the gate moved')
    ap.add_argument('--system', help='regate: file holding a replacement critique system prompt')
    ap.add_argument('--model', help='regate: critique head override')
    ap.add_argument('--workers', type=int, default=6)
    a = ap.parse_args()
    {'sheet': cmd_sheet, 'score': cmd_score, 'regate': cmd_regate}[a.cmd](a)
