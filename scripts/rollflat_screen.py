#!/usr/bin/env python3
"""Per-instrument roll-flat headroom: carry avoided per day / cost of a round trip.

Roll-flat pays a full round trip in place of one day's carry, and both legs are
LINEAR in units, so whether it wins is a per-instrument RATIO, not a book-wide
policy. Above 1.0x the swap avoided exceeds the round trip and roll-flat saves
money; below 1.0x applying it is a loss.

WHY THIS IS A SCRIPT NOW. The ratios were last measured by hand on 2026-08-11 and
written into the roll_flat_scope docstring, which still says "BELOW ONE for ETH
(0.85x), BTC (0.65x) and every FX pair". That FX conclusion was an artifact of the
swap tables at the time, not a property of FX:

  * USD_JPY was in NEITHER swap table, so swap_charge returned 0.0 for it. Its
    carry was zero by omission, so its ratio was zero, so it read "below one" for
    the one reason that has nothing to do with economics. It is 1.89x once the
    real card rate is in (added 2026-08-22).
  * EUR_JPY, GBP_JPY and GBP_USD all carried the unsourced -0.000120 placeholder,
    and GBP_USD was in the wrong table on top of that, so its carry was overstated
    ~1.8x and its ratio with it.

A hand-computed constant in a docstring cannot notice when the inputs under it
change. This script recomputes from the live tables so it can.

⚠ THE RATIO IS A LONG-SLEEVE NUMBER. Both swap tables hold ONE rate per
instrument and swap_charge applies it symmetrically, but the broker's card is not
symmetric: AU200_AUD is swapLong -26.0 against swapShort -3.19, so a SHORT position
carries 8.2x cheaper than a long one. The ratio below is computed from the stored
(long-side) rate, so for a short-biased sleeve it OVERSTATES the benefit by up to
that asymmetry factor.

This is not hypothetical. au200aud_auto_20260817_153231_i10 is live, is short 11%
of bars against long 8%, and screens at 1.44x. Costed symmetrically its carry looks
like -26.7% of notional and roll-flat looks worth +23pp of total return. Costed on
the real card it is -12.9%, and roll-flat is worth +0.7pp — nothing. The screen said
"add it"; the sleeve says "do not bother".

So: screen with this, then ALWAYS re-cost the specific sleeve on the real card
before changing a scope. Pass --card to fetch swapLong/swapShort live and see both
ratios. A sleeve's direction mix decides which one applies to it.

COST MODEL. Round trip = 2 x (half spread + per-side commission), both taken from
the simulator's own _half_spread/_commission so this cannot drift from what the
book actually charges. Commission is NOT optional: on USD_JPY it is 5.1x the
spread, and omitting it inflates the headroom from 1.89x to 11.51x — which would
put a marginal instrument in NAS100's league.

WHAT IT DOES NOT ANSWER. Only whether roll-flat is CHEAPER, never whether it is
better: closing daily changes the return stream and its risk, the re-entry fills
at a different price, and roll-flat's first live window had all 8 closes rejected
on session timing. Weekend-flat cannot be screened this way at all — its cost is
foregone edge, not spread (see weekend_flat_scope), so it has to be simulated.

Usage:
    python3 scripts/rollflat_screen.py
    python3 scripts/rollflat_screen.py --units 10000
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import oanda_book_simulator as O

# DELIBERATELY NOT A CONSTANT. The live scope is whatever ROLL_FLAT_INSTRUMENTS is
# set to on the pod, and this process cannot see that. fix_runner's DEFAULT is
# NAS100_USD,DE30_EUR,SPX500_USD and its comment says "the pod does not set
# ROLL_FLAT_INSTRUMENTS" — but the 2026-08-18 decision records the pod scope as
# DE30_EUR,NAS100_USD,XAG_USD,XAU_USD, so that comment is stale and the default is
# not the live value. Hardcoding either would bake in the same kind of stale
# constant this script exists to catch, so the comparison is opt-in: pass
# --live-scope with the scope you have VERIFIED on the pod.


def _price(inst, cache):
    """Representative 2024+ mean close, the same basis the derived rates use."""
    if inst in cache:
        return cache[inst]
    from data_fetcher import get_candles_date_range
    df = get_candles_date_range(inst, '2024-01-01', '2026-08-21', 'D')
    cache[inst] = float(df['close'].mean())
    return cache[inst]


def _quote_to_usd(inst, cache):
    pair, inverse = O._quote_to_usd_pair(inst)
    if pair is None:
        return 1.0
    p = _price(pair, cache)
    return 1.0 / p if inverse else p


def headroom(inst, units, cache):
    px = _price(inst, cache)
    q2u = _quote_to_usd(inst, cache)
    notional = units * px * q2u                     # USD

    carry_usd = abs(O.swap_charge(inst, units, px, q2u, 1, False))
    rt_usd = 2 * (O._half_spread(inst, units, px, q2u)
                  + O._commission(inst, units, px, q2u, venue='ctrader', sides=1))
    return {
        'price': px,
        'carry_pct': 100 * carry_usd / notional,
        'rt_pct': 100 * rt_usd / notional,
        'ratio': (carry_usd / rt_usd) if rt_usd else float('inf'),
        'derived': inst in O.SWAP_DERIVED,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--units', type=int, default=10_000)
    ap.add_argument('--card', action='store_true',
                    help="fetch swapLong/swapShort live and show the SHORT-side "
                         "ratio too; without it every ratio is the long-side one")
    ap.add_argument('--live-scope', default=None,
                    help='comma-separated ROLL_FLAT_INSTRUMENTS as VERIFIED on the '
                         'pod; omit to skip the on/off-leg comparison entirely')
    a = ap.parse_args()

    insts = sorted(set(O.SWAP_PER_UNIT_DAY) | set(O.SWAP_PCT_NOTIONAL_DAY))
    cache: dict[str, float] = {}
    rows = []
    for i in insts:
        try:
            rows.append((i, headroom(i, a.units, cache)))
        except Exception as e:
            print(f'  {i:<12} skipped: {type(e).__name__}: {e}')

    live = ({i.strip() for i in a.live_scope.split(',') if i.strip()}
            if a.live_scope else None)
    card = {}
    if a.card:
        import json as _json
        from swap_card import fetch, SYMS
        with open(SYMS) as fh:
            known = set(_json.load(fh)['instruments'])
        # WHEAT_USD and anything else absent from the catalogue has no card to fetch;
        # asking for it aborts the whole request, so drop it rather than lose the run
        want = [i for i, _ in rows if i in known]
        skipped = [i for i, _ in rows if i not in known]
        try:
            card = fetch(want)
        except Exception as e:
            print(f'  --card fetch failed ({type(e).__name__}: {e}); long-side only')
        if skipped:
            print(f'  no broker card for {skipped} — long-side ratio only for those')

    rows.sort(key=lambda kv: -kv[1]['ratio'])
    print(f'\n{"instrument":<12}{"carry %/day":>12}{"round trip %":>14}'
          f'{"headroom":>10}  flags')
    for inst, h in rows:
        flags = []
        if live is not None:
            if h['ratio'] >= 1.0 and inst not in live:
                flags.append('CLEARS 1.0x, not on the live leg')
            if h['ratio'] < 1.0 and inst in live:
                flags.append('ON THE LIVE LEG BUT BELOW 1.0x')
        cd = card.get(inst)
        if cd and cd['per_unit_day_long'] and cd['per_unit_day_short']:
            asym = cd['per_unit_day_long'] / cd['per_unit_day_short']
            if asym > 1.5:
                flags.append(f'SHORT carries {asym:.1f}x cheaper -> short-side ratio '
                             f'only {h["ratio"]/asym:.2f}x')
        if h['derived']:
            flags.append('rate DERIVED, unconfirmed by an accrual')
        print(f'{inst:<12}{h["carry_pct"]:>12.5f}{h["rt_pct"]:>14.5f}'
              f'{h["ratio"]:>9.2f}x  {"; ".join(flags)}')

    if live is None:
        print('\nno --live-scope given, so nothing is compared against the pod. '
              'Verify ROLL_FLAT_INSTRUMENTS on the pod and pass it to get that column.')
    else:
        print(f'\nlive roll-flat leg (as supplied): {sorted(live)}')
    print('above 1.0x = roll-flat is CHEAPER for a LONG position. It is not '
          'automatically better, the ratio does not hold for a short-biased sleeve '
          '(pass --card), and changing the live scope is a DEPLOY.')


if __name__ == '__main__':
    main()
