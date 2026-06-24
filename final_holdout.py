#!/usr/bin/env python
"""Score ONE strategy on the LOCKED holdout — the only place that window is read.

The validation loop stops at validator.LOCKED_HOLDOUT_START (see validator.py);
the most recent window is never touched during the search, so it isn't mined.
Run this manually for the single final winner, once, to get an honest
out-of-sample read the loop never had access to.

Usage:  source ~/.zshrc && ./venv/bin/python final_holdout.py <strategy_id>
"""
import sys
import json
import sqlite3
from datetime import datetime

import numpy as np
import pandas as pd

import validator as V
from pipeline_utils import compute_net_strategy_returns
from portfolio import _infer_instrument, _infer_archetype


def score(strategy_id: str) -> dict:
    con = sqlite3.connect("pipeline.db"); con.row_factory = sqlite3.Row
    s = con.execute("SELECT code, timeframe FROM strategies WHERE id=?", (strategy_id,)).fetchone()
    v = con.execute("SELECT best_params FROM validation_results WHERE strategy_id=? "
                    "ORDER BY tested_at DESC LIMIT 1", (strategy_id,)).fetchone()
    con.close()
    if not s or not v:
        raise SystemExit(f"{strategy_id}: not found / never validated")
    bp = json.loads(v["best_params"]); inst = _infer_instrument(strategy_id)
    tf = s["timeframe"]; arch = _infer_archetype(s["code"])
    func = V.create_strategy_function(s["code"])
    end = datetime.now().strftime("%Y-%m-%d")
    df = V.get_candles_date_range(inst, V.LOCKED_HOLDOUT_START, end, granularity=tf)
    if arch != "standard":
        df = V.inject_supplementary_data(df, arch, inst, None, V.LOCKED_HOLDOUT_START, end, tf)
    ret = compute_net_strategy_returns(df, func(df, bp), inst, tf, params=bp)
    ret = np.asarray(ret, dtype=float)
    sd = ret.std()
    return dict(bars=len(ret), trades=int((ret != 0).sum()),
                daily_sharpe=float(ret.mean() / sd) if sd > 0 else 0.0,
                total_return=float((1 + pd.Series(ret)).prod() - 1),
                window=f"{V.LOCKED_HOLDOUT_START} -> {end}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: final_holdout.py <strategy_id>")
    r = score(sys.argv[1])
    print(f"LOCKED HOLDOUT [{r['window']}]  {sys.argv[1]}")
    print(f"  bars={r['bars']} trades={r['trades']} "
          f"daily_sharpe={r['daily_sharpe']:.3f} total_return={r['total_return']:+.1%}")
