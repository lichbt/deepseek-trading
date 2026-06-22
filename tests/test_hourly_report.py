"""Tests for the Telegram-safe HTML escaping in the status report.

A single stray '<' (e.g. incubation's '(< 5)' or a 'IS 0.21 < 0.3' fail string)
made Telegram's HTML parser 400 the entire report, so no report was delivered.
_telegram_safe_html escapes stray <,>,& while preserving the report's <b> tags.
"""
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
