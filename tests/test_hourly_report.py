"""Tests for the Telegram-safe HTML escaping in the status report.

A single stray '<' (e.g. incubation's '(< 5)' or a 'IS 0.21 < 0.3' fail string)
made Telegram's HTML parser 400 the entire report, so no report was delivered.
_telegram_safe_html escapes stray <,>,& while preserving the report's <b> tags.
"""
import sqlite3

import hourly_report
from hourly_report import _telegram_safe_html as S


def test_preserves_bold_tags():
    assert S("<b>Status</b>") == "<b>Status</b>"


def test_escapes_stray_less_than():
    assert S("0 active days (< 5)") == "0 active days (&lt; 5)"


def test_escapes_is_comparison():
    assert S("FAIL: IS 0.21 < 0.3") == "FAIL: IS 0.21 &lt; 0.3"


def test_escapes_ampersand():
    assert S("P&L") == "P&amp;L"


def test_escapes_stray_greater_than():
    assert S("WF > 0.5") == "WF &gt; 0.5"


def test_mixed_bold_and_stray():
    assert S("<b>Incubation</b>\n  sleeve 0 active (< 5)") == \
        "<b>Incubation</b>\n  sleeve 0 active (&lt; 5)"


def test_real_report_line_is_telegram_safe():
    # the exact line that broke it at byte 1102
    out = S("🧪 xauusd_auto_..._i9  10d  incubating — 0 active days (< 5)")
    assert "(&lt; 5)" in out and "(< 5)" not in out


def test_live_section_prefers_sleeve_units():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE strategies(id TEXT, status TEXT);
        CREATE TABLE live_status(strategy_id TEXT, equity_curve TEXT,
          current_gt_score REAL, current_position INTEGER, last_updated TEXT, start_date TEXT);
        CREATE TABLE sleeve_units(sleeve_id TEXT, units REAL, stop REAL);
        INSERT INTO strategies VALUES ('eurusd_auto_test', 'paper_trading');
        INSERT INTO live_status VALUES ('eurusd_auto_test', NULL, NULL, 0, NULL, '2026-01-01');
        INSERT INTO sleeve_units VALUES ('eurusd_auto_test', -1000, NULL);
    ''')
    out = hourly_report.build_live_section(conn.cursor())
    assert '1 in-market' in out
    assert 'SHORT' in out


class TestReportTargetsTheFundedAccount:
    """The 4h report is about the PROP account.

    prop_guard defaults to VENUE='oanda' and only the pod sets the env var, so
    the section headed "Prop Limits" was rendering the OANDA PAPER book's
    drawdown. Measured 2026-08-09: -1.57% total from a 102,051 start, while the
    funded account sat at -0.02% from 100,000. The venues keep separate state
    files because their anchors differ — reading the wrong one is the wrong
    account, not a rounding error.
    """

    def test_report_pins_the_ctrader_venue(self):
        import inspect
        src = inspect.getsource(hourly_report.build_report)
        assert "PROP_GUARD_VENUE" in src and "'ctrader'" in src
        assert 'importlib.reload' in src, 'no guard against an earlier import'

    def test_incubation_and_live_equity_are_not_assembled(self, monkeypatch):
        """Behavioural, not textual. A first cut grepped the source and failed on
        the comment that RECORDS why these were dropped — the explanation is not
        the thing under test. Inject sections that shout, and assert silence.
        """
        import sys, types

        incub = types.ModuleType('incubation')
        incub.report_section = lambda: 'INCUB_MARKER'
        monkeypatch.setitem(sys.modules, 'incubation', incub)

        guard = types.ModuleType('prop_guard')
        guard.VENUE = 'ctrader'
        guard.report_section = lambda m=None, compact=False: 'PROP_MARKER'
        monkeypatch.setitem(sys.modules, 'prop_guard', guard)

        monkeypatch.setattr(hourly_report, 'build_live_section',
                            lambda cur: 'LIVE_EQUITY_MARKER')

        out = hourly_report.build_report()
        assert 'INCUB_MARKER' not in out
        assert 'LIVE_EQUITY_MARKER' not in out
        assert 'PROP_MARKER' in out, 'the prop section must still be assembled'

    def test_compact_prop_section_keeps_every_figure(self):
        import prop_guard
        m = {'nav': 99980.0, 'start_nav': 100000.0, 'day_anchor': 99987.56,
             'daily_dd_now': -0.0001, 'daily_dd_worst': -0.0009,
             'total_dd_now': -0.0002, 'gain': -0.0002,
             'max_total_dd': -0.0009, 'worst_daily_dd_all': -0.0009,
             'day_base_balance': 99987.56, 'day_base_equity': 99977.52}
        short = prop_guard.report_section(m, compact=True)
        assert short.count('\n') == 1, 'compact must be two lines'
        for frag in ('99,980', 'day ', 'worst', 'total', 'P '):
            assert frag in short, frag

    def test_stale_fix_state_is_not_reported_as_current(self, tmp_path, monkeypatch):
        # Since the Zeabur cutover the local file stops changing; printing it as
        # current showed 13-day-old positions.
        f = tmp_path / 'fix_runner_state.json'
        f.write_text('{"x": {"pos_id": "1", "side": 1, "units": 100}}')
        monkeypatch.setattr(hourly_report, 'FIX_STATE_PATH', f)
        assert hourly_report.build_fix_section() != ''
        import os as _os, time as _t
        old = _t.time() - (hourly_report.FIX_STATE_MAX_AGE_H + 1) * 3600
        _os.utime(f, (old, old))
        assert hourly_report.build_fix_section() == ''
