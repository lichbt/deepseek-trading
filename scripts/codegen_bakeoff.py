"""End-to-end bake-off for CODEGEN_MODELS candidates.

    ./venv/bin/python scripts/codegen_bakeoff.py N model_a model_b ...

WHY END-TO-END: parse-success is a TRAP. The 2026-08-04 bake-off measured
seed-2-0-pro at 100% parse and 13% usable — it omitted its own imports and blew
up at run time on 26 of 40. So a candidate is scored only if it survives every
step the real pipeline puts it through:

    1. _validate_code        (the auto_research pre-flight + auto-repairs)
    2. param_grid <= 4 keys  (a 5th key is a hard reject; the strategy is dropped)
    3. create_strategy_function
    4. runs on a REAL price frame without raising
    5. emits a NON-DEGENERATE signal (both a long and a flat state, not all-zero
       and not all-one) — code that never trades "passes" every earlier step

Theses come from real `strategies` rows so the prompts match production shape.
Cost is priced from scripts/llm_prices.py at the hour given by --hour.

This ranks USABILITY and COST. It does NOT rank the quality of the resulting
strategies — only the validator's walk-forward score can do that, and at the
measured pass rate that needs far more samples than this harness runs.
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

import auto_research as ar
from validator import create_strategy_function
import llm_prices as lp


def load_theses(n):
    """Real theses from the critique corpus.

    NOT pipeline.db: `strategies` stores the generated code and rationale but
    NOT the entry/filter/exit conditions, so a prompt rebuilt from it would be
    the wrong shape. The critique corpus records the FULL thesis dict, which is
    exactly what the code-gen step receives in production. Only theses the gate
    PASSED are used — a rejected one never reaches code-gen.
    """
    import auto_research as _ar
    out = []
    for line in reversed(open(_ar._CRITIQUE_LOG).read().splitlines()):
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get('verdict') != 'pass':
            continue
        th = row.get('thesis') or {}
        if not (th.get('entry_condition') and th.get('rationale')):
            continue
        out.append((row.get('instrument'), th.get('timeframe'), th.get('strategy_family'),
                    th.get('rationale'), th.get('entry_condition'),
                    th.get('filter_condition'), th.get('exit_condition'),
                    json.dumps(th.get('param_hints')) if th.get('param_hints') else None))
        if len(out) >= n:
            break
    return out


def real_frame():
    import pandas as pd
    import numpy as np
    # A deterministic frame with every column the archetypes may touch. Not a
    # market simulation — this step only asks "does the code RUN and signal?".
    n = 400
    idx = pd.date_range('2024-01-01', periods=n, freq='D')
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    df = pd.DataFrame({
        'date': idx, 'open': close + rng.normal(0, .2, n),
        'high': close + abs(rng.normal(0, .6, n)), 'low': close - abs(rng.normal(0, .6, n)),
        'close': close, 'volume': rng.integers(1e3, 1e4, n).astype(float),
    })
    for col in ('fed_rate', 'us10y', 'us_real_yield', 'us_cpi', 'dxy', 'close_leg2', 'spread'):
        df[col] = 1 + np.cumsum(rng.normal(0, .05, n))
    # Calendar columns the pipeline INJECTS (codegen.md documents df['dow'] and
    # df['turn_of_month'] as available). Leaving them out made legitimate code
    # fail with KeyError and understated every arm equally — a harness artefact
    # that reads exactly like a model failure.
    df['dow'] = idx.dayofweek
    df['tdom'] = idx.day
    df['turn_of_month'] = ((idx.day >= 28) | (idx.day <= 3)).astype(int)
    df['event_window'] = 0
    df['session'] = 'London'
    df['days_to_event'] = rng.integers(0, 60, n)
    df['days_since_event'] = rng.integers(0, 60, n)
    return df


def score_one(candidate, df):
    """Return (ok, reason). Every gate the real pipeline applies, in order."""
    if not isinstance(candidate, dict):
        return False, 'no candidate'
    err, clean = ar._validate_code(candidate.get('code') or '')
    if err:
        return False, f'validate: {err}'
    grid = candidate.get('param_grid') or {}
    if len(grid) > 4:
        return False, f'param_grid has {len(grid)} keys (max 4)'
    try:
        fn = create_strategy_function(clean)
    except Exception as e:
        return False, f'compile: {type(e).__name__}: {str(e)[:60]}'
    try:
        # The contract is generate_signals(df, params) — a params DICT, one
        # positional arg. Splatting it as **kwargs made every arm score 0/12
        # with "unexpected keyword argument", which is a harness bug that looks
        # exactly like a model failure. See validator.py:92.
        params = {k: (v[0] if isinstance(v, (list, tuple)) and v else v) for k, v in grid.items()}
        sig = fn(df.copy(), params)
    except Exception as e:
        return False, f'run: {type(e).__name__}: {str(e)[:60]}'
    try:
        vals = set(int(x) for x in sig.fillna(0).unique())
    except Exception:
        return False, 'signal not a numeric series'
    if len(vals) < 2:
        return False, f'degenerate signal (only {vals})'
    return True, 'ok'


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('n', type=int, help='theses per arm')
    ap.add_argument('models', nargs='+')
    ap.add_argument('--hour', type=int, default=23, help='local hour to price at (default night)')
    args = ap.parse_args(argv)

    theses = load_theses(args.n)
    print(f'{len(theses)} real theses per arm; pricing at {args.hour}:00 '
          f'({"off-peak" if lp.is_offpeak(datetime(2026,1,1,args.hour)) else "peak"})\n')
    df = real_frame()
    spec_tmpl, static = ar._split_codegen_template()
    system = ar._CODE_SYSTEM_PROMPT + '\n\n' + static
    when = datetime(2026, 1, 1, args.hour)

    for model in args.models:
        ar.CODE_FALLBACK_MODELS = [model]     # pin the arm; no chain fallback
        usable, cost, fails = 0, 0.0, []
        log = os.path.join(ROOT, '.auto-research-logs', f'bakeoff_{model.replace("/","_").replace(":","_")}.jsonl')
        if os.path.exists(log):
            os.remove(log)
        ar._USAGE_LOG_PATH = log
        for t in theses:
            inst, tf, fam, rat, ent, filt, ext, hints = t
            prompt = spec_tmpl.format(
                instrument=inst, timeframe=tf or 'D', family=fam or 'momentum',
                hypothesis=rat, entry=ent, filter=filt or 'none', exit=ext or 'none',
                param_hints=hints or '{"lookback": [10, 20]}')
            res = ar.generate_code_via_openrouter(prompt, system_prompt=system)
            ok, why = (False, f"gen: {str(res.get('error'))[:60]}") if not res.get('success') \
                else score_one(res.get('candidate'), df)
            usable += ok
            if not ok:
                fails.append(why)
        for line in open(log):
            c = lp.cost_of(json.loads(line), when)
            if c:
                cost += c
        n = len(theses)
        print(f'{model:34s} usable {usable}/{n} ({100*usable/n:3.0f}%)  '
              f'cost ${cost:.5f}  ${cost/n:.6f}/call')
        for f in fails[:4]:
            print(f'    fail: {f}')


if __name__ == '__main__':
    main()
