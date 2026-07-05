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
