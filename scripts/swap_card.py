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

SUPERSEDED 2026-09-07 — pipPosition 0 IS VALIDATED. This docstring used to lead
with "KNOWN EXCEPTION, found 2026-08-22 — THE RULE FAILS AT pipPosition 0", on the
grounds that NAS100_USD (pip 0) had a card swapLong of -3.575 against a MEASURED
-35.875, so the rule looked 10x too small. That reading was wrong, and the way it
was wrong is the lesson:

    A STORED MEASUREMENT HAS A DATE. A fresh card disagreeing with a stale
    measurement cannot tell "the rule is broken" from "the rate moved". It means
    RE-MEASURE — never re-derive the rule to fit one card against one old number.

The broker CUT the NAS100 rate ~10x between 2026-08-10 and 2026-08-22; the card
read -3.575 on 2026-08-22 because that was already the new rate. A re-measurement
on the 2026-09-04 Friday roll (two positions, 0.44u + 0.23u, one 3-day window,
unit-weighted -3.577 USD/unit/day) agrees with the card to 0.06%. The tell the
2026-08-22 read missed: the disagreement was a clean 10x on the ONE symbol whose
card had just changed, while every other symbol still agreed to <1.5%.

WHICH EXPONENTS ARE ACTUALLY VALIDATED. Only a check against a MEASURED rate
counts. Agreeing with NATGAS/XCU/AU200/HK33 proves nothing — those four were
themselves produced by this rule, so that comparison is circular. Against real
measurements:

    pip 0  VALIDATED   NAS100 0.06%, re-measured 2026-09-04 (USD-quoted)
    pip 2  VALIDATED   XAG 0.23%, XAU 0.11% (both USD-quoted)
    pip 4  VALIDATED   EUR_USD 2.0% (USD-quoted)
    pip 5  VALIDATED   XCU 1.34%, measured 2026-08-28 (USD-quoted)
    pip 1  UNVALIDATED only NATGAS sits here, and it is derived

The non-USD-quoted majors read 19-25% off (AUD_USD, USD_CHF, EUR_GBP) but those
stored values are rough or need an FX leg the raw card figure does not carry, so
they neither confirm nor refute the exponent.

CONSEQUENCE: derive at pip 0, 2, 4 or 5, and treat pip 1 as provisional —
measure it from a real accrual before trusting it. --verify refuses to bless any
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
    python3 scripts/swap_card.py --verify-all
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYMS = os.path.join(REPO, 'ctrader_symbols.json')

# The rates in oanda_book_simulator.SWAP_PER_UNIT_DAY that were MEASURED from
# real accruals on this account, not derived. --verify checks the rule against
# these; they span pipPosition 0, 2 and 5.
# NAS100 is the 2026-09-04 RE-measurement (0.44u + 0.23u over one 3-day Friday
# roll). The -35.875 it replaced was not an error — it was a real measurement of
# a rate regime the broker has since cut ~10x. See the docstring.
MEASURED = {'NAS100_USD': -3.577, 'XAG_USD': -0.042800, 'XAU_USD': -0.890,
            # first accrual 2026-08-28 (pos 4720262, 500u, -0.22 on a single-day
            # roll). The stored rate is the DERIVED -0.0004341; this is what the
            # broker actually charged, and it is the only evidence for pip 5.
            'XCU_USD': -0.000440}
# pipPosition values at which the rule reproduces a MEASURED rate. 1 is excluded
# because the only instrument sitting there (NATGAS) was itself derived by this
# rule, so it cannot validate it. 5 JOINED the set 2026-08-28: XCU stopped being
# circular evidence when it took a real accrual. 0 JOINED 2026-09-07, when the
# NAS100 re-measurement agreed with the card to 0.06% — it had been excluded on
# the strength of a stale measurement, not a bad rule. See the docstring.
RULE_OK_PIPS = {0, 2, 4, 5}


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


def _mean_close(inst: str, cache: dict) -> float | None:
    """Representative 2024+ mean close — the SAME basis the derived pct rates used.

    SWAP_PCT_NOTIONAL_DAY stores a fraction of notional, so comparing it to a card
    figure (quote currency per unit per day) needs a price, and it has to be the
    price the stored value was built against or the comparison invents an error
    that is really a basis mismatch. scripts/rollflat_screen.py._price uses this
    same window; keep them in step.
    """
    if inst in cache:
        return cache[inst]
    try:
        from data_fetcher import get_candles_date_range
        df = get_candles_date_range(inst, '2024-01-01', '2026-08-21', 'D')
        cache[inst] = float(df['close'].mean())
    except Exception:
        cache[inst] = None          # no price -> print side by side, never guess
    return cache[inst]


def verify_all() -> None:
    """Diff the live card against BOTH model tables, for every instrument in them.

    --verify only covers the handful of MEASURED rates, which is the check that
    validates the CONVERSION RULE. This is the other question: does what we store
    still match what the broker publishes, across the whole book? Those come apart
    when the broker moves a rate — which it does (NAS100, ~10x, 2026-08).

    Read-only at the broker: one ProtoOASymbolByIdReq, same as fetch().

    TWO CLASSES OF FALSE ALARM ARE TAGGED, NOT SILENCED. A raw card-vs-model diff
    flags both of these as disagreements when neither is a model error, and the
    first run of this function flagged 7 instruments of which only one survived
    reading:

      quote != USD — the card figure is in the QUOTE currency, a stored per-unit
      rate is in USD (it came off broker_swap.swap_usd). USD_CHF and EUR_GBP read
      ~19-26% off for that reason alone. The gap is the missing FX leg, not a bad
      rate.

      pct table basis — SWAP_PCT_NOTIONAL_DAY holds a FRACTION of notional, and
      swap_charge multiplies it by the BAR close. Comparing it here needs some
      price, and the honest one is the basis the stored value was derived against
      (2024+ mean close). An instrument that has rallied since therefore reads
      low: SPX500 showed 24% here while its own 2026-09-03 accrual matches the
      model to 0.4% at the current price. The tag says which number to distrust.

    Also worth knowing: a stored rate measured off a SHORT position is compared
    against swapLong by default, so the short-side error is computed too and the
    better match is reported (BTC_USD's -60.0 is swapShort exactly).
    """
    import oanda_book_simulator as S

    with open(SYMS) as fh:
        cat = json.load(fh)['instruments']
    per_unit = dict(S.SWAP_PER_UNIT_DAY)
    pct = dict(S.SWAP_PCT_NOTIONAL_DAY)
    want = sorted(set(per_unit) | set(pct))
    absent = [i for i in want if i not in cat]
    want = [i for i in want if i in cat]
    if not want:
        raise SystemExit('no instrument in either swap table is in ctrader_symbols.json')

    cards = fetch(want)
    prices: dict = {}
    derived_set = set(getattr(S, 'SWAP_DERIVED', ()))

    print('\n-- card vs model, every instrument in both swap tables --')
    print('price basis for the pct table: 2024-01-01..2026-08-21 mean daily close')
    print(f'{"instrument":<12} {"pip":>3} {"card/unit/day":>15} {"model/unit/day":>15} '
          f'{"err":>8}  {"table":<9} {"provenance":<10} flag')
    rows = []
    for inst in want:
        c = cards.get(inst)
        if not c or c.get('per_unit_day_long') is None:
            print(f'{inst:<12}  -- no card returned --')
            continue
        card = c['per_unit_day_long']
        pip = c['pip_position']
        pair, _inv = S._quote_to_usd_pair(inst)
        caveat = '' if pair is None else ' [quote != USD: needs FX leg]'
        if inst in per_unit:
            model, table, basis = per_unit[inst], 'per-unit', None
            short = c.get('per_unit_day_short')
            if short and model and abs(short - model) < abs(card - model):
                caveat += ' [matches swapShort — measured off a short?]'
        else:
            caveat += ' [pct basis: model prices at the BAR close, not this mean]'
            basis = _mean_close(inst, prices)
            model = pct[inst] * basis if basis else None
            table = 'pct'
        prov = ('measured' if inst in MEASURED else
                'DERIVED' if inst in derived_set else
                'proxy' if table == 'pct' else 'stored')
        if model is None:
            print(f'{inst:<12} {pip!s:>3} {card:>15.6g} {"— no price —":>15} '
                  f'{"n/a":>8}  {table:<9} {prov:<10} stored pct '
                  f'{pct[inst]:.6g} of notional — NOT directly comparable')
            continue
        err = abs(card - model) / abs(model) * 100 if model else float('inf')
        flag = ('  <<< CARD DISAGREES' if err > 5 and not caveat else
                '  (explained)' if err > 5 else '')
        if pip not in RULE_OK_PIPS:
            flag += '  (pip not in RULE_OK_PIPS)'
        flag += caveat
        rows.append((err, inst, bool(caveat)))
        print(f'{inst:<12} {pip!s:>3} {card:>15.6g} {model:>15.6g} '
              f'{err:>7.2f}%  {table:<9} {prov:<10}{flag}')

    if absent:
        print('\nnot in ctrader_symbols.json, no card exists: %s' % ', '.join(absent))
    bad = sorted((e, i) for e, i, cav in rows if e > 5 and not cav)
    expl = sorted(i for e, i, cav in rows if e > 5 and cav)
    print('\n%d of %d comparable instruments disagree by >5%% UNEXPLAINED%s'
          % (len(bad), len(rows), (': ' + ', '.join(i for _, i in bad)) if bad else ''))
    if expl:
        print('%d more differ but carry a tag explaining why: %s'
              % (len(expl), ', '.join(expl)))
    print('A disagreement is not automatically a model bug — the card can move under')
    print('a rate that was correctly measured. Re-measure from an accrual before')
    print('changing a MEASURED entry; see the docstring.')



def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('instruments', nargs='*')
    ap.add_argument('--verify', action='store_true',
                    help='re-derive the measured rates and print the error')
    ap.add_argument('--verify-all', action='store_true', dest='verify_all',
                    help='diff the card against BOTH model tables, whole book')
    a = ap.parse_args()

    if a.verify_all:
        verify_all()
        if not (a.verify or a.instruments):
            return

    want = list(dict.fromkeys((list(MEASURED) if a.verify else []) + a.instruments))
    if not want:
        raise SystemExit('nothing to fetch: pass instruments, --verify or --verify-all')

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
