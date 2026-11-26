"""Re-reading portfolio_state.json on every bar, safely.

Both books used to freeze this file at process start, so an automatic DECAYED
verdict (0.5x conviction AND 0.5x decay_kelly_scale) did nothing until someone
restarted the book. live_test now refreshes per bar.

The danger is the fallback, not the reload. `weights.get(sid, 1.0/n) * n` is
EXACTLY 1.0, and so is the bare `except`. That default is correct on a FIRST load
(an incubating sleeve sizes nominally) and a silent RISK INCREASE on a refresh —
a sleeve deliberately held at conviction 0.11 (~0.5x) would double to 1.0x
because it dropped out of the file or the read tore.
"""
import json
import os

import pytest

import live_test as L


STATE = {
    "n_strategies": 4,
    "weights": {"a": 0.40, "b": 0.20, "small": 0.05},
    "decay_kelly_scale": {"a": 1.0, "small": 0.5},
    "correlated_pairs": [{"a": "a", "b": "b", "corr": 0.8, "weaker": "b"}],
}


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    p = tmp_path / "portfolio_state.json"
    p.write_text(json.dumps(STATE))
    monkeypatch.setattr(L, "PORTFOLIO_STATE_FILE", str(p))
    return p


# --- first load keeps the documented nominal behaviour --------------------

def test_first_load_of_an_absent_sleeve_is_nominal(state_file):
    """An incubating sleeve is deliberately NOT in portfolio_state.json and must
    still size at the equal-weight baseline on paper."""
    ws, peers, dks = L._load_portfolio_state("incubating")
    assert ws == pytest.approx(1.0)
    assert dks == 1.0


def test_first_load_reads_weight_and_decay(state_file):
    ws, peers, dks = L._load_portfolio_state("small")
    assert ws == pytest.approx(0.05 * 4)      # 0.20x — deliberately small
    assert dks == 0.5


# --- refresh must never silently size UP ----------------------------------

def test_refresh_holds_when_the_sleeve_vanishes_from_the_file(state_file):
    """THE hazard. Falling back to 1.0 here would take a 0.20x sleeve to 1.0x —
    a 5x silent increase — because a regenerated file dropped it."""
    prev = L._load_portfolio_state("small")
    state_file.write_text(json.dumps({**STATE, "weights": {"a": 0.5, "b": 0.5}}))
    assert L._load_portfolio_state("small", previous=prev) == prev


def test_refresh_holds_on_a_torn_read(state_file):
    """portfolio.py rewrites this file at 00:05 while the book is live."""
    prev = L._load_portfolio_state("small")
    state_file.write_text('{"weights": {"small": 0.0')      # truncated JSON
    assert L._load_portfolio_state("small", previous=prev) == prev


def test_refresh_holds_when_the_file_disappears(state_file):
    prev = L._load_portfolio_state("small")
    os.remove(state_file)
    assert L._load_portfolio_state("small", previous=prev) == prev


def test_refresh_applies_a_real_decay_verdict(state_file):
    """The whole point: a DECAYED verdict must take effect without a restart."""
    prev = L._load_portfolio_state("a")
    assert prev[2] == 1.0
    state_file.write_text(json.dumps({
        **STATE,
        "weights": {**STATE["weights"], "a": 0.20},          # conviction halved
        "decay_kelly_scale": {**STATE["decay_kelly_scale"], "a": 0.5},
    }))
    ws, _, dks = L._load_portfolio_state("a", previous=prev)
    assert dks == 0.5
    assert ws == pytest.approx(0.20 * 4)
    assert ws < prev[0]


def test_refresh_still_clamps_a_runaway_weight(state_file):
    """The MAX_WEIGHT_SCALE guard must survive the refresh path."""
    prev = L._load_portfolio_state("a")
    state_file.write_text(json.dumps({**STATE, "weights": {"a": 99.0}}))
    ws, _, _ = L._load_portfolio_state("a", previous=prev)
    assert ws <= L.MAX_WEIGHT_SCALE


# --- the writer must not produce a torn read ------------------------------

def test_portfolio_writes_state_atomically():
    """live_test reads this on every bar; a truncate-and-write leaves a window
    where a reader sees half a document. os.replace closes it."""
    import inspect

    import portfolio as P

    src = inspect.getsource(P.main) if hasattr(P, 'main') else inspect.getsource(P)
    assert 'os.replace' in src, 'portfolio_state.json must be written atomically'


# --- fix_runner is deliberately NOT refreshed -----------------------------

def test_fix_runner_still_loads_sleeves_once():
    """The pod reads /app/portfolio_state.json from the IMAGE — it cannot change
    without a redeploy, so re-reading buys nothing and adds the hazards above.
    Pinned so a well-meaning 'consistency' change has to argue with a test."""
    import inspect

    import fix_runner

    doc = inspect.getdoc(fix_runner.load_sleeves) or ''
    assert 'ONCE' in doc and 'IMAGE' in doc.upper(), (
        'the rationale for not refreshing fix_runner must stay documented')
