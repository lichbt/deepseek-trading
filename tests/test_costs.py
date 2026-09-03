"""Tests for trading cost model in pipeline_utils.py."""

import pytest
import pandas as pd
import numpy as np

import pipeline_utils as pu


class TestCostConfig:
    def test_spread_known_instruments(self):
        assert pu.get_spread_pips('EUR_USD') == 1.2
        assert pu.get_spread_pips('XAU_USD') == 30.0
        assert pu.get_spread_pips('USD_JPY') == 0.12

    def test_spread_unknown_instrument(self):
        assert pu.get_spread_pips('EXOTIC_XYZ') == 2.0

    def test_pip_value_forex(self):
        assert pu.get_pip_value('EUR_USD') == 0.0001

    def test_pip_value_jpy(self):
        assert pu.get_pip_value('USD_JPY') == 0.01

    def test_commission_forex(self):
        assert pu.get_commission('EUR_USD') == 0.0

    def test_commission_commodity(self):
        assert pu.get_commission('XAU_USD') == 0.30
        # CORN/NATGAS are deliberately 0.0, NOT 0.10 (pipeline_utils, 2026-07-06):
        # a flat $0.10/unit is trivial on $2000 gold but ~2.4% per trade on a ~$4
        # grain, which made every grain strategy lose money after costs (424/468
        # corn hit the IS=0 wall). OANDA prices these CFDs in the spread instead.
        # Do not "restore" 0.10 here — that reintroduces the bug.
        assert pu.get_commission('CORN_USD') == 0.0
        assert pu.get_commission('NATGAS_USD') == 0.0

    def test_daily_swap(self):
        # Card-derived 2026-09-03, NOT the pre-2026-09-03 guesses. EUR_USD moved
        # -0.00003 -> -8.8368e-05 and BTC_USD stopped being carry-free.
        assert pu.get_daily_swap('EUR_USD') == pytest.approx(-0.000088368)
        assert pu.get_daily_swap('BTC_USD') == pytest.approx(-0.00074264)

    def test_every_pooled_instrument_is_rated_or_declared(self):
        """No instrument may be carry-free by silent omission.

        This is the check that was missing when the 2026-08-22 swap-card work
        fixed the simulator's table and left the validator's alone, so GBP_JPY
        was scored carry-free while the simulator charged it 5.8%/yr.
        """
        from auto_research import AutoResearcher
        pool = set(AutoResearcher.DEFAULT_INSTRUMENT_POOL)
        covered = set(pu.DAILY_SWAP_RATE) | set(pu.SWAP_UNSOURCED)
        assert not (pool - covered), (
            f"carry-free by omission: {sorted(pool - covered)} — derive a rate "
            f"with scripts/swap_card.py or add to SWAP_UNSOURCED"
        )

    def test_unknown_instrument_warns_instead_of_silently_zeroing(self):
        with pytest.warns(RuntimeWarning, match='CARRY-FREE'):
            pu._SWAP_WARNED.discard('ZZZ_ZZZ')
            assert pu.get_daily_swap('ZZZ_ZZZ') == 0.0

    def test_declared_unsourced_is_silent(self):
        import warnings as _w
        with _w.catch_warnings(record=True) as rec:
            _w.simplefilter('always')
            assert pu.get_daily_swap('WTICO_USD') == 0.0
        assert not rec, 'declared-unsourced must not warn'

    def test_agrees_with_the_simulator_where_both_have_a_rate(self):
        """The two tables drifting apart IS the bug this fix closes."""
        import oanda_book_simulator as sim
        # WTICO is a deliberate disagreement: the simulator charges the card's
        # -0.7/unit, which this table rejects as a contract-size artifact.
        deliberate = {'WTICO_USD', 'WHEAT_USD'}
        shared = (set(pu.DAILY_SWAP_RATE) & set(sim.SWAP_PCT_NOTIONAL_DAY)) - deliberate
        assert shared, 'expected overlap between the two swap tables'
        for inst in sorted(shared):
            assert pu.DAILY_SWAP_RATE[inst] == pytest.approx(
                sim.SWAP_PCT_NOTIONAL_DAY[inst], rel=0.02
            ), f"{inst} disagrees between validator and simulator"


class TestApplyTradingCosts:
    def _make_data_and_signals(self):
        """5 bars: flat → long → hold → exit → flat."""
        data = pd.DataFrame({
            'close': [1.0000, 1.0010, 1.0020, 1.0010, 1.0000]
        })
        signals = pd.Series([0, 1, 1, 0, 0])  # enter bar 1, exit bar 3
        return data, signals

    def test_no_trades_flat(self):
        """No trades → no spread or commission costs."""
        data = pd.DataFrame({'close': [1.0, 1.1, 1.2, 1.1, 1.0]})
        signals = pd.Series([0, 0, 0, 0, 0])
        raw = pu.compute_strategy_returns(data, signals)
        net = pu.apply_trading_costs(raw, signals, 'EUR_USD')
        assert np.allclose(raw, net)

    def test_entry_cost(self):
        """Entry deducts half spread plus swap on first return bar.

        Spread comes from get_spread_pct, not the pip model — this account trades
        cTrader and the two venues differ by 3x on EUR_USD. Sourcing it the same
        way the code does keeps this test honest when the card is re-sampled.
        """
        data, signals = self._make_data_and_signals()
        raw = pu.compute_strategy_returns(data, signals)
        net = pu.apply_trading_costs(raw, signals, 'EUR_USD')
        half_spread = pu.get_spread_pct('EUR_USD', price=1.0) * 0.5
        swap = pu.get_daily_swap('EUR_USD')
        # Entry bar: half spread + swap (position is held for first bar → overnight)
        expected = raw.iloc[0] - half_spread + swap
        assert net.iloc[0] == pytest.approx(expected)

    def test_hold_cost(self):
        """Holding incurs costs (swap, optionally spread on transition)."""
        data = pd.DataFrame({'close': [1.0] * 5})
        signals = pd.Series([1, 1, 1, 1, 0])
        raw = pu.compute_strategy_returns(data, signals)
        net = pu.apply_trading_costs(raw, signals, 'EUR_USD')
        swap = pu.get_daily_swap('EUR_USD')
        # Holding should always incur cost (swap is negative)
        assert (net <= raw).all()
        # Each held bar should have swap applied
        for i in range(len(net)):
            assert net.iloc[i] < raw.iloc[i]  # swap is cost

    def test_commission_commodity(self):
        """Commission on entry, and XAU pays ROLL-FLAT carry, not swap.

        XAU_USD is in ROLL_FLAT_SCOPE — the pod closes it before the rollover, so
        no swap is charged and a round trip is paid instead. Asserting swap here
        would price a cost the pod does not pay.
        """
        data, signals = self._make_data_and_signals()
        raw = pu.compute_strategy_returns(data, signals)
        net = pu.apply_trading_costs(raw, signals, 'XAU_USD')
        assert 'XAU_USD' in pu.ROLL_FLAT_SCOPE
        comm = pu.get_commission('XAU_USD')
        full_spread = pu.get_spread_pct('XAU_USD', price=1.0)
        # Entry: half spread + commission, then the held bar pays a round trip
        expected = raw.iloc[0] - full_spread * 0.5 - comm - (full_spread + comm)
        assert net.iloc[0] == pytest.approx(expected)

    def test_roll_flat_instrument_is_not_charged_swap(self):
        """The defect the 2026-09-03 re-gate hit: three NAS100 sleeves scored
        IS = 0 because full swap was charged to instruments the pod roll-flats,
        whose headroom is 17.24x (roll-flat removes ~94% of the carry)."""
        data = pd.DataFrame({'close': [1.0] * 6})
        signals = pd.Series([1] * 6)
        raw = pu.compute_strategy_returns(data, signals)
        assert 'NAS100_USD' in pu.ROLL_FLAT_SCOPE
        net = pu.apply_trading_costs(raw, signals, 'NAS100_USD')
        swap = abs(pu.get_daily_swap('NAS100_USD'))
        rt = pu.get_spread_pct('NAS100_USD', price=1.0) + pu.get_commission('NAS100_USD')
        per_bar = float((raw - net).iloc[-1])
        assert per_bar == pytest.approx(rt), 'held bar should pay a round trip'
        assert per_bar < swap, 'roll-flat must be cheaper than the swap it replaces'

    def test_non_scope_instrument_still_pays_swap(self):
        """USD_CHF is not in the live scope, so it pays carry the normal way."""
        data = pd.DataFrame({'close': [1.0] * 6})
        signals = pd.Series([1] * 6)
        raw = pu.compute_strategy_returns(data, signals)
        assert 'USD_CHF' not in pu.ROLL_FLAT_SCOPE
        net = pu.apply_trading_costs(raw, signals, 'USD_CHF')
        assert float((raw - net).iloc[-1]) == pytest.approx(abs(pu.get_daily_swap('USD_CHF')))

    def test_reversal(self):
        """Reversal charges full spread plus swap on first return bar."""
        data = pd.DataFrame({'close': [1.0, 1.01, 1.0, 1.01, 1.0]})
        signals = pd.Series([1, -1, 0, 0, 0])
        raw = pu.compute_strategy_returns(data, signals)
        net = pu.apply_trading_costs(raw, signals, 'EUR_USD')
        full_spread = pu.get_spread_pct('EUR_USD', price=1.0)
        # The bar is held SHORT, so it pays the short-side rate. The card prices
        # the two sides separately and they are not equal (EUR_USD short is
        # 0.9899x long; AU200 short is 0.1227x).
        swap = pu.get_daily_swap('EUR_USD', -1)
        assert swap != pu.get_daily_swap('EUR_USD')
        expected = raw.iloc[0] - full_spread + swap
        assert net.iloc[0] == pytest.approx(expected)

    def test_short_side_uses_the_cards_short_rate(self):
        """AU200 charges 8.2x more to hold long than short. Applying the long
        rate to both over-charges a short-biased sleeve ~2x — the reverse of every
        other cost error found on 2026-09-03."""
        data = pd.DataFrame({'close': [1.0] * 6})
        raw = pu.compute_strategy_returns(data, pd.Series([1] * 6))
        lo = pu.apply_trading_costs(raw, pd.Series([1] * 6), 'AU200_AUD')
        sh = pu.apply_trading_costs(raw, pd.Series([-1] * 6), 'AU200_AUD')
        assert (sh > lo).all(), 'short must cost less than long on AU200'
        ratio = float((raw - sh).iloc[-1]) / float((raw - lo).iloc[-1])
        assert ratio == pytest.approx(pu.SWAP_SHORT_RATIO['AU200_AUD'], rel=1e-6)

    def test_weekend_bar_is_charged_three_days_of_carry(self):
        """The broker bills weekdays only with a 3x Friday roll, so charge-days
        equal the CALENDAR GAP. A flat 1.0 per bar bills ~260 days against a real
        ~365 — carry ~29% light."""
        # Thu-stamped bar is Friday's session; the next stamp is Sunday = Monday's.
        dates = pd.to_datetime(['2026-08-19', '2026-08-20', '2026-08-23'])
        data = pd.DataFrame({'date': dates, 'close': [1.0, 1.0, 1.0]})
        signals = pd.Series([1, 1, 1])
        raw = pu.compute_strategy_returns(data, signals)
        net = pu.apply_trading_costs(raw, signals, 'USD_CHF', 'D', data=data)
        charged = (raw - net).values
        assert charged[1] == pytest.approx(3 * charged[0], rel=1e-6), \
            'the Fri->Mon bar must carry three days'


class TestSwapPerBarScaling:
    """Swap was incorrectly applied at the full daily rate on every bar of a held
    position, inflating intraday costs by 6× (H4) and 24× (H1)."""

    def _flat_data(self, n=10):
        return pd.DataFrame({'close': [1.0] * n})

    def _all_long(self, n=10):
        return pd.Series([1] * n)

    def test_h1_swap_is_24x_smaller_than_daily(self):
        data = self._flat_data(50)
        sigs = self._all_long(50)
        raw = pu.compute_strategy_returns(data, sigs)
        net_d  = pu.apply_trading_costs(raw, sigs, 'EUR_USD', granularity='D')
        net_h1 = pu.apply_trading_costs(raw, sigs, 'EUR_USD', granularity='H1')
        swap_d  = pu.get_daily_swap('EUR_USD')
        # On held bars, net = raw + swap_d (D) vs raw + swap_d/24 (H1)
        # The H1 deduction per bar should be 24× smaller
        per_bar_d  = (net_d.iloc[5] - raw.iloc[5])
        per_bar_h1 = (net_h1.iloc[5] - raw.iloc[5])
        assert per_bar_h1 == pytest.approx(per_bar_d / 24.0)

    def test_h4_swap_is_6x_smaller_than_daily(self):
        data = self._flat_data(50)
        sigs = self._all_long(50)
        raw = pu.compute_strategy_returns(data, sigs)
        net_d  = pu.apply_trading_costs(raw, sigs, 'EUR_USD', granularity='D')
        net_h4 = pu.apply_trading_costs(raw, sigs, 'EUR_USD', granularity='H4')
        per_bar_d  = (net_d.iloc[5] - raw.iloc[5])
        per_bar_h4 = (net_h4.iloc[5] - raw.iloc[5])
        assert per_bar_h4 == pytest.approx(per_bar_d / 6.0)

    def test_unknown_granularity_defaults_to_daily(self):
        """Unrecognised granularity falls back to 1×, matching D behaviour."""
        data = self._flat_data(20)
        sigs = self._all_long(20)
        raw = pu.compute_strategy_returns(data, sigs)
        net_d   = pu.apply_trading_costs(raw, sigs, 'EUR_USD', granularity='D')
        net_xxx = pu.apply_trading_costs(raw, sigs, 'EUR_USD', granularity='UNKNOWN')
        assert (net_d == net_xxx).all()

    def test_bars_per_day_table(self):
        assert pu._bars_per_day('D')   == 1.0
        assert pu._bars_per_day('H4')  == 6.0
        assert pu._bars_per_day('H1')  == 24.0
        assert pu._bars_per_day('M30') == 48.0
        assert pu._bars_per_day('W')   == 0.2


class TestComputeNetStrategyReturns:
    def test_combined_pipeline(self):
        """compute_net_strategy_returns = raw + costs (full pipeline)."""
        data = pd.DataFrame({'close': [1.0000, 1.0010, 1.0020, 1.0010, 1.0000]})
        signals = pd.Series([0, 1, 1, 0, 0])

        net = pu.compute_net_strategy_returns(data, signals, 'EUR_USD')
        raw = pu.compute_strategy_returns(data, signals)

        # Costs should reduce returns
        assert (net <= raw).all()
        assert len(net) == len(raw)

    def test_empty_data(self):
        data = pd.DataFrame({'close': []})
        signals = pd.Series([], dtype=int)
        net = pu.compute_net_strategy_returns(data, signals, 'EUR_USD')
        assert len(net) == 0


class TestGtScore:
    def _returns(self, n, mean=0.001, std=0.01, seed=42):
        np.random.seed(seed)
        return pd.Series(np.random.normal(mean, std, n))

    def test_fewer_than_20_active_returns_zero(self):
        """< 20 non-zero returns must produce GT=0 regardless of their values."""
        # 4 active bars (like the GBP/USD skew strategy bug)
        r = pd.Series([0.013, 0.0, 0.003, -0.00009, -0.00009, 0.0] * 3)
        assert pu.compute_gt_score(r) == 0.0

    def test_exactly_20_active_bars_allowed(self):
        """Exactly 20 non-zero returns with positive edge should produce a non-zero score."""
        # Alternating +0.5% / -0.1% gives positive mean with variance — avoids zero-std trap
        r = pd.Series([0.005 if i % 2 == 0 else -0.001 for i in range(20)])
        assert pu.compute_gt_score(r) > 0.0

    def test_sortino_cap_prevents_blowup(self):
        """Near-identical tiny losses must not blow up sortino even with many trades."""
        np.random.seed(7)
        # 40 normal winning bars + 2 nearly-identical tiny losses (std≈0 → old bug)
        wins = np.random.normal(0.001, 0.01, 40)
        losses = np.array([-0.00009, -0.000090001])  # nearly identical → std ≈ 0
        r = pd.Series(np.concatenate([wins, losses]))
        score = pu.compute_gt_score(r)
        # Sortino should fall back to Sharpe (not blow up); realistic sharpe ≈ 1-3
        assert score < 15.0, f"GT-score {score:.2f} blew up (sortino not capped)"

    def test_no_losses_uses_sharpe_for_sortino(self):
        """Zero negative returns → sortino falls back to Sharpe; result is finite."""
        # Small positive returns with variance — no negative bars
        r = pd.Series([0.001 + (i % 5) * 0.0005 for i in range(50)])  # 0.001 – 0.003, all positive
        score = pu.compute_gt_score(r)
        assert 0.0 < score
        assert np.isfinite(score)

    def test_single_loss_uses_its_magnitude(self):
        """One negative return: downside_dev = |loss| * sqrt(252), no std needed."""
        r = pd.Series([0.002] * 30 + [-0.01])
        score = pu.compute_gt_score(r)
        assert 0.0 < score < 20.0

    def test_normal_strategy_score_in_expected_range(self):
        """A genuinely good strategy (mean 0.1%, std 1% daily) stays in 0.5-4 range."""
        r = self._returns(500, mean=0.001, std=0.01)
        score = pu.compute_gt_score(r)
        assert 0.0 < score < 8.0, f"Expected 0-8, got {score:.3f}"

    def test_negative_edge_returns_zero(self):
        """Strategy with negative expected return gets GT=0 (max(0, ...) floor)."""
        r = self._returns(500, mean=-0.002, std=0.01)
        assert pu.compute_gt_score(r) == 0.0

    def test_empty_series_returns_zero(self):
        assert pu.compute_gt_score(pd.Series([], dtype=float)) == 0.0

class TestCTraderCommissionCard:
    """The prop book's commission, asserted against the broker's own fills.

    Six real positions from the The5ers cTrader account (opened 2026-08-06/07),
    with the opening commission the broker itemised. The card itself was read from
    the broker on 2026-08-11 via ProtoOASymbolByIdReq — these tests are what keeps
    it honest, because the OANDA card it replaced was wrong on every line but the
    indices and nothing caught it.
    """

    def _c(self, inst, units, price, **kw):
        import oanda_book_simulator as S
        return S._commission(inst, units, price, 1.0, "ctrader", 1, **kw)

    def test_fx_is_two_dollars_per_lot_per_side(self):
        # USD_CHF 0.05 lots = 5,000 base units -> 0.10
        assert abs(self._c('USD_CHF', 5000, 0.80808) - 0.10) < 0.01
        # EUR_GBP 0.02 lots = 2,000 -> 0.04
        assert abs(self._c('EUR_GBP', 2000, 0.86) - 0.04) < 0.01
        # AUD_USD 0.14 lots = 14,000 -> 0.28
        assert abs(self._c('AUD_USD', 14000, 0.65) - 0.28) < 0.01

    def test_fx_is_price_independent(self):
        """type 2 is USD_PER_LOT — the quote must not enter it."""
        assert self._c('EUR_USD', 100000, 1.16) == self._c('EUR_USD', 100000, 0.5)

    def test_metals_are_one_dollar_per_100k_notional(self):
        # XAG_USD 0.01 lots = 50 oz @ ~62.5 -> $3,125 notional -> 0.03
        assert abs(self._c('XAG_USD', 50, 62.5) - 0.03) < 0.01

    def test_crypto_is_thirty_dollars_per_100k_notional(self):
        # BTC_USD 0.01 lots = 0.01 BTC @ ~64,700 -> $647 notional -> 0.19
        assert abs(self._c('BTC_USD', 0.01, 64700) - 0.19) < 0.01

    def test_indices_are_commission_free(self):
        # NAS100 0.08 lots = 8 units @ 29,730 -> the broker showed 0.00
        assert self._c('NAS100_USD', 8, 29730) == 0.0

    def test_energy_sits_in_the_crypto_tier_not_the_fx_one(self):
        """WTI is raw 3000 like BTC, not 200 like FX — the OANDA card had it ~5x
        too expensive and in the wrong shape entirely."""
        # 100 bbl @ $65 = $6,500 notional * 3e-4 = $1.95 per side
        assert abs(self._c('WTICO_USD', 100, 65.0) - 1.95) < 0.001

    def test_sides_multiplies(self):
        one = self._c('XAU_USD', 100, 3400)
        import oanda_book_simulator as S
        two = S._commission('XAU_USD', 100, 3400, 1.0, "ctrader", 2)
        assert abs(two - 2 * one) < 1e-12
        # 100 oz @ $3,400 = $340,000 notional -> $1 per 100k = $3.40 per side
        assert abs(one - 3.40) < 1e-6

    def test_quote_conversion_applies_to_notional_rates(self):
        import oanda_book_simulator as S
        base = S._commission('XAG_USD', 50, 62.5, 1.0, "ctrader", 1)
        conv = S._commission('XAG_USD', 50, 62.5, 2.0, "ctrader", 1)
        assert abs(conv - 2 * base) < 1e-12

    def test_instrument_absent_from_card_is_free_not_guessed(self):
        """WHEAT_USD is a live sleeve but is not on cTrader at all. It must return
        0.0 so the reporting layer can NAME it, never a fabricated rate."""
        assert self._c('WHEAT_USD', 1000, 5.5) == 0.0

    def test_oanda_venue_is_untouched(self):
        """Every OANDA/paper figure ever printed must still reproduce."""
        import oanda_book_simulator as S
        assert S._commission('XAU_USD', 100) == 100 * 0.30
        assert S._commission('EUR_USD', 100000) == 0.0
        # price/quote/sides are ignored on the OANDA path
        assert S._commission('XAU_USD', 100, 3400, 1.0, "oanda", 2) == 100 * 0.30

    def test_the_card_is_cheaper_than_the_oanda_card_on_gold(self):
        """The specific error this replaced: OANDA charged $0.30/oz round trip,
        the broker charges ~$0.068 — 4.4x. Directional assertion so a future
        edit that reverts to the OANDA magnitude fails loudly."""
        import oanda_book_simulator as S
        real_rt = 2 * S._commission('XAU_USD', 1, 3400, 1.0, "ctrader", 1)
        assert real_rt < 0.5 * S._commission('XAU_USD', 1, venue="oanda")
