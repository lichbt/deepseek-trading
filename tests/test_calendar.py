"""Calendar archetype: explicit seasonal columns so day-of-week / turn-of-month
strategies read df['dow'] etc. instead of df.index.dayofweek (the RangeIndex crash)."""
import numpy as np
import pandas as pd

import supplementary_data as S
import auto_research as A
import live_test as L


def _df(n=300):
    dates = pd.bdate_range('2022-01-03', periods=n)   # business days = Mon..Fri
    return pd.DataFrame({'date': dates, 'open': 1.0, 'high': 1.0, 'low': 1.0,
                         'close': np.arange(n) + 1.0})


def test_inject_calendar_columns():
    df = S.inject_calendar_columns(_df())
    for c in S.CALENDAR_COLS:
        assert c in df.columns
    assert df['dow'].between(0, 4).all()              # business days
    assert df['tdom'].min() == 1
    assert df['cal_month'].between(1, 12).all()
    # turn-of-month definition holds, and the first trading day is in it
    assert (df['turn_of_month'] == ((df['tdom'] <= 3) | (df['tdom_left'] <= 1)).astype(int)).all()
    assert df.loc[df['tdom'] == 1, 'turn_of_month'].eq(1).all()


def test_infer_archetype_calendar():
    code = ("def generate_signals(df, p):\n"
            "    return (df['dow'] == 0).astype(int) - (df['dow'] == 4).astype(int)")
    assert A._infer_archetype(code) == 'calendar'   # gen side
    assert L._infer_archetype(code) == 'calendar'   # live side


def test_calendar_strategy_runs_without_index_crash():
    # the failure mode this fixes: df.index is a RangeIndex at signal time
    df = S.inject_supplementary_data(_df().reset_index(drop=True), 'calendar', 'EUR_USD')
    sig = (df['dow'] == 0).astype(int) - (df['dow'] == 4).astype(int)   # long Mon / short Fri (two-sided)
    assert set(sig.unique()) <= {-1, 0, 1} and (sig != 0).any()


def _oanda_shaped(start='2025-01-01', end='2026-09-01'):
    """Daily frame with OANDA's real stamp weekdays: Mon-Thu plus Sun, never Fri/Sat."""
    rng = pd.date_range(start, end, freq='D')
    rng = rng[rng.dayofweek.isin((0, 1, 2, 3, 6))]
    return pd.DataFrame({'date': rng, 'open': 1.0, 'high': 1.0, 'low': 1.0,
                         'close': np.arange(len(rng)) + 1.0})


class TestCalendarColumnsAreFrameIndependent:
    """tdom/tdom_left must be properties of the MONTH, not of the rows present.

    They used to be cumcount() and size-cumcount() within the frame. A live frame
    ends at the newest bar, so its current month is always partial: the last row
    always came out tdom_left == 1 and turn_of_month == 1. Both runners read only
    that last row (live_test signals.iloc[-1], fix_runner sig[-1]), so a
    turn-of-month strategy fired EVERY live pass while its backtest fired a few
    times a month. Measured 2026-09-03 on gbpjpy_auto_20260705_161108_i13: a
    500-bar rolling frame diverged from full history on 8 of 11 decision days.
    """

    def test_truncating_the_frame_does_not_change_shared_rows(self):
        full = S.inject_calendar_columns(_oanda_shaped())
        cut = len(full) - 137                       # ends mid-month, like a live frame
        part = S.inject_calendar_columns(_oanda_shaped().iloc[:cut].reset_index(drop=True))
        shared = full.iloc[:cut]
        for col in ('tdom', 'tdom_left', 'turn_of_month', 'dow', 'cal_month'):
            assert (shared[col].values == part[col].values).all(), \
                f'{col} changed when the frame was truncated'

    def test_last_row_of_a_partial_month_is_not_turn_of_month(self):
        """The exact bug: every live pass thought it was the last trading day."""
        df = _oanda_shaped(end='2026-08-19')        # mid-August, month incomplete
        out = S.inject_calendar_columns(df)
        assert out['tdom_left'].iloc[-1] > 1, \
            'last row of a partial month still reads as the last trading day'
        assert out['turn_of_month'].iloc[-1] == 0

    def test_every_rolling_window_agrees_with_full_history(self):
        """Simulate consecutive live passes over the same series."""
        base = _oanda_shaped()
        full = S.inject_calendar_columns(base)
        for cut in range(len(base) - 30, len(base)):
            w = S.inject_calendar_columns(base.iloc[:cut].reset_index(drop=True))
            assert w['turn_of_month'].iloc[-1] == full['turn_of_month'].iloc[cut - 1], \
                f'rolling frame ending at row {cut} disagrees with full history'

    def test_a_real_last_trading_day_still_reads_as_turn_of_month(self):
        """The fix must not simply zero the flag."""
        out = S.inject_calendar_columns(_oanda_shaped(end='2026-09-01'))
        aug = out[out['date'].dt.to_period('M') == pd.Period('2026-08')]
        assert aug['tdom_left'].iloc[-1] == 1
        assert aug['turn_of_month'].iloc[-1] == 1
        assert aug['turn_of_month'].sum() >= 4      # first 3 + last
