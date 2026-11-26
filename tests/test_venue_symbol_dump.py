"""Boot-time venue symbol dump must never be able to block a trading pass.

`dump_venue_symbols` (fix_runner.py) exists so the pod log is the authority on which
instruments the broker carries — the mapped tables cannot distinguish "broker does
not list UK100" from "we guessed the wrong venue name for UK100".
"""
import fix_runner


class _Fake:
    def __init__(self, mapping=None, boom=None):
        self._m = mapping or {}
        self._boom = boom

    def get_symbols(self):
        if self._boom:
            raise self._boom
        return self._m


def test_dump_returns_sorted_unique_names():
    got = fix_runner.dump_venue_symbols(_Fake({1: 'EURUSD', 2: 'UK100', 3: 'EURUSD'}))
    assert got == ['EURUSD', 'UK100']


def test_dump_swallows_errors_and_returns_empty():
    assert fix_runner.dump_venue_symbols(_Fake(boom=RuntimeError('auth'))) == []
    # and a client with no get_symbols at all must not explode either
    assert fix_runner.dump_venue_symbols(object()) == []
