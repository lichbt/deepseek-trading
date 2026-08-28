"""Integrity of the two swap tables in oanda_book_simulator.

An instrument that is absent from BOTH tables is charged 0.0 by swap_charge() and
nothing says so. That hole has now been found four times — NATGAS (to 2026-08-14),
AU200_AUD and HK33_HKD (to 2026-08-18), and USD_JPY (to 2026-08-22) — so it gets a
test rather than another comment.

The 2026-08-22 pass also found the other half of the failure mode: a rate that is
PRESENT but unsourced. EUR_JPY, GBP_JPY and GBP_USD all sat at exactly -0.000120,
the same round number as the WHEAT_USD entry that is explicitly labelled
"placeholder, no source". Three unrelated instruments cannot share a real measured
rate, and GBP_USD was in the wrong table on top of it.
"""
import pytest

import oanda_book_simulator as S

BOTH = dict(S.SWAP_PER_UNIT_DAY, **S.SWAP_PCT_NOTIONAL_DAY)

# The value that marked an unsourced guess. WHEAT_USD is allowed to keep it: it has
# no source, says so, and is unroutable on this broker, so it is inert either way.
PLACEHOLDER = -0.000120
PLACEHOLDER_ALLOWED = {'WHEAT_USD'}


def test_no_instrument_sits_in_both_tables():
    # swap_charge() checks SWAP_PER_UNIT_DAY first and returns early, so a duplicate
    # would make the SWAP_PCT_NOTIONAL_DAY entry silently unreachable
    dupes = set(S.SWAP_PER_UNIT_DAY) & set(S.SWAP_PCT_NOTIONAL_DAY)
    assert not dupes, f'in both tables, pct entry is dead: {sorted(dupes)}'


def test_every_derived_rate_is_actually_in_a_table():
    missing = sorted(S.SWAP_DERIVED - set(BOTH))
    assert not missing, f'in SWAP_DERIVED but charged nothing: {missing}'


def test_no_rate_is_positive_or_zero():
    # the broker's card charges both sides on every symbol checked 2026-08-22;
    # a positive rate would mean the book EARNS carry, which would be a sign error
    bad = {k: v for k, v in BOTH.items() if v >= 0}
    assert not bad, f'non-negative swap rate: {bad}'


def test_the_unsourced_placeholder_did_not_come_back():
    stuck = sorted(k for k, v in BOTH.items()
                   if v == PLACEHOLDER and k not in PLACEHOLDER_ALLOWED)
    assert not stuck, (
        f'{stuck} carry the -0.000120 placeholder again. Derive from the broker '
        f'card with scripts/swap_card.py instead of copying a round number.')


@pytest.mark.parametrize('inst', ['USD_JPY', 'EUR_JPY', 'GBP_JPY', 'GBP_USD',
                                  'NATGAS_USD', 'AU200_AUD', 'HK33_HKD'])
def test_the_instruments_that_were_once_carry_free_are_charged(inst):
    assert inst in BOTH, f'{inst} is back to being carry-free by omission'
    assert S.swap_charge(inst, 1000, 100.0, 1.0, 1, False) != 0.0


def test_gbp_usd_is_per_unit_because_it_is_usd_quoted():
    # it lived in the pct table until 2026-08-22, where the charge got multiplied by
    # price — at a ~1.27 cable that billed 1.8x the card
    assert 'GBP_USD' in S.SWAP_PER_UNIT_DAY
    assert 'GBP_USD' not in S.SWAP_PCT_NOTIONAL_DAY


def test_jpy_crosses_are_pct_because_they_need_an_fx_leg():
    for inst in ('USD_JPY', 'EUR_JPY', 'GBP_JPY'):
        assert inst in S.SWAP_PCT_NOTIONAL_DAY, inst
        assert inst not in S.SWAP_PER_UNIT_DAY, inst


def test_a_missing_instrument_still_returns_zero_silently():
    # documents the hole this file guards rather than pretending it is fixed:
    # swap_charge cannot raise on an unknown key without breaking every caller
    assert S.swap_charge('NOT_AN_INSTRUMENT', 1000, 100.0, 1.0, 1, False) == 0.0


def test_xcu_is_measured_not_derived():
    """XCU left SWAP_DERIVED on 2026-08-28 and must not drift back in.

    It sat in that set from the day it was written because pipPosition 5 had no
    measured rate to check against — XCU was the only symbol there and was itself
    an output of the conversion rule, so the evidence was circular. The circle
    broke when the account took its first XCU accrual: broker_swap position
    4720262 held 500 units and was charged -0.22 USD on the 2026-08-27 (Thu)
    single-day roll, i.e. -0.00044/unit/day. WTICO_USD took its own independently
    measured -0.70 on the SAME roll, which is what rules out a Friday triple
    inflating that figure 3x.
    """
    assert 'XCU_USD' not in S.SWAP_DERIVED
    assert 'XCU_USD' in S.SWAP_PER_UNIT_DAY  # USD-quoted, needs no FX leg

    observed = -0.22 / 500.0
    stored = S.SWAP_PER_UNIT_DAY['XCU_USD']
    err = abs(stored - observed) / abs(observed)
    assert err < 0.05, (
        f'XCU stored {stored} is {err:.1%} from the 2026-08-28 accrual '
        f'{observed:.7f}/unit/day — re-measure before changing the rate')


def test_wtico_still_matches_the_accrual_that_validated_the_xcu_roll():
    # the XCU measurement above is only single-day because this one is: pos
    # 4720224 held 1 unit and took exactly -0.70 on the same 2026-08-27 roll
    assert S.SWAP_PER_UNIT_DAY['WTICO_USD'] == pytest.approx(-0.70, rel=1e-9)
    assert 'WTICO_USD' not in S.SWAP_DERIVED
