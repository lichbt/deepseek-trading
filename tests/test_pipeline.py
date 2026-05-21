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

    def _one_sided_sparse(self, df, params):
        """Long ~12% of bars, never shorts — the long-only USD/JPY fake-pass
        pattern. long_frac is well under 60% but it is structurally one-sided."""
        s = pd.Series(0, index=df.index)
        s.iloc[::8] = 1
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

    def test_one_sided_sparse_flagged(self):
        """Regression: long-only strategy, long <60% of bars but never shorts —
        must be flagged. The >60% check alone missed exactly this pattern."""
        flags = self._run(self._one_sided_sparse)
        bias = [f for f in flags if f.startswith('directional_bias')]
        assert bias
        assert any('one_sided' in f for f in bias)

    def test_few_trades_one_sided_not_flagged(self):
        """Too few trades for one-sidedness to be structural — not flagged."""
        flags = self._run(self._few_one_sided)
        assert not any('one_sided' in f for f in flags)

    def test_flag_includes_detail(self):
        """always-long trips BOTH sub-checks: >60% long and one-sided."""
        flags = self._run(self._always_long, 'XAU_USD')
        bias = [f for f in flags if f.startswith('directional_bias')]
        assert any('100%' in f for f in bias)
        assert any('one_sided' in f for f in bias)


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

    def test_ho_decay_reason_carries_raw_return(self):
        """HO decay reason must include raw_ann= so a -0.5% miss is
        distinguishable from a -40% blow-up (ho_score floors both to 0)."""
        always = lambda df, params: pd.Series(1, index=df.index)
        dev = self._frame('2015-01-01', 120)
        holdout = self._frame('2024-01-01', 60)  # flat prices → 0 return → HO decay
        with patch('validator.grid_search', return_value=({'p': 1}, 0.5)), \
             patch('validator.walk_forward', return_value=self._passing_wf_result()):
            result = validate_on_timeframe(dev, dev, holdout, always, {'p': [1]},
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

    def test_returns_false(self):
        """A strategy that is always long should be hard-rejected."""
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
        assert 'directional_bias' in msg

    def test_db_status_is_research_failed(self):
        """Hard-rejected strategy must not appear as passed in the DB."""
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
        assert s['status'] == 'research_failed'


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
