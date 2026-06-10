"""Tests for portfolio.py weight-sizing universe selection."""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import portfolio


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE strategies (
            id TEXT PRIMARY KEY, status TEXT, timeframe TEXT, code TEXT
        )""")
    conn.execute("""
        CREATE TABLE validation_results (
            strategy_id TEXT, best_params TEXT, walk_forward_gt_score REAL,
            is_gt_score REAL, torture_flags TEXT
        )""")
    rows = [
        # id,                 status,                wf
        ("live_a",            "paper_trading",       0.80),
        ("live_b",            "paper_trading",       0.50),
        ("parked_passed",     "passed",              0.95),  # high WF but NOT trading
        ("parked_fragile",    "passed_but_fragile",  0.90),  # not deployed
        ("failed",            "research_failed",     0.99),
        ("retired",           "retired",             0.85),
        ("low_wf_live",       "paper_trading",       0.05),  # trading but below min_wf
    ]
    for sid, status, wf in rows:
        conn.execute("INSERT INTO strategies VALUES (?,?,?,?)",
                     (sid, status, "H4", "def generate_signals(df,p): pass"))
        conn.execute("INSERT INTO validation_results VALUES (?,?,?,?,?)",
                     (sid, "{}", wf, wf, "[]"))
    conn.commit()
    conn.close()


@pytest.fixture
def patched_db(tmp_path, monkeypatch):
    db = str(tmp_path / "pipeline.db")
    _make_db(db)
    monkeypatch.setattr(portfolio, "DB_PATH", db)
    return db


def test_only_paper_trading_strategies_loaded(patched_db):
    ids = {r["id"] for r in portfolio.load_strategies(min_wf=0.0)}
    # Trading strategies are included
    assert "live_a" in ids
    assert "live_b" in ids
    assert "low_wf_live" in ids  # min_wf=0 so the low-WF live one is in
    # Parked / non-trading strategies are excluded even with high WF
    assert "parked_passed" not in ids
    assert "parked_fragile" not in ids
    assert "failed" not in ids
    assert "retired" not in ids
    assert ids == {"live_a", "live_b", "low_wf_live"}


def test_min_wf_filters_within_trading_set(patched_db):
    ids = {r["id"] for r in portfolio.load_strategies(min_wf=0.10)}
    # low_wf_live (0.05) drops out; parked_passed (0.95) still excluded by status
    assert ids == {"live_a", "live_b"}


def test_parked_passed_never_dilutes_even_with_top_wf(patched_db):
    # The bug this guards: a parked 'passed' candidate with the highest WF in
    # the DB must not appear in the sizing universe.
    rows = portfolio.load_strategies(min_wf=0.0)
    assert all(r["status"] == "paper_trading" for r in rows)


from auto_research import AutoResearcher


class TestInferInstrumentCoversPool:
    """Regression guard: every instrument in the research pool must be
    inferable from a strategy id. The bug this caught — pool-expansion
    instruments (indices, copper, LTC, wheat, ...) were absent from the
    _PMAP/_IMAP maps, so _infer_instrument silently returned the EUR_USD
    fallback and the portfolio reconstructed those sleeves' returns (and
    thus their inverse-vol weights / correlation haircuts) on EUR_USD data."""

    def test_every_pool_instrument_infers_correctly(self):
        wrong = {}
        for inst in AutoResearcher.DEFAULT_INSTRUMENT_POOL:
            sid = f"{inst.replace('_', '').lower()}_auto_20260101_000000_i1"
            got = portfolio._infer_instrument(sid)
            if got != inst:
                wrong[sid] = f"{got} (expected {inst})"
        assert not wrong, f"mis-routed (likely EUR_USD fallback): {wrong}"

    def test_pool_expansion_ids_do_not_fall_back_to_eurusd(self):
        for sid in ("spx500usd_auto_20260603_112639_i21",
                    "de30eur_auto_20260603_211355_i23",
                    "jp225usd_auto_20260607_085740_i21",
                    "wheatusd_auto_20260524_071105_i17",
                    "ltcusd_auto_20260603_135645_i20",
                    "xcuusd_auto_20260606_222204_i27",
                    "hk33hkd_auto_20260608_174625_i6"):
            got = portfolio._infer_instrument(sid)
            assert got != "EUR_USD", f"{sid} fell back to EUR_USD"
            # inferred symbol, underscores stripped, must equal the id prefix
            assert got.replace("_", "").lower() == sid.split("_auto_")[0]


class TestPortfolioIncompleteWriteGuard:
    """missing_active_strategies() backs the guard that refuses to overwrite
    portfolio_state.json with a partial book — the bug where a creds-less
    rebalance dropped the H4 NZD sleeves and would have zeroed their live
    weight. The helper only inspects keys, so return values are placeholders."""

    def test_all_reconstructed_returns_empty(self):
        strategies = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        returns = {"a": 1, "b": 2, "c": 3}
        assert portfolio.missing_active_strategies(strategies, returns) == []

    def test_dropped_strategies_are_reported(self):
        strategies = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        returns = {"a": 1}
        assert portfolio.missing_active_strategies(strategies, returns) == ["b", "c"]

    def test_order_follows_strategies_list(self):
        strategies = [{"id": "z"}, {"id": "y"}, {"id": "x"}]
        returns = {"y": 1}
        assert portfolio.missing_active_strategies(strategies, returns) == ["z", "x"]

    def test_main_aborts_write_when_a_strategy_is_dropped(self, tmp_path, monkeypatch):
        """End-to-end: if a live strategy fails to reconstruct, main() must
        exit non-zero and leave the existing portfolio_state.json untouched."""
        import sys as _sys
        import pandas as pd
        fake = [
            {"id": "live_a", "timeframe": "D",  "walk_forward_gt_score": 0.8, "best_params": "{}", "torture_flags": "[]"},
            {"id": "live_b", "timeframe": "D",  "walk_forward_gt_score": 0.7, "best_params": "{}", "torture_flags": "[]"},
            {"id": "live_c", "timeframe": "H4", "walk_forward_gt_score": 0.6, "best_params": "{}", "torture_flags": "[]"},
        ]
        monkeypatch.setattr(portfolio, "load_strategies", lambda *a, **k: fake)
        idx = pd.date_range("2020-01-01", periods=600, freq="D")
        good = pd.Series([0.001] * 600, index=idx)
        # live_c (the H4 sleeve) "fails to fetch" -> dropped
        monkeypatch.setattr(portfolio, "build_strategy_returns",
                            lambda r, s, e: None if r["id"] == "live_c" else good.copy())
        state = tmp_path / "portfolio_state.json"
        state.write_text('{"sentinel": "last-good"}')
        monkeypatch.setattr(portfolio, "PORTFOLIO_STATE_FILE", str(state))
        monkeypatch.setattr(_sys, "argv", ["portfolio.py", "--write"])
        with pytest.raises(SystemExit) as exc:
            portfolio.main()
        assert exc.value.code == 1
        assert state.read_text() == '{"sentinel": "last-good"}'  # NOT clobbered


class TestParseLogReturnsStrategyPnl:
    """parse_log_returns must return the STRATEGY's per-bar return (the P&L
    field = position x bar move), not the raw market 'Bar return' — which is
    nonzero even when the trader is flat. Regression for the incubation
    wheat false-negation (2026-06-10)."""

    def _write_log(self, tmp_path, monkeypatch, lines):
        monkeypatch.setattr(portfolio, "LOG_DIR", str(tmp_path))
        (tmp_path / "sleeve_x.log").write_text("\n".join(lines) + "\n")

    def test_flat_trader_yields_zero_not_market_moves(self, tmp_path, monkeypatch):
        self._write_log(tmp_path, monkeypatch, [
            "[2026-06-01 21:00:00+00:00] [D] Bar return: -0.0222, Position: +0, P&L: -0.0000",
            "[2026-06-02 21:00:00+00:00] [D] Bar return: -0.0271, Position: +0, P&L: -0.0000",
            "[2026-06-03 21:00:00+00:00] [D] Bar return: +0.0150, Position: +0, P&L: +0.0000",
        ])
        s = portfolio.parse_log_returns("sleeve_x")
        assert s is not None
        assert (s == 0.0).all(), "flat trader must show zero strategy returns"

    def test_short_position_uses_pnl_sign(self, tmp_path, monkeypatch):
        self._write_log(tmp_path, monkeypatch, [
            "[2026-06-01 21:00:00+00:00] [D] Bar return: -0.0100, Position: -1, P&L: +0.0100",
            "[2026-06-02 21:00:00+00:00] [D] Bar return: +0.0050, Position: -1, P&L: -0.0050",
            "[2026-06-03 21:00:00+00:00] [D] Bar return: -0.0020, Position: -1, P&L: +0.0020",
        ])
        s = portfolio.parse_log_returns("sleeve_x")
        assert s is not None
        assert s.iloc[0] == pytest.approx(+0.0100)
        assert s.iloc[1] == pytest.approx(-0.0050)

    def test_old_format_daily_return_still_parsed(self, tmp_path, monkeypatch):
        self._write_log(tmp_path, monkeypatch, [
            "[2026-05-11] Daily return: -0.0042, equity: 99958",
            "[2026-05-12] Daily return: +0.0031, equity: 99989",
            "[2026-05-13] Daily return: +0.0008, equity: 99997",
        ])
        s = portfolio.parse_log_returns("sleeve_x")
        assert s is not None
        assert s.iloc[0] == pytest.approx(-0.0042)
