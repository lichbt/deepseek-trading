"""
Auto Research: Automated strategy generation + validation loop.
Uses OpenRouter (Gemini Flash / Claude) to generate candidates, then runs them
through the validator, records results, and iterates.

Usage:
    python auto_research.py --target 3 --max-iter 20 --instrument EUR_USD

Or programmatically:
    from auto_research import AutoResearcher
    ar = AutoResearcher(instruments=['EUR_USD'])
    ar.run(target_passed=3, max_iterations=20)
"""

import os
import sys
import json
import time
import hashlib
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests

import pipeline_utils as pu
from validator import validate_strategy, create_strategy_function
from telegram_bot import (
    notify_research_start,
    notify_iteration,
    notify_research_complete,
)


# ============================================================================
# CONFIGURATION
# ============================================================================

OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY', '')
OPENROUTER_BASE = 'https://openrouter.ai/api/v1'

# Thesis generation: free OpenRouter model (rate-limited but no cost)
THESIS_MODEL = 'openai/gpt-oss-120b:free'
THESIS_FALLBACK = 'google/gemini-2.5-flash'

# Code generation: claude CLI (uses Pro plan subscription, no API cost)
CLAUDE_CLI = os.getenv('CLAUDE_CLI', '/Users/lich/.local/bin/claude')
CLAUDE_CODE_MODEL = 'claude-sonnet-4-6'

# Fallback for code generation when Claude CLI is rate-limited or unavailable.
# openrouter/auto:free lets OpenRouter pick the best available free model automatically.
CODE_FALLBACK_MODELS = [
    'openrouter/auto:free',
    'openai/gpt-oss-120b:free',   # explicit backup if auto:free is unavailable
]

# Creative constraints rotated per iteration — forces structural diversity in thesis proposals.
# Wild mode (every 8th iteration) overrides the constraint with an open exploration directive.
_CREATIVE_CONSTRAINTS = [
    "Must avoid all moving-average crossover logic. Use price-relative or range-based entry instead.",
    "Entry must be based on a statistical property (skewness, kurtosis, or autocorrelation).",
    "Use only day-of-week or time-of-session effects — no rolling indicator windows.",
    "Build a two-instrument spread strategy. Use the spread as the signal, not individual price.",
    "Exit must be purely time-based (fixed bar count). No price-based stop.",
    "Entry only on breakout above/below a quantile of the last N bars' range.",
    "Strategy must be mean-reverting in entry but momentum-confirming in filter.",
    "Use an asymmetric parameter grid: longs and shorts use different lookbacks.",
    "Signal must come from comparing current bar range to historical bar range distribution.",
    "Base entry on open-to-close direction relative to prior day's range midpoint.",
]

# Legacy: kept for fallback
DEFAULT_MODEL = THESIS_MODEL
FALLBACK_MODEL = THESIS_FALLBACK

# Max previous failures to include in context (keep small to avoid context overflow)
MAX_FAILURE_CONTEXT = 3

# Fallback prompt if program.md is missing
DEFAULT_PROMPT = """You are a quantitative trading strategy researcher.
Output ONLY valid JSON with keys: strategy_id, code, param_grid, rationale.
Code must define generate_signals(df, params) and return pd.Series of int values in {-1,0,1}.
Do not use future data or volume."""

# Output directory for generated candidates
CANDIDATE_DIR = Path(__file__).parent / '.auto-research-candidates'


# ============================================================================
# PROMPT BUILDER
# ============================================================================

def _build_system_prompt() -> str:
    # Load instructions from program.md
    program_path = Path(__file__).parent / 'program.md'
    if program_path.exists():
        with open(program_path) as f:
            return f.read().strip()
    # Fallback to hardcoded prompt
    return DEFAULT_PROMPT


def _get_research_phase() -> str:
    """Extract current research directives from program.md markers."""
    program_path = Path(__file__).parent / 'program.md'
    if not program_path.exists():
        return ''
    text = program_path.read_text()
    start = text.find('<!-- RESEARCH_PHASE_START -->')
    end   = text.find('<!-- RESEARCH_PHASE_END -->')
    if start == -1 or end == -1:
        return ''
    lines = text[start + len('<!-- RESEARCH_PHASE_START -->'):end].strip()
    return lines if lines else ''
def _shorten(text: str, limit: int = 180) -> str:
    if not text:
        return 'none'
    txt = str(text).strip().replace('\n', ' ')
    return txt if len(txt) <= limit else txt[:limit] + '...'


def _build_user_prompt(
    instrument: str,
    failed_strategies: List[Dict],
    iteration: int
) -> str:
    """Build user prompt with compact failure context to avoid token blowups."""
    lines = [
        f'Generate a new trading strategy for {instrument}.',
        f'This is iteration {iteration}.',
        '',
    ]

    if failed_strategies:
        lines.append('=== PREVIOUSLY FAILED STRATEGIES (DO NOT REPEAT) ===')
        for fs in failed_strategies[:MAX_FAILURE_CONTEXT]:
            fs_status = _shorten(fs.get('final_status', fs.get('status', 'unknown')), 120)
            rationale = _shorten(fs.get('rationale', 'none'), 180)
            lines.append(f'- ID: {fs["id"]} | Status: {fs_status} | Rationale: {rationale}')
            scores = []
            if fs.get('is_gt_score') is not None:
                scores.append(f'IS={fs["is_gt_score"]:.2f}')
            if fs.get('wf_gt_score') is not None:
                scores.append(f'WF={fs["wf_gt_score"]:.2f}')
            if fs.get('ho_gt_score') is not None:
                scores.append(f'HO={fs["ho_gt_score"]:.2f}')
            if scores:
                lines.append(f'  Scores: {", ".join(scores)}')
        lines.append('')
        lines.append('Propose a genuinely DIFFERENT hypothesis. Do NOT tweak parameters of a failed strategy.')
    else:
        lines.append('No prior failures. Propose a fresh, economically-grounded strategy.')

    lines.append('')
    lines.append('Output ONLY valid JSON with keys: strategy_id, code, param_grid, rationale, timeframe.')
    

    return '\n'.join(lines)


# ============================================================================
# LLM CLIENT
# ============================================================================

def _estimate_tokens(text: str) -> int:
    # rough estimate: ~4 chars/token for English/code mix
    return max(1, len(text) // 4)


def call_openrouter(
    system_prompt: str,
    user_prompt: str,
    model: str = DEFAULT_MODEL,
    api_key: str = None,
    temperature: float = 0.7,
    max_tokens: int = 2048
) -> Dict[str, Any]:
    """
    Call OpenRouter API and return parsed JSON response.

    Returns:
        {'success': bool, 'candidate': dict or None, 'error': str or None}
    """
    key = api_key or OPENROUTER_API_KEY
    if not key:
        return {'success': False, 'candidate': None, 'error': 'OPENROUTER_API_KEY not set'}

    estimated_prompt_tokens = _estimate_tokens(system_prompt) + _estimate_tokens(user_prompt)
    print(f'  Prompt size: ~{estimated_prompt_tokens} tokens', flush=True)

    # Guardrail against runaway prompt growth
    if estimated_prompt_tokens > 12000:
        return {
            'success': False,
            'candidate': None,
            'error': f'Prompt too large (~{estimated_prompt_tokens} tokens). Trim failure context.'
        }

    headers = {
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
    }

    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        'temperature': temperature,
        'max_tokens': max_tokens,
    }

    try:
        resp = requests.post(
            f'{OPENROUTER_BASE}/chat/completions',
            headers=headers,
            json=payload,
            timeout=60
        )
        resp.raise_for_status()
        data = resp.json()
        content = data['choices'][0]['message']['content']

        candidate = _extract_json(content)
        if candidate is None:
            return {'success': False, 'candidate': None, 'error': f'Failed to parse JSON: {content[:200]}'}

        return {'success': True, 'candidate': candidate, 'error': None}

    except requests.exceptions.HTTPError as e:
        try:
            err_body = resp.text[:500]
            return {'success': False, 'candidate': None, 'error': f'HTTP {resp.status_code}: {err_body}'}
        except Exception:
            return {'success': False, 'candidate': None, 'error': f'HTTP error: {e}'}
    except requests.exceptions.Timeout:
        return {'success': False, 'candidate': None, 'error': 'OpenRouter timeout'}
    except requests.exceptions.RequestException as e:
        return {'success': False, 'candidate': None, 'error': f'API error: {e}'}
    except Exception as e:
        return {'success': False, 'candidate': None, 'error': f'Unexpected error: {e}'}


def _seconds_until_claude_reset(output: str) -> int:
    """Parse 'resets Xpm (Asia/Saigon)' from claude CLI output and return seconds to wait."""
    import re
    from datetime import datetime, timedelta

    m = re.search(r'resets\s+(\d+(?::\d+)?)\s*(am|pm)', output, re.IGNORECASE)
    if m:
        parts = m.group(1).split(':')
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        ampm = m.group(2).lower()
        if ampm == 'pm' and hour != 12:
            hour += 12
        elif ampm == 'am' and hour == 12:
            hour = 0

        # Asia/Saigon = UTC+7
        now_utc = datetime.utcnow()
        now_saigon = now_utc + timedelta(hours=7)
        reset_saigon = now_saigon.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reset_saigon <= now_saigon:
            reset_saigon += timedelta(days=1)
        return max(60, int((reset_saigon - now_saigon).total_seconds()))

    return 3600  # default: wait 1 hour if we can't parse


_CODE_SYSTEM_PROMPT = (
    "You are a quantitative trading strategy coder. "
    "Output ONLY valid JSON with keys: strategy_id, code, param_grid, rationale, timeframe, instrument."
)


def call_code_fallback(prompt: str, api_key: str = None) -> Dict[str, Any]:
    """
    Try CODE_FALLBACK_MODELS in order when Claude CLI is unavailable.
    Returns the first successful result, or the last error if all fail.
    """
    last_error = 'No fallback models configured'
    for model in CODE_FALLBACK_MODELS:
        print(f'  [Fallback] Trying {model}...', flush=True)
        result = call_openrouter(
            system_prompt=_CODE_SYSTEM_PROMPT,
            user_prompt=prompt,
            model=model,
            api_key=api_key or OPENROUTER_API_KEY,
            temperature=0.3,   # lower temp for code — we want precision not creativity
            max_tokens=3000,
        )
        if result['success']:
            print(f'  [Fallback] {model} succeeded', flush=True)
            return result
        last_error = result['error']
        # Skip to next model on rate-limit or model-unavailable errors
        if '429' in last_error or 'unavailable' in last_error.lower() or 'overloaded' in last_error.lower():
            print(f'  [Fallback] {model} rate-limited/unavailable, trying next...', flush=True)
            continue
        # For other errors (bad JSON, etc.) also try next
        print(f'  [Fallback] {model} failed: {last_error[:120]}', flush=True)
    return {'success': False, 'candidate': None, 'error': f'All fallback models failed. Last: {last_error}'}


def call_claude_cli(prompt: str, max_retries: int = 2, api_key: str = None) -> Dict[str, Any]:
    """
    Generate strategy code using the claude CLI (Pro plan, no API cost).
    Falls back to CODE_FALLBACK_MODELS immediately if the CLI is rate-limited or unavailable.
    Returns {'success': bool, 'candidate': dict or None, 'error': str or None}
    """
    import subprocess
    full_prompt = _CODE_SYSTEM_PROMPT + '\n\n' + prompt

    for attempt in range(max_retries):
        try:
            result = subprocess.run(
                [CLAUDE_CLI, '--model', 'sonnet', '-p', full_prompt],
                capture_output=True, text=True, timeout=300
            )
            combined = result.stdout + result.stderr

            if 'hit your limit' in combined or 'usage limit' in combined.lower():
                reset_secs = _seconds_until_claude_reset(combined)
                h, m = divmod(reset_secs, 3600)
                print(f'  Claude CLI limit reached (resets in {h}h {m//60}m) — using OpenRouter fallback', flush=True)
                return call_code_fallback(prompt, api_key=api_key)

            if result.returncode != 0:
                err = (result.stderr or result.stdout).strip()[:300]
                # Auth / binary / quota errors → fall back immediately, don't retry
                if any(x in err.lower() for x in (
                    'not logged in', 'authenticate', '401', 'invalid',
                    'extra usage', '1m context', 'extended context',
                )):
                    print(f'  Claude CLI error (fallback triggered): {err[:120]}', flush=True)
                    return call_code_fallback(prompt, api_key=api_key)
                return {'success': False, 'candidate': None, 'error': f'claude CLI error: {err}'}

            candidate = _extract_json(result.stdout)
            if candidate is None:
                if attempt < max_retries - 1:
                    continue
                return {'success': False, 'candidate': None, 'error': f'Failed to parse JSON: {result.stdout[:200]}'}
            return {'success': True, 'candidate': candidate, 'error': None}

        except subprocess.TimeoutExpired:
            print(f'  Claude CLI timed out — using OpenRouter fallback', flush=True)
            return call_code_fallback(prompt, api_key=api_key)
        except Exception as e:
            return {'success': False, 'candidate': None, 'error': f'Unexpected error: {e}'}

    return {'success': False, 'candidate': None, 'error': 'Max retries exceeded'}


def _validate_code(code: str) -> tuple:
    """Validate strategy code before execution. Returns (error_str_or_None, cleaned_code)."""
    if not code or 'generate_signals' not in code:
        return ('missing generate_signals function', code)

    import re
    code_clean = code

    # Fix uppercase AND/OR/NOT (Python uses lowercase)
    code_clean = re.sub(r'\bAND\b', 'and', code_clean)
    code_clean = re.sub(r'\bOR\b', 'or', code_clean)
    code_clean = re.sub(r'\bNOT\b', 'not', code_clean)

    # Auto-repair: simple unambiguous Series boolean patterns
    # Pattern: (series_expr) and (series_expr) -> (series_expr) & (series_expr)
    # Only repair when both sides clearly look like Series (have brackets/parentheses/df.column)
    # Be conservative: only match clear patterns, let ambiguous ones fail
    code_clean = re.sub(
        r'\(([^)]+)\)\s+and\s+\(([^)]+)\)',
        lambda m: f'({m.group(1)}) & ({m.group(2)})',
        code_clean
    )
    code_clean = re.sub(
        r'\(([^)]+)\)\s+or\s+\(([^)]+)\)',
        lambda m: f'({m.group(1)}) | ({m.group(2)})',
        code_clean
    )

    # After auto-repair, reject ANY remaining and/or in assignment/boolean contexts
    # These patterns indicate Series boolean misuse that auto-repair didn't catch
    # Match: "series_expr and series_expr" without parentheses on BOTH sides
    # Exclude: "if bool(...)" and "if ...:" (scalar contexts), ".iloc[i]" (scalar access)
    lines = code_clean.split('\n')
    for i, line in enumerate(lines, 1):
        # Skip comment lines
        if line.strip().startswith('#'):
            continue
        # Skip if/while conditions (scalar contexts)
        if re.match(r'\s*if\s+bool\(', line):
            continue
        if re.match(r'\s*if\s+.*\.iloc\[', line):
            continue
        # Detect and/or in assignment or expression context
        # Pattern: any " and " or " or " that's NOT inside both parens
        if re.search(r'\band\b', line) or re.search(r'\bor\b', line):
            # If line has = or if it looks like a boolean combo, flag it
            if '=' in line or re.search(r'_entry|_filter|trend|vol_|long_|short_', line, re.IGNORECASE):
                return (f'line {i}: uses Python "and"/"or" between expressions (use "&" and "|" with parentheses)', code)

    # Also reject mixed bitwise + logical operators without explicit parens
    if re.search(r'&\s*(and|or)|(and|or)\s*&', code_clean):
        return ('mixed "&" and "and"/"or" without parentheses (precedence ambiguous; wrap in parens)', code)

    try:
        import ast
        ast.parse(code_clean)
    except SyntaxError as e:
        return (f'Invalid Python syntax: {e}', code)

    if 'shift(-1)' in code_clean:
        return ('uses look-ahead bias (shift(-1))', code)

    if 'df["volume"]' in code_clean or "df['volume']" in code_clean or 'df.volume' in code_clean:
        return ('references df volume column (does not exist in OHLC data)', code)
    if "'Volume'" in code_clean or '"Volume"' in code_clean:
        return ('references Volume column', code)

    # Detect references to non-OHLC columns (macro data that doesn't exist in the feed).
    # Rule: any df['col'] read where col is not in the valid set AND never written to in-code.
    from macro_fetcher import ALL_MACRO_COLS
    _VALID_DF_COLS = frozenset({
        'close', 'open', 'high', 'low', 'date',            # standard OHLC
        'spread', 'event_impact', 'event_surprise',         # news archetype
        'session',                                          # session archetype
        'close_leg2',                                       # pair archetype
    }) | ALL_MACRO_COLS                                     # macro archetype
    all_refs  = set(re.findall(r'df\[["\'](\w+)["\']\]', code_clean))
    write_refs = set(re.findall(r'df\[["\'](\w+)["\']\]\s*=', code_clean))
    external_reads = all_refs - write_refs
    bad_cols = external_reads - _VALID_DF_COLS
    if bad_cols:
        return (
            f'references non-OHLC columns not available in dataframe: {sorted(bad_cols)}',
            code
        )
    if 'import talib' in code_clean:
        return ('uses talib instead of ta library', code)
    if 'import pandas' not in code_clean and 'import pd' not in code_clean:
        return ('missing import pandas / import pd', code)
    has_ta = 'import ta' in code_clean or 'from ta' in code_clean
    has_np = 'import numpy' in code_clean or 'import np' in code_clean
    if not has_ta and not has_np:
        return ('missing import ta or import numpy (need at least one)', code)
    _price_refs = ('df.low', 'df.high', 'df.close', 'df.open',
                   'df["close"]', "df['close']", 'df["high"]', "df['high']",
                   'df["low"]', "df['low']", 'df["open"]', "df['open']",
                   'df["Close"]', 'df["High"]', 'df["Low"]', 'df["Open"]')
    if not any(ref in code_clean for ref in _price_refs):
        return ('never references price data (close/high/low)', code)

    if 'ta.momentum.cci' in code_clean:
        return ('use ta.trend.cci NOT ta.momentum.cci', code)
    if 'ta.trend.aroon[' in code_clean or 'ta.trend.aroon(' in code_clean:
        return ('use ta.trend.aroon_up() and ta.trend.aroon_down() (returns Series)', code)
    if 'ta.volatility.supertrend' in code_clean:
        return ('use ta.trend.supertrendindicator from ta.trend', code)
    if 'ta.trend.williams' in code_clean:
        return ('use ta.momentum.williams_r', code)

    return (None, code_clean)


def _validate_basic_signals(code: str, param_grid: dict, min_signals: int = 5) -> Optional[str]:
    """
    Validate that a strategy generates enough signals on real data.
    Quick sanity check: try first param combo on recent data.
    Returns None if OK, error string if not.

    Minimum 5 signals: WF validation has 5 windows, even 1 signal/window
    is enough to compute meaningful returns. Validation gates (IS/WF/HO)
    will filter out bad strategies regardless of signal count.
    """
    import os
    from pathlib import Path
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        os.environ.setdefault('OANDA_ACCOUNT_ID', os.environ.get('OANDA_ACCOUNT_ID', ''))
        os.environ.setdefault('OANDA_API_TOKEN', os.environ.get('OANDA_API_TOKEN', ''))
        from data_fetcher import get_candles_date_range
    except Exception:
        return None  # Can't validate without data — skip

    ns = {}
    try:
        exec(code, ns)
    except Exception:
        return None  # let _validate_code catch this

    if 'generate_signals' not in ns:
        return None

    fn = ns['generate_signals']

    # Use first param combo
    first_params = {}
    for k, v in param_grid.items():
        if isinstance(v, list) and len(v) > 0:
            first_params[k] = v[0]
        else:
            first_params[k] = v

    # Test on 2019 data (medium dataset, no chunking needed)
    try:
        df = get_candles_date_range('EUR_USD', '2019-01-01', '2019-06-30', 'D')
    except Exception:
        return None  # data fetch issue — skip check

    if len(df) < 30:
        return None

    # Run strategy — surface runtime errors so they trigger a retry, not silent skip
    try:
        signals = fn(df, first_params)
    except Exception as e:
        return f'runtime error on first param combo: {type(e).__name__}: {e}'

    non_zero = int((signals != 0).sum())
    if non_zero < min_signals:
        return f'only {non_zero} signals (min {min_signals} needed)'

    return None


def _extract_json(text: str) -> Optional[Dict]:
    """Try to extract JSON from LLM output (supports fenced markdown JSON)."""
    text = text.strip()

    # Handle fenced blocks like ```json ... ``` and ``` ... ```
    if text.startswith('```'):
        lines = text.splitlines()
        if lines:
            first = lines[0].strip().lower()
            if first in ('```json', '```'):
                lines = lines[1:]
            if lines and lines[-1].strip() == '```':
                lines = lines[:-1]
            text = '\n'.join(lines).strip()

    # If model returned multiple fenced blocks, grab first json-looking block
    if '```' in text:
        for block in text.split('```'):
            candidate = block.strip()
            if not candidate:
                continue
            if candidate.lower().startswith('json'):
                candidate = candidate[4:].strip()
            if candidate.startswith('{') and candidate.endswith('}'):
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pass

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object between first { and last }
    start = text.find('{')
    end = text.rfind('}') + 1
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass

    return None


# ============================================================================
# AUTO RESEARCH LOOP
# ============================================================================

class AutoResearcher:
    """Automated research loop: generate → validate → record → repeat."""

    # Multi-instrument pool for diversity (FX majors, crosses, commodities)
    DEFAULT_INSTRUMENT_POOL = [
        'EUR_USD', 'GBP_USD', 'USD_JPY', 'USD_CHF',
        'AUD_USD', 'NZD_USD', 'EUR_GBP', 'EUR_JPY', 'GBP_JPY',
        'XAU_USD', 'XAG_USD', 'BCO_USD', 'WTICO_USD',
        'NATGAS_USD', 'CORN_USD', 'SOYBN_USD', 'WHEAT_USD',
        'BTC_USD', 'ETH_USD', 'LTC_USD',
    ]

    def __init__(
        self,
        instruments: List[str] = None,
        model: str = DEFAULT_MODEL,
        api_key: str = None,
        temperature: float = 0.7,
        min_delay_seconds: float = 2.0
    ):
        self.instruments = instruments or self.DEFAULT_INSTRUMENT_POOL
        self.model = model
        self.api_key = api_key or OPENROUTER_API_KEY
        self.temperature = temperature
        self.min_delay = min_delay_seconds

        # Ensure DB and candidate dir exist
        pu.init_db()
        CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)

    def _rotate_instrument(self, iteration: int) -> str:
        return self.instruments[iteration % len(self.instruments)]

    def _generate_strategy_id(self, prefix: str, iteration: int) -> str:
        ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        return f'{prefix}_auto_{ts}_i{iteration}'

    def _check_duplicate(self, candidate: Dict) -> Optional[str]:
        """Return existing status if fingerprint exists, else None."""
        code = candidate.get('code', '')
        param_grid = candidate.get('param_grid', {})
        fp = pu.compute_strategy_fingerprint(code, param_grid, candidate.get('timeframe', 'D'), candidate.get('instrument', ''))
        existing = pu.check_idea_is_new(fp)
        if not existing['new']:
            return existing.get('status', 'unknown')
        return None

    def _save_candidate(self, candidate: Dict, iteration: int) -> Path:
        """Save candidate JSON to disk."""
        fp = CANDIDATE_DIR / f'candidate_{iteration:03d}.json'
        with open(fp, 'w') as f:
            json.dump(candidate, f, indent=2)
        return fp

    def _validate_candidate(self, candidate: Dict) -> tuple:
        """Run validator on candidate. Returns (passed: bool, message: str)."""
        try:
            # Ensure instrument is set
            if 'instrument' not in candidate:
                candidate['instrument'] = self.instruments[0]
            return validate_strategy(candidate)
        except Exception as e:
            return False, f'Validator exception: {e}'

    def _get_scores(self, strategy_id: str) -> Dict[str, float]:
        """Get validation scores for a strategy from DB."""
        with pu.get_db_connection() as conn:
            c = conn.cursor()
            c.execute(
                'SELECT is_gt_score, walk_forward_gt_score, holdout_gt_score FROM validation_results WHERE strategy_id = ?',
                (strategy_id,)
            )
            row = c.fetchone()
            if row:
                return {
                    'is_score': row['is_gt_score'] or 0.0,
                    'wf_score': row['walk_forward_gt_score'] or 0.0,
                    'ho_score': row['holdout_gt_score'] or 0.0,
                }
            return {'is_score': 0.0, 'wf_score': 0.0, 'ho_score': 0.0}

    def run(
        self,
        target_passed: int = 3,
        max_iterations: int = 30,
        instruments: List[str] = None
    ) -> Dict[str, Any]:
        """
        Run the auto-research loop.

        Args:
            target_passed: Stop after this many strategies pass validation
            max_iterations: Maximum LLM calls before giving up
            instruments: Override instruments list

        Returns:
            Summary dict: {
                'iterations': int,
                'passed': [id, ...],
                'failed': [id, ...],
                'errors': int,
                'duration_seconds': float
            }
        """
        if instruments:
            self.instruments = instruments

        results = {
            'iterations': 0,
            'passed': [],
            'failed': [],
            'errors': 0,
            'start_time': datetime.utcnow().isoformat(),
        }
        start = time.time()

        print(f"\n{'='*70}")
        print(f"Auto Research Loop")
        print(f"Target: {target_passed} passed | Max iterations: {max_iterations}")
        print(f"Instruments: {self.instruments} | Model: {self.model}")
        print(f"{'='*70}\n")

        # Skip Telegram start notification — only send summary at end

        for iteration in range(1, max_iterations + 1):
            results['iterations'] = iteration

            if len(results['passed']) >= target_passed:
                print(f"\n✓ Target reached: {len(results['passed'])} strategies passed")
                break

            instrument = self._rotate_instrument(iteration)

            try:
                # Step 1: Query DB for failures
                failed = pu.get_failed_strategies()

                # Step 2: Build prompts (old single-step flow — kept for reference but not used)
                # system_prompt = _build_system_prompt()
                # user_prompt = _build_user_prompt(instrument, failed, iteration)

                # Step 3: Call LLM - Two-step generation
                # Step A: Generate thesis via free OpenRouter model
                # Step B: Generate code via claude CLI (Pro plan, no cost)
                print(f"\n[Iteration {iteration}/{max_iterations}] {instrument}", flush=True)
                print(f"  Step A: Generating thesis (free model)...", flush=True)

                # Build a minimal thesis prompt (separate from the code-gen prompt to avoid schema confusion)
                failed_ctx = ""
                if failed:
                    lines = ["Previously failed strategies (do not repeat):"]
                    for fs in failed[:5]:
                        lines.append(f"- {fs.get('rationale', '')[:120]}")
                    failed_ctx = "\n".join(lines) + "\n\n"

                # ── Research phase directives from meta-review ──────────────────────
                research_phase = _get_research_phase()
                phase_block = ""
                if research_phase:
                    phase_block = f"\nCURRENT RESEARCH DIRECTIVES (follow these):\n{research_phase}\n"

                # ── Creative constraint (rotates each iteration, wild every 8th) ───
                constraint = _CREATIVE_CONSTRAINTS[iteration % len(_CREATIVE_CONSTRAINTS)]
                wild = (iteration % 8 == 0)
                if wild:
                    constraint = (
                        "WILD MODE: Ignore conventional strategy families. "
                        "Propose something structurally different from anything tried before — "
                        "unusual timeframe, non-standard entry logic, exotic exit rule."
                    )
                mode_label = "WILD" if wild else f"constraint[{iteration % len(_CREATIVE_CONSTRAINTS)}]"
                print(f"  [{mode_label}] {constraint[:80]}...", flush=True)

                thesis_system = (
                    "You are a quantitative trading researcher. "
                    "Output ONLY valid JSON. No explanation, no preamble, no markdown."
                    "\n\nCONSTRAINT FOR THIS ITERATION: " + constraint
                )

                thesis_prompt = (
                    f"Instrument: {instrument}\n"
                    f"{phase_block}"
                    f"{failed_ctx}"
                    "Pick a STRATEGY FAMILY (one of: speed-based, cross-market, regime, flow-proxy, "
                    "event-driven, statistical, risk-factor) and design a precise trading strategy spec.\n\n"
                    "Reply with ONLY this JSON and nothing else:\n"
                    "{\n"
                    '  "strategy_family": "regime",\n'
                    '  "rationale": "One sentence — WHY this edge exists economically.",\n'
                    '  "entry_condition": "Exact measurable entry: which price/indicator relationship, threshold, lookback. Specific enough to code without ambiguity.",\n'
                    '  "filter_condition": "Regime or volatility filter that must be true before entry (e.g. ADX>25, ATR above 20-bar median, price above 200-bar MA). State exact threshold.",\n'
                    '  "exit_condition": "When and how to exit: target multiple, stop multiple, time-based bars, or indicator cross. State exact lookback or multiplier.",\n'
                    '  "param_hints": {"lookback": [10, 20, 30], "threshold": [0.5, 1.0, 1.5]}\n'
                    "}"
                )

                thesis_result = call_openrouter(
                    system_prompt=thesis_system,
                    user_prompt=thesis_prompt,
                    model=THESIS_MODEL,
                    api_key=self.api_key,
                    temperature=0.7,
                    max_tokens=600,
                )

                # On rate limit: wait and retry free model (extract retry_after if available)
                if not thesis_result['success']:
                    err = thesis_result['error']
                    if '429' in err or 'rate' in err.lower():
                        import re as _re
                        wait = 30
                        m = _re.search(r'retry_after_seconds["\s:]+(\d+)', err)
                        if m:
                            wait = int(m.group(1)) + 2
                        print(f"  ! Rate limited, waiting {wait}s and retrying free model...")
                        time.sleep(wait)
                        thesis_result = call_openrouter(
                            system_prompt=thesis_system,
                            user_prompt=thesis_prompt,
                            model=THESIS_MODEL,
                            api_key=self.api_key,
                            temperature=0.7,
                            max_tokens=600,
                        )
                    if not thesis_result['success']:
                        print(f"  ✗ Thesis error: {thesis_result['error']}")
                        results['errors'] += 1
                        time.sleep(self.min_delay)
                        continue
                    print(f"  ✓ Thesis retry succeeded")

                thesis_data = thesis_result['candidate']
                strategy_family = thesis_data.get('strategy_family', 'unknown')
                rationale = thesis_data.get('rationale', '')
                entry_cond  = thesis_data.get('entry_condition', '')
                filter_cond = thesis_data.get('filter_condition', '')
                exit_cond   = thesis_data.get('exit_condition', '')
                param_hints = thesis_data.get('param_hints', {})

                if not rationale:
                    print(f"  ✗ No rationale in thesis response")
                    results['errors'] += 1
                    continue

                print(f"  Strategy Family: {strategy_family}", flush=True)
                print(f"  Rationale: {rationale[:80]}...", flush=True)
                if entry_cond:
                    print(f"  Entry:     {entry_cond[:80]}...", flush=True)
                if filter_cond:
                    print(f"  Filter:    {filter_cond[:80]}...", flush=True)
                if exit_cond:
                    print(f"  Exit:      {exit_cond[:80]}...", flush=True)

                # Step B: Generate code via claude CLI (free via Pro plan)
                print(f"  Step B: Generating code (claude CLI)...", flush=True)

                code_prompt = f"""Implement this trading strategy EXACTLY as specified. Do NOT substitute generic indicators.

STRATEGY SPEC:
- Instrument:  {instrument}
- Family:      {strategy_family}
- Hypothesis:  {rationale}
- Entry:       {entry_cond if entry_cond else '(implement based on family and hypothesis)'}
- Filter:      {filter_cond if filter_cond else 'ATR above 20-bar median (low-volatility chop filter)'}
- Exit:        {exit_cond if exit_cond else 'Exit after 10 bars of no new signal or trailing stop'}
- Param hints: {param_hints if param_hints else '{{"lookback": [10, 20, 30]}}'}

Rules:
- Use ONLY pandas and numpy. No ta, talib, or external libraries.
- The Entry, Filter, and Exit conditions above are MANDATORY — implement each one literally.
- Build a param_grid sweeping the param_hints values (add ±1 variants where sensible).
- Grid size must stay ≤ 200 combinations.
- Define generate_signals(df, params) returning pd.Series of int in {{-1, 0, 1}}.
- Include explicit exit logic so the strategy exits during extended chop (no new signal after N bars).

Available df columns by archetype (choose one, set "archetype" key in JSON):
- standard  : close, open, high, low, date  (default — use pandas/numpy only)
- macro     : above + fed_rate, ecb_rate, boe_rate, boj_rate, rba_rate,
              us10y, eu10y, uk10y, jp10y, au10y, us_real_yield,
              us_cpi, eu_cpi, uk_cpi, jp_cpi, au_cpi, dxy
              (use when entry/filter depend on interest rates, yields, or CPI)
- session   : above + session ('London','New_York','Asian','Overlap','Closed')
- news      : above + event_impact ('high'/'medium'/'low'/'none'), event_surprise (float)
- pair      : above + close_leg2, spread  (also set "instrument2" key)

Output ONLY valid JSON with keys: strategy_id, code, param_grid, rationale, timeframe, archetype."""

                code_result = call_claude_cli(code_prompt)

                if not code_result['success']:
                    print(f"  ✗ Code generation error: {code_result['error']}")
                    results['errors'] += 1
                    time.sleep(self.min_delay)
                    continue

                candidate = code_result['candidate']

                candidate['strategy_id'] = self._generate_strategy_id(
                    instrument.lower().replace('_', ''), iteration
                )
                tf = candidate.get('timeframe', 'D')
                if tf is None or isinstance(tf, list):
                    tf = 'D'
                # Normalize common LLM timeframe variants to OANDA format
                _TF_MAP = {
                    '1H': 'H1', '4H': 'H4', '1D': 'D', '1W': 'W',
                    '30M': 'M30', '30m': 'M30', '1h': 'H1', '4h': 'H4',
                    'd': 'D', 'w': 'W', 'daily': 'D', 'weekly': 'W',
                    'hourly': 'H1', '1hour': 'H1', '4hour': 'H4',
                }
                tf = _TF_MAP.get(tf, tf)
                if tf not in ('M30', 'H1', 'H4', 'D', 'W'):
                    tf = 'D'  # safe default
                candidate['timeframe'] = tf

                # Step 4: Validate candidate structure
                required = ['strategy_id', 'code', 'param_grid', 'rationale', 'timeframe']
                missing = [k for k in required if k not in candidate]
                if missing:
                    print(f"  ✗ Missing keys: {missing}")
                    results['errors'] += 1
                    continue

                candidate['instrument'] = instrument

                # Override rationale with the approved thesis (keeps LLM honest)
                candidate['rationale'] = rationale

                # Step 5b: Validate code quality (with simple strategy enforcement)
                # Retry with SAME thesis anchored (prevents drift to new ideas)
                code_err, cleaned_code = _validate_code(candidate['code'])
                if code_err:
                    # Retry once with feedback - keep same thesis
                    print(f"  ! Code issue: {code_err}, retrying...")
                    fix_prompt = f"""The previous candidate had this code error: {code_err}

BROKEN CODE:
{candidate['code']}

THESIS (DO NOT CHANGE):
- Strategy Family: {strategy_family}
- Rationale: {rationale}

Fix ONLY the code error above. Use "&" and "|" instead of "and"/"or" for pandas boolean expressions.
Output ONLY valid JSON with keys: strategy_id, code, param_grid, rationale, timeframe."""

                    fix_result = call_claude_cli(fix_prompt)
                    if fix_result['success'] and fix_result['candidate']:
                        candidate = fix_result['candidate']
                        # Restore approved thesis
                        candidate['rationale'] = rationale
                        # Re-normalize timeframe from retry candidate
                        tf_retry = candidate.get('timeframe', 'D') or 'D'
                        tf_retry = _TF_MAP.get(tf_retry, tf_retry)
                        if tf_retry not in ('M30', 'H1', 'H4', 'D', 'W'):
                            tf_retry = 'D'
                        candidate['timeframe'] = tf_retry
                        code_err, cleaned_code = _validate_code(candidate['code'])
                        if code_err:
                            print(f"  ✗ Retry failed: {code_err}")
                            results['errors'] += 1
                            continue
                        # Use cleaned code
                        candidate['code'] = cleaned_code
                    else:
                        print(f"  ✗ Retry error: {fix_result.get('error', 'failed')}")
                        results['errors'] += 1
                        continue
                else:
                    candidate['code'] = cleaned_code

                # Step 4c: Skip signal count pre-filter
                # Validation gates (IS/WF/HO) will filter strategies with insufficient activity
                # We'll let the validator decide what's enough

                # Step 5: Check fingerprint dedup
                dup_status = self._check_duplicate(candidate)
                if dup_status:
                    print(f"  ✗ Duplicate fingerprint (status: {dup_status})")
                    results['failed'].append(candidate.get('strategy_id', 'unknown'))
                    time.sleep(self.min_delay)
                    continue

                candidate['instrument'] = instrument

                print(f"  Strategy: {candidate['strategy_id']}")
                print(f"  Rationale: {candidate.get('rationale', 'none')}")

                # Step 7: Save candidate
                json_path = self._save_candidate(candidate, iteration)
                print(f"  Saved to: {json_path}")

                # Step 8: Validate
                print(f"  Validating...")
                passed, message = self._validate_candidate(candidate)

                # Query scores from DB for notification
                sid = candidate['strategy_id']
                db_scores = self._get_scores(sid)

                if passed:
                    results['passed'].append(sid)
                    print(f"  ✓ PASS: {message}")
                    # Skip per-iteration Telegram notifications — only send summary at end
                else:
                    results['failed'].append(sid)
                    print(f"  ✗ {message}")
                    # Skip per-iteration Telegram notifications

                # Check for meta-review trigger (consecutive failures)
                if len(results['failed']) >= 15 and len(results['failed']) % 5 == 0:
                    print(f"\n[Meta-Review] {len(results['failed'])} consecutive failures, generating new directive...")
                    try:
                        import meta_review
                        meta_review.run_meta_review()
                    except Exception as e:
                        print(f"  Meta-review error: {e}")

                # Rate limit
                time.sleep(self.min_delay)

            except Exception as e:
                print(f"  ❌ Iteration {iteration} crashed: {e}")
                print("  Continuing to next iteration...")
                results['errors'] += 1
                time.sleep(self.min_delay)
                continue

        # Final summary
        elapsed = time.time() - start
        results['duration_seconds'] = elapsed

        print(f"\n{'='*70}")
        print(f"Auto Research Complete")
        print(f"{'='*70}")
        print(f"  Iterations:     {results['iterations']}")
        print(f"  Passed:         {len(results['passed'])}")
        for pid in results['passed']:
            print(f"    ✓ {pid}")
        print(f"  Failed:         {len(results['failed'])}")
        print(f"  Errors:         {results['errors']}")
        print(f"  Duration:       {elapsed:.0f}s")
        print(f"{'='*70}\n")

        notify_research_complete(results['iterations'], results['passed'],
                                 len(results['failed']), results['errors'],
                                 elapsed)

        return results


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Auto Research: Automated strategy generation + validation loop'
    )
    parser.add_argument(
        '--target', type=int, default=3,
        help='Stop after N strategies pass validation (default: 3)'
    )
    parser.add_argument(
        '--max-iter', type=int, default=30,
        help='Maximum LLM calls before giving up (default: 30)'
    )
    parser.add_argument(
        '--instrument', type=str, default=','.join(AutoResearcher.DEFAULT_INSTRUMENT_POOL),
        help='Instrument(s) to cycle through (default: all 11 in pool). Use commas for subset, e.g. EUR_USD,XAU_USD'
    )
    parser.add_argument(
        '--model', type=str, default=DEFAULT_MODEL,
        help=f'OpenRouter model (default: {DEFAULT_MODEL})'
    )
    parser.add_argument(
        '--temperature', type=float, default=0.7,
        help='LLM temperature (default: 0.7)'
    )
    parser.add_argument(
        '--api-key', type=str, default=None,
        help='OpenRouter API key (or set OPENROUTER_API_KEY env var)'
    )
    args = parser.parse_args()

    api_key = args.api_key or OPENROUTER_API_KEY
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY not set. Set env var or pass --api-key.")
        sys.exit(1)

    instruments = [i.strip() for i in args.instrument.split(',')]

    ar = AutoResearcher(
        instruments=instruments,
        model=args.model,
        api_key=api_key,
        temperature=args.temperature,
    )

    results = ar.run(
        target_passed=args.target,
        max_iterations=args.max_iter,
    )

    # Exit code: 0 if target reached, 2 if exhausted iterations
    sys.exit(0 if results['passed'] else 2)


if __name__ == '__main__':
    main()
