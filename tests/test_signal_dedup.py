"""Behavioural dedup: hash the SIGNAL VECTOR, not the code text.

Measured 2026-09-16: gbpjpy_auto_20260709_073651_i20 and
gbpjpy_auto_20260719_131055_i10 carry DIFFERENT compute_strategy_fingerprint
values, both PASSED, and emit byte-identical signal vectors. The code-text
fingerprint cannot see that; compute_signal_fingerprint can.
"""
import contextlib
import sqlite3

import numpy as np
import pandas as pd

import pipeline_utils as pu
from validator import compute_signal_fingerprint as fp


def _cal_code(short_guard="tdom_left <= 1"):
    return ("def generate_signals(df, params):\n"
            "    import pandas as pd\n"
            "    long_ = (df['turn_of_month'] == 1).astype(int)\n"
            f"    short_ = (df['{short_guard.split()[0]}'] "
            f"{short_guard.split()[1]} {short_guard.split()[2]}).astype(int)\n"
            "    return long_ - short_\n")


def test_reworded_twin_has_the_same_signal_fingerprint():
    a = _cal_code("tdom_left <= 1")
    b = _cal_code("tdom_left == 1")
    assert a != b                                   # different code text
    assert fp(a, {}, 'D', 'calendar') == fp(b, {}, 'D', 'calendar')


def test_different_behaviour_has_a_different_fingerprint():
    a = _cal_code("tdom_left <= 1")
    c = _cal_code("tdom <= 1")
    assert fp(a, {}, 'D', 'calendar') != fp(c, {}, 'D', 'calendar')


def test_timeframe_is_folded_in():
    a = "def generate_signals(df, params):\n    return (df['close'] > df['open']).astype(int)"
    assert fp(a, {}, 'D', 'standard') != fp(a, {}, 'H4', 'standard')


def test_unrunnable_code_fails_open():
    bad = "def generate_signals(df, params):\n    return df['no_such_column']"
    assert fp(bad, {}, 'D', 'standard') is None
    assert fp('', {}, 'D', 'standard') is None


def test_calendar_probe_frame_does_not_raise():
    """Regression: the probe frame must carry the calendar columns, or every
    calendar strategy fingerprints to None and the dedup silently never fires."""
    code = _cal_code("tdom_left <= 1")
    assert fp(code, {}, 'D', 'calendar') is not None


def _fake_conn(path):
    @contextlib.contextmanager
    def _cm():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()
    return _cm


def _db(tmp_path):
    path = tmp_path / 's.db'
    c = sqlite3.connect(path)
    c.execute('CREATE TABLE strategies (id TEXT PRIMARY KEY, signal_fp TEXT, '
              'instrument TEXT, status TEXT)')
    c.execute("INSERT INTO strategies VALUES ('old', 'fp1', 'GBP_JPY', 'passed')")
    c.execute("INSERT INTO strategies VALUES ('dead', 'fp2', 'GBP_JPY', 'holdout_failed')")
    c.commit(); c.close()
    return path


def test_same_signal_same_instrument_is_a_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(pu, 'get_db_connection', _fake_conn(_db(tmp_path)))
    assert pu.check_signal_is_new('fp1', 'GBP_JPY')['new'] is False


def test_failed_twin_is_not_a_duplicate(tmp_path, monkeypatch):
    """Failures may be re-proposed and re-validated — only the passed population
    blocks. Otherwise the search freezes on its own history."""
    monkeypatch.setattr(pu, 'get_db_connection', _fake_conn(_db(tmp_path)))
    assert pu.check_signal_is_new('fp2', 'GBP_JPY')['new'] is True


def test_different_instrument_or_fp_is_new(tmp_path, monkeypatch):
    monkeypatch.setattr(pu, 'get_db_connection', _fake_conn(_db(tmp_path)))
    assert pu.check_signal_is_new('fp1', 'EUR_USD')['new'] is True
    assert pu.check_signal_is_new('other', 'GBP_JPY')['new'] is True


def test_missing_fp_is_new(tmp_path, monkeypatch):
    monkeypatch.setattr(pu, 'get_db_connection', _fake_conn(_db(tmp_path)))
    assert pu.check_signal_is_new('', 'GBP_JPY')['new'] is True
