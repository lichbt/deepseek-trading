"""The log-line format is an undeclared interface between two files.

live_test.py PRINTS the bar line; scripts/backfill_sleeve_equity.py PARSES it.
Nothing connects them, so a cosmetic change to the print statement silently
breaks the backfill — it would parse 0 lines and report success. These tests pin
the format from the parser's side.

The timestamp assertion is the load-bearing one. live_test writes
str(current_bar_time) as sleeve_equity.bar_time, and the log prefix is that same
value; if the parser normalised or reformatted it, UNIQUE(sleeve_id, bar_time)
would stop deduping and the table would hold two rows per bar in two formats.
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "bf", ROOT / "scripts" / "backfill_sleeve_equity.py")
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)


def _write(tmp_path, text):
    p = tmp_path / "s.log"
    p.write_text(text)
    return p


def test_parses_the_real_line_format(tmp_path):
    p = _write(tmp_path,
               "[2026-07-27 21:00:00+00:00] [D] Bar return: -0.0084, Position: -1, P&L: +0.0084\n")
    (ts, pos, br, pr), = bf.parse_log(p)
    assert ts == "2026-07-27 21:00:00+00:00", "timestamp must be VERBATIM — dedupe depends on it"
    assert pos == -1
    assert br == pytest.approx(-0.0084)
    assert pr == pytest.approx(0.0084)


def test_short_position_return_is_signed_correctly(tmp_path):
    """A short on a falling bar profits — the sign must survive the parse."""
    p = _write(tmp_path,
               "[2026-07-27 21:00:00+00:00] [D] Bar return: -0.0084, Position: -1, P&L: +0.0084\n")
    (_, pos, br, pr), = bf.parse_log(p)
    assert br < 0 < pr and pos == -1


def test_flat_bar_is_kept_not_dropped(tmp_path):
    """A flat bar is real information: the sleeve was observed and chose to be flat.

    Dropping it would make 'flat' indistinguishable from 'not observed'.
    """
    p = _write(tmp_path,
               "[2026-07-26 21:00:00+00:00] [D] Bar return: -0.0034, Position: +0, P&L: -0.0000\n")
    (_, pos, br, pr), = bf.parse_log(p)
    assert pos == 0 and pr == 0.0
    assert br == pytest.approx(-0.0034), "the instrument still moved"


def test_ignores_noise_and_other_timeframes_parse(tmp_path):
    p = _write(tmp_path, "\n".join([
        "  [Macro] Injected 5 columns: ['fed_rate']",
        "[Kelly] Recomputed: kelly=0.031 -> mult=2.0x",
        "[2026-07-27 21:00:00+00:00] [D] Bar return: -0.0084, Position: -1, P&L: +0.0084",
        "[2026-07-27 17:00:00+00:00] [H4] Bar return: +0.0010, Position: +1, P&L: +0.0010",
        "Starting live trading loop (D bars, polling every 3600s)...",
    ]))
    rows = bf.parse_log(p)
    assert len(rows) == 2, "only bar lines, but ALL timeframes"


def test_old_daily_return_format_is_not_silently_half_parsed(tmp_path):
    """The pre-2026-06 format carries no Position field.

    It must be SKIPPED rather than parsed with a guessed position — inventing a
    position would fabricate position_return, which is the column decisions get
    made on.
    """
    p = _write(tmp_path, "[2026-05-11] Daily return: -0.0042, equity: 100000\n")
    assert bf.parse_log(p) == []


def test_missing_log_is_not_an_error(tmp_path):
    assert bf.parse_log(tmp_path / "nope.log") == []
