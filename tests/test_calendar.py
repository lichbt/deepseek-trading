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
