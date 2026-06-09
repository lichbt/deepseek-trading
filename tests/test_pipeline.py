"""
Tests for the critical auto_research and validator pipeline functions.
Covers the bugs fixed in May 2026 and guards against regressions.
"""
import sys
import os
import json
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import auto_research as ar
import pipeline_utils as pu


# ─────────────────────────────────────────────────────────────────────────────
# _extract_code_blocks
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractCodeBlocks:
    def _wrap(self, python_code, json_body):
        return f"```python\n{python_code}\n```\n```json\n{json_body}\n```"

    def test_happy_path(self):
        code = "import pandas as pd\ndef generate_signals(df, p): return pd.Series(0, index=df.index)"
        jblk = json.dumps({"param_grid": {"n": [10, 20]}, "archetype": "standard"})
        result = ar._extract_code_blocks(self._wrap(code, jblk))
        assert result['code'] == code
        assert result['param_grid'] == {"n": [10, 20]}
        assert result['archetype'] == 'standard'

    def test_missing_python_block_raises(self):
        with pytest.raises(ValueError, match='No.*python block'):
            ar._extract_code_blocks("some text without code blocks")

    def test_missing_json_block_raises(self):
        with pytest.raises(ValueError, match='No.*json block'):
            ar._extract_code_blocks("```python\ncode\n```")

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match='param_grid JSON invalid'):
            ar._extract_code_blocks("```python\ncode\n```\n```json\n{bad json\n```")

    def test_empty_param_grid_raises(self):
        jblk = json.dumps({"param_grid": {}, "archetype": "standard"})
        with pytest.raises(ValueError, match='param_grid missing or empty'):
            ar._extract_code_blocks("```python\ncode\n```\n```json\n" + jblk + "\n```")

    def test_extra_prose_ignored(self):
        code = "import numpy as np\ndef generate_signals(df, p): return pd.Series(0)"
        jblk = json.dumps({"param_grid": {"k": [5]}, "archetype": "standard"})
        text = f"Here is my strategy:\n\n{self._wrap(code, jblk)}\n\nDone."
        result = ar._extract_code_blocks(text)
        assert result['code'] == code

    def test_archetype_defaults_to_standard(self):
        code = "import numpy as np\ndef generate_signals(df, p): return pd.Series(0)"
        jblk = json.dumps({"param_grid": {"k": [5]}})  # no archetype key
        result = ar._extract_code_blocks("```python\n" + code + "\n```\n```json\n" + jblk + "\n```")
        assert result['archetype'] == 'standard'


# ─────────────────────────────────────────────────────────────────────────────
# _validate_code — import auto-injection
# ─────────────────────────────────────────────────────────────────────────────

class TestValidateCodeImportInjection:
    BASE = (
        "def generate_signals(df, params):\n"
        "    return df['close'].apply(lambda x: 1 if x > 0 else 0)\n"
    )

    def test_no_pandas_gets_injected(self):
        code = "import numpy as np\n" + self.BASE
        err, cleaned = ar._validate_code(code)
        assert err is None
        assert 'import pandas' in cleaned

    def test_no_numpy_gets_injected(self):
        code = "import pandas as pd\n" + self.BASE
        err, cleaned = ar._validate_code(code)
        assert err is None
        assert 'import numpy' in cleaned

    def test_both_missing_both_injected(self):
        err, cleaned = ar._validate_code(self.BASE)
        assert err is None
        assert 'import pandas' in cleaned
        assert 'import numpy' in cleaned

    def test_existing_imports_not_doubled(self):
        code = "import pandas as pd\nimport numpy as np\n" + self.BASE
        err, cleaned = ar._validate_code(code)
        assert err is None
        assert cleaned.count('import pandas') == 1
        assert cleaned.count('import numpy') == 1

    def test_ta_import_satisfies_numpy_requirement(self):
        code = "import pandas as pd\nimport ta\n" + self.BASE
        err, cleaned = ar._validate_code(code)
        assert err is None  # ta counts as satisfying the requirement

    def test_talib_still_rejected(self):
        code = "import pandas as pd\nimport numpy as np\nimport talib\n" + self.BASE
        err, _ = ar._validate_code(code)
        assert err is not None
        assert 'talib' in err


# ─────────────────────────────────────────────────────────────────────────────
# _validate_basic_signals — timezone stripping
# ─────────────────────────────────────────────────────────────────────────────

class TestValidateBasicSignalsTZ:
    def _make_tz_df(self, tz='UTC'):
        dates = pd.date_range('2019-01-01', periods=60, freq='D', tz=tz)
        df = pd.DataFrame({
            'date':  dates,
            'open':  np.random.uniform(1800, 2000, 60),
            'high':  np.random.uniform(1800, 2000, 60),
            'low':   np.random.uniform(1800, 2000, 60),
            'close': np.random.uniform(1800, 2000, 60),
        })
        return df

    def test_tz_aware_doesnt_crash(self):
        """Code that calls df['date'].values should not raise TypeError on TZ-aware df."""
        code = (
            "import numpy as np\nimport pandas as pd\n"
            "def generate_signals(df, params):\n"
            "    # This would crash without TZ stripping:\n"
            "    dates_np = df['date'].values  # datetime64[ns, UTC] → fails in numpy as dtype\n"
            "    return pd.Series(1, index=df.index)\n"
        )
        param_grid = {"dummy": [1]}
        df = self._make_tz_df()

        # Patch get_candles_date_range to return TZ-aware df
        with patch('data_fetcher.get_candles_date_range', return_value=df):
            result = ar._validate_basic_signals(code, param_grid, instrument='XAU_USD')
        # Should not crash with TypeError; result is None (passes) or error string
        assert result is None or isinstance(result, str)

    def test_tz_naive_still_works(self):
        """TZ-naive df should work without modification."""
        dates = pd.date_range('2019-01-01', periods=60, freq='D')
        df = pd.DataFrame({
            'date':  dates,
            'open':  np.ones(60),
            'high':  np.ones(60) * 1.1,
            'low':   np.ones(60) * 0.9,
            'close': np.ones(60),
        })
        code = (
            "import pandas as pd\nimport numpy as np\n"
            "def generate_signals(df, params):\n"
            "    return pd.Series(1, index=df.index)\n"
        )
        with patch('data_fetcher.get_candles_date_range', return_value=df):
            result = ar._validate_basic_signals(code, {"n": [1]})
        assert result is None  # all-1 signals → passes min_signals=5

    def test_macro_archetype_injects_columns(self):
        """A macro strategy referencing df['us10y'] must NOT KeyError in the
        pre-check. _validate_basic_signals must inject macro columns for
        non-standard archetypes, matching the full validator."""
        dates = pd.date_range('2019-01-01', periods=120, freq='D')
        df = pd.DataFrame({
            'date':  dates,
            'open':  np.ones(120), 'high': np.ones(120) * 1.1,
            'low':   np.ones(120) * 0.9, 'close': np.ones(120),
        })
        macro_code = (
            "import pandas as pd\nimport numpy as np\n"
            "def generate_signals(df, params):\n"
            "    spread = df['us10y'] - df['us10y'].rolling(params['n']).mean()\n"
            "    return (spread > 0).astype(int)\n"
        )
        with patch('data_fetcher.get_candles_date_range', return_value=df):
            result = ar._validate_basic_signals(
                macro_code, {'n': [10]}, instrument='EUR_USD', archetype='macro')
        # The macro column reference must not crash — result is None or a
        # benign error string, never a KeyError on 'us10y'.
        assert result is None or 'KeyError' not in str(result)

    def test_standard_archetype_no_injection(self):
        """Standard archetype must not attempt macro injection."""
        dates = pd.date_range('2019-01-01', periods=60, freq='D')
        df = pd.DataFrame({
            'date':  dates,
            'open':  np.ones(60), 'high': np.ones(60) * 1.1,
            'low':   np.ones(60) * 0.9, 'close': np.ones(60),
        })
        code = (
            "import pandas as pd\nimport numpy as np\n"
            "def generate_signals(df, params):\n"
            "    return pd.Series(1, index=df.index)\n"
        )
        with patch('data_fetcher.get_candles_date_range', return_value=df):
            result = ar._validate_basic_signals(code, {'n': [1]}, archetype='standard')
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# directional_bias torture flag
# ─────────────────────────────────────────────────────────────────────────────

from validator import run_torture_tests

class TestDirectionalBias:
    def _make_df(self, n=500):
        np.random.seed(42)
        close = 100 + np.cumsum(np.random.randn(n) * 0.5)
        return pd.DataFrame({
            'date':  pd.date_range('2015-01-01', periods=n, freq='D'),
            'open':  close * 0.999,
            'high':  close * 1.002,
            'low':   close * 0.998,
            'close': close,
        })

    def _always_long(self, df, params):
        return pd.Series(1, index=df.index)

    def _two_sided(self, df, params):
        """Balanced — ~25% long, ~25% short. A genuine two-sided strategy."""
        s = pd.Series(0, index=df.index)
        s.iloc[::4] = 1
        s.iloc[2::4] = -1
        return s

    def _selective_one_sided(self, df, params):
        """Long ~12% of bars, never shorts — a SELECTIVE one-sided strategy.
        Flat most of the time, only takes longs during a regime. This is a
        timing edge (e.g. macro DXY-weakness NZD long), NOT beta, and must
        NOT be flagged."""
        s = pd.Series(0, index=df.index)
        s.iloc[::8] = 1
        return s

    def _always_short(self, df, params):
        """Short ~88% of bars — always-short beta. long_frac~0 so the >60%
        long check misses it; the active_frac>60% one-sided guard must catch
        it (this was the gap the long-only check left open)."""
        s = pd.Series(-1, index=df.index)
        s.iloc[::8] = 0
        return s

    def _few_one_sided(self, df, params):
        """One-sided but only 10 trades — below the n_trades>=20 structural guard."""
        s = pd.Series(0, index=df.index)
        s.iloc[:10] = 1
        return s

    def _run(self, func, instrument='EUR_USD'):
        return run_torture_tests(
            strategy_func=func, best_params={}, dev_data=self._make_df(),
            wf_result={'per_window_best_params': []},
            instrument=instrument, granularity='D', n_shuffle=10,
        )

    def test_always_long_flagged(self):
        flags = self._run(self._always_long, 'XAU_USD')
        assert any(f.startswith('directional_bias') for f in flags)

    def test_two_sided_not_flagged(self):
        flags = self._run(self._two_sided)
        assert not any(f.startswith('directional_bias') for f in flags)

    def test_selective_one_sided_not_flagged(self):
        """Core behaviour: a SELECTIVE one-sided strategy (long <60% of bars,
        in-market only ~12% of the time, never shorts) is timing a regime, not
        riding beta — it must NOT be flagged. Previously the one_sided check
        fired on 'never shorts' regardless of selectivity and hard-rejected
        legitimate regime-conditioned macro edges (e.g. DXY-weakness NZD long)."""
        flags = self._run(self._selective_one_sided)
        assert not any(f.startswith('directional_bias') for f in flags)

    def test_always_short_flagged(self):
        """Regression: always-SHORT beta (short ~88% of bars). long_frac~0 so
        the >60%-long check misses it; the active_frac>60% one-sided guard must
        catch it."""
        flags = self._run(self._always_short)
        bias = [f for f in flags if f.startswith('directional_bias')]
        assert bias
        assert any('one_sided_short' in f for f in bias)

    def test_few_trades_one_sided_not_flagged(self):
        """Too few trades for one-sidedness to be structural — not flagged."""
        flags = self._run(self._few_one_sided)
        assert not any('one_sided' in f for f in flags)

    def test_flag_includes_detail(self):
        """always-long is caught by the >60% long check (100%). The redundant
        one_sided flag is suppressed when 'biased' already fired."""
        flags = self._run(self._always_long, 'XAU_USD')
        bias = [f for f in flags if f.startswith('directional_bias')]
        assert any('long=100%' in f for f in bias)
        assert not any('one_sided' in f for f in bias)


# ─────────────────────────────────────────────────────────────────────────────
# param_instability torture flag — CoV guard against near-zero-mean degeneracy
# ─────────────────────────────────────────────────────────────────────────────

class TestParamStability:
    """The CoV (std/|mean|) stability metric blows up when a parameter's WF-chosen
    values sit near zero — a param that merely TOGGLES between two adjacent grid
    options gets std > mean and was falsely flagged 'param_instability'. The guard
    only flags when chosen values scatter across NON-adjacent grid levels."""

    def _noop(self, df, params):
        # No trades → no signal_shuffle/directional_bias flags to pollute the result.
        return pd.Series(0, index=pd.RangeIndex(len(df)))

    def _df(self, n=400):
        close = 100 + np.cumsum(np.random.RandomState(1).randn(n) * 0.5)
        return pd.DataFrame({
            'date': pd.date_range('2015-01-01', periods=n, freq='D'),
            'open': close, 'high': close * 1.001, 'low': close * 0.999, 'close': close,
        })

    def _run(self, per_window_params, param_grid):
        return run_torture_tests(
            strategy_func=self._noop, best_params={},
            dev_data=self._df(),
            wf_result={'per_window_best_params': per_window_params},
            instrument='EUR_USD', granularity='D', n_shuffle=5,
            param_grid=param_grid,
        )

    def test_adjacent_toggle_near_zero_not_flagged(self):
        """The real WTI case: regime_thresh in {0.0, 0.05} → CoV>1 but adjacent
        grid toggle → must NOT be flagged as unstable."""
        pwp = [{'regime_thresh': v} for v in (0.0, 0.0, 0.0, 0.05, 0.05)]
        flags = self._run(pwp, {'regime_thresh': [0.0, 0.05]})
        assert 'param_instability' not in flags

    def test_wild_nonadjacent_still_flagged(self):
        """A param bouncing across the full grid (non-adjacent levels) with CoV>1
        is genuine instability and must still be flagged."""
        pwp = [{'lookback': v} for v in (10, 10, 10, 10, 200)]
        flags = self._run(pwp, {'lookback': [10, 30, 60, 120, 200]})
        assert 'param_instability' in flags

    def test_missing_grid_preserves_flag(self):
        """If the grid for a CoV>1 param isn't supplied, fall back to flagging
        (preserve the original conservative behaviour)."""
        pwp = [{'lookback': v} for v in (10, 10, 10, 10, 200)]
        flags = self._run(pwp, {})  # no grid for 'lookback'
        assert 'param_instability' in flags


# ─────────────────────────────────────────────────────────────────────────────
# Multi-regime gate: windows_with_edge must be >= MIN_WINDOWS_WITH_EDGE
# ─────────────────────────────────────────────────────────────────────────────

from pipeline_utils import walk_forward
from validator import MIN_WINDOWS_WITH_EDGE


class TestWindowsWithEdge:
    """walk_forward must report how many windows are profitable so the
    validator can reject single-regime flukes (one great window, rest zero)."""

    def _always_long(self, df, params):
        return pd.Series(1, index=df.index)

    def _segmented_data(self, up_windows, n_per=100, train=300):
        """Build OHLC where each test window trends up or down per up_windows.

        up_windows: list of bools — one per test window — True = rising segment.
        Layout matches walk_forward's window math (test_len=n//8, train=3x).
        """
        # 5 test windows after the initial train block
        total = train + n_per * len(up_windows)
        close = np.zeros(total)
        close[0] = 100.0
        # train block: flat-ish noise
        np.random.seed(7)
        for i in range(1, train):
            close[i] = close[i-1] + np.random.randn() * 0.05
        # each test window: strong up or strong down drift
        for w, rising in enumerate(up_windows):
            seg_start = train + w * n_per
            drift = 0.3 if rising else -0.3
            for i in range(seg_start, seg_start + n_per):
                close[i] = close[i-1] + drift + np.random.randn() * 0.05
        return pd.DataFrame({
            'date': pd.date_range('2015-01-01', periods=total, freq='D'),
            'open':  close,
            'high':  close * 1.001,
            'low':   close * 0.999,
            'close': close,
        })

    def test_all_windows_profitable(self):
        """Always-long on all-rising data → every window has an edge."""
        df = self._segmented_data([True] * 5)
        wf = walk_forward(df, self._always_long, {'dummy': [1]},
                          n_windows=5, instrument='EUR_USD', granularity='D')
        assert wf['windows_with_edge'] == wf['num_valid_windows']
        assert wf['windows_with_edge'] >= MIN_WINDOWS_WITH_EDGE

    def test_single_regime_fluke_flagged(self):
        """Always-long, only ONE window rising → windows_with_edge below gate."""
        df = self._segmented_data([False, True, False, False, False])
        wf = walk_forward(df, self._always_long, {'dummy': [1]},
                          n_windows=5, instrument='EUR_USD', granularity='D')
        # Only the rising window should be profitable
        assert wf['windows_with_edge'] < MIN_WINDOWS_WITH_EDGE

    def test_gate_constant_is_sane(self):
        """MIN_WINDOWS_WITH_EDGE must be a usable threshold (2..5)."""
        assert 2 <= MIN_WINDOWS_WITH_EDGE <= 5


# ─────────────────────────────────────────────────────────────────────────────
# Zero holdout trades must fail (not silently auto-pass)
# ─────────────────────────────────────────────────────────────────────────────

from validator import validate_on_timeframe


class TestZeroHoldoutTradesFails:
    """A strategy that fires 0 trades across the holdout window is unverified
    out-of-sample and must NOT pass. The HO decay check is `ho_trade_count > 0
    and ...`, so a zero-trade strategy used to skip it and auto-pass."""

    def _passing_wf_result(self):
        """Canned walk_forward result that clears every WF gate."""
        return {
            'combined_gt_score': 0.5,
            'per_window_gt_scores': [0.4, 0.5, 0.6, 0.45],
            'per_window_trade_counts': [20, 25, 22, 18],
            'per_window_best_params': [{'p': 1}] * 4,
            'min_window_score': 0.0,
            'windows_with_edge': 4,
            'all_oos_returns': pd.Series(dtype=float),
            'num_valid_windows': 4,
            'total_windows': 5,
            'has_sufficient_windows': True,
        }

    def _frame(self, start, n):
        return pd.DataFrame({
            'date': pd.date_range(start, periods=n, freq='D'),
            'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0,
        })

    def test_zero_holdout_signals_rejected(self):
        """Strategy flat in holdout → passed=False, reason names holdout."""
        flat = lambda df, params: pd.Series(0, index=df.index)
        dev = self._frame('2015-01-01', 120)
        holdout = self._frame('2024-01-01', 60)
        with patch('validator.grid_search', return_value=({'p': 1}, 0.5)), \
             patch('validator.walk_forward', return_value=self._passing_wf_result()):
            result = validate_on_timeframe(dev, dev, holdout, flat, {'p': [1]},
                                           'EUR_USD', 'D', 'test_zero_ho')
        assert result['passed'] is False
        assert 'holdout' in result['reason'].lower()
        assert result['ho_trade_count'] == 0

    def test_trading_strategy_not_caught_by_zero_gate(self):
        """A strategy that DOES fire in holdout passes the zero-trade gate
        (it may still fail later for HO decay — just not for 'no trades')."""
        always = lambda df, params: pd.Series(1, index=df.index)
        dev = self._frame('2015-01-01', 120)
        holdout = self._frame('2024-01-01', 60)
        with patch('validator.grid_search', return_value=({'p': 1}, 0.5)), \
             patch('validator.walk_forward', return_value=self._passing_wf_result()):
            result = validate_on_timeframe(dev, dev, holdout, always, {'p': [1]},
                                           'EUR_USD', 'D', 'test_trading_ho')
        # Whatever the verdict, it must NOT be the no-holdout-trades rejection
        assert 'no holdout trades' not in result['reason'].lower()

    def test_few_holdout_entries_rejected(self):
        """A strategy with a handful of distinct holdout trades is rejected: a
        high HO score from <10 trades is small-sample noise, not edge."""
        # Two long entries over the 60-bar holdout (bars 0-9 and 30-39) — well
        # under the MIN_HO_ENTRIES floor regardless of how many bars it holds.
        def few(df, params):
            s = pd.Series(0, index=df.index)
            s.iloc[0:10] = 1
            s.iloc[30:40] = 1
            return s
        dev = self._frame('2015-01-01', 120)
        holdout = self._frame('2024-01-01', 60)
        with patch('validator.grid_search', return_value=({'p': 1}, 0.5)), \
             patch('validator.walk_forward', return_value=self._passing_wf_result()):
            result = validate_on_timeframe(dev, dev, holdout, few, {'p': [1]},
                                           'EUR_USD', 'D', 'test_few_ho')
        assert result['passed'] is False
        assert 'holdout trades' in result['reason'].lower()
        assert result['ho_entries'] == 2

    def test_ho_decay_reason_carries_raw_return(self):
        """HO decay reason must include raw_ann= so a -0.5% miss is
        distinguishable from a -40% blow-up (ho_score floors both to 0)."""
        # Multi-entry (long/flat blocks) so it clears the min-holdout-trades floor
        # and reaches the HO-decay check; flat prices still give 0 return → decay.
        multi = lambda df, params: pd.Series(
            [1 if (i % 4 < 2) else 0 for i in range(len(df))], index=df.index)
        dev = self._frame('2015-01-01', 120)
        holdout = self._frame('2024-01-01', 60)  # flat prices → 0 return → HO decay
        with patch('validator.grid_search', return_value=({'p': 1}, 0.5)), \
             patch('validator.walk_forward', return_value=self._passing_wf_result()):
            result = validate_on_timeframe(dev, dev, holdout, multi, {'p': [1]},
                                           'EUR_USD', 'D', 'test_raw_ann')
        assert result['passed'] is False
        assert 'HO decay' in result['reason']
        assert 'raw_ann=' in result['reason']


# ─────────────────────────────────────────────────────────────────────────────
# Hard-reject path: directional_bias → False + research_failed in DB
# ─────────────────────────────────────────────────────────────────────────────

from validator import validate_strategy

@pytest.fixture(autouse=True)
def isolate_db():
    old_path = pu.DB_PATH
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        tmp_path = Path(f.name)
    pu.DB_PATH = tmp_path
    pu.init_db()
    yield
    pu.DB_PATH = old_path
    if tmp_path.exists():
        os.unlink(str(tmp_path))


class TestHardRejectDirectionalBias:
    ALWAYS_LONG_CODE = (
        "import pandas as pd\nimport numpy as np\n"
        "def generate_signals(df, params):\n"
        "    return pd.Series(1, index=df.index)\n"
    )
    PARAM_GRID = {"n": [10]}

    # NOTE: a pure always-long strategy is one-sided BETA, not alpha. It is now
    # caught one gate earlier than the directional_bias torture test — by the
    # min-holdout-trades floor (a buy-and-hold has a single distinct entry). Both
    # are correct hard-rejections; the safety property these tests guard is that a
    # one-sided beta strategy never passes / never reaches paper trading. The
    # directional_bias torture LOGIC itself is unit-tested in TestDirectionalBias.
    _FAILED_STATUSES = {'research_failed', 'holdout_failed', 'walk_forward_failed'}

    def test_returns_false(self):
        """A strategy that is always long (one-sided beta) must be hard-rejected."""
        candidate = {
            'strategy_id': 'test_bias_v1',
            'code': self.ALWAYS_LONG_CODE,
            'param_grid': self.PARAM_GRID,
            'rationale': 'always long test',
            'instrument': 'XAU_USD',
            'timeframe': 'D',
        }
        passed, msg = validate_strategy(candidate, skip_insert=False)
        assert passed is False

    def test_db_status_is_research_failed(self):
        """Hard-rejected strategy must not appear as passed/deployable in the DB."""
        candidate = {
            'strategy_id': 'test_bias_db_v1',
            'code': self.ALWAYS_LONG_CODE,
            'param_grid': self.PARAM_GRID,
            'rationale': 'always long db test',
            'instrument': 'XAU_USD',
            'timeframe': 'D',
        }
        validate_strategy(candidate, skip_insert=False)
        s = pu.get_strategy_by_id('test_bias_db_v1')
        assert s is not None
        assert s['status'] in self._FAILED_STATUSES
        assert s['status'] not in {'passed', 'passed_but_fragile', 'paper_trading'}


class TestFailureScoresPreserved:
    """Validator must preserve gate-specific reason + actual scores on failure
    so meta_review can see WHY strategies fail (close vs nowhere near)."""

    # A strategy that fires very few signals → fails the IS gate with a
    # specific score, not just a generic "did not pass" with zeros.
    SPARSE_CODE = (
        "import pandas as pd\nimport numpy as np\n"
        "def generate_signals(df, params):\n"
        "    s = pd.Series(0, index=df.index)\n"
        "    s.iloc[100:103] = 1\n"  # 3 signals total
        "    return s\n"
    )

    def test_failure_reason_includes_gate(self):
        """Reason must say WHICH gate failed, not just 'did not pass'."""
        candidate = {
            'strategy_id': 'test_sparse_v1',
            'code': self.SPARSE_CODE,
            'param_grid': {'n': [10]},
            'rationale': 'sparse strategy test',
            'instrument': 'EUR_USD',
            'timeframe': 'D',
        }
        passed, msg = validate_strategy(candidate, skip_insert=False)
        assert passed is False
        # Now records the specific gate reason (e.g. "IS 0.05 < 0.3")
        assert msg != 'FAIL: Validation did not pass all gates'

    def test_db_records_specific_failure_reason(self):
        """final_status in DB must contain gate-specific reason."""
        candidate = {
            'strategy_id': 'test_sparse_db_v1',
            'code': self.SPARSE_CODE,
            'param_grid': {'n': [10]},
            'rationale': 'sparse db test',
            'instrument': 'EUR_USD',
            'timeframe': 'D',
        }
        validate_strategy(candidate, skip_insert=False)
        with pu.get_db_connection() as conn:
            row = conn.execute(
                'SELECT final_status FROM validation_results WHERE strategy_id = ?',
                ('test_sparse_db_v1',)
            ).fetchone()
        assert row is not None
        # Must be more specific than the old generic message
        assert row['final_status'] != 'FAIL: Validation did not pass all gates'


from validator import get_dev_window, get_dev_start, DEV_START, DEV_END, DEV_OVERRIDES


class TestPerInstrumentDevWindow:
    """get_dev_window pushes the DEV start AND end forward for instruments
    whose OANDA history doesn't go back to 2015 (mostly crypto). The 2026-05-26
    bug: overriding only the start while keeping global DEV_END='2019-12-31'
    produced start>end for ETH/LTC (2020-01-02 > 2019-12-31) → empty fetch →
    40 ETH/LTC iterations the next day still failed silently."""

    def test_crypto_window_is_non_empty(self):
        """Window's start must precede its end — the bug that bit us."""
        for inst in ('BTC_USD', 'ETH_USD', 'LTC_USD'):
            s, e = get_dev_window(inst)
            assert s < e, f'{inst} window inverted: start={s} > end={e} (empty range)'

    def test_crypto_window_is_forward_of_global_default(self):
        """Start must be >= the global default; otherwise no point overriding."""
        for inst in ('BTC_USD', 'ETH_USD', 'LTC_USD'):
            s, _ = get_dev_window(inst)
            assert s > DEV_START, (
                f'{inst} start {s!r} not forward of global DEV_START {DEV_START!r}'
            )

    def test_crypto_window_is_at_least_one_year(self):
        """Walk-forward needs ~5 windows × ~10 bars minimum; a 6-month dev
        period would auto-size to too-tiny WF windows."""
        from datetime import date
        for inst in ('BTC_USD', 'ETH_USD', 'LTC_USD'):
            s, e = get_dev_window(inst)
            sd = date.fromisoformat(s); ed = date.fromisoformat(e)
            assert (ed - sd).days >= 365, (
                f'{inst} dev window only {(ed - sd).days} days — need >= 365'
            )

    def test_non_crypto_uses_global_window(self):
        for inst in ('EUR_USD', 'GBP_USD', 'XAU_USD', 'WTICO_USD', 'WHEAT_USD'):
            assert get_dev_window(inst) == (DEV_START, DEV_END)

    def test_unknown_instrument_falls_back_to_default(self):
        assert get_dev_window('FAKE_PAIR_NOT_REAL') == (DEV_START, DEV_END)

    def test_overrides_map_only_contains_known_instruments(self):
        traded = {'BTC_USD', 'ETH_USD', 'LTC_USD'}
        unknown = set(DEV_OVERRIDES) - traded
        assert not unknown, f'unknown instruments in overrides: {unknown}'

    def test_get_dev_start_back_compat_alias(self):
        """Back-compat: get_dev_start should still return the start of the
        window (preserves any external imports)."""
        assert get_dev_start('BTC_USD') == get_dev_window('BTC_USD')[0]
        assert get_dev_start('EUR_USD') == DEV_START


class TestGridSearchBudget:
    """grid_search must abort a slow-but-finite strategy via its wall-clock
    budget. The per-call SIGALRM only catches a single hung call — a strategy
    that is merely slow per call never trips it but stalls the whole sweep."""

    def test_slow_strategy_raises_timeout(self, monkeypatch):
        import time as _time
        monkeypatch.setattr(pu, '_GRID_SEARCH_BUDGET', 0.05)
        data = pd.DataFrame({
            'date': pd.date_range('2020-01-01', periods=50, freq='D'),
            'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0,
        })

        def slow_strategy(df, params):
            _time.sleep(0.04)                    # well under the 30s per-call alarm
            return pd.Series(0, index=df.index)

        # Several combos — cumulative cost exceeds the budget though no single
        # call is anywhere near the per-call timeout.
        with pytest.raises(TimeoutError):
            pu.grid_search(data, slow_strategy, {'n': [1, 2, 3, 4, 5]},
                           apply_costs=False)

    def test_fast_strategy_completes(self, monkeypatch):
        """A normal fast strategy must finish well within the budget."""
        monkeypatch.setattr(pu, '_GRID_SEARCH_BUDGET', 5.0)
        data = pd.DataFrame({
            'date': pd.date_range('2020-01-01', periods=50, freq='D'),
            'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0,
        })

        def fast_strategy(df, params):
            return pd.Series(0, index=df.index)

        best_params, score = pu.grid_search(data, fast_strategy, {'n': [1, 2, 3]},
                                            apply_costs=False)
        assert isinstance(best_params, dict)


# ─────────────────────────────────────────────────────────────────────────────
# portfolio.build_strategy_returns — macro strategy reconstruction
# ─────────────────────────────────────────────────────────────────────────────

import portfolio


class TestInferArchetype:
    """portfolio._infer_archetype recovers the archetype from the columns the
    code references, because the strategies table doesn't persist it."""

    def test_macro_detected_from_dxy(self):
        assert portfolio._infer_archetype("x = df['dxy']") == 'macro'

    def test_macro_detected_from_us10y(self):
        assert portfolio._infer_archetype("x = df['us10y'].mean()") == 'macro'

    def test_session_detected(self):
        assert portfolio._infer_archetype("x = df['session']") == 'session'

    def test_news_detected(self):
        assert portfolio._infer_archetype("x = df['event_surprise']") == 'news'

    def test_pair_detected(self):
        assert portfolio._infer_archetype("x = df['close_leg2']") == 'pair'

    def test_spread_detected(self):
        assert portfolio._infer_archetype("x = df['spread']") == 'spread'

    def test_plain_ohlc_is_standard(self):
        assert portfolio._infer_archetype("x = df['close'] - df['open']") == 'standard'


class TestBuildStrategyReturnsMacro:
    """A macro strategy (references df['dxy']) must be reconstructed, not
    silently SKIPped. Regression: the bare except swallowed the KeyError raised
    when the dxy column was missing, dropping the strategy from the portfolio."""

    # NZD_USD/DXY trend strategy, same shape as the live i6 strategy.
    _MACRO_CODE = (
        "import pandas as pd\nimport numpy as np\n"
        "def generate_signals(df, params):\n"
        "    dxy = df['dxy']\n"
        "    sma = dxy.rolling(params['dxy_ma_lookback'], min_periods=1).mean()\n"
        "    sig = pd.Series(0, index=df.index, dtype=int)\n"
        "    sig[dxy > sma] = -1\n"
        "    sig[dxy < sma] = 1\n"
        "    return sig\n"
    )

    def _ohlc(self, n=300):
        dates = pd.date_range('2018-01-01', periods=n, freq='D')
        rng = np.random.RandomState(0)
        close = 0.65 + np.cumsum(rng.normal(0, 0.002, n))
        return pd.DataFrame({
            'date': dates, 'open': close, 'high': close * 1.001,
            'low': close * 0.999, 'close': close,
        })

    def _row(self):
        return {
            'id': 'nzdusd_auto_test_i6',
            'timeframe': 'D',
            'code': self._MACRO_CODE,
            'best_params': json.dumps({'dxy_ma_lookback': 20}),
        }

    def test_macro_strategy_reconstructed_not_skipped(self):
        ohlc = self._ohlc()

        def fake_inject(df, archetype, instrument, instrument2, start, end, gran):
            assert archetype == 'macro'          # recovered from df['dxy']
            df = df.copy()
            rng = np.random.RandomState(1)
            df['dxy'] = 100 + np.cumsum(rng.normal(0, 0.3, len(df)))
            return df

        with patch('portfolio.get_candles_date_range', return_value=ohlc), \
             patch('portfolio.inject_supplementary_data', side_effect=fake_inject):
            ret = portfolio.build_strategy_returns(
                self._row(), '2018-01-01', '2019-01-01')

        # Reconstructed: a non-empty daily return series, not None (the old SKIP).
        assert ret is not None
        assert len(ret) > 0
        assert ret.name == 'nzdusd_auto_test_i6'

    def test_missing_dxy_column_would_skip_without_injection(self):
        """Sanity check on the failure mode: if injection is bypassed and the
        dxy column is absent, the strategy raises and the function returns None
        (now with the error logged to stderr instead of silently)."""
        ohlc = self._ohlc()

        with patch('portfolio.get_candles_date_range', return_value=ohlc), \
             patch('portfolio.inject_supplementary_data', side_effect=lambda df, *a, **k: df):
            ret = portfolio.build_strategy_returns(
                self._row(), '2018-01-01', '2019-01-01')

        assert ret is None

    def test_standard_strategy_skips_injection(self):
        """A plain-OHLC strategy must not trigger supplementary injection."""
        ohlc = self._ohlc()
        std_row = {
            'id': 'eurusd_auto_test',
            'timeframe': 'D',
            'code': (
                "import pandas as pd\nimport numpy as np\n"
                "def generate_signals(df, params):\n"
                "    sma = df['close'].rolling(params['n'], min_periods=1).mean()\n"
                "    sig = pd.Series(0, index=df.index, dtype=int)\n"
                "    sig[df['close'] > sma] = 1\n"
                "    sig[df['close'] < sma] = -1\n"
                "    return sig\n"
            ),
            'best_params': json.dumps({'n': 20}),
        }

        with patch('portfolio.get_candles_date_range', return_value=ohlc), \
             patch('portfolio.inject_supplementary_data') as mock_inject:
            ret = portfolio.build_strategy_returns(std_row, '2018-01-01', '2019-01-01')

        mock_inject.assert_not_called()
        assert ret is not None


from auto_research import AutoResearcher

# Reference prices (recent) for round-trip-cost sanity checks — kept local so
# the test never hits the network.
_REF_PRICES = {
    'SPX500_USD': 7361.0, 'NAS100_USD': 28798.0, 'US30_USD': 50740.0,
    'DE30_EUR': 24592.0, 'UK100_GBP': 10344.0, 'JP225_USD': 63838.0,
    'AU200_AUD': 8505.0, 'HK33_HKD': 24609.0, 'CN50_USD': 15348.0,
    'XCU_USD': 6.2174, 'XPT_USD': 1763.78, 'XPD_USD': 1212.83,
    'LTC_USD': 43.68, 'WHEAT_USD': 5.75, 'SOYBN_USD': 11.12,
    'EUR_GBP': 0.8637, 'EUR_JPY': 184.727, 'GBP_JPY': 213.866,
}

# Indices held overnight pay financing — they must carry a swap cost.
_INDEX_INSTRUMENTS = {
    'SPX500_USD', 'NAS100_USD', 'US30_USD', 'DE30_EUR', 'UK100_GBP',
    'JP225_USD', 'AU200_AUD', 'HK33_HKD', 'CN50_USD',
}


class TestRealisticCostsForPool:
    """Every instrument in the research pool must resolve to a realistic,
    non-trivial trading cost. Regression guard for the bug where pool-expansion
    instruments (indices, copper, LTC, wheat, JPY crosses) were absent from the
    cost tables and silently fell back to the forex default (2.0 pips x 0.0001),
    which is ~0% on a ~25,000-point index — making validations cost-blind."""

    @staticmethod
    def _is_plain_fx(inst):
        """True for non-JPY FX pairs, where the 0.0001 pip-value default is
        correct (1 pip = 0.0001). JPY pairs, indices, metals, crypto and
        grains all need an explicit pip-value or they mis-cost."""
        ccy = {'EUR', 'USD', 'GBP', 'AUD', 'NZD', 'CHF', 'CAD'}
        parts = inst.split('_')
        return len(parts) == 2 and parts[0] in ccy and parts[1] in ccy

    def test_no_pool_instrument_falls_back_to_forex_default(self):
        """No pool instrument may rely on the spread/pip-value defaults
        (except non-JPY FX pairs, for which the default is correct)."""
        missing_spread, missing_pip = [], []
        for inst in AutoResearcher.DEFAULT_INSTRUMENT_POOL:
            if inst not in pu.TYPICAL_SPREADS_PIPS:
                missing_spread.append(inst)
            if inst not in pu.PIP_VALUE and not self._is_plain_fx(inst):
                missing_pip.append(inst)
        assert not missing_spread, f"no spread entry: {missing_spread}"
        assert not missing_pip, f"no pip-value entry (would mis-cost): {missing_pip}"

    def test_round_trip_spread_in_realistic_band(self):
        """Resolved round-trip spread cost is neither ~0 (the bug) nor absurd."""
        for inst, price in _REF_PRICES.items():
            rt = pu.get_spread_pips(inst) * pu.get_pip_value(inst) / price
            assert rt > 2e-5, f"{inst} RT spread {rt:.6%} ~ 0 (forex-default bug)"
            assert rt < 0.01, f"{inst} RT spread {rt:.6%} unrealistically high"

    def test_hk33_is_no_longer_near_zero_cost(self):
        """The specific instrument that surfaced the bug."""
        rt = pu.get_spread_pips('HK33_HKD') * pu.get_pip_value('HK33_HKD') / 24609.0
        assert rt > 3e-4, f"HK33 RT spread should be ~0.045%, got {rt:.6%}"

    def test_indices_carry_financing(self):
        """Leveraged index CFDs held overnight must incur a daily swap cost."""
        for inst in _INDEX_INSTRUMENTS:
            assert pu.get_daily_swap(inst) < 0, f"{inst} has no financing cost"

    def test_costs_materially_reduce_held_index_returns(self):
        """apply_trading_costs must visibly bite a long-held HK33 stream."""
        n = 200
        close = pd.Series(np.linspace(24000, 25000, n + 1))
        data = pd.DataFrame({'close': close})
        signals = pd.Series([1] * (n + 1))  # long-and-hold
        raw = pu.compute_strategy_returns(data, signals)
        net = pu.apply_trading_costs(raw, signals, 'HK33_HKD', 'H4', data=data)
        # Held every bar -> financing alone should drag net below raw.
        assert net.sum() < raw.sum(), "costs did not reduce held HK33 returns"
        drag = raw.sum() - net.sum()
        assert drag > 1e-3, f"cost drag {drag:.5f} implausibly small for 200 H4 bars"
