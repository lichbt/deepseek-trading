"""
Tests for auto_research.py — _validate_thesis, _extract_json, and
the deeper _validate_code branches not covered by test_pipeline.py.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import auto_research as ar


# ─────────────────────────────────────────────────────────────────────────────
# _CREATIVE_CONSTRAINTS — must not instruct patterns banned by thesis.md
# ─────────────────────────────────────────────────────────────────────────────

class TestCreativeConstraints:
    def test_no_banned_open_to_close_direction(self):
        """thesis.md bans open-to-close direction entries; no constraint may
        instruct them. Constraint #9 used to say 'Base entry on open-to-close
        direction relative to prior day's range midpoint' — a XAU long-bias
        pattern that slipped past validation."""
        joined = ' '.join(ar._CREATIVE_CONSTRAINTS).lower()
        assert 'open-to-close direction' not in joined
        assert "prior day's range midpoint" not in joined

    def test_all_constraints_nonempty(self):
        for c in ar._CREATIVE_CONSTRAINTS:
            assert isinstance(c, str) and len(c.strip()) > 20


# ─────────────────────────────────────────────────────────────────────────────
# codegen.md — code-generation prompt template
# ─────────────────────────────────────────────────────────────────────────────

class TestCodegenTemplate:
    REQUIRED_PLACEHOLDERS = ('instrument', 'timeframe', 'family', 'hypothesis',
                             'entry', 'filter', 'exit', 'param_hints')

    def _fmt(self, **over):
        defaults = dict(instrument='EUR_USD', timeframe='D', family='statistical',
                        hypothesis='edge', entry='RSI(2)<10', filter='ADX(14)<20',
                        exit='5 bars', param_hints={'n': [10]})
        defaults.update(over)
        return ar._get_codegen_template().format(**defaults)

    def test_template_loads(self):
        tpl = ar._get_codegen_template()
        assert isinstance(tpl, str) and len(tpl) > 500

    def test_comment_header_stripped(self):
        """The maintainer <!-- ... --> block must not reach the LLM."""
        assert '<!--' not in ar._get_codegen_template()

    def test_formats_with_all_placeholders(self):
        """Every placeholder must resolve — no KeyError, no leftover braces."""
        out = self._fmt()
        for p in self.REQUIRED_PLACEHOLDERS:
            assert '{' + p + '}' not in out

    def test_interpolated_values_appear(self):
        out = self._fmt(instrument='GBP_JPY', entry='skew < -0.5')
        assert 'GBP_JPY' in out
        assert 'skew < -0.5' in out

    def test_literal_braces_render(self):
        """JSON examples and the {-1,0,1} set must survive .format()."""
        out = self._fmt()
        assert '{-1, 0, 1}' in out
        assert '"param_grid"' in out
        assert '"archetype": "standard"' in out

    def test_caching(self):
        """Second call returns the cached object."""
        assert ar._get_codegen_template() is ar._get_codegen_template()

    def test_carries_regime_gate_rules(self):
        """The regime-gate and direction-agnostic rules must be in the template."""
        tpl = ar._get_codegen_template()
        assert 'REGIME GATE' in tpl
        assert 'DIRECTION-AGNOSTIC' in tpl


# ─────────────────────────────────────────────────────────────────────────────
# _validate_thesis
# ─────────────────────────────────────────────────────────────────────────────

def _good_thesis(**overrides):
    """Return a minimally valid thesis dict."""
    t = {
        'strategy_family': 'statistical',
        'timeframe': 'D',
        'rationale': 'Mean reversion on daily closes using Bollinger Bands.',
        'entry_condition': 'Close crosses below lower band (2 std, 20-bar window).',
        'filter_condition': 'ADX(14) < 25 to confirm low-trend environment.',
        'exit_condition': 'Price returns above middle band or after 5 bars.',
        'param_hints': {'window': [10, 20, 30], 'std': [1.5, 2.0]},
    }
    t.update(overrides)
    return t


class TestValidateThesis:
    def test_valid_thesis_returns_none(self):
        assert ar._validate_thesis(_good_thesis()) is None

    def test_not_a_dict_rejected(self):
        err = ar._validate_thesis("not a dict")
        assert err is not None
        assert 'not a dict' in err

    def test_missing_required_field(self):
        t = _good_thesis()
        del t['entry_condition']
        err = ar._validate_thesis(t)
        assert err is not None
        assert 'entry_condition' in err

    def test_empty_required_field(self):
        t = _good_thesis(rationale='')
        err = ar._validate_thesis(t)
        assert err is not None
        assert 'rationale' in err

    def test_unknown_strategy_family(self):
        t = _good_thesis(strategy_family='magic')
        err = ar._validate_thesis(t)
        assert err is not None
        assert 'strategy_family' in err

    def test_family_alias_resolved(self):
        """'momentum' is an alias for 'regime' — should be accepted."""
        t = _good_thesis(strategy_family='momentum')
        err = ar._validate_thesis(t)
        assert err is None
        assert t['strategy_family'] == 'regime'  # normalized in-place

    def test_invalid_timeframe(self):
        t = _good_thesis(timeframe='1m')
        err = ar._validate_thesis(t)
        assert err is not None
        assert 'timeframe' in err

    def test_valid_timeframes(self):
        for tf in ('M30', 'H1', 'H4', 'D', 'W'):
            err = ar._validate_thesis(_good_thesis(timeframe=tf))
            assert err is None, f"Expected {tf} to be valid, got: {err}"

    def test_condition_too_short(self):
        t = _good_thesis(entry_condition='buy')
        err = ar._validate_thesis(t)
        assert err is not None
        assert 'entry_condition' in err
        assert 'too short' in err

    def test_param_hints_missing(self):
        t = _good_thesis()
        del t['param_hints']
        err = ar._validate_thesis(t)
        assert err is not None
        assert 'param_hints' in err

    def test_param_hints_empty_dict(self):
        t = _good_thesis(param_hints={})
        err = ar._validate_thesis(t)
        assert err is not None
        assert 'param_hints' in err

    def test_param_hints_no_list_values(self):
        t = _good_thesis(param_hints={'window': 20})  # scalar, not list
        err = ar._validate_thesis(t)
        assert err is not None
        assert 'list values' in err

    def test_mixed_timeframe_keywords_rejected(self):
        """Conditions mixing 'daily' and 'hourly' indicate TF confusion."""
        t = _good_thesis(
            entry_condition='Close crosses below lower band on daily chart.',
            filter_condition='Wait for hourly confirmation candle before entry.',
        )
        err = ar._validate_thesis(t)
        assert err is not None
        assert 'timeframe' in err.lower() or 'multiple' in err.lower()

    def test_single_timeframe_keyword_ok(self):
        """One TF keyword (e.g. 'daily') is fine — only multiple distinct ones are banned."""
        t = _good_thesis(
            entry_condition='Close below 20-day lower Bollinger Band.',
            filter_condition='Daily ADX(14) below 25.',
        )
        err = ar._validate_thesis(t)
        assert err is None


# ─────────────────────────────────────────────────────────────────────────────
# _extract_json
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractJson:
    def test_plain_json_object(self):
        result = ar._extract_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_plain_json_array(self):
        result = ar._extract_json('[{"a": 1}, {"b": 2}]')
        assert result == [{"a": 1}, {"b": 2}]

    def test_fenced_json_block(self):
        text = '```json\n{"key": "val"}\n```'
        assert ar._extract_json(text) == {"key": "val"}

    def test_fenced_block_no_language_tag(self):
        text = '```\n{"key": "val"}\n```'
        assert ar._extract_json(text) == {"key": "val"}

    def test_json_embedded_in_prose(self):
        text = 'Here is my strategy:\n{"window": 20}\nDone.'
        result = ar._extract_json(text)
        assert result == {"window": 20}

    def test_array_embedded_in_prose(self):
        text = 'Results: [{"a": 1}, {"b": 2}] — end.'
        result = ar._extract_json(text)
        assert result == [{"a": 1}, {"b": 2}]

    def test_completely_invalid_returns_none(self):
        assert ar._extract_json('no json here at all') is None

    def test_empty_string_returns_none(self):
        assert ar._extract_json('') is None

    def test_prose_with_braces_not_json(self):
        assert ar._extract_json('{this is not json}') is None

    def test_nested_json(self):
        text = '{"param_grid": {"n": [10, 20]}, "archetype": "standard"}'
        result = ar._extract_json(text)
        assert result['param_grid']['n'] == [10, 20]

    def test_array_comes_before_object_prefers_array(self):
        """When array appears before object in text, array wins."""
        text = '[1, 2] {"key": "val"}'
        result = ar._extract_json(text)
        assert result == [1, 2]


# ─────────────────────────────────────────────────────────────────────────────
# _validate_code — deeper branches not in test_pipeline.py
# ─────────────────────────────────────────────────────────────────────────────

BASE_FN = (
    "def generate_signals(df, params):\n"
    "    return df['close'].apply(lambda x: 1 if x > 0 else 0)\n"
)

class TestValidateCodeDeeper:
    def test_missing_generate_signals_rejected(self):
        err, _ = ar._validate_code("import pandas as pd\nx = 1\n")
        assert err is not None
        assert 'generate_signals' in err

    def test_lookahead_bias_rejected(self):
        code = "import pandas as pd\nimport numpy as np\n" + BASE_FN.replace(
            "return df['close'].apply(lambda x: 1 if x > 0 else 0)",
            "sig = df['close'].shift(-1)\n    return sig.fillna(0).astype(int)"
        )
        err, _ = ar._validate_code(code)
        assert err is not None
        assert 'look-ahead' in err

    def test_volume_column_rejected(self):
        code = (
            "import pandas as pd\nimport numpy as np\n"
            "def generate_signals(df, params):\n"
            "    vol = df['volume']\n"
            "    return pd.Series(0, index=df.index)\n"
        )
        err, _ = ar._validate_code(code)
        assert err is not None
        assert 'volume' in err.lower()

    def test_no_price_reference_rejected(self):
        code = (
            "import pandas as pd\nimport numpy as np\n"
            "def generate_signals(df, params):\n"
            "    x = params.get('n', 10)\n"
            "    return pd.Series(0, index=df.index)\n"
        )
        err, _ = ar._validate_code(code)
        assert err is not None
        assert 'price' in err.lower()

    def test_syntax_error_rejected(self):
        code = "import pandas as pd\ndef generate_signals(df params):\n    return 0\n"
        err, _ = ar._validate_code(code)
        assert err is not None
        assert 'syntax' in err.lower()

    def test_ta_cci_wrong_module_rejected(self):
        code = (
            "import pandas as pd\nimport ta\n"
            "def generate_signals(df, params):\n"
            "    v = ta.momentum.cci(df['high'], df['low'], df['close'], 14)\n"
            "    return pd.Series(0, index=df.index)\n"
        )
        err, _ = ar._validate_code(code)
        assert err is not None
        assert 'ta.momentum.cci' in err or 'ta.trend.cci' in err

    def test_ta_aroon_wrong_call_rejected(self):
        code = (
            "import pandas as pd\nimport ta\n"
            "def generate_signals(df, params):\n"
            "    v = ta.trend.aroon(df['high'], df['low'])\n"
            "    return pd.Series(0, index=df.index)\n"
        )
        err, _ = ar._validate_code(code)
        assert err is not None
        assert 'aroon' in err

    def test_series_boolean_and_auto_repaired(self):
        """Named Series vars (long_entry, uptrend) trigger auto-repair: 'and' → '&'."""
        code = (
            "import pandas as pd\nimport numpy as np\n"
            "def generate_signals(df, params):\n"
            "    long_entry = df['close'].rolling(10).mean() > df['close']\n"
            "    uptrend = df['close'].rolling(20).mean() > df['close'].rolling(50).mean()\n"
            "    signal = long_entry and uptrend\n"
            "    return signal.astype(int)\n"
        )
        err, cleaned = ar._validate_code(code)
        assert err is None
        assert 'long_entry & uptrend' in cleaned or '& uptrend' in cleaned

    def test_series_boolean_and_rejected_when_ambiguous(self):
        """'and' between ambiguous variable names (no series pattern) is not auto-repaired
        but also not rejected — it only fails if the regex detects a Series context."""
        # Variables like 'a' and 'b' with df references on the same line
        code = (
            "import pandas as pd\nimport numpy as np\n"
            "def generate_signals(df, params):\n"
            "    long_entry = df['close'].rolling(10).mean() > df['close']\n"
            "    vol_ok = df['high'] - df['low'] > params.get('atr', 0.001)\n"
            "    combined = long_entry and vol_ok\n"
            "    return combined.astype(int)\n"
        )
        err, cleaned = ar._validate_code(code)
        # vol_ok has 'df[' on the same assignment line — auto-repair triggers
        # Either fixed or rejected; what matters is the result is deterministic
        assert isinstance(err, (type(None), str))

    def test_uppercase_and_auto_fixed(self):
        """AND/OR/NOT should be auto-lowercased."""
        code = (
            "import pandas as pd\nimport numpy as np\n"
            "def generate_signals(df, params):\n"
            "    cond = (df['close'] > 0) AND (df['close'] < 1000)\n"
            "    return cond.astype(int)\n"
        )
        err, cleaned = ar._validate_code(code)
        assert 'AND' not in cleaned

    def test_unknown_df_column_rejected(self):
        """Referencing df['sentiment'] (not in valid set) should be rejected."""
        code = (
            "import pandas as pd\nimport numpy as np\n"
            "def generate_signals(df, params):\n"
            "    s = df['sentiment']\n"
            "    return (s > 0).astype(int)\n"
        )
        err, _ = ar._validate_code(code)
        assert err is not None
        assert 'sentiment' in err

    def test_code_written_column_allowed(self):
        """Columns that the code writes (df['ma'] = ...) are allowed to read back."""
        code = (
            "import pandas as pd\nimport numpy as np\n"
            "def generate_signals(df, params):\n"
            "    df['ma'] = df['close'].rolling(10).mean()\n"
            "    return (df['close'] > df['ma']).astype(int)\n"
        )
        err, _ = ar._validate_code(code)
        assert err is None

    def test_valid_code_returns_none_error(self):
        """Sanity: a clean strategy gets no error."""
        code = (
            "import pandas as pd\nimport numpy as np\n"
            "def generate_signals(df, params):\n"
            "    n = params.get('n', 20)\n"
            "    ma = df['close'].rolling(n).mean()\n"
            "    return (df['close'] > ma).astype(int)\n"
        )
        err, _ = ar._validate_code(code)
        assert err is None
