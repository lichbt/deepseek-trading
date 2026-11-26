"""scripts/steer_report.py — the instrument that scores a directive's effect.

It has to stay comparable to the baseline it is scored against, so the tests
pin the two controls that make it comparable: a FIXED UTC hour window applied
identically to every night, and shares rather than counts.
"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import steer_report as sr  # noqa: E402

# Real keywords from meta_review._MECH_BUCKETS, so _mechanism_of agrees.
RAT = {
    'trend':          'momentum continuation persists after the breakout',
    'flow':           'order flow imbalance around the liquidity pocket',
    'cross-market':   'lead-lag divergence versus the related instrument',
    'event':          'post-CPI drift after the scheduled release',
    'calendar':       'turn-of-month rebalancing flow',
}


def _db(tmp_path, rows):
    """rows: list of (created_at, family)."""
    p = tmp_path / 'p.db'
    c = sqlite3.connect(str(p))
    c.execute('CREATE TABLE strategies (created_at TEXT, rationale TEXT)')
    c.executemany('INSERT INTO strategies VALUES (?,?)',
                  [(ts, RAT[f]) for ts, f in rows])
    c.commit(); c.close()
    return str(p)


def _night(date, family, n, hour='17'):
    return [('%sT%s:%02d:00' % (date, hour, k % 60), family) for k in range(n)]


def test_shares_are_percentages_of_that_night(tmp_path):
    rows = _night('2026-09-01', 'trend', 30) + _night('2026-09-01', 'flow', 10)
    per = sr.load_nights(_db(tmp_path, rows), 10, 16, 19)
    sh, n = sr.shares(per['2026-09-01'])
    assert n == 40
    assert sh['trend'] == pytest.approx(75.0)
    assert sh['flow'] == pytest.approx(25.0)
    assert sum(sh.values()) == pytest.approx(100.0)


def test_rows_outside_the_utc_window_are_ignored(tmp_path):
    rows = _night('2026-09-01', 'trend', 40) + _night('2026-09-01', 'flow', 40, hour='09')
    per = sr.load_nights(_db(tmp_path, rows), 10, 16, 19)
    _, n = sr.shares(per['2026-09-01'])
    assert n == 40                      # the 09:00 batch is a different window
    assert sr.shares(per['2026-09-01'])[0]['flow'] == 0.0


def test_window_is_half_open(tmp_path):
    rows = (_night('2026-09-01', 'trend', 5, hour='16')
            + _night('2026-09-01', 'flow', 5, hour='19'))
    per = sr.load_nights(_db(tmp_path, rows), 10, 16, 19)
    assert sr.shares(per['2026-09-01'])[1] == 5      # 16 in, 19 out


def test_night_below_min_n_is_excluded_from_baseline(tmp_path):
    rows = []
    for d in ('2026-09-01', '2026-09-02', '2026-09-03'):
        rows += _night(d, 'trend', 40)
    rows += _night('2026-09-04', 'trend', 5)          # thin night
    rows += _night('2026-09-05', 'trend', 40)         # recent
    per = sr.load_nights(_db(tmp_path, rows), 10, 16, 19)
    _, meta = sr.score(per, recent=1, min_n=30)
    assert '2026-09-04' in meta['excluded_below_min_n']
    assert '2026-09-04' not in meta['baseline_nights']


def test_elevated_family_reports_a_positive_shift(tmp_path):
    rows = []
    for d in ('2026-09-01', '2026-09-02', '2026-09-03', '2026-09-04'):
        rows += _night(d, 'trend', 38) + _night(d, 'cross-market', 2)
    # recent night: cross-market planted far above the baseline
    rows += _night('2026-09-05', 'trend', 20) + _night('2026-09-05', 'cross-market', 20)
    per = sr.load_nights(_db(tmp_path, rows), 10, 16, 19)
    got = {r['family']: r for r in sr.score(per, 1, 30)[0]}
    assert got['cross-market']['recent'] == pytest.approx(50.0)
    assert got['cross-market']['baseline_mean'] == pytest.approx(5.0)
    assert got['trend']['shift_sd'] is None or got['trend']['shift_sd'] < 0


def test_zero_sd_yields_none_not_zero_division(tmp_path):
    rows = []
    for d in ('2026-09-01', '2026-09-02', '2026-09-03'):
        rows += _night(d, 'trend', 40)               # identical every night -> sd 0
    rows += _night('2026-09-04', 'trend', 40)
    per = sr.load_nights(_db(tmp_path, rows), 10, 16, 19)
    got = {r['family']: r for r in sr.score(per, 1, 30)[0]}
    assert got['trend']['baseline_sd'] == 0.0
    assert got['trend']['shift_sd'] is None


def test_json_and_table_carry_the_same_numbers(tmp_path, capsys):
    rows = []
    for d in ('2026-09-01', '2026-09-02', '2026-09-03'):
        rows += _night(d, 'trend', 30) + _night(d, 'flow', 10)
    rows += _night('2026-09-04', 'trend', 20) + _night('2026-09-04', 'flow', 20)
    db = _db(tmp_path, rows)
    sr.main(['--db', db, '--recent', '1', '--json'])
    payload = json.loads(capsys.readouterr().out)
    flow = [f for f in payload['families'] if f['family'] == 'flow'][0]
    assert flow['recent'] == pytest.approx(50.0)
    sr.main(['--db', db, '--recent', '1', '--family', 'flow'])
    table = capsys.readouterr().out
    assert '50.0%' in table and 'flow' in table


def test_it_never_writes_to_the_db(tmp_path):
    rows = _night('2026-09-01', 'trend', 40) + _night('2026-09-02', 'trend', 40)
    db = _db(tmp_path, rows)
    before = Path(db).stat().st_mtime_ns
    sr.load_nights(db, 10, 16, 19)
    assert Path(db).stat().st_mtime_ns == before


def test_reproduces_the_published_baseline_on_the_real_db():
    """The checkpoint: this instrument must reproduce the figures the DIRECTED
    slot was justified against — cross-market 6.2 / 5.5 / 0.8 over 8 baseline
    nights. If this drifts, before/after is no longer a comparison."""
    db = ROOT / 'pipeline.db'
    if not db.exists():
        pytest.skip('research pipeline.db not present')
    out = subprocess.run(
        [sys.executable, str(ROOT / 'scripts' / 'steer_report.py'),
         '--db', str(db), '--nights', '9', '--recent', '1', '--json'],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    fam = {f['family']: f for f in json.loads(out.stdout)['families']}
    for name, recent, mu, sd in (('cross-market', 6.2, 5.5, 0.8),
                                 ('event', 1.7, 1.1, 0.6),
                                 ('volatility', 23.3, 23.4, 1.8),
                                 ('carry/macro', 17.6, 19.1, 1.6)):
        assert fam[name]['recent'] == pytest.approx(recent, abs=0.15), name
        assert fam[name]['baseline_mean'] == pytest.approx(mu, abs=0.15), name
        assert fam[name]['baseline_sd'] == pytest.approx(sd, abs=0.15), name
