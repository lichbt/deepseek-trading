"""Tests for live_test.order_decision — the flip/startup-alignment matrix.

The 2026-06-10 incubation finding: a sleeve deployed while its strategy was
mid-position never entered (the loop only traded on signal FLIPS), so live
sat flat for 12 days while validation said short +4.3%. order_decision adds
a one-time startup alignment WITHOUT breaking the mid-run stop-out semantics
(stop fires -> stay flat until the signal changes, as validation models).
"""
import os
import sqlite3
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import live_test
from live_test import order_decision, stop_out_still_binding


class TestOrderDecision:
    def test_normal_flip_fires(self):
        assert order_decision(+1, 0, 0, False, False) == 'flip'
        assert order_decision(0, -1, -1, False, False) == 'flip'
        assert order_decision(-1, +1, +1, False, True) == 'flip'  # flip wins over align

    def test_startup_mid_position_aligns(self):
        """Deploy while strategy already short: signal -1 == prev -1, flat broker."""
        assert order_decision(-1, -1, 0, False, True) == 'align'
        assert order_decision(+1, +1, 0, False, True) == 'align'

    def test_restart_with_open_position_does_nothing(self):
        """Respawn with the broker position already matching: no churn."""
        assert order_decision(+1, +1, +1, False, True) is None
        assert order_decision(-1, -1, -1, False, True) is None

    def test_mid_run_stop_out_stays_flat(self):
        """Stop fired between bars (position 0, signal persists): the
        validated stream models flat-until-signal-change — must NOT re-enter."""
        assert order_decision(-1, -1, 0, False, False) is None
        assert order_decision(+1, +1, 0, False, False) is None

    def test_halted_never_aligns(self):
        assert order_decision(-1, -1, 0, True, True) is None

    def test_flat_signal_at_startup_never_orders(self):
        assert order_decision(0, 0, 0, False, True) is None


class TestStopOutMemory:
    """The restart half of flat-until-the-signal-changes.

    Mid-run the rule holds by accident: prev_signal is re-derived from the candle
    series each bar, so a persistent signal never re-flips. A RESTART throws that
    away, and 'align' cannot tell a stopped-out sleeve (signal -1, position 0)
    from one deployed mid-position — so it re-enters. Measured on the paper book
    2026-08-07: 5 software stops in the whole log history, 5 of them undone by
    the next restart's alignment. xcuusd_i27 stopped at 6.65189 on 2026-08-04 and
    was re-entered short 24h later at 1117.8 units against the original 4953.1.
    """

    def test_restart_after_stop_out_does_not_align_back_in(self):
        # The exact xcuusd_i27 shape: strategy still short, sleeve flat, fresh process.
        assert order_decision(-1, -1, 0, False, True, stopped_signal=-1) is None
        assert order_decision(+1, +1, 0, False, True, stopped_signal=+1) is None

    def test_a_real_signal_change_still_re_enters(self):
        """Positive control — the memory must not swallow a genuine flip."""
        assert order_decision(+1, -1, 0, False, True, stopped_signal=-1) == 'flip'
        assert order_decision(+1, -1, 0, False, False, stopped_signal=-1) == 'flip'

    def test_memory_for_a_different_signal_does_not_block_alignment(self):
        """Stopped out of a LONG, strategy now short: alignment is legitimate."""
        assert order_decision(-1, -1, 0, False, True, stopped_signal=+1) == 'align'

    def test_no_memory_behaves_exactly_as_before(self):
        assert order_decision(-1, -1, 0, False, True, stopped_signal=None) == 'align'


class TestStopOutStillBinding:
    """A stop-out recorded before a DOWNTIME may already be spent: if the signal
    changed and changed back while the process was dead, the re-entry is real."""

    BARS = list(pd.to_datetime(["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"]))
    STOP = pd.Timestamp("2026-08-04")

    def test_unchanged_signal_since_the_stop_is_binding(self):
        assert stop_out_still_binding([-1, -1, -1, -1], self.BARS, -1, self.STOP) is True

    def test_signal_that_moved_after_the_stop_releases_it(self):
        # -1 -> 0 -> -1 across the downtime: bar 3 is an entry validation takes.
        assert stop_out_still_binding([-1, -1, 0, -1], self.BARS, -1, self.STOP) is False

    def test_only_bars_after_the_stop_count(self):
        # The signal differed BEFORE the stop; that says nothing about after it.
        assert stop_out_still_binding([0, -1, -1, -1], self.BARS, -1, self.STOP) is True

    def test_stop_bar_predating_the_window_is_unverifiable_and_released(self):
        assert stop_out_still_binding(
            [-1, -1, -1, -1], self.BARS, -1, pd.Timestamp("2026-07-01")) is False

    def test_no_memory_is_never_binding(self):
        assert stop_out_still_binding([-1], self.BARS[:1], None, None) is False

    def test_missing_bar_time_stays_binding(self):
        """A pre-migration row with a signal but no bar: keep it flat rather than
        silently returning to the behaviour this fixes."""
        assert stop_out_still_binding([-1, -1, -1, -1], self.BARS, -1, None) is True


class TestStopOutPersistence:
    """sleeve_units predates these columns and is created with CREATE TABLE IF
    NOT EXISTS, so the migration is the part that can silently not happen."""

    def _db(self, tmp_path, monkeypatch, legacy=False):
        db = str(tmp_path / "pipeline.db")
        if legacy:      # the shape every deployed book already has on disk
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE sleeve_units(sleeve_id TEXT PRIMARY KEY, units REAL, stop REAL)")
            con.execute("INSERT INTO sleeve_units VALUES ('s', -4953.1, 6.65189)")
            con.commit(); con.close()
        monkeypatch.setattr(live_test, "_NETTING_DB", db)
        return db

    def test_round_trip(self, tmp_path, monkeypatch):
        self._db(tmp_path, monkeypatch)
        live_test._save_own_units("s", -4953.1, 6.65189)
        live_test._save_stop_out("s", -1, pd.Timestamp("2026-08-04 21:00:00+00:00"))
        assert live_test._load_stop_out("s") == (-1, "2026-08-04 21:00:00+00:00")

    def test_migrates_a_legacy_table_without_losing_units(self, tmp_path, monkeypatch):
        self._db(tmp_path, monkeypatch, legacy=True)
        assert live_test._load_stop_out("s") == (None, None)
        live_test._save_stop_out("s", -1, "2026-08-04 21:00:00+00:00")
        assert live_test._load_stop_out("s") == (-1, "2026-08-04 21:00:00+00:00")
        assert live_test._load_own_units("s") == (-4953.1, 6.65189)

    def test_saving_units_does_not_erase_the_memory(self, tmp_path, monkeypatch):
        """_save_own_units ran INSERT OR REPLACE: it would blank stopped_signal on
        the very order that closes the stopped-out position."""
        self._db(tmp_path, monkeypatch)
        live_test._save_stop_out("s", -1, "2026-08-04 21:00:00+00:00")
        live_test._save_own_units("s", 0.0, None)
        assert live_test._load_stop_out("s") == (-1, "2026-08-04 21:00:00+00:00")

    def test_xcuusd_i27_reproduction_across_a_restart(self, tmp_path, monkeypatch):
        """The whole bug, end to end, at the restart boundary that caused it.

        2026-08-04 21:00  software stop @ 6.65189 -> flat (Δ+4953 closed)
        2026-08-05 21:00  RESTART -> 'Startup alignment: signal -1 vs position +0'
                          -> re-entered short at 1117.8 units.
        """
        self._db(tmp_path, monkeypatch)
        # --- process A: the stop fires on a -1 signal, then the sleeve closes.
        live_test._save_stop_out("xcuusd_i27", -1, "2026-08-04 21:00:00+00:00")
        live_test._save_own_units("xcuusd_i27", 0.0, None)

        # --- process B: fresh start next day. prev_signal is re-derived to -1.
        stopped_signal, stopped_bar = live_test._load_stop_out("xcuusd_i27")
        assert stopped_signal == -1
        bars = list(pd.to_datetime(["2026-08-04 21:00:00+00:00", "2026-08-05 21:00:00+00:00"]))
        assert stop_out_still_binding([-1, -1], bars, stopped_signal,
                                      pd.to_datetime(stopped_bar)) is True
        assert order_decision(-1, -1, 0, False, True, stopped_signal) is None   # was 'align'

        # --- and once copper's signal actually turns, the sleeve trades again.
        assert order_decision(+1, -1, 0, False, True, stopped_signal) == 'flip'

    def test_clearing(self, tmp_path, monkeypatch):
        self._db(tmp_path, monkeypatch)
        live_test._save_stop_out("s", -1, "2026-08-04 21:00:00+00:00")
        live_test._save_stop_out("s", None, None)
        assert live_test._load_stop_out("s") == (None, None)
