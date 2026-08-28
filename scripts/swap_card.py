#!/usr/bin/env python3
"""Read the broker's PUBLISHED swap card for one or more instruments.

READ-ONLY at the broker: issues ProtoOASymbolByIdReq and nothing else. Places no
order, amends no position, touches no runner state. Same safety class as
scripts/swap_log.py, and unlike that script it needs NO open position -- which is
the whole point: it is how an instrument the account has never held gets a swap
rate instead of the silent 0.0 that swap_charge() returns for an absent key.

The conversion rule (decided 2026-08-14):

    per-unit-per-day, in the QUOTE currency = swapLong / 10**pipPosition

For a USD-quoted instrument that number goes straight into
oanda_book_simulator.SWAP_PER_UNIT_DAY. For anything quoted in another currency
it needs an FX leg, so it is divided by a representative price and recorded in
SWAP_PCT_NOTIONAL_DAY as a fraction of notional instead -- the same treatment
AU200_AUD and HK33_HKD got on 2026-08-18.

--verify re-derives the MEASURED rates in the simulator from the live card and
prints the error against them. Run it EVERY time before trusting a newly derived
rate: it is the only check that the rule still holds and that the card has not
changed shape.

KNOWN EXCEPTION, found 2026-08-22 — THE RULE FAILS AT pipPosition 0. NAS100_USD
has pipPosition 0 and a card swapLong of -3.575, so the rule derives -3.575/unit/day.
The MEASURED rate is -35.875, i.e. the rule is 10x too small, and the measurement is
right: broker_swap position 4424307 held 0.01 units and took -1.07 USD on the
2026-07-31 Friday 3-day roll, which is -35.67/unit/day (0.6% off the stored value).
WHICH EXPONENTS ARE ACTUALLY VALIDATED. Only a check against a MEASURED rate
counts. Agreeing with NATGAS/XCU/AU200/HK33 proves nothing — those four were
themselves produced by this rule, so that comparison is circular. Against real
measurements:

    pip 2  VALIDATED   XAG 0.23%, XAU 0.11% (both USD-quoted)
    pip 4  VALIDATED   EUR_USD 2.0% (USD-quoted)
    pip 5  VALIDATED   XCU 1.4%, measured 2026-08-28 (USD-quoted)
    pip 0  DISPROVEN   NAS100 is 10x off, see above
    pip 1  UNVALIDATED only NATGAS sits here, and it is derived

The non-USD-quoted majors read 19-25% off (AUD_USD, USD_CHF, EUR_GBP) but those
stored values are rough or need an FX leg the raw card figure does not carry, so
they neither confirm nor refute the exponent.

CONSEQUENCE: derive only at pip 2, 4 or 5. Never at pip 0 — measure from a real
accrual instead — and treat pip 1 as provisional. --verify refuses to bless any
target outside RULE_OK_PIPS.

pip 5 was UNVALIDATED until 2026-08-28, when XCU_USD took the first accrual this
account has ever recorded on it: broker_swap position 4720262, 500 units, -0.22 USD
on the 2026-08-27 (Thu) single-day roll = -0.00044/unit/day against a derived
-0.0004341, a 1.4% error. WTICO_USD took its own measured -0.70 on the SAME roll,
which is what rules out a Friday triple inflating the XCU figure 3x. XCU therefore
stopped being circular evidence and became a measurement, and it left
oanda_book_simulator.SWAP_DERIVED in the same change.

Usage:
    python3 scripts/swap_card.py USD_JPY
    python3 scripts/swap_card.py --verify
    python3 scripts/swap_card.py --verify USD_JPY EUR_JPY GBP_JPY
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYMS = os.path.join(REPO, 'ctrader_symbols.json')

# The three rates in oanda_book_simulator.SWAP_PER_UNIT_DAY that were MEASURED
# from real accruals on this account, not derived. --verify checks the rule
# against these; they span pipPosition 0, 2 and 2.
MEASURED = {'NAS100_USD': -35.875, 'XAG_USD': -0.042800, 'XAU_USD': -0.890,
            # first accrual 2026-08-28 (pos 4720262, 500u, -0.22 on a single-day
            # roll). The stored rate is the DERIVED -0.0004341; this is what the
            # broker actually charged, and it is the only evidence for pip 5.
            'XCU_USD': -0.000440}
# pipPosition values at which the rule reproduces a MEASURED rate. 1 is excluded
# because the only instrument sitting there (NATGAS) was itself derived by this
# rule, so it cannot validate it, and 0 is excluded because NAS100 disproves it
# outright. 5 JOINED the set 2026-08-28: XCU stopped being circular evidence when
# it took a real accrual. See the note above.
RULE_OK_PIPS = {2, 4, 5}


def fetch(instruments: list[str]) -> dict[str, dict]:
    from ctrader_client import get_client
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASymbolByIdReq

    with open(SYMS) as fh:
        cat = json.load(fh)['instruments']
    missing = [i for i in instruments if i not in cat]
    if missing:
        raise SystemExit(f'not in ctrader_symbols.json: {missing}')
    by_id = {cat[i]['symbol_id']: i for i in instruments}

    cli = get_client().start()
    req = ProtoOASymbolByIdReq()
    req.ctidTraderAccountId = cli.account_id
    req.symbolId.extend(sorted(by_id))

    out = {}
    for sym in cli.send(req).symbol:
        inst = by_id.get(sym.symbolId)
        if not inst:
            continue
        pip = getattr(sym, 'pipPosition', None)
        sl = getattr(sym, 'swapLong', None)
        ss = getattr(sym, 'swapShort', None)
        out[inst] = {
            'symbol_id': sym.symbolId,
            'pip_position': pip,
            'swap_long_raw': sl,
            'swap_short_raw': ss,
            'swap_rollover_3days': getattr(sym, 'swapRollover3Days', None),
            'swap_calculation_type': getattr(sym, 'swapCalculationType', None),
            # the rule: quote-currency units per base unit per day
            'per_unit_day_long': (sl / 10 ** pip) if (sl is not None and pip is not None) else None,
            'per_unit_day_short': (ss / 10 ** pip) if (ss is not None and pip is not None) else None,
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('instruments', nargs='*')
    ap.add_argument('--verify', action='store_true',
                    help='re-derive the 3 measured rates and print the error')
    a = ap.parse_args()

    want = list(dict.fromkeys((list(MEASURED) if a.verify else []) + a.instruments))
    if not want:
        raise SystemExit('nothing to fetch: pass instruments or --verify')

    cards = fetch(want)

    print(f'{"instrument":<12} {"pip":>3} {"swapLong":>10} {"swapShort":>10} '
          f'{"long/unit/day":>15} {"short/unit/day":>15} {"3day":>5}')
    for inst in want:
        c = cards.get(inst)
        if not c:
            print(f'{inst:<12}  -- no card returned --')
            continue
        print(f'{inst:<12} {c["pip_position"]!s:>3} {c["swap_long_raw"]!s:>10} '
              f'{c["swap_short_raw"]!s:>10} {c["per_unit_day_long"]!s:>15} '
              f'{c["per_unit_day_short"]!s:>15} {c["swap_rollover_3days"]!s:>5}')

    if a.verify:
        print('\n-- rule check vs the MEASURED rates in oanda_book_simulator --')
        for inst, measured in MEASURED.items():
            c = cards.get(inst)
            if not c or c['per_unit_day_long'] is None:
                print(f'  {inst:<12} NO CARD — cannot verify')
                continue
            derived, pip = c['per_unit_day_long'], c['pip_position']
            err = abs(derived - measured) / abs(measured) * 100 if measured else float('inf')
            note = '' if pip in RULE_OK_PIPS else '   <- pip not in RULE_OK_PIPS'
            print(f'  {inst:<12} pip {pip}  derived {derived:>12.6f}  '
                  f'measured {measured:>12.6f}  err {err:5.2f}%{note}')

        targets = [i for i in a.instruments if i in cards]
        if targets:
            print('\n-- is a derived rate safe for the requested instruments? --')
            for inst in targets:
                pip = cards[inst]['pip_position']
                ok = pip in RULE_OK_PIPS
                print(f'  {inst:<12} pip {pip}  '
                      f'{"SAFE to derive" if ok else "DO NOT DERIVE — measure from a real accrual"}')


if __name__ == '__main__':
    main()
