"""The5ers' daily-loss base, and the day-roll snapshot that latches it.

The rule, as stated by the firm:

    At midnight server time the risk engine takes a snapshot of your account. It
    compares your starting balance and starting equity, selects the higher number,
    and uses it to set your loss limit for the next 24 hours.

        base  = max(balance at midnight, equity at midnight)
        floor = base x daily_drawdown_pct

    Both closed losses and open floating losses count. If equity drops below the
    threshold at any point during the day, you breach.

prop_guard anchored on EQUITY at the first sample after the roll, which agrees
with the firm only when the day opens in floating PROFIT. Open in floating LOSS
and the firm measures from the higher BALANCE, so its floor sits ABOVE ours and
the account can be disqualified while the guard still reads green. The error is
one-directional and it is the dangerous direction, which is why the -$2,000 case
below is the test that matters.
"""
import json
import os
import tempfile

import pytest

os.environ.setdefault('PROP_GUARD_VENUE', 'oanda')

import prop_guard as pg
from prop_guard import daily_base


class TestDailyBase:
    def test_floating_profit_selects_equity(self):
        assert daily_base(100_000, 100_500) == 100_500

    def test_floating_loss_selects_balance(self):
        """The case the old anchor got wrong."""
        assert daily_base(100_000, 98_000) == 100_000

    def test_flat_day_is_unambiguous(self):
        assert daily_base(100_000, 100_000) == 100_000

    def test_missing_leg_falls_back_rather_than_failing(self):
        assert daily_base(None, 98_000) == 98_000
        assert daily_base(100_000, None) == 100_000

    def test_the_floor_the_firm_would_apply(self):
        """-$2,000 floating carried across the roll on a 100k, 3% plan.

        firm:  base 100,000 -> floor 97,000
        old:   base  98,000 -> floor 95,060   ($1,940 past a permanent DQ)
        """
        balance, equity = 100_000.0, 98_000.0
        assert daily_base(balance, equity) * 0.97 == pytest.approx(97_000)
        assert equity * 0.97 == pytest.approx(95_060)


class TestDayRollSnapshot:
    """The base is a MIDNIGHT snapshot: latched once at the roll and then fixed
    for 24h, however far equity travels afterwards."""

    @pytest.fixture(autouse=True)
    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pg, "STATE_FILE", str(tmp_path / "s.json"))
        monkeypatch.setattr(pg, "_fetch_balance", lambda: None)   # never fetch in tests

    def _state_now(self):
        with open(pg.STATE_FILE) as fh:
            return json.load(fh)

    def test_first_run_snapshots_both_legs(self):
        m = pg.update(nav=98_000, balance=100_000)
        assert m['day_anchor'] == 100_000          # balance won
        st = self._state_now()
        assert st['day_base_balance'] == 100_000
        assert st['day_base_equity'] == 98_000

    def test_base_does_not_move_intraday(self, monkeypatch):
        pg.update(nav=98_000, balance=100_000)
        for nav in (99_000, 101_000, 97_500):
            m = pg.update(nav=nav, balance=nav)     # balance moves; base must not
            assert m['day_anchor'] == 100_000

    def test_base_is_re_snapshotted_on_the_next_day(self, monkeypatch):
        pg.update(nav=98_000, balance=100_000)
        monkeypatch.setattr(pg, "_trading_day", lambda now: "2999-01-01")
        m = pg.update(nav=99_000, balance=98_500)
        assert m['day_anchor'] == 99_000            # equity won this time
        assert m['day_low'] == 99_000               # and the low restarts

    def test_daily_dd_is_measured_from_the_base(self):
        """98,000 equity against a 100,000 balance base is already -2.00% used,
        not the 0.00% an equity anchor would report."""
        m = pg.update(nav=98_000, balance=100_000)
        assert m['daily_dd_now'] == pytest.approx(-0.02)
        assert m['daily_dd_worst'] == pytest.approx(-0.02)

    def test_intraday_low_is_tracked_for_the_any_point_rule(self):
        pg.update(nav=100_000, balance=100_000)
        pg.update(nav=97_400)                       # the dip
        m = pg.update(nav=99_500)                   # recovered by the next sample
        assert m['day_low'] == 97_400
        assert m['daily_dd_now'] == pytest.approx(-0.005)
        assert m['daily_dd_worst'] == pytest.approx(-0.026)


class TestEquityFromBrokerPnL:
    """equity = balance + SUM(netUnrealizedPnL), taken from the broker instead of
    reconstructed from bid/ask. `net` carries swap and commission; the old
    hand-rolled figure valued the price move only, so it read optimistic by the
    accrued swap — against a limit that counts floating loss."""

    def _client(self, balance, pnl):
        class _C:
            def start(self_): return self_
            def get_trader(self_): return {'balance': balance}
            def get_unrealized_pnl(self_): return pnl
        return _C()

    def test_equity_adds_net_pnl(self, monkeypatch):
        import sys, types
        mod = types.ModuleType('ctrader_client')
        mod.get_client = lambda: self._client(
            99_987.56, {1: {'gross': 40.0, 'net': 28.04}, 2: {'gross': -5.0, 'net': -6.0}})
        monkeypatch.setitem(sys.modules, 'ctrader_client', mod)
        bal, eq = pg._fetch_account_ctrader()
        assert bal == pytest.approx(99_987.56)
        assert eq == pytest.approx(99_987.56 + 28.04 - 6.0)

    def test_net_not_gross(self, monkeypatch):
        """Using gross would silently drop swap and commission."""
        import sys, types
        mod = types.ModuleType('ctrader_client')
        mod.get_client = lambda: self._client(100_000.0, {1: {'gross': 100.0, 'net': 60.0}})
        monkeypatch.setitem(sys.modules, 'ctrader_client', mod)
        assert pg._fetch_account_ctrader()[1] == pytest.approx(100_060.0)

    def test_no_positions_is_just_balance(self, monkeypatch):
        import sys, types
        mod = types.ModuleType('ctrader_client')
        mod.get_client = lambda: self._client(100_000.0, {})
        monkeypatch.setitem(sys.modules, 'ctrader_client', mod)
        assert pg._fetch_account_ctrader() == (100_000.0, 100_000.0)

    def test_failure_returns_no_partial_figure(self, monkeypatch):
        import sys, types
        mod = types.ModuleType('ctrader_client')
        def _boom(): raise RuntimeError('socket closed')
        mod.get_client = _boom
        monkeypatch.setitem(sys.modules, 'ctrader_client', mod)
        assert pg._fetch_account_ctrader() == (None, None)
