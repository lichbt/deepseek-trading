#!/usr/bin/env python3
"""Analyse a chain-head A/B run against its pre-registration.

WHY IT READS THE PRE-REGISTRATION INSTEAD OF HARDCODING IT. n=147 per arm is valid
ONLY under the asymmetric decision rule: the challenger is ~1.8x slower per candidate,
so it has to win decisively to be worth adopting. Soften the rule to "adopt on any
improvement" and the effect to detect becomes +50%, which needs 510 per arm. Two
numbers that must move together are the classic place for an analysis to drift after
the fact — so this script recomputes the required n from the pre-registered effect
size and ABORTS if it disagrees with the pre-registered n. Editing one without the
other is not discouraged here; it is impossible.

It also refuses to emit a verdict if the git sha moved mid-run. The research loop runs
the WORKING TREE, so an edit or a checkout during the run changes the code generating
the sample — which is a different experiment wearing the same name.

    ./venv/bin/python scripts/ab_analyse.py                     # the real thing
    ./venv/bin/python scripts/ab_analyse.py --smoke 400         # executability only

`--smoke N` synthesises arms by alternating over the most recent N validated
strategies. It proves the analysis runs end to end WITHOUT any experimental data
existing, and its output is meaningless by construction — it says so, loudly.
"""
import argparse
import json
import math
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREREG = ROOT / '.scratch' / 'thesis-ab' / 'preregistration.md'
DB = ROOT / 'pipeline.db'
NEG_INF = -1e30              # is_gt_score stores -inf as a very large negative float


# ── the pre-registration is the input, not a comment ──────────────────────────

def load_prereg(path):
    """The fenced ```json block. Aborts on missing or unparseable — never defaults."""
    if not path.exists():
        sys.exit(f"ABORT: no pre-registration at {path}. The analysis is not defined "
                 f"without one.")
    blocks = re.findall(r'```json\s*\n(.*?)\n```', path.read_text(), re.S)
    if len(blocks) != 1:
        sys.exit(f"ABORT: expected exactly one ```json block in {path}, found "
                 f"{len(blocks)}. Ambiguous pre-registration is no pre-registration.")
    try:
        spec = json.loads(blocks[0])
    except Exception as exc:
        sys.exit(f"ABORT: pre-registration JSON is unparseable: {exc}")
    for key in ('chain', 'control', 'challenger', 'primary', 'baseline_rate',
                'relative_effect', 'alpha', 'power', 'n_per_arm', 'decision_rule'):
        if key not in spec:
            sys.exit(f"ABORT: pre-registration is missing {key!r}")
    return spec


def _z(p):
    """Inverse normal CDF, so the power check needs no scipy at import time."""
    from statistics import NormalDist
    return NormalDist().inv_cdf(p)


def required_n(p1, rel_effect, alpha, power):
    """Two-proportion sample size per arm, the standard normal approximation."""
    p2 = p1 * (1.0 + rel_effect)
    if not (0 < p2 < 1):
        sys.exit(f"ABORT: pre-registered effect puts the challenger rate at {p2:.3f}")
    pbar = (p1 + p2) / 2.0
    num = (_z(1 - alpha / 2) * math.sqrt(2 * pbar * (1 - pbar))
           + _z(power) * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
    return math.ceil(num / (p2 - p1) ** 2)


def check_power_coupling(spec):
    """The rule and the sample size must not be editable independently."""
    if spec['decision_rule'] != 'asymmetric_challenger_must_win':
        sys.exit(f"ABORT: n={spec['n_per_arm']} was computed under the asymmetric rule. "
                 f"decision_rule is {spec['decision_rule']!r} — recompute n before "
                 f"analysing, or the test is underpowered for the rule being applied.")
    want = required_n(spec['baseline_rate'], spec['relative_effect'],
                      spec['alpha'], spec['power'])
    if want != spec['n_per_arm']:
        sys.exit(f"ABORT: pre-registration says n={spec['n_per_arm']} per arm, but "
                 f"baseline {spec['baseline_rate']} +{spec['relative_effect']*100:.0f}% "
                 f"at alpha={spec['alpha']}/power={spec['power']} needs {want}. "
                 f"One of them was edited without the other.")
    print(f"power check       OK — n={want}/arm for +{spec['relative_effect']*100:.0f}% "
          f"on a {spec['baseline_rate']:.1%} baseline, alpha={spec['alpha']}, "
          f"power={spec['power']}")


# ── the data ──────────────────────────────────────────────────────────────────

def load_sidecar(chain, path=None):
    path = Path(path) if path else ROOT / '.ab_test' / f'tags-{chain}.jsonl'
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass                       # a torn final line is not a reason to abort
    return rows


def load_validation(ids):
    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ("select strategy_id, is_gt_score, walk_forward_gt_score "
             "from validation_results where strategy_id in (%s)"
             % ','.join('?' * len(chunk)))
        for sid, is_gt, wf in con.execute(q, chunk):
            out[sid] = {'is_gt_score': is_gt, 'walk_forward_gt_score': wf}
    con.close()
    return out


# ── the tests ─────────────────────────────────────────────────────────────────

def two_proportion(a_hits, a_n, b_hits, b_n):
    """(rate_a, rate_b, p_value) — two-sided pooled z test."""
    if not a_n or not b_n:
        return (0.0, 0.0, float('nan'))
    pa, pb = a_hits / a_n, b_hits / b_n
    pool = (a_hits + b_hits) / (a_n + b_n)
    se = math.sqrt(pool * (1 - pool) * (1 / a_n + 1 / b_n))
    if se == 0:
        return (pa, pb, float('nan'))
    from statistics import NormalDist
    z = (pb - pa) / se
    return (pa, pb, 2 * (1 - NormalDist().cdf(abs(z))))


def nonzero(rows, column, null_is_failure=True, neg_inf_is_failure=False):
    hits = 0
    for r in rows:
        v = r.get(column)
        if v is None:
            if not null_is_failure:
                continue
            v = 0.0
        if neg_inf_is_failure and v <= NEG_INF:
            v = 0.0
        if v != 0:
            hits += 1
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--prereg', default=str(PREREG))
    ap.add_argument('--sidecar', help='override the sidecar path (testing)')
    ap.add_argument('--smoke', type=int, default=0,
                    help='synthesise arms over the N most recent validated strategies; '
                         'proves executability, produces a MEANINGLESS result')
    a = ap.parse_args()

    spec = load_prereg(Path(a.prereg))
    print(f"pre-registration  {a.prereg}")
    print(f"chain             {spec['chain']}   control {spec['control']}   "
          f"challenger {spec['challenger']}")
    check_power_coupling(spec)

    if a.smoke:
        con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
        ids = [r[0] for r in con.execute(
            "select strategy_id from validation_results order by tested_at desc limit ?",
            (a.smoke,))]
        con.close()
        tags = [{'strategy_id': s, 'arm': 'control' if i % 2 == 0 else 'challenger',
                 'git_sha': 'SMOKE', 'failed_closed': False}
                for i, s in enumerate(ids)]
        print("\n*** SMOKE RUN — arms are synthetic (alternating over historical rows).")
        print("*** Every number below is MEANINGLESS. It proves the script runs.\n")
    else:
        tags = load_sidecar(spec['chain'], a.sidecar)
        if not tags:
            print(f"\nNo sidecar at {a.sidecar or '.ab_test/tags-' + spec['chain'] + '.jsonl'} — the experiment "
                  f"has not produced any tagged candidate yet.")
            print("Nothing to analyse. This is the expected state before the run starts.")
            return 0

    # provenance: a mid-run code change is a different experiment
    shas = Counter(t.get('git_sha', '') for t in tags)
    sha_split = len(shas) > 1

    failed_closed = [t for t in tags if t.get('failed_closed')]
    usable = [t for t in tags if not t.get('failed_closed')]

    vals = load_validation({t['strategy_id'] for t in usable})
    excluded_no_validation = [t for t in usable if t['strategy_id'] not in vals]
    joined = [dict(t, **vals[t['strategy_id']]) for t in usable
              if t['strategy_id'] in vals]

    arms = {'control': [r for r in joined if r['arm'] == 'control'],
            'challenger': [r for r in joined if r['arm'] == 'challenger']}

    print("counts            control %d   challenger %d   (target %d/arm)"
          % (len(arms['control']), len(arms['challenger']), spec['n_per_arm']))
    print("excluded          %d failed_closed, %d tagged but never validated"
          % (len(failed_closed), len(excluded_no_validation)))

    prim = spec['primary']
    ch = nonzero(arms['challenger'], prim['column'], prim.get('null_is_failure', True))
    co = nonzero(arms['control'], prim['column'], prim.get('null_is_failure', True))
    pa, pb, p = two_proportion(co, len(arms['control']), ch, len(arms['challenger']))
    print("\nPRIMARY  %s" % prim['metric'])
    print("  control     %3d/%3d = %.1f%%" % (co, len(arms['control']), 100 * pa))
    print("  challenger  %3d/%3d = %.1f%%" % (ch, len(arms['challenger']), 100 * pb))
    print("  p = %.4f (two-sided)" % p)

    for sec in spec.get('secondary', []):
        if sec['metric'] == 'rank_test':
            from scipy.stats import mannwhitneyu
            xs = [r[sec['column']] for r in arms['control']
                  if r.get(sec['column']) not in (None, 0)]
            ys = [r[sec['column']] for r in arms['challenger']
                  if r.get(sec['column']) not in (None, 0)]
            if len(xs) >= 3 and len(ys) >= 3:
                u, pu = mannwhitneyu(xs, ys, alternative='two-sided')
                print("\nSECONDARY rank test on the non-zero tail "
                      "(n %d vs %d): U=%.1f p=%.4f" % (len(xs), len(ys), u, pu))
            else:
                print("\nSECONDARY rank test: too few non-zero values (%d vs %d)"
                      % (len(xs), len(ys)))
        else:
            c2 = nonzero(arms['control'], sec['column'],
                         sec.get('null_is_failure', True),
                         sec.get('neg_inf_is_failure', False))
            h2 = nonzero(arms['challenger'], sec['column'],
                         sec.get('null_is_failure', True),
                         sec.get('neg_inf_is_failure', False))
            r1, r2, p2 = two_proportion(c2, len(arms['control']),
                                        h2, len(arms['challenger']))
            print("\nSECONDARY %s: control %.1f%%  challenger %.1f%%  p=%.4f"
                  % (sec['metric'], 100 * r1, 100 * r2, p2))

    print()
    if sha_split:
        print("VERDICT REFUSED — the git sha changed mid-run, so these rows come from "
              "more than one codebase:")
        for sha, n in shas.most_common():
            print("    %-12s %d rows" % (sha[:12] or '(none)', n))
        return 2
    if a.smoke:
        print("VERDICT WITHHELD — smoke run, synthetic arms.")
        return 0
    if min(len(arms['control']), len(arms['challenger'])) < spec['n_per_arm']:
        print("VERDICT WITHHELD — below the pre-registered n. The stopping rule is a "
              "SINGLE look at n=%d/arm; an interim look at an uncorrected alpha is how "
              "a null result becomes a positive one." % spec['n_per_arm'])
        return 0
    won = (pb > pa) and (p < spec['alpha'])
    print("VERDICT: %s" % (
        "ADOPT the challenger (%s) — it won the primary at alpha=%.2f"
        % (spec['challenger'], spec['alpha']) if won else
        "KEEP the control (%s) — the challenger did not win the primary, and the rule "
        "is asymmetric: a tie or a loss keeps the control on its throughput advantage."
        % spec['control']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
