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
