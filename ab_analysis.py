#!/usr/bin/env python
"""Analyse the data-grounded (fingerprint) A/B test.

Splits every strategy generated during the test into the ON (fingerprint) or OFF
(control) arm by created_at — using the per-batch arm windows in
.ab_test/ledger.jsonl — and compares the IS-score DISTRIBUTIONS (the only
meaningful signal; individual strategies are noise). Run after ~24h:

    ./venv/bin/python ab_analysis.py
"""
import json
import sqlite3
import statistics as st
from datetime import datetime
from pathlib import Path

LEDGER = Path(__file__).parent / '.ab_test' / 'ledger.jsonl'
DB = Path(__file__).parent / 'pipeline.db'


def _arm_windows():
    rows = [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()]
    rows.sort(key=lambda r: r['start'])
    out = []
    for i, r in enumerate(rows):
        end = rows[i + 1]['start'] if i + 1 < len(rows) else '9999'
        out.append((r['start'], end, r['arm']))
    return out


def _arm_for(ts, windows):
    for start, end, arm in windows:
        if start <= ts < end:
            return arm
    return None


def _summary(name, scores, passes, n_strat):
    if not scores:
        print(f"  {name:4}: no validated strategies"); return
    above = sum(1 for s in scores if s >= 0.30)
    print(f"  {name:4}: n={len(scores):4}  passed={passes:3}  "
          f"IS mean={st.mean(scores):.3f} median={st.median(scores):.3f} "
          f"p75={_pct(scores,75):.3f} max={max(scores):.3f}  "
          f">=0.30: {above} ({above/len(scores)*100:.1f}%)")


def _pct(xs, p):
    xs = sorted(xs); k = (len(xs) - 1) * p / 100
    f = int(k); return xs[f] if f + 1 >= len(xs) else xs[f] + (k - f) * (xs[f + 1] - xs[f])


def _mwu_greater_p(a, b):
    """One-sided Mann-Whitney U p-value (H1: a > b), normal approx + tie correction."""
    import math
    from collections import Counter
    na, nb, n = len(a), len(b), len(a) + len(b)
    if na == 0 or nb == 0 or n < 2:
        return None
    combined = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and combined[j + 1][0] == combined[i][0]:
            j += 1
        avg = (i + j) / 2 + 1  # 1-based average rank
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    Ra = sum(rk for rk, (_, g) in zip(ranks, combined) if g == 0)
    Ua = Ra - na * (na + 1) / 2
    mu = na * nb / 2
    ties = sum(t ** 3 - t for t in Counter(v for v, _ in combined).values())
    sigma = math.sqrt(na * nb / 12 * ((n + 1) - ties / (n * (n - 1))))
    if sigma == 0:
        return None
    z = (Ua - mu) / sigma
    return 0.5 * math.erfc(z / math.sqrt(2))  # upper tail: a greater


def _inst(sid):
    import re
    m = re.match(r'([a-z0-9]+)_auto_', sid or '')
    return m.group(1) if m else (sid or 'unknown').split('_')[0]


def _entropy(items):
    """Shannon entropy (bits) over the item frequencies — a diversity measure."""
    from collections import Counter
    import math
    n = len(items)
    if not n:
        return 0.0
    return -sum((c / n) * math.log2(c / n) for c in Counter(items).values())


def main():
    if not LEDGER.exists():
        print("No A/B ledger yet — the test hasn't run."); return
    windows = _arm_windows()
    t0, t1 = windows[0][0], windows[-1][1]
    print(f"A/B window: {t0[:19]} → now   ({len(windows)} batches: "
          f"{sum(1 for w in windows if w[2]=='on')} on / {sum(1 for w in windows if w[2]=='off')} off)\n")

    conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT s.id, s.created_at, v.is_gt_score, v.walk_forward_gt_score, v.final_status "
        "FROM strategies s JOIN validation_results v ON v.strategy_id=s.id "
        "WHERE s.created_at >= ? ORDER BY s.created_at", (t0,)).fetchall()

    import math
    arms = {'on': {'is': [], 'pass': 0, 'n': 0, 'insts': []},
            'off': {'is': [], 'pass': 0, 'n': 0, 'insts': []}}
    for r in rows:
        arm = _arm_for(r['created_at'], windows)
        if arm not in arms:
            continue
        arms[arm]['n'] += 1
        arms[arm]['insts'].append(_inst(r['id']))
        v = r['is_gt_score']
        if v is not None and math.isfinite(v):
            arms[arm]['is'].append(v)
        if 'pass' in (r['final_status'] or '').lower():
            arms[arm]['pass'] += 1

    print("IS-score distribution of validated strategies, by arm:")
    _summary('ON', arms['on']['is'], arms['on']['pass'], arms['on']['n'])
    _summary('OFF', arms['off']['is'], arms['off']['pass'], arms['off']['n'])

    # Diversity guardrail — if a data-driven arm lifts IS but DROPS instrument
    # entropy, it's exploiting into a corner (mode collapse). Watch both together.
    print("\nDiversity (exploration guardrail), by arm:")
    for nm, key in (('ON', 'on'), ('OFF', 'off')):
        ins = arms[key]['insts']
        print(f"  {nm:4}: {len(set(ins))} distinct instruments  entropy={_entropy(ins):.2f} bits  (n={len(ins)})")

    # significance of the IS-distribution shift — pure-python Mann-Whitney U
    # (rank-sum), one-sided H1: ON stochastically greater than OFF.
    if arms['on']['is'] and arms['off']['is']:
        p = _mwu_greater_p(arms['on']['is'], arms['off']['is'])
        d = st.mean(arms['on']['is']) - st.mean(arms['off']['is'])
        verdict = ('ON shifts the IS distribution UP (significant)' if (p is not None and p < 0.05)
                   else 'no significant shift — fingerprint not helping the IS distribution')
        p_str = f"{p:.3f}" if p is not None else "n/a"
        print(f"\nMann-Whitney U (ON > OFF): p={p_str}   ON−OFF mean IS={d:+.3f}   → {verdict}")
        print("  (need a few hundred validated theses/arm for power; check n above)")

    # struct-rejected per arm (best-effort, from the forever logs)
    try:
        n_rej = 0
        for lg in (Path(__file__).parent / '.auto-research-logs').glob('forever_*.log'):
            n_rej += lg.read_text(errors='replace').count('Contradicts measured structure')
        print(f"\nStruct-rejected (contradiction gate, ON arm only, all logs): {n_rej}")
    except Exception:
        pass
    conn.close()


if __name__ == '__main__':
    main()
