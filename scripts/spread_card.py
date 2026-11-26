#!/usr/bin/env python3
"""Live cTrader/The5ers SPREAD card — the round-trip cost on the venue we trade.

WHY THIS EXISTS. Swap and commission are both priced off the broker's own card
(scripts/swap_card.py, oanda_book_simulator.CTRADER_COMMISSION). Spread was not:
oanda_book_simulator._half_spread calls pipeline_utils.get_spread_pips, which is
OANDA's live quote with a static TYPICAL_SPREADS_PIPS fallback. So the validator,
the simulator and rollflat_screen all price the round trip on a venue this account
does not trade.

That matters less for the total bill than it sounds — swap is ~95% of round-trip
cost and commission ~1% (2026-08-11) — but the round trip is the DENOMINATOR of
every roll-flat and weekend-flat headroom ratio, and those ratios pick the carry
policy. XAG's 1.38x, NAS100's 17.94x and AU200's 0.72x were all computed on an
OANDA spread. Within OANDA alone the static and live figures differ by 80% on
AU200 (1.8 vs 1.0 pips), which is enough to move a ratio across 1.0.

WHAT THIS IS NOT. A swap card is a PUBLISHED CONSTANT; a spread is not. This is a
SNAPSHOT, and it is only as representative as the moment it was taken. Spreads
widen in thin liquidity, around the rollover and at the session edges, so a
sample taken in the Asian session is an UPPER BOUND on the European one. Every
row carries its timestamp for that reason. Use --samples to average over several
ticks, and re-run in the session a sleeve actually trades before trusting a
ratio that sits near 1.0.

Prices arrive scaled by a FIXED 1e5 (ctrader_client.SPOT_SCALE), never by the
symbol's own `digits` — the 2026-07-31 bug that read NAS100 as 28,465,810.
"""
import argparse, json, os, statistics, sys, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYMS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'ctrader_symbols.json')


def fetch(instruments, samples=1, gap=1.0):
    from ctrader_client import get_client
    with open(SYMS) as fh:
        cat = json.load(fh)['instruments']
    missing = [i for i in instruments if i not in cat]
    if missing:
        raise SystemExit(f'not in ctrader_symbols.json: {missing}')
    cli = get_client().start()
    out = {}
    for inst in instruments:
        sid = cat[inst]['symbol_id']
        obs = []
        for k in range(samples):
            try:
                bid, ask = cli.get_price(sid)
                if bid and ask and ask > bid:
                    obs.append((bid, ask))
            except Exception as e:
                out.setdefault(inst, {})['error'] = f'{type(e).__name__}: {e}'[:70]
                break
            if k + 1 < samples:
                time.sleep(gap)
        if obs:
            sp = [a - b for b, a in obs]
            mid = statistics.fmean([(a + b) / 2 for b, a in obs])
            out[inst] = {
                'symbol_id': sid, 'n': len(obs), 'mid': mid,
                'spread_abs': statistics.fmean(sp),
                'spread_min': min(sp), 'spread_max': max(sp),
                'spread_pct_notional': statistics.fmean(sp) / mid,
                'sampled_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            }
        else:
            out.setdefault(inst, {}).setdefault('error', 'no tick (market shut?)')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('instruments', nargs='*')
    ap.add_argument('--samples', type=int, default=1)
    ap.add_argument('--gap', type=float, default=1.0, help='seconds between samples')
    ap.add_argument('--compare', action='store_true',
                    help='show what the OANDA model currently charges instead')
    ap.add_argument('--json', help='write the card to this path')
    a = ap.parse_args()
    if not a.instruments:
        raise SystemExit('pass instruments')

    card = fetch(a.instruments, a.samples, a.gap)
    hdr = f'{"instrument":<12}{"n":>3}{"mid":>12}{"spread":>11}{"% notional":>12}'
    if a.compare:
        hdr += f'{"OANDA %":>10}{"ratio":>8}'
    print(hdr)
    if a.compare:
        from pipeline_utils import get_spread_pips, get_pip_value
    for inst in a.instruments:
        c = card.get(inst, {})
        if 'error' in c:
            print(f'{inst:<12}{"":>3}  {c["error"]}')
            continue
        line = (f'{inst:<12}{c["n"]:>3}{c["mid"]:>12.4f}{c["spread_abs"]:>11.5f}'
                f'{100*c["spread_pct_notional"]:>11.5f}%')
        if a.compare:
            o = get_spread_pips(inst) * get_pip_value(inst) / c['mid']
            line += f'{100*o:>9.5f}%{c["spread_pct_notional"]/o:>7.2f}x'
        print(line)
    if a.json:
        with open(a.json, 'w') as fh:
            json.dump(card, fh, indent=1)
        print(f'\nwrote {a.json}')
    print('\nSNAPSHOT, not a published constant — spreads widen in thin liquidity, '
          'near the rollover and at session edges. Re-sample in the session that '
          'matters before trusting a ratio near 1.0.')


if __name__ == '__main__':
    main()
