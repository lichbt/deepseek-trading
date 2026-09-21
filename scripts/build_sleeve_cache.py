#!/usr/bin/env python3
"""Rebuild the sleeve cache that risk_model_sim reads.

WHY THIS EXISTS. risk_model_sim takes its book from a PICKLE
(.scratch/costed/sleeves_ctrader.pkl, DEFAULT_SLEEVES) rather than from
pipeline.db, and it only ever does `Path(args.sleeves).read_bytes()` — it cannot
rebuild. The pickle was built inline during the 2026-08-11 carry-policy session
and no script was left behind, so it silently froze: by 2026-09-03 it still held
22 sleeves whose USD_CHF entry (usdchf_auto_20260706_133908_i21) had been retired
on 2026-08-21, while the live book was 23.

Everything that harness produced in between describes a book that no longer
exists — including the 2026-08-17 BTC/EUR_GBP roll-flat rejection, whose evidence
records "22 sleeves", the same pickle. RELATIVE deltas between arms survive that
(same book on both sides); absolute returns, pass rates and per-instrument carry
bills do not.

    ./venv/bin/python scripts/build_sleeve_cache.py            # default window
    ./venv/bin/python scripts/build_sleeve_cache.py --start 2024-01-01
"""
import argparse
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import oanda_book_simulator as S  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2024-01-01')
    ap.add_argument('--end', default=(datetime.now(timezone.utc)).date().isoformat())
    ap.add_argument('--warmup-days', type=int, default=400)
    ap.add_argument('--state', default=str(ROOT / 'portfolio_state.json'))
    ap.add_argument('--out', default=str(ROOT / '.scratch' / 'costed' / 'sleeves_ctrader.pkl'))
    a = ap.parse_args()

    sleeves = S.load_sleeves(a.start, a.end, a.warmup_days, a.state)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pickle.dumps(sleeves))

    print(f'wrote {out}  ({out.stat().st_size/1e6:.1f} MB)')
    print(f'{len(sleeves)} sleeves, {a.start} -> {a.end}, warmup {a.warmup_days}d')
    for s in sorted(sleeves, key=lambda s: s.sid):
        print(f'  {s.sid:38s} {s.instrument:11s} ws={s.weight_scale:.3f}')


if __name__ == '__main__':
    main()
