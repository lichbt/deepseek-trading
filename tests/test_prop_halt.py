"""Kill-switch logic: halt_adjusted_target + flag reader + prop_guard opt-in."""
import os
import json
import pytest

import live_test as lt
from live_test import halt_adjusted_target as h


class TestHaltAdjustedTarget:
    def test_not_halted_passthrough(self):
        assert h(5, 0, False, False) == 5
        assert h(-3, 10, False, True) == -3

    def test_flatten_always_zero(self):
        assert h(5, 0, True, True) == 0.0
        assert h(0, 10, True, True) == 0.0
        assert h(-3, 10, True, True) == 0.0

    def test_halt_only_blocks_fresh_entry(self):
        assert h(5, 0, True, False) == 0.0        # flat -> no new entry

    def test_halt_only_allows_exit(self):
        assert h(0, 10, True, False) == 0.0        # exit to flat allowed

    def test_halt_only_blocks_increase(self):
        assert h(20, 10, True, False) == 10        # would increase -> hold

    def test_halt_only_blocks_flip(self):
        assert h(-5, 10, True, False) == 10        # long->short flip -> hold

    def test_halt_only_allows_reduce(self):
        assert h(4, 10, True, False) == 4          # reduce long
        assert h(-4, -10, True, False) == -4       # reduce short


class TestFlagReader:
    def test_absent_flag_not_halted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(lt, "_HALT_FLAG", str(tmp_path / "none.flag"))
        assert lt._read_trading_halt() == (False, False)

    def test_flatten_and_halt_only(self, tmp_path, monkeypatch):
        f = tmp_path / "trading_halt.flag"
        monkeypatch.setattr(lt, "_HALT_FLAG", str(f))
        f.write_text(json.dumps({"flatten": True}))
        assert lt._read_trading_halt() == (True, True)
        f.write_text(json.dumps({"flatten": False}))
        assert lt._read_trading_halt() == (True, False)

    def test_unreadable_flag_halts_conservatively(self, tmp_path, monkeypatch):
        f = tmp_path / "trading_halt.flag"
        monkeypatch.setattr(lt, "_HALT_FLAG", str(f))
        f.write_text("{ not json")
        halted, flatten = lt._read_trading_halt()
        assert halted is True and flatten is False   # present-but-broken = halt, no flatten


class TestPropGuardOptIn:
    def test_disabled_never_writes_flag(self, tmp_path, monkeypatch):
        import prop_guard as pg
        f = tmp_path / "trading_halt.flag"
        monkeypatch.setattr(pg, "HALT_FLAG_FILE", str(f))
        monkeypatch.setattr(pg, "HALT_ENABLED", False)
        pg._update_halt_flag({"daily_dd_worst": -0.99, "total_dd_now": -0.99, "nav": 1})
        assert not f.exists()

    def test_enabled_writes_then_clears(self, tmp_path, monkeypatch):
        import prop_guard as pg
        f = tmp_path / "trading_halt.flag"
        monkeypatch.setattr(pg, "HALT_FLAG_FILE", str(f))
        monkeypatch.setattr(pg, "HALT_ENABLED", True)
        monkeypatch.setattr(pg, "HALT_FLATTEN", True)
        # breach -> write
        pg._update_halt_flag({"daily_dd_worst": -0.045, "total_dd_now": 0.0, "nav": 100000})
        assert f.exists() and json.loads(f.read_text())["flatten"] is True
        # recovered -> clear
        pg._update_halt_flag({"daily_dd_worst": -0.01, "total_dd_now": 0.0, "nav": 100000})
        assert not f.exists()


# ---------------------------------------------------------------------------
# cTrader equity — the number the whole guard trips on
#
# The Open API reports balance but NOT equity, so prop_guard assembles it. Every
# one of these is a way the assembly can be wrong in the SAFE direction, which is
# the dangerous direction for a drawdown guard: understate the floating loss and
# it alerts after the DQ instead of before it.
# ---------------------------------------------------------------------------
def _usd_symbol():
    """A real (instrument, symbol_id) with a USD quote, read from the venue dump."""
    import json as _json
    import os as _os
    path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                         'ctrader_symbols.json')
    syms = _json.load(open(path))['instruments']
    inst = next(k for k in sorted(syms) if k.endswith('_USD'))
    return inst, syms[inst]['symbol_id']


def _fake_venue(monkeypatch, balance, positions, prices, q2u=1.0, price_raises=False):
    """Stand in for ctrader_client + fix_runner so no network call happens.

    Both are imported INSIDE _fetch_nav_ctrader, so patching sys.modules is what
    reaches them — and it keeps the real fix_runner (which connects to cTrader at
    import time just to read volume specs) out of the test run entirely.
    """
    import sys
    import types

    class _Client:
        def start(self):
            return self

        def get_trader(self):
            return {'balance': balance}

        def get_positions(self):
            return positions

        def get_price(self, symbol_id):
            if price_raises:
                raise RuntimeError('market closed — no ticks')
            return prices[symbol_id]

    cc = types.ModuleType('ctrader_client')
    cc.get_client = lambda: _Client()
    fr = types.ModuleType('fix_runner')
    fr.q2usd = lambda inst: q2u
    monkeypatch.setitem(sys.modules, 'ctrader_client', cc)
    monkeypatch.setitem(sys.modules, 'fix_runner', fr)


def _pos(symbol_id, side, volume, entry):
    return {'position_id': 1, 'symbol_id': symbol_id, 'side': side,
            'volume': volume, 'entry_price': entry, 'sl': None, 'tp': None}


class TestCTraderEquity:
    def test_flat_account_is_just_balance(self, monkeypatch):
        import prop_guard as pg
        _fake_venue(monkeypatch, 2250.42, [], {})
        assert pg._fetch_nav_ctrader() == pytest.approx(2250.42)

    def test_long_in_profit_adds(self, monkeypatch):
        import prop_guard as pg
        inst, sid = _usd_symbol()
        # volume 1000 centi-units = 10 units; +1.00 move = +10.00
        _fake_venue(monkeypatch, 100_000.0, [_pos(sid, 'BUY', 1000, 100.0)],
                    {sid: (101.0, 101.2)})
        assert pg._fetch_nav_ctrader() == pytest.approx(100_010.0)

    def test_long_in_loss_subtracts(self, monkeypatch):
        import prop_guard as pg
        inst, sid = _usd_symbol()
        _fake_venue(monkeypatch, 100_000.0, [_pos(sid, 'BUY', 1000, 100.0)],
                    {sid: (97.0, 97.2)})
        assert pg._fetch_nav_ctrader() == pytest.approx(99_970.0)

    def test_short_in_profit_adds(self, monkeypatch):
        import prop_guard as pg
        inst, sid = _usd_symbol()
        _fake_venue(monkeypatch, 100_000.0, [_pos(sid, 'SELL', 1000, 100.0)],
                    {sid: (98.8, 99.0)})
        assert pg._fetch_nav_ctrader() == pytest.approx(100_010.0)

    def test_values_the_exit_side_of_the_spread(self, monkeypatch):
        """A long closes at the BID and a short at the ASK. Using the mid (or the
        wrong side) flatters every open position by half a spread each — across a
        23-sleeve book that is a systematic understatement of drawdown."""
        import prop_guard as pg
        inst, sid = _usd_symbol()
        _fake_venue(monkeypatch, 0.0, [_pos(sid, 'BUY', 100, 100.0)],
                    {sid: (99.0, 101.0)})          # 1 unit, wide spread
        assert pg._fetch_nav_ctrader() == pytest.approx(-1.0)   # bid, not mid (0.0)

        _fake_venue(monkeypatch, 0.0, [_pos(sid, 'SELL', 100, 100.0)],
                    {sid: (99.0, 101.0)})
        assert pg._fetch_nav_ctrader() == pytest.approx(-1.0)   # ask, not mid

    def test_non_usd_quote_is_converted(self, monkeypatch):
        import prop_guard as pg
        inst, sid = _usd_symbol()
        _fake_venue(monkeypatch, 100_000.0, [_pos(sid, 'BUY', 1000, 100.0)],
                    {sid: (101.0, 101.2)}, q2u=0.5)
        assert pg._fetch_nav_ctrader() == pytest.approx(100_005.0)   # +10 quote = +5 USD

    def test_several_positions_sum(self, monkeypatch):
        import prop_guard as pg
        inst, sid = _usd_symbol()
        _fake_venue(monkeypatch, 100_000.0,
                    [_pos(sid, 'BUY', 1000, 100.0), _pos(sid, 'SELL', 1000, 100.0)],
                    {sid: (101.0, 101.0)})
        assert pg._fetch_nav_ctrader() == pytest.approx(100_000.0)   # +10 and -10

    def test_unmapped_symbol_returns_none_not_a_partial(self, monkeypatch):
        """The account is hand-traded too, so a position may exist for a symbol this
        repo never mapped. Its loss still counts against the limit — reporting the
        rest of the book without it would understate drawdown."""
        import prop_guard as pg
        _fake_venue(monkeypatch, 100_000.0, [_pos(987654, 'BUY', 1000, 100.0)],
                    {987654: (101.0, 101.2)})
        assert pg._fetch_nav_ctrader() is None

    def test_price_failure_returns_none_not_balance(self, monkeypatch):
        """get_price raises on a shut market. Falling back to bare balance would
        silently drop every floating loss and read as a healthy account."""
        import prop_guard as pg
        inst, sid = _usd_symbol()
        _fake_venue(monkeypatch, 100_000.0, [_pos(sid, 'BUY', 1000, 100.0)],
                    {sid: (101.0, 101.2)}, price_raises=True)
        assert pg._fetch_nav_ctrader() is None


class TestTradingDayBoundary:
    """The day anchor decides which equity the daily loss is measured FROM.

    VERIFIED 2026-08-05 against the venue's own D1 trendbars on ctid 48171893:
    bars open at 21:00 UTC = 00:00 EEST. The old DAY_RESET_UTC_HOUR=22 constant
    was the same boundary expressed in WINTER, so it ran an hour late for half of
    every year — carrying the previous session's loss into the new day.
    """

    def _utc(self, *args):
        from datetime import datetime, timezone
        return datetime(*args, tzinfo=timezone.utc)

    def test_summer_rolls_at_2100_utc(self):
        import prop_guard as pg
        assert pg._trading_day(self._utc(2026, 8, 5, 20, 59)) == '2026-08-05'
        assert pg._trading_day(self._utc(2026, 8, 5, 21, 0)) == '2026-08-06'

    def test_winter_rolls_at_2200_utc(self):
        """Same broker-local midnight, one hour later in UTC — which is exactly
        why a fixed UTC constant cannot be right all year."""
        import prop_guard as pg
        assert pg._trading_day(self._utc(2026, 1, 15, 21, 59)) == '2026-01-15'
        assert pg._trading_day(self._utc(2026, 1, 15, 22, 0)) == '2026-01-16'

    def test_a_fixed_utc_hour_would_be_wrong_in_one_season(self):
        """Pins the regression: no single constant satisfies both."""
        import prop_guard as pg
        summer_roll = 21 if pg._trading_day(self._utc(2026, 8, 5, 21, 0)) == '2026-08-06' else None
        winter_roll = 22 if pg._trading_day(self._utc(2026, 1, 15, 22, 0)) == '2026-01-16' else None
        assert summer_roll == 21 and winter_roll == 22
        assert summer_roll != winter_roll

    def test_midday_is_the_same_calendar_day(self):
        import prop_guard as pg
        assert pg._trading_day(self._utc(2026, 8, 5, 7, 48)) == '2026-08-05'

    def test_rolls_on_the_us_dst_calendar_not_the_eu_one(self):
        """The case the 2026-08-05 verification could not see.

        Measured on ctid 48171893's own D1 trendbars: bars open 21:00 UTC through
        2026-03-09..27, i.e. the broker moved to UTC+3 on the US date (Mar 8),
        three weeks before Europe (Mar 29). Europe/Athens is still UTC+2 there and
        would roll at 22:00 — an hour late, carrying the prior session's loss in.
        """
        import prop_guard as pg
        assert pg._trading_day(self._utc(2026, 3, 10, 20, 59)) == '2026-03-10'
        assert pg._trading_day(self._utc(2026, 3, 10, 21, 0)) == '2026-03-11'
        # The autumn window has the same shape: EU falls back Oct 25, the US not
        # until Nov 1, so Oct 26 is another broker-UTC+3 / Athens-UTC+2 day.
        assert pg._trading_day(self._utc(2026, 10, 26, 20, 59)) == '2026-10-26'
        assert pg._trading_day(self._utc(2026, 10, 26, 21, 0)) == '2026-10-27'

    def test_europe_athens_would_fail_the_divergence_windows(self):
        """Pins WHY the zone was changed: the old default is wrong here, so a
        revert to it cannot pass silently."""
        from zoneinfo import ZoneInfo
        athens = self._utc(2026, 3, 10, 21, 0).astimezone(ZoneInfo('Europe/Athens'))
        assert athens.strftime('%Y-%m-%d') == '2026-03-10'      # still the old day
        import prop_guard as pg
        assert pg._trading_day(self._utc(2026, 3, 10, 21, 0)) == '2026-03-11'

    def test_explicit_zone_override_still_works(self):
        """One file serves both products: FTMO resets at 00:00 CE(S)T."""
        import importlib, os
        import prop_guard as pg
        old = os.environ.get('PROP_DAY_RESET_TZ')
        os.environ['PROP_DAY_RESET_TZ'] = 'Europe/Berlin'
        try:
            pg = importlib.reload(pg)
            assert pg._trading_day(self._utc(2026, 8, 5, 21, 59)) == '2026-08-05'
            assert pg._trading_day(self._utc(2026, 8, 5, 22, 0)) == '2026-08-06'
        finally:
            if old is None:
                os.environ.pop('PROP_DAY_RESET_TZ', None)
            else:
                os.environ['PROP_DAY_RESET_TZ'] = old
            importlib.reload(pg)


class TestAnchorDurability:
    """The anchors decide WHERE the breaker fires, so losing them inverts it.

    prop_guard wrote beside the module (/app in the container), which is not the
    mounted volume and is in .dockerignore — so the file never shipped and was
    re-created empty on every pod start, re-seeding start_nav from current equity.
    On a 100k account that moves the 80%-of-10% halt from 92,000 to 87,400 after a
    restart at 95,000: below the 90,000 DQ, i.e. the breaker fires after the
    account is already dead.
    """

    def test_state_follows_the_runner_symlink_onto_the_volume(self, tmp_path):
        """On the pod /app/fix_runner_state.json is a symlink to /data."""
        volume = tmp_path / 'data'
        volume.mkdir()
        app = tmp_path / 'app'
        app.mkdir()
        (volume / 'fix_runner_state.json').write_text('{}')
        os.symlink(volume / 'fix_runner_state.json', app / 'fix_runner_state.json')

        import prop_guard as pg
        assert pg._resolve_state_dir(here=str(app)) == str(volume)

    def test_dangling_symlink_falls_back_instead_of_writing_nowhere(self, tmp_path):
        """A symlink whose target directory is gone must not become the state dir."""
        app = tmp_path / 'app'
        app.mkdir()
        os.symlink(tmp_path / 'gone' / 'fix_runner_state.json',
                   app / 'fix_runner_state.json')
        import prop_guard as pg
        assert pg._resolve_state_dir(here=str(app)) == str(app)

    def test_plain_file_keeps_state_beside_the_module(self):
        """Off the pod there is no symlink — local behaviour must not change."""
        import prop_guard as pg
        here = os.path.dirname(os.path.abspath(pg.__file__))
        assert pg._resolve_state_dir() == here
        assert pg.STATE_FILE.startswith(here)

    def test_explicit_state_dir_wins(self, monkeypatch):
        import importlib
        import prop_guard as pg
        monkeypatch.setenv('PROP_GUARD_STATE_DIR', '/tmp/anchors')
        pg = importlib.reload(pg)
        try:
            assert pg.STATE_DIR == '/tmp/anchors'
        finally:
            monkeypatch.delenv('PROP_GUARD_STATE_DIR', raising=False)
            importlib.reload(pg)


class TestStartBalanceAnchor:
    def test_configured_balance_beats_observed_nav(self, monkeypatch):
        """A restart at 95k must still measure the total limit from 100k."""
        import importlib
        import prop_guard as pg
        monkeypatch.setenv('PROP_START_BALANCE', '100000')
        pg = importlib.reload(pg)
        try:
            assert pg._sane_start_balance(95000.0) == 100000.0
        finally:
            monkeypatch.delenv('PROP_START_BALANCE', raising=False)
            importlib.reload(pg)

    def test_stale_variable_is_refused_not_obeyed(self, monkeypatch):
        """The trap: FIX_START_EQUITY still reads 2500 (the old ~$2.5k account).

        Copy-pasted in, it would put a 100k account 97% 'down' and halt on the
        first tick. A prop account cannot be 50% down and still open, so that gap
        can only be misconfiguration — fall back to NAV rather than act on it.
        """
        import importlib
        import prop_guard as pg
        monkeypatch.setenv('PROP_START_BALANCE', '2500')
        pg = importlib.reload(pg)
        try:
            assert pg._sane_start_balance(100000.0) == 100000.0
        finally:
            monkeypatch.delenv('PROP_START_BALANCE', raising=False)
            importlib.reload(pg)

    def test_unset_keeps_the_original_seed_from_nav(self):
        import prop_guard as pg
        assert pg.START_BALANCE is None
        assert pg._sane_start_balance(1234.5) == 1234.5

    def test_existing_state_is_healed_to_the_configured_anchor(self, tmp_path, monkeypatch):
        """A bad anchor persisted to the VOLUME would otherwise be permanent."""
        import importlib
        import prop_guard as pg
        monkeypatch.setenv('PROP_START_BALANCE', '100000')
        monkeypatch.setenv('PROP_GUARD_STATE_DIR', str(tmp_path))
        pg = importlib.reload(pg)
        try:
            with open(pg.STATE_FILE, 'w') as fh:      # seeded by an ephemeral restart
                json.dump({'peak_nav': 95000.0, 'start_nav': 95000.0, 'day': '1999-01-01',
                           'day_anchor_nav': 95000.0, 'day_low_nav': 95000.0,
                           'max_total_dd': 0.0, 'worst_daily_dd': 0.0}, fh)
            m = pg.update(nav=95000.0)
            assert m['start_nav'] == 100000.0
            # -5% from the contractual anchor, not 0% from the re-based one
            assert m['total_dd_now'] == pytest.approx(-0.05)
        finally:
            monkeypatch.delenv('PROP_START_BALANCE', raising=False)
            monkeypatch.delenv('PROP_GUARD_STATE_DIR', raising=False)
            importlib.reload(pg)


class TestVenueAndLimits:
    def test_default_venue_is_oanda_and_keeps_the_original_state_file(self):
        import prop_guard as pg
        assert pg.VENUE == 'oanda'
        assert pg.STATE_FILE.endswith('prop_guard_state.json')

    def test_daily_limit_is_three_percent(self):
        """The5ers 100k two-step. At the old 5% the 80% halt fired at -4.0% —
        past a DQ."""
        import prop_guard as pg
        assert pg.DAILY_DD_LIMIT == pytest.approx(0.03)
        assert pg.TOTAL_DD_LIMIT == pytest.approx(0.10)

    def test_halt_threshold_sits_below_the_wall(self):
        import prop_guard as pg
        halt_at = pg.DAILY_DD_LIMIT * pg.HALT_FRACTION
        assert halt_at == pytest.approx(0.024)
        assert halt_at < pg.DAILY_DD_LIMIT

    def test_ctrader_venue_uses_a_separate_state_file(self, monkeypatch):
        """Splicing a 2.2k account's history onto a 100k one would corrupt every
        drawdown figure in it."""
        import importlib
        import prop_guard as pg
        monkeypatch.setenv('PROP_GUARD_VENUE', 'ctrader')
        reloaded = importlib.reload(pg)
        try:
            assert reloaded.VENUE == 'ctrader'
            assert reloaded.STATE_FILE.endswith('prop_guard_state_ctrader.json')
            assert not reloaded.STATE_FILE.endswith('/prop_guard_state.json')
        finally:
            monkeypatch.delenv('PROP_GUARD_VENUE', raising=False)
            importlib.reload(pg)
